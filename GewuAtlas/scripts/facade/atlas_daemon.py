#!/usr/bin/env python3
"""Self-hosted Hot-Index Local Daemon & ultra-light thin client for GewuAtlas.

Design Goals:
- Zero Agent-host MCP configuration required (`git clone` works out-of-the-box).
- Explicit task lifecycle (`./gewu atlas daemon start|status|stop|restart`) + automatic Idle TTL self-destruction.
- Security parity with cold CLI:
  - `0600` AF_UNIX socket + `0600` metadata file inside protected state directory.
  - Dual authentication: OS peer UID check (`LOCAL_PEERCRED` on macOS / `SO_PEERCRED` on Linux) + 256-bit random token.
  - Zero physical paths and zero token exposure on stdout.
  - `code_fingerprint` guard auto-retires stale daemons after `SYS` source changes.
- Fail-open thin client: missing daemon, Windows, strict sandbox blocking AF_UNIX, or `--no_daemon true`
  transparently falls back to direct in-process execution without error.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import secrets
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

PROTOCOL_VERSION = 1
DEFAULT_IDLE_TIMEOUT = 600
MAX_FRAME_BYTES = 16 * 1024 * 1024  # 16 MiB safety cap
_SANDBOX_IPC_EXIT_CODE = 13

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
APP_DIR = SCRIPTS_DIR.parent

_SAFE_FORWARD_ENV_KEYS = (
    "GEWU_SESSION_ID",
    "GEWU_ATLAS_SESSION_ID",
    "CODEX_THREAD_ID",
    "GEWU_ATLAS_WORKSPACE_ROOT",
    "GEWU_APP_RUNTIME_DIR",
    "GEWU_APP_KEK",
    "GEWU_TICKET_VOLUME",
)

_READ_ONLY_DAEMON_ACTIONS = frozenset(
    {
        "vault",
        "vault-list",
        "vault-get",
        "vault-context",
        "vault-default-get",
        "project-list",
        "project-get",
        "project-brief",
        "note-list",
        "note-get",
        "note-search",
        "note-grep",
        "file-read",
        "graph-report",
        "vfs-links",
        "vfs-validate",
        "vfs-trash-list",
        "health-check",
        "schema-check",
        "schema-list",
        "delivery-check",
        "doc-status",
        "change-list",
        "change-get",
        "journal-show",
        "ticket-list",
        "ticket-get",
        "usecase",
    }
)

# Actions that require caller's own network/proxy/sandbox context or heavy external subprocesses
# MUST bypass the long-lived hot daemon and execute directly in the caller process.
_DAEMON_BYPASS_ACTIONS = frozenset(
    {
        "ingest",
        "url-ingest",
        "fmt",
        "doc-sync",
        "doc-commit",
        "doc-remote",
        "archive-pack",
    }
)
_DAEMON_BYPASS_GUARD_MODES = frozenset(
    {
        "doc-sync",
        "doc-commit",
        "doc-remote",
        "archive-pack",
    }
)


def _should_bypass_action(action: str, mode: str = "") -> bool:
    act = str(action or "").strip().lower()
    if not act:
        return False
    if act in _DAEMON_BYPASS_ACTIONS:
        return True
    if act == "guard" and str(mode or "").strip().lower() in _DAEMON_BYPASS_GUARD_MODES:
        return True
    return False


def _extract_argv_action_mode(raw_argv: List[str]) -> Tuple[str, str]:
    action = ""
    mode = ""
    i = 0
    while i < len(raw_argv):
        tok = raw_argv[i]
        if tok == "--action" and i + 1 < len(raw_argv):
            action = str(raw_argv[i + 1]).strip()
            i += 2
            continue
        if tok.startswith("--action="):
            action = tok.split("=", 1)[1].strip()
            i += 1
            continue
        if tok == "--mode" and i + 1 < len(raw_argv):
            mode = str(raw_argv[i + 1]).strip()
            i += 2
            continue
        if tok.startswith("--mode="):
            mode = tok.split("=", 1)[1].strip()
            i += 1
            continue
        i += 1
    return action, mode

_FINGERPRINT_FILES = (
    "scripts/facade/atlas_cli.py",
    "scripts/facade/atlas_hot_kernel.py",
    "scripts/facade/atlas_daemon.py",
    "scripts/notes/atlas_note_engine.py",
    "scripts/notes/atlas_search_engine.py",
    "scripts/storage/note_index_cache.py",
    "scripts/storage/note_kek_store.py",
    "scripts/storage/vault_kek_store.py",
)


def _runtime_state_dir() -> Path:
    custom = os.environ.get("GEWU_APP_RUNTIME_DIR", "").strip()
    base = Path(custom) if custom else (APP_DIR / ".local")
    return base / "state"


def _meta_file_path(state_dir: Path) -> Path:
    return state_dir / "atlas-daemon.meta.json"


def _compute_code_fingerprint() -> str:
    h = hashlib.sha256()
    candidates: List[Path] = []
    scripts_root = APP_DIR / "scripts"
    if scripts_root.is_dir():
        for p in scripts_root.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            candidates.append(p)
    dicts_root = APP_DIR / "dicts"
    if dicts_root.is_dir():
        for p in dicts_root.glob("*.yaml"):
            candidates.append(p)
    if not candidates:
        for rel in _FINGERPRINT_FILES:
            candidates.append(APP_DIR / rel)
    for fp in sorted(candidates, key=lambda item: item.relative_to(APP_DIR).as_posix()):
        rel = fp.relative_to(APP_DIR).as_posix()
        try:
            st = fp.stat()
            h.update(f"{rel}:{st.st_mtime_ns}:{st.st_size}\n".encode("utf-8"))
        except OSError:
            h.update(f"{rel}:missing\n".encode("utf-8"))
    return h.hexdigest()[:24]


def _compute_socket_path(state_dir: Path) -> Path:
    direct = state_dir / "atlas-daemon.sock"
    # macOS sun_path max is 104 bytes; keep generous safety margin for deep/CJK workspace paths.
    if len(str(direct).encode("utf-8")) <= 94:
        return direct
    uid = os.getuid() if hasattr(os, "getuid") else 0
    tmp_base = Path(tempfile.gettempdir())
    if len(str(tmp_base).encode("utf-8")) > 48 and Path("/tmp").is_dir():
        tmp_base = Path("/tmp")
    sock_dir = tmp_base / f"gewu-atlas-sock-{uid}"
    if sock_dir.exists():
        if sock_dir.is_symlink() or not sock_dir.is_dir():
            raise OSError("unsafe socket parent directory")
        if hasattr(os, "getuid") and sock_dir.stat().st_uid != uid:
            raise OSError("socket parent directory uid mismatch")
        try:
            os.chmod(sock_dir, 0o700)
        except OSError:
            pass
    else:
        sock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(sock_dir, 0o700)
    digest = hashlib.sha256(str(state_dir.resolve()).encode("utf-8")).hexdigest()[:16]
    return sock_dir / f"gwa-{digest}.sock"


def _is_permission_error(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in (errno.EPERM, errno.EACCES):
        return True
    return False


def _probe_af_unix_ipc(state_dir: Path) -> Optional[str]:
    """Quick (<5ms) preflight probe to detect sandbox-blocked AF_UNIX IPC without waiting 5s."""
    probe_path: Optional[Path] = None
    srv: Optional[socket.socket] = None
    cli: Optional[socket.socket] = None
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        sock_dir = _compute_socket_path(state_dir).parent
        sock_dir.mkdir(parents=True, exist_ok=True)
        probe_path = sock_dir / f".p-{os.getpid()}-{secrets.token_hex(3)}.s"
        if len(str(probe_path).encode("utf-8")) > 100:
            return None
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(probe_path))
        srv.listen(1)
        cli = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        cli.settimeout(0.5)
        cli.connect(str(probe_path))
        return None
    except BaseException as exc:
        if _is_permission_error(exc):
            return "sandbox_ipc_restricted"
        return None
    finally:
        for sock_obj in (cli, srv):
            if sock_obj is not None:
                try:
                    sock_obj.close()
                except Exception:
                    pass
        if probe_path is not None and probe_path.exists() and not probe_path.is_symlink():
            try:
                probe_path.unlink()
            except OSError:
                pass


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _read_meta(state_dir: Path) -> Optional[Dict[str, Any]]:
    meta_path = _meta_file_path(state_dir)
    if not meta_path.exists() or meta_path.is_symlink():
        return None
    try:
        data = json.loads(meta_path.read_text("utf-8"))
        if not isinstance(data, dict):
            return None
        if int(data.get("v", 0)) != PROTOCOL_VERSION:
            return None
        return data
    except Exception:
        return None


def _atomic_write_meta(state_dir: Path, payload: Dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(state_dir, 0o700)
    except OSError:
        pass
    meta_path = _meta_file_path(state_dir)
    if meta_path.exists() and meta_path.is_symlink():
        raise OSError("refusing symlink daemon meta file")
    tmp = state_dir / f".atlas-daemon.meta.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, meta_path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _cleanup_stale_files(state_dir: Path, sock_path: Optional[Path] = None) -> None:
    meta_path = _meta_file_path(state_dir)
    if meta_path.exists() and not meta_path.is_symlink():
        try:
            meta_path.unlink()
        except OSError:
            pass
    if sock_path is not None and sock_path.exists() and not sock_path.is_symlink():
        try:
            sock_path.unlink()
        except OSError:
            pass


def _verify_peer_uid(conn: socket.socket) -> bool:
    if not hasattr(os, "getuid"):
        return True
    my_uid = os.getuid()
    if sys.platform == "darwin":
        try:
            # macOS SOL_LOCAL=0, LOCAL_PEERCRED=0x001 -> struct xucred { u_int cr_version; uid_t cr_uid; ... }
            raw = conn.getsockopt(0, 0x001, 76)
            _ver, peer_uid = struct.unpack_from("II", raw, 0)
            return int(peer_uid) == my_uid
        except OSError:
            return False
    if sys.platform.startswith("linux"):
        try:
            so_peercred = getattr(socket, "SO_PEERCRED", 17)
            raw = conn.getsockopt(socket.SOL_SOCKET, so_peercred, struct.calcsize("3i"))
            _pid, peer_uid, _gid = struct.unpack("3i", raw)
            return int(peer_uid) == my_uid
        except OSError:
            return False
    return True


def _send_frame(sock: socket.socket, obj: Dict[str, Any]) -> None:
    raw = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > MAX_FRAME_BYTES:
        raise ValueError("frame exceeds MAX_FRAME_BYTES")
    sock.sendall(raw)


def _recv_frame(sock: socket.socket) -> Optional[Dict[str, Any]]:
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_FRAME_BYTES:
            raise ValueError("incoming frame exceeds MAX_FRAME_BYTES")
        if b"\n" in chunk:
            break
    if not chunks:
        return None
    line = b"".join(chunks).split(b"\n", 1)[0]
    if not line:
        return None
    data = json.loads(line.decode("utf-8"))
    return data if isinstance(data, dict) else None


class _DaemonPostSendError(OSError):
    """Raised when an RPC failure occurs AFTER the request frame was transmitted to the daemon."""


def _rpc_call(
    sock_path: Path,
    payload: Dict[str, Any],
    *,
    connect_timeout: float = 0.5,
    read_timeout: float = 120.0,
) -> Dict[str, Any]:
    if not hasattr(socket, "AF_UNIX"):
        raise OSError("AF_UNIX unsupported")
    if sock_path.is_symlink():
        raise OSError("refusing symlink socket")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sent = False
    try:
        sock.settimeout(connect_timeout)
        sock.connect(str(sock_path))
        sock.settimeout(read_timeout)
        _send_frame(sock, payload)
        sent = True
        resp = _recv_frame(sock)
        if resp is None:
            raise _DaemonPostSendError("empty daemon response")
        return resp
    except _DaemonPostSendError:
        raise
    except Exception as exc:
        if sent:
            raise _DaemonPostSendError(str(exc)) from exc
        raise
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _collect_safe_env() -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k in _SAFE_FORWARD_ENV_KEYS:
        val = os.environ.get(k)
        out[k] = val if (val is not None and val != "") else ""
    return out


def _is_read_only_request(action: str, mode: str, raw_argv: List[str], stdin_text: Optional[str]) -> bool:
    act = str(action or "").strip().lower()
    md = str(mode or "").strip().lower()
    if "--dry_run" in raw_argv or "--dry-run" in raw_argv:
        for i, tok in enumerate(raw_argv):
            if tok in ("--dry_run", "--dry-run") and i + 1 < len(raw_argv) and str(raw_argv[i + 1]).strip().lower() in ("1", "true", "yes"):
                return True
    if not act and stdin_text:
        try:
            parsed = json.loads(stdin_text)
            if isinstance(parsed, dict):
                act = str(parsed.get("action") or "").strip().lower()
                md = str(parsed.get("mode") or "").strip().lower()
                if parsed.get("dry_run") is True:
                    return True
                if act == "batch" and isinstance(parsed.get("steps"), list):
                    steps = parsed["steps"]
                    return bool(steps) and all(
                        isinstance(s, dict) and str(s.get("action") or "").strip().lower() in _READ_ONLY_DAEMON_ACTIONS
                        for s in steps
                    )
        except Exception:
            pass
    if act == "guard":
        return md in ("", "delivery", "health", "schema", "validate", "links", "doc-status", "change-list", "change-get")
    if act == "usecase":
        return md in ("", "list", "get", "match")
    return act in _READ_ONLY_DAEMON_ACTIONS


def try_forward_to_daemon(
    raw_argv: List[str],
    *,
    stdin_loader: Callable[[], Optional[str]],
) -> Optional[Tuple[int, str]]:
    """Ultra-light fail-open client used at the very top of atlas_cli.py.

    Returns `(exit_code, stdout_line)` when handled by a live, authenticated hot daemon,
    or `None` to transparently fall back to local execution.
    """
    if sys.platform == "win32" or not hasattr(socket, "AF_UNIX"):
        return None
    if os.environ.get("GEWU_ATLAS_NO_DAEMON", "").strip().lower() in ("1", "true", "yes"):
        return None
    for i, tok in enumerate(raw_argv):
        if tok in ("--template", "--describe", "-h", "--help"):
            return None
        if tok in ("--no_daemon", "--no-daemon"):
            if i + 1 >= len(raw_argv) or str(raw_argv[i + 1]).strip().lower() not in ("0", "false", "no"):
                return None
        if tok.startswith(("--no_daemon=", "--no-daemon=")):
            val = tok.split("=", 1)[1].strip().lower()
            if val not in ("0", "false", "no"):
                return None

    argv_action, argv_mode = _extract_argv_action_mode(raw_argv)
    if _should_bypass_action(argv_action, argv_mode):
        return None

    state_dir = _runtime_state_dir()
    meta = _read_meta(state_dir)
    if not meta:
        return None
    sock_raw = str(meta.get("sock_path") or "").strip()
    token = str(meta.get("token") or "").strip()
    pid = int(meta.get("pid") or 0)
    if not sock_raw or not token or pid <= 0:
        return None
    sock_path = Path(sock_raw)
    if not sock_path.exists() or sock_path.is_symlink():
        if not _is_pid_alive(pid):
            _cleanup_stale_files(state_dir, sock_path)
        return None

    # Auto-retire stale daemon if source files changed on disk
    cur_fp = _compute_code_fingerprint()
    if str(meta.get("code_fingerprint") or "") != cur_fp:
        try:
            _rpc_call(sock_path, {"v": PROTOCOL_VERSION, "token": token, "op": "shutdown"}, connect_timeout=0.3, read_timeout=2.0)
        except Exception:
            if not _is_pid_alive(pid):
                _cleanup_stale_files(state_dir, sock_path)
        return None

    try:
        stdin_text = stdin_loader()
        if stdin_text and not argv_action:
            try:
                parsed_in = json.loads(stdin_text)
                if isinstance(parsed_in, dict) and _should_bypass_action(
                    str(parsed_in.get("action") or ""),
                    str(parsed_in.get("mode") or ""),
                ):
                    return None
            except Exception:
                pass
        resp = _rpc_call(
            sock_path,
            {
                "v": PROTOCOL_VERSION,
                "token": token,
                "op": "exec",
                "argv": list(raw_argv),
                "stdin": stdin_text,
                "session_env": _collect_safe_env(),
            },
            connect_timeout=0.5,
            read_timeout=120.0,
        )
        if resp.get("ok") is True and isinstance(resp.get("stdout"), str) and isinstance(resp.get("exit_code"), int):
            return int(resp["exit_code"]), str(resp["stdout"])
        if not _is_read_only_request(argv_action, argv_mode, raw_argv, stdin_text):
            err_code = str(resp.get("error") or "E_DAEMON_EXEC_FAILED")
            err_payload = {
                "ok": False,
                "code": "E_DAEMON_RPC_UNCERTAIN",
                "isError": True,
                "structuredContent": {
                    "capsule": {
                        "kind": "GUARD",
                        "mode": "HALTED",
                        "terminal": True,
                        "state": f"热内核守护进程已接收写请求但返回异常（{err_code}），为防重复执行已熔断本地重放",
                        "STATE": f"热内核守护进程已接收写请求但返回异常（{err_code}），为防重复执行已熔断本地重放",
                        "ORBIT": ["先用只读命令（note-list / change-list / daemon status）核对落盘状态后再决定是否重试"],
                        "ANCHOR": "GUARD: HALTED | 写请求状态未决，禁止自动本地重放",
                    },
                    "data": {"daemon_error": err_code, "replay_blocked": True},
                },
                "data": {"daemon_error": err_code, "replay_blocked": True},
            }
            return 1, json.dumps(err_payload, ensure_ascii=False)
        return None
    except _DaemonPostSendError as exc:
        if not _is_pid_alive(pid):
            _cleanup_stale_files(state_dir, sock_path)
        if not _is_read_only_request(argv_action, argv_mode, raw_argv, locals().get("stdin_text")):
            err_payload = {
                "ok": False,
                "code": "E_DAEMON_RPC_UNCERTAIN",
                "isError": True,
                "structuredContent": {
                    "capsule": {
                        "kind": "GUARD",
                        "mode": "HALTED",
                        "terminal": True,
                        "state": f"热内核守护进程在接收写请求后响应超时或中断（{exc}），为防写操作重复执行已阻断本地自动回退",
                        "STATE": f"热内核守护进程在接收写请求后响应超时或中断（{exc}），为防写操作重复执行已阻断本地自动回退",
                        "ORBIT": ["请先执行 note-list 或 change-list 核对前序写操作是否已落盘"],
                        "ANCHOR": "GUARD: HALTED | 写操作防重放保护生效",
                    },
                    "data": {"daemon_error": str(exc), "replay_blocked": True},
                },
                "data": {"daemon_error": str(exc), "replay_blocked": True},
            }
            return 1, json.dumps(err_payload, ensure_ascii=False)
        return None
    except Exception:
        if not _is_pid_alive(pid):
            _cleanup_stale_files(state_dir, sock_path)
        return None


def _serve_daemon(idle_timeout: int, preload_vaults: List[str]) -> int:
    if sys.platform == "win32" or not hasattr(socket, "AF_UNIX"):
        return 2
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))

    from facade.atlas_hot_kernel import AtlasHotKernel

    state_dir = _runtime_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(state_dir, 0o700)
    except OSError:
        pass

    sock_path = _compute_socket_path(state_dir)
    if sock_path.exists():
        if sock_path.is_symlink():
            return 2
        try:
            sock_path.unlink()
        except OSError:
            return 2

    token = secrets.token_hex(32)
    code_fp = _compute_code_fingerprint()
    kernel = AtlasHotKernel()
    stop_flag = False

    def _handle_sig(_signum: int, _frame: Any) -> None:
        nonlocal stop_flag
        stop_flag = True

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:
            srv.bind(str(sock_path))
        except OSError as exc:
            if _is_permission_error(exc):
                return _SANDBOX_IPC_EXIT_CODE
            raise
        os.chmod(sock_path, 0o600)
        srv.listen(32)
        srv.settimeout(1.0)

        meta = {
            "v": PROTOCOL_VERSION,
            "pid": os.getpid(),
            "sock_path": str(sock_path),
            "token": token,
            "code_fingerprint": code_fp,
            "started_at": time.time(),
            "idle_timeout": int(idle_timeout),
        }
        _atomic_write_meta(state_dir, meta)

        for vref in preload_vaults:
            try:
                kernel.preload_vault(vref)
            except Exception:
                pass

        last_activity = time.monotonic()
        while not stop_flag:
            if idle_timeout > 0 and (time.monotonic() - last_activity) >= float(idle_timeout):
                break
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            with conn:
                conn.settimeout(30.0)
                if not _verify_peer_uid(conn):
                    try:
                        _send_frame(conn, {"ok": False, "error": "E_UNAUTHORIZED_UID"})
                    except Exception:
                        pass
                    continue
                try:
                    req = _recv_frame(conn)
                except Exception:
                    continue
                if not req or int(req.get("v", 0)) != PROTOCOL_VERSION:
                    try:
                        _send_frame(conn, {"ok": False, "error": "E_BAD_PROTOCOL"})
                    except Exception:
                        pass
                    continue
                req_tok = str(req.get("token") or "")
                if not hmac.compare_digest(req_tok, token):
                    try:
                        _send_frame(conn, {"ok": False, "error": "E_UNAUTHORIZED_TOKEN"})
                    except Exception:
                        pass
                    continue

                op = str(req.get("op") or "").strip().lower()
                if op == "ping":
                    _send_frame(conn, {"ok": True, "pid": os.getpid(), "v": PROTOCOL_VERSION})
                    continue
                if op == "status":
                    st = kernel.stats(idle_timeout=float(idle_timeout))
                    st["pid"] = os.getpid()
                    st["protocol_version"] = PROTOCOL_VERSION
                    st["code_fingerprint"] = code_fp
                    _send_frame(conn, {"ok": True, "stats": st})
                    continue
                if op == "shutdown":
                    st = kernel.stats(idle_timeout=float(idle_timeout))
                    _send_frame(conn, {"ok": True, "status": "stopping", "stats": st})
                    stop_flag = True
                    continue
                if op == "preload":
                    last_activity = time.monotonic()
                    vref = str(req.get("vault_ref") or "").strip()
                    s_env = req.get("session_env") if isinstance(req.get("session_env"), dict) else None
                    try:
                        res = kernel.preload_vault(vref, session_env=s_env)
                        _send_frame(conn, {"ok": True, "result": res})
                    except Exception as exc:
                        _send_frame(conn, {"ok": False, "error": str(exc)})
                    continue
                if op == "exec":
                    last_activity = time.monotonic()
                    argv = req.get("argv")
                    stdin_val = req.get("stdin")
                    s_env = req.get("session_env") if isinstance(req.get("session_env"), dict) else None
                    if not isinstance(argv, list):
                        _send_frame(conn, {"ok": False, "error": "E_BAD_ARGV"})
                        continue
                    try:
                        ec, out_line = kernel.execute_cli(
                            [str(a) for a in argv],
                            stdin_text=str(stdin_val) if stdin_val is not None else None,
                            session_env=s_env,
                        )
                        _send_frame(conn, {"ok": True, "exit_code": ec, "stdout": out_line})
                    except Exception:
                        _send_frame(conn, {"ok": False, "error": "E_DAEMON_EXEC_FAILED"})
                    continue
                _send_frame(conn, {"ok": False, "error": f"E_UNKNOWN_OP:{op}"})
        return 0
    finally:
        try:
            srv.close()
        except OSError:
            pass
        kernel.close()
        _cleanup_stale_files(state_dir, sock_path)


def _capsule_out(ok: bool, code: str, state: str, orbit: List[str], anchor: str, data: Dict[str, Any], exit_code: int = 0) -> int:
    payload = {
        "ok": ok,
        "code": code,
        "isError": not ok,
        "structuredContent": {
            "capsule": {
                "kind": "TRIAD" if ok else "GUARD",
                "mode": "NORMAL" if ok else "PARAM_FIX",
                "terminal": True,
                "state": state,
                "STATE": state,
                "ORBIT": orbit,
                "ANCHOR": anchor,
            },
            "data": data,
        },
        "data": data,
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return exit_code


def _daemon_status() -> Tuple[bool, Optional[Dict[str, Any]]]:
    state_dir = _runtime_state_dir()
    meta = _read_meta(state_dir)
    if not meta:
        return False, None
    pid = int(meta.get("pid") or 0)
    sock_path = Path(str(meta.get("sock_path") or ""))
    token = str(meta.get("token") or "")
    if not pid or not token or not sock_path.exists():
        if pid and not _is_pid_alive(pid):
            _cleanup_stale_files(state_dir, sock_path)
        return False, None
    try:
        resp = _rpc_call(sock_path, {"v": PROTOCOL_VERSION, "token": token, "op": "status"}, connect_timeout=0.5, read_timeout=3.0)
        if resp.get("ok") and isinstance(resp.get("stats"), dict):
            return True, resp["stats"]
    except (socket.timeout, TimeoutError, _DaemonPostSendError):
        if _is_pid_alive(pid) and sock_path.exists():
            return True, {
                "pid": pid,
                "status": "busy",
                "protocol_version": PROTOCOL_VERSION,
                "code_fingerprint": meta.get("code_fingerprint"),
                "idle_timeout_seconds": meta.get("idle_timeout"),
            }
        if not _is_pid_alive(pid):
            _cleanup_stale_files(state_dir, sock_path)
    except Exception:
        if not _is_pid_alive(pid):
            _cleanup_stale_files(state_dir, sock_path)
    return False, None


def _daemon_stop() -> Tuple[bool, Dict[str, Any]]:
    state_dir = _runtime_state_dir()
    meta = _read_meta(state_dir)
    if not meta:
        return False, {"status": "not_running"}
    pid = int(meta.get("pid") or 0)
    sock_path = Path(str(meta.get("sock_path") or ""))
    token = str(meta.get("token") or "")
    stats: Dict[str, Any] = {}
    if sock_path.exists() and token:
        try:
            resp = _rpc_call(sock_path, {"v": PROTOCOL_VERSION, "token": token, "op": "shutdown"}, connect_timeout=0.5, read_timeout=3.0)
            if isinstance(resp.get("stats"), dict):
                stats = resp["stats"]
        except Exception:
            pass
    deadline = time.monotonic() + 3.0
    while pid > 0 and _is_pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if pid > 0 and _is_pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        deadline2 = time.monotonic() + 1.5
        while _is_pid_alive(pid) and time.monotonic() < deadline2:
            time.sleep(0.05)
    if pid > 0 and _is_pid_alive(pid):
        sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
        try:
            os.kill(pid, sigkill)
        except OSError:
            pass
        deadline3 = time.monotonic() + 0.5
        while _is_pid_alive(pid) and time.monotonic() < deadline3:
            time.sleep(0.02)
    _cleanup_stale_files(state_dir, sock_path)
    return True, {
        "status": "stopped",
        "pid": pid,
        "requests_served": int(stats.get("requests_served", 0)),
        "uptime_seconds": stats.get("uptime_seconds"),
    }


def _daemon_start(idle_timeout: int, preload_vaults: List[str]) -> Tuple[int, Dict[str, Any]]:
    if sys.platform == "win32" or not hasattr(socket, "AF_UNIX"):
        return 1, {"status": "unsupported", "reason": "当前平台不支持 AF_UNIX 常驻热内核，日常命令将自动直跑"}

    state_dir = _runtime_state_dir()
    if _probe_af_unix_ipc(state_dir) == "sandbox_ipc_restricted":
        return 1, {
            "status": "sandbox_ipc_restricted",
            "reason": "当前受限沙箱禁止本地 AF_UNIX 守护进程（已快速降级直跑，无孤儿进程；多步写请优先用 batch 合批提速）",
        }

    running, st = _daemon_status()
    if running and st is not None:
        if str(st.get("code_fingerprint") or "") == _compute_code_fingerprint():
            preloaded: List[Dict[str, Any]] = []
            if preload_vaults:
                meta = _read_meta(_runtime_state_dir())
                if meta:
                    for vref in preload_vaults:
                        try:
                            r = _rpc_call(
                                Path(str(meta["sock_path"])),
                                {"v": PROTOCOL_VERSION, "token": str(meta["token"]), "op": "preload", "vault_ref": vref, "session_env": _collect_safe_env()},
                                connect_timeout=0.5,
                                read_timeout=15.0,
                            )
                            if r.get("ok") and isinstance(r.get("result"), dict):
                                preloaded.append(r["result"])
                        except Exception:
                            pass
            return 0, {
                "status": "already_running",
                "pid": st.get("pid"),
                "idle_timeout_seconds": st.get("idle_timeout_seconds"),
                "idle_remaining_seconds": st.get("idle_remaining_seconds"),
                "hot_vaults": st.get("hot_vaults", []),
                "requests_served": st.get("requests_served", 0),
                "preloaded": preloaded,
                "protocol_version": PROTOCOL_VERSION,
            }
        # Code changed since daemon started -> cleanly restart
        _daemon_stop()

    cmd = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "_serve", "--idle-timeout", str(idle_timeout)]

    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "LD_PRELOAD", "LD_LIBRARY_PATH") and not k.startswith("DYLD_")}
    proc = subprocess.Popen(
        cmd,
        cwd=str(APP_DIR.parent),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        polled = proc.poll()
        if polled is not None:
            _cleanup_stale_files(state_dir)
            if polled == _SANDBOX_IPC_EXIT_CODE:
                return 1, {
                    "status": "sandbox_ipc_restricted",
                    "reason": "当前受限沙箱禁止本地 AF_UNIX 守护进程（已快速降级直跑，无孤儿进程；多步写请优先用 batch 合批提速）",
                }
            return 1, {"status": "failed_to_start", "reason": "守护进程启动失败或被当前沙箱限制（日常 ./gewu atlas 命令将自动无感降级直跑）"}
        meta_now = _read_meta(state_dir)
        if meta_now and meta_now.get("sock_path") and Path(str(meta_now["sock_path"])).exists():
            try:
                _rpc_call(
                    Path(str(meta_now["sock_path"])),
                    {"v": PROTOCOL_VERSION, "token": str(meta_now.get("token") or ""), "op": "ping"},
                    connect_timeout=0.3,
                    read_timeout=1.0,
                )
            except BaseException as exc:
                if _is_permission_error(exc):
                    try:
                        proc.terminate()
                        proc.wait(timeout=1.0)
                    except Exception:
                        try:
                            proc.kill()
                            proc.wait(timeout=1.0)
                        except Exception:
                            pass
                    _cleanup_stale_files(state_dir, Path(str(meta_now["sock_path"])))
                    return 1, {
                        "status": "sandbox_ipc_restricted",
                        "reason": "当前受限沙箱禁止本地 AF_UNIX 连接（已快速终止并降级直跑，无孤儿进程；多步写请优先用 batch 合批提速）",
                    }
        ok, st2 = _daemon_status()
        if ok and st2 is not None:
            preloaded: List[Dict[str, Any]] = []
            if preload_vaults:
                meta = _read_meta(_runtime_state_dir())
                if meta:
                    for vref in preload_vaults:
                        try:
                            r = _rpc_call(
                                Path(str(meta["sock_path"])),
                                {"v": PROTOCOL_VERSION, "token": str(meta["token"]), "op": "preload", "vault_ref": vref, "session_env": _collect_safe_env()},
                                connect_timeout=0.5,
                                read_timeout=15.0,
                            )
                            if r.get("ok") and isinstance(r.get("result"), dict):
                                preloaded.append(r["result"])
                        except Exception:
                            pass
                    ok_after, st_after = _daemon_status()
                    if ok_after and st_after is not None:
                        st2 = st_after
            return 0, {
                "status": "running",
                "pid": st2.get("pid"),
                "idle_timeout_seconds": st2.get("idle_timeout_seconds"),
                "hot_vaults": st2.get("hot_vaults", []),
                "preloaded": preloaded,
                "protocol_version": PROTOCOL_VERSION,
            }
        time.sleep(0.05)

    # Timeout / unreachable (e.g., sandbox blocked AF_UNIX): MUST kill spawned process to prevent orphan daemons!
    try:
        proc.terminate()
        proc.wait(timeout=1.5)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=1.0)
        except Exception:
            pass
    state_dir = _runtime_state_dir()
    meta_stale = _read_meta(state_dir)
    stale_sock = Path(str(meta_stale["sock_path"])) if (meta_stale and meta_stale.get("sock_path")) else None
    _cleanup_stale_files(state_dir, stale_sock)
    return 1, {"status": "start_timeout", "reason": "守护进程未在 5 秒内就绪并已自动终止清理，无残留孤儿进程（日常 ./gewu atlas 命令将自动无感降级直跑）"}


def main(argv: Optional[List[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    env_idle = os.environ.get("GEWU_ATLAS_DAEMON_IDLE_SECONDS", "").strip()
    idle_timeout = int(env_idle) if env_idle.isdigit() else DEFAULT_IDLE_TIMEOUT
    preload_vaults: List[str] = []
    subcmd = ""

    i = 0
    while i < len(raw):
        tok = raw[i]
        if tok in ("start", "status", "stop", "restart", "_serve") and not subcmd:
            subcmd = tok
            i += 1
            continue
        if tok in ("--action", "--mode") and i + 1 < len(raw) and not subcmd:
            subcmd = raw[i + 1].strip().lower()
            i += 2
            continue
        if tok in ("--idle-timeout", "--idle_timeout") and i + 1 < len(raw):
            try:
                idle_timeout = max(0, int(raw[i + 1]))
            except ValueError:
                return _capsule_out(False, "E_PARAM_INVALID", "--idle-timeout 必须为非负整数秒数", ["重试: ./gewu atlas daemon start --idle-timeout 600"], "0 表示关闭空闲自毁。", {}, exit_code=1)
            i += 2
            continue
        if tok in ("--preload-vault", "--preload_vault", "--ref") and i + 1 < len(raw):
            preload_vaults.extend([x.strip() for x in raw[i + 1].split(",") if x.strip()])
            i += 2
            continue
        if tok in ("-h", "--help", "--template"):
            return _capsule_out(
                True,
                "OK_DAEMON_HELP",
                "格物志自托管热内核守护进程控制面（零宿主 MCP 依赖）",
                [
                    "开工拉起: ./gewu atlas daemon start [--idle-timeout 600] [--preload-vault vlt_01]",
                    "查看状态: ./gewu atlas daemon status",
                    "收工停机: ./gewu atlas daemon stop",
                ],
                "运行期间所有 ./gewu atlas --action ... 自动走 0600 本地 Socket 复用内存热索引；未启动或沙箱受限时自动无感降级直跑。",
                {"commands": ["start", "status", "stop", "restart"], "default_idle_timeout": DEFAULT_IDLE_TIMEOUT},
            )
        i += 1

    if subcmd == "_serve":
        return _serve_daemon(idle_timeout, preload_vaults)

    if not subcmd or subcmd == "status":
        running, st = _daemon_status()
        if running and st is not None:
            return _capsule_out(
                True,
                "OK_DAEMON_STATUS",
                f"热内核守护进程运行中（PID {st.get('pid')}，已服务 {st.get('requests_served', 0)} 次请求）",
                ["收工停机: ./gewu atlas daemon stop", "直接向人类汇报现状"],
                "所有常规 ./gewu atlas --action ... 自动复用热内存索引；空闲超时自动退出清空内存。",
                {"running": True, **st},
            )
        return _capsule_out(
            True,
            "OK_DAEMON_STATUS",
            "热内核守护进程当前未运行（日常命令直跑模式）",
            ["按需启动热内核: ./gewu atlas daemon start", "直接向人类汇报现状"],
            "L2/L3 多步任务前执行 daemon start 可显著降低冷启动与重复解密开销。",
            {"running": False, "protocol_version": PROTOCOL_VERSION},
        )

    if subcmd == "start":
        rc, info = _daemon_start(idle_timeout, preload_vaults)
        if rc == 0:
            return _capsule_out(
                True,
                "OK_DAEMON_STARTED",
                f"热内核守护进程已就绪（{info.get('status')}，PID {info.get('pid')}，空闲自毁 {info.get('idle_timeout_seconds')}s）",
                ["正常执行业务命令: ./gewu atlas --action vault", "任务结束停机: ./gewu atlas daemon stop"],
                "零宿主 MCP 依赖；内存热索引随 stop 或空闲超时自动销毁，零明文落盘。",
                info,
            )
        if info.get("status") == "sandbox_ipc_restricted":
            return _capsule_out(
                False,
                "E_DAEMON_SANDBOX_RESTRICTED",
                str(info.get("reason") or "当前受限沙箱禁止本地 AF_UNIX 守护进程"),
                [
                    "直接执行常规 ./gewu atlas --action ...（自动直跑，多步写优先用 batch 合批提速）",
                    "如需热内核加速，请在宿主终端或允许本地 IPC 的模式下运行 daemon start",
                ],
                "守护进程为纯加速层，受限沙箱已快速降级且零孤儿进程，不影响业务正确性与安全边界。",
                info,
                exit_code=1,
            )
        return _capsule_out(
            False,
            "E_DAEMON_START_FAILED",
            str(info.get("reason") or "热内核守护进程未能启动"),
            ["继续直接使用常规 ./gewu atlas --action ...（将自动无感降级直跑）"],
            "守护进程为纯加速层，启动受限时不影响任何业务正确性与安全边界。",
            info,
            exit_code=1,
        )

    if subcmd == "stop":
        stopped, info = _daemon_stop()
        msg = f"热内核守护进程已停止并清空内存（共服务 {info.get('requests_served', 0)} 次请求）" if stopped else "热内核守护进程未在运行，无需停止"
        return _capsule_out(
            True,
            "OK_DAEMON_STOPPED",
            msg,
            ["直接向人类汇报现状"],
            "所有内存热态与受保护 Socket 文件已清理完毕。",
            info,
        )

    if subcmd == "restart":
        _daemon_stop()
        rc, info = _daemon_start(idle_timeout, preload_vaults)
        if rc == 0:
            return _capsule_out(
                True,
                "OK_DAEMON_RESTARTED",
                f"热内核守护进程已重启（PID {info.get('pid')}）",
                ["查看状态: ./gewu atlas daemon status", "任务结束停机: ./gewu atlas daemon stop"],
                "已载入最新源码与内存热内核。",
                info,
            )
        if info.get("status") == "sandbox_ipc_restricted":
            return _capsule_out(False, "E_DAEMON_SANDBOX_RESTRICTED", str(info.get("reason") or "当前受限沙箱禁止本地 AF_UNIX 守护进程"), ["回退常规 ./gewu atlas 命令直跑（多步写优先用 batch）"], "不影响常规业务。", info, exit_code=1)
        return _capsule_out(False, "E_DAEMON_START_FAILED", str(info.get("reason") or "重启失败"), ["回退常规 ./gewu atlas 命令直跑"], "不影响常规业务。", info, exit_code=1)

    return _capsule_out(
        False,
        "E_PARAM_INVALID",
        f"不支持的 daemon 子命令: {subcmd}",
        ["支持子命令: ./gewu atlas daemon start|status|stop|restart"],
        "请从声明子命令中选择。",
        {"allowed": ["start", "status", "stop", "restart"]},
        exit_code=1,
    )


if __name__ == "__main__":
    sys.exit(main())
