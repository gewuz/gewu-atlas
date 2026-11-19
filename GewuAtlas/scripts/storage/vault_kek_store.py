"""Pure-stdlib KEK + HKDF-SHA256 + XChaCha20-Poly1305 :memory: SQLite Vault registry."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import struct
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
CONTAINER_MAGIC = b"GWVA1"
SALT_LEN = 16
NONCE_LEN = 24
TAG_LEN = 16
VAULT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
HANDLE_RE = re.compile(r"^vlt_[0-9]{2,}$")
RESERVED_VAULT_IDS = frozenset({"all", "null", "none", "tmp", "temp", "trash", "default-reserved"})


class VaultStoreError(Exception):
    """Sanitized KEK vault store error."""

    def __init__(self, code: str, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def validate_rel_vault_path(rel_path: str) -> str:
    """Enforce strict relative POSIX path under Volumes/GewuAtlas-data/<vault>."""
    if not isinstance(rel_path, str) or not rel_path:
        raise VaultStoreError("E_PARAM_INVALID", "Vault 相对路径不能为空", exit_code=1)
    if rel_path.startswith("/") or "\\" in rel_path or ":" in rel_path or "\x00" in rel_path:
        raise VaultStoreError("E_PARAM_INVALID", "Vault 路径必须为合法相对 POSIX 路径", exit_code=1)
    p = PurePosixPath(rel_path)
    if str(p) != rel_path or p.is_absolute() or ".." in p.parts or "." in p.parts:
        raise VaultStoreError("E_PARAM_INVALID", "Vault 路径禁止越界或包含 '.' / '..'", exit_code=1)
    if len(p.parts) != 3 or p.parts[0] != "Volumes" or p.parts[1] != "GewuAtlas-data":
        raise VaultStoreError("E_PARAM_INVALID", "Vault 相对路径必须位于 Volumes/GewuAtlas-data/<vault>", exit_code=1)
    vid = p.parts[2]
    if not VAULT_ID_RE.fullmatch(vid) or vid in RESERVED_VAULT_IDS:
        raise VaultStoreError("E_PARAM_INVALID", f"非法或保留的 Vault 标识: {vid}", exit_code=1)
    return rel_path


def _rotl32(v: int, c: int) -> int:
    return ((v << c) & 0xFFFFFFFF) | (v >> (32 - c))


def _quarter_round(st: List[int], a: int, b: int, c: int, d: int) -> None:
    st[a] = (st[a] + st[b]) & 0xFFFFFFFF
    st[d] = _rotl32(st[d] ^ st[a], 16)
    st[c] = (st[c] + st[d]) & 0xFFFFFFFF
    st[b] = _rotl32(st[b] ^ st[c], 12)
    st[a] = (st[a] + st[b]) & 0xFFFFFFFF
    st[d] = _rotl32(st[d] ^ st[a], 8)
    st[c] = (st[c] + st[d]) & 0xFFFFFFFF
    st[b] = _rotl32(st[b] ^ st[c], 7)


def _chacha_core(state: List[int]) -> List[int]:
    w = list(state)
    for _ in range(10):
        _quarter_round(w, 0, 4, 8, 12)
        _quarter_round(w, 1, 5, 9, 13)
        _quarter_round(w, 2, 6, 10, 14)
        _quarter_round(w, 3, 7, 11, 15)
        _quarter_round(w, 0, 5, 10, 15)
        _quarter_round(w, 1, 6, 11, 12)
        _quarter_round(w, 2, 7, 8, 13)
        _quarter_round(w, 3, 4, 9, 14)
    return w


def _init_chacha_state(key: bytes, counter: int, nonce12: bytes) -> List[int]:
    consts = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)
    k = list(struct.unpack("<8I", key))
    n = list(struct.unpack("<3I", nonce12))
    return [*consts, *k, counter & 0xFFFFFFFF, *n]


def chacha20_block(key: bytes, counter: int, nonce12: bytes) -> bytes:
    st = _init_chacha_state(key, counter, nonce12)
    w = _chacha_core(st)
    out = [(w[i] + st[i]) & 0xFFFFFFFF for i in range(16)]
    return struct.pack("<16I", *out)


def hchacha20(key: bytes, nonce16: bytes) -> bytes:
    consts = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)
    k = list(struct.unpack("<8I", key))
    n = list(struct.unpack("<4I", nonce16))
    w = _chacha_core([*consts, *k, *n])
    return struct.pack("<8I", *w[0:4], *w[12:16])


def chacha20_xor(key: bytes, counter: int, nonce12: bytes, data: bytes) -> bytes:
    blocks = (len(data) + 63) // 64
    if counter < 0 or counter + blocks > 0x1_0000_0000:
        raise VaultStoreError("E_PARAM_INVALID", "ChaCha20 块计数器溢出，禁止密钥流复用", exit_code=2)
    out = bytearray(len(data))
    pos = 0
    blk_cnt = counter
    while pos < len(data):
        ks = chacha20_block(key, blk_cnt, nonce12)
        chunk = data[pos : pos + 64]
        for i, b in enumerate(chunk):
            out[pos + i] = b ^ ks[i]
        pos += 64
        blk_cnt += 1
    return bytes(out)


def poly1305_mac(msg: bytes, one_time_key: bytes) -> bytes:
    r = int.from_bytes(one_time_key[:16], "little") & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
    s = int.from_bytes(one_time_key[16:32], "little")
    p = (1 << 130) - 5
    acc = 0
    for i in range(0, len(msg), 16):
        block = msg[i : i + 16]
        n = int.from_bytes(block + b"\x01", "little")
        acc = ((acc + n) * r) % p
    tag_int = (acc + s) & ((1 << 128) - 1)
    return tag_int.to_bytes(16, "little")


def _pad16(data: bytes) -> bytes:
    rem = len(data) % 16
    return b"" if rem == 0 else b"\x00" * (16 - rem)


def aead_chacha20_poly1305_encrypt(key: bytes, nonce12: bytes, plaintext: bytes, aad: bytes) -> Tuple[bytes, bytes]:
    otk = chacha20_block(key, 0, nonce12)[:32]
    ciphertext = chacha20_xor(key, 1, nonce12, plaintext)
    mac_data = aad + _pad16(aad) + ciphertext + _pad16(ciphertext) + struct.pack("<QQ", len(aad), len(ciphertext))
    tag = poly1305_mac(mac_data, otk)
    return ciphertext, tag


def aead_chacha20_poly1305_decrypt(key: bytes, nonce12: bytes, ciphertext: bytes, aad: bytes, tag: bytes) -> bytes:
    otk = chacha20_block(key, 0, nonce12)[:32]
    mac_data = aad + _pad16(aad) + ciphertext + _pad16(ciphertext) + struct.pack("<QQ", len(aad), len(ciphertext))
    expected_tag = poly1305_mac(mac_data, otk)
    if not hmac.compare_digest(expected_tag, tag):
        raise VaultStoreError("E_HALT_SECURITY", "AEAD 认证校验失败（疑似密文篡改或跨区移植）", exit_code=2)
    return chacha20_xor(key, 1, nonce12, ciphertext)


def aead_xchacha20_poly1305_encrypt(key: bytes, nonce24: bytes, plaintext: bytes, aad: bytes) -> Tuple[bytes, bytes]:
    if len(key) != 32 or len(nonce24) != 24:
        raise VaultStoreError("E_HALT_SECURITY", "密钥或 Nonce 长度无效", exit_code=2)
    subkey = hchacha20(key, nonce24[:16])
    nonce12 = b"\x00\x00\x00\x00" + nonce24[16:24]
    return aead_chacha20_poly1305_encrypt(subkey, nonce12, plaintext, aad)


def aead_xchacha20_poly1305_decrypt(key: bytes, nonce24: bytes, ciphertext: bytes, aad: bytes, tag: bytes) -> bytes:
    if len(key) != 32 or len(nonce24) != 24 or len(tag) != 16:
        raise VaultStoreError("E_HALT_SECURITY", "密文信封格式非法", exit_code=2)
    subkey = hchacha20(key, nonce24[:16])
    nonce12 = b"\x00\x00\x00\x00" + nonce24[16:24]
    return aead_chacha20_poly1305_decrypt(subkey, nonce12, ciphertext, aad, tag)


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    prk = hmac.new(salt if salt else b"\x00" * 32, ikm, hashlib.sha256).digest()
    okm = bytearray()
    t = b""
    counter = 1
    while len(okm) < length:
        t = hmac.new(prk, t + info + bytes([counter]), hashlib.sha256).digest()
        okm.extend(t)
        counter += 1
    return bytes(okm[:length])


def _check_no_symlink(path: Path) -> None:
    for p in (path, path.parent, path.parent.parent):
        if p.is_symlink():
            raise VaultStoreError("E_HALT_SECURITY", "运行态密钥或状态路径拒绝符号链接", exit_code=2)


def _atomic_write_0600(path: Path, data: bytes) -> None:
    _check_no_symlink(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp_name = f".{path.name}.{secrets.token_hex(6)}.tmp"
    tmp_path = path.parent / tmp_name
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def load_or_init_kek(key_path: Path) -> bytes:
    """Load 32-byte KEK from GEWU_APP_KEK or protected 0600 key slot."""
    env_kek = os.environ.get("GEWU_APP_KEK", "").strip()
    if env_kek:
        if re.fullmatch(r"[0-9a-fA-F]{64}", env_kek):
            return bytes.fromhex(env_kek)
        return hashlib.sha256(b"GEWU_ATLAS_ENV_KEK|" + env_kek.encode("utf-8")).digest()

    _check_no_symlink(key_path)
    if key_path.exists():
        fd = os.open(key_path, os.O_RDONLY | _O_NOFOLLOW)
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read().strip()
        if len(raw) == 64 and re.fullmatch(rb"[0-9a-fA-F]{64}", raw):
            return bytes.fromhex(raw.decode("ascii"))
        if len(raw) == 32:
            return raw
        raise VaultStoreError("E_HALT_SECURITY", "KEK 密钥槽格式损坏", exit_code=2)

    kek = secrets.token_bytes(32)
    _atomic_write_0600(key_path, kek.hex().encode("ascii") + b"\n")
    return kek


class KekVaultRegistry:
    """Zero-disk double-encrypted :memory: SQLite registry for relative-path Vaults."""

    def __init__(self, app_id: str, enc_path: Path, kek: bytes) -> None:
        self.app_id = app_id
        self.enc_path = enc_path
        self.kek = kek
        if len(kek) != 32:
            raise VaultStoreError("E_HALT_SECURITY", "KEK 长度必须为 32 字节", exit_code=2)

    def _derive_keys(self, salt: bytes) -> Tuple[bytes, bytes]:
        db_key = hkdf_sha256(self.kek, salt, f"{self.app_id}|container-db|v1".encode("utf-8"), 32)
        field_dek = hkdf_sha256(self.kek, salt, f"{self.app_id}|vault-fields|v1".encode("utf-8"), 32)
        return db_key, field_dek

    def _container_aad(self) -> bytes:
        return f"GWVA1|{self.app_id}".encode("utf-8")

    def _field_aad(self, vault_id: str, key_version: int) -> bytes:
        return f"{self.app_id}||vaults||{vault_id}||v{key_version}".encode("utf-8")

    def _open_memory_db(self) -> Tuple[sqlite3.Connection, bytes]:
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA temp_store = MEMORY;")
        conn.execute(
            """
            CREATE TABLE vault_rows (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                handle TEXT UNIQUE NOT NULL,
                vault_id TEXT UNIQUE NOT NULL,
                key_version INTEGER NOT NULL,
                nonce_hex TEXT NOT NULL,
                ciphertext_b64 TEXT NOT NULL,
                tag_hex TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        _check_no_symlink(self.enc_path)
        if not self.enc_path.exists():
            return conn, secrets.token_bytes(SALT_LEN)

        fd = os.open(self.enc_path, os.O_RDONLY | _O_NOFOLLOW)
        with os.fdopen(fd, "rb") as fh:
            blob = fh.read()
        min_len = len(CONTAINER_MAGIC) + SALT_LEN + NONCE_LEN + TAG_LEN
        if len(blob) < min_len or blob[: len(CONTAINER_MAGIC)] != CONTAINER_MAGIC:
            conn.close()
            raise VaultStoreError("E_HALT_SECURITY", "加密容器头部校验失败", exit_code=2)

        off = len(CONTAINER_MAGIC)
        salt = blob[off : off + SALT_LEN]
        off += SALT_LEN
        nonce = blob[off : off + NONCE_LEN]
        off += NONCE_LEN
        tag = blob[off : off + TAG_LEN]
        off += TAG_LEN
        ct = blob[off:]

        db_key, _ = self._derive_keys(salt)
        pt = aead_xchacha20_poly1305_decrypt(db_key, nonce, ct, self._container_aad(), tag)
        rows_doc = json.loads(pt.decode("utf-8"))
        if rows_doc.get("app_id") != self.app_id:
            conn.close()
            raise VaultStoreError("E_HALT_SECURITY", "容器归属 App 不匹配", exit_code=2)
        for row in rows_doc.get("rows", []):
            conn.execute(
                "INSERT INTO vault_rows(seq, handle, vault_id, key_version, nonce_hex, ciphertext_b64, tag_hex, deleted) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(row["seq"]),
                    str(row["handle"]),
                    str(row["vault_id"]),
                    int(row["key_version"]),
                    str(row["nonce_hex"]),
                    str(row["ciphertext_b64"]),
                    str(row["tag_hex"]),
                    int(row["deleted"]),
                ),
            )
        return conn, salt

    def _save_memory_db(self, conn: sqlite3.Connection, salt: bytes) -> None:
        cur = conn.execute(
            "SELECT seq, handle, vault_id, key_version, nonce_hex, ciphertext_b64, tag_hex, deleted "
            "FROM vault_rows ORDER BY seq ASC"
        )
        rows = [
            {
                "seq": r[0],
                "handle": r[1],
                "vault_id": r[2],
                "key_version": r[3],
                "nonce_hex": r[4],
                "ciphertext_b64": r[5],
                "tag_hex": r[6],
                "deleted": r[7],
            }
            for r in cur.fetchall()
        ]
        payload = json.dumps({"schema": "gewu.vault-reg/1", "app_id": self.app_id, "rows": rows}, ensure_ascii=False, sort_keys=True).encode("utf-8")
        db_key, _ = self._derive_keys(salt)
        nonce = secrets.token_bytes(NONCE_LEN)
        ct, tag = aead_xchacha20_poly1305_encrypt(db_key, nonce, payload, self._container_aad())
        blob = CONTAINER_MAGIC + salt + nonce + tag + ct
        _atomic_write_0600(self.enc_path, blob)

    def _decrypt_row(self, row: Tuple[Any, ...], field_dek: bytes) -> Dict[str, Any]:
        seq, handle, vault_id, kv, nonce_hex, ct_b64, tag_hex, deleted = row
        nonce = bytes.fromhex(nonce_hex)
        ct = base64.b64decode(ct_b64.encode("ascii"))
        tag = bytes.fromhex(tag_hex)
        pt = aead_xchacha20_poly1305_decrypt(field_dek, nonce, ct, self._field_aad(vault_id, kv), tag)
        data = json.loads(pt.decode("utf-8"))
        if data.get("vault_id") != vault_id:
            raise VaultStoreError("E_HALT_SECURITY", "Vault 记录身份与密文 AAD 不一致", exit_code=2)
        validate_rel_vault_path(str(data.get("rel_path", "")))
        data["ref"] = handle
        data["seq"] = seq
        data["key_version"] = kv
        data["deleted"] = bool(deleted)
        return data

    def _encrypt_payload(self, vault_id: str, key_version: int, payload: Dict[str, Any], field_dek: bytes) -> Tuple[str, str, str]:
        validate_rel_vault_path(str(payload.get("rel_path", "")))
        nonce = secrets.token_bytes(NONCE_LEN)
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ct, tag = aead_xchacha20_poly1305_encrypt(field_dek, nonce, raw, self._field_aad(vault_id, key_version))
        return nonce.hex(), base64.b64encode(ct).decode("ascii"), tag.hex()

    @staticmethod
    def _find_row(conn: sqlite3.Connection, vault_id: Optional[str] = None, ref: Optional[str] = None) -> Optional[Tuple[Any, ...]]:
        if ref:
            cur = conn.execute(
                "SELECT seq, handle, vault_id, key_version, nonce_hex, ciphertext_b64, tag_hex, deleted FROM vault_rows WHERE handle = ?",
                (ref,),
            )
            return cur.fetchone()
        if vault_id:
            cur = conn.execute(
                "SELECT seq, handle, vault_id, key_version, nonce_hex, ciphertext_b64, tag_hex, deleted FROM vault_rows WHERE vault_id = ?",
                (vault_id,),
            )
            return cur.fetchone()
        return None

    def create_vault(
        self,
        *,
        vault_id: str,
        rel_path: str,
        title: str,
        obsidian_base: bool,
        default_mode: str,
        summary: str = "",
    ) -> Dict[str, Any]:
        validate_rel_vault_path(rel_path)
        conn, salt = self._open_memory_db()
        try:
            if self._find_row(conn, vault_id=vault_id) is not None:
                raise VaultStoreError("E_PARAM_CONFLICT", f"Vault '{vault_id}' 已存在或处于软删除占用中", exit_code=1)
            _, field_dek = self._derive_keys(salt)
            cur = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM vault_rows")
            next_seq = int(cur.fetchone()[0]) + 1
            handle = f"vlt_{next_seq:02d}"
            now = _now_iso()
            payload = {
                "vault_id": vault_id,
                "rel_path": rel_path,
                "title": title,
                "summary": summary,
                "obsidian_base": bool(obsidian_base),
                "default_mode": default_mode,
                "status": "active",
                "created_at": now,
                "updated_at": now,
                "deleted_at": None,
                "trash_rel_path": None,
            }
            n_hex, ct_b64, t_hex = self._encrypt_payload(vault_id, 1, payload, field_dek)
            conn.execute(
                "INSERT INTO vault_rows(seq, handle, vault_id, key_version, nonce_hex, ciphertext_b64, tag_hex, deleted) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                (next_seq, handle, vault_id, 1, n_hex, ct_b64, t_hex),
            )
            self._save_memory_db(conn, salt)
            return {
                "ref": handle,
                "vault_id": vault_id,
                "rel_path": rel_path,
                "title": title,
                "summary": summary,
                "obsidian_base": bool(obsidian_base),
                "default_mode": default_mode,
                "status": "active",
                "created_at": now,
                "updated_at": now,
            }
        finally:
            conn.close()

    def list_vaults(self, *, include_deleted: bool = False) -> List[Dict[str, Any]]:
        conn, salt = self._open_memory_db()
        try:
            _, field_dek = self._derive_keys(salt)
            sql = "SELECT seq, handle, vault_id, key_version, nonce_hex, ciphertext_b64, tag_hex, deleted FROM vault_rows"
            if not include_deleted:
                sql += " WHERE deleted = 0"
            sql += " ORDER BY seq ASC"
            out = []
            for row in conn.execute(sql).fetchall():
                out.append(self._decrypt_row(row, field_dek))
            return out
        finally:
            conn.close()

    def get_vault(self, *, vault_id: Optional[str] = None, ref: Optional[str] = None) -> Dict[str, Any]:
        conn, salt = self._open_memory_db()
        try:
            row = self._find_row(conn, vault_id=vault_id, ref=ref)
            if row is None:
                raise VaultStoreError("E_NOT_FOUND", "目标 Vault 不存在", exit_code=1)
            _, field_dek = self._derive_keys(salt)
            return self._decrypt_row(row, field_dek)
        finally:
            conn.close()

    def update_vault(
        self,
        *,
        vault_id: Optional[str] = None,
        ref: Optional[str] = None,
        new_vault_id: Optional[str] = None,
        new_rel_path: Optional[str] = None,
        title: Optional[str] = None,
        summary: Optional[str] = None,
        obsidian_base: Optional[bool] = None,
        default_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        conn, salt = self._open_memory_db()
        try:
            row = self._find_row(conn, vault_id=vault_id, ref=ref)
            if row is None:
                raise VaultStoreError("E_NOT_FOUND", "目标 Vault 不存在", exit_code=1)
            _, field_dek = self._derive_keys(salt)
            cur_data = self._decrypt_row(row, field_dek)
            if cur_data.get("deleted"):
                raise VaultStoreError("E_PARAM_INVALID", "已软删除 Vault 不可直接修改", exit_code=1)

            target_vid = new_vault_id or cur_data["vault_id"]
            target_rel = new_rel_path or cur_data["rel_path"]
            validate_rel_vault_path(target_rel)
            if target_vid != cur_data["vault_id"] and self._find_row(conn, vault_id=target_vid) is not None:
                raise VaultStoreError("E_PARAM_CONFLICT", f"目标 Vault '{target_vid}' 已存在", exit_code=1)

            next_kv = int(cur_data["key_version"]) + 1
            now = _now_iso()
            payload = {
                "vault_id": target_vid,
                "rel_path": target_rel,
                "title": title if title is not None else cur_data["title"],
                "summary": summary if summary is not None else cur_data.get("summary", ""),
                "obsidian_base": bool(obsidian_base) if obsidian_base is not None else bool(cur_data["obsidian_base"]),
                "default_mode": default_mode if default_mode is not None else cur_data["default_mode"],
                "status": cur_data["status"],
                "created_at": cur_data["created_at"],
                "updated_at": now,
                "deleted_at": cur_data.get("deleted_at"),
                "trash_rel_path": cur_data.get("trash_rel_path"),
            }
            n_hex, ct_b64, t_hex = self._encrypt_payload(target_vid, next_kv, payload, field_dek)
            conn.execute(
                "UPDATE vault_rows SET vault_id = ?, key_version = ?, nonce_hex = ?, ciphertext_b64 = ?, tag_hex = ? WHERE seq = ?",
                (target_vid, next_kv, n_hex, ct_b64, t_hex, int(cur_data["seq"])),
            )
            self._save_memory_db(conn, salt)
            return {
                "ref": cur_data["ref"],
                "vault_id": target_vid,
                "rel_path": target_rel,
                "title": payload["title"],
                "summary": payload.get("summary", ""),
                "obsidian_base": payload["obsidian_base"],
                "default_mode": payload["default_mode"],
                "status": payload["status"],
                "key_version": next_kv,
                "updated_at": now,
            }
        finally:
            conn.close()

    def delete_vault(
        self,
        *,
        vault_id: Optional[str] = None,
        ref: Optional[str] = None,
        purge: bool = False,
        trash_rel_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        conn, salt = self._open_memory_db()
        try:
            row = self._find_row(conn, vault_id=vault_id, ref=ref)
            if row is None:
                raise VaultStoreError("E_NOT_FOUND", "目标 Vault 不存在", exit_code=1)
            _, field_dek = self._derive_keys(salt)
            cur_data = self._decrypt_row(row, field_dek)
            if purge:
                conn.execute("DELETE FROM vault_rows WHERE seq = ?", (int(cur_data["seq"]),))
                self._save_memory_db(conn, salt)
                return {
                    "ref": cur_data["ref"],
                    "vault_id": cur_data["vault_id"],
                    "rel_path": cur_data["rel_path"],
                    "status": "purged",
                }

            if cur_data.get("deleted"):
                raise VaultStoreError("E_PARAM_INVALID", "Vault 已处于软删除状态；如需彻底清除请传入 --purge true", exit_code=1)
            now = _now_iso()
            next_kv = int(cur_data["key_version"]) + 1
            payload = {
                "vault_id": cur_data["vault_id"],
                "rel_path": cur_data["rel_path"],
                "title": cur_data["title"],
                "summary": cur_data.get("summary", ""),
                "obsidian_base": cur_data["obsidian_base"],
                "default_mode": cur_data["default_mode"],
                "status": "deleted",
                "created_at": cur_data["created_at"],
                "updated_at": now,
                "deleted_at": now,
                "trash_rel_path": trash_rel_path,
            }
            n_hex, ct_b64, t_hex = self._encrypt_payload(cur_data["vault_id"], next_kv, payload, field_dek)
            conn.execute(
                "UPDATE vault_rows SET key_version = ?, nonce_hex = ?, ciphertext_b64 = ?, tag_hex = ?, deleted = 1 WHERE seq = ?",
                (next_kv, n_hex, ct_b64, t_hex, int(cur_data["seq"])),
            )
            self._save_memory_db(conn, salt)
            return {
                "ref": cur_data["ref"],
                "vault_id": cur_data["vault_id"],
                "rel_path": cur_data["rel_path"],
                "trash_rel_path": trash_rel_path,
                "status": "deleted",
                "deleted_at": now,
            }
        finally:
            conn.close()

    def restore_vault(
        self,
        *,
        vault_id: Optional[str] = None,
        ref: Optional[str] = None,
    ) -> Dict[str, Any]:
        conn, salt = self._open_memory_db()
        try:
            row = self._find_row(conn, vault_id=vault_id, ref=ref)
            if row is None:
                raise VaultStoreError("E_NOT_FOUND", "目标 Vault 不存在", exit_code=1)
            _, field_dek = self._derive_keys(salt)
            cur_data = self._decrypt_row(row, field_dek)
            if not cur_data.get("deleted"):
                raise VaultStoreError("E_PARAM_CONFLICT", "Vault 当前处于活跃状态，无需恢复", exit_code=1)
            now = _now_iso()
            next_kv = int(cur_data["key_version"]) + 1
            payload = {
                "vault_id": cur_data["vault_id"],
                "rel_path": cur_data["rel_path"],
                "title": cur_data["title"],
                "summary": cur_data.get("summary", ""),
                "obsidian_base": cur_data["obsidian_base"],
                "default_mode": cur_data["default_mode"],
                "status": "active",
                "created_at": cur_data["created_at"],
                "updated_at": now,
                "deleted_at": None,
                "trash_rel_path": None,
            }
            n_hex, ct_b64, t_hex = self._encrypt_payload(cur_data["vault_id"], next_kv, payload, field_dek)
            conn.execute(
                "UPDATE vault_rows SET key_version = ?, nonce_hex = ?, ciphertext_b64 = ?, tag_hex = ?, deleted = 0 WHERE seq = ?",
                (next_kv, n_hex, ct_b64, t_hex, int(cur_data["seq"])),
            )
            self._save_memory_db(conn, salt)
            return {
                "ref": cur_data["ref"],
                "vault_id": cur_data["vault_id"],
                "rel_path": cur_data["rel_path"],
                "title": cur_data["title"],
                "summary": cur_data.get("summary", ""),
                "obsidian_base": cur_data["obsidian_base"],
                "default_mode": cur_data["default_mode"],
                "key_version": next_kv,
                "status": "active",
                "created_at": cur_data["created_at"],
                "updated_at": now,
                "restored_from_trash_rel": cur_data.get("trash_rel_path"),
            }
        finally:
            conn.close()
