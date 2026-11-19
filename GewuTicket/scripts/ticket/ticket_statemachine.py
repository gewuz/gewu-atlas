#!/usr/bin/env python3
"""Dual-dimension orthogonal ticket state machine & 7-day archive engine for GewuTicket."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from ticket.ticket_volume import STATE_BUCKETS, VolumeError, VolumeFS, load_categories

ACTIVE_STATUSES = ("open", "in_progress")
CLOSED_STATUSES = ("done", "cancelled")
ALL_STATUSES = ("open", "in_progress", "paused", "done", "cancelled")
PRE_REVIEW_STATES = ("not_required", "pending", "approved", "rejected")
POST_REVIEW_STATES = ("not_required", "pending_review", "approved", "changes_requested")

_TICKET_ID_RE = re.compile(r"^TICKET-\d{8}T\d{4}-[A-Z][A-Z0-9_-]{1,15}-[a-z0-9-]+$")
_TASK_LINE_RE = re.compile(r"^\s*-\s*\[([ xX])\]\s*(?:\[(L[012])\]\s*)?(.*)$")


def _now() -> datetime:
    return datetime.now().astimezone()


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _slug(name: str) -> str:
    raw = name.strip().lower()
    cleaned = "".join(c for c in raw if ("a" <= c <= "z") or ("0" <= c <= "9") or c == "-").strip("-")
    if cleaned:
        return cleaned
    if raw:
        return "u-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:6]
    return "agent"


def _ident_ok(name: str, label: str = "主体") -> str:
    if not isinstance(name, str) or not name.strip() or re.search(r'[\r\n"\\]', name):
        raise VolumeError("E_PARAM_INVALID", f"{label}标识包含非法字符或为空", exit_code=2)
    return name.strip()


def _fm_scalar(val: str) -> str:
    flat = re.sub(r"[\r\n]+", " ", str(val)).strip()
    return flat.replace("\\", "\\\\").replace('"', '\\"')


def _parse_yaml_scalar(raw: str) -> Any:
    s = raw.strip()
    if s == "true":
        return True
    if s == "false":
        return False
    if s == "null" or s == "~":
        return None
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        parts = []
        for item in inner.split(","):
            parts.append(_parse_yaml_scalar(item))
        return parts
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        inner = s[1:-1]
        if s.startswith('"'):
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return s


def _format_yaml_value(val: Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if val is None:
        return "null"
    if isinstance(val, list):
        items = ", ".join(f'"{_fm_scalar(str(x))}"' for x in val)
        return f"[{items}]"
    return f'"{_fm_scalar(str(val))}"'


def parse_ticket(text: str) -> Tuple[Dict[str, Any], str]:
    match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n?", text, re.S)
    if not match:
        raise VolumeError("E_TICKET_CORRUPT", "工单缺少 YAML Frontmatter", exit_code=1)
    meta: Dict[str, Any] = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = _parse_yaml_scalar(v)
    body = text[match.end():]
    return meta, body


_FIELD_ORDER = (
    "doc_type",
    "ticket_id",
    "category",
    "status",
    "pre_review_status",
    "post_review_status",
    "need_human",
    "human_levels",
    "title",
    "summary",
    "created_by",
    "to_agent",
    "picked_by",
    "picked_at",
    "project_id",
    "write_scope",
    "dev_by",
    "dev_scope_sha256",
    "dev_approved_sha256",
    "pause_reason",
    "resume_when",
    "paused_by",
    "paused_at",
    "closed_at",
    "archived",
    "archived_at",
    "created_at",
    "updated",
)


def dump_ticket(meta: Dict[str, Any], body: str) -> str:
    keys = [k for k in _FIELD_ORDER if k in meta] + sorted(k for k in meta if k not in _FIELD_ORDER)
    lines = ["---"]
    for k in keys:
        lines.append(f"{k}: {_format_yaml_value(meta[k])}")
    lines.append("---")
    clean_body = body.lstrip("\n")
    return "\n".join(lines) + "\n\n" + clean_body


def _scope_digest(meta: Dict[str, Any], body: str) -> str:
    sections: List[str] = []
    for num in (1, 2):
        matches = re.findall(
            r"^##[ \t]+" + str(num) + r"(?:[ .、．][^\n]*)?\n(.*?)(?=^##[ \t]|\Z)",
            body,
            re.M | re.S,
        )
        sections.append("\n".join(re.sub(r"\[[xX ]\]", "[ ]", sec).strip() for sec in matches))
    payload = {
        "ticket_id": meta.get("ticket_id"),
        "category": meta.get("category"),
        "title": meta.get("title"),
        "summary": meta.get("summary"),
        "write_scope": meta.get("write_scope", []),
        "tasks_and_anchors": sections,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _locate_ticket(fs: VolumeFS, ticket_id: str) -> Tuple[str, str, Dict[str, Any], str, str]:
    if not isinstance(ticket_id, str) or not _TICKET_ID_RE.fullmatch(ticket_id):
        raise VolumeError("E_PARAM_INVALID", f"非法工单号格式: {ticket_id}", exit_code=2)
    cats = load_categories(fs)
    matches: List[Tuple[str, str]] = []
    for rel in fs.list_files(""):
        parts = rel.split("/")
        if len(parts) >= 3 and parts[0] in cats and parts[1] in STATE_BUCKETS and rel.endswith(f"/{ticket_id}.md"):
            matches.append((parts[1], rel))
    if not matches:
        raise VolumeError("E_TICKET_NOT_FOUND", f"未找到工单: {ticket_id}", exit_code=1)
    if len(matches) > 1:
        raise VolumeError("E_TICKET_COLLISION", f"发现重复工单副本，需人工核对: {ticket_id}", exit_code=1)
    bucket, rel = matches[0]
    raw = fs.read_text(rel)
    meta, body = parse_ticket(raw)
    return bucket, rel, meta, body, _sha256_text(raw)


def _bucket_for_status(status: str, archived: bool = False) -> str:
    if status in ACTIVE_STATUSES:
        return "active"
    if status == "paused":
        return "paused"
    if status in CLOSED_STATUSES:
        return "archive" if archived else "closed"
    raise VolumeError("E_TICKET_CORRUPT", f"未知工单状态: {status}", exit_code=1)


def _append_stream(body: str, actor: str, action: str, detail: str, now_iso: Optional[str] = None) -> str:
    ts = now_iso or _now_iso()
    entry = f"\n### {ts} · {actor} · {action}\n\n{detail.strip()}\n"
    if "## 6 对话流" not in body and "## 6. 对话流" not in body:
        body = body.rstrip() + "\n\n## 6 对话流\n"
    return body.rstrip() + "\n" + entry


def _build_body(template: str, title: str, actor: str, content: str, now_iso: str) -> str:
    note = content.strip() or f"由 {actor} 立项创建：{title}"
    if template == "structured_5sec":
        return (
            f"# {title}\n\n"
            "## 1 断点\n\n"
            f"- [ ] [L1] {note}\n\n"
            "## 2 锚点\n\n"
            "- 待补充受影响文件与位置\n\n"
            "## 3 现场\n\n"
            "- 待记录执行环境与基线验证命令\n\n"
            "## 4 接手任务\n\n"
            "- 按本票 §1 断点核对后推进\n\n"
            "## 5 告知信息\n\n"
            "- 遵守本分类权限与审阅门禁约束\n\n"
            "## 6 对话流\n\n"
            f"### {now_iso} · {actor} · 建单立项\n\n"
            f"{note}\n"
        )
    if template == "light_msg":
        return (
            f"# {title}\n\n"
            "## 1 消息目标\n\n"
            f"{note}\n\n"
            "## 2 关联与下一步\n\n"
            "- 待接收方回复或确认；不授予系统修改范围\n\n"
            "## 6 对话流\n\n"
            f"### {now_iso} · {actor} · 发起通知\n\n"
            f"{note}\n"
        )
    return (
        f"# {title}\n\n"
        "## 1 目标与清单\n\n"
        f"- [ ] {note}\n\n"
        "## 2 关联说明与产物\n\n"
        "- 待补充输入输出与验收说明\n\n"
        "## 6 对话流\n\n"
        f"### {now_iso} · {actor} · 建单立项\n\n"
        f"{note}\n"
    )


def _complete_l1_tasks(body: str) -> str:
    """Tick completed L1 checklist items inside `## 1` while leaving L0/L2 gates untouched."""
    lines = body.splitlines(keepends=True)
    out: List[str] = []
    in_sec1 = False
    in_fence = False
    sec1_re = re.compile(r"^##[ \t]+1(?:[ .、．]|$)")
    other_h2_re = re.compile(r"^##[ \t]+")
    for ln in lines:
        stripped = ln.strip()
        if sec1_re.match(ln):
            in_sec1 = True
            in_fence = False
            out.append(ln)
            continue
        if in_sec1 and other_h2_re.match(ln):
            in_sec1 = False
        if in_sec1:
            if stripped.startswith("```"):
                in_fence = not in_fence
            elif not in_fence:
                m = _TASK_LINE_RE.match(ln.rstrip("\r\n"))
                if m and m.group(1) == " " and (m.group(2) or "L1").upper() == "L1":
                    ln = ln.replace("[ ]", "[x]", 1)
        out.append(ln)
    return "".join(out)


def _check_structured_done(meta: Dict[str, Any], body: str) -> None:
    sec1_match = re.findall(r"^##[ \t]+1(?:[ .、．][^\n]*)?\n(.*?)(?=^##[ \t]|\Z)", body, re.M | re.S)
    if len(sec1_match) != 1:
        raise VolumeError("E_DONE_GATE_BLOCKED", "结构化工程工单须有且仅有一个 `## 1 断点` 小节", exit_code=1)
    sec1 = sec1_match[0]
    in_fence = False
    human_levels = {str(x).upper() for x in (meta.get("human_levels") or [])}
    l0_open, l1_open, l2_open_unauth, total_tasks = 0, 0, 0, 0
    for line in sec1.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _TASK_LINE_RE.match(line)
        if not m:
            continue
        total_tasks += 1
        checked = m.group(1).lower() == "x"
        level = (m.group(2) or "L1").upper()
        if not checked:
            if level == "L0":
                l0_open += 1
            elif level == "L2" and "L2" in human_levels:
                continue
            elif level == "L2":
                l2_open_unauth += 1
            else:
                l1_open += 1
    if total_tasks == 0 and "无任务理由：" not in sec1:
        raise VolumeError("E_DONE_GATE_BLOCKED", "§1 断点未发现任务项且缺少 `无任务理由：` 说明", exit_code=1)
    if l0_open or l1_open or l2_open_unauth:
        raise VolumeError(
            "E_DONE_GATE_BLOCKED",
            f"存在未完成任务项（L0={l0_open}, L1={l1_open}, 未授凭据L2={l2_open_unauth}），禁止结单",
            exit_code=1,
        )
    sec6_match = re.search(r"^##[ \t]+6(?:[ .、．][^\n]*)?\n(.*)$", body, re.M | re.S)
    if not sec6_match or "完成反馈" not in sec6_match.group(1):
        raise VolumeError("E_DONE_GATE_BLOCKED", "结单须在 §6 对话流包含非空「完成反馈」记录", exit_code=1)


def _refresh_index(fs: VolumeFS) -> None:
    cats = load_categories(fs)
    active_rows: List[str] = []
    paused_rows: List[str] = []
    closed_rows: List[str] = []
    for rel in fs.list_files(""):
        parts = rel.split("/")
        if len(parts) < 3 or parts[0] not in cats or parts[1] not in STATE_BUCKETS or not rel.endswith(".md"):
            continue
        bucket = parts[1]
        if bucket == "archive":
            continue
        meta, _ = parse_ticket(fs.read_text(rel))
        tid = str(meta.get("ticket_id", ""))
        cat = str(meta.get("category", parts[0]))
        st = str(meta.get("status", ""))
        title = str(meta.get("title", "")).replace("|", "/")
        upd = str(meta.get("updated", ""))
        if bucket == "active":
            rev = f"pre:{meta.get('pre_review_status', 'not_required')}"
            picked = str(meta.get("picked_by") or "-")
            active_rows.append(f"| {tid} | {cat} | {st} | {rev} | {picked} | {title} | {upd} |")
        elif bucket == "paused":
            reason = str(meta.get("pause_reason") or "").replace("|", "/")
            rwhen = str(meta.get("resume_when") or "").replace("|", "/")
            paused_rows.append(f"| {tid} | {cat} | {reason} | {rwhen} | {upd} |")
        elif bucket == "closed":
            post_rev = str(meta.get("post_review_status", "not_required"))
            closed_at = str(meta.get("closed_at") or upd)
            closed_rows.append(f"| {tid} | {cat} | {st} | {post_rev} | {closed_at} | {title} |")

    content = (
        "# 格物工单协同台账\n\n"
        "## 在途工单 (active: open / in_progress)\n\n"
        "| 单号 | 分类 | 主状态 | 审阅状态 | 接手方 | 标题 | 更新时间 |\n"
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n"
        + ("\n".join(active_rows) + "\n" if active_rows else "")
        + "\n## 暂停工单 (paused)\n\n"
        "| 单号 | 分类 | 暂停原因 | 恢复条件 | 更新时间 |\n"
        "| :--- | :--- | :--- | :--- | :--- |\n"
        + ("\n".join(paused_rows) + "\n" if paused_rows else "")
        + "\n## 近期结单与待完工审阅 (closed: 7天内)\n\n"
        "| 单号 | 分类 | 终态 | 完工审阅 | 结单时间 | 标题 |\n"
        "| :--- | :--- | :--- | :--- | :--- | :--- |\n"
        + ("\n".join(closed_rows) + "\n" if closed_rows else "")
    )
    fs.write_text("INDEX.md", content)


def _sweep_archive_unlocked(fs: VolumeFS, now_dt: Optional[datetime] = None) -> Dict[str, Any]:
    now_val = now_dt or _now()
    cats = load_categories(fs)
    moved: List[str] = []
    held_unreviewed: List[str] = []
    purged_from_archive: List[str] = []
    for cid, policy in cats.items():
        after_days = int(policy.get("archive_after_days", 7))
        purge_days = int(policy.get("purge_archive_after_days", 30))
        hold_unrev = bool(policy.get("hold_archive_if_post_unreviewed", True))
        prefix = f"{cid}/closed"
        for rel in fs.list_files(prefix):
            if not rel.endswith(".md"):
                continue
            raw = fs.read_text(rel)
            meta, body = parse_ticket(raw)
            closed_raw = str(meta.get("closed_at") or meta.get("updated") or "")
            if not closed_raw:
                continue
            try:
                closed_dt = datetime.fromisoformat(closed_raw)
                if closed_dt.tzinfo is None:
                    closed_dt = closed_dt.replace(tzinfo=now_val.tzinfo)
            except ValueError:
                continue
            if now_val - closed_dt <= timedelta(days=after_days):
                continue
            if meta.get("post_review_status") == "pending_review" and hold_unrev:
                held_unreviewed.append(str(meta.get("ticket_id")))
                continue
            tid = str(meta.get("ticket_id"))
            meta["archived"] = True
            meta["archived_at"] = now_val.isoformat(timespec="seconds")
            meta["updated"] = meta["archived_at"]
            new_body = _append_stream(body, "VolumeSweeper", "7天冷归档", f"超过 {after_days} 天自动迁入 `{cid}/archive/`。", now_val.isoformat(timespec="seconds"))
            new_rel = f"{cid}/archive/{tid}.md"
            fs.ensure_dir(f"{cid}/archive")
            fs.write_text(new_rel, dump_ticket(meta, new_body))
            fs.remove_file(rel)
            moved.append(tid)

        # 清理 archive/ 中结单（closed_at）超过 30 天的历史归档工单
        arch_prefix = f"{cid}/archive"
        for rel in fs.list_files(arch_prefix):
            if not rel.endswith(".md"):
                continue
            raw = fs.read_text(rel)
            meta, _ = parse_ticket(raw)
            closed_raw = str(meta.get("closed_at") or meta.get("archived_at") or meta.get("updated") or "")
            if not closed_raw:
                continue
            try:
                closed_dt = datetime.fromisoformat(closed_raw)
                if closed_dt.tzinfo is None:
                    closed_dt = closed_dt.replace(tzinfo=now_val.tzinfo)
            except ValueError:
                continue
            if now_val - closed_dt <= timedelta(days=purge_days):
                continue
            if meta.get("post_review_status") == "pending_review" and hold_unrev:
                continue
            tid = str(meta.get("ticket_id"))
            fs.remove_file(rel)
            purged_from_archive.append(tid)
    return {
        "moved_to_archive": moved,
        "held_unreviewed": held_unreviewed,
        "purged_from_archive": purged_from_archive,
    }


def op_out(
    fs: VolumeFS,
    *,
    category: str,
    created_by: str,
    title: str,
    to_agent: str = "all",
    project_id: str = "",
    need_human: bool = False,
    write_scope: Optional[List[str]] = None,
    content: str = "",
    key: str = "",
) -> Dict[str, Any]:
    actor = _ident_ok(created_by, "建单方")
    to_val = _ident_ok(to_agent or "all", "接收方")
    title_clean = re.sub(r"[\r\n]+", " ", str(title)).strip()
    if not title_clean:
        raise VolumeError("E_PARAM_MISSING", "建单必须提供非空 --title", exit_code=2)
    cats = load_categories(fs)
    if category not in cats:
        raise VolumeError("E_CATEGORY_UNKNOWN", f"未注册分类 '{category}'，请先 register-category", exit_code=1)
    policy = cats[category]
    scopes = [s.strip() for s in (write_scope or []) if s.strip()]
    if scopes and not policy.get("allow_write_scope", False):
        raise VolumeError("E_POLICY_FORBIDDEN", f"分类 '{category}' 策略禁止声明 write_scope", exit_code=1)

    now_dt = _now()
    now_iso = now_dt.isoformat(timespec="seconds")
    base_slug = _slug(actor)
    if key.strip():
        base_slug = f"{base_slug}-{_slug(key)}"
    request_hash = _sha256_text(json.dumps({"title": title_clean, "to": to_val,
        "project": project_id.strip(), "need_human": need_human, "scope": scopes,
        "content": content}, ensure_ascii=False, sort_keys=True))
    with fs.lock():
        if key.strip():
            for bucket in ("active", "paused"):
                for rel in fs.list_files(f"{category}/{bucket}"):
                    if not rel.endswith(".md"):
                        continue
                    text = fs.read_text(rel)
                    prior, _ = parse_ticket(text)
                    if prior.get("created_by") == actor and prior.get("request_key") == key.strip():
                        if prior.get("request_hash") != request_hash:
                            raise VolumeError("E_KEY_CONFLICT", "同一在途 key 已用于不同内容，请查原票", 1)
                        return {"ticket_id": prior["ticket_id"], "category": category,
                            "status": prior["status"], "bucket": bucket, "reused": True,
                            "pre_review_status": prior.get("pre_review_status"),
                            "post_review_status": prior.get("post_review_status"),
                            "approval_sha256": _sha256_text(text)}
        ts = now_dt.strftime("%Y%m%dT%H%M")
        tid = f"TICKET-{ts}-{category}-{base_slug}"
        suffix = 1
        while any(fs.exists(f"{category}/{b}/{tid}.md") for b in STATE_BUCKETS):
            suffix += 1
            tid = f"TICKET-{ts}-{category}-{base_slug}-{suffix}"
        pre_gate = policy.get("pre_review_gate", "none")
        pre_status = "pending" if (need_human or pre_gate in ("before_pickup", "before_done", "both")) else "not_required"
        meta: Dict[str, Any] = {
            "request_key": key.strip(),
            "request_hash": request_hash,
            "doc_type": "ticket",
            "ticket_id": tid,
            "category": category,
            "status": "open",
            "pre_review_status": pre_status,
            "post_review_status": "not_required",
            "need_human": bool(need_human or pre_gate in ("before_pickup", "both")),
            "human_levels": [],
            "title": title_clean,
            "summary": title_clean[:120],
            "created_by": actor,
            "to_agent": to_val,
            "picked_by": "",
            "picked_at": "",
            "project_id": project_id.strip(),
            "write_scope": scopes,
            "archived": False,
            "created_at": now_iso,
            "updated": now_iso,
        }
        body = _build_body(policy["template"], title_clean, actor, content, now_iso)
        rel = f"{category}/active/{tid}.md"
        fs.ensure_dir(f"{category}/active")
        text = dump_ticket(meta, body)
        fs.write_text(rel, text)
        _refresh_index(fs)
    return {
        "ticket_id": tid,
        "category": category,
        "status": "open",
        "pre_review_status": pre_status,
        "post_review_status": "not_required",
        "bucket": "active",
        "approval_sha256": _sha256_text(text),
    }


def _slice_ticket_lines(
    text: str,
    *,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> Dict[str, Any]:
    lines_keepends = text.splitlines(keepends=True)
    total_lines = len(lines_keepends)
    if start_line is not None and start_line < 1:
        raise VolumeError("E_PARAM_INVALID", "--start-line 必须 >= 1", exit_code=2)
    if end_line is not None and end_line < 1:
        raise VolumeError("E_PARAM_INVALID", "--end-line 必须 >= 1", exit_code=2)
    if start_line is not None and end_line is not None and end_line < start_line:
        raise VolumeError("E_PARAM_INVALID", "--end-line 不能小于 --start-line", exit_code=2)
    if start_line is None and end_line is None:
        return {
            "raw_text": text,
            "total_lines": total_lines,
            "returned_start_line": 1 if total_lines else 0,
            "returned_end_line": total_lines,
            "truncated": False,
        }
    s_idx = start_line if start_line is not None else 1
    e_idx = min(end_line, total_lines) if end_line is not None else total_lines
    if total_lines == 0 or s_idx > total_lines:
        return {
            "raw_text": "",
            "total_lines": total_lines,
            "returned_start_line": 0,
            "returned_end_line": 0,
            "truncated": total_lines > 0,
        }
    return {
        "raw_text": "".join(lines_keepends[s_idx - 1 : e_idx]),
        "total_lines": total_lines,
        "returned_start_line": s_idx,
        "returned_end_line": e_idx,
        "truncated": s_idx > 1 or e_idx < total_lines,
    }


def op_in(
    fs: VolumeFS,
    *,
    ticket_id: str,
    full: bool = False,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> Dict[str, Any]:
    bucket, rel, meta, body, sha = _locate_ticket(fs, ticket_id)
    out: Dict[str, Any] = {
        "ticket_id": ticket_id,
        "category": meta.get("category"),
        "status": meta.get("status"),
        "bucket": bucket,
        "pre_review_status": meta.get("pre_review_status", "not_required"),
        "post_review_status": meta.get("post_review_status", "not_required"),
        "need_human": bool(meta.get("need_human", False)),
        "created_by": meta.get("created_by"),
        "to_agent": meta.get("to_agent"),
        "picked_by": meta.get("picked_by"),
        "title": meta.get("title"),
        "approval_sha256": sha,
        "rel_path": rel,
        "body_preview": body[:600],
        "body_truncated": len(body) > 600,
    }
    if full or start_line is not None or end_line is not None:
        raw_text = fs.read_text(rel)
        sliced = _slice_ticket_lines(raw_text, start_line=start_line, end_line=end_line)
        out["body"] = body
        out["size_bytes"] = len(raw_text.encode("utf-8"))
        out.update(sliced)
    return out


def op_list(
    fs: VolumeFS,
    *,
    category: Optional[str] = None,
    status: Optional[str] = None,
    pre_review_status: Optional[str] = None,
    post_review_status: Optional[str] = None,
    archived: Optional[bool] = None,
    query: str = "",
) -> Dict[str, Any]:
    cats = load_categories(fs)
    items: List[Dict[str, Any]] = []
    q = query.strip().lower()
    for rel in fs.list_files(""):
        parts = rel.split("/")
        if len(parts) < 3 or parts[0] not in cats or parts[1] not in STATE_BUCKETS or not rel.endswith(".md"):
            continue
        bucket = parts[1]
        raw = fs.read_text(rel)
        meta, body = parse_ticket(raw)
        if category and meta.get("category") != category:
            continue
        if status and meta.get("status") != status:
            continue
        if pre_review_status and meta.get("pre_review_status") != pre_review_status:
            continue
        if post_review_status and meta.get("post_review_status") != post_review_status:
            continue
        is_arch = bucket == "archive" or bool(meta.get("archived", False))
        if archived is not None and is_arch != archived:
            continue
        if q and q not in (str(meta.get("ticket_id", "")) + " " + str(meta.get("title", "")) + " " + body).lower():
            continue
        items.append(
            {
                "ticket_id": meta.get("ticket_id"),
                "category": meta.get("category"),
                "status": meta.get("status"),
                "bucket": bucket,
                "pre_review_status": meta.get("pre_review_status", "not_required"),
                "post_review_status": meta.get("post_review_status", "not_required"),
                "picked_by": meta.get("picked_by", ""),
                "title": meta.get("title", ""),
                "updated": meta.get("updated", ""),
            }
        )
    return {"count": len(items), "tickets": items}


def op_pickup(fs: VolumeFS, *, ticket_id: str, by: str, force: bool = False) -> Dict[str, Any]:
    actor = _ident_ok(by, "接单方")
    with fs.lock():
        bucket, rel, meta, body, _ = _locate_ticket(fs, ticket_id)
        if bucket != "active" or meta.get("status") != "open":
            raise VolumeError("E_TICKET_STATE", f"仅 active/open 工单可认领，当前为 {bucket}/{meta.get('status')}", exit_code=1)
        cats = load_categories(fs)
        policy = cats.get(str(meta.get("category")), {})
        pre_gate = policy.get("pre_review_gate", "none")
        pre_st = meta.get("pre_review_status", "not_required")
        if (meta.get("need_human") is True or pre_gate in ("before_pickup", "both")) and pre_st != "approved":
            raise VolumeError(
                "E_PRE_REVIEW_REQUIRED",
                f"阻塞继续前审阅未通过（pre_review_status={pre_st}），请先请人类执行 review --stage pre --outcome approve",
                exit_code=1,
            )
        to_agent = str(meta.get("to_agent") or "all")
        if to_agent != "all" and to_agent != actor and not force:
            raise VolumeError("E_TARGET_MISMATCH", f"该工单定向派单给 '{to_agent}'，非 '{actor}'", exit_code=1)
        now_iso = _now_iso()
        meta["status"] = "in_progress"
        meta["picked_by"] = actor
        meta["picked_at"] = now_iso
        meta["updated"] = now_iso
        new_body = _append_stream(body, actor, "接单开始", "认领锁定工单进入 in_progress。", now_iso)
        fs.write_text(rel, dump_ticket(meta, new_body))
        _refresh_index(fs)
    return {"ticket_id": ticket_id, "status": "in_progress", "picked_by": actor, "bucket": "active"}


def op_reply(fs: VolumeFS, *, ticket_id: str, by: str, note: str, complete_l1: bool = False) -> Dict[str, Any]:
    actor = _ident_ok(by, "回复方")
    if not str(note).strip():
        raise VolumeError("E_PARAM_MISSING", "reply 必须提供非空 --note", exit_code=2)
    with fs.lock():
        bucket, rel, meta, body, _ = _locate_ticket(fs, ticket_id)
        if bucket != "active":
            raise VolumeError("E_TICKET_STATE", f"仅在途(active)工单可回复，当前位于 {bucket}", exit_code=1)
        now_iso = _now_iso()
        meta["updated"] = now_iso
        updated_body = _complete_l1_tasks(body) if complete_l1 else body
        new_body = _append_stream(updated_body, actor, "多轮回复", note, now_iso)
        fs.write_text(rel, dump_ticket(meta, new_body))
        _refresh_index(fs)
    return {"ticket_id": ticket_id, "status": meta["status"], "replied_by": actor, "completed_l1": bool(complete_l1)}


def op_pause(
    fs: VolumeFS,
    *,
    ticket_id: str,
    by: str,
    reason: str,
    resume_when: str,
    expected_sha256: str = "",
    force: bool = False,
) -> Dict[str, Any]:
    actor = _ident_ok(by, "暂停方")
    if not str(reason).strip() or not str(resume_when).strip():
        raise VolumeError("E_PARAM_MISSING", "pause 必须提供暂停原因(note/reason)与恢复条件(resume_when)", exit_code=2)
    with fs.lock():
        bucket, rel, meta, body, sha = _locate_ticket(fs, ticket_id)
        if expected_sha256 and expected_sha256 != sha:
            raise VolumeError("E_VERSION_MISMATCH", "原票版本哈希已变更，拒绝基于过期版本暂停", exit_code=1)
        if bucket != "active" or meta.get("status") not in ACTIVE_STATUSES:
            raise VolumeError("E_TICKET_STATE", f"仅 active 工单可暂停，当前为 {bucket}/{meta.get('status')}", exit_code=1)
        if meta.get("status") == "in_progress" and not force and actor not in (meta.get("picked_by"), meta.get("created_by")):
            raise VolumeError(
                "E_ACTOR_MISMATCH",
                f"仅建单方 '{meta.get('created_by')}'、当前接手方 '{meta.get('picked_by')}' 或人类 force=true 可暂停进行中工单",
                exit_code=1,
            )
        cat = str(meta["category"])
        now_iso = _now_iso()
        meta["status"] = "paused"
        meta["picked_by"] = ""
        meta["picked_at"] = ""
        meta.pop("dev_by", None)
        meta.pop("dev_scope_sha256", None)
        meta.pop("dev_approved_sha256", None)
        meta["pause_reason"] = reason.strip()
        meta["resume_when"] = resume_when.strip()
        meta["paused_by"] = actor
        meta["paused_at"] = now_iso
        meta["updated"] = now_iso
        new_body = _append_stream(body, actor, "暂缓搁置", f"原因：{reason.strip()}；恢复条件：{resume_when.strip()}", now_iso)
        new_rel = f"{cat}/paused/{ticket_id}.md"
        fs.ensure_dir(f"{cat}/paused")
        fs.write_text(new_rel, dump_ticket(meta, new_body))
        fs.remove_file(rel)
        _refresh_index(fs)
    return {"ticket_id": ticket_id, "status": "paused", "bucket": "paused"}


def op_resume(
    fs: VolumeFS,
    *,
    ticket_id: str,
    by: str,
    note: str,
    expected_sha256: str = "",
) -> Dict[str, Any]:
    actor = _ident_ok(by, "恢复方")
    if not str(note).strip():
        raise VolumeError("E_PARAM_MISSING", "resume 必须提供恢复条件落实说明 --note", exit_code=2)
    with fs.lock():
        bucket, rel, meta, body, sha = _locate_ticket(fs, ticket_id)
        if expected_sha256 and expected_sha256 != sha:
            raise VolumeError("E_VERSION_MISMATCH", "原票版本哈希已变更，拒绝恢复", exit_code=1)
        if bucket != "paused" or meta.get("status") != "paused":
            raise VolumeError("E_TICKET_STATE", f"仅 paused 工单可恢复，当前为 {bucket}/{meta.get('status')}", exit_code=1)
        cat = str(meta["category"])
        cats = load_categories(fs)
        pre_gate = cats.get(cat, {}).get("pre_review_gate", "none")
        now_iso = _now_iso()
        meta["status"] = "open"
        for pkey in ("pause_reason", "resume_when", "paused_by", "paused_at"):
            meta.pop(pkey, None)
        recheck_hint = ""
        if pre_gate in ("before_pickup", "before_done", "both"):
            meta["pre_review_status"] = "pending"
            meta["need_human"] = bool(pre_gate in ("before_pickup", "both"))
            recheck_hint = "；已重置前置审阅为 pending，须重新人类审批"
        meta["updated"] = now_iso
        new_body = _append_stream(body, actor, "恢复在途", f"落实依据：{note.strip()}（回退 open{recheck_hint}，须重新接单）", now_iso)
        new_rel = f"{cat}/active/{ticket_id}.md"
        fs.ensure_dir(f"{cat}/active")
        fs.write_text(new_rel, dump_ticket(meta, new_body))
        fs.remove_file(rel)
        _refresh_index(fs)
    return {
        "ticket_id": ticket_id,
        "status": "open",
        "bucket": "active",
        "pre_review_status": meta.get("pre_review_status", "not_required"),
    }


def op_abort(fs: VolumeFS, *, ticket_id: str, by: str, force: bool = False) -> Dict[str, Any]:
    actor = _ident_ok(by, "执行方")
    with fs.lock():
        bucket, rel, meta, body, _ = _locate_ticket(fs, ticket_id)
        if bucket != "active" or meta.get("status") != "in_progress":
            raise VolumeError("E_TICKET_STATE", "仅 in_progress 工单可执行 abort 退回 open", exit_code=1)
        if not force and meta.get("picked_by") != actor:
            raise VolumeError("E_ACTOR_MISMATCH", f"仅当前接手方 '{meta.get('picked_by')}' 可弃权；死锁由人类用 force=true 解除", exit_code=1)
        now_iso = _now_iso()
        meta["status"] = "open"
        meta["picked_by"] = ""
        meta["picked_at"] = ""
        meta.pop("dev_by", None)
        meta.pop("dev_scope_sha256", None)
        meta.pop("dev_approved_sha256", None)
        meta["updated"] = now_iso
        tag = "人类强制解除锁定 [HUMAN FORCE ABORT]" if force else "主动弃权交回"
        new_body = _append_stream(body, actor, tag, "清空接手人并回退至 open。", now_iso)
        fs.write_text(rel, dump_ticket(meta, new_body))
        _refresh_index(fs)
    return {"ticket_id": ticket_id, "status": "open", "bucket": "active"}


def op_cancel(fs: VolumeFS, *, ticket_id: str, by: str, note: str = "", force: bool = False) -> Dict[str, Any]:
    actor = _ident_ok(by, "取消方")
    with fs.lock():
        bucket, rel, meta, body, _ = _locate_ticket(fs, ticket_id)
        if bucket != "active" or meta.get("status") not in ACTIVE_STATUSES:
            raise VolumeError("E_TICKET_STATE", f"仅 active 工单可取消，当前为 {bucket}/{meta.get('status')}", exit_code=1)
        if not force and meta.get("created_by") != actor and meta.get("picked_by") != actor:
            raise VolumeError("E_ACTOR_MISMATCH", "仅建单方、当前接手方或人类 force=true 可取消工单", exit_code=1)
        cat = str(meta["category"])
        now_iso = _now_iso()
        meta["status"] = "cancelled"
        meta["picked_by"] = ""
        meta["picked_at"] = ""
        meta.pop("dev_by", None)
        meta.pop("dev_scope_sha256", None)
        meta.pop("dev_approved_sha256", None)
        meta["closed_at"] = now_iso
        meta["archived"] = False
        meta["updated"] = now_iso
        new_body = _append_stream(body, actor, "取消关闭", note.strip() or "不再推进，按 cancelled 迁入 closed/。", now_iso)
        new_rel = f"{cat}/closed/{ticket_id}.md"
        fs.ensure_dir(f"{cat}/closed")
        fs.write_text(new_rel, dump_ticket(meta, new_body))
        fs.remove_file(rel)
        sweep = _sweep_archive_unlocked(fs)
        _refresh_index(fs)
    return {"ticket_id": ticket_id, "status": "cancelled", "bucket": "closed", "archive_sweep": sweep}


def op_review(
    fs: VolumeFS,
    *,
    ticket_id: str,
    stage: str,
    outcome: str,
    by: str,
    expected_sha256: str,
    note: str,
    levels: Optional[List[str]] = None,
    reopen_on_reject: bool = False,
) -> Dict[str, Any]:
    actor = _ident_ok(by, "人类审阅者")
    if stage not in ("pre", "post"):
        raise VolumeError("E_PARAM_INVALID", "review stage 须为 pre 或 post", exit_code=2)
    if outcome not in ("approve", "reject"):
        raise VolumeError("E_PARAM_INVALID", "review outcome 须为 approve 或 reject", exit_code=2)
    if not str(expected_sha256).strip():
        raise VolumeError("E_PARAM_MISSING", "审阅必须提供 --expected-sha256 防重放校验原票版本", exit_code=2)
    if not str(note).strip():
        raise VolumeError("E_PARAM_MISSING", "审阅必须提供明确 --note 决定依据", exit_code=2)

    with fs.lock():
        bucket, rel, meta, body, sha = _locate_ticket(fs, ticket_id)
        if expected_sha256.strip() != sha:
            raise VolumeError("E_VERSION_MISMATCH", "原票字节哈希已变化（或传入过期哈希），拒绝审阅转录", exit_code=1)
        cat = str(meta["category"])
        now_iso = _now_iso()

        if stage == "pre":
            if bucket not in ("active", "paused"):
                raise VolumeError("E_TICKET_STATE", f"仅 active/paused 工单可办理前置审阅，当前位于 {bucket}", exit_code=1)
            if outcome == "approve":
                meta["pre_review_status"] = "approved"
                meta["need_human"] = False
                if levels:
                    cur = {str(x).upper() for x in (meta.get("human_levels") or [])}
                    for lv in levels:
                        if lv.upper() in ("L0", "L2"):
                            cur.add(lv.upper())
                    meta["human_levels"] = sorted(cur)
                tag = "人类预审通过 [HUMAN PRE-APPROVE]"
            else:
                meta["pre_review_status"] = "rejected"
                meta["need_human"] = True
                tag = "人类预审驳回 [HUMAN PRE-REJECT]"
            meta["updated"] = now_iso
            new_body = _append_stream(body, actor, tag, note, now_iso)
            fs.write_text(rel, dump_ticket(meta, new_body))
            _refresh_index(fs)
            return {
                "ticket_id": ticket_id,
                "stage": "pre",
                "pre_review_status": meta["pre_review_status"],
                "need_human": meta["need_human"],
            }

        # Post-completion asynchronous review (does not block normal task flow, works in closed/ or archive/)
        if meta.get("status") != "done":
            raise VolumeError("E_TICKET_STATE", f"仅 done 状态工单可办理完工后审阅，当前为 {meta.get('status')}", exit_code=1)
        if outcome == "approve":
            meta["post_review_status"] = "approved"
            meta["updated"] = now_iso
            new_body = _append_stream(body, actor, "人类完工审阅通过 [HUMAN POST-APPROVE]", note, now_iso)
            fs.write_text(rel, dump_ticket(meta, new_body))
            _refresh_index(fs)
            return {"ticket_id": ticket_id, "stage": "post", "post_review_status": "approved", "bucket": bucket}

        meta["post_review_status"] = "changes_requested"
        meta["updated"] = now_iso
        if reopen_on_reject:
            meta["status"] = "open"
            meta["picked_by"] = ""
            meta["picked_at"] = ""
            meta["archived"] = False
            for stale_key in ("archived_at", "closed_at", "dev_by", "dev_scope_sha256", "dev_approved_sha256", "pause_reason", "resume_when", "paused_by", "paused_at"):
                meta.pop(stale_key, None)
            new_body = _append_stream(body, actor, "人类完工审阅退回返修 [HUMAN POST-REOPEN]", note, now_iso)
            new_rel = f"{cat}/active/{ticket_id}.md"
            fs.ensure_dir(f"{cat}/active")
            fs.write_text(new_rel, dump_ticket(meta, new_body))
            fs.remove_file(rel)
            _refresh_index(fs)
            return {"ticket_id": ticket_id, "stage": "post", "post_review_status": "changes_requested", "status": "open", "bucket": "active"}

        new_body = _append_stream(body, actor, "人类完工审阅批注需改 [HUMAN POST-CHANGES]", note, now_iso)
        fs.write_text(rel, dump_ticket(meta, new_body))
        _refresh_index(fs)
        return {"ticket_id": ticket_id, "stage": "post", "post_review_status": "changes_requested", "bucket": bucket}


def op_develop(
    fs: VolumeFS,
    *,
    ticket_id: str,
    by: str,
    expected_sha256: str,
    note: str,
    write_scope: Optional[List[str]] = None,
) -> Dict[str, Any]:
    actor = _ident_ok(by, "开发主体")
    if not str(expected_sha256).strip() or not str(note).strip():
        raise VolumeError("E_PARAM_MISSING", "develop 必须提供 --expected-sha256 与批准依据 --note", exit_code=2)
    with fs.lock():
        bucket, rel, meta, body, sha = _locate_ticket(fs, ticket_id)
        if expected_sha256.strip() != sha:
            raise VolumeError("E_VERSION_MISMATCH", "原票哈希已漂移，拒绝绑定开发授权", exit_code=1)
        if bucket != "active":
            raise VolumeError("E_TICKET_STATE", "仅 active 工单可授予开发权限", exit_code=1)
        cats = load_categories(fs)
        policy = cats.get(str(meta["category"]), {})
        if not policy.get("allow_write_scope", False):
            raise VolumeError("E_POLICY_FORBIDDEN", f"分类 '{meta['category']}' 不允许开发授权", exit_code=1)
        pre_gate = policy.get("pre_review_gate", "none")
        pre_st = meta.get("pre_review_status", "not_required")
        if (meta.get("need_human") is True or pre_gate in ("before_pickup", "both")) and pre_st != "approved":
            raise VolumeError(
                "E_PRE_REVIEW_REQUIRED",
                f"前置人类审阅未通过（pre_review_status={pre_st}），禁止授予开发范围",
                exit_code=1,
            )
        if write_scope:
            meta["write_scope"] = [s.strip() for s in write_scope if s.strip()]
        if not meta.get("write_scope"):
            raise VolumeError("E_PARAM_MISSING", "开发授权须声明非空 write_scope", exit_code=2)
        now_iso = _now_iso()
        meta["dev_by"] = actor
        meta["dev_scope_sha256"] = _scope_digest(meta, body)
        meta["dev_approved_sha256"] = sha
        meta["updated"] = now_iso
        new_body = _append_stream(body, actor, "开发权限转录 [HUMAN DEV GRANT]", note, now_iso)
        fs.write_text(rel, dump_ticket(meta, new_body))
        _refresh_index(fs)
    return {"ticket_id": ticket_id, "dev_by": actor, "dev_scope_sha256": meta["dev_scope_sha256"]}


def op_leave(fs: VolumeFS, *, ticket_id: str, by: str) -> Dict[str, Any]:
    actor = _ident_ok(by, "开发主体")
    with fs.lock():
        bucket, rel, meta, body, _ = _locate_ticket(fs, ticket_id)
        if bucket != "active":
            raise VolumeError("E_TICKET_STATE", "仅 active 工单可执行 leave", exit_code=1)
        now_iso = _now_iso()
        meta.pop("dev_by", None)
        meta.pop("dev_scope_sha256", None)
        meta.pop("dev_approved_sha256", None)
        meta["updated"] = now_iso
        new_body = _append_stream(body, actor, "退出开发授权", "已撤销本票开发凭据，断点保留。", now_iso)
        fs.write_text(rel, dump_ticket(meta, new_body))
        _refresh_index(fs)
    return {"ticket_id": ticket_id, "dev_granted": False}


def op_done(fs: VolumeFS, *, ticket_id: str, by: str, note: str = "", complete_l1: bool = False) -> Dict[str, Any]:
    actor = _ident_ok(by, "结单方")
    with fs.lock():
        bucket, rel, meta, body, _ = _locate_ticket(fs, ticket_id)
        if bucket != "active" or meta.get("status") != "in_progress":
            raise VolumeError("E_TICKET_STATE", f"仅 active/in_progress 工单可结单，当前为 {bucket}/{meta.get('status')}", exit_code=1)
        if meta.get("picked_by") != actor:
            raise VolumeError("E_ACTOR_MISMATCH", f"done 执行者 '{actor}' 须匹配当前接手方 '{meta.get('picked_by')}'", exit_code=1)
        cat = str(meta["category"])
        cats = load_categories(fs)
        policy = cats.get(cat, {})
        pre_gate = policy.get("pre_review_gate", "none")
        pre_st = meta.get("pre_review_status", "not_required")
        if (meta.get("need_human") is True or pre_gate in ("before_done", "both")) and pre_st != "approved":
            raise VolumeError(
                "E_PRE_REVIEW_REQUIRED",
                f"结单前阻塞审阅未通过（pre_review_status={pre_st}），禁止 done",
                exit_code=1,
            )

        now_iso = _now_iso()
        new_body = _complete_l1_tasks(body) if complete_l1 else body
        if note.strip():
            new_body = _append_stream(new_body, actor, "完成反馈", note, now_iso)
        if policy.get("template") == "structured_5sec":
            _check_structured_done(meta, new_body)

        post_policy = policy.get("post_review_policy", "none")
        post_status = "pending_review" if post_policy == "required" else "not_required"

        meta["status"] = "done"
        meta["post_review_status"] = post_status
        meta["closed_at"] = now_iso
        meta["archived"] = False
        meta.pop("dev_by", None)
        meta.pop("dev_scope_sha256", None)
        meta.pop("dev_approved_sha256", None)
        meta["updated"] = now_iso

        new_rel = f"{cat}/closed/{ticket_id}.md"
        fs.ensure_dir(f"{cat}/closed")
        fs.write_text(new_rel, dump_ticket(meta, new_body))
        fs.remove_file(rel)
        sweep = _sweep_archive_unlocked(fs)
        _refresh_index(fs)
    return {
        "ticket_id": ticket_id,
        "status": "done",
        "bucket": "closed",
        "post_review_status": post_status,
        "archive_sweep": sweep,
    }


def op_reopen(fs: VolumeFS, *, ticket_id: str, by: str, to_agent: str = "", note: str = "") -> Dict[str, Any]:
    actor = _ident_ok(by, "人类重开者")
    with fs.lock():
        bucket, rel, meta, body, _ = _locate_ticket(fs, ticket_id)
        if bucket not in ("closed", "archive") or meta.get("status") not in CLOSED_STATUSES:
            raise VolumeError("E_TICKET_STATE", f"仅 closed/archive 终态工单可重开，当前位于 {bucket}/{meta.get('status')}", exit_code=1)
        cat = str(meta["category"])
        cats = load_categories(fs)
        pre_gate = cats.get(cat, {}).get("pre_review_gate", "none")
        now_iso = _now_iso()
        meta["status"] = "open"
        meta["picked_by"] = ""
        meta["picked_at"] = ""
        meta["post_review_status"] = "not_required"
        if pre_gate in ("before_pickup", "before_done", "both"):
            meta["pre_review_status"] = "pending"
            meta["need_human"] = bool(pre_gate in ("before_pickup", "both"))
        for stale_key in ("dev_by", "dev_scope_sha256", "dev_approved_sha256", "pause_reason", "resume_when", "paused_by", "paused_at"):
            meta.pop(stale_key, None)
        meta["archived"] = False
        meta.pop("archived_at", None)
        meta.pop("closed_at", None)
        if to_agent.strip():
            meta["to_agent"] = _ident_ok(to_agent, "新指派方")
        meta["updated"] = now_iso
        new_body = _append_stream(body, actor, "重新激活 [HUMAN REOPEN]", note.strip() or "由人类从归档/结单区拉回 active/ 重新开放。", now_iso)
        new_rel = f"{cat}/active/{ticket_id}.md"
        fs.ensure_dir(f"{cat}/active")
        fs.write_text(new_rel, dump_ticket(meta, new_body))
        fs.remove_file(rel)
        _refresh_index(fs)
    return {"ticket_id": ticket_id, "status": "open", "bucket": "active"}


def op_archive_sweep(fs: VolumeFS) -> Dict[str, Any]:
    with fs.lock():
        res = _sweep_archive_unlocked(fs)
        _refresh_index(fs)
    return res
