#!/usr/bin/env python3
"""Contract-Rail compliant CLI facade for GewuTicket (4-channel params, Triad/Guard capsules, circuit breaker)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import traceback
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
APP_DIR = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from ticket.ticket_statemachine import (  # noqa: E402
    op_abort,
    op_archive_sweep,
    op_cancel,
    op_develop,
    op_done,
    op_in,
    op_leave,
    op_list,
    op_out,
    op_pause,
    op_pickup,
    op_reopen,
    op_reply,
    op_resume,
    op_review,
)
from guard.delivery_harness import scan_workspace_sprawl  # noqa: E402
from ticket.ticket_usecase import (  # noqa: E402
    usecase_delete,
    usecase_get,
    usecase_list,
    usecase_match,
    usecase_upsert,
)
from ticket.ticket_volume import (  # noqa: E402
    MOUNT_MODES,
    VolumeError,
    VolumeFS,
    default_shadow_dir,
    init_volume,
    load_categories,
    register_category,
)

ACTIONS = (
    "init-volume",
    "register-category",
    "categories",
    "out",
    "in",
    "list",
    "find",
    "pickup",
    "reply",
    "pause",
    "resume",
    "abort",
    "cancel",
    "review",
    "develop",
    "leave",
    "done",
    "reopen",
    "archive-sweep",
    "shadow-diff",
    "shadow-apply",
    "shadow-drop",
    "usecase",
)
READ_ONLY_ACTIONS = frozenset({"in", "list", "find", "categories", "shadow-diff"})
KNOWN_SLOTS = {
    "action": "str",
    "volume": "str",
    "mount_mode": "str",
    "shadow_dir": "str",
    "vol_id": "str",
    "category": "str",
    "ticket_id": "str",
    "title": "str",
    "from": "str",
    "by": "str",
    "to_agent": "str",
    "project_id": "str",
    "note": "str",
    "content": "str",
    "reason": "str",
    "resume_when": "str",
    "stage": "str",
    "outcome": "str",
    "expected_sha256": "str",
    "status": "str",
    "pre_review_status": "str",
    "post_review_status": "str",
    "archived": "bool",
    "query": "str",
    "key": "str",
    "need_human": "bool",
    "force": "bool",
    "reopen": "bool",
    "complete_l1": "bool",
    "full": "bool",
    "start_line": "int",
    "end_line": "int",
    "write_scope": "list",
    "levels": "list",
    "template_kind": "str",
    "pre_review_gate": "str",
    "post_review_policy": "str",
    "allow_write_scope": "bool",
    "allow_patch": "bool",
    "archive_after_days": "int",
    "purge_archive_after_days": "int",
    "hold_archive_if_post_unreviewed": "bool",
    "mode": "str",
    "uc_id": "str",
    "scenarios": "str",
    "tags": "list",
    "top_k": "int",
}
_BAD_FRAGMENT_RE = re.compile(r"\x00|(?:^|[\\/])\.\.(?:[\\/]|$)")
_SQL_FRAGMENT_RE = re.compile(r"\b(?:union\s+select|drop\s+table|insert\s+into|delete\s+from)\b", re.I)


def _runtime_dirs() -> Tuple[Path, Path]:
    custom = os.environ.get("GEWU_APP_RUNTIME_DIR", "").strip()
    base = Path(custom) if custom else (APP_DIR / ".local")
    return base / "state", base / "logs"


def _log_error(log_dir: Path, raw_tb: str) -> None:
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(log_dir, 0o700)
        lf = log_dir / "ticket-cli-errors.log"
        fd = os.open(lf, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(raw_tb + "\n")
        os.chmod(lf, 0o600)
    except OSError:
        pass


def _triad(state: str, orbit: List[str], anchor: str, terminal: bool = True) -> Dict[str, Any]:
    branches = [*orbit, "若信息齐全或任务已收口: 直接向人类汇报现状与结果"]
    return {
        "kind": "TRIAD",
        "terminal": terminal,
        "STATE": state,
        "ORBIT": branches,
        "ANCHOR": anchor[:80],
    }


def _guard(mode: str, reason: str, guidance: str, anchor: str, fill_template: Optional[Dict[str, Any]] = None, remaining: Optional[int] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "kind": f"GUARD:{mode}",
        "mode": mode,
        "reason": reason,
        "action_guidance": guidance,
        "ANCHOR": anchor[:80],
    }
    if fill_template is not None:
        out["fill_template"] = fill_template
    if remaining is not None:
        out["attempt_remaining"] = remaining
    return out


def _envelope(ok: bool, code: str, capsule: Dict[str, Any], *, data: Any = None, missing_slots: Optional[List[str]] = None) -> Dict[str, Any]:
    kind = capsule.get("kind", "TRIAD")
    if kind == "TRIAD":
        orbit_txt = "\n".join(f"- {item}" for item in capsule["ORBIT"])
        reminder = f"🧭 状态胶囊 (Triad Capsule)\n📍 STATE: {capsule['STATE']}\n🔀 ORBIT:\n{orbit_txt}\n⚓ ANCHOR: {capsule['ANCHOR']}"
    else:
        mode = capsule["mode"]
        extra = f"\n📝 fill_template: {json.dumps(capsule['fill_template'], ensure_ascii=False)}" if "fill_template" in capsule else ""
        rem = f" | 剩余重试: {capsule['attempt_remaining']}" if "attempt_remaining" in capsule else ""
        no_retry = " | 禁止重试" if mode == "HALTED" else ""
        reminder = f"🛡️ 门禁胶囊 (GUARD: {mode}{no_retry}{rem})\n❌ 原因: {capsule['reason']}\n🎯 指引: {capsule['action_guidance']}{extra}\n⚓ ANCHOR: {capsule['ANCHOR']}"
    payload: Dict[str, Any] = {
        "ok": ok,
        "code": code,
        "isError": not ok,
        "structuredContent": {"capsule": capsule},
        "reminder": reminder,
    }
    if data is not None:
        payload["data"] = data
    if missing_slots:
        payload["missing_slots"] = missing_slots
    return payload


def _parse_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in ("true", "1", "yes", "y"):
        return True
    if s in ("false", "0", "no", "n"):
        return False
    raise VolumeError("E_PARAM_INVALID", f"无效布尔值: {val}", exit_code=2)


def _parse_list(val: Any) -> List[str]:
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    s = str(val).strip()
    if not s:
        return []
    if s.startswith("["):
        arr = json.loads(s)
        if not isinstance(arr, list):
            raise VolumeError("E_PARAM_INVALID", "列表槽位 JSON 须为数组", exit_code=2)
        return [str(x).strip() for x in arr if str(x).strip()]
    return [x.strip() for x in s.split(",") if x.strip()]


def _coerce_slot(key: str, val: Any, stdin_text: Optional[str]) -> Any:
    if key in ("content", "note") and val == "-":
        if stdin_text is None:
            raise VolumeError("E_PARAM_MISSING", f"--{key} - 需要标准输入内容", exit_code=2)
        val = stdin_text
    if isinstance(val, str) and (_BAD_FRAGMENT_RE.search(val) or _SQL_FRAGMENT_RE.search(val)):
        raise VolumeError("E_PARAM_INVALID", f"槽位 '{key}' 包含越界或非法片段", exit_code=2)
    kind = KNOWN_SLOTS[key]
    if kind == "bool":
        return _parse_bool(val)
    if kind == "int":
        try:
            return int(val)
        except (TypeError, ValueError) as exc:
            raise VolumeError("E_PARAM_INVALID", f"槽位 '{key}' 需要整数", exit_code=2) from exc
    if kind == "list":
        return _parse_list(val)
    return str(val)


def _normalize_four_channels(argv: List[str], stdin_text: Optional[str]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    if any(tok in ("--template", "--describe", "-h", "--help") for tok in argv):
        tpl = {
            "schema": "gewu.fill-template/1",
            "command": "python3 -I -B scripts/ticket/ticket_cli.py",
            "actions": list(ACTIONS),
            "mount_modes": list(MOUNT_MODES),
            "required_common": ["action", "volume"],
            "examples": {
                "init_volume": {"action": "init-volume", "volume": "./Volumes/GewuTicket-data", "mount_mode": "rw"},
                "out": {"action": "out", "volume": "./Volumes/GewuTicket-data", "category": "DOC", "from": "Antigravity", "title": "<任务标题>"},
                "review": {"action": "review", "volume": "./Volumes/GewuTicket-data", "ticket_id": "<TICKET-...>", "stage": "pre|post", "outcome": "approve|reject", "by": "<审阅人>", "expected_sha256": "<哈希>", "note": "<决定依据>"},
            },
        }
        return {}, tpl

    cli_vals: Dict[str, Any] = {}
    set_vals: Dict[str, Any] = {}
    payload_vals: Dict[str, Any] = {}
    i = 0
    while i < len(argv):
        tok = argv[i]
        if not tok.startswith("--"):
            raise VolumeError("E_PARAM_UNKNOWN_FIELD", f"未知位置参数: {tok}", exit_code=2)
        if "=" in tok:
            k_raw, v_raw = tok[2:].split("=", 1)
            k = k_raw.replace("-", "_")
            has_inline = True
        else:
            k = tok[2:].replace("-", "_")
            v_raw = None
            has_inline = False

        if k == "set":
            kv = v_raw if has_inline else (argv[i + 1] if i + 1 < len(argv) else None)
            if not has_inline:
                i += 1
            if not kv or "=" not in kv:
                raise VolumeError("E_PARAM_INVALID", "--set 需要 k=v 格式", exit_code=2)
            sk, sv = kv.split("=", 1)
            sk_norm = sk.strip().replace("-", "_")
            if sk_norm not in KNOWN_SLOTS:
                raise VolumeError("E_PARAM_UNKNOWN_FIELD", f"未知字段: {sk_norm}", exit_code=2)
            set_vals[sk_norm] = _coerce_slot(sk_norm, sv, stdin_text)
        elif k in ("payload", "json_stdin"):
            raw_p = v_raw if has_inline else (argv[i + 1] if i + 1 < len(argv) else None)
            if not has_inline and k == "payload":
                i += 1
            if raw_p in (None, "-") or k == "json_stdin":
                raw_p = stdin_text
            if not raw_p:
                raise VolumeError("E_PARAM_MISSING", f"--{k} 需要 JSON 载荷", exit_code=2)
            try:
                loaded = json.loads(raw_p)
            except json.JSONDecodeError as exc:
                raise VolumeError("E_PARAM_INVALID", "payload 不是合法 JSON", exit_code=2) from exc
            if not isinstance(loaded, dict):
                raise VolumeError("E_PARAM_INVALID", "payload 必须为 JSON 对象", exit_code=2)
            for pk, pv in loaded.items():
                pk_norm = str(pk).replace("-", "_")
                if pk_norm not in KNOWN_SLOTS:
                    raise VolumeError("E_PARAM_UNKNOWN_FIELD", f"未知字段: {pk_norm}", exit_code=2)
                if pk_norm in payload_vals:
                    raise VolumeError("E_PARAM_INVALID", f"重复字段: {pk_norm}", exit_code=2)
                payload_vals[pk_norm] = _coerce_slot(pk_norm, pv, stdin_text)
        else:
            if k not in KNOWN_SLOTS:
                raise VolumeError("E_PARAM_UNKNOWN_FIELD", f"未知参数: --{tok[2:]}", exit_code=2)
            if has_inline:
                val: Any = v_raw
            elif KNOWN_SLOTS[k] == "bool":
                if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                    val = argv[i + 1]
                    i += 1
                else:
                    val = True
            else:
                if i + 1 >= len(argv):
                    raise VolumeError("E_PARAM_MISSING", f"参数 --{tok[2:]} 缺少值", exit_code=2)
                val = argv[i + 1]
                i += 1
            coerced = _coerce_slot(k, val, stdin_text)
            if KNOWN_SLOTS[k] == "list" and k in cli_vals:
                cli_vals[k] = cli_vals[k] + coerced
            else:
                cli_vals[k] = coerced
        i += 1

    merged: Dict[str, Any] = {"mount_mode": "rw"}
    merged.update(payload_vals)
    merged.update(set_vals)
    merged.update(cli_vals)
    return merged, None


def _fingerprint(request) -> str:
    session = os.environ.get("GEWU_TICKET_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or str(os.getppid())
    canonical = json.dumps({"skill": "gewu-ticket", "request": request, "session": session}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _load_breaker(state_dir: Path) -> Dict[str, Any]:
    bp = state_dir / "ticket-circuit-breaker.json"
    if bp.is_file():
        try:
            loaded = json.loads(bp.read_text("utf-8"))
            return {k: v for k, v in loaded.items() if time.time() - v.get("updated", 0) <= 300}
        except Exception:
            return {}
    return {}


def _save_breaker(state_dir: Path, data: Dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    bp = state_dir / "ticket-circuit-breaker.json"
    for value in data.values():
        value.setdefault("updated", time.time())
    from ticket.ticket_volume import _write_atomic_noexec
    _write_atomic_noexec(bp, json.dumps(data, ensure_ascii=False).encode(), 0o600)


def _require_fields(args: Dict[str, Any], *fields: str) -> None:
    missing = [f for f in fields if not str(args.get(f, "")).strip()]
    if missing:
        tpl = {"action": args.get("action", "<action>")}
        for f in fields:
            tpl[f] = args.get(f) or f"<必填:{f}>"
        raise VolumeError("E_PARAM_MISSING", f"缺少必填槽位: {', '.join(missing)}", exit_code=2)


def _attach_delivery_gate(env: Dict[str, Any], ws_root: Path) -> None:
    sprawl = scan_workspace_sprawl(ws_root)
    tag = f"[DELIVERY_GATE: BLOCKED (根越界:{','.join(sprawl)})]" if sprawl else "[DELIVERY_GATE: PASS (60/60)]"
    sc = env.get("structuredContent")
    cap = sc.get("capsule") if isinstance(sc, dict) else None
    if isinstance(cap, dict):
        anchor = str(cap.get("ANCHOR") or "")
        if "DELIVERY_GATE" not in anchor:
            cap["ANCHOR"] = f"{anchor} {tag}".strip()
    if "reminder" in env and "DELIVERY_GATE" not in str(env["reminder"]):
        env["reminder"] = f"{env['reminder']}\n{tag}"
    if isinstance(env.get("data"), dict):
        env["data"]["delivery_gate"] = {
            "checked": True,
            "operator_passed": not sprawl,
            "sprawl_items": sprawl,
        }


def _dispatch(args: Dict[str, Any], state_dir: Path) -> Tuple[int, Dict[str, Any]]:
    action = args.get("action")
    if not action or action not in ACTIONS:
        raise VolumeError("E_PARAM_MISSING" if not action else "E_PARAM_INVALID", f"action 须为 {', '.join(ACTIONS)}", exit_code=2)

    if action == "usecase":
        mode = str(args.get("mode") or "list").strip().lower()
        if mode not in ("list", "get", "match", "upsert", "delete"):
            raise VolumeError("E_PARAM_INVALID", f"usecase --mode 不支持: {mode}", exit_code=2)
        if mode == "list":
            data = usecase_list(APP_DIR)
            cap = _triad(f"已列出 {data['count']} 张 GewuTicket 实战经验路书 [只读]", ["匹配路书: --action usecase --mode match --query '<关键词>'"], "入口位于 GewuTicket/usecase-for-agent/README.md。")
            return 0, _envelope(True, "OK_USECASE_LIST", cap, data=data)
        if mode == "get":
            _require_fields(args, "uc_id")
            try:
                data = usecase_get(APP_DIR, args["uc_id"])
            except (ValueError, FileNotFoundError) as exc:
                raise VolumeError("E_PARAM_INVALID", str(exc), exit_code=2) from exc
            cap = _triad(f"已读取路书 {data['uc_id']} [只读]", ["更新心得: --action usecase --mode upsert"], "建议开工前直接复用卡片内避坑对策。")
            return 0, _envelope(True, "OK_USECASE_GET", cap, data=data)
        if mode == "match":
            _require_fields(args, "query")
            data = usecase_match(APP_DIR, args["query"], top_k=args.get("top_k") or 3)
            cap = _triad(f"匹配到 {data['count']} 张相关经验路书 [只读]", ["查看单卡: --action usecase --mode get --uc_id <id>"], "优先复用高频损耗对照表。")
            return 0, _envelope(True, "OK_USECASE_MATCH", cap, data=data)
        if mode == "upsert":
            _require_fields(args, "uc_id", "title", "content")
            try:
                data = usecase_upsert(
                    APP_DIR,
                    uc_id=args["uc_id"],
                    title=args["title"],
                    scenarios=args.get("scenarios", ""),
                    tags=args.get("tags"),
                    content=args["content"],
                    by=args.get("by", "Antigravity"),
                )
            except ValueError as exc:
                raise VolumeError("E_PARAM_INVALID", str(exc), exit_code=2) from exc
            cap = _triad(f"已{data['operation']}路书 {data['uc_id']} 并刷新 README.md", ["查看列表: --action usecase --mode list"], "经验卡已落盘至 GewuTicket/usecase-for-agent/。")
            return 0, _envelope(True, "OK_USECASE_UPSERTED", cap, data=data)
        _require_fields(args, "uc_id")
        try:
            data = usecase_delete(APP_DIR, args["uc_id"])
        except (ValueError, FileNotFoundError) as exc:
            raise VolumeError("E_PARAM_INVALID", str(exc), exit_code=2) from exc
        cap = _triad(f"已删除路书 {data['uc_id']} 并更新 README.md", ["查看列表: --action usecase --mode list"], "索引与磁盘卡片保持同步。")
        return 0, _envelope(True, "OK_USECASE_DELETED", cap, data=data)

    _require_fields(args, "volume")
    vol_path = Path(args["volume"]).expanduser()
    if not vol_path.is_absolute():
        vol_path = (Path.cwd() / vol_path).resolve()

    if action == "init-volume":
        data = init_volume(
            vol_path,
            vol_id=args.get("vol_id"),
            archive_after_days=args.get("archive_after_days", 7),
            purge_archive_after_days=args.get("purge_archive_after_days", 30),
        )
        cap = _triad(f"已初始化静态无执行位工单卷 '{data['vol_id']}'", ["创建首张工单: --action out"], "数据卷严禁可执行位。")
        return 0, _envelope(True, "OK_VOLUME_INIT", cap, data=data)

    mount_mode = str(args.get("mount_mode", "rw"))
    shadow_path: Optional[Path] = None
    if mount_mode == "shadow-rw":
        if args.get("shadow_dir"):
            sp = Path(args["shadow_dir"]).expanduser()
            shadow_path = sp if sp.is_absolute() else (Path.cwd() / sp).resolve()
        else:
            shadow_path = default_shadow_dir(state_dir.parent, vol_path.name)

    fs = VolumeFS(vol_path, mount_mode=mount_mode, shadow_root=shadow_path)

    if action == "categories":
        cats = load_categories(fs)
        cap = _triad(f"已读取 {len(cats)} 个已注册工单分类策略 [只读]", ["建单: --action out"], "分类可在 _meta/categories.json 动态扩展。")
        return 0, _envelope(True, "OK_CATEGORIES", cap, data={"vol_id": fs.vol_id, "categories": cats})

    if action == "register-category":
        _require_fields(args, "category")
        spec = {
            "title": args.get("title") or args["category"],
            "template": args.get("template_kind", "freeform"),
            "allow_write_scope": args.get("allow_write_scope", False),
            "allow_patch": args.get("allow_patch", False),
            "pre_review_gate": args.get("pre_review_gate", "none"),
            "post_review_policy": args.get("post_review_policy", "none"),
            "archive_after_days": args.get("archive_after_days", 7),
            "purge_archive_after_days": args.get("purge_archive_after_days", 30),
            "hold_archive_if_post_unreviewed": args.get("hold_archive_if_post_unreviewed", True),
        }
        data = register_category(fs, args["category"], spec)
        cap = _triad(f"已注册分类 '{args['category']}' 并创建四级状态桶", ["用该分类建单: --action out"], "新分类无需改代码即可参与状态流转。")
        return 0, _envelope(True, "OK_CATEGORY_REGISTERED", cap, data=data)

    if action == "out":
        _require_fields(args, "category", "title")
        creator = args.get("from") or args.get("by") or ""
        if not creator:
            raise VolumeError("E_PARAM_MISSING", "out 必须提供 --from <建单方>", exit_code=2)
        data = op_out(
            fs,
            category=args["category"],
            created_by=creator,
            title=args["title"],
            to_agent=args.get("to_agent", "all"),
            project_id=args.get("project_id", ""),
            need_human=args.get("need_human", False),
            write_scope=args.get("write_scope"),
            content=args.get("content", ""),
            key=args.get("key", ""),
        )
        nxt = "请人类预审放行: --action review" if data["pre_review_status"] == "pending" else f"认领接手: --action pickup --ticket-id {data['ticket_id']}"
        cap = _triad(f"已创建工单 {data['ticket_id']}（{data['bucket']}/{data['status']}）", [nxt], "open 与 in_progress 共居 active/ 目录。")
        return 0, _envelope(True, "OK_OUT", cap, data=data)

    if action == "in":
        _require_fields(args, "ticket_id")
        data = op_in(
            fs,
            ticket_id=args["ticket_id"],
            full=bool(args.get("full", False)),
            start_line=args.get("start_line"),
            end_line=args.get("end_line"),
        )
        cap = _triad(f"已读取 {data['ticket_id']}（{data['bucket']}/{data['status']} | pre:{data['pre_review_status']} | post:{data['post_review_status']}）[只读]", ["如需审阅或授权请使用返回之 approval_sha256"], "只读核对不修改状态或锁。")
        return 0, _envelope(True, "OK_IN", cap, data=data)

    if action in ("list", "find"):
        data = op_list(
            fs,
            category=args.get("category"),
            status=args.get("status"),
            pre_review_status=args.get("pre_review_status"),
            post_review_status=args.get("post_review_status"),
            archived=args.get("archived"),
            query=args.get("query", ""),
        )
        cap = _triad(f"共检索到 {data['count']} 张匹配工单 [只读]", ["查单详情: --action in --ticket-id <单号>"], "支持 ro/rw/shadow-rw 挂载检索。")
        return 0, _envelope(True, "OK_LIST", cap, data=data)

    if action == "pickup":
        _require_fields(args, "ticket_id", "by")
        data = op_pickup(fs, ticket_id=args["ticket_id"], by=args["by"], force=args.get("force", False))
        cap = _triad(f"已认领 {data['ticket_id']} 进入 in_progress", [f"推进后回复: --action reply --ticket-id {data['ticket_id']}"], "同目录 active/ 内锁定，不搬移路径。")
        return 0, _envelope(True, "OK_PICKUP", cap, data=data)

    if action == "reply":
        _require_fields(args, "ticket_id", "by", "note")
        data = op_reply(
            fs,
            ticket_id=args["ticket_id"],
            by=args["by"],
            note=args["note"],
            complete_l1=args.get("complete_l1", False),
        )
        cap = _triad(f"已向 {data['ticket_id']} 追加 §6 回复", ["核验完成可结单: --action done"], "reply 只追加对话流，不改变状态。")
        return 0, _envelope(True, "OK_REPLY", cap, data=data)

    if action == "pause":
        _require_fields(args, "ticket_id", "by")
        reason = args.get("reason") or args.get("note") or ""
        resume_when = args.get("resume_when") or ""
        data = op_pause(
            fs,
            ticket_id=args["ticket_id"],
            by=args["by"],
            reason=reason,
            resume_when=resume_when,
            expected_sha256=args.get("expected_sha256", ""),
            force=args.get("force", False),
        )
        cap = _triad(f"已暂停 {data['ticket_id']} 迁入 paused/", ["条件落实后恢复: --action resume"], "暂停清空接手人，保留原单号与人工门禁。")
        return 0, _envelope(True, "OK_PAUSE", cap, data=data)

    if action == "resume":
        _require_fields(args, "ticket_id", "by", "note")
        data = op_resume(fs, ticket_id=args["ticket_id"], by=args["by"], note=args["note"], expected_sha256=args.get("expected_sha256", ""))
        cap = _triad(f"已恢复 {data['ticket_id']} 回 active/open", ["重新认领接手: --action pickup"], "恢复回 open，需重新审批/接单。")
        return 0, _envelope(True, "OK_RESUME", cap, data=data)

    if action == "abort":
        _require_fields(args, "ticket_id", "by")
        data = op_abort(fs, ticket_id=args["ticket_id"], by=args["by"], force=args.get("force", False))
        cap = _triad(f"已弃权 {data['ticket_id']} 回退 active/open", ["由下一主体接手: --action pickup"], "已保留断点并释放接手锁定。")
        return 0, _envelope(True, "OK_ABORT", cap, data=data)

    if action == "cancel":
        _require_fields(args, "ticket_id", "by")
        data = op_cancel(fs, ticket_id=args["ticket_id"], by=args["by"], note=args.get("note", ""), force=args.get("force", False))
        cap = _triad(f"已取消 {data['ticket_id']} 迁入 closed/", ["如需重启由人类执行: --action reopen"], "已顺带完成超 7 天归档扫盘。")
        return 0, _envelope(True, "OK_CANCEL", cap, data=data)

    if action == "review":
        _require_fields(args, "ticket_id", "stage", "outcome", "by", "expected_sha256", "note")
        data = op_review(
            fs,
            ticket_id=args["ticket_id"],
            stage=args["stage"],
            outcome=args["outcome"],
            by=args["by"],
            expected_sha256=args["expected_sha256"],
            note=args["note"],
            levels=args.get("levels"),
            reopen_on_reject=args.get("reopen", False),
        )
        cap = _triad(f"已完成 {data['ticket_id']} ({data['stage']}) 人类审阅留痕", ["查询最新状态: --action in"], "审阅强校验原票 SHA-256，防止旧决定重放。")
        return 0, _envelope(True, "OK_REVIEW", cap, data=data)

    if action == "develop":
        _require_fields(args, "ticket_id", "by", "expected_sha256", "note")
        data = op_develop(
            fs,
            ticket_id=args["ticket_id"],
            by=args["by"],
            expected_sha256=args["expected_sha256"],
            note=args["note"],
            write_scope=args.get("write_scope"),
        )
        cap = _triad(f"已绑定 {data['ticket_id']} 开发范围授权", ["接手执行: --action pickup"], "范围或 §1/§2 变动须重批。")
        return 0, _envelope(True, "OK_DEVELOP", cap, data=data)

    if action == "leave":
        _require_fields(args, "ticket_id", "by")
        data = op_leave(fs, ticket_id=args["ticket_id"], by=args["by"])
        cap = _triad(f"已撤销 {args['ticket_id']} 当前开发授权", ["后续恢复开发需重办 develop"], "断点内容保留在原票。")
        return 0, _envelope(True, "OK_LEAVE", cap, data=data)

    if action == "done":
        _require_fields(args, "ticket_id", "by")
        data = op_done(
            fs,
            ticket_id=args["ticket_id"],
            by=args["by"],
            note=args.get("note", ""),
            complete_l1=args.get("complete_l1", False),
        )
        post_hint = "该分类需完工后人类审阅（不阻塞后续推进）" if data["post_review_status"] == "pending_review" else "本单已完成收口"
        cap = _triad(f"已结单 {data['ticket_id']} → closed/（{post_hint}）", ["查台账: --action list"], "已顺带执行超 7 天 closed→archive 扫盘。")
        return 0, _envelope(True, "OK_DONE", cap, data=data)

    if action == "reopen":
        _require_fields(args, "ticket_id", "by")
        data = op_reopen(fs, ticket_id=args["ticket_id"], by=args["by"], to_agent=args.get("to_agent", ""), note=args.get("note", ""))
        cap = _triad(f"已重开 {data['ticket_id']} 迁回 active/open", ["重新认领: --action pickup"], "已保留历史对话流。")
        return 0, _envelope(True, "OK_REOPEN", cap, data=data)

    if action == "archive-sweep":
        data = op_archive_sweep(fs)
        purged = len(data.get("purged_from_archive") or [])
        cap = _triad(
            f"冷归档扫盘完成：归档 {len(data['moved_to_archive'])} 份，待审暂留 {len(data['held_unreviewed'])} 份，清理超30天历史归档 {purged} 份",
            ["查阅待审: --action list --post-review-status pending_review"],
            "超7天 closed/ 自动进 archive/，archive/ 中结单超 30 天自动删除。",
        )
        return 0, _envelope(True, "OK_ARCHIVE_SWEEP", cap, data=data)

    if action == "shadow-diff":
        data = fs.shadow_diff()
        cap = _triad(f"影子层增量：新增 {len(data['added'])}、修改 {len(data['modified'])}、删除白标 {len(data['deleted'])} [只读]", ["合入底卷: --action shadow-apply", "丢弃影子: --action shadow-drop"], "底卷始终保持零污染。")
        return 0, _envelope(True, "OK_SHADOW_DIFF", cap, data=data)

    if action == "shadow-apply":
        data = fs.shadow_apply()
        cap = _triad(f"影子改动已可恢复合入底卷，影子保留供核对/丢弃 '{data['vol_id']}'", ["查询底卷: --action list --mount-mode rw"], "合入前已核验底卷基线未漂移。")
        return 0, _envelope(True, "OK_SHADOW_APPLY", cap, data=data)

    data = fs.shadow_drop()
    cap = _triad(f"已清理影子层（丢弃 {data['discarded_changes']} 项变更），底卷完好未动", ["重新只读查看: --action list --mount-mode ro"], "底卷一字节未改。")
    return 0, _envelope(True, "OK_SHADOW_DROP", cap, data=data)


def main(argv: Optional[List[str]] = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    state_dir, log_dir = _runtime_dirs()
    wants_stdin = any(x == "-" or x.split("=", 1)[0] in ("--json-stdin", "--json_stdin") or x.endswith("=-") for x in raw_argv)
    stdin_text = sys.stdin.read() if wants_stdin else None
    fp = _fingerprint({"argv": raw_argv, "stdin_sha256": hashlib.sha256((stdin_text or "").encode()).hexdigest()})
    breaker = _load_breaker(state_dir)

    try:
        args, tpl = _normalize_four_channels(raw_argv, stdin_text)
        if tpl is not None:
            cap = _triad("已返回格物工单最小填空模板（零副作用）", ["按 fill_template 槽位传参调用"], "严禁编造未声明参数。", terminal=False)
            print(json.dumps(_envelope(True, "OK_TEMPLATE", cap, data=tpl), ensure_ascii=False))
            return 0

        fp = _fingerprint(args)
        is_ro = (args.get("action") in READ_ONLY_ACTIONS) or (
            args.get("action") == "usecase" and str(args.get("mode") or "list").strip().lower() in ("list", "get", "match")
        )
        rec = breaker.get(fp, {"fail_streak": 0, "ro_streak": 0})
        if time.time() - rec.get("updated", 0) > 300:
            rec = {"fail_streak": 0, "ro_streak": 0}
        if rec.get("fail_streak", 0) >= 2 or (is_ro and rec.get("ro_streak", 0) >= 3):
            cap = _guard("HALTED", "E_HALT_LOOP_DETECTED: 检测到相同参数连续失败或只读空转，触发硬熔断", "立即停止同参重试，核对现状并向人类汇报。", "GUARD: HALTED | 禁止重试")
            print(json.dumps(_envelope(False, "E_HALT_LOOP_DETECTED", cap), ensure_ascii=False))
            return 2

        code, env = _dispatch(args, state_dir)
        if not is_ro and code == 0:
            _attach_delivery_gate(env, APP_DIR.parent)
        if is_ro:
            breaker[fp] = {"fail_streak": 0, "ro_streak": rec.get("ro_streak", 0) + 1}
        else:
            # Any successful state mutation invalidates prior read-only polling streaks across fingerprints.
            for bval in breaker.values():
                if isinstance(bval, dict) and bval.get("ro_streak", 0):
                    bval["ro_streak"] = 0
            breaker[fp] = {"fail_streak": 0, "ro_streak": 0}
        _save_breaker(state_dir, breaker)
        print(json.dumps(env, ensure_ascii=False))
        return code

    except VolumeError as ve:
        is_input_req = ve.code in ("E_PRE_REVIEW_REQUIRED", "E_VERSION_MISMATCH", "E_VOLUME_READONLY", "E_SHADOW_CONFLICT")
        is_param = ve.code.startswith("E_PARAM_")
        if is_param:
            fail_n = breaker.get(fp, {}).get("fail_streak", 0) + 1
            breaker[fp] = {"fail_streak": fail_n, "ro_streak": 0}
            _save_breaker(state_dir, breaker)
            if fail_n >= 2:
                cap = _guard("HALTED", f"E_HALT_LOOP_DETECTED: 同指纹连续参数错误 ({ve.code})", "停止重试并向人类确认正确槽位。", "GUARD: HALTED | 禁止重试")
                print(json.dumps(_envelope(False, "E_HALT_LOOP_DETECTED", cap), ensure_ascii=False))
                return 2
            cap = _guard("PARAM_FIX", f"{ve.code}: {ve.message}", "请先运行 --template 对照合法槽位修正后重试。", "最多剩余 1 次重试机会。", fill_template={"action": "<action>", "volume": "<卷路径>"}, remaining=1)
            print(json.dumps(_envelope(False, ve.code, cap), ensure_ascii=False))
            return 2

        mode = "INPUT_REQUIRED" if is_input_req else "HALTED"
        guidance = "请向人类呈报当前原票状态并请求对应决定/授权。" if is_input_req else "业务状态拒绝或并发冲突，禁止换参绕过。"
        cap = _guard(mode, f"{ve.code}: {ve.message}", guidance, f"GUARD: {mode} | 禁止重试")
        print(json.dumps(_envelope(False, ve.code, cap), ensure_ascii=False))
        return ve.exit_code

    except Exception:  # noqa: BLE001 - swallow all internal tracebacks at CLI boundary
        _log_error(log_dir, traceback.format_exc())
        cap = _guard("HALTED", "E_HALT_INTERNAL: 工单算子内部异常（原始堆栈已脱敏写入运行日志）", "立即停止重试并向人类工程师汇报。", "GUARD: HALTED | 禁止重试")
        print(json.dumps(_envelope(False, "E_HALT_INTERNAL", cap), ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
