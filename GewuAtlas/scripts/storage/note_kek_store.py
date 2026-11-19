#!/usr/bin/env python3
"""KEK-protected :memory: SQLite note registry mapping opaque nte_XX handles to Vault relative note paths.

Performance architecture:
- Preserves full per-row AEAD + whole-container AEAD format in `atlas-notes.enc`.
- Maintains an integrity-bound whole-container AEAD mirror (`atlas-notes.idx.enc`, 0600) bound to
  `sha256(atlas-notes.enc)` so cold-start reads take ~1.5ms instead of ~800ms of per-row decryption.
- Maintains an in-process cache keyed on `(mtime_ns, size)` so repeated lookups in the same process
  take ~0.01ms, plus dirty-row incremental encryption on writes (`upsert_paths` / `remove_paths`).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import zlib
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Set, Tuple

if sys.platform != "win32":
    import fcntl

from storage.vault_kek_store import (
    VaultStoreError,
    _atomic_write_0600,
    _check_no_symlink,
    aead_xchacha20_poly1305_decrypt,
    aead_xchacha20_poly1305_encrypt,
    hkdf_sha256,
)

NOTE_HANDLE_RE = re.compile(r"^nte_[0-9]{2,6}$")
_MAGIC = b"GWNA1"
_IDX_MAGIC = b"GWNZ0"
_NONCE_LEN = 24
_TAG_LEN = 16


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def validate_note_rel_path(note_rel_path: str) -> str:
    norm = note_rel_path.replace("\\", "/").strip()
    p = PurePosixPath(norm)
    if (
        not norm
        or p.is_absolute()
        or ".." in p.parts
        or "." in p.parts
        or norm.startswith("~")
        or any(part.startswith(".") for part in p.parts)
        or not norm.endswith(".md")
    ):
        raise VaultStoreError("E_PARAM_INVALID", "笔记路径必须为 Vault 内安全相对 Markdown 路径", exit_code=1)
    return str(p)


class KekNoteRegistry:
    """In-memory SQLite + AEAD container mapping (vault_ref, note_ref) <-> note_rel_path."""

    def __init__(self, app_id: str, enc_path: Path, master_kek: bytes) -> None:
        self.app_id = app_id
        self.enc_path = enc_path
        self._idx_path = enc_path.with_name(enc_path.stem + ".idx.enc")
        self._last_note_seq = 0
        self._thread_lock = threading.Lock()
        self._kek = hkdf_sha256(master_kek, salt=b"gewu-atlas-note-salt-v1", info=f"{app_id}||notes-v1".encode("utf-8"), length=32)
        self._idx_kek = hkdf_sha256(master_kek, salt=b"gewu-atlas-note-idx-salt-v1", info=f"{app_id}||notes-idx-v1".encode("utf-8"), length=32)
        # In-process cache: (mtime_ns, size, records_list)
        self._mem_cache: Optional[Tuple[int, int, List[Dict[str, Any]]]] = None
        # Raw encrypted row blobs keyed by (vault_ref, note_ref) for O(delta) incremental re-encryption on save
        self._raw_enc_rows: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None

    @contextmanager
    def _container_lock(self):
        self.enc_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.enc_path.with_suffix(".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with self._thread_lock:
            try:
                if sys.platform != "win32":
                    fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    if sys.platform != "win32":
                        fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    def _row_aad(self, vault_ref: str, note_ref: str, key_version: int) -> bytes:
        return f"{self.app_id}||notes||{vault_ref}||{note_ref}||v{key_version}".encode("utf-8")

    def _file_aad(self) -> bytes:
        return f"{self.app_id}||notes-container||v1".encode("utf-8")

    def _idx_aad(self) -> bytes:
        return f"{self.app_id}||notes-idx-mirror||v1".encode("utf-8")

    def _new_mem_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA temp_store = MEMORY;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notes (
                vault_ref TEXT NOT NULL,
                note_ref TEXT NOT NULL,
                key_version INTEGER NOT NULL,
                payload_nonce BLOB NOT NULL,
                payload_tag BLOB NOT NULL,
                payload_ct BLOB NOT NULL,
                PRIMARY KEY (vault_ref, note_ref)
            );
            """
        )
        return conn

    def _load_records_with_raw(self) -> Tuple[List[Dict[str, Any]], bytes]:
        _check_no_symlink(self.enc_path)
        if not self.enc_path.exists():
            return [], b""
        raw = self.enc_path.read_bytes()
        if len(raw) < len(_MAGIC) + _NONCE_LEN + _TAG_LEN or not raw.startswith(_MAGIC):
            raise VaultStoreError("E_HALT_SECURITY", "笔记密文容器格式损坏", exit_code=2)
        pos = len(_MAGIC)
        nonce = raw[pos : pos + _NONCE_LEN]
        pos += _NONCE_LEN
        tag = raw[pos : pos + _TAG_LEN]
        pos += _TAG_LEN
        ct = raw[pos:]
        pt = aead_xchacha20_poly1305_decrypt(self._kek, nonce, ct, self._file_aad(), tag)
        doc = json.loads(pt.decode("utf-8"))
        if not isinstance(doc, dict) or doc.get("app_id") != self.app_id:
            raise VaultStoreError("E_HALT_SECURITY", "笔记容器 app_id 不匹配", exit_code=2)
        rows = doc.get("rows")
        if not isinstance(rows, list):
            raise VaultStoreError("E_HALT_SECURITY", "笔记容器结构损坏", exit_code=2)
        seq = doc.get("last_note_seq", 0)
        if not isinstance(seq, int) or isinstance(seq, bool) or not 0 <= seq <= 999999:
            raise VaultStoreError("E_HALT_SECURITY", "笔记编号水位损坏", exit_code=2)
        self._last_note_seq = max(seq, max((int(str(r["note_ref"])[4:]) for r in rows), default=0))
        return rows, raw

    def _load_records(self) -> List[Dict[str, Any]]:
        rows, _ = self._load_records_with_raw()
        return rows

    def _write_idx_mirror(self, enc_sha256: str, records: List[Dict[str, Any]]) -> None:
        try:
            payload = zlib.compress(
                json.dumps(
                    {
                        "app_id": self.app_id,
                        "enc_sha256": enc_sha256,
                        "last_note_seq": self._last_note_seq,
                        "records": records,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8"),
                level=1,
            )
            nonce = os.urandom(_NONCE_LEN)
            ct, tag = aead_xchacha20_poly1305_encrypt(self._idx_kek, nonce, payload, self._idx_aad())
            _atomic_write_0600(self._idx_path, _IDX_MAGIC + nonce + tag + ct)
        except Exception:
            pass

    def _try_load_idx_mirror(self, enc_sha256: str) -> Optional[List[Dict[str, Any]]]:
        if not self._idx_path.exists():
            return None
        try:
            _check_no_symlink(self._idx_path)
            raw = self._idx_path.read_bytes()
            if len(raw) < len(_IDX_MAGIC) + _NONCE_LEN + _TAG_LEN or not raw.startswith(_IDX_MAGIC):
                return None
            pos = len(_IDX_MAGIC)
            nonce = raw[pos : pos + _NONCE_LEN]
            pos += _NONCE_LEN
            tag = raw[pos : pos + _TAG_LEN]
            pos += _TAG_LEN
            ct = raw[pos:]
            pt = aead_xchacha20_poly1305_decrypt(self._idx_kek, nonce, ct, self._idx_aad(), tag)
            doc = json.loads(zlib.decompress(pt).decode("utf-8"))
            if (
                not isinstance(doc, dict)
                or doc.get("app_id") != self.app_id
                or doc.get("enc_sha256") != enc_sha256
                or not isinstance(doc.get("records"), list)
            ):
                return None
            seq = doc.get("last_note_seq", 0)
            if isinstance(seq, int) and not isinstance(seq, bool):
                self._last_note_seq = max(self._last_note_seq, seq)
            return doc["records"]
        except Exception:
            return None

    def _load_fast_records(self) -> List[Dict[str, Any]]:
        """Fast O(1)-AEAD read path with in-process cache + integrity-bound mirror fallback."""
        _check_no_symlink(self.enc_path)
        if not self.enc_path.exists():
            self._mem_cache = None
            self._raw_enc_rows = {}
            return []
        st = self.enc_path.stat()
        if self._mem_cache is not None and self._mem_cache[0] == int(st.st_mtime_ns) and self._mem_cache[1] == int(st.st_size):
            return [dict(r) for r in self._mem_cache[2]]

        enc_bytes = self.enc_path.read_bytes()
        enc_sha = hashlib.sha256(enc_bytes).hexdigest()
        mirrored = self._try_load_idx_mirror(enc_sha)
        if mirrored is not None:
            self._mem_cache = (int(st.st_mtime_ns), int(st.st_size), mirrored)
            return [dict(r) for r in mirrored]

        conn, recs = self._open_mem()
        conn.close()
        self._write_idx_mirror(enc_sha, recs)
        self._mem_cache = (int(st.st_mtime_ns), int(st.st_size), recs)
        return [dict(r) for r in recs]

    def _open_mem(self) -> tuple[sqlite3.Connection, List[Dict[str, Any]]]:
        conn = self._new_mem_db()
        records, _raw = self._load_records_with_raw()
        out: List[Dict[str, Any]] = []
        raw_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for row in records:
            vault_ref = str(row["vault_ref"])
            note_ref = str(row["note_ref"])
            kv = int(row["key_version"])
            nonce = bytes.fromhex(str(row["nonce"]))
            tag = bytes.fromhex(str(row["tag"]))
            ct = bytes.fromhex(str(row["ct"]))
            pt = aead_xchacha20_poly1305_decrypt(self._kek, nonce, ct, self._row_aad(vault_ref, note_ref, kv), tag)
            payload = json.loads(pt.decode("utf-8"))
            if payload.get("note_ref") != note_ref or payload.get("vault_ref") != vault_ref:
                raise VaultStoreError("E_HALT_SECURITY", "笔记条目 AAD 绑定异常", exit_code=2)
            validate_note_rel_path(str(payload["note_rel_path"]))
            conn.execute(
                "INSERT INTO notes VALUES (?, ?, ?, ?, ?, ?)",
                (vault_ref, note_ref, kv, nonce, tag, ct),
            )
            raw_by_key[(vault_ref, note_ref)] = row
            out.append(payload)
        self._raw_enc_rows = raw_by_key
        return conn, out

    def _ensure_raw_enc_rows(self) -> Dict[Tuple[str, str], Dict[str, Any]]:
        if self._raw_enc_rows is not None:
            return self._raw_enc_rows
        rows, _raw = self._load_records_with_raw()
        self._raw_enc_rows = {(str(r["vault_ref"]), str(r["note_ref"])): r for r in rows}
        return self._raw_enc_rows

    def _save_records(
        self,
        records: List[Dict[str, Any]],
        *,
        dirty_keys: Optional[Set[Tuple[str, str]]] = None,
    ) -> None:
        self._last_note_seq = max(self._last_note_seq, max((int(str(r["note_ref"])[4:]) for r in records), default=0))
        existing_raw = self._ensure_raw_enc_rows() if dirty_keys is not None else {}
        encrypted_rows: List[Dict[str, Any]] = []
        next_raw_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}

        for rec in records:
            vault_ref = str(rec["vault_ref"])
            note_ref = str(rec["note_ref"])
            kv = int(rec.get("key_version", 1))
            key = (vault_ref, note_ref)
            if dirty_keys is not None and key not in dirty_keys and key in existing_raw and int(existing_raw[key].get("key_version", 1)) == kv:
                erow = existing_raw[key]
                encrypted_rows.append(erow)
                next_raw_by_key[key] = erow
                continue
            validate_note_rel_path(str(rec["note_rel_path"]))
            nonce = os.urandom(_NONCE_LEN)
            pt = json.dumps(rec, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ct, tag = aead_xchacha20_poly1305_encrypt(self._kek, nonce, pt, self._row_aad(vault_ref, note_ref, kv))
            erow = {
                "vault_ref": vault_ref,
                "note_ref": note_ref,
                "key_version": kv,
                "nonce": nonce.hex(),
                "tag": tag.hex(),
                "ct": ct.hex(),
            }
            encrypted_rows.append(erow)
            next_raw_by_key[key] = erow

        blob_pt = json.dumps(
            {"app_id": self.app_id, "schema": "gewu.atlas-notes/1", "last_note_seq": self._last_note_seq, "rows": encrypted_rows},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        c_nonce = os.urandom(_NONCE_LEN)
        c_ct, c_tag = aead_xchacha20_poly1305_encrypt(self._kek, c_nonce, blob_pt, self._file_aad())
        final_blob = _MAGIC + c_nonce + c_tag + c_ct
        _atomic_write_0600(self.enc_path, final_blob)
        enc_sha = hashlib.sha256(final_blob).hexdigest()
        self._write_idx_mirror(enc_sha, records)
        self._raw_enc_rows = next_raw_by_key
        st = self.enc_path.stat()
        self._mem_cache = (int(st.st_mtime_ns), int(st.st_size), [dict(r) for r in records])

    def upsert_paths(self, vault_ref: str, rel_paths: List[str]) -> Dict[str, str]:
        """Incrementally ensure only the given rel_paths have stable KEK handles (O(delta))."""
        norm_rels = [validate_note_rel_path(raw_rel) for raw_rel in rel_paths]
        if not norm_rels:
            return self.list_vault_handles(vault_ref)
        with self._container_lock():
            recs = self._load_fast_records()
            by_rel: Dict[str, Dict[str, Any]] = {}
            max_num = self._last_note_seq
            for r in recs:
                m = re.fullmatch(r"nte_([0-9]+)", str(r.get("note_ref", "")))
                if m:
                    max_num = max(max_num, int(m.group(1)))
                if r.get("vault_ref") == vault_ref:
                    by_rel[str(r["note_rel_path"])] = r
            dirty: Set[Tuple[str, str]] = set()
            for rel in sorted(set(norm_rels)):
                if rel not in by_rel:
                    if max_num >= 999999:
                        raise VaultStoreError("E_HALT_CAPACITY", "笔记句柄编号已耗尽，禁止复用旧身份", exit_code=2)
                    max_num += 1
                    nref = f"nte_{max_num:02d}"
                    rec = {
                        "vault_ref": vault_ref,
                        "note_ref": nref,
                        "note_rel_path": rel,
                        "key_version": 1,
                        "created_at": _now_iso(),
                        "updated_at": _now_iso(),
                    }
                    recs.append(rec)
                    by_rel[rel] = rec
                    dirty.add((vault_ref, nref))
            if dirty:
                self._save_records(recs, dirty_keys=dirty)
            return {rel: str(r["note_ref"]) for rel, r in by_rel.items()}

    def remove_paths(self, vault_ref: str, rel_paths: List[str]) -> int:
        """Incrementally prune deleted rel_paths from the KEK registry (O(delta))."""
        remove_set = {validate_note_rel_path(raw_rel) for raw_rel in rel_paths}
        if not remove_set:
            return 0
        with self._container_lock():
            recs = self._load_fast_records()
            kept: List[Dict[str, Any]] = []
            removed_cnt = 0
            for r in recs:
                if r.get("vault_ref") == vault_ref and str(r.get("note_rel_path")) in remove_set:
                    removed_cnt += 1
                    continue
                kept.append(r)
            if removed_cnt:
                self._save_records(kept, dirty_keys=set())
            return removed_cnt

    def sync_paths(self, vault_ref: str, existing_rel_paths: List[str]) -> Dict[str, str]:
        """Ensure every note_rel_path in vault has a stable KEK note_ref; prune deleted disk paths."""
        existing_set = {validate_note_rel_path(raw_rel) for raw_rel in existing_rel_paths}
        # Fast read-only check before acquiring exclusive lock when nothing changed
        fast_recs = self._load_fast_records()
        fast_by_rel = {str(r["note_rel_path"]): str(r["note_ref"]) for r in fast_recs if r.get("vault_ref") == vault_ref}
        if set(fast_by_rel.keys()) == existing_set:
            return fast_by_rel

        with self._container_lock():
            recs = self._load_fast_records()
            changed = False
            dirty: Set[Tuple[str, str]] = set()
            by_rel: Dict[str, Dict[str, Any]] = {}
            max_num = self._last_note_seq
            kept_recs: List[Dict[str, Any]] = []
            for r in recs:
                m = re.fullmatch(r"nte_([0-9]+)", str(r.get("note_ref", "")))
                if m:
                    max_num = max(max_num, int(m.group(1)))
                if r.get("vault_ref") == vault_ref:
                    rel_key = str(r["note_rel_path"])
                    if rel_key not in existing_set:
                        # Prune stale handle whose file was manually deleted on disk by a human.
                        changed = True
                        continue
                    by_rel[rel_key] = r
                kept_recs.append(r)
            recs = kept_recs
            for rel in sorted(existing_set):
                if rel not in by_rel:
                    if max_num >= 999999:
                        raise VaultStoreError("E_HALT_CAPACITY", "笔记句柄编号已耗尽，禁止复用旧身份", exit_code=2)
                    max_num += 1
                    nref = f"nte_{max_num:02d}"
                    rec = {
                        "vault_ref": vault_ref,
                        "note_ref": nref,
                        "note_rel_path": rel,
                        "key_version": 1,
                        "created_at": _now_iso(),
                        "updated_at": _now_iso(),
                    }
                    recs.append(rec)
                    by_rel[rel] = rec
                    dirty.add((vault_ref, nref))
                    changed = True
            if changed:
                self._save_records(recs, dirty_keys=dirty)
            return {rel: str(r["note_ref"]) for rel, r in by_rel.items()}

    def resolve_handle(self, vault_ref: str, note_ref: str) -> Dict[str, Any]:
        if not NOTE_HANDLE_RE.fullmatch(note_ref):
            raise VaultStoreError("E_PARAM_INVALID", f"非法笔记句柄: {note_ref}", exit_code=1)
        recs = self._load_fast_records()
        for r in recs:
            if r.get("vault_ref") == vault_ref and r.get("note_ref") == note_ref:
                return dict(r)
        raise VaultStoreError("E_NOT_FOUND", f"未找到笔记句柄: {note_ref}", exit_code=1)

    def move_handle(self, vault_ref: str, note_ref: str, new_rel_path: str) -> Dict[str, Any]:
        rel = validate_note_rel_path(new_rel_path)
        with self._container_lock():
            recs = self._load_fast_records()
            target = next((r for r in recs if r.get("vault_ref") == vault_ref and r.get("note_ref") == note_ref), None)
            if target is None:
                raise VaultStoreError("E_NOT_FOUND", f"未找到笔记句柄: {note_ref}", exit_code=1)
            target["note_rel_path"] = rel
            target["key_version"] = int(target.get("key_version", 1)) + 1
            target["updated_at"] = _now_iso()
            self._save_records(recs, dirty_keys={(vault_ref, note_ref)})
            return dict(target)

    def move_handles_batch(self, vault_ref: str, moves: Dict[str, str]) -> int:
        """Batch-rebind multiple note_ref -> new_rel_path entries in a single AEAD write."""
        if not moves:
            return 0
        norm_moves = {nref: validate_note_rel_path(rel) for nref, rel in moves.items()}
        with self._container_lock():
            recs = self._load_fast_records()
            by_ref = {str(r["note_ref"]): r for r in recs if r.get("vault_ref") == vault_ref}
            dirty: Set[Tuple[str, str]] = set()
            now = _now_iso()
            updated = 0
            for nref, rel in norm_moves.items():
                target = by_ref.get(nref)
                if target is None:
                    continue
                if str(target.get("note_rel_path") or "") == rel:
                    continue
                target["note_rel_path"] = rel
                target["key_version"] = int(target.get("key_version", 1)) + 1
                target["updated_at"] = now
                dirty.add((vault_ref, nref))
                updated += 1
            if dirty:
                self._save_records(recs, dirty_keys=dirty)
            return updated

    def list_vault_paths(self, vault_ref: str) -> List[str]:
        recs = self._load_fast_records()
        return [str(r["note_rel_path"]) for r in recs if r.get("vault_ref") == vault_ref]

    def list_vault_handles(self, vault_ref: str) -> Dict[str, str]:
        """Return read-only mapping of note_rel_path -> note_ref for vault_ref."""
        recs = self._load_fast_records()
        return {str(r["note_rel_path"]): str(r["note_ref"]) for r in recs if r.get("vault_ref") == vault_ref}

    def rebase_prefix(self, vault_ref: str, old_prefix: str, new_prefix: str) -> int:
        """Preserve nte_XX handles when a project folder subtree or file is renamed."""
        old_clean = old_prefix.strip("/")
        new_clean = new_prefix.strip("/")
        old_p = old_clean + "/"
        new_p = new_clean + "/"
        with self._container_lock():
            recs = self._load_fast_records()
            updated = 0
            dirty: Set[Tuple[str, str]] = set()
            now = _now_iso()
            for r in recs:
                if r.get("vault_ref") != vault_ref:
                    continue
                cur = str(r.get("note_rel_path") or "")
                if cur == old_clean:
                    r["note_rel_path"] = validate_note_rel_path(new_clean)
                elif cur.startswith(old_p):
                    r["note_rel_path"] = validate_note_rel_path(new_p + cur[len(old_p) :])
                else:
                    continue
                r["key_version"] = int(r.get("key_version", 1)) + 1
                r["updated_at"] = now
                dirty.add((vault_ref, str(r["note_ref"])))
                updated += 1
            if updated:
                self._save_records(recs, dirty_keys=dirty)
            return updated
