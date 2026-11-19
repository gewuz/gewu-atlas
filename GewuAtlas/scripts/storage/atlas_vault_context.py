"""Encrypted routing preferences; no business content and no workflow state."""
from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
from contextlib import contextmanager
from pathlib import Path

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


def _os_user_tag() -> str:
    if hasattr(os, "getuid"):
        return str(os.getuid())
    if sys.platform == "win32":
        try:
            class _TOKEN_USER(ctypes.Structure):
                _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", _wintypes.DWORD)]

            advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
            _K32.GetCurrentProcess.restype = _wintypes.HANDLE
            _K32.CloseHandle.argtypes = [_wintypes.HANDLE]
            _K32.LocalFree.argtypes = [ctypes.c_void_p]
            advapi32.OpenProcessToken.argtypes = [_wintypes.HANDLE, _wintypes.DWORD, ctypes.POINTER(_wintypes.HANDLE)]
            advapi32.OpenProcessToken.restype = _wintypes.BOOL
            advapi32.GetTokenInformation.argtypes = [_wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, _wintypes.DWORD, ctypes.POINTER(_wintypes.DWORD)]
            advapi32.GetTokenInformation.restype = _wintypes.BOOL
            advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_wintypes.LPWSTR)]
            advapi32.ConvertSidToStringSidW.restype = _wintypes.BOOL

            token = _wintypes.HANDLE()
            if advapi32.OpenProcessToken(_K32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
                try:
                    needed = _wintypes.DWORD(0)
                    advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
                    buf = (ctypes.c_ubyte * needed.value)()
                    if advapi32.GetTokenInformation(token, 1, buf, needed, ctypes.byref(needed)):
                        user = _TOKEN_USER.from_buffer(buf)
                        sid_str = _wintypes.LPWSTR()
                        if advapi32.ConvertSidToStringSidW(user.Sid, ctypes.byref(sid_str)):
                            try:
                                if sid_str.value:
                                    return sid_str.value
                            finally:
                                _K32.LocalFree(sid_str)
                finally:
                    _K32.CloseHandle(token)
        except OSError:
            pass
    return re.sub(r"[^A-Za-z0-9_.-]", "_", getpass.getuser())

from storage.vault_kek_store import (
    VaultStoreError, _atomic_write_0600, _check_no_symlink,
    aead_xchacha20_poly1305_encrypt as encrypt,
    aead_xchacha20_poly1305_decrypt as decrypt, hkdf_sha256,
)

CONTEXT_ACTIONS = frozenset({"vault-context", "vault-use", "vault-reset",
                             "vault-default-get", "vault-default-set"})


def session_id(args, environ=None):
    env = os.environ if environ is None else environ
    value = args.get("session") or env.get("GEWU_ATLAS_SESSION_ID") or env.get("CODEX_THREAD_ID")
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value):
        raise VaultStoreError("E_PARAM_INVALID", "会话标识须为稳定的不透明 ID", 1)
    return value


class VaultContext:
    def __init__(self, state_dir: Path, app_id: str, kek: bytes, registry, available=None):
        # Workspace runtime + OS user define the preference namespace.
        user_tag = _os_user_tag()
        self.path = state_dir / ("atlas-vault-context-" + user_tag + ".enc")
        self.aad = (app_id + ":vault-context:1:" + user_tag).encode()
        self.key = hkdf_sha256(kek, self.aad, b"context-container")
        self.field_key = hkdf_sha256(kek, self.aad, b"context-preferences")
        self.registry = registry
        self.available = available

    def _seal(self, key, raw):
        nonce = secrets.token_bytes(24)
        ciphertext, tag = encrypt(key, nonce, raw, self.aad)
        return nonce + tag + ciphertext

    def _open(self, key, raw):
        if len(raw) < 40:
            raise ValueError("invalid context container")
        return decrypt(key, raw[:24], raw[40:], self.aad, raw[24:40])

    @contextmanager
    def transaction(self):
        _check_no_symlink(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = self.path.with_suffix(".lock")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            _lock_ex(fd)
            if self.path.exists():
                try:
                    raw = self._open(self.field_key, self._open(self.key, self.path.read_bytes()))
                    state = json.loads(raw)
                    if state.get("version") != 1 or not isinstance(state["sessions"], dict):
                        raise ValueError("invalid context schema")
                except Exception as exc:
                    raise VaultStoreError("E_HALT_SECURITY", "库上下文校验失败，禁止回退或覆盖", 2) from exc
            else:
                state = {"version": 1, "default": None, "sessions": {}}
            before = json.dumps(state, sort_keys=True)
            yield state
            after = json.dumps(state, sort_keys=True)
            if after != before:
                _atomic_write_0600(self.path, self._seal(self.key, self._seal(self.field_key, after.encode())))
                if sys.platform != "win32":
                    parent_fd = os.open(self.path.parent, os.O_RDONLY)
                    try:
                        os.fsync(parent_fd)
                    finally:
                        os.close(parent_fd)
        finally:
            try:
                _unlock_fd(fd)
            finally:
                os.close(fd)

    def _session_key(self, session):
        if not session:
            raise VaultStoreError("E_VAULT_SESSION_REQUIRED", "宿主未提供会话 ID；请接入 --session 或 GEWU_ATLAS_SESSION_ID，或本次明确指定库", 1)
        return hmac.new(self.key, session.encode(), hashlib.sha256).hexdigest()

    def _valid(self, ref):
        if not ref:
            raise VaultStoreError("E_VAULT_CONTEXT_REQUIRED", "尚未选择知识库，请指定本次目标或进入一个库", 1)
        try:
            row = self.registry.get_vault(ref=ref)
        except VaultStoreError as exc:
            raise VaultStoreError("E_VAULT_CONTEXT_UNAVAILABLE", "已选库不可用，请明确选择其他库；不会自动回退", 1) from exc
        if row.get("deleted") or row.get("status", "active") != "active" or (self.available and not self.available(row)):
            raise VaultStoreError("E_VAULT_CONTEXT_UNAVAILABLE", "已选库不可用，请明确选择其他库；不会自动回退", 1)
        return row

    def command(self, action, session, ref=None, switch=False):
        with self.transaction() as state:
            if action == "vault-default-get":
                selected = state["default"]
                if selected:
                    self._valid(selected)
                return {"default_ref": selected, "selection_source": "default", "path_sealed": True}
            if action == "vault-default-set":
                self._valid(ref)
                if switch:
                    state["sessions"][self._session_key(session)] = ref
                state["default"] = ref
                return {"default_ref": ref, "switched_current": switch, "selection_source": "explicit", "path_sealed": True}
            key = self._session_key(session)
            if action == "vault-use":
                self._valid(ref)
                state["sessions"][key] = ref
                source = "explicit"
            elif action == "vault-reset":
                self._valid(state["default"])
                state["sessions"][key] = state["default"]
                source = "default_reset"
            else:
                source = "session" if key in state["sessions"] else "default"
                if key not in state["sessions"]:
                    state["sessions"][key] = state["default"]
            selected = state["sessions"][key]
            # Persist an unbound session too: future default updates cannot silently bind it.
            if selected:
                self._valid(selected)
            return {"ref": selected, "selection_source": source, "path_sealed": True}

    def resolve(self, session, explicit=None, allow_legacy_federated=False):
        with self.transaction() as state:
            source = "explicit" if explicit else "session"
            if session:
                key = self._session_key(session)
                if key not in state["sessions"]:
                    state["sessions"][key] = state["default"]
                    if not explicit:
                        source = "default"
                selected = explicit or state["sessions"][key]
            elif explicit:
                selected = explicit
            elif state["default"] or not allow_legacy_federated:
                self._session_key(session)
            else:
                return {"ref": None, "selection_source": "legacy_federated"}
            # Explicit targets are validated by the business action (restore may target deleted).
            if not explicit:
                self._valid(selected)
            return {"ref": selected, "selection_source": source}
