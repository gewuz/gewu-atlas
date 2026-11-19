#!/usr/bin/env python3
"""Vault-scoped common utilities for GewuAtlas multimodal converters."""

from __future__ import annotations

import datetime
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

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
    _K32.LockFileEx.argtypes = [_wintypes.HANDLE, _wintypes.DWORD, _wintypes.DWORD, _wintypes.DWORD, _wintypes.DWORD, ctypes.POINTER(_OVERLAPPED)]
    _K32.LockFileEx.restype = _wintypes.BOOL
    _K32.UnlockFileEx.argtypes = [_wintypes.HANDLE, _wintypes.DWORD, _wintypes.DWORD, _wintypes.DWORD, ctypes.POINTER(_OVERLAPPED)]
    _K32.UnlockFileEx.restype = _wintypes.BOOL

    def _lock_ex(fd: int) -> None:
        ov = _OVERLAPPED()
        if not _K32.LockFileEx(_wintypes.HANDLE(msvcrt.get_osfhandle(fd)), 2, 0, 1, 0, ctypes.byref(ov)):
            err = ctypes.get_last_error()
            raise OSError(err, ctypes.FormatError(err))

    def _unlock_fd(fd: int) -> None:
        ov = _OVERLAPPED()
        _K32.UnlockFileEx(_wintypes.HANDLE(msvcrt.get_osfhandle(fd)), 0, 1, 0, ctypes.byref(ov))
else:
    import fcntl

    def _lock_ex(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock_fd(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def get_vault_root() -> Path:
    raw = os.environ.get("GEWU_ATLAS_VAULT_ROOT", "").strip()
    if not raw:
        raise RuntimeError("GEWU_ATLAS_VAULT_ROOT is not set")
    return Path(raw).resolve()


def today() -> str:
    return datetime.date.today().isoformat()


def gate(cond: Any, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


def fm_scalar(val: Any) -> str:
    s = "" if val is None else str(val)
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ")
    return s


def _parse_yaml_block(front: str) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(front)
        if isinstance(loaded, dict):
            return loaded
    except Exception:
        pass
    out: Dict[str, Any] = {}
    for line in front.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or ":" not in s:
            continue
        k, v = s.split(":", 1)
        key = k.strip()
        val = v.strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        elif val == "true":
            val = True
        elif val == "false":
            val = False
        out[key] = val
    return out


def extract_frontmatter(content: str) -> Tuple[Dict[str, Any], str, bool]:
    m = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", content, re.S)
    if not m:
        return {}, content, False
    meta = _parse_yaml_block(m.group(1))
    body = content[m.end() :]
    return meta, body, True


def read_frontmatter(path: Path) -> Tuple[Dict[str, Any], str, bool]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return extract_frontmatter(text)


def replace_frontmatter_field(content: str, key: str, raw_value: str) -> str:
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return content
    end = None
    for idx in range(1, len(lines)):
        if lines[idx].rstrip("\r\n") == "---":
            end = idx
            break
    if end is None:
        return content
    newline = "\r\n" if lines[0].endswith("\r\n") else "\n"
    new_line = f"{key}: {raw_value}" + newline
    for i in range(1, end):
        line = lines[i].rstrip("\r\n")
        if (
            not line.startswith((" ", "\t"))
            and (line == f"{key}:" or line.startswith(f"{key}: ") or line.startswith(f"{key}:\t"))
        ):
            j = i + 1
            while j < end and lines[j].startswith((" ", "\t")):
                j += 1
            lines[i:j] = [new_line]
            return "".join(lines)
    lines.insert(end, new_line)
    return "".join(lines)


def trash_file(path: Any, root: Optional[Path] = None) -> Path:
    raw_p = Path(path)
    if raw_p.is_symlink():
        raise ValueError(f"Refusing to trash symlink directly: {raw_p}")
    p = raw_p.resolve()
    if not p.exists():
        return p
    r = Path(root).resolve() if root else get_vault_root()
    trash_dir = (r / ".trash").resolve()
    if not p.is_relative_to(r) or p == r or p == trash_dir or p.is_relative_to(trash_dir):
        raise ValueError(f"Path outside vault boundary cannot be trashed: {p}")
    trash_dir.mkdir(parents=True, exist_ok=True)
    with (trash_dir / ".trash.lock").open("a", encoding="utf-8") as lock:
        _lock_ex(lock.fileno())
        try:
            dst = trash_dir / p.name
            n = 1
            while os.path.lexists(dst):
                dst = trash_dir / f"{p.name}.{n}"
                n += 1
            shutil.move(str(p), str(dst))
        finally:
            _unlock_fd(lock.fileno())
    return dst
