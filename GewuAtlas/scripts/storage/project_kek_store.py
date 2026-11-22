#!/usr/bin/env python3
"""KEK-protected :memory: SQLite project registry mapping opaque prj_XX handles to 30-Projects/<dir>."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List

from storage.vault_kek_store import (
    VaultStoreError,
    _atomic_write_0600,
    _check_no_symlink,
    aead_xchacha20_poly1305_decrypt,
    aead_xchacha20_poly1305_encrypt,
    hkdf_sha256,
    registry_file_lock,
)

PROJECT_HANDLE_RE = re.compile(r"^prj_[0-9]{2,6}$")
_MAGIC = b"GWPA1"
_NONCE_LEN = 24
_TAG_LEN = 16


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def validate_project_rel_path(project_rel_path: str) -> str:
    norm = project_rel_path.replace("\\", "/").strip().rstrip("/")
    p = PurePosixPath(norm)
    if (
        not norm
        or p.is_absolute()
        or ".." in p.parts
        or "." in p.parts
        or len(p.parts) != 2
        or p.parts[0] != "30-Projects"
        or any(part.startswith(".") for part in p.parts)
    ):
        raise VaultStoreError("E_PARAM_INVALID", "课题目录必须为 Vault 内 30-Projects/<dir> 安全相对路径", exit_code=1)
    return str(p)


class KekProjectRegistry:
    """In-memory SQLite + AEAD container mapping (vault_ref, project_ref) <-> project_rel_path."""

    def __init__(self, app_id: str, enc_path: Path, master_kek: bytes) -> None:
        self.app_id = app_id
        self.enc_path = enc_path
        self._thread_lock = threading.Lock()
        self._kek = hkdf_sha256(
            master_kek,
            salt=b"gewu-atlas-project-salt-v1",
            info=f"{app_id}||projects-v1".encode("utf-8"),
            length=32,
        )

    @contextmanager
    def _container_lock(self, *, shared: bool = False):
        with registry_file_lock(self.enc_path, shared=shared, thread_lock=self._thread_lock):
            yield

    def _row_aad(self, vault_ref: str, project_ref: str, key_version: int) -> bytes:
        return f"{self.app_id}||projects||{vault_ref}||{project_ref}||v{key_version}".encode("utf-8")

    def _file_aad(self) -> bytes:
        return f"{self.app_id}||projects-container||v1".encode("utf-8")

    def _new_mem_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA temp_store = MEMORY;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                vault_ref TEXT NOT NULL,
                project_ref TEXT NOT NULL,
                key_version INTEGER NOT NULL,
                payload_nonce BLOB NOT NULL,
                payload_tag BLOB NOT NULL,
                payload_ct BLOB NOT NULL,
                PRIMARY KEY (vault_ref, project_ref)
            );
            """
        )
        return conn

    def _load_records(self) -> List[Dict[str, Any]]:
        _check_no_symlink(self.enc_path)
        if not self.enc_path.exists():
            return []
        raw = self.enc_path.read_bytes()
        if len(raw) < len(_MAGIC) + _NONCE_LEN + _TAG_LEN or not raw.startswith(_MAGIC):
            raise VaultStoreError("E_HALT_SECURITY", "课题密文容器格式损坏", exit_code=2)
        pos = len(_MAGIC)
        nonce = raw[pos : pos + _NONCE_LEN]
        pos += _NONCE_LEN
        tag = raw[pos : pos + _TAG_LEN]
        pos += _TAG_LEN
        ct = raw[pos:]
        pt = aead_xchacha20_poly1305_decrypt(self._kek, nonce, ct, self._file_aad(), tag)
        doc = json.loads(pt.decode("utf-8"))
        if not isinstance(doc, dict) or doc.get("app_id") != self.app_id:
            raise VaultStoreError("E_HALT_SECURITY", "课题容器 app_id 不匹配", exit_code=2)
        rows = doc.get("rows")
        if not isinstance(rows, list):
            raise VaultStoreError("E_HALT_SECURITY", "课题容器结构损坏", exit_code=2)
        return rows

    def _open_mem(self) -> tuple[sqlite3.Connection, List[Dict[str, Any]]]:
        conn = self._new_mem_db()
        records = self._load_records()
        out: List[Dict[str, Any]] = []
        for row in records:
            vault_ref = str(row["vault_ref"])
            project_ref = str(row["project_ref"])
            kv = int(row["key_version"])
            nonce = bytes.fromhex(str(row["nonce"]))
            tag = bytes.fromhex(str(row["tag"]))
            ct = bytes.fromhex(str(row["ct"]))
            pt = aead_xchacha20_poly1305_decrypt(self._kek, nonce, ct, self._row_aad(vault_ref, project_ref, kv), tag)
            payload = json.loads(pt.decode("utf-8"))
            if payload.get("project_ref") != project_ref or payload.get("vault_ref") != vault_ref:
                raise VaultStoreError("E_HALT_SECURITY", "课题条目 AAD 绑定异常", exit_code=2)
            validate_project_rel_path(str(payload["project_rel_path"]))
            conn.execute(
                "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?)",
                (vault_ref, project_ref, kv, nonce, tag, ct),
            )
            out.append(payload)
        return conn, out

    def _save_records(self, records: List[Dict[str, Any]]) -> None:
        encrypted_rows: List[Dict[str, Any]] = []
        for rec in records:
            vault_ref = str(rec["vault_ref"])
            project_ref = str(rec["project_ref"])
            kv = int(rec.get("key_version", 1))
            validate_project_rel_path(str(rec["project_rel_path"]))
            nonce = os.urandom(_NONCE_LEN)
            pt = json.dumps(rec, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ct, tag = aead_xchacha20_poly1305_encrypt(self._kek, nonce, pt, self._row_aad(vault_ref, project_ref, kv))
            encrypted_rows.append(
                {
                    "vault_ref": vault_ref,
                    "project_ref": project_ref,
                    "key_version": kv,
                    "nonce": nonce.hex(),
                    "tag": tag.hex(),
                    "ct": ct.hex(),
                }
            )
        blob_pt = json.dumps(
            {"app_id": self.app_id, "schema": "gewu.atlas-projects/1", "rows": encrypted_rows},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        c_nonce = os.urandom(_NONCE_LEN)
        c_ct, c_tag = aead_xchacha20_poly1305_encrypt(self._kek, c_nonce, blob_pt, self._file_aad())
        _atomic_write_0600(self.enc_path, _MAGIC + c_nonce + c_tag + c_ct)

    def sync_projects(self, vault_ref: str, existing_rel_paths: List[str]) -> Dict[str, str]:
        """Ensure every 30-Projects/<dir> has a stable KEK project_ref; prune stale missing dirs."""
        with self._container_lock():
            conn, recs = self._open_mem()
            try:
                changed = False
                by_rel: Dict[str, Dict[str, Any]] = {}
                max_num = 0
                existing_set = {validate_project_rel_path(p) for p in existing_rel_paths}
                kept: List[Dict[str, Any]] = []
                for rec in recs:
                    if rec["vault_ref"] != vault_ref:
                        kept.append(rec)
                        continue
                    if rec["project_rel_path"] not in existing_set:
                        changed = True
                        continue
                    kept.append(rec)
                    by_rel[rec["project_rel_path"]] = rec
                    m = re.match(r"^prj_(\d+)$", str(rec["project_ref"]))
                    if m:
                        max_num = max(max_num, int(m.group(1)))

                now = _now_iso()
                for rel in sorted(existing_set):
                    if rel not in by_rel:
                        max_num += 1
                        pref = f"prj_{max_num:02d}"
                        rec = {
                            "vault_ref": vault_ref,
                            "project_ref": pref,
                            "project_rel_path": rel,
                            "key_version": 1,
                            "created_at": now,
                            "updated_at": now,
                        }
                        kept.append(rec)
                        by_rel[rel] = rec
                        changed = True
                if changed:
                    self._save_records(kept)
                return {rel: rec["project_ref"] for rel, rec in by_rel.items()}
            finally:
                conn.close()

    def bind_new_project(self, vault_ref: str, project_rel_path: str) -> Dict[str, Any]:
        norm = validate_project_rel_path(project_rel_path)
        with self._container_lock():
            conn, recs = self._open_mem()
            try:
                for r in recs:
                    if r["vault_ref"] == vault_ref and r["project_rel_path"] == norm:
                        return dict(r)
                max_num = 0
                for r in recs:
                    if r["vault_ref"] == vault_ref:
                        m = re.match(r"^prj_(\d+)$", str(r["project_ref"]))
                        if m:
                            max_num = max(max_num, int(m.group(1)))
                now = _now_iso()
                new_rec = {
                    "vault_ref": vault_ref,
                    "project_ref": f"prj_{max_num + 1:02d}",
                    "project_rel_path": norm,
                    "key_version": 1,
                    "created_at": now,
                    "updated_at": now,
                }
                recs.append(new_rec)
                self._save_records(recs)
                return dict(new_rec)
            finally:
                conn.close()

    def list_projects(self, vault_ref: str) -> List[Dict[str, Any]]:
        """Return all registered project handle records for the given vault_ref."""
        with self._container_lock(shared=True):
            conn, recs = self._open_mem()
            try:
                out = [dict(r) for r in recs if r.get("vault_ref") == vault_ref]
                out.sort(key=lambda item: str(item.get("project_ref") or ""))
                return out
            finally:
                conn.close()

    def resolve_handle(self, vault_ref: str, project_ref: str) -> Dict[str, Any]:
        if not PROJECT_HANDLE_RE.match(project_ref or ""):
            raise VaultStoreError("E_PARAM_INVALID", "课题句柄格式非法，必须为 prj_01 样式", exit_code=1)
        with self._container_lock(shared=True):
            conn, recs = self._open_mem()
            try:
                for r in recs:
                    if r["vault_ref"] == vault_ref and r["project_ref"] == project_ref:
                        return dict(r)
                raise VaultStoreError("E_NOT_FOUND", f"未找到课题句柄: {project_ref}", exit_code=1)
            finally:
                conn.close()

    def move_project(self, vault_ref: str, project_ref: str, new_rel_path: str) -> Dict[str, Any]:
        """Rebind an existing project handle to a renamed directory path."""
        norm = validate_project_rel_path(new_rel_path)
        with self._container_lock():
            conn, recs = self._open_mem()
            try:
                target = next((r for r in recs if r["vault_ref"] == vault_ref and r["project_ref"] == project_ref), None)
                if target is None:
                    raise VaultStoreError("E_NOT_FOUND", f"未找到课题句柄: {project_ref}", exit_code=1)
                target["project_rel_path"] = norm
                target["key_version"] = int(target.get("key_version", 1)) + 1
                target["updated_at"] = _now_iso()
                self._save_records(recs)
                return dict(target)
            finally:
                conn.close()
