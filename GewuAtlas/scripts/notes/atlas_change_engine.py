#!/usr/bin/env python3
"""Zero-loss change ledger, unified diff logger, rollback ("后悔药"), detached DOC Git & 30-day retention engine.

Implements:
1. Pre/post Vault snapshot capture, line-level `difflib.unified_diff`, Markdown logs under
   `90-System/92-Logs/Changes/<YYYY-MM>/<chg_id>-<action>.md`, and KEK-sealed rollback snapshots (`chg_XX`)
   under `state_dir/atlas-note-undo/<vault_ref>/`.
2. Safe lossless Undo (`--action undo`): conflict detection (blocks if disk modified after change unless `--force`),
   restores deleted/modified files byte-for-byte, and soft-moves newly created files into `.trash/` (`trs_XX`).
3. Per-Vault isolated DOC Git track (`state_dir/atlas-git/<vault_ref>.git`, `0700`, `--git-dir`/`--work-tree`)
   initialized on `vault-create` with excludes for `00-DropZone/`, `source/`, `99-Assets/98-Archives/`, `.trash/`;
   automatic pre-commit BEFORE and post-commit AFTER when expected or cumulative task edits > 20 files.
4. 30-day (1 month) retention sweeps (`trash-purge`, `retention-sweep`) for `.trash/`, undo snapshots & logs,
   and `98-Archives/Tickets/`.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import zlib
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from pipeline.atlas_vfs_engine import KekTrashRegistry
from notes.atlas_yamlmini import extract_frontmatter, fm_scalar
from storage.note_kek_store import KekNoteRegistry
from storage.vault_kek_store import (
    VaultStoreError,
    _atomic_write_0600,
    _check_no_symlink,
    aead_xchacha20_poly1305_decrypt,
    aead_xchacha20_poly1305_encrypt,
    hkdf_sha256,
    registry_file_lock,
)

CHG_HANDLE_RE = re.compile(r"^chg_[0-9]{2,6}$")
_CHG_MAGIC = b"GWCH1"
_NONCE_LEN = 24
_TAG_LEN = 16
DOC_COMMIT_THRESHOLD = 20
DEFAULT_RETENTION_DAYS = 30
_MAX_CAPTURE_BYTES = 2 * 1024 * 1024
_MAX_DIFF_LINES_PER_FILE = 180
_MAX_LOG_BYTES = 96 * 1024

VAULT_GIT_EXCLUDE_BEGIN = "# --- GewuAtlas DOC track exclusions (managed) ---"
VAULT_GIT_EXCLUDE_END = "# --- End GewuAtlas DOC track exclusions ---"
VAULT_GIT_EXCLUDES = (
    VAULT_GIT_EXCLUDE_BEGIN,
    "source/",
    "*/source/",
    "**/source/",
    "98-Archives/",
    "98-Archives/**",
    "**/98-Archives/",
    "99-Assets/98-Archives/",
    "99-Assets/98-Archives/**",
    "*/99-Assets/98-Archives/",
    "**/99-Assets/98-Archives/",
    "99-Assets/*archive*/",
    "99-Assets/*archieve*/",
    "**/99-Assets/*archive*/",
    "**/99-Assets/*archieve*/",
    ".trash/",
    ".trash/**",
    ".obsidian/",
    ".obsidian/**",
    ".DS_Store",
    "**/.DS_Store",
    "__pycache__/",
    "**/__pycache__/",
    "*.tar",
    "*.tar.gz",
    VAULT_GIT_EXCLUDE_END,
)


def _now_dt() -> datetime:
    return datetime.now().astimezone()


def _now_iso() -> str:
    return _now_dt().isoformat(timespec="seconds")


def _parse_iso_dt(raw: str) -> Optional[datetime]:
    txt = str(raw or "").strip()
    if not txt:
        return None
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        try:
            dt = datetime.strptime(txt[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_now_dt().tzinfo)
    return dt


def _ensure_0700_dir(path: Path) -> Path:
    _check_no_symlink(path)
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _seal_bytes(kek: bytes, aad: bytes, plaintext: bytes) -> bytes:
    nonce = os.urandom(_NONCE_LEN)
    ct, tag = aead_xchacha20_poly1305_encrypt(kek, nonce, plaintext, aad)
    return _CHG_MAGIC + nonce + tag + ct


def _open_bytes(kek: bytes, aad: bytes, blob: bytes, err_msg: str) -> bytes:
    if len(blob) < len(_CHG_MAGIC) + _NONCE_LEN + _TAG_LEN or not blob.startswith(_CHG_MAGIC):
        raise VaultStoreError("E_HALT_SECURITY", err_msg, exit_code=2)
    pos = len(_CHG_MAGIC)
    nonce = blob[pos : pos + _NONCE_LEN]
    pos += _NONCE_LEN
    tag = blob[pos : pos + _TAG_LEN]
    pos += _TAG_LEN
    ct = blob[pos:]
    return aead_xchacha20_poly1305_decrypt(kek, nonce, ct, aad, tag)


def _atomic_write_noexec_bytes(path: Path, data: bytes) -> None:
    _check_no_symlink(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".tmp.{path.name}.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _is_snapshot_skipped(rel: str, include_journal: bool = False) -> bool:
    parts = Path(rel).parts
    if not parts:
        return True
    if any(p in (".git", ".trash", ".obsidian", "__pycache__", "node_modules") for p in parts):
        return True
    if not include_journal and parts[0] == "05-Journal":
        return True
    if rel.startswith("90-System/92-Logs/Changes/") or rel == "90-System/92-Logs/Changes":
        return True
    if "/98-Archives/Tickets/" in f"/{rel}":
        return True
    return Path(rel).name in (".DS_Store", ".gitkeep") or rel.startswith(".tmp.")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _is_text_candidate(rel: str, size: int) -> bool:
    if size > _MAX_CAPTURE_BYTES:
        return False
    ext = Path(rel).suffix.lower()
    return ext in {".md", ".markdown", ".txt", ".yaml", ".yml", ".json", ".csv", ".base", ".canvas", ""}


def _format_label(rel: str, idx_hint: Optional[int] = None) -> str:
    stem = Path(rel).stem or "file"
    tag = f"file_{idx_hint:02d}" if idx_hint is not None else "file"
    safe_stem = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff._-]+", "-", stem).strip("-") or "note"
    return f"{tag}:{safe_stem}"


def _sanitize_public_diff(diff_text: str, rel: str, idx_hint: int) -> str:
    label = _format_label(rel, idx_hint)
    out_lines: List[str] = []
    for line in diff_text.splitlines():
        if line.startswith("--- "):
            out_lines.append(f"--- a/{label}" if "/dev/null" not in line else "--- /dev/null")
        elif line.startswith("+++ "):
            out_lines.append(f"+++ b/{label}" if "/dev/null" not in line else "+++ /dev/null")
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def _compute_unified_diff(rel: str, pre_bytes: Optional[bytes], post_bytes: Optional[bytes]) -> Tuple[str, int, int]:
    """Compute bounded unified diff + (+lines, -lines) without losing semantics."""
    if pre_bytes is None and post_bytes is None:
        return "", 0, 0
    if pre_bytes is not None and b"\x00" in pre_bytes[:4096]:
        return f"--- a/{rel}\n+++ /dev/null\n@@ binary deleted ({len(pre_bytes)} bytes) @@", 0, 1
    if post_bytes is not None and b"\x00" in post_bytes[:4096]:
        if pre_bytes is None:
            return f"--- /dev/null\n+++ b/{rel}\n@@ binary created ({len(post_bytes)} bytes) @@", 1, 0
        return f"--- a/{rel}\n+++ b/{rel}\n@@ binary modified ({len(pre_bytes)} -> {len(post_bytes)} bytes) @@", 1, 1

    old_lines = pre_bytes.decode("utf-8", errors="replace").splitlines() if pre_bytes is not None else []
    new_lines = post_bytes.decode("utf-8", errors="replace").splitlines() if post_bytes is not None else []
    fromfile = "/dev/null" if pre_bytes is None else f"a/{rel}"
    tofile = "/dev/null" if post_bytes is None else f"b/{rel}"
    diff_lines = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=fromfile,
            tofile=tofile,
            lineterm="",
            n=3,
        )
    )
    added = sum(1 for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++ "))
    removed = sum(1 for ln in diff_lines if ln.startswith("-") and not ln.startswith("--- "))
    if len(diff_lines) > _MAX_DIFF_LINES_PER_FILE:
        kept = diff_lines[:_MAX_DIFF_LINES_PER_FILE]
        kept.append(f"... ({len(diff_lines) - _MAX_DIFF_LINES_PER_FILE} more diff lines truncated in log)")
        diff_lines = kept
    return "\n".join(diff_lines), added, removed


_PRUNE_WALK_DIRS = frozenset({".git", ".trash", ".obsidian", "__pycache__", "node_modules"})


def _iter_snapshot_files(root_res: Path, include_journal: bool = False):
    for cur_root, dirs, files in os.walk(root_res, followlinks=False):
        cur = Path(cur_root)
        rel_cur = "" if cur == root_res else cur.relative_to(root_res).as_posix()
        kept_dirs: List[str] = []
        for d in sorted(dirs):
            if d in _PRUNE_WALK_DIRS or (cur / d).is_symlink():
                continue
            d_rel = f"{rel_cur}/{d}" if rel_cur else d
            if not include_journal and d_rel == "05-Journal":
                continue
            if d_rel == "90-System/92-Logs/Changes" or d_rel.endswith("/98-Archives/Tickets") or d_rel == "98-Archives/Tickets":
                continue
            kept_dirs.append(d)
        dirs[:] = kept_dirs
        for fname in sorted(files):
            if fname in (".DS_Store", ".gitkeep") or fname.startswith(".tmp."):
                continue
            p = cur / fname
            if p.is_symlink() or not p.is_file():
                continue
            yield p, (f"{rel_cur}/{fname}" if rel_cur else fname)


def _load_stat_cache(cache_dir: Optional[Path]) -> Dict[str, List[Any]]:
    if cache_dir is None:
        return {}
    cp = cache_dir / ".stat-cache.json"
    if not cp.is_file() or cp.is_symlink():
        return {}
    try:
        data = json.loads(cp.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_stat_cache(cache_dir: Optional[Path], cache: Dict[str, List[Any]]) -> None:
    if cache_dir is None:
        return
    try:
        _ensure_0700_dir(cache_dir)
        cp = cache_dir / ".stat-cache.json"
        _atomic_write_noexec_bytes(cp, json.dumps(cache, ensure_ascii=False).encode("utf-8"))
    except Exception:
        pass


def capture_targeted_snapshot(vault_dir: Path, target_rels: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """Fast O(k) pre-mutation snapshot for known target paths (avoids full-vault scan on single/batch note-update)."""
    snap: Dict[str, Dict[str, Any]] = {}
    if not vault_dir.is_dir():
        return snap
    for rel in sorted(set(target_rels)):
        p = vault_dir / rel
        if not p.is_file() or p.is_symlink():
            continue
        try:
            st = p.stat()
            raw = p.read_bytes() if st.st_size <= _MAX_CAPTURE_BYTES else None
            sha = _sha256_bytes(raw) if raw is not None else _sha256_file(p)
            snap[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": sha, "bytes": raw}
        except OSError:
            continue
    return snap


def _collect_targeted_changes(
    vault_dir: Path,
    pre_snap: Dict[str, Dict[str, Any]],
    target_rels: Iterable[str],
) -> List[Dict[str, Any]]:
    """Compare only target_rels against pre_snap in O(k) time."""
    all_rels = sorted(set(pre_snap.keys()) | set(target_rels))
    changes: List[Dict[str, Any]] = []
    for idx, rel in enumerate(all_rels, start=1):
        pre = pre_snap.get(rel)
        p = vault_dir / rel
        post = None
        if p.is_file() and not p.is_symlink():
            try:
                st = p.stat()
                if pre and pre["size"] == st.st_size and pre["mtime_ns"] == st.st_mtime_ns:
                    post = pre
                else:
                    raw = p.read_bytes() if st.st_size <= _MAX_CAPTURE_BYTES else None
                    sha = _sha256_bytes(raw) if raw is not None else _sha256_file(p)
                    post = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": sha, "bytes": raw}
            except OSError:
                post = None
        if pre is None and post is not None:
            diff_txt, added, removed = _compute_unified_diff(rel, None, post.get("bytes"))
            changes.append({
                "kind": "created", "rel_path": rel, "label": _format_label(rel, idx),
                "pre_sha256": None, "post_sha256": post["sha256"], "pre_bytes": None,
                "added": added, "removed": removed, "diff_raw": diff_txt,
                "diff_sanitized": _sanitize_public_diff(diff_txt, rel, idx),
            })
        elif pre is not None and post is None:
            diff_txt, added, removed = _compute_unified_diff(rel, pre.get("bytes"), None)
            changes.append({
                "kind": "deleted", "rel_path": rel, "label": _format_label(rel, idx),
                "pre_sha256": pre["sha256"], "post_sha256": None, "pre_bytes": pre.get("bytes"),
                "added": added, "removed": removed, "diff_raw": diff_txt,
                "diff_sanitized": _sanitize_public_diff(diff_txt, rel, idx),
            })
        elif pre is not None and post is not None and pre["sha256"] != post["sha256"]:
            diff_txt, added, removed = _compute_unified_diff(rel, pre.get("bytes"), post.get("bytes"))
            changes.append({
                "kind": "modified", "rel_path": rel, "label": _format_label(rel, idx),
                "pre_sha256": pre["sha256"], "post_sha256": post["sha256"], "pre_bytes": pre.get("bytes"),
                "added": added, "removed": removed, "diff_raw": diff_txt,
                "diff_sanitized": _sanitize_public_diff(diff_txt, rel, idx),
            })
    return changes


def capture_vault_snapshot(
    vault_dir: Path,
    include_journal: bool = False,
    *,
    cache_dir: Optional[Path] = None,
    eager_binary_bytes: bool = True,
) -> Dict[str, Dict[str, Any]]:
    """Capture pre-mutation (stat, sha256, optional text bytes) map for all mutable Vault files."""
    snap: Dict[str, Dict[str, Any]] = {}
    if not vault_dir.is_dir():
        return snap
    root_res = vault_dir.resolve()
    stat_cache = _load_stat_cache(cache_dir)
    for p, rel in _iter_snapshot_files(root_res, include_journal=include_journal):
        try:
            st = p.stat()
        except OSError:
            continue
        raw: Optional[bytes] = None
        is_text = st.st_size <= _MAX_CAPTURE_BYTES and _is_text_candidate(rel, st.st_size)
        if is_text:
            raw = p.read_bytes()
            sha = _sha256_bytes(raw)
        else:
            cached = stat_cache.get(rel)
            cache_usable = (
                isinstance(cached, list)
                and len(cached) == 3
                and cached[0] == st.st_size
                and cached[1] == st.st_mtime_ns
                and (st.st_size > _MAX_CAPTURE_BYTES or not eager_binary_bytes)
            )
            if cache_usable:
                sha = str(cached[2])
            elif st.st_size <= _MAX_CAPTURE_BYTES and eager_binary_bytes:
                raw = p.read_bytes()
                sha = _sha256_bytes(raw)
            else:
                sha = _sha256_file(p)
        snap[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": sha, "bytes": raw}
    return snap


def _collect_vault_changes(
    vault_dir: Path,
    pre_snap: Dict[str, Dict[str, Any]],
    include_journal: bool = False,
    *,
    cache_dir: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Compare current Vault state on disk against pre_snap."""
    post_meta: Dict[str, Dict[str, Any]] = {}
    root_res = vault_dir.resolve()
    if root_res.is_dir():
        for p, rel in _iter_snapshot_files(root_res, include_journal=include_journal):
            try:
                st = p.stat()
            except OSError:
                continue
            pre_entry = pre_snap.get(rel)
            if pre_entry and pre_entry["size"] == st.st_size and pre_entry["mtime_ns"] == st.st_mtime_ns:
                post_meta[rel] = {
                    "size": st.st_size,
                    "mtime_ns": st.st_mtime_ns,
                    "sha256": pre_entry["sha256"],
                    "bytes": pre_entry["bytes"],
                }
                continue
            raw = p.read_bytes() if st.st_size <= _MAX_CAPTURE_BYTES else None
            sha = _sha256_bytes(raw) if raw is not None else _sha256_file(p)
            post_meta[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": sha, "bytes": raw}

    if cache_dir is not None and post_meta:
        new_cache = {
            rel: [info["size"], info["mtime_ns"], info["sha256"]]
            for rel, info in post_meta.items()
            if info["size"] > _MAX_CAPTURE_BYTES or not _is_text_candidate(rel, info["size"])
        }
        _save_stat_cache(cache_dir, new_cache)

    changes: List[Dict[str, Any]] = []
    all_rels = sorted(set(pre_snap.keys()) | set(post_meta.keys()))
    for idx, rel in enumerate(all_rels, start=1):
        pre = pre_snap.get(rel)
        post = post_meta.get(rel)
        if pre is None and post is not None:
            diff_txt, added, removed = _compute_unified_diff(rel, None, post.get("bytes"))
            changes.append(
                {
                    "kind": "created",
                    "rel_path": rel,
                    "label": _format_label(rel, idx),
                    "pre_sha256": None,
                    "post_sha256": post["sha256"],
                    "pre_bytes": None,
                    "added": added,
                    "removed": removed,
                    "diff_raw": diff_txt,
                    "diff_sanitized": _sanitize_public_diff(diff_txt, rel, idx),
                }
            )
        elif pre is not None and post is None:
            diff_txt, added, removed = _compute_unified_diff(rel, pre.get("bytes"), None)
            changes.append(
                {
                    "kind": "deleted",
                    "rel_path": rel,
                    "label": _format_label(rel, idx),
                    "pre_sha256": pre["sha256"],
                    "post_sha256": None,
                    "pre_bytes": pre.get("bytes"),
                    "added": added,
                    "removed": removed,
                    "diff_raw": diff_txt,
                    "diff_sanitized": _sanitize_public_diff(diff_txt, rel, idx),
                }
            )
        elif pre is not None and post is not None and pre["sha256"] != post["sha256"]:
            diff_txt, added, removed = _compute_unified_diff(rel, pre.get("bytes"), post.get("bytes"))
            changes.append(
                {
                    "kind": "modified",
                    "rel_path": rel,
                    "label": _format_label(rel, idx),
                    "pre_sha256": pre["sha256"],
                    "post_sha256": post["sha256"],
                    "pre_bytes": pre.get("bytes"),
                    "added": added,
                    "removed": removed,
                    "diff_raw": diff_txt,
                    "diff_sanitized": _sanitize_public_diff(diff_txt, rel, idx),
                }
            )
    return changes


class KekChangeRegistry:
    """KEK-sealed per-Vault change ledger & pre-image rollback store (`chg_XX`)."""

    def __init__(self, app_id: str, state_dir: Path, vault_ref: str, master_kek: bytes) -> None:
        self.app_id = app_id
        self.state_dir = state_dir
        self.vault_ref = vault_ref
        self.base_dir = state_dir / "atlas-note-undo" / vault_ref
        self.index_path = self.base_dir / "index.enc"
        self._thread_lock = threading.Lock()
        self._kek = hkdf_sha256(
            master_kek,
            salt=b"gewu-atlas-change-salt-v1",
            info=f"{app_id}||change-undo-v1||{vault_ref}".encode("utf-8"),
            length=32,
        )

    @contextmanager
    def _container_lock(self, *, shared: bool = False):
        with registry_file_lock(self.index_path, shared=shared, thread_lock=self._thread_lock):
            yield

    def _index_aad(self) -> bytes:
        return f"{self.app_id}||change-index||{self.vault_ref}".encode("utf-8")

    def _blob_aad(self, chg_id: str) -> bytes:
        return f"{self.app_id}||change-blob||{self.vault_ref}||{chg_id}".encode("utf-8")

    def _blob_path(self, chg_id: str) -> Path:
        return self.base_dir / f"{chg_id}.blob.enc"

    def _load_index(self) -> Dict[str, Any]:
        if not self.index_path.exists():
            return {
                "app_id": self.app_id,
                "vault_ref": self.vault_ref,
                "next_seq": 1,
                "changes": [],
                "ticket_ledger": {},
            }
        _check_no_symlink(self.index_path)
        raw = self.index_path.read_bytes()
        pt = _open_bytes(self._kek, self._index_aad(), raw, "变更台账密文格式损坏")
        doc = json.loads(pt.decode("utf-8"))
        if not isinstance(doc, dict) or doc.get("app_id") != self.app_id:
            raise VaultStoreError("E_HALT_SECURITY", "变更台账容器 app_id 不匹配", exit_code=2)
        doc.setdefault("changes", [])
        doc.setdefault("ticket_ledger", {})
        return doc

    def _save_index(self, doc: Dict[str, Any]) -> None:
        _ensure_0700_dir(self.base_dir)
        pt = json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8")
        _atomic_write_0600(self.index_path, _seal_bytes(self._kek, self._index_aad(), pt))

    def _save_blob(self, chg_id: str, payload: Dict[str, Any]) -> None:
        _ensure_0700_dir(self.base_dir)
        raw_json = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        compressed = zlib.compress(raw_json, level=6)
        _atomic_write_0600(self._blob_path(chg_id), _seal_bytes(self._kek, self._blob_aad(chg_id), compressed))

    def _load_blob(self, chg_id: str) -> Dict[str, Any]:
        bp = self._blob_path(chg_id)
        _check_no_symlink(bp)
        if not bp.is_file():
            raise VaultStoreError("E_NOT_FOUND", f"回滚快照 {chg_id} 不存在或已被清理", exit_code=1)
        compressed = _open_bytes(self._kek, self._blob_aad(chg_id), bp.read_bytes(), f"回滚快照 {chg_id} 密文损坏")
        return json.loads(zlib.decompress(compressed).decode("utf-8"))

    def ticket_touched_files(self, ticket_id: str) -> List[str]:
        if not ticket_id:
            return []
        doc = self._load_index()
        entry = doc.get("ticket_ledger", {}).get(ticket_id) or {}
        files = entry.get("touched_files") or []
        return [str(x) for x in files if isinstance(x, str)]

    def mark_ticket_precommitted(self, ticket_id: str) -> None:
        if not ticket_id:
            return
        with self._container_lock():
            doc = self._load_index()
            tl = doc.setdefault("ticket_ledger", {})
            entry = tl.setdefault(ticket_id, {"touched_files": [], "pre_committed": False, "post_commit_count": 0})
            entry["pre_committed"] = True
            self._save_index(doc)

    def ticket_precommitted(self, ticket_id: str) -> bool:
        if not ticket_id:
            return False
        doc = self._load_index()
        entry = doc.get("ticket_ledger", {}).get(ticket_id) or {}
        return bool(entry.get("pre_committed"))

    def record_change(
        self,
        *,
        vault_dir: Path,
        action: str,
        actor: str,
        ticket_id: str,
        changes: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        with self._container_lock():
            return self._record_change_unlocked(
                vault_dir=vault_dir,
                action=action,
                actor=actor,
                ticket_id=ticket_id,
                changes=changes,
            )

    def _record_change_unlocked(
        self,
        *,
        vault_dir: Path,
        action: str,
        actor: str,
        ticket_id: str,
        changes: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        import base64

        doc = self._load_index()
        seq = int(doc.get("next_seq", 1))
        chg_id = f"chg_{seq:02d}"
        doc["next_seq"] = seq + 1
        ts = _now_iso()
        month_str = ts[:7]
        log_rel = f"90-System/92-Logs/Changes/{month_str}/{chg_id}-{action}.md"

        created_cnt = sum(1 for c in changes if c["kind"] == "created")
        modified_cnt = sum(1 for c in changes if c["kind"] == "modified")
        deleted_cnt = sum(1 for c in changes if c["kind"] == "deleted")
        total_added = sum(int(c["added"]) for c in changes)
        total_removed = sum(int(c["removed"]) for c in changes)

        blob_items: List[Dict[str, Any]] = []
        sanitized_files: List[Dict[str, Any]] = []
        touched_rels: List[str] = []
        for c in changes:
            rel = str(c["rel_path"])
            touched_rels.append(rel)
            pre_b64 = base64.b64encode(c["pre_bytes"]).decode("ascii") if c["pre_bytes"] is not None else None
            blob_items.append(
                {
                    "kind": c["kind"],
                    "rel_path": rel,
                    "label": c["label"],
                    "pre_sha256": c["pre_sha256"],
                    "post_sha256": c["post_sha256"],
                    "pre_b64": pre_b64,
                    "added": c["added"],
                    "removed": c["removed"],
                    "diff_raw": c["diff_raw"],
                    "diff_sanitized": c["diff_sanitized"],
                }
            )
            sanitized_files.append(
                {
                    "kind": c["kind"],
                    "label": c["label"],
                    "added": c["added"],
                    "removed": c["removed"],
                    "diff": c["diff_sanitized"],
                }
            )

        self._save_blob(
            chg_id,
            {
                "change_id": chg_id,
                "action": action,
                "actor": actor,
                "ticket_id": ticket_id,
                "created_at": ts,
                "items": blob_items,
            },
        )

        # Write Vault Markdown changelog under 90-System/92-Logs/Changes/<YYYY-MM>/<chg_id>-<action>.md
        _write_markdown_changelog(
            vault_dir=vault_dir,
            vault_ref=self.vault_ref,
            log_rel=log_rel,
            chg_id=chg_id,
            action=action,
            actor=actor,
            ticket_id=ticket_id,
            ts=ts,
            changes=changes,
        )

        if ticket_id:
            tl = doc.setdefault("ticket_ledger", {})
            tentry = tl.setdefault(ticket_id, {"touched_files": [], "pre_committed": False, "post_commit_count": 0})
            merged = list(dict.fromkeys([*tentry.get("touched_files", []), *touched_rels]))
            tentry["touched_files"] = merged

        rec = {
            "change_id": chg_id,
            "action": action,
            "actor": actor,
            "ticket_id": ticket_id,
            "created_at": ts,
            "log_rel": log_rel,
            "created_count": created_cnt,
            "modified_count": modified_cnt,
            "deleted_count": deleted_cnt,
            "file_count": len(changes),
            "lines_added": total_added,
            "lines_removed": total_removed,
            "undone": False,
            "undone_at": None,
            "undoable": all(
                (c["kind"] == "created") or (c["pre_bytes"] is not None)
                for c in changes
            ),
        }
        doc["changes"].append(rec)
        self._save_index(doc)
        return {
            "change_id": chg_id,
            "action": action,
            "ticket_id": ticket_id,
            "created_at": ts,
            "file_count": len(changes),
            "created_count": created_cnt,
            "modified_count": modified_cnt,
            "deleted_count": deleted_cnt,
            "lines_added": total_added,
            "lines_removed": total_removed,
            "undoable": rec["undoable"],
            "undo_hint": f"./gewu atlas --action undo --ref {self.vault_ref} --change_id {chg_id}",
            "files": [{"kind": f["kind"], "label": f["label"], "added": f["added"], "removed": f["removed"]} for f in sanitized_files],
        }

    def list_changes(self, *, ticket_id: str = "", include_undone: bool = True, limit: int = 50) -> Dict[str, Any]:
        doc = self._load_index()
        rows = list(doc.get("changes", []))
        if ticket_id:
            rows = [r for r in rows if r.get("ticket_id") == ticket_id]
        if not include_undone:
            rows = [r for r in rows if not r.get("undone")]
        rows = list(reversed(rows))[: max(1, min(200, int(limit or 50)))]
        items = [
            {
                "change_id": r["change_id"],
                "action": r["action"],
                "actor": r["actor"],
                "ticket_id": r.get("ticket_id") or None,
                "created_at": r["created_at"],
                "file_count": int(r.get("file_count", 0)),
                "created_count": int(r.get("created_count", 0)),
                "modified_count": int(r.get("modified_count", 0)),
                "deleted_count": int(r.get("deleted_count", 0)),
                "lines_added": int(r.get("lines_added", 0)),
                "lines_removed": int(r.get("lines_removed", 0)),
                "undone": bool(r.get("undone", False)),
                "undone_at": r.get("undone_at"),
                "undoable": bool(r.get("undoable", False)) and not bool(r.get("undone", False)),
            }
            for r in rows
        ]
        return {"vault_ref": self.vault_ref, "count": len(items), "changes": items, "path_sealed": True}

    def get_change(self, chg_id: str) -> Dict[str, Any]:
        if not CHG_HANDLE_RE.fullmatch((chg_id or "").strip()):
            raise VaultStoreError("E_PARAM_INVALID", f"非法变更句柄: {chg_id}", exit_code=1)
        doc = self._load_index()
        rec = next((r for r in doc.get("changes", []) if r.get("change_id") == chg_id), None)
        if not rec:
            raise VaultStoreError("E_NOT_FOUND", f"变更句柄 {chg_id} 不存在", exit_code=1)
        blob = self._load_blob(chg_id)
        files = [
            {
                "kind": item["kind"],
                "label": item["label"],
                "added": int(item.get("added", 0)),
                "removed": int(item.get("removed", 0)),
                "diff": item.get("diff_sanitized") or "",
            }
            for item in blob.get("items", [])
        ]
        return {
            "vault_ref": self.vault_ref,
            "change_id": rec["change_id"],
            "action": rec["action"],
            "actor": rec["actor"],
            "ticket_id": rec.get("ticket_id") or None,
            "created_at": rec["created_at"],
            "file_count": int(rec.get("file_count", 0)),
            "created_count": int(rec.get("created_count", 0)),
            "modified_count": int(rec.get("modified_count", 0)),
            "deleted_count": int(rec.get("deleted_count", 0)),
            "lines_added": int(rec.get("lines_added", 0)),
            "lines_removed": int(rec.get("lines_removed", 0)),
            "undone": bool(rec.get("undone", False)),
            "undone_at": rec.get("undone_at"),
            "undoable": bool(rec.get("undoable", False)) and not bool(rec.get("undone", False)),
            "files": files,
            "path_sealed": True,
        }

    def undo_change(
        self,
        *,
        vault_dir: Path,
        chg_id: str,
        note_reg: KekNoteRegistry,
        trash_reg: KekTrashRegistry,
        force: bool = False,
    ) -> Tuple[Dict[str, Any], List[str]]:
        with self._container_lock():
            return self._undo_change_unlocked(
                vault_dir=vault_dir,
                chg_id=chg_id,
                note_reg=note_reg,
                trash_reg=trash_reg,
                force=force,
            )

    def _undo_change_unlocked(
        self,
        *,
        vault_dir: Path,
        chg_id: str,
        note_reg: KekNoteRegistry,
        trash_reg: KekTrashRegistry,
        force: bool = False,
    ) -> Tuple[Dict[str, Any], List[str]]:
        import base64

        if not CHG_HANDLE_RE.fullmatch((chg_id or "").strip()):
            raise VaultStoreError("E_PARAM_INVALID", f"非法变更句柄: {chg_id}", exit_code=1)
        doc = self._load_index()
        rec = next((r for r in doc.get("changes", []) if r.get("change_id") == chg_id), None)
        if not rec:
            raise VaultStoreError("E_NOT_FOUND", f"变更句柄 {chg_id} 不存在", exit_code=1)
        if rec.get("undone"):
            raise VaultStoreError("E_PARAM_CONFLICT", f"变更 {chg_id} 已被撤回，禁止重复撤回", exit_code=1)
        if not rec.get("undoable"):
            raise VaultStoreError("E_PARAM_INVALID", f"变更 {chg_id} 包含超大二进制前置快照缺失，无法自动撤回", exit_code=1)

        blob = self._load_blob(chg_id)
        items: List[Dict[str, Any]] = list(blob.get("items", []))
        conflicts: List[Dict[str, str]] = []
        sealed: List[str] = [str(it["rel_path"]) for it in items]

        # Pre-flight check: verify no post-op edits collided unless force=True, and protect 05-Journal created files
        for it in items:
            rel = str(it["rel_path"])
            target = vault_dir / rel
            if target.is_symlink():
                raise VaultStoreError("E_SECURITY_VIOLATION", "撤回目标包含符号链接", exit_code=2)
            kind = it["kind"]
            if kind == "created" and rel.startswith("05-Journal/") and str(rec.get("action") or "") != "journal-archive":
                raise VaultStoreError(
                    "E_JOURNAL_PROTECTED",
                    "日记（05-Journal）只归档不删除，禁止通过 undo 移入废纸篓或删除日记文件",
                    exit_code=1,
                )
            if kind in ("created", "modified"):
                if not target.is_file():
                    if not force:
                        conflicts.append(
                            {
                                "label": it["label"],
                                "reason": "human_manually_deleted",
                                "human_action_detected": True,
                                "hint": "文件已被人类手动删除或移走，严禁 Agent 擅自恢复或重建",
                            }
                        )
                else:
                    cur_sha = _sha256_file(target)
                    if cur_sha != it.get("post_sha256") and not force:
                        conflicts.append(
                            {
                                "label": it["label"],
                                "reason": "human_manually_edited",
                                "human_action_detected": True,
                                "hint": "文件已被人类手工修改，默认禁止 undo 覆盖人类改动",
                            }
                        )
            elif kind == "deleted":
                if target.exists() and not force:
                    conflicts.append(
                        {
                            "label": it["label"],
                            "reason": "human_manually_recreated",
                            "human_action_detected": True,
                            "hint": "目标槽位已存在人类新放入的文件，默认禁止覆盖",
                        }
                    )

        if conflicts:
            return {
                "requires_approval": True,
                "vault_ref": self.vault_ref,
                "change_id": chg_id,
                "conflicts": conflicts,
                "path_sealed": True,
            }, sealed

        restored_count = 0
        trashed_created_refs: List[str] = []
        trash_root = vault_dir / ".trash"
        trash_root.mkdir(parents=True, exist_ok=True)
        stamp = _now_dt().strftime("%Y%m%d%H%M%S")

        for it in items:
            rel = str(it["rel_path"])
            target = vault_dir / rel
            kind = it["kind"]
            if kind == "created":
                if target.is_file():
                    trash_name = f"undo-{chg_id}-{stamp}-{target.name}"
                    dst = trash_root / trash_name
                    sz = target.stat().st_size
                    shutil.move(str(target), str(dst))
                    trec = trash_reg.record_trashed(
                        vault_ref=self.vault_ref,
                        trash_rel=f".trash/{trash_name}",
                        orig_rel=rel,
                        orig_zone="undo",
                        display_name=Path(rel).stem,
                        kind="note" if rel.endswith(".md") else "file",
                        size_bytes=sz,
                    )
                    trashed_created_refs.append(trec["trash_ref"])
            else:
                if force and target.is_file():
                    # Even forced overwrite preserves the current disk file into .trash/ first!
                    backup_name = f"force-undo-{chg_id}-{stamp}-{target.name}"
                    shutil.copy2(str(target), str(trash_root / backup_name))
                raw_bytes = base64.b64decode(it["pre_b64"].encode("ascii"))
                _atomic_write_noexec_bytes(target, raw_bytes)
                restored_count += 1

        # Re-sync note registry
        from notes.atlas_note_engine import _scan_vault_markdowns

        note_reg.sync_paths(self.vault_ref, _scan_vault_markdowns(vault_dir))
        rec["undone"] = True
        rec["undone_at"] = _now_iso()
        self._save_index(doc)

        # Annotate markdown changelog file as undone
        log_path = vault_dir / str(rec.get("log_rel") or "")
        if log_path.is_file():
            try:
                old_log = log_path.read_text(encoding="utf-8")
                updated_log = old_log.replace("undone: false", f"undone: true\nundone_at: \"{rec['undone_at']}\"", 1)
                updated_log += f"\n\n> [!NOTE] 本次变更已于 {rec['undone_at']} 通过 `undo --change_id {chg_id}` 无损撤回。\n"
                _atomic_write_noexec_bytes(log_path, updated_log.encode("utf-8"))
            except OSError:
                pass

        return {
            "vault_ref": self.vault_ref,
            "change_id": chg_id,
            "undone": True,
            "restored_count": restored_count,
            "trashed_created_count": len(trashed_created_refs),
            "trashed_created_refs": trashed_created_refs,
            "ticket_id": rec.get("ticket_id") or None,
            "path_sealed": True,
        }, sealed

    def purge_older_than(self, vault_dir: Path, days: int = DEFAULT_RETENTION_DAYS, dry_run: bool = False) -> Dict[str, Any]:
        with self._container_lock():
            return self._purge_older_than_unlocked(vault_dir=vault_dir, days=days, dry_run=dry_run)

    def _purge_older_than_unlocked(self, vault_dir: Path, days: int = DEFAULT_RETENTION_DAYS, dry_run: bool = False) -> Dict[str, Any]:
        cutoff = _now_dt() - timedelta(days=max(0, int(days)))
        doc = self._load_index()
        kept_rows: List[Dict[str, Any]] = []
        expired_ids: List[str] = []
        removed_logs = 0

        for r in doc.get("changes", []):
            dt = _parse_iso_dt(str(r.get("created_at") or ""))
            if dt is not None and dt < cutoff:
                expired_ids.append(str(r.get("change_id")))
                if not dry_run:
                    bp = self._blob_path(str(r.get("change_id")))
                    if bp.exists():
                        bp.unlink(missing_ok=True)
                    lp = vault_dir / str(r.get("log_rel") or "")
                    if lp.is_file():
                        lp.unlink(missing_ok=True)
                        removed_logs += 1
            else:
                kept_rows.append(r)

        # Also clean orphan changelog markdown files inside 90-System/92-Logs/Changes/ older than cutoff
        changes_root = vault_dir / "90-System" / "92-Logs" / "Changes"
        orphan_logs = 0
        if changes_root.is_dir():
            for md in sorted(changes_root.rglob("*.md")):
                if md.is_symlink() or not md.is_file():
                    continue
                mtime_dt = datetime.fromtimestamp(md.stat().st_mtime, tz=cutoff.tzinfo)
                if mtime_dt < cutoff:
                    orphan_logs += 1
                    if not dry_run:
                        md.unlink(missing_ok=True)

        if not dry_run and expired_ids:
            doc["changes"] = kept_rows
            self._save_index(doc)

        return {
            "expired_change_count": len(expired_ids),
            "removed_change_logs": removed_logs + orphan_logs,
        }


def _write_markdown_changelog(
    *,
    vault_dir: Path,
    vault_ref: str,
    log_rel: str,
    chg_id: str,
    action: str,
    actor: str,
    ticket_id: str,
    ts: str,
    changes: List[Dict[str, Any]],
) -> None:
    lines: List[str] = [
        "---",
        "doc_type: change_log",
        f'change_id: "{fm_scalar(chg_id)}"',
        f'action: "{fm_scalar(action)}"',
        f'actor: "{fm_scalar(actor)}"',
        f'ticket_id: "{fm_scalar(ticket_id)}"',
        f"created: {ts[:10]}",
        f'created_at: "{ts}"',
        f"file_count: {len(changes)}",
        "undone: false",
        "---",
        "",
        f"# 变更明细日志 {chg_id}（`{action}`）",
        "",
        f"- **执行时间**：`{ts}`",
        f"- **执行主体**：`{actor}`",
        f"- **关联工单**：`{ticket_id or 'N/A'}`",
        f"- **一键后悔药**：`./gewu atlas --action undo --ref {vault_ref} --change_id {chg_id}`",
        "",
        "## 受影响文件清单",
        "",
        "| 操作 | 文件相对路径 | +行 | -行 |",
        "| :--- | :--- | ---: | ---: |",
    ]
    for c in changes:
        lines.append(f"| `{c['kind']}` | `{c['rel_path']}` | +{c['added']} | -{c['removed']} |")
    lines.append("")
    lines.append("## 行级 Diff 明细")
    lines.append("")

    cur_bytes = len("\n".join(lines).encode("utf-8"))
    for c in changes:
        block = [
            f"### `{c['rel_path']}` ({c['kind']})",
            "",
            "```diff",
            c["diff_raw"] or "(binary or metadata unchanged)",
            "```",
            "",
        ]
        chunk = "\n".join(block)
        chunk_bytes = len(chunk.encode("utf-8"))
        if cur_bytes + chunk_bytes > _MAX_LOG_BYTES:
            lines.append("> [!NOTE] 后续部分文件 diff 因单篇日志体积上限（96KB）已折叠，完整前置快照仍可通过 `undo` 恢复。")
            break
        lines.extend(block)
        cur_bytes += chunk_bytes

    target = vault_dir / log_rel
    _atomic_write_noexec_bytes(target, ("\n".join(lines).rstrip() + "\n").encode("utf-8"))


# ---------------------------------------------------------------------------
# Detached Per-Vault DOC Git Track & Unified Remote (GitHub/gh) Sync
# ---------------------------------------------------------------------------

_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9._/-]{1,80}$")
_SCP_REMOTE_RE = re.compile(r"^git@[A-Za-z0-9._-]+:[A-Za-z0-9._/-]+(?:\.git)?$")


def _git_available() -> bool:
    return shutil.which("git") is not None


def _gh_binary() -> Optional[str]:
    return shutil.which("gh")


def resolve_vault_git_dir(state_dir: Path, vault_ref: str, vault_dir: Path) -> Tuple[Path, str]:
    """Use existing `<vault>/.git` if present, otherwise store detached git-dir under `state_dir/atlas-git/<vlt>.git`."""
    in_vault = vault_dir / ".git"
    if in_vault.exists():
        return in_vault, "in_vault_git"
    detached = state_dir / "atlas-git" / f"{vault_ref}.git"
    return detached, "detached_git_dir"


def _run_git(
    git_dir: Path,
    vault_dir: Path,
    args: List[str],
    *,
    timeout: int = 20,
    network: bool = False,
) -> Tuple[int, str, str]:
    cmd: List[str] = [
        "git",
        "-c",
        "user.name=GewuAtlas",
        "-c",
        "user.email=atlas@gewu.local",
    ]
    if network:
        gh_bin = _gh_binary()
        if gh_bin:
            cmd.extend([
                "-c",
                "credential.https://github.com.helper=",
                "-c",
                f"credential.https://github.com.helper=!{shlex.quote(gh_bin)} auth git-credential",
            ])
    cmd.extend([
        "--git-dir",
        str(git_dir),
        "--work-tree",
        str(vault_dir),
        *args,
    ])
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GCM_INTERACTIVE", "never")
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(vault_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "git command timed out"


def _validate_remote_url(raw_url: str) -> str:
    url = str(raw_url or "").strip()
    if not url:
        raise VaultStoreError("E_PARAM_INVALID", "远端仓库地址不能为空", exit_code=1)
    if url.startswith("-") or any(ch in url for ch in ("\r", "\n", "\t", "\x00", " ")):
        raise VaultStoreError("E_PARAM_INVALID", "远端仓库地址含非法字符或前导横线", exit_code=1)
    if re.match(r"^https?://", url, re.I):
        rest = re.sub(r"^https?://", "", url, flags=re.I)
        authority = rest.split("/", 1)[0]
        if "@" in authority:
            raise VaultStoreError("E_PARAM_INVALID", "禁止在远端 URL 中内嵌明文凭据或 Token，请使用 gh auth login 或 SSH", exit_code=1)
        if not authority or "." not in authority:
            raise VaultStoreError("E_PARAM_INVALID", "非法 HTTP(S) 远端地址", exit_code=1)
        return url
    if re.match(r"^ssh://", url, re.I):
        return url
    if _SCP_REMOTE_RE.fullmatch(url):
        return url
    if url.startswith("file://"):
        return url
    p = Path(url)
    if p.is_absolute() and p.exists() and p.is_dir():
        return str(p.resolve())
    raise VaultStoreError(
        "E_PARAM_INVALID",
        "远端仓库地址仅支持 https://、ssh://、git@host:owner/repo.git 或已存在的本地 Git 裸仓目录",
        exit_code=1,
    )


def _validate_branch_name(raw_branch: str) -> str:
    br = str(raw_branch or "").strip()
    if not br or br.startswith("-") or ".." in br or br.endswith("/") or br.startswith("/") or not _BRANCH_NAME_RE.fullmatch(br):
        raise VaultStoreError("E_PARAM_INVALID", "非法分支名", exit_code=1)
    return br


def _sanitize_remote_url_for_display(raw_url: Optional[str]) -> Optional[str]:
    if not raw_url:
        return None
    url = str(raw_url).strip()
    if not url:
        return None
    if re.match(r"^https?://", url, re.I):
        return re.sub(r"^(https?://)[^/@]+@", r"\1", url, flags=re.I)
    if re.match(r"^ssh://", url, re.I) or _SCP_REMOTE_RE.fullmatch(url):
        return url
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    return f"local-remote:{digest}"


def _classify_git_remote_error(stderr_text: str, rc: int) -> Tuple[str, str]:
    low = (stderr_text or "").lower()
    if rc == 124 or "timed out" in low:
        return "network_error", "远端同步超时，请检查网络连接后重试"
    if any(k in low for k in (
        "authentication failed",
        "could not read username",
        "terminal prompts disabled",
        "permission denied (publickey)",
        "403",
        "invalid username or token",
        "repository not found",
        "http basic: access denied",
    )):
        return "auth_required", "GitHub 远端认证失败或无权限，请在终端执行 gh auth login && gh auth setup-git 后重试"
    if any(k in low for k in (
        "could not resolve host",
        "failed to connect",
        "unable to access",
        "connection reset",
        "network is unreachable",
        "ssl",
    )):
        return "network_error", "无法连接远端仓库，请检查网络或代理设置后重试"
    if any(k in low for k in ("non-fast-forward", "fetch first", "rejected")):
        return "diverged", "远端存在更新提交导致非快进拒绝，请执行 doc-sync 协调后再推送"
    return "remote_error", _sanitize_git_msg_part(stderr_text, "remote_sync_error")


def _git_config_get(git_dir: Path, vault_dir: Path, key: str) -> Optional[str]:
    rc, out, _ = _run_git(git_dir, vault_dir, ["config", "--get", key])
    if rc != 0:
        return None
    val = out.strip()
    return val if val else None


def ensure_vault_git_exclude(git_dir: Path) -> None:
    info_dir = git_dir / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    excl = info_dir / "exclude"
    existing = excl.read_text(encoding="utf-8") if excl.is_file() else ""
    block = "\n".join(VAULT_GIT_EXCLUDES)
    if VAULT_GIT_EXCLUDE_BEGIN in existing and VAULT_GIT_EXCLUDE_END in existing:
        b = existing.index(VAULT_GIT_EXCLUDE_BEGIN)
        e = existing.index(VAULT_GIT_EXCLUDE_END) + len(VAULT_GIT_EXCLUDE_END)
        new_txt = existing[:b] + block + existing[e:]
    else:
        sep = "\n\n" if existing.strip() else ""
        new_txt = existing.rstrip() + sep + block + "\n"
    if new_txt != existing:
        excl.write_text(new_txt, encoding="utf-8")


def _sanitize_git_msg_part(raw: str, fallback: str) -> str:
    txt = str(raw or "").replace("Volumes/", "").replace("GewuAtlas-data", "vault").replace(".trash", "trash")
    txt = re.sub(r"/Users/[^\s'\":,)]+", "<path>", txt)
    txt = re.sub(r"/private/[^\s'\":,)]+", "<path>", txt)
    txt = re.sub(r"/tmp/[^\s'\":,)]+", "<path>", txt)
    txt = re.sub(r"/var/[^\s'\":,)]+", "<path>", txt)
    txt = re.sub(r"\b(?:20-AI-Zone|30-Projects|99-Assets|00-DropZone|90-System)/[^\s,)]*", "", txt)
    txt = re.sub(r"[A-Za-z0-9._-]+\.md\b", "note", txt)
    txt = re.sub(r"[\r\n\t]+", " ", txt).strip(" .:-/")
    return (txt[:80] if txt else fallback)


def init_vault_git_repo(state_dir: Path, vault_ref: str, vault_dir: Path) -> Dict[str, Any]:
    """Initialize per-Vault DOC Git repository (detached `.git` outside Vault by default) with mandatory excludes."""
    if not _git_available():
        return {"initialized": False, "mode": "git_unavailable", "head_short": None}
    git_dir, mode = resolve_vault_git_dir(state_dir, vault_ref, vault_dir)
    if mode == "detached_git_dir":
        _ensure_0700_dir(git_dir.parent)
        _ensure_0700_dir(git_dir)
        if not (git_dir / "HEAD").is_file():
            proc = subprocess.run(
                ["git", "--bare", "init", "-b", "main", str(git_dir)],
                cwd=str(vault_dir),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if proc.returncode != 0:
                proc = subprocess.run(
                    ["git", "--bare", "init", str(git_dir)],
                    cwd=str(vault_dir),
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                if proc.returncode != 0:
                    return {"initialized": False, "mode": "git_init_error", "head_short": None}
            # Ensure bare config works cleanly with --work-tree
            subprocess.run(
                ["git", "--git-dir", str(git_dir), "config", "core.bare", "false"],
                cwd=str(vault_dir),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
    else:
        if not (git_dir / "HEAD").is_file() and not git_dir.is_file():
            subprocess.run(["git", "init", "-b", "main"], cwd=str(vault_dir), capture_output=True, text=True, timeout=20, check=False)

    ensure_vault_git_exclude(git_dir)

    rc_head, out_head, _ = _run_git(git_dir, vault_dir, ["rev-parse", "--short", "HEAD"])
    if rc_head != 0:
        _run_git(git_dir, vault_dir, ["add", "-A"])
        _run_git(
            git_dir,
            vault_dir,
            ["commit", "--allow-empty", "-m", "chore(atlas): [GewuAtlas] init DOC vault baseline"],
        )
        _, out_head, _ = _run_git(git_dir, vault_dir, ["rev-parse", "--short", "HEAD"])

    return {
        "initialized": True,
        "mode": mode,
        "head_short": out_head.strip() or None,
        "included_scopes": ["dropzone", "journal"],
        "ignored_scopes": ["obsidian", "archives", "source", "trash"],
    }


def _doc_zone_for_rel(rel_path: str) -> str:
    low = rel_path.replace("\\", "/").lstrip("/").lower()
    if low.startswith("00-dropzone/"):
        return "dropzone"
    if low.startswith("05-journal/"):
        return "journal"
    if low.startswith("10-index/"):
        return "index"
    if low.startswith("30-projects/"):
        return "projects"
    if low.startswith("30-workbench/"):
        return "workbench"
    if low.startswith("90-system/"):
        return "system"
    if low.startswith("99-assets/"):
        return "assets"
    if "10-artifacts/" in low:
        return "artifacts"
    if "21-library/" in low:
        return "library"
    if "22-atomics/" in low:
        return "atomics"
    if "23-moc/" in low:
        return "moc"
    if "24-slices/" in low:
        return "slices"
    if "25-slip/" in low:
        return "slip"
    if "97-attic/" in low:
        return "attic"
    return "other"


def _classify_file_kind(rel_path: str) -> str:
    low = rel_path.lower()
    if low.startswith("10-index/") and (low.endswith(".base") or low == "10-index/readme.md"):
        return "index"
    if low.endswith(".md"):
        return "note"
    if low.endswith(".canvas"):
        return "canvas"
    return "asset"


def _parse_doc_git_porcelain_z(
    raw_out: str,
    note_rel_to_ref: Optional[Dict[str, str]] = None,
    *,
    max_items: int = 40,
) -> Dict[str, Any]:
    """Parse `git status --porcelain=v1 -z -uall` output into strictly path-free change metrics."""
    tokens = raw_out.split("\0")
    added = 0
    modified = 0
    deleted = 0
    renamed = 0
    zone_summary: Dict[str, Dict[str, int]] = {}
    items: List[Dict[str, Any]] = []
    idx = 0
    total = 0
    rel_map = note_rel_to_ref or {}

    while idx < len(tokens):
        tok = tokens[idx]
        idx += 1
        if not tok or len(tok) < 4:
            continue
        xy = tok[:2]
        rel = tok[3:].replace("\\", "/")
        is_rename = xy[0] in ("R", "C") or xy[1] in ("R", "C")
        if is_rename and idx < len(tokens):
            idx += 1

        if xy == "??" or "A" in xy:
            ckind = "added"
            added += 1
        elif is_rename:
            ckind = "renamed"
            renamed += 1
        elif "D" in xy:
            ckind = "deleted"
            deleted += 1
        else:
            ckind = "modified"
            modified += 1

        total += 1
        zone = _doc_zone_for_rel(rel)
        z_bucket = zone_summary.setdefault(zone, {"added": 0, "modified": 0, "deleted": 0, "renamed": 0})
        z_bucket[ckind] += 1

        if len(items) < max_items:
            digest = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:8]
            nref = rel_map.get(rel) if ckind != "deleted" else None
            entry: Dict[str, Any] = {
                "handle": nref or f"chg_{ckind[:3]}_{digest}",
                "change_kind": ckind,
                "file_kind": _classify_file_kind(rel),
                "zone": zone,
            }
            if nref:
                entry["note_ref"] = nref
            items.append(entry)

    return {
        "dirty_count": total,
        "added_count": added,
        "modified_count": modified,
        "deleted_count": deleted,
        "renamed_count": renamed,
        "has_deletions": deleted > 0,
        "human_changes_detected": None if total else False,
        "change_origin": "unknown" if total else "clean",
        "pending_changes_detected": total > 0,
        "zone_summary": zone_summary,
        "items": items,
        "items_truncated": total > len(items),
        "path_sealed": True,
    }


def get_vault_doc_remote(
    state_dir: Path,
    vault_ref: str,
    vault_dir: Path,
    *,
    fetch: bool = False,
) -> Dict[str, Any]:
    """Read Vault DOC Git remote binding, sync policy, and local ahead/behind status (zero-path safe)."""
    base: Dict[str, Any] = {
        "configured": False,
        "remote_name": "origin",
        "remote_url": None,
        "branch": "main",
        "auto_push": True,
        "ahead": 0,
        "behind": 0,
        "tracking_ref_ready": False,
        "gh_cli_available": _gh_binary() is not None,
        "fetched": False,
    }
    if not _git_available():
        return base
    git_dir, _mode = resolve_vault_git_dir(state_dir, vault_ref, vault_dir)
    if not (git_dir / "HEAD").is_file() and not git_dir.is_file():
        return base

    raw_url = _git_config_get(git_dir, vault_dir, "remote.origin.url")
    branch = _git_config_get(git_dir, vault_dir, "gewu.syncBranch") or "main"
    auto_raw = _git_config_get(git_dir, vault_dir, "gewu.autoPush")
    auto_push = True if auto_raw is None else (auto_raw.lower() in ("true", "1", "yes", "on"))
    base["branch"] = branch
    base["auto_push"] = auto_push

    if not raw_url:
        return base

    base["configured"] = True
    base["remote_url"] = _sanitize_remote_url_for_display(raw_url)

    if fetch:
        rc_f, _, err_f = _run_git(git_dir, vault_dir, ["fetch", "--prune", "origin"], timeout=45, network=True)
        if rc_f == 0:
            base["fetched"] = True
        else:
            st, msg = _classify_git_remote_error(err_f, rc_f)
            base["fetch_error"] = st
            base["fetch_detail"] = msg

    remote_ref = f"refs/remotes/origin/{branch}"
    rc_ref, _, _ = _run_git(git_dir, vault_dir, ["rev-parse", "--verify", remote_ref])
    if rc_ref == 0:
        base["tracking_ref_ready"] = True
        rc_mb, _, _ = _run_git(git_dir, vault_dir, ["merge-base", "HEAD", remote_ref])
        if rc_mb != 0:
            base["unrelated_histories"] = True
        else:
            base["unrelated_histories"] = False
            rc_lr, out_lr, _ = _run_git(git_dir, vault_dir, ["rev-list", "--left-right", "--count", f"HEAD...{remote_ref}"])
            if rc_lr == 0:
                parts = out_lr.strip().split()
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    base["ahead"] = int(parts[0])
                    base["behind"] = int(parts[1])
    return base


def _ensure_vault_git_layout(
    state_dir: Path,
    vault_ref: str,
    vault_dir: Path,
    git_layout: str,
) -> str:
    """Safely switch a Vault's DOC Git dir between `detached` (runtime state) and `in-vault` (Obsidian Git compatible)."""
    if git_layout not in ("in-vault", "detached"):
        raise VaultStoreError("E_PARAM_INVALID", "git_layout 仅支持 in-vault 或 detached", exit_code=1)
    in_vault = vault_dir / ".git"
    detached = state_dir / "atlas-git" / f"{vault_ref}.git"
    _check_no_symlink(in_vault)
    if git_layout == "in-vault":
        if in_vault.exists():
            ensure_vault_git_exclude(in_vault)
            return "in_vault_git"
        if (detached / "HEAD").is_file():
            shutil.move(str(detached), str(in_vault))
            try:
                os.chmod(in_vault, 0o755)
            except OSError:
                pass
            _run_git(in_vault, vault_dir, ["config", "core.bare", "false"])
            _run_git(in_vault, vault_dir, ["config", "--unset-all", "core.worktree"])
            ensure_vault_git_exclude(in_vault)
            return "in_vault_git"
        proc = subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=str(vault_dir),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if proc.returncode != 0:
            subprocess.run(["git", "init"], cwd=str(vault_dir), capture_output=True, text=True, timeout=20, check=False)
        ensure_vault_git_exclude(in_vault)
        return "in_vault_git"

    # git_layout == "detached"
    if not in_vault.exists():
        return "detached_git_dir"
    if not in_vault.is_dir():
        raise VaultStoreError("E_PARAM_CONFLICT", "Vault 内 .git 不是标准目录，拒绝自动迁移至 detached", exit_code=1)
    if detached.exists():
        raise VaultStoreError("E_PARAM_CONFLICT", "运行态已存在同名 detached DOC Git 目录，拒绝覆盖迁移", exit_code=1)
    _ensure_0700_dir(detached.parent)
    shutil.move(str(in_vault), str(detached))
    _ensure_0700_dir(detached)
    _run_git(detached, vault_dir, ["config", "core.bare", "false"])
    ensure_vault_git_exclude(detached)
    return "detached_git_dir"


def configure_vault_doc_remote(
    *,
    state_dir: Path,
    vault_ref: str,
    vault_dir: Path,
    remote_url: Optional[str] = None,
    branch: Optional[str] = None,
    auto_push: Optional[bool] = None,
    git_layout: Optional[str] = None,
    clear: bool = False,
) -> Dict[str, Any]:
    """Bind, update, inspect, or clear a Vault's DOC Git remote (e.g., GitHub repo via gh CLI)."""
    changed = False
    if git_layout is not None:
        _ensure_vault_git_layout(state_dir, vault_ref, vault_dir, git_layout)
        changed = True
    init_info = init_vault_git_repo(state_dir, vault_ref, vault_dir)
    if not init_info.get("initialized"):
        raise VaultStoreError("E_HALT_GIT", "当前环境未安装 Git 或 DOC 版本库初始化失败", exit_code=2)
    git_dir, mode = resolve_vault_git_dir(state_dir, vault_ref, vault_dir)
    ensure_vault_git_exclude(git_dir)

    if clear:
        _run_git(git_dir, vault_dir, ["remote", "remove", "origin"])
        _run_git(git_dir, vault_dir, ["config", "--unset-all", "gewu.syncBranch"])
        _run_git(git_dir, vault_dir, ["config", "--unset-all", "gewu.autoPush"])
        changed = True
    else:
        if remote_url is not None:
            valid_url = _validate_remote_url(remote_url)
            existing_url = _git_config_get(git_dir, vault_dir, "remote.origin.url")
            if existing_url is None:
                rc, _, err = _run_git(git_dir, vault_dir, ["remote", "add", "origin", valid_url])
            else:
                rc, _, err = _run_git(git_dir, vault_dir, ["remote", "set-url", "origin", valid_url])
            if rc != 0:
                raise VaultStoreError("E_PARAM_INVALID", f"绑定远端失败: {_sanitize_git_msg_part(err, 'remote_error')}", exit_code=1)
            if _git_config_get(git_dir, vault_dir, "gewu.autoPush") is None and auto_push is None:
                _run_git(git_dir, vault_dir, ["config", "gewu.autoPush", "true"])
            changed = True

        if branch is not None:
            valid_branch = _validate_branch_name(branch)
            _run_git(git_dir, vault_dir, ["config", "gewu.syncBranch", valid_branch])
            # Align initial local branch name when switching default branch cleanly
            rc_cur, out_cur, _ = _run_git(git_dir, vault_dir, ["symbolic-ref", "--short", "HEAD"])
            cur_br = out_cur.strip() if rc_cur == 0 else ""
            if cur_br and cur_br != valid_branch:
                rc_exists, _, _ = _run_git(git_dir, vault_dir, ["show-ref", "--verify", f"refs/heads/{valid_branch}"])
                if rc_exists != 0:
                    _run_git(git_dir, vault_dir, ["branch", "-M", valid_branch])
            changed = True
        elif remote_url is not None and _git_config_get(git_dir, vault_dir, "gewu.syncBranch") is None:
            _run_git(git_dir, vault_dir, ["config", "gewu.syncBranch", "main"])
            rc_cur, out_cur, _ = _run_git(git_dir, vault_dir, ["symbolic-ref", "--short", "HEAD"])
            cur_br = out_cur.strip() if rc_cur == 0 else ""
            if cur_br and cur_br != "main":
                rc_exists, _, _ = _run_git(git_dir, vault_dir, ["show-ref", "--verify", "refs/heads/main"])
                if rc_exists != 0:
                    _run_git(git_dir, vault_dir, ["branch", "-M", "main"])

        if auto_push is not None:
            _run_git(git_dir, vault_dir, ["config", "gewu.autoPush", "true" if auto_push else "false"])
            changed = True

    info = get_vault_doc_remote(state_dir, vault_ref, vault_dir, fetch=False)
    info["mode"] = mode
    info["changed"] = changed
    info["cleared"] = bool(clear)
    info["included_scopes"] = ["dropzone", "journal"]
    info["ignored_scopes"] = ["obsidian", "archives", "source", "trash"]
    info["path_sealed"] = True
    return info


def _has_non_skeleton_untracked(git_dir: Path, vault_dir: Path) -> bool:
    rc, out, _ = _run_git(git_dir, vault_dir, ["status", "--porcelain=v1", "-z", "-uall"])
    if rc != 0:
        return True
    for tok in out.split("\0"):
        if not tok or len(tok) < 4:
            continue
        xy = tok[:2]
        rel = tok[3:].replace("\\", "/")
        if xy == "??":
            if Path(rel).name == ".gitkeep":
                continue
            return True
        return True
    return False


def sync_vault_doc_remote(
    *,
    state_dir: Path,
    vault_ref: str,
    vault_dir: Path,
    direction: str = "both",
    bootstrap: str = "",
    allow_rebase: bool = True,
    force: bool = False,
) -> Dict[str, Any]:
    """Synchronize Vault DOC Git repository with its configured remote (fail-closed on conflict/unrelated history)."""
    if direction not in ("both", "pull", "push"):
        raise VaultStoreError("E_PARAM_INVALID", "direction 仅支持 both|pull|push", exit_code=1)
    if bootstrap not in ("", "adopt-remote"):
        raise VaultStoreError("E_PARAM_INVALID", "bootstrap 仅支持 adopt-remote", exit_code=1)

    init_info = init_vault_git_repo(state_dir, vault_ref, vault_dir)
    if not init_info.get("initialized"):
        return {"synced": False, "status": "git_unavailable", "pulled": False, "pushed": False, "head_changed": False}
    git_dir, mode = resolve_vault_git_dir(state_dir, vault_ref, vault_dir)
    ensure_vault_git_exclude(git_dir)

    raw_url = _git_config_get(git_dir, vault_dir, "remote.origin.url")
    branch = _git_config_get(git_dir, vault_dir, "gewu.syncBranch") or "main"
    auto_raw = _git_config_get(git_dir, vault_dir, "gewu.autoPush")
    auto_push = True if auto_raw is None else (auto_raw.lower() in ("true", "1", "yes", "on"))
    display_url = _sanitize_remote_url_for_display(raw_url)
    if not raw_url:
        return {
            "synced": False,
            "status": "skipped_unconfigured",
            "configured": False,
            "remote_url": None,
            "branch": branch,
            "auto_push": auto_push,
            "pulled": False,
            "pushed": False,
            "head_changed": False,
            "mode": mode,
        }

    _, pre_head_full, _ = _run_git(git_dir, vault_dir, ["rev-parse", "HEAD"])
    pre_head_full = pre_head_full.strip()

    rc_f, _, err_f = _run_git(git_dir, vault_dir, ["fetch", "--prune", "origin"], timeout=45, network=True)
    if rc_f != 0:
        st, msg = _classify_git_remote_error(err_f, rc_f)
        return {
            "synced": False,
            "status": st,
            "configured": True,
            "remote_url": display_url,
            "branch": branch,
            "auto_push": auto_push,
            "pulled": False,
            "pushed": False,
            "head_changed": False,
            "mode": mode,
            "detail": msg,
        }

    remote_ref = f"refs/remotes/origin/{branch}"
    rc_rref, _, _ = _run_git(git_dir, vault_dir, ["rev-parse", "--verify", remote_ref])
    pulled = False
    pushed = False

    if rc_rref != 0:
        # Remote branch does not exist yet (brand new empty GitHub repo)
        if direction == "pull":
            _, post_head, _ = _run_git(git_dir, vault_dir, ["rev-parse", "--short", "HEAD"])
            return {
                "synced": True,
                "status": "remote_empty",
                "configured": True,
                "remote_url": display_url,
                "branch": branch,
                "auto_push": auto_push,
                "pulled": False,
                "pushed": False,
                "head_changed": False,
                "head_short": post_head.strip() or None,
                "mode": mode,
            }
        rc_p, _, err_p = _run_git(git_dir, vault_dir, ["push", "-u", "origin", f"HEAD:refs/heads/{branch}"], timeout=60, network=True)
        if rc_p != 0:
            st, msg = _classify_git_remote_error(err_p, rc_p)
            return {
                "synced": False,
                "status": st,
                "configured": True,
                "remote_url": display_url,
                "branch": branch,
                "auto_push": auto_push,
                "pulled": False,
                "pushed": False,
                "head_changed": False,
                "mode": mode,
                "detail": msg,
            }
        _, post_head, _ = _run_git(git_dir, vault_dir, ["rev-parse", "--short", "HEAD"])
        return {
            "synced": True,
            "status": "pushed",
            "configured": True,
            "remote_url": display_url,
            "branch": branch,
            "auto_push": auto_push,
            "pulled": False,
            "pushed": True,
            "head_changed": False,
            "head_short": post_head.strip() or None,
            "ahead": 0,
            "behind": 0,
            "mode": mode,
        }

    # Remote branch exists: check common ancestor
    rc_mb, _, _ = _run_git(git_dir, vault_dir, ["merge-base", "HEAD", remote_ref])
    if rc_mb != 0:
        if bootstrap == "adopt-remote":
            rc_cnt, out_cnt, _ = _run_git(git_dir, vault_dir, ["rev-list", "--count", "HEAD"])
            local_commits = int(out_cnt.strip()) if rc_cnt == 0 and out_cnt.strip().isdigit() else 999
            has_extra_local = _has_non_skeleton_untracked(git_dir, vault_dir)
            if (local_commits > 1 or has_extra_local) and not force:
                return {
                    "synced": False,
                    "status": "unrelated_histories_local_nonempty",
                    "requires_approval": True,
                    "configured": True,
                    "remote_url": display_url,
                    "branch": branch,
                    "auto_push": auto_push,
                    "pulled": False,
                    "pushed": False,
                    "head_changed": False,
                    "mode": mode,
                    "detail": "本地存在额外提交或非占位未追踪文件；如需以远端历史覆盖对齐，请人工确认后加传 --force true",
                }
            # Cleanly adopt remote branch history & working tree (replacing initial skeleton)
            rc_co, _, err_co = _run_git(git_dir, vault_dir, ["checkout", "-f", "-B", branch, remote_ref])
            if rc_co != 0:
                return {
                    "synced": False,
                    "status": "bootstrap_failed",
                    "configured": True,
                    "remote_url": display_url,
                    "branch": branch,
                    "auto_push": auto_push,
                    "pulled": False,
                    "pushed": False,
                    "head_changed": False,
                    "mode": mode,
                    "detail": _sanitize_git_msg_part(err_co, "checkout_failed"),
                }
            _run_git(git_dir, vault_dir, ["branch", f"--set-upstream-to=origin/{branch}", branch])
            ensure_vault_git_exclude(git_dir)
            _, post_head, _ = _run_git(git_dir, vault_dir, ["rev-parse", "--short", "HEAD"])
            _, post_head_full, _ = _run_git(git_dir, vault_dir, ["rev-parse", "HEAD"])
            return {
                "synced": True,
                "status": "bootstrapped_from_remote",
                "configured": True,
                "remote_url": display_url,
                "branch": branch,
                "auto_push": auto_push,
                "pulled": True,
                "pushed": False,
                "head_changed": post_head_full.strip() != pre_head_full,
                "head_short": post_head.strip() or None,
                "ahead": 0,
                "behind": 0,
                "mode": mode,
            }
        return {
            "synced": False,
            "status": "unrelated_histories",
            "requires_approval": True,
            "configured": True,
            "remote_url": display_url,
            "branch": branch,
            "auto_push": auto_push,
            "pulled": False,
            "pushed": False,
            "head_changed": False,
            "mode": mode,
            "detail": "本地 DOC 仓与远端仓库无共同提交历史（典型场景：新建空 Vault 首次对接已有远端库）；请确认后使用 doc-sync --bootstrap adopt-remote 对齐远端内容，严禁强推覆盖",
        }

    # Common ancestor exists: compute ahead/behind
    ahead, behind = 0, 0
    rc_lr, out_lr, _ = _run_git(git_dir, vault_dir, ["rev-list", "--left-right", "--count", f"HEAD...{remote_ref}"])
    if rc_lr == 0:
        parts = out_lr.strip().split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            ahead, behind = int(parts[0]), int(parts[1])

    if behind > 0 and (direction in ("pull", "both") or (direction == "push" and allow_rebase)):
        if ahead == 0:
            rc_mg, _, err_mg = _run_git(git_dir, vault_dir, ["merge", "--ff-only", remote_ref])
            if rc_mg != 0:
                return {
                    "synced": False,
                    "status": "diverged",
                    "requires_approval": True,
                    "configured": True,
                    "remote_url": display_url,
                    "branch": branch,
                    "auto_push": auto_push,
                    "pulled": False,
                    "pushed": False,
                    "head_changed": False,
                    "ahead": ahead,
                    "behind": behind,
                    "mode": mode,
                    "detail": _sanitize_git_msg_part(err_mg, "ff_only_failed"),
                }
            pulled = True
        elif allow_rebase:
            rc_rb, _, _err_rb = _run_git(git_dir, vault_dir, ["rebase", remote_ref])
            if rc_rb != 0:
                _run_git(git_dir, vault_dir, ["rebase", "--abort"])
                return {
                    "synced": False,
                    "status": "diverged",
                    "requires_approval": True,
                    "configured": True,
                    "remote_url": display_url,
                    "branch": branch,
                    "auto_push": auto_push,
                    "pulled": False,
                    "pushed": False,
                    "head_changed": False,
                    "ahead": ahead,
                    "behind": behind,
                    "mode": mode,
                    "detail": "本地提交与远端存在同文件编辑冲突，已自动中止变基并保全双方现场，请人工协调后重试",
                }
            pulled = True
        else:
            return {
                "synced": False,
                "status": "diverged",
                "requires_approval": True,
                "configured": True,
                "remote_url": display_url,
                "branch": branch,
                "auto_push": auto_push,
                "pulled": False,
                "pushed": False,
                "head_changed": False,
                "ahead": ahead,
                "behind": behind,
                "mode": mode,
                "detail": "远端领先本地提交，请先执行 doc-sync --direction both 合并后再推送",
            }

    # Recompute ahead/behind after pull/rebase
    rc_lr2, out_lr2, _ = _run_git(git_dir, vault_dir, ["rev-list", "--left-right", "--count", f"HEAD...{remote_ref}"])
    if rc_lr2 == 0:
        parts2 = out_lr2.strip().split()
        if len(parts2) == 2 and parts2[0].isdigit() and parts2[1].isdigit():
            ahead, behind = int(parts2[0]), int(parts2[1])

    if direction in ("push", "both"):
        if behind > 0:
            return {
                "synced": False,
                "status": "diverged",
                "requires_approval": True,
                "configured": True,
                "remote_url": display_url,
                "branch": branch,
                "auto_push": auto_push,
                "pulled": pulled,
                "pushed": False,
                "head_changed": False,
                "ahead": ahead,
                "behind": behind,
                "mode": mode,
                "detail": "远端仍领先本地提交，已阻断推送以防覆盖",
            }
        if ahead > 0:
            rc_p, _, err_p = _run_git(git_dir, vault_dir, ["push", "-u", "origin", f"HEAD:refs/heads/{branch}"], timeout=60, network=True)
            if rc_p != 0:
                st, msg = _classify_git_remote_error(err_p, rc_p)
                _, post_head_full, _ = _run_git(git_dir, vault_dir, ["rev-parse", "HEAD"])
                return {
                    "synced": False,
                    "status": st,
                    "configured": True,
                    "remote_url": display_url,
                    "branch": branch,
                    "auto_push": auto_push,
                    "pulled": pulled,
                    "pushed": False,
                    "head_changed": post_head_full.strip() != pre_head_full,
                    "ahead": ahead,
                    "behind": behind,
                    "mode": mode,
                    "detail": msg,
                }
            pushed = True
            ahead = 0

    ensure_vault_git_exclude(git_dir)
    _, post_head, _ = _run_git(git_dir, vault_dir, ["rev-parse", "--short", "HEAD"])
    _, post_head_full, _ = _run_git(git_dir, vault_dir, ["rev-parse", "HEAD"])
    if pulled and pushed:
        final_status = "pulled_and_pushed"
    elif pushed:
        final_status = "pushed"
    elif pulled:
        final_status = "pulled"
    else:
        final_status = "up_to_date"
    return {
        "synced": True,
        "status": final_status,
        "configured": True,
        "remote_url": display_url,
        "branch": branch,
        "auto_push": auto_push,
        "pulled": pulled,
        "pushed": pushed,
        "head_changed": post_head_full.strip() != pre_head_full,
        "head_short": post_head.strip() or None,
        "ahead": ahead,
        "behind": behind,
        "mode": mode,
    }


def inspect_vault_doc_git_state(
    state_dir: Path,
    vault_ref: str,
    vault_dir: Path,
    note_reg: Optional[Any] = None,
    *,
    fetch: bool = False,
) -> Dict[str, Any]:
    """Read-only inspection of DOC Git working tree classifying human/pending edits without leaking paths."""
    if not _git_available():
        return {"initialized": False, "mode": "git_unavailable", "dirty_count": 0, "human_changes_detected": False}
    git_dir, mode = resolve_vault_git_dir(state_dir, vault_ref, vault_dir)
    if not (git_dir / "HEAD").is_file() and not git_dir.is_file():
        return {"initialized": False, "mode": mode, "dirty_count": 0, "human_changes_detected": False}
    ensure_vault_git_exclude(git_dir)
    _, out_head, _ = _run_git(git_dir, vault_dir, ["rev-parse", "--short", "HEAD"])
    rc_st, out_st, _ = _run_git(git_dir, vault_dir, ["status", "--porcelain=v1", "-z", "-uall"])
    if rc_st != 0:
        return {"initialized": True, "mode": mode, "head_short": out_head.strip() or None, "dirty_count": 0, "error": "git_status_failed"}

    rel_to_ref: Dict[str, str] = {}
    if note_reg is not None:
        rel_to_ref = note_reg.list_vault_handles(vault_ref)

    parsed = _parse_doc_git_porcelain_z(out_st, rel_to_ref)
    parsed["initialized"] = True
    parsed["mode"] = mode
    parsed["head_short"] = out_head.strip() or None
    parsed["clean"] = parsed["dirty_count"] == 0
    parsed["included_scopes"] = ["dropzone", "journal"]
    parsed["ignored_scopes"] = ["obsidian", "archives", "source", "trash"]
    parsed["remote_sync"] = get_vault_doc_remote(state_dir, vault_ref, vault_dir, fetch=fetch)
    return parsed


def run_vault_doc_commit(
    *,
    state_dir: Path,
    vault_ref: str,
    vault_dir: Path,
    agent: str = "Agent",
    ticket_id: str = "",
    reason: str = "",
    phase: str = "post",
    file_count: int = 0,
    commit_mode: str = "standard",
    push: Optional[bool] = None,
) -> Dict[str, Any]:
    """Stage & commit non-ignored Vault DOC changes cleanly without path leaks, optionally auto-pushing to remote."""
    init_info = init_vault_git_repo(state_dir, vault_ref, vault_dir)
    if not init_info.get("initialized"):
        return {"committed": False, "mode": init_info.get("mode"), "phase": phase}
    git_dir, mode = resolve_vault_git_dir(state_dir, vault_ref, vault_dir)
    ensure_vault_git_exclude(git_dir)

    rc_pre, out_pre, _ = _run_git(git_dir, vault_dir, ["status", "--porcelain=v1", "-z", "-uall"])
    breakdown = _parse_doc_git_porcelain_z(out_pre) if rc_pre == 0 else {"dirty_count": 0}

    rc_add, _, err_add = _run_git(git_dir, vault_dir, ["add", "-A"])
    if rc_add != 0:
        return {"committed": False, "phase": phase, "error": "git_add_failed", "detail": _sanitize_git_msg_part(err_add, "add_err")}

    rc_st, out_st, _ = _run_git(git_dir, vault_dir, ["status", "--porcelain=v1", "-z", "-uall"])
    if rc_st != 0:
        return {"committed": False, "phase": phase, "error": "git_status_failed"}
    post_breakdown = _parse_doc_git_porcelain_z(out_st)
    staged_cnt = int(post_breakdown.get("dirty_count", 0) or int(breakdown.get("dirty_count", 0)))
    remote_cfg = get_vault_doc_remote(state_dir, vault_ref, vault_dir, fetch=False)

    def _maybe_push(did_commit: bool) -> Dict[str, Any]:
        if phase == "pre":
            return {"synced": False, "status": "skipped_pre_phase", "configured": remote_cfg["configured"], "auto_push": remote_cfg["auto_push"]}
        if not remote_cfg["configured"]:
            return {"synced": False, "status": "skipped_unconfigured", "configured": False, "auto_push": remote_cfg["auto_push"]}
        if push is False:
            return {"synced": False, "status": "skipped_by_flag", "configured": True, "remote_url": remote_cfg["remote_url"], "branch": remote_cfg["branch"], "auto_push": remote_cfg["auto_push"]}
        should_push = (push is True) or (push is None and remote_cfg["auto_push"] and (did_commit or int(remote_cfg.get("ahead", 0)) > 0))
        if not should_push:
            return {"synced": False, "status": "skipped_auto_push_off" if not remote_cfg["auto_push"] else "up_to_date", "configured": True, "remote_url": remote_cfg["remote_url"], "branch": remote_cfg["branch"], "auto_push": remote_cfg["auto_push"]}
        return sync_vault_doc_remote(
            state_dir=state_dir,
            vault_ref=vault_ref,
            vault_dir=vault_dir,
            direction="both",
            allow_rebase=True,
        )

    if staged_cnt == 0:
        remote_sync = _maybe_push(False)
        _, out_head, _ = _run_git(git_dir, vault_dir, ["rev-parse", "--short", "HEAD"])
        return {
            "committed": False,
            "skipped_clean": True,
            "phase": phase,
            "mode": mode,
            "commit_mode": commit_mode,
            "head_short": out_head.strip() or None,
            "remote_sync": remote_sync,
        }

    safe_agent = _sanitize_git_msg_part(agent, "Agent")
    safe_reason = _sanitize_git_msg_part(reason, "sync DOC vault changes")
    tid_tag = f" ({ticket_id})" if ticket_id else ""
    if phase == "pre":
        msg = f"docs(atlas): [{safe_agent}] pre-commit baseline before batch >20 edits{tid_tag}"
    elif commit_mode == "human-sync":
        msg = (
            f"refactor(content): [{safe_agent}] reconcile {staged_cnt} human manual changes "
            f"(+{post_breakdown.get('added_count', 0)}/~{post_breakdown.get('modified_count', 0)}"
            f"/-{post_breakdown.get('deleted_count', 0)}/R{post_breakdown.get('renamed_count', 0)}){tid_tag}"
        )
    else:
        cnt = max(int(file_count or 0), staged_cnt)
        msg = f"docs(atlas): [{safe_agent}] {safe_reason} ({cnt} files){tid_tag}"

    rc_cm, _, err_cm = _run_git(git_dir, vault_dir, ["commit", "-m", msg])
    if rc_cm != 0:
        return {"committed": False, "phase": phase, "error": "git_commit_failed", "detail": _sanitize_git_msg_part(err_cm, "commit_err")}
    remote_sync = _maybe_push(True)
    _, out_head, _ = _run_git(git_dir, vault_dir, ["rev-parse", "--short", "HEAD"])
    return {
        "committed": True,
        "phase": phase,
        "mode": mode,
        "commit_mode": commit_mode,
        "commit_short": out_head.strip() or None,
        "staged_count": staged_cnt,
        "added_count": post_breakdown.get("added_count", 0),
        "modified_count": post_breakdown.get("modified_count", 0),
        "deleted_count": post_breakdown.get("deleted_count", 0),
        "renamed_count": post_breakdown.get("renamed_count", 0),
        "ticket_id": ticket_id or None,
        "remote_sync": remote_sync,
    }


def doc_git_status_summary(
    state_dir: Path,
    vault_ref: str,
    vault_dir: Path,
    note_reg: Optional[Any] = None,
    *,
    fetch: bool = False,
) -> Dict[str, Any]:
    info = inspect_vault_doc_git_state(state_dir, vault_ref, vault_dir, note_reg=note_reg, fetch=fetch)
    if not info.get("initialized"):
        return {"initialized": False, "mode": info.get("mode")}
    return {
        "initialized": True,
        "mode": info.get("mode"),
        "head_short": info.get("head_short"),
        "dirty_count": info.get("dirty_count", 0),
        "added_count": info.get("added_count", 0),
        "modified_count": info.get("modified_count", 0),
        "deleted_count": info.get("deleted_count", 0),
        "renamed_count": info.get("renamed_count", 0),
        "has_deletions": bool(info.get("has_deletions")),
        "human_changes_detected": info.get("human_changes_detected"),
        "change_origin": info.get("change_origin", "unknown"),
        "pending_changes_detected": int(info.get("dirty_count", 0)) > 0,
        "clean": bool(info.get("clean")),
        "remote_sync": info.get("remote_sync"),
    }


# ---------------------------------------------------------------------------
# 30-Day Retention Cleanup (Trash / Undo / Changes / Vault Tickets)
# ---------------------------------------------------------------------------

def _is_trashed_journal_file(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    name = path.name
    if re.match(r"^\d{4}-\d{2}-\d{2}(?:\.\d+)?\.md$", name):
        return True
    if path.suffix.lower() == ".md":
        try:
            meta, _b, _h = extract_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
            if str(meta.get("doc_type") or "").strip().lower() == "journal":
                return True
        except Exception:
            pass
    return False


def trash_purge(
    *,
    vault_dir: Path,
    vault_ref: str,
    trash_reg: KekTrashRegistry,
    days: int = DEFAULT_RETENTION_DAYS,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Purge `.trash/` entries & KEK trash records older than `days` (default 30), never deleting journals."""
    d_val = max(0, min(3650, int(days)))
    cutoff = _now_dt() - timedelta(days=d_val)
    trash_root = vault_dir / ".trash"
    purged_refs: List[str] = []
    purged_unindexed = 0
    protected_journal_skipped = 0

    rows = trash_reg.sync_vault(vault_dir, vault_ref)
    for row in rows:
        orig_zone = str(row.get("orig_zone") or "").strip().lower()
        orig_rel = str(row.get("orig_rel") or "").strip()
        if orig_zone == "journal" or orig_rel.startswith("05-Journal/"):
            protected_journal_skipped += 1
            continue
        dt = _parse_iso_dt(str(row.get("deleted_at") or row.get("trashed_at") or ""))
        if dt is None or dt >= cutoff:
            continue
        tref = str(row.get("trash_ref") or "")
        trash_rel = str(row.get("trash_rel") or "")
        tp = (vault_dir / trash_rel) if trash_rel else None
        if tp is not None and _is_trashed_journal_file(tp):
            protected_journal_skipped += 1
            continue
        purged_refs.append(tref)
        if not dry_run and tp is not None:
            if tp.exists() and not tp.is_symlink():
                if tp.is_dir():
                    shutil.rmtree(tp, ignore_errors=True)
                else:
                    tp.unlink(missing_ok=True)

    if not dry_run and purged_refs:
        # Remove purged rows cleanly from encrypted registry
        doc = trash_reg._load_doc()
        purged_set = set(purged_refs)
        doc["rows"] = [r for r in doc.get("rows", []) if r.get("trash_ref") not in purged_set]
        trash_reg._save_doc(doc)

    # Also sweep unindexed legacy `.trash/` files/dirs (e.g. from clean-attachments or user drags) older than cutoff
    tracked_after = {str(r.get("trash_rel") or "") for r in trash_reg._load_doc().get("rows", [])}
    if trash_root.is_dir() and not trash_root.is_symlink():
        for entry in sorted(trash_root.iterdir()):
            if entry.is_symlink() or entry.name in (".gitkeep", ".DS_Store"):
                continue
            rel = f".trash/{entry.name}"
            if rel in tracked_after:
                continue
            if _is_trashed_journal_file(entry):
                protected_journal_skipped += 1
                continue
            mtime_dt = datetime.fromtimestamp(entry.stat().st_mtime, tz=cutoff.tzinfo)
            if mtime_dt < cutoff:
                purged_unindexed += 1
                if not dry_run:
                    if entry.is_dir():
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        entry.unlink(missing_ok=True)

    return {
        "vault_ref": vault_ref,
        "retention_days": d_val,
        "dry_run": bool(dry_run),
        "purged_trash_refs": purged_refs,
        "purged_indexed_count": len(purged_refs),
        "purged_unindexed_count": purged_unindexed,
        "protected_journal_skipped": protected_journal_skipped,
        "journal_policy": "archive_only_never_delete",
        "path_sealed": True,
    }


def _purge_expired_project_undo(state_dir: Path, vault_ref: str, days: int, dry_run: bool) -> int:
    u_dir = state_dir / "atlas-project-undo" / vault_ref
    if not u_dir.is_dir() or u_dir.is_symlink():
        return 0
    cutoff = _now_dt() - timedelta(days=max(0, int(days)))
    removed = 0
    for meta_file in sorted(u_dir.glob("op_*.json")):
        if meta_file.is_symlink() or not meta_file.is_file():
            continue
        dt: Optional[datetime] = None
        try:
            doc = json.loads(meta_file.read_text(encoding="utf-8"))
            # Unresolved recovery evidence is not ordinary expired undo history.
            status = doc.get("status")
            if status in ("prepared", "undo_started", "undo_failed_restored"):
                continue
            if status != "undone" and (u_dir / meta_file.stem / "undo-current").exists():
                continue
            dt = _parse_iso_dt(str(doc.get("created_at") or ""))
        except Exception:
            continue
        if dt is None:
            dt = datetime.fromtimestamp(meta_file.stat().st_mtime, tz=cutoff.tzinfo)
        if dt < cutoff:
            removed += 1
            if not dry_run:
                snap_dir = u_dir / meta_file.stem
                meta_file.unlink(missing_ok=True)
                if snap_dir.is_dir() and not snap_dir.is_symlink():
                    shutil.rmtree(snap_dir, ignore_errors=True)
    return removed


def _purge_expired_vault_tickets(vault_dir: Path, days: int, dry_run: bool) -> int:
    cutoff = _now_dt() - timedelta(days=max(0, int(days)))
    purged = 0
    for p in sorted(vault_dir.rglob("TICKET-*.md")):
        if p.is_symlink() or not p.is_file():
            continue
        rel = p.relative_to(vault_dir).as_posix()
        if "/98-Archives/Tickets/" not in f"/{rel}" and "98-Archives/Tickets/" not in rel:
            continue
        dt: Optional[datetime] = None
        try:
            meta, _b, _h = extract_frontmatter(p.read_text(encoding="utf-8"))
            dt = _parse_iso_dt(str(meta.get("closed_at") or meta.get("archived_at") or meta.get("updated") or ""))
        except Exception:
            dt = None
        if dt is None:
            dt = datetime.fromtimestamp(p.stat().st_mtime, tz=cutoff.tzinfo)
        if dt < cutoff:
            purged += 1
            if not dry_run:
                p.unlink(missing_ok=True)
    return purged


def retention_sweep(
    *,
    vault_dir: Path,
    vault_ref: str,
    state_dir: Path,
    app_id: str,
    kek: bytes,
    trash_reg: KekTrashRegistry,
    chg_reg: KekChangeRegistry,
    days: int = DEFAULT_RETENTION_DAYS,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Sweep >30-day retention artifacts: `.trash/`, undo snapshots & logs, project undo history & Vault ticket archives."""
    d_val = max(0, min(3650, int(days)))
    tr_res = trash_purge(vault_dir=vault_dir, vault_ref=vault_ref, trash_reg=trash_reg, days=d_val, dry_run=dry_run)
    chg_res = chg_reg.purge_older_than(vault_dir, days=d_val, dry_run=dry_run)
    proj_undo_purged = _purge_expired_project_undo(state_dir, vault_ref, days=d_val, dry_run=dry_run)
    vault_tkt_purged = _purge_expired_vault_tickets(vault_dir, days=d_val, dry_run=dry_run)
    return {
        "vault_ref": vault_ref,
        "retention_days": d_val,
        "dry_run": bool(dry_run),
        "trash_purged_indexed": tr_res["purged_indexed_count"],
        "trash_purged_unindexed": tr_res["purged_unindexed_count"],
        "undo_purged": chg_res["expired_change_count"],
        "change_logs_purged": chg_res["removed_change_logs"],
        "project_undo_purged": proj_undo_purged,
        "vault_tickets_purged": vault_tkt_purged,
        "protected_journal_skipped": tr_res["protected_journal_skipped"],
        "journal_policy": "archive_only_never_delete",
        "path_sealed": True,
    }


def estimate_affected_files(
    *,
    action: str,
    vault_dir: Path,
    args: Dict[str, Any],
    proj_reg: Any,
    vault_ref: str,
) -> int:
    """Fast read-only estimate of how many files an action may touch before execution."""
    if action in ("note-route",):
        if args.get("note_ref"):
            return 1
        dz = vault_dir / "00-DropZone"
        if dz.is_dir():
            return max(1, sum(1 for p in dz.rglob("*.md") if p.is_file()))
        return 1

    if action == "note-format":
        if args.get("note_ref"):
            return 1
        return max(1, sum(1 for p in vault_dir.rglob("*.md") if p.is_file() and ".trash" not in p.parts and "90-System" not in p.parts))

    if action == "clean-attachments":
        if not bool(args.get("apply", False)):
            return 0
        att_dir = vault_dir / "99-Assets" / "10-Attachments"
        cnt = sum(1 for p in att_dir.rglob("*") if p.is_file()) if att_dir.is_dir() else 0
        return max(1, cnt)

    if action in ("project-merge", "project-split", "project-archive", "project-reopen", "project-trash", "project-rename"):
        try:
            refs: List[str] = []
            if args.get("source_refs"):
                refs.extend([x.strip() for x in str(args["source_refs"]).split(",") if x.strip()])
            if args.get("project_ref"):
                refs.append(str(args["project_ref"]).strip())
            total = 0
            for pr in refs:
                rec = proj_reg.resolve_handle(vault_ref, pr)
                pdir = vault_dir / str(rec["project_rel_path"])
                if pdir.is_dir():
                    total += sum(1 for p in pdir.rglob("*") if p.is_file() and p.name != ".gitkeep")
            return max(1, total)
        except Exception:
            return 1

    if action in ("project-untrash",):
        return 25 if args.get("trash_ref") else 0

    if action in ("vfs-move", "vfs-copy", "vfs-trash"):
        if args.get("subdir"):
            # Could move/copy/trash an entire folder
            for sub in vault_dir.rglob(str(args["subdir"]).strip("/")):
                if sub.is_dir():
                    return max(1, sum(1 for p in sub.rglob("*") if p.is_file() and p.name != ".gitkeep"))
        return 1

    if action in ("note-slice", "atomic-extract", "atomic-approve", "moc-sync"):
        return 3 if action == "note-slice" else 2

    if action in ("trash-purge", "retention-sweep", "archive-pack"):
        return 0

    return 1
