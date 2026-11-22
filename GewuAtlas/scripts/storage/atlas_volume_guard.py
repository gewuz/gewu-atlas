#!/usr/bin/env python3
"""Domain PreToolUse guard extending protection to GewuAtlas separated data volume."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_GUARD_DIR = str(Path(__file__).resolve().parents[1] / "guard")
if _GUARD_DIR not in sys.path:
    sys.path.insert(0, _GUARD_DIR)

from pre_tool_use_guard import (  # noqa: E402
    BASH_TOOL_NAMES,
    DANGEROUS_BASH_PATTERNS,
    GIT_HISTORY_PROBE_PATTERNS,
    PATH_INPUT_KEYS,
    READ_LIKE_TOOL_NAMES,
    ROOT_SPRAWL_BASH_PATTERNS,
    WRITE_PATH_KEYS,
    WRITE_TOOL_NAMES,
    WS_ROOT,
    _candidate_resolved_paths,
    is_protected_path_value,
    is_root_sprawl_write_value,
)

DATA_VOLUME_PATH_PATTERNS = (
    re.compile(r"Volumes[\\/]GewuAtlas-data", re.I),
    re.compile(r"GewuAtlas-data", re.I),
    re.compile(r"(^|[\\/])Volumes([\\/]|$)"),
)

DATA_VOLUME_BASH_PATTERNS = (
    re.compile(r"Volumes[\\/]GewuAtlas-data", re.I),
    re.compile(r"GewuAtlas-data", re.I),
    re.compile(
        r"\b(ls|ll|la|dir|find|tree|cd|cat|head|tail|less|more|sed|awk|grep|rg|rm|mv|cp|stat|du|chmod|python3?|sqlite3)\b[^;\n|&]*\bVolumes\b"
    ),
)

VOLUME_GIT_PROBE_PATTERNS = (
    re.compile(r"\bgit\b.*\b(GewuAtlas-data|Volumes)\b"),
)

_SAFE_GEWU_CMD_RE = re.compile(r"^\s*(?:python3\s+-I\s+-B\s+)?(?:\./)?gewu(?:\.command|\.cmd)?\s+")
_UNSAFE_SHELL_CONTROL_TOKENS = (";", "|", "&", "\n", "\r", "$(", "`", "<(", ">(")


def _is_data_volume_path_value(val: str) -> bool:
    if not val:
        return False
    for pat in DATA_VOLUME_PATH_PATTERNS:
        if pat.search(val):
            return True
    vol_roots = (
        (WS_ROOT / "Volumes").resolve(),
        (WS_ROOT / "Volumes" / "GewuAtlas-data").resolve(),
    )
    for res in _candidate_resolved_paths(val):
        if any(res == vr or res.is_relative_to(vr) for vr in vol_roots):
            return True
        if "GewuAtlas-data" in res.parts:
            return True
    return False


def inspect_tool_call(tool_name: str, tool_input: Dict[str, Any]) -> Optional[str]:
    name = tool_name.strip().lower()
    if name in READ_LIKE_TOOL_NAMES:
        for key in PATH_INPUT_KEYS:
            val = str(tool_input.get(key, ""))
            if not val:
                continue
            if is_protected_path_value(val):
                return f"禁止直接读取或检索受保护运行态路径（{key} 命中安全边界）"
            if _is_data_volume_path_value(val):
                return f"禁止绕过 ./gewu atlas 直接读取或枚举数据卷路径（{key} 命中隔离边界）"

    if name in BASH_TOOL_NAMES:
        cmd = str(tool_input.get("command", "") or tool_input.get("CommandLine", ""))
        for pat in (*GIT_HISTORY_PROBE_PATTERNS, *VOLUME_GIT_PROBE_PATTERNS):
            if pat.search(cmd):
                return "禁止通过 Git 历史/对象探查命令绕过数据卷或隐藏运行态隔离"
        for pat in DANGEROUS_BASH_PATTERNS:
            if pat.search(cmd):
                return "禁止通过原生 Shell/sqlite3 直接探查隐藏核心或 KEK 加密状态库"
        if not _SAFE_GEWU_CMD_RE.match(cmd) or any(ch in cmd for ch in _UNSAFE_SHELL_CONTROL_TOKENS):
            for pat in DATA_VOLUME_BASH_PATTERNS:
                if pat.search(cmd):
                    return "禁止使用原生 Shell 直接探测或操作数据卷目录，请经 ./gewu atlas --ref <vlt_01> 调用"
    if name in WRITE_TOOL_NAMES:
        for key in WRITE_PATH_KEYS:
            val = str(tool_input.get(key, ""))
            if is_root_sprawl_write_value(val):
                return "禁止在工作区根目录自建 reports/sources/*-research/99-Assets 等非受控目录；请通过 ./gewu atlas --action ingest 或 --action note-create --zone workbench 写入受控工作台"
    if name in BASH_TOOL_NAMES:
        cmd = str(tool_input.get("command", "") or tool_input.get("CommandLine", ""))
        for pat in ROOT_SPRAWL_BASH_PATTERNS:
            if pat.search(cmd):
                return "禁止在工作区根目录使用 mkdir/curl/wget 自建研究或下载目录；请通过 ./gewu atlas --action ingest --url ... 或 workbench-workspace 在受控空间内操作"
    return None


def _block_response(reason: str) -> Dict[str, Any]:
    reminder = (
        "<system-reminder>\n"
        "🛡️ 门禁熔断胶囊 (Guard Capsule)\n"
        f"❌ 中断拦截 (GUARD: HALTED | 禁止重试 | Exit Code: 2): {reason}\n"
        "🎯 行动指引 (ACTION): 严禁绕过 Skill 直接探测路径，请仅使用 ./gewu atlas --action ... --ref <vlt_01>。\n"
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
            sys.stdout.write(json.dumps(_block_response(reason), ensure_ascii=False) + "\n")
            return 2
        sys.stdout.write(json.dumps({"decision": "allow"}, ensure_ascii=False) + "\n")
        return 0
    except Exception:  # noqa: BLE001
        sys.stdout.write(json.dumps(_block_response("PreToolUse 钩子输入解析异常"), ensure_ascii=False) + "\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
