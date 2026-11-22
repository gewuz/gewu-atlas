#!/usr/bin/env python3
"""Optional zero-dependency stdio MCP Server (`./gewu atlas mcp`) sharing AtlasHotKernel.

Supports both newline-delimited JSON-RPC 2.0 (standard MCP stdio transport) and
`Content-Length:` framed JSON-RPC 2.0. Reuses the exact same `AtlasHotKernel` as
`AtlasHotDaemon`, preserving KEK isolation, zero-path output sealing, and session breakers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "gewu-atlas-mcp", "version": "1.0.0"}

TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "name": "atlas_vault",
        "description": "查看当前会话绑定的知识库上下文与全部知识库句柄（vlt_XX），或切换/设置默认库。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "description": "可选: context/use/reset/default-get/default-set"},
                "ref": {"type": "string", "description": "可选知识库句柄，如 vlt_01"},
            },
        },
    },
    {
        "name": "atlas_search",
        "description": "在知识库中执行 BM25 + 双链拓扑加权检索（支持 locate/passage/deep 三档模式与跨库检索）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "query": {"type": "string"},
                "mode": {"type": "string", "enum": ["locate", "passage", "deep"]},
                "top_k": {"type": "integer"},
                "all_vaults": {"type": "boolean"},
                "doc_type": {"type": "string"},
                "zone": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "atlas_read",
        "description": "按不透明句柄读取单张或多张笔记正文、章节、行段或仅读大纲（outline=true）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "note_ref": {"type": "string"},
                "note_refs": {"type": "string", "description": "逗号分隔多句柄，如 nte_01,nte_02"},
                "section": {"type": "string"},
                "outline": {"type": "boolean"},
                "raw": {"type": "boolean"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
        },
    },
    {
        "name": "atlas_grep",
        "description": "跨笔记受控字面/正则精准检索，返回不透明句柄与行号上下文（零物理路径）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "pattern": {"type": "string"},
                "regex": {"type": "boolean"},
                "case_sensitive": {"type": "boolean"},
                "zone": {"type": "string"},
                "project_ref": {"type": "string"},
                "context_lines": {"type": "integer"},
                "max_matches": {"type": "integer"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "atlas_project_brief",
        "description": "一屏获取课题域或全库的分区统计、卡片句柄目录与核心双链骨架。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "project_ref": {"type": "string"},
                "include_slices": {"type": "boolean"},
            },
        },
    },
    {
        "name": "atlas_patch",
        "description": "对指定笔记执行局部增量编辑（old_text/new_text、按 section 替换、append/prepend 或 ops_json 原子多段补丁），自动保护受控块、原文只读与日记人类区。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "note_ref": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
                "replace_all": {"type": "boolean"},
                "section": {"type": "string"},
                "mode": {"type": "string", "enum": ["append", "prepend"]},
                "content": {"type": "string"},
                "ops_json": {"type": "string"},
                "expected_sha256": {"type": "string"},
                "by": {"type": "string"},
            },
            "required": ["note_ref"],
        },
    },
    {
        "name": "atlas_batch",
        "description": "在单次加锁与合并闸门事务中顺序执行多步 Atlas 算子，支持 $step_id.field 前序句柄引用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "project_ref": {"type": "string"},
                "by": {"type": "string"},
                "on_error": {"type": "string", "enum": ["stop", "continue"]},
                "steps": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["steps"],
        },
    },
    {
        "name": "atlas_exec",
        "description": "通用受控门面入口：按 GewuAtlas 声明动作名与参数字典执行任意已注册算子。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "GewuAtlas 声明的 --action 名称"},
                "args": {"type": "object", "description": "与 CLI 等价的槽位键值对"},
            },
            "required": ["action"],
        },
    },
]


def _tool_call_to_payload(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = dict(arguments or {})
    if name == "atlas_vault":
        args["action"] = "vault"
        return args
    if name == "atlas_search":
        args["action"] = "note-search"
        return args
    if name == "atlas_read":
        args["action"] = "note-get"
        return args
    if name == "atlas_grep":
        args["action"] = "note-grep"
        return args
    if name == "atlas_project_brief":
        args["action"] = "project-brief"
        return args
    if name == "atlas_patch":
        args["action"] = "note-patch"
        return args
    if name == "atlas_batch":
        steps = args.pop("steps", None)
        if steps is not None and "batch_json" not in args:
            args["batch_json"] = json.dumps({"steps": steps}, ensure_ascii=False)
        args["action"] = "batch"
        return args
    if name == "atlas_exec":
        action = str(args.get("action") or "").strip()
        inner = args.get("args") if isinstance(args.get("args"), dict) else {}
        payload = dict(inner)
        payload["action"] = action
        return payload
    raise ValueError(f"unknown tool: {name}")


def _read_message(stream: Any) -> Tuple[Optional[Dict[str, Any]], str]:
    """Read one JSON-RPC message; returns `(msg_or_none, framing)` where framing is 'lsp' or 'ndjson'."""
    while True:
        raw_line = stream.readline()
        if not raw_line:
            return None, "ndjson"
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, (bytes, bytearray)) else str(raw_line)
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith("content-length:"):
            length = int(stripped.split(":", 1)[1].strip())
            if length < 0 or length > 16 * 1024 * 1024:
                raise ValueError(f"Content-Length out of bounds: {length}")
            # Consume remaining headers until empty line
            while True:
                hdr = stream.readline()
                if not hdr:
                    return None, "lsp"
                hdr_s = hdr.decode("utf-8", errors="replace") if isinstance(hdr, (bytes, bytearray)) else str(hdr)
                if not hdr_s.strip():
                    break
            body_bytes = stream.read(length)
            if len(body_bytes) < length:
                return None, "lsp"
            body_s = body_bytes.decode("utf-8", errors="replace") if isinstance(body_bytes, (bytes, bytearray)) else str(body_bytes)
            return json.loads(body_s), "lsp"
        return json.loads(stripped), "ndjson"


def _write_message(stream: Any, msg: Dict[str, Any], framing: str) -> None:
    raw = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if framing == "lsp":
        header = f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii")
        stream.write(header + raw)
    else:
        stream.write(raw + b"\n")
    stream.flush()


def handle_rpc_request(kernel: Any, req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = req.get("id")
    method = str(req.get("method") or "").strip()
    params = req.get("params") if isinstance(req.get("params"), dict) else {}

    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOL_SPECS}}
    if method == "tools/call":
        tname = str(params.get("name") or "").strip()
        targs = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            payload = _tool_call_to_payload(tname, targs)
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": json.dumps({"ok": False, "code": "E_PARAM_INVALID", "error": str(exc)}, ensure_ascii=False)}],
                },
            }
        try:
            import subprocess
            from facade.atlas_daemon import _should_bypass_action

            stdin_json = json.dumps(payload, ensure_ascii=False)
            if _should_bypass_action(str(payload.get("action") or ""), str(payload.get("mode") or "")):
                proc = subprocess.run(
                    [sys.executable, "-I", "-B", str(SCRIPTS_DIR / "facade" / "atlas_cli.py"), "--no-daemon", "--json-stdin"],
                    input=stdin_json,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                ec = int(proc.returncode)
                out_line = proc.stdout.strip()
            else:
                ec, out_line = kernel.execute_cli(
                    ["--json-stdin"],
                    stdin_text=stdin_json,
                )
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": f"Internal execution error: {exc}"},
            }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "isError": ec != 0,
                "content": [{"type": "text", "text": out_line.rstrip("\n")}],
            },
        }

    if req_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> int:
    from facade.atlas_hot_kernel import AtlasHotKernel

    kernel = AtlasHotKernel()
    in_stream = sys.stdin.buffer
    out_stream = sys.stdout.buffer
    try:
        while True:
            try:
                req, framing = _read_message(in_stream)
            except Exception as exc:
                _write_message(
                    out_stream,
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {exc}"}},
                    "ndjson",
                )
                continue
            if req is None:
                break
            resp = handle_rpc_request(kernel, req)
            if resp is not None:
                _write_message(out_stream, resp, framing)
        return 0
    finally:
        kernel.close()


if __name__ == "__main__":
    sys.exit(main())
