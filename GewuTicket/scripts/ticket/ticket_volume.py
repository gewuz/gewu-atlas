#!/usr/bin/env python3
"""Static asset Volume manager for GewuTicket (zero exec-bits, ro/rw/shadow-rw overlay, dynamic categories)."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterator, List, Optional, Tuple

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes as _wintypes
    import msvcrt

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", _wintypes.DWORD),
            ("OffsetHigh", _wintypes.DWORD),
            ("hEvent", _wintypes.HANDLE),
        ]

    _K32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def _lock_ex_nb(fd: int) -> None:
        ov = _OVERLAPPED()
        if not _K32.LockFileEx(_wintypes.HANDLE(msvcrt.get_osfhandle(fd)), 3, 0, 1, 0, ctypes.byref(ov)):
            err = ctypes.get_last_error()
            if err == 33:
                raise BlockingIOError(err, "lock busy")
            raise OSError(err, ctypes.FormatError(err))

    def _unlock_fd(fd: int) -> None:
        ov = _OVERLAPPED()
        _K32.UnlockFileEx(_wintypes.HANDLE(msvcrt.get_osfhandle(fd)), 0, 1, 0, ctypes.byref(ov))
else:
    import fcntl

    def _lock_ex_nb(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_fd(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _open_lock_rw(lock_path: Path) -> int:
    safe_canonical_path(lock_path)
    try:
        return os.open(lock_path, os.O_RDWR | _O_NOFOLLOW)
    except FileNotFoundError:
        try:
            return os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR | _O_NOFOLLOW, 0o600)
        except FileExistsError:
            return os.open(lock_path, os.O_RDWR | _O_NOFOLLOW)

CATEGORY_RE = re.compile(r"^[A-Z][A-Z0-9_-]{1,15}$")
STATE_BUCKETS = ("active", "paused", "closed", "archive")
MOUNT_MODES = ("rw", "ro", "shadow-rw")
VALID_TEMPLATES = ("structured_5sec", "freeform", "light_msg")
VALID_PRE_GATES = ("none", "before_pickup", "before_done", "both")
VALID_POST_POLICIES = ("none", "required", "optional")

DEFAULT_CATEGORIES: Dict[str, Dict[str, Any]] = {
    "SYS": {
        "title": "系统开发与维护",
        "template": "structured_5sec",
        "allow_write_scope": True,
        "allow_patch": True,
        "pre_review_gate": "before_pickup",
        "post_review_policy": "optional",
        "archive_after_days": 7,
        "purge_archive_after_days": 30,
        "hold_archive_if_post_unreviewed": True,
    },
    "DOC": {
        "title": "文档与内容流程",
        "template": "freeform",
        "allow_write_scope": False,
        "allow_patch": False,
        "pre_review_gate": "none",
        "post_review_policy": "required",
        "archive_after_days": 7,
        "purge_archive_after_days": 30,
        "hold_archive_if_post_unreviewed": True,
    },
    "MSG": {
        "title": "跨主体通知与评议",
        "template": "light_msg",
        "allow_write_scope": False,
        "allow_patch": False,
        "pre_review_gate": "none",
        "post_review_policy": "none",
        "archive_after_days": 7,
        "purge_archive_after_days": 30,
        "hold_archive_if_post_unreviewed": False,
    },
    "TASK": {
        "title": "任务分配与记录",
        "template": "freeform",
        "allow_write_scope": False,
        "allow_patch": False,
        "pre_review_gate": "none",
        "post_review_policy": "none",
        "archive_after_days": 7,
        "purge_archive_after_days": 30,
        "hold_archive_if_post_unreviewed": False,
    },
    "SLIP": {
        "title": "Slip 书签批注与批处理记录",
        "template": "freeform",
        "allow_write_scope": False,
        "allow_patch": False,
        "pre_review_gate": "none",
        "post_review_policy": "none",
        "archive_after_days": 7,
        "purge_archive_after_days": 30,
        "hold_archive_if_post_unreviewed": False,
    },
}

class VolumeError(Exception):
    """Base structured error for volume operations."""

    def __init__(self, code: str, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_canonical_path(path: Path) -> Path:
    """Resolve OS parent prefixes while rejecting symlink targets."""
    raw = Path(os.path.abspath(str(path)))
    if raw.exists() and raw.is_symlink():
        raise VolumeError("E_PARAM_INVALID", f"不允许符号链接路径: {raw}", exit_code=2)
    return raw.resolve()


def _rel_posix(rel: str) -> PurePosixPath:
    p = PurePosixPath(rel)
    if (
        not rel
        or str(p) != rel
        or p.is_absolute()
        or ".." in p.parts
        or "." in p.parts
        or "\\" in rel
        or ":" in rel
        or "\x00" in rel
    ):
        raise VolumeError("E_PARAM_INVALID", f"非法相对路径: {rel}", exit_code=2)
    return p


def _write_atomic_noexec(path: Path, data: bytes, mode: int = 0o644) -> None:
    if mode & 0o111:
        raise VolumeError("E_VOLUME_EXEC_BIT_FORBIDDEN", "数据卷禁止写入可执行权限位", exit_code=2)
    safe_canonical_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".vol-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def assert_no_exec_bits(root: Path) -> None:
    """Verify every regular file in volume/shadow root carries zero execute bits."""
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dp = Path(dirpath)
        for d in dirnames:
            sub = dp / d
            if sub.is_symlink():
                raise VolumeError("E_VOLUME_EXEC_BIT_FORBIDDEN", f"数据卷禁止符号链接: {sub.name}", exit_code=2)
        for fn in filenames:
            fp = dp / fn
            if fp.is_symlink():
                raise VolumeError("E_VOLUME_EXEC_BIT_FORBIDDEN", f"数据卷禁止符号链接: {fp.name}", exit_code=2)
            st = fp.lstat()
            if stat.S_ISREG(st.st_mode) and (st.st_mode & 0o111) != 0:
                raise VolumeError(
                    "E_VOLUME_EXEC_BIT_FORBIDDEN",
                    f"数据卷检测到非法可执行位文件: {fp.relative_to(root)}",
                    exit_code=2,
                )


def _normalize_category_spec(cat_id: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(cat_id, str) or not CATEGORY_RE.fullmatch(cat_id):
        raise VolumeError("E_PARAM_INVALID", f"分类 ID 非法（须为大写字母开头 2-16 位）: {cat_id}", exit_code=2)
    title = str(spec.get("title") or cat_id).strip()
    tpl = str(spec.get("template") or "freeform").strip()
    pre = str(spec.get("pre_review_gate") or "none").strip()
    post = str(spec.get("post_review_policy") or "none").strip()
    days = int(spec.get("archive_after_days", 7))
    purge_days = int(spec.get("purge_archive_after_days", 30))
    if tpl not in VALID_TEMPLATES:
        raise VolumeError("E_PARAM_INVALID", f"无效模板类型: {tpl}", exit_code=2)
    if pre not in VALID_PRE_GATES:
        raise VolumeError("E_PARAM_INVALID", f"无效前置审阅门禁: {pre}", exit_code=2)
    if post not in VALID_POST_POLICIES:
        raise VolumeError("E_PARAM_INVALID", f"无效完工审阅策略: {post}", exit_code=2)
    if days < 0 or days > 3650:
        raise VolumeError("E_PARAM_INVALID", "archive_after_days 须在 0~3650 之间", exit_code=2)
    if purge_days < 0 or purge_days > 3650:
        raise VolumeError("E_PARAM_INVALID", "purge_archive_after_days 须在 0~3650 之间", exit_code=2)
    return {
        "title": title,
        "template": tpl,
        "allow_write_scope": bool(spec.get("allow_write_scope", False)),
        "allow_patch": bool(spec.get("allow_patch", False)),
        "pre_review_gate": pre,
        "post_review_policy": post,
        "archive_after_days": days,
        "purge_archive_after_days": purge_days,
        "hold_archive_if_post_unreviewed": bool(spec.get("hold_archive_if_post_unreviewed", True)),
    }


class VolumeFS:
    """Unified filesystem view over rw / ro / shadow-rw (Solution A incremental overlay)."""

    def __init__(self, base_root: Path, mount_mode: str = "rw", shadow_root: Optional[Path] = None) -> None:
        if mount_mode not in MOUNT_MODES:
            raise VolumeError("E_PARAM_INVALID", f"不支持的挂载模式: {mount_mode}", exit_code=2)
        self.base_root = safe_canonical_path(base_root)
        if not (self.base_root / "volume.json").is_file():
            raise VolumeError("E_VOLUME_NOT_INITIALIZED", f"目标目录不是有效工单数据卷: {self.base_root.name}", exit_code=1)
        assert_no_exec_bits(self.base_root)
        self.meta = json.loads((self.base_root / "volume.json").read_text("utf-8"))
        self.vol_id = str(self.meta.get("vol_id") or self.base_root.name)
        self.mount_mode = mount_mode
        self._transaction = None
        self.shadow_root: Optional[Path] = None
        if mount_mode == "shadow-rw":
            if (self.base_root / ".ticket-transaction.json").exists():
                raise VolumeError("E_RECOVERY_REQUIRED", "底卷存在中断事务，先用 rw 恢复再挂载影子", 1)
            if shadow_root is None:
                raise VolumeError("E_PARAM_INVALID", "shadow-rw 模式必须解析有效 shadow_root", exit_code=2)
            self.shadow_root = safe_canonical_path(shadow_root)
            self._check_shadow_location()
            self.shadow_root.mkdir(parents=True, exist_ok=True)
            assert_no_exec_bits(self.shadow_root)
            self._ensure_shadow_meta()
        if self._journal_path().exists():
            if self.mount_mode == "ro":
                raise VolumeError("E_RECOVERY_REQUIRED", "存在中断事务，请先以 rw 挂载恢复", 1)
            with self.lock():
                pass

    def _check_shadow_location(self) -> None:
        if self.shadow_root is None:
            raise VolumeError("E_PARAM_INVALID", "需要 shadow-rw 挂载", 2)
        base, shadow = self.base_root.resolve(), self.shadow_root.resolve()
        if base == shadow or base in shadow.parents or shadow in base.parents:
            raise VolumeError("E_SHADOW_OVERLAP", "影子目录与底卷必须独立，不能相同或互相包含", 2)

    def _journal_path(self) -> Path:
        root = self.shadow_root if self.mount_mode == "shadow-rw" else self.base_root
        return root / ".ticket-transaction.json"

    def _remember(self, path: Path) -> None:
        if self._transaction is None:
            return
        root = self.shadow_root if self.mount_mode == "shadow-rw" else self.base_root
        rel = path.relative_to(root).as_posix()
        if rel not in self._transaction:
            self._transaction[rel] = ({"data": base64.b64encode(path.read_bytes()).decode("ascii"),
                                       "mode": stat.S_IMODE(path.stat().st_mode)} if path.is_file() else None)
            _write_atomic_noexec(self._journal_path(), json.dumps(self._transaction).encode(), 0o600)

    def _recover(self) -> None:
        journal = self._journal_path()
        if not journal.exists():
            return
        root = self.shadow_root if self.mount_mode == "shadow-rw" else self.base_root
        entries = json.loads(journal.read_text())
        for rel, old in entries.items():
            path = root / _rel_posix(rel)
            if old is None:
                path.unlink(missing_ok=True)
            else:
                _write_atomic_noexec(path, base64.b64decode(old["data"]), old["mode"])
        journal.unlink()

    def _shadow_meta_path(self) -> Path:
        assert self.shadow_root is not None
        return self.shadow_root / "_shadow_meta.json"

    def _ensure_shadow_meta(self) -> Dict[str, Any]:
        assert self.shadow_root is not None
        mp = self._shadow_meta_path()
        if mp.is_file():
            data = json.loads(mp.read_text("utf-8"))
            if data.get("base_root") != str(self.base_root):
                raise VolumeError("E_SHADOW_BASE_MISMATCH", "影子目录已绑定其他底卷，拒绝混用", exit_code=1)
            return data
        data = {
            "schema": "gewu.ticket-shadow/1",
            "vol_id": self.vol_id,
            "base_root": str(self.base_root),
            "mounted_at": _now_iso(),
            "baseline_hashes": {},
            "tombstones": [],
            "overwritten": [],
        }
        _write_atomic_noexec(mp, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), mode=0o600)
        return data

    def _save_shadow_meta(self, meta: Dict[str, Any]) -> None:
        self._remember(self._shadow_meta_path())
        _write_atomic_noexec(
            self._shadow_meta_path(),
            (json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            mode=0o600,
        )

    def _require_writable(self) -> None:
        if self.mount_mode == "ro":
            raise VolumeError("E_VOLUME_READONLY", "当前数据卷以只读(ro)模式挂载，拒绝写入或状态流转", exit_code=1)

    def _record_baseline_if_needed(self, meta: Dict[str, Any], rel: str) -> None:
        if rel in meta["baseline_hashes"]:
            return
        bp = self.base_root / rel
        meta["baseline_hashes"][rel] = _sha256_bytes(bp.read_bytes()) if bp.is_file() else "absent"

    def exists(self, rel: str) -> bool:
        p = _rel_posix(rel)
        s_rel = str(p)
        if self.mount_mode == "shadow-rw":
            assert self.shadow_root is not None
            smeta = self._ensure_shadow_meta()
            if s_rel in smeta["tombstones"]:
                return False
            if (self.shadow_root / p).is_file():
                return True
        return (self.base_root / p).is_file()

    def read_bytes(self, rel: str) -> bytes:
        p = _rel_posix(rel)
        s_rel = str(p)
        if self.mount_mode == "shadow-rw":
            assert self.shadow_root is not None
            smeta = self._ensure_shadow_meta()
            if s_rel in smeta["tombstones"]:
                raise FileNotFoundError(s_rel)
            sp = self.shadow_root / p
            if sp.is_file():
                return sp.read_bytes()
        bp = self.base_root / p
        if not bp.is_file():
            raise FileNotFoundError(s_rel)
        return bp.read_bytes()

    def read_text(self, rel: str) -> str:
        return self.read_bytes(rel).decode("utf-8")

    def write_bytes(self, rel: str, data: bytes, mode: int = 0o644) -> None:
        self._require_writable()
        p = _rel_posix(rel)
        s_rel = str(p)
        if self.mount_mode == "shadow-rw":
            assert self.shadow_root is not None
            smeta = self._ensure_shadow_meta()
            self._record_baseline_if_needed(smeta, s_rel)
            self._remember(self.shadow_root / p)
            _write_atomic_noexec(self.shadow_root / p, data, mode=mode)
            if s_rel in smeta["tombstones"]:
                smeta["tombstones"] = [x for x in smeta["tombstones"] if x != s_rel]
            if s_rel not in smeta["overwritten"]:
                smeta["overwritten"].append(s_rel)
            self._save_shadow_meta(smeta)
            return
        self._remember(self.base_root / p)
        _write_atomic_noexec(self.base_root / p, data, mode=mode)

    def write_text(self, rel: str, text: str, mode: int = 0o644) -> None:
        self.write_bytes(rel, text.encode("utf-8"), mode=mode)

    def remove_file(self, rel: str) -> None:
        self._require_writable()
        p = _rel_posix(rel)
        s_rel = str(p)
        if self.mount_mode == "shadow-rw":
            assert self.shadow_root is not None
            smeta = self._ensure_shadow_meta()
            self._record_baseline_if_needed(smeta, s_rel)
            sp = self.shadow_root / p
            if sp.is_file():
                self._remember(sp)
                sp.unlink()
            if (self.base_root / p).is_file():
                if s_rel not in smeta["tombstones"]:
                    smeta["tombstones"].append(s_rel)
            smeta["overwritten"] = [x for x in smeta["overwritten"] if x != s_rel]
            self._save_shadow_meta(smeta)
            return
        bp = self.base_root / p
        if bp.is_file():
            self._remember(bp)
            bp.unlink()

    def ensure_dir(self, rel: str) -> None:
        self._require_writable()
        p = _rel_posix(rel)
        target = (self.shadow_root if self.mount_mode == "shadow-rw" else self.base_root) / p
        assert target is not None
        target.mkdir(parents=True, exist_ok=True)

    def list_files(self, rel_prefix: str = "") -> List[str]:
        """List relative POSIX paths under rel_prefix honoring shadow overlay & tombstones."""
        prefix_norm = str(_rel_posix(rel_prefix)) if rel_prefix else ""
        results = set()
        tombstones: set[str] = set()

        def _scan(root: Path) -> None:
            start = root / prefix_norm if prefix_norm else root
            if not start.is_dir():
                return
            for dp, _, fns in os.walk(start, followlinks=False):
                for fn in fns:
                    if fn.startswith(".") or fn == "_shadow_meta.json":
                        continue
                    rel = Path(dp, fn).relative_to(root).as_posix()
                    results.add(rel)

        _scan(self.base_root)
        if self.mount_mode == "shadow-rw" and self.shadow_root is not None:
            smeta = self._ensure_shadow_meta()
            tombstones = set(smeta.get("tombstones", []))
            _scan(self.shadow_root)
        return sorted(r for r in results if r not in tombstones)

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Acquire non-blocking cross-OS kernel lock on stable lock file."""
        if self.mount_mode == "ro":
            yield
            return
        if self.mount_mode == "shadow-rw":
            assert self.shadow_root is not None
            lock_path = self.shadow_root / ".shadow.lock"
        else:
            lock_dir = self.base_root / "_meta" / "locks"
            lock_dir.mkdir(parents=True, exist_ok=True)
            lock_path = lock_dir / "ticket.lock"
        fd = _open_lock_rw(lock_path)
        try:
            try:
                _lock_ex_nb(fd)
            except BlockingIOError as exc:
                raise VolumeError("E_LOCK_BUSY", "工单卷当前正被其他主体锁定，请稍后重试", exit_code=1) from exc
            self._recover()
            self._transaction = {}
            try:
                yield
            except BaseException:
                self._transaction = None
                self._recover()
                raise
            else:
                self._journal_path().unlink(missing_ok=True)
            finally:
                self._transaction = None
        finally:
            try:
                _unlock_fd(fd)
            finally:
                os.close(fd)

    def shadow_diff(self) -> Dict[str, Any]:
        if self.mount_mode != "shadow-rw" or self.shadow_root is None:
            raise VolumeError("E_PARAM_INVALID", "shadow-diff 仅适用于 shadow-rw 挂载模式", exit_code=2)
        smeta = self._ensure_shadow_meta()
        added: List[str] = []
        modified: List[str] = []
        deleted = sorted(smeta.get("tombstones", []))
        drifted: List[str] = []
        for rel in sorted(smeta.get("overwritten", [])):
            if (self.base_root / rel).is_file():
                modified.append(rel)
            else:
                added.append(rel)
        for rel, exp in smeta.get("baseline_hashes", {}).items():
            bp = self.base_root / rel
            cur = _sha256_bytes(bp.read_bytes()) if bp.is_file() else "absent"
            if cur != exp:
                drifted.append(rel)
        return {
            "vol_id": self.vol_id,
            "shadow_dir": str(self.shadow_root),
            "added": added,
            "modified": modified,
            "deleted": deleted,
            "base_drifted": drifted,
        }

    def shadow_apply(self) -> Dict[str, Any]:
        self._check_shadow_location()
        base = VolumeFS(self.base_root)
        # Always shadow then base; no unlocked baseline is trusted.
        with self.lock():
            with base.lock():
                diff = self.shadow_diff()
                if diff["base_drifted"]:
                    raise VolumeError("E_SHADOW_CONFLICT", "底卷已漂移，拒绝覆盖", 1)
                for rel in diff["added"] + diff["modified"]:
                    base.write_bytes(rel, (self.shadow_root / rel).read_bytes())
                for rel in diff["deleted"]:
                    base.remove_file(rel)
                assert_no_exec_bits(self.base_root)
        # Keep lock inode stable. If interrupted here, the overlay remains available
        # for inspection; a subsequent apply refuses baseline drift.
        return {"applied": True, "vol_id": self.vol_id,
                "added": len(diff["added"]), "modified": len(diff["modified"]),
                "deleted": len(diff["deleted"]), "shadow_retained": True}

    def shadow_drop(self) -> Dict[str, Any]:
        self._check_shadow_location()
        with self.lock():
            diff = self.shadow_diff()
            meta = self._ensure_shadow_meta()
            for rel in meta.get("overwritten", []):
                path = self.shadow_root / _rel_posix(rel)
                self._remember(path)
                path.unlink(missing_ok=True)
            meta.update(baseline_hashes={}, tombstones=[], overwritten=[])
            self._save_shadow_meta(meta)
        return {"dropped": True, "vol_id": self.vol_id,
                "discarded_changes": len(diff["added"]) + len(diff["modified"]) + len(diff["deleted"])}


def _init_volume_unlocked(
    vol_root: Path,
    vol_id: Optional[str] = None,
    archive_after_days: int = 7,
    purge_archive_after_days: int = 30,
    categories: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Initialize a zero-exec static Ticket Volume with level-2 categories & level-3 state buckets."""
    root = safe_canonical_path(vol_root)
    if (root / "volume.json").exists():
        assert_no_exec_bits(root)
        meta = json.loads((root / "volume.json").read_text())
        cats = json.loads((root / "_meta" / "categories.json").read_text())
        for cid, spec in cats.items():
            _normalize_category_spec(cid, spec)
        return {"vol_id": meta["vol_id"], "categories": sorted(cats), "buckets": list(STATE_BUCKETS), "already_initialized": True}
    vid = (vol_id or root.name).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", vid):
        raise VolumeError("E_PARAM_INVALID", f"非法卷标识: {vid}", exit_code=2)
    cats_raw = categories if categories is not None else DEFAULT_CATEGORIES
    cats = {cid: _normalize_category_spec(cid, spec) for cid, spec in cats_raw.items()}
    if not 0 <= int(archive_after_days) <= 3650 or not 0 <= int(purge_archive_after_days) <= 3650:
        raise VolumeError("E_PARAM_INVALID", "归档与清理天数须在 0~3650", 2)
    if categories is None:
        for spec in cats.values():
            spec["archive_after_days"] = int(archive_after_days)
    if categories is None and int(purge_archive_after_days) != 30:
        for spec in cats.values():
            spec["purge_archive_after_days"] = max(0, min(3650, int(purge_archive_after_days)))

    root.mkdir(parents=True, exist_ok=True)
    (root / "_meta" / "locks").mkdir(parents=True, exist_ok=True)
    (root / "_meta" / "artifacts").mkdir(parents=True, exist_ok=True)
    for cid in cats:
        for bucket in STATE_BUCKETS:
            (root / cid / bucket).mkdir(parents=True, exist_ok=True)

    vol_meta = {
        "schema": "gewu.ticket-volume/1",
        "vol_id": vid,
        "created_at": _now_iso(),
        "archive_after_days": int(archive_after_days),
        "purge_archive_after_days": int(purge_archive_after_days),
        "exec_bits_forbidden": True,
    }
    _write_atomic_noexec(root / "_meta" / "categories.json", (json.dumps(cats, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"), 0o644)
    if not (root / "INDEX.md").exists():
        _write_atomic_noexec(
            root / "INDEX.md",
            (
                "# 格物工单协同台账\n\n"
                "## 在途工单 (active: open / in_progress)\n\n"
                "| 单号 | 分类 | 主状态 | 审阅状态 | 接手方 | 标题 | 更新时间 |\n"
                "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n\n"
                "## 暂停工单 (paused)\n\n"
                "| 单号 | 分类 | 暂停原因 | 恢复条件 | 更新时间 |\n"
                "| :--- | :--- | :--- | :--- | :--- |\n\n"
                "## 近期结单与待完工审阅 (closed: 7天内)\n\n"
                "| 单号 | 分类 | 终态 | 完工审阅 | 结单时间 | 标题 |\n"
                "| :--- | :--- | :--- | :--- | :--- | :--- |\n"
            ).encode("utf-8"),
            0o644,
        )
    _write_atomic_noexec(root / "volume.json", (json.dumps(vol_meta, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), 0o644)
    assert_no_exec_bits(root)
    return {"vol_id": vid, "categories": sorted(cats.keys()), "buckets": list(STATE_BUCKETS)}


def init_volume(vol_root: Path, vol_id: Optional[str] = None, archive_after_days: int = 7,
                purge_archive_after_days: int = 30, categories=None) -> Dict[str, Any]:
    root = safe_canonical_path(vol_root)
    (root / "_meta" / "locks").mkdir(parents=True, exist_ok=True)
    fd = _open_lock_rw(root / "_meta" / "locks" / "ticket.lock")
    try:
        try:
            _lock_ex_nb(fd)
        except BlockingIOError as exc:
            raise VolumeError("E_LOCK_BUSY", "工单卷正在初始化或写入", 1) from exc
        return _init_volume_unlocked(root, vol_id, archive_after_days, purge_archive_after_days, categories)
    finally:
        _unlock_fd(fd)
        os.close(fd)


def load_categories(fs: VolumeFS) -> Dict[str, Dict[str, Any]]:
    raw = json.loads(fs.read_text("_meta/categories.json"))
    cats = {cid: _normalize_category_spec(cid, spec) for cid, spec in raw.items()}
    if all(k in cats for k in ("SYS", "DOC", "MSG")):
        for builtin_cat in ("TASK", "SLIP"):
            if builtin_cat not in cats:
                cats[builtin_cat] = _normalize_category_spec(builtin_cat, DEFAULT_CATEGORIES[builtin_cat])
    return cats


def register_category(fs: VolumeFS, cat_id: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    norm = _normalize_category_spec(cat_id, spec)
    with fs.lock():
        cats = load_categories(fs)
        cats[cat_id] = norm
        for bucket in STATE_BUCKETS:
            fs.ensure_dir(f"{cat_id}/{bucket}")
        fs.write_text("_meta/categories.json", json.dumps(cats, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {"category": cat_id, "policy": norm}


def default_shadow_dir(runtime_dir: Path, vol_id: str) -> Path:
    safe_vid = re.sub(r"[^A-Za-z0-9_-]", "_", vol_id)
    return safe_canonical_path(runtime_dir) / "shadows" / safe_vid
