"""Zero-Disk Encrypted SQLite + Opaque Short Handle Vault (Pure Python 3.9+ Stdlib).

Implements:
- Whole-container AEAD encryption (XChaCha20-Poly1305, no 'SQLite format 3' header on disk).
- Per-field AEAD encryption inside SQLite BLOBs bound with AAD (app_id||table||record_id||key_version).
- HKDF-SHA256 key separation between container key and field DEK; KEK loaded externally.
- Pure :memory: SQLite execution with PRAGMA temp_store = MEMORY; zero /tmp or -wal plaintext files.
- Opaque short handles (rec_01, rec_02, ...) with non-teasing Agent declarations.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_MAGIC = b"GWV1"  # 4-byte container header + 16-byte salt + 24-byte nonce + ciphertext + 16-byte tag
_SIGMA = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)
_MASK32 = 0xFFFFFFFF


class VaultSecurityError(RuntimeError):
    """Raised on AEAD tag failure, AAD transplant, or corrupted vault container."""


def _rotl32(v: int, c: int) -> int:
    return ((v << c) & _MASK32) | (v >> (32 - c))


def _qr(s: List[int], a: int, b: int, c: int, d: int) -> None:
    s[a] = (s[a] + s[b]) & _MASK32
    s[d] = _rotl32(s[d] ^ s[a], 16)
    s[c] = (s[c] + s[d]) & _MASK32
    s[b] = _rotl32(s[b] ^ s[c], 12)
    s[a] = (s[a] + s[b]) & _MASK32
    s[d] = _rotl32(s[d] ^ s[a], 8)
    s[c] = (s[c] + s[d]) & _MASK32
    s[b] = _rotl32(s[b] ^ s[c], 7)


def _chacha_core(state: List[int], add_input: bool) -> List[int]:
    x = list(state)
    for _ in range(10):
        _qr(x, 0, 4, 8, 12)
        _qr(x, 1, 5, 9, 13)
        _qr(x, 2, 6, 10, 14)
        _qr(x, 3, 7, 11, 15)
        _qr(x, 0, 5, 10, 15)
        _qr(x, 1, 6, 11, 12)
        _qr(x, 2, 7, 8, 13)
        _qr(x, 3, 4, 9, 14)
    if add_input:
        return [(x[i] + state[i]) & _MASK32 for i in range(16)]
    return x


def _chacha20_block(key: bytes, counter: int, nonce12: bytes) -> bytes:
    kw = struct.unpack("<8I", key)
    nw = struct.unpack("<3I", nonce12)
    state = list(_SIGMA) + list(kw) + [counter & _MASK32] + list(nw)
    out = _chacha_core(state, add_input=True)
    return struct.pack("<16I", *out)


def _hchacha20(key: bytes, nonce16: bytes) -> bytes:
    kw = struct.unpack("<8I", key)
    nw = struct.unpack("<4I", nonce16)
    state = list(_SIGMA) + list(kw) + list(nw)
    out = _chacha_core(state, add_input=False)
    return struct.pack("<8I", out[0], out[1], out[2], out[3], out[12], out[13], out[14], out[15])


def _chacha20_xor(key: bytes, counter: int, nonce12: bytes, data: bytes) -> bytes:
    out = bytearray(len(data))
    offset = 0
    blk = counter
    while offset < len(data):
        ks = _chacha20_block(key, blk, nonce12)
        chunk = data[offset : offset + 64]
        for i, b in enumerate(chunk):
            out[offset + i] = b ^ ks[i]
        offset += len(chunk)
        blk = (blk + 1) & _MASK32
    return bytes(out)


def _poly1305_mac(msg: bytes, key32: bytes) -> bytes:
    r = int.from_bytes(key32[:16], "little") & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
    s = int.from_bytes(key32[16:32], "little")
    p = (1 << 130) - 5
    acc = 0
    for i in range(0, len(msg), 16):
        block = msg[i : i + 16]
        n = int.from_bytes(block + b"\x01", "little")
        acc = ((acc + n) * r) % p
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, "little")


def _pad16(data: bytes) -> bytes:
    rem = len(data) % 16
    return b"" if rem == 0 else b"\x00" * (16 - rem)


def aead_encrypt(key32: bytes, nonce24: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """RFC 8439 XChaCha20-Poly1305 authenticated encryption."""
    if len(key32) != 32 or len(nonce24) != 24:
        raise ValueError("XChaCha20-Poly1305 requires 32-byte key and 24-byte nonce")
    subkey = _hchacha20(key32, nonce24[:16])
    nonce12 = b"\x00\x00\x00\x00" + nonce24[16:24]
    otk = _chacha20_block(subkey, 0, nonce12)[:32]
    ct = _chacha20_xor(subkey, 1, nonce12, plaintext)
    mac_data = aad + _pad16(aad) + ct + _pad16(ct) + struct.pack("<QQ", len(aad), len(ct))
    tag = _poly1305_mac(mac_data, otk)
    return ct + tag


def aead_decrypt(key32: bytes, nonce24: bytes, ct_and_tag: bytes, aad: bytes) -> bytes:
    """RFC 8439 XChaCha20-Poly1305 authenticated decryption."""
    if len(key32) != 32 or len(nonce24) != 24 or len(ct_and_tag) < 16:
        raise VaultSecurityError("invalid ciphertext container")
    ct, tag = ct_and_tag[:-16], ct_and_tag[-16:]
    subkey = _hchacha20(key32, nonce24[:16])
    nonce12 = b"\x00\x00\x00\x00" + nonce24[16:24]
    otk = _chacha20_block(subkey, 0, nonce12)[:32]
    mac_data = aad + _pad16(aad) + ct + _pad16(ct) + struct.pack("<QQ", len(aad), len(ct))
    expected_tag = _poly1305_mac(mac_data, otk)
    if not hmac.compare_digest(tag, expected_tag):
        raise VaultSecurityError("E_HALT_SECURITY: AEAD authentication or AAD binding failed")
    return _chacha20_xor(subkey, 1, nonce12, ct)


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    """RFC 5869 HKDF-SHA256."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm = bytearray()
    t = b""
    counter = 1
    while len(okm) < length:
        t = hmac.new(prk, t + info + bytes([counter]), hashlib.sha256).digest()
        okm.extend(t)
        counter += 1
    return bytes(okm[:length])


def load_or_init_kek(key_file: Path, env_var: str = "GEWU_APP_KEK") -> bytes:
    """Load KEK from external env var or protected 0600 runtime key slot (never hardcoded)."""
    env_hex = os.environ.get(env_var, "").strip()
    if env_hex:
        raw = bytes.fromhex(env_hex)
        if len(raw) != 32:
            raise VaultSecurityError("external KEK must be 32 bytes (64 hex chars)")
        return raw
    if key_file.exists():
        raw = bytes.fromhex(key_file.read_text("utf-8").strip())
        if len(raw) != 32:
            raise VaultSecurityError("runtime KEK file invalid")
        return raw
    key_file.parent.mkdir(parents=True, exist_ok=True)
    kek = os.urandom(32)
    tmp = key_file.parent / f".tmp-kek-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (kek.hex() + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, key_file)
    return kek


class OpaqueVault:
    """Zero-disk double-encrypted SQLite vault returning opaque short handles (rec_01...)."""

    def __init__(self, app_id: str, vault_path: Path, kek: bytes, key_version: int = 1) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", app_id):
            raise ValueError("invalid app_id")
        self.app_id = app_id
        self.vault_path = vault_path
        self.kek = kek
        self.key_version = key_version

    def _derive_keys(self, salt: bytes) -> Tuple[bytes, bytes]:
        db_key = hkdf_sha256(self.kek, salt, f"gewu-vault-db-v{self.key_version}:{self.app_id}".encode("utf-8"))
        field_dek = hkdf_sha256(self.kek, salt, f"gewu-vault-field-v{self.key_version}:{self.app_id}".encode("utf-8"))
        return db_key, field_dek

    def _open_memory_conn(self) -> Tuple[sqlite3.Connection, bytes]:
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA temp_store = MEMORY;")
        if not self.vault_path.exists():
            salt = os.urandom(16)
            conn.executescript(
                """
                CREATE TABLE records (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    handle TEXT UNIQUE NOT NULL,
                    record_id TEXT UNIQUE NOT NULL,
                    kind TEXT NOT NULL,
                    key_version INTEGER NOT NULL,
                    aad_meta TEXT NOT NULL,
                    payload_blob BLOB NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            return conn, salt

        raw = self.vault_path.read_bytes()
        if len(raw) < 4 + 16 + 24 + 16 or raw[:4] != _MAGIC:
            raise VaultSecurityError("E_HALT_SECURITY: unrecognized vault container magic")
        salt = raw[4:20]
        nonce24 = raw[20:44]
        ct_and_tag = raw[44:]
        db_key, _ = self._derive_keys(salt)
        container_aad = f"{self.app_id}||vault-container||v{self.key_version}".encode("utf-8")
        sql_bytes = aead_decrypt(db_key, nonce24, ct_and_tag, container_aad)
        conn.executescript(sql_bytes.decode("utf-8"))
        conn.execute("PRAGMA temp_store = MEMORY;")
        return conn, salt

    def _save_memory_conn(self, conn: sqlite3.Connection, salt: bytes) -> None:
        sql_text = "\n".join(conn.iterdump()) + "\n"
        db_key, _ = self._derive_keys(salt)
        nonce24 = os.urandom(24)
        container_aad = f"{self.app_id}||vault-container||v{self.key_version}".encode("utf-8")
        ct_and_tag = aead_encrypt(db_key, nonce24, sql_text.encode("utf-8"), container_aad)
        blob = _MAGIC + salt + nonce24 + ct_and_tag

        self.vault_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.vault_path.parent / f".tmp-{os.getpid()}-{self.vault_path.name}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, blob)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.vault_path)

    def _field_aad(self, record_id: str, key_version: int) -> bytes:
        return f"{self.app_id}||records||{record_id}||v{key_version}".encode("utf-8")

    def put_record(self, kind: str, payload: Dict[str, Any], prefix: str = "rec") -> Dict[str, Any]:
        """Store encrypted payload inside double-encrypted SQLite; return ONLY opaque handle."""
        if not re.fullmatch(r"[a-z]{2,8}", prefix):
            raise ValueError("invalid handle prefix")
        conn, salt = self._open_memory_conn()
        try:
            row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM records").fetchone()
            next_seq = int(row[0]) + 1
            handle = f"{prefix}_{next_seq:02d}"
            record_id = os.urandom(12).hex()
            _, field_dek = self._derive_keys(salt)
            nonce24 = os.urandom(24)
            aad = self._field_aad(record_id, self.key_version)
            plaintext = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            encrypted_blob = nonce24 + aead_encrypt(field_dek, nonce24, plaintext, aad)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO records(handle, record_id, kind, key_version, aad_meta, payload_blob, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (handle, record_id, kind, self.key_version, aad.decode("utf-8"), sqlite3.Binary(encrypted_blob), now),
            )
            conn.commit()
            self._save_memory_conn(conn, salt)
            return {
                "ref": handle,
                "kind": kind,
                "handle_notice": f"{handle} 为运行时内部托管句柄（Agent 侧无可读内容，无需也不可解析；请直接传入 --ref 槽位）。",
            }
        finally:
            conn.close()

    def list_handles(self) -> List[Dict[str, str]]:
        """List non-sensitive opaque handles only (never exposes payload or record_id)."""
        if not self.vault_path.exists():
            return []
        conn, _ = self._open_memory_conn()
        try:
            rows = conn.execute("SELECT handle, kind, created_at FROM records ORDER BY seq ASC").fetchall()
            return [{"ref": r[0], "kind": r[1], "created_at": r[2]} for r in rows]
        finally:
            conn.close()

    def execute_on_handle(
        self,
        handle: str,
        executor: Callable[[str, Dict[str, Any]], Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Decrypt targeted record strictly in memory, run executor, return sanitized receipt only."""
        if not re.fullmatch(r"[a-z]{2,8}_[0-9]{2,8}", handle):
            raise ValueError("E_PARAM_INVALID: invalid opaque handle format")
        conn, salt = self._open_memory_conn()
        try:
            row = conn.execute(
                "SELECT record_id, kind, key_version, payload_blob FROM records WHERE handle = ?",
                (handle,),
            ).fetchone()
            if row is None:
                raise KeyError(f"E_PARAM_INVALID: unknown handle '{handle}'")
            record_id, kind, kver, blob = row
            blob_bytes = bytes(blob)
            if len(blob_bytes) < 24 + 16:
                raise VaultSecurityError("E_HALT_SECURITY: corrupted field ciphertext")
            nonce24, ct_and_tag = blob_bytes[:24], blob_bytes[24:]
            _, field_dek = self._derive_keys(salt)
            aad = self._field_aad(record_id, int(kver))
            plaintext = aead_decrypt(field_dek, nonce24, ct_and_tag, aad)
            payload = json.loads(plaintext.decode("utf-8"))
            return executor(kind, payload)
        finally:
            conn.close()
