"""PreToolUse hook guard blocking direct Agent probing of hidden core or .local state.

Supports Claude Code PreToolUse JSON stdin protocol and --self-test CLI mode.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROTECTED_PATH_PATTERNS = (
    re.compile(r"(^|[\\/])app_core([\\/]|$)"),
    re.compile(r"(^|[\\/])\.local([\\/]|$)"),
    re.compile(r"(^|[\\/])\.runtime([\\/]|$)"),
    re.compile(r"(^|[\\/])human-only([\\/]|$)"),
    re.compile(r"gewu_vault_exec\b"),
    re.compile(r"vault\.enc\b"),
    re.compile(r"\.vault_kek\b"),
    re.compile(r"kek\.key\b"),
    re.compile(r"\.enc\.db\b"),
)

DANGEROUS_BASH_PATTERNS = (
    re.compile(r"\bsqlite3\b"),
    re.compile(r"(^|[\s'\"=/\\])(app_core|\.local[\\/]state|\.local[\\/]logs|\.local[\\/]bin|\.runtime|human-only|gewu_vault_exec|vault\.enc|\.vault_kek|kek\.key|\.enc\.db)([\s'\"/\\]|$)"),
    re.compile(r"\b(strings|hexdump|xxd|od|lldb|gdb|dtrace)\b.*(\.enc|\.db|\.key|kek|gewu_vault_exec|audit\.log)"),
)

GIT_HISTORY_PROBE_PATTERNS = (
    # Targeting hidden core/state/key names anywhere in a git invocation (catches git log/show/diff/grep/ls-tree/checkout)
    re.compile(r"\bgit\b.*\b(app_core|\.local|\.runtime|human-only|gewu_vault_exec|vault\.enc|\.vault_kek|kek\.key|\.enc\.db)\b"),
    # Patch/pickaxe/full-diff history sweeps that bypass Read deny when core .py is in same primary repo
    re.compile(r"\bgit\b\s+(?:-[Cc]\s+\S+\s+)*log\b.*(?:\s-(?:p|S|G)\b|\s--patch\b)"),
    re.compile(r"\bgit\b\s+(?:-[Cc]\s+\S+\s+)*show\b"),
    re.compile(r"\bgit\b\s+(?:-[Cc]\s+\S+\s+)*cat-file\b"),
    re.compile(r"\bgit\b\s+(?:-[Cc]\s+\S+\s+)*stash\b\s+(?:show|apply|pop|branch|list)\b"),
    re.compile(r"\bgit\b\s+(?:-[Cc]\s+\S+\s+)*diff\b.*(?:\bHEAD\b|\bstash@|\.\.|~|\^|--cached)"),
    re.compile(r"\bgit\b\s+(?:-[Cc]\s+\S+\s+)*(?:rev-list|fsck)\b"),
)



ROOT_SPRAWL_PATH_PATTERNS = (
    re.compile(r"(^|[\\/])(reports|sources|99-Assets|[a-z0-9_-]+-research)([\\/]|$)", re.I),
)

ROOT_SPRAWL_BASH_PATTERNS = (
    re.compile(r"\bmkdir\b[^;\n|&]*\b(reports|sources|99-Assets|[a-z0-9_-]+-research)\b", re.I),
    re.compile(r"\b(curl|wget)\b[^;\n|&]*-(?:o|O)\s*(?:\./)?(reports|sources|[a-z0-9_-]+-research)[\\/]", re.I),
)

DOMAIN_SYSTEM_FILE_RE = re.compile(
    r"(^|[\\/])(GewuAtlas|GewuForge|GewuTicket|GewuLog)[\\/](AGENTS\.md|bootstrap\.py|gewu|(rules|contracts|skills|scripts|dicts|templates|tests)[\\/].*)"
)
ROOT_SYSTEM_FILE_RE = re.compile(r"^(?:\.[\\/])?(AGENTS\.md|gewu|bootstrap\.py)$")
WRITE_TOOL_NAMES = frozenset({
    "write",
    "write_file",
    "write_to_file",
    "edit",
    "edit_file",
    "replace_file_content",
    "multi_replace_file_content",
})


def _parse_sys_ticket_meta(path: Path) -> Dict[str, Any]:
    """Parse minimal YAML frontmatter keys from a GewuTicket markdown file without external dependencies."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    meta: Dict[str, Any] = {}
    cur_list_key: Optional[str] = None
    for line in lines[1:80]:
        s = line.strip()
        if s == "---":
            break
        if cur_list_key and s.startswith("- "):
            item = s[2:].strip().strip('"').strip("'")
            if item:
                meta[cur_list_key].append(item)
            continue
        cur_list_key = None
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip()
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            meta[k] = [x.strip().strip('"').strip("'") for x in inner.split(",") if x.strip()] if inner else []
        elif not v:
            meta[k] = []
            cur_list_key = k
        else:
            meta[k] = v.strip('"').strip("'")
    return meta


def _resolve_sys_ticket_root(target_val: str) -> Tuple[Optional[Path], str]:
    """Return (ws_root, rel_to_ws) when workspace mounts Volumes/GewuTicket-data/SYS, else (None, target_val)."""
    norm = target_val.replace("\\", "/")
    p = Path(target_val)
    if p.is_absolute():
        resolved = p.resolve()
        for anc in (resolved.parent, *resolved.parents):
            if (anc / "Volumes" / "GewuTicket-data" / "SYS").is_dir():
                try:
                    rel = resolved.relative_to(anc).as_posix()
                except ValueError:
                    rel = norm
                return anc, rel
        return None, norm
    cwd = Path.cwd().resolve()
    for anc in (cwd, *cwd.parents[:4]):
        if (anc / "Volumes" / "GewuTicket-data" / "SYS").is_dir():
            return anc, norm.lstrip("./")
    return None, norm


def _is_controlled_system_target(rel_path: str) -> bool:
    norm = rel_path.replace("\\", "/").lstrip("./")
    if "/usecase-for-agent/" in f"/{norm}" or norm.startswith("Volumes/"):
        return False
    return bool(DOMAIN_SYSTEM_FILE_RE.search(norm) or ROOT_SYSTEM_FILE_RE.match(norm))


def _has_approved_sys_ticket(ws_root: Path, rel_path: str) -> bool:
    active_dir = ws_root / "Volumes" / "GewuTicket-data" / "SYS" / "active"
    if not active_dir.is_dir():
        return False
    wanted_id = os.environ.get("GEWU_SYS_TICKET_ID", "").strip()
    norm_rel = rel_path.replace("\\", "/").lstrip("./").lower()
    domain_head = norm_rel.split("/", 1)[0] if "/" in norm_rel else ""
    for tf in sorted(active_dir.glob("*.md")):
        meta = _parse_sys_ticket_meta(tf)
        if str(meta.get("category", "")).upper() != "SYS":
            continue
        if str(meta.get("status", "")).lower() != "in_progress":
            continue
        if str(meta.get("pre_review_status", "")).lower() != "approved":
            continue
        if wanted_id and str(meta.get("ticket_id", "")).strip() != wanted_id:
            continue
        scope = meta.get("write_scope")
        if isinstance(scope, list) and scope:
            matched = False
            for raw_sc in scope:
                sc = str(raw_sc).replace("\\", "/").strip().strip("./").lower()
                if not sc:
                    continue
                if sc in ("*", "all") or norm_rel.startswith(sc) or sc.startswith(norm_rel) or (domain_head and sc == domain_head):
                    matched = True
                    break
            if not matched:
                continue
        return True
    return False


def inspect_tool_call(tool_name: str, tool_input: Dict[str, Any]) -> Optional[str]:
    """Return block reason if tool call targets protected core/state, Git history, or un-ticketed system edits."""
    name = tool_name.strip().lower()
    if name in ("read", "read_file", "view_file", "edit", "edit_file", "replace_file_content", "multi_replace_file_content", "write", "write_file", "write_to_file", "grep", "glob", "list_dir"):
        for key in ("file_path", "path", "Path", "TargetFile", "DirectoryPath", "SearchPath", "pattern"):
            val = str(tool_input.get(key, ""))
            for pat in PROTECTED_PATH_PATTERNS:
                if pat.search(val):
                    return f"禁止直接读取或检索受保护运行态路径（{key} 命中安全边界）"
    if name in ("bash", "run_command", "shell", "terminal"):
        cmd = str(tool_input.get("command", "") or tool_input.get("CommandLine", ""))
        for pat in GIT_HISTORY_PROBE_PATTERNS:
            if pat.search(cmd):
                return "禁止通过 Git 历史/对象/Stash 探查命令（git show/log -p/stash 等）绕过 Read deny"
        for pat in DANGEROUS_BASH_PATTERNS:
            if pat.search(cmd):
                return "禁止通过原生 Shell/sqlite3 直接探查隐藏核心或加密状态库"
    if name in WRITE_TOOL_NAMES:
        for key in ("file_path", "path", "Path", "TargetFile"):
            val = str(tool_input.get(key, ""))
            if not val:
                continue
            ws_root, rel_to_ws = _resolve_sys_ticket_root(val)
            if ws_root is not None and _is_controlled_system_target(rel_to_ws):
                if not _has_approved_sys_ticket(ws_root, rel_to_ws):
                    return (
                        f"修改受控系统文件（{rel_to_ws}）须先在 GewuTicket 开立 SYS 工单并获人类 pre 审批进入 in_progress："
                        "./gewu.command ticket --action out --volume ./Volumes/GewuTicket-data --category SYS --from <Agent> --key <task-key> --title \"<摘要>\" --write-scope \"<作用域>\""
                    )
            if not any(seg in val for seg in ("Volumes", "GewuAtlas", "GewuForge", "GewuTicket", "GewuTrack", "GewuLog")):
                for pat in ROOT_SPRAWL_PATH_PATTERNS:
                    if pat.search(val):
                        return "禁止在工作区根目录自建 reports/sources/*-research/99-Assets 等非受控目录；请写入受控工作台或数据卷"
    if name in ("bash", "run_command", "shell", "terminal"):
        cmd = str(tool_input.get("command", "") or tool_input.get("CommandLine", ""))
        for pat in ROOT_SPRAWL_BASH_PATTERNS:
            if pat.search(cmd):
                return "禁止在工作区根目录使用 mkdir/curl/wget 自建研究或下载目录；请通过域受控算子操作"
    return None


def _block_response(reason: str) -> Dict[str, Any]:
    reminder = (
        "<system-reminder>\n"
        "🛡️ 门禁熔断胶囊 (Guard Capsule)\n"
        f"❌ 中断拦截 (GUARD: HALTED | 禁止重试 | Exit Code: 2): {reason}\n"
        "🎯 行动指引 (ACTION): 立即停止探测底层文件，严格沿 SKILL.md 填空模板调用受控门面脚本。\n"
        "</system-reminder>"
    )
    return {"decision": "block", "reason": reminder}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test-tool", default="")
    parser.add_argument("--self-test-input", default="{}")
    args = parser.parse_args()

    try:
        if args.self_test_tool:
            payload = {"tool_name": args.self_test_tool, "tool_input": json.loads(args.self_test_input)}
        else:
            raw = sys.stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
        reason = inspect_tool_call(str(payload.get("tool_name", "")), payload.get("tool_input") or {})
        if reason:
            print(json.dumps(_block_response(reason), ensure_ascii=False))
            return 2
        print(json.dumps({"decision": "allow"}, ensure_ascii=False))
        return 0
    except Exception:  # noqa: BLE001
        print(json.dumps(_block_response("PreToolUse 钩子输入解析异常"), ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
