#!/usr/bin/env python3
"""Gateway bridge delegating GewuAtlas ticket actions to GewuTicket and recording Vault DOC tickets."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from notes.atlas_yamlmini import fm_scalar
from storage.vault_kek_store import VaultStoreError

TICKET_VOLUME_REL = "./Volumes/GewuTicket-data"
_INPROC_LOCK = threading.RLock()
_INPROC_TICKET_CLI_CACHE: Dict[str, Any] = {}


def _load_inproc_ticket_cli(ws_root: Path) -> Optional[Any]:
    """Load GewuTicket's ticket_cli module once in isolation without colliding with GewuAtlas's `guard`/`app_core` packages."""
    if os.environ.get("GEWU_TICKET_FORCE_SUBPROCESS", "").strip() == "1":
        return None
    cli_path = (ws_root / "GewuTicket" / "scripts" / "ticket" / "ticket_cli.py").resolve()
    if not cli_path.is_file():
        return None
    cache_key = str(cli_path)
    if cache_key in _INPROC_TICKET_CLI_CACHE:
        return _INPROC_TICKET_CLI_CACHE[cache_key]

    tkt_scripts = str((ws_root / "GewuTicket" / "scripts").resolve())
    saved_sys_path = list(sys.path)
    saved_conflicts: Dict[str, Any] = {}
    conflict_prefixes = ("guard", "app_core")
    for mod_name in list(sys.modules.keys()):
        if any(mod_name == p or mod_name.startswith(p + ".") for p in conflict_prefixes):
            saved_conflicts[mod_name] = sys.modules.pop(mod_name)
    try:
        sys.path.insert(0, tkt_scripts)
        spec = importlib.util.spec_from_file_location("_gewu_inproc_ticket_cli", str(cli_path))
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not callable(getattr(mod, "main", None)):
            return None
        _INPROC_TICKET_CLI_CACHE[cache_key] = mod
        return mod
    except Exception:
        return None
    finally:
        # Remove GewuTicket's guard/app_core entries from sys.modules and restore GewuAtlas's own modules.
        for mod_name in list(sys.modules.keys()):
            if any(mod_name == p or mod_name.startswith(p + ".") for p in conflict_prefixes):
                sys.modules.pop(mod_name, None)
        sys.modules.update(saved_conflicts)
        sys.path[:] = saved_sys_path


def _run_gewu_ticket_inproc(ws_root: Path, argv: list[str]) -> Optional[tuple[int, Dict[str, Any], str]]:
    with _INPROC_LOCK:
        mod = _load_inproc_ticket_cli(ws_root)
        if mod is None:
            return None
        prev_cwd = os.getcwd()
        prev_rt = os.environ.get("GEWU_APP_RUNTIME_DIR")
        buf = io.StringIO()
        try:
            # Never let GewuTicket write its breaker/logs into GewuAtlas's runtime dir.
            if prev_rt and "GewuAtlas" in prev_rt:
                os.environ.pop("GEWU_APP_RUNTIME_DIR", None)
            os.chdir(str(ws_root))
            with contextlib.redirect_stdout(buf):
                try:
                    rc = int(mod.main(list(argv)))
                except SystemExit as se:
                    rc = int(se.code) if isinstance(se.code, int) else 1
        except Exception:
            return None
        finally:
            try:
                os.chdir(prev_cwd)
            except OSError:
                pass
            if prev_rt is None:
                os.environ.pop("GEWU_APP_RUNTIME_DIR", None)
            else:
                os.environ["GEWU_APP_RUNTIME_DIR"] = prev_rt
        out = buf.getvalue().strip()
        try:
            parsed = json.loads(out) if out else {}
        except Exception:
            parsed = {"raw": out}
        msg = str(parsed.get("code") or parsed.get("error_code") or f"exit={rc}")
        return rc, parsed, msg


def _run_gewu_ticket_once(ws_root: Path, argv: list[str]) -> tuple[int, Dict[str, Any], str]:
    gateway = ws_root / "gewu"
    if not gateway.is_file():
        raise VaultStoreError("E_NOT_FOUND", "工作区根缺少 ./gewu 网关脚本", exit_code=2)
    inproc = _run_gewu_ticket_inproc(ws_root, argv)
    if inproc is not None:
        return inproc
    sub_env = dict(os.environ)
    if "GewuAtlas" in str(sub_env.get("GEWU_APP_RUNTIME_DIR", "")):
        sub_env.pop("GEWU_APP_RUNTIME_DIR", None)
    proc = subprocess.run(
        [sys.executable, "-I", "-B", str(gateway), "ticket", *argv],
        cwd=str(ws_root),
        env=sub_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )
    out = proc.stdout.strip()
    try:
        parsed = json.loads(out) if out else {}
    except Exception:
        parsed = {"raw": out}
    msg = str(parsed.get("code") or parsed.get("error_code") or f"exit={proc.returncode}")
    return proc.returncode, parsed, msg


def _run_gewu_ticket(ws_root: Path, argv: list[str]) -> Dict[str, Any]:
    rc, parsed, msg = _run_gewu_ticket_once(ws_root, argv)
    if rc != 0 and msg == "E_VOLUME_NOT_INITIALIZED" and "init-volume" not in argv:
        init_rc, _, init_msg = _run_gewu_ticket_once(
            ws_root,
            ["--action", "init-volume", "--volume", TICKET_VOLUME_REL, "--mount_mode", "rw"],
        )
        if init_rc != 0:
            raise VaultStoreError("E_BRIDGE_TICKET", f"GewuTicket 网关初始化失败: {init_msg}", exit_code=1)
        rc, parsed, msg = _run_gewu_ticket_once(ws_root, argv)
    if rc != 0:
        raise VaultStoreError("E_BRIDGE_TICKET", f"GewuTicket 网关调用失败: {msg}", exit_code=1)
    return parsed


def _ensure_ticket_volume(ws_root: Path) -> bool:
    """Return True when the workspace exposes both ./gewu and GewuTicket (volume auto-inits lazily)."""
    return (ws_root / "gewu").is_file() and (ws_root / "GewuTicket").is_dir()


def create_atlas_ticket(ws_root: Path, *, category: str, title: str, actor: str = "GewuAtlas", key: str = "") -> Dict[str, Any]:
    cat = (category or "DOC").strip().upper()
    if cat not in ("SYS", "DOC", "MSG", "TASK", "SLIP"):
        raise VaultStoreError("E_PARAM_INVALID", "--category 仅支持 SYS / DOC / MSG / TASK / SLIP", exit_code=1)
    _ensure_ticket_volume(ws_root)
    argv = [
        "--action",
        "out",
        "--volume",
        TICKET_VOLUME_REL,
        "--category",
        cat,
        "--from",
        actor,
        "--title",
        title.strip(),
    ]
    if key.strip():
        argv.extend(["--key", key.strip()])
    res = _run_gewu_ticket(ws_root, argv)
    data = res.get("data") if isinstance(res, dict) else {}
    return {
        "ticket_id": (data or {}).get("ticket_id") or (data or {}).get("ref") or "",
        "category": cat,
        "title": title.strip(),
        "bridge": "GewuTicket",
    }


def list_atlas_tickets(ws_root: Path) -> Dict[str, Any]:
    _ensure_ticket_volume(ws_root)
    res = _run_gewu_ticket(
        ws_root,
        [
            "--action",
            "list",
            "--volume",
            TICKET_VOLUME_REL,
        ],
    )
    data = res.get("data") if isinstance(res, dict) else {}
    return {
        "bridge": "GewuTicket",
        "tickets": (data or {}).get("tickets") or (data or {}).get("items") or [],
        "count": (data or {}).get("count", 0),
    }


def _atomic_write_noexec(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tkt-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def record_project_operation_ticket(
    *,
    ws_root: Path,
    vault_dir: Path,
    op_id: str,
    op_kind: str,
    title: str,
    summary: str,
    details_lines: List[str],
    undo_hint: str,
    project_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Record a project merge/split/undo audit log via GewuTicket + Vault DOC Ticket note."""
    ext_ticket_id = ""
    bridge_status = "vault_only"
    if _ensure_ticket_volume(ws_root):
        try:
            created = create_atlas_ticket(ws_root, category="DOC", title=title, actor="GewuAtlas", key=op_id)
            ext_ticket_id = str(created.get("ticket_id") or "")
            bridge_status = "bridged"
            if ext_ticket_id:
                try:
                    _run_gewu_ticket(
                        ws_root,
                        ["--action", "pickup", "--volume", TICKET_VOLUME_REL, "--ticket-id", ext_ticket_id, "--by", "GewuAtlas"],
                    )
                    _run_gewu_ticket(
                        ws_root,
                        ["--action", "reply", "--volume", TICKET_VOLUME_REL, "--ticket-id", ext_ticket_id, "--by", "GewuAtlas", "--note", summary[:240]],
                    )
                    _run_gewu_ticket(
                        ws_root,
                        ["--action", "done", "--volume", TICKET_VOLUME_REL, "--ticket-id", ext_ticket_id, "--by", "GewuAtlas", "--note", undo_hint[:200]],
                    )
                except VaultStoreError:
                    bridge_status = "bridged_open"
        except VaultStoreError:
            bridge_status = "vault_fallback"

    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    rel_ticket_path = f"20-AI-Zone/99-Assets/98-Archives/Tickets/{op_id}-{op_kind}.md"
    pids_yaml = ", ".join(f'"{fm_scalar(p)}"' for p in (project_ids or []))
    body_lines = [
        "---",
        "doc_type: ticket",
        "track: DOC",
        f'title: "{fm_scalar(title)}"',
        f'summary: "{fm_scalar(summary)}"',
        "status: active",
        "ticket_status: completed",
        f'op_id: "{fm_scalar(op_id)}"',
        f'op_kind: "{fm_scalar(op_kind)}"',
        f'external_ticket_id: "{fm_scalar(ext_ticket_id)}"',
        f"related_projects: [{pids_yaml}]",
        'tags: ["Ticket", "Project", "DOC"]',
        f"updated: {today}",
        "---",
        "",
        f"# {title}",
        "",
        f"- **记录时间**：{now_iso}",
        f"- **操作编号**：`{op_id}`（类型：`{op_kind}`）",
        f"- **协同单号**：`{ext_ticket_id or op_id}`（桥梁状态：`{bridge_status}`）",
        f"- **概要**：{summary}",
        "",
        "## 变更明细",
        "",
        *[f"- {ln}" for ln in details_lines],
        "",
        "## 后悔药（一键回滚指令）",
        "",
        f"```bash\n{undo_hint}\n```",
        "",
    ]
    _atomic_write_noexec(vault_dir / rel_ticket_path, "\n".join(body_lines))
    return {
        "op_id": op_id,
        "ticket_id": ext_ticket_id or f"DOC-{op_id}",
        "ticket_rel_path": rel_ticket_path,
        "bridge_status": bridge_status,
    }


def _stable_key_slug(task_key: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (task_key or "").lower()).strip("-")[:18]
    digest = hashlib.sha256((task_key or "task").encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}" if slug else digest


def _ensure_track_ticket(
    ws_root: Path,
    vault_dir: Path,
    *,
    category: str,
    task_key: str,
    title: str,
    summary: str = "",
    project_id: str = "",
    actor: str = "GewuAtlas",
    ticket_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or idempotently reuse an active DOC or SLIP ticket for batch workflows."""
    cat = category.strip().upper()
    key_slug = _stable_key_slug(task_key)
    has_bridge = _ensure_ticket_volume(ws_root)

    if ticket_id:
        tid = ticket_id.strip()
        if not has_bridge:
            if tid.startswith(f"{cat}-"):
                rel_only = f"20-AI-Zone/99-Assets/98-Archives/Tickets/{cat.lower()}-{tid[len(cat) + 1:]}.md"
                f_only = vault_dir / rel_only
                if f_only.is_file() and "ticket_status: in_progress" in f_only.read_text(encoding="utf-8", errors="replace"):
                    return {
                        "ticket_id": tid,
                        "category": cat,
                        "reused": True,
                        "status": "in_progress",
                        "bridge_status": "vault_only",
                        "key_slug": tid[len(cat) + 1 :],
                        "ticket_rel_path": rel_only,
                    }
            raise VaultStoreError("E_NOT_FOUND", "当前工作区未配置 GewuTicket，无法绑定指定工单号", exit_code=1)
        info = _run_gewu_ticket(ws_root, ["--action", "in", "--volume", TICKET_VOLUME_REL, "--ticket-id", tid])
        tdata = info.get("data") if isinstance(info, dict) else {}
        if not isinstance(tdata, dict) or str(tdata.get("category") or "").upper() != cat:
            raise VaultStoreError("E_PARAM_CONFLICT", f"工单 {tid} 不是有效的 {cat} 工单", exit_code=1)
        st = str(tdata.get("status") or "").lower()
        if st in ("done", "cancelled", "paused") or tdata.get("archived"):
            raise VaultStoreError("E_PARAM_CONFLICT", f"工单 {tid} 当前状态 ({st}) 不可继续写入", exit_code=1)
        picked_by = str(tdata.get("picked_by") or "")
        created_by = str(tdata.get("created_by") or "")
        to_agent = str(tdata.get("to_agent") or "all")
        pre_st = str(tdata.get("pre_review_status") or "not_required").lower()
        need_human = bool(tdata.get("need_human", False))
        if st == "open":
            if need_human or pre_st in ("pending", "rejected"):
                raise VaultStoreError(
                    "E_PRE_REVIEW_REQUIRED",
                    f"工单 {tid} 尚待人类前置放行（pre_review_status={pre_st}），请先审批后再执行",
                    exit_code=1,
                )
            if to_agent not in ("", "all", actor):
                raise VaultStoreError(
                    "E_PARAM_CONFLICT",
                    f"工单 {tid} 定向指派给 '{to_agent}'，当前执行体 '{actor}' 无权代为认领",
                    exit_code=1,
                )
            _run_gewu_ticket(
                ws_root,
                ["--action", "pickup", "--volume", TICKET_VOLUME_REL, "--ticket-id", tid, "--by", actor],
            )
            picked_by = actor
        can_auto_done = (picked_by == actor) and (created_by in ("", actor))
        return {
            "ticket_id": tid,
            "category": cat,
            "reused": True,
            "status": "in_progress",
            "bridge_status": "bridged",
            "key_slug": key_slug,
            "created_by": created_by,
            "picked_by": picked_by,
            "can_auto_done": can_auto_done,
        }

    if has_bridge:
        listed = _run_gewu_ticket(
            ws_root,
            ["--action", "list", "--volume", TICKET_VOLUME_REL, "--category", cat, "--query", key_slug],
        )
        items = ((listed.get("data") or {}).get("tickets") or (listed.get("data") or {}).get("items") or []) if isinstance(listed, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            cand_id = str(item.get("ticket_id") or "")
            cand_st = str(item.get("status") or "").lower()
            if cand_id.endswith(f"-{key_slug}") or f"-{key_slug}-" in cand_id:
                if cand_st in ("open", "in_progress"):
                    cand_picked = str(item.get("picked_by") or "")
                    if cand_st == "open":
                        _run_gewu_ticket(
                            ws_root,
                            ["--action", "pickup", "--volume", TICKET_VOLUME_REL, "--ticket-id", cand_id, "--by", actor],
                        )
                        cand_picked = actor
                    return {
                        "ticket_id": cand_id,
                        "category": cat,
                        "reused": True,
                        "status": "in_progress",
                        "bridge_status": "bridged",
                        "key_slug": key_slug,
                        "created_by": actor,
                        "picked_by": cand_picked,
                        "can_auto_done": cand_picked in ("", actor),
                    }

        out_args = [
            "--action",
            "out",
            "--volume",
            TICKET_VOLUME_REL,
            "--category",
            cat,
            "--from",
            actor,
            "--title",
            title.strip(),
            "--key",
            key_slug,
        ]
        if project_id:
            out_args.extend(["--project-id", project_id])
        if summary:
            out_args.extend(["--content", summary])
        created = _run_gewu_ticket(ws_root, out_args)
        new_id = str(((created.get("data") or {}).get("ticket_id") or ""))
        if new_id:
            try:
                _run_gewu_ticket(
                    ws_root,
                    ["--action", "pickup", "--volume", TICKET_VOLUME_REL, "--ticket-id", new_id, "--by", actor],
                )
            except VaultStoreError:
                pass
        return {
            "ticket_id": new_id,
            "category": cat,
            "reused": False,
            "status": "in_progress",
            "bridge_status": "bridged",
            "key_slug": key_slug,
            "created_by": actor,
            "picked_by": actor,
            "can_auto_done": True,
        }

    # Standalone Vault fallback (e.g. isolated unit tests without workspace GewuTicket)
    prefix = cat.lower()
    rel_path = f"20-AI-Zone/99-Assets/98-Archives/Tickets/{prefix}-{key_slug}.md"
    fpath = vault_dir / rel_path
    reused = False
    if fpath.is_file() and "ticket_status: in_progress" in fpath.read_text(encoding="utf-8", errors="replace"):
        reused = True
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    if not reused:
        lines = [
            "---",
            "doc_type: ticket",
            f"track: {cat}",
            f'title: "{fm_scalar(title.strip())}"',
            f'summary: "{fm_scalar((summary or title).strip())}"',
            "status: active",
            "ticket_status: in_progress",
            f'task_key: "{key_slug}"',
            f'tags: ["Ticket", "{cat}"]',
            f"updated: {today}",
            "---",
            "",
            f"# {title.strip()}",
            "",
            f"- **立项时间**：{now_iso}",
            f"- **任务键**：`{key_slug}`",
            "",
        ]
        _atomic_write_noexec(fpath, "\n".join(lines))
    return {
        "ticket_id": f"{cat}-{key_slug}",
        "category": cat,
        "reused": reused,
        "status": "in_progress",
        "bridge_status": "vault_only",
        "key_slug": key_slug,
        "ticket_rel_path": rel_path,
    }


def ensure_doc_ticket(
    ws_root: Path,
    vault_dir: Path,
    *,
    task_key: str,
    title: str,
    summary: str = "",
    project_id: str = "",
    actor: str = "GewuAtlas",
    ticket_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or idempotently reuse an active DOC ticket for long/batch content workflows."""
    return _ensure_track_ticket(
        ws_root,
        vault_dir,
        category="DOC",
        task_key=task_key,
        title=title,
        summary=summary,
        project_id=project_id,
        actor=actor,
        ticket_id=ticket_id,
    )


def ensure_slip_ticket(
    ws_root: Path,
    vault_dir: Path,
    *,
    task_key: str,
    title: str,
    summary: str = "",
    project_id: str = "",
    actor: str = "GewuAtlas",
    ticket_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or idempotently reuse one SLIP ticket per slip batch workflow."""
    return _ensure_track_ticket(
        ws_root,
        vault_dir,
        category="SLIP",
        task_key=task_key,
        title=title,
        summary=summary,
        project_id=project_id,
        actor=actor,
        ticket_id=ticket_id,
    )


def update_slip_ticket_progress(
    ws_root: Path,
    vault_dir: Path,
    *,
    ticket_info: Dict[str, Any],
    note: str,
    actor: str = "GewuAtlas",
    mark_done: bool = False,
) -> Dict[str, Any]:
    """Append slip batch progress to SLIP ticket and optionally close on completion."""
    return update_doc_ticket_progress(
        ws_root,
        vault_dir,
        ticket_info=ticket_info,
        note=note,
        actor=actor,
        mark_done=mark_done,
    )


def update_doc_ticket_progress(
    ws_root: Path,
    vault_dir: Path,
    *,
    ticket_info: Dict[str, Any],
    note: str,
    actor: str = "GewuAtlas",
    mark_done: bool = False,
) -> Dict[str, Any]:
    """Append execution details to DOC ticket; respect creator/assignee boundary before done."""
    tid = str(ticket_info.get("ticket_id") or "")
    out = dict(ticket_info)
    if ticket_info.get("bridge_status") == "bridged" and tid:
        can_close = bool(ticket_info.get("can_auto_done", True))
        reply_note = note[:360]
        if mark_done and not can_close:
            owner = str(ticket_info.get("created_by") or ticket_info.get("picked_by") or "原负责主体")
            reply_note = f"{note[:260]}（请由 {owner} 核对签阅并决定是否 done，GewuAtlas 不越权代结）"
        _run_gewu_ticket(
            ws_root,
            ["--action", "reply", "--volume", TICKET_VOLUME_REL, "--ticket-id", tid, "--by", actor, "--note", reply_note],
        )
        if mark_done and can_close:
            close_actor = str(ticket_info.get("picked_by") or actor)
            _run_gewu_ticket(
                ws_root,
                ["--action", "done", "--volume", TICKET_VOLUME_REL, "--ticket-id", tid, "--by", close_actor, "--note", f"完成反馈：{note[:240]}"],
            )
            out["status"] = "done"
        else:
            out["status"] = "in_progress"
            if mark_done and not can_close:
                out["auto_done_skipped"] = True
        return out

    rel_path = str(ticket_info.get("ticket_rel_path") or f"20-AI-Zone/99-Assets/98-Archives/Tickets/doc-{ticket_info.get('key_slug', 'task')}.md")
    fpath = vault_dir / rel_path
    if fpath.is_file():
        now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
        raw = fpath.read_text(encoding="utf-8", errors="replace")
        if mark_done:
            raw = raw.replace("ticket_status: in_progress", "ticket_status: completed", 1)
        raw = raw.rstrip() + f"\n- [{now_iso}] {actor}: {note}\n"
        _atomic_write_noexec(fpath, raw)
    out["status"] = "done" if mark_done else "in_progress"
    return out


def record_artifact_ticket_audit(
    ws_root: Path,
    vault_dir: Path,
    *,
    ticket_id: Optional[str],
    action_kind: str,
    note_ref: str,
    title: str,
    domain_scope: str,
    project_id: str,
    file_sha256: str,
    actor: str = "GewuAtlas",
) -> Optional[Dict[str, Any]]:
    """Verify optional --ticket_id (active DOC ticket) and log artifact SHA-256 audit."""
    if not ticket_id:
        return None
    tinfo = ensure_doc_ticket(
        ws_root,
        vault_dir,
        task_key=f"artifact:{note_ref}",
        title=f"Artifact {action_kind}: {title}",
        project_id=project_id,
        actor=actor,
        ticket_id=ticket_id,
    )
    audit_note = f"artifact-{action_kind}: ref={note_ref} title=「{title}」 scope={domain_scope} ({project_id}) sha256={file_sha256}"
    return update_doc_ticket_progress(
        ws_root,
        vault_dir,
        ticket_info=tinfo,
        note=audit_note,
        actor=actor,
        mark_done=False,
    )
