#!/usr/bin/env python3
"""Pure-stdlib structural Markdown normalizer & checker for GewuAtlas (`markdown-formatting-v1`).

Preserves YAML frontmatter and fenced code blocks (``` / ~~~) verbatim.
Only fixes structural layout outside fences:
- trailing whitespace and collapsed duplicate blank lines;
- blank-line isolation around ATX headings;
- unordered bullet marker normalization (* / + -> -);
- level jump smoothing & unclosed code fence diagnostics.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from notes.atlas_note_engine import _atomic_write_md, _public_note, _scan_vault_markdowns
from notes.atlas_yamlmini import FrontmatterError, extract_frontmatter
from storage.note_kek_store import KekNoteRegistry
from storage.vault_kek_store import VaultStoreError

_ATX_RE = re.compile(r"^(#{1,6})(\s+.*|\s*)$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_BULLET_RE = re.compile(r"^(\s*)([*+])\s+(.*)$")


def _split_frontmatter_lines(lines: List[str]) -> Tuple[List[str], List[str]]:
    if not lines or lines[0].strip() != "---":
        return [], lines
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return lines[: idx + 1], lines[idx + 1 :]
    return [], lines


def _detect_unclosed_fence(lines: List[str]) -> Optional[str]:
    opened: Optional[str] = None
    for line in lines:
        m = _FENCE_RE.match(line)
        if not m:
            continue
        marker, tail = m.groups()
        if opened is None:
            if marker[0] != "`" or "`" not in tail:
                opened = marker
        elif marker[0] == opened[0] and len(marker) >= len(opened) and not tail.strip():
            opened = None
    return opened


def _normalize_body_lines(lines: List[str]) -> Tuple[List[str], Dict[str, Any]]:
    """Normalize Markdown body outside code fences without altering prose semantics."""
    blank_fixes = 0
    bullet_fixes = 0
    heading_space_fixes = 0
    heading_level_fixes = 0
    warnings: List[str] = []

    opened: Optional[str] = None
    pass1: List[str] = []
    heading_positions: List[Tuple[int, int]] = []  # (index in pass1, raw_level)

    for raw_line in lines:
        m_fence = _FENCE_RE.match(raw_line)
        is_fence_edge = False
        if m_fence:
            marker, tail = m_fence.groups()
            if opened is None:
                if marker[0] != "`" or "`" not in tail:
                    opened = marker
                    is_fence_edge = True
            elif marker[0] == opened[0] and len(marker) >= len(opened) and not tail.strip():
                opened = None
                is_fence_edge = True

        if opened is not None or is_fence_edge:
            pass1.append(raw_line)
            continue

        stripped_line = raw_line.rstrip()
        if stripped_line != raw_line:
            blank_fixes += 1

        if not stripped_line:
            if pass1 and not pass1[-1].strip():
                blank_fixes += 1
                continue
            pass1.append("")
            continue

        m_bullet = _BULLET_RE.match(stripped_line)
        if m_bullet:
            indent, _marker, rest = m_bullet.groups()
            norm_indent = indent.replace("\t", "  ")
            cand = f"{norm_indent}- {rest}"
            if cand != stripped_line:
                bullet_fixes += 1
            pass1.append(cand)
            continue

        m_atx = _ATX_RE.match(stripped_line)
        if m_atx and m_atx.group(2).strip():
            hashes, rest = m_atx.groups()
            if len(rest) - len(rest.lstrip()) != 1:
                stripped_line = f"{hashes} {rest.lstrip()}"
                heading_space_fixes += 1
            heading_positions.append((len(pass1), len(hashes)))
        pass1.append(stripped_line)

    # Compress skipped heading levels globally if multiple headings exist (anchor min level)
    if heading_positions:
        h1_count = sum(1 for _, lvl in heading_positions if lvl == 1)
        if h1_count > 1:
            warnings.append(f"检测到 {h1_count} 个一级标题 (#)，建议每篇文档保留单一主标题")
        prev_out_level = 0
        for pos, raw_lvl in heading_positions:
            target_lvl = raw_lvl
            if prev_out_level > 0 and raw_lvl > prev_out_level + 1:
                target_lvl = prev_out_level + 1
                heading_level_fixes += 1
                m_atx = _ATX_RE.match(pass1[pos])
                if m_atx:
                    pass1[pos] = f"{'#' * target_lvl} {m_atx.group(2).lstrip()}"
            prev_out_level = target_lvl

    # Ensure single blank line isolation around ATX headings outside fences
    pass2: List[str] = []
    opened = None
    for idx, line in enumerate(pass1):
        m_fence = _FENCE_RE.match(line)
        is_fence_edge = False
        if m_fence:
            marker, tail = m_fence.groups()
            if opened is None:
                if marker[0] != "`" or "`" not in tail:
                    opened = marker
                    is_fence_edge = True
            elif marker[0] == opened[0] and len(marker) >= len(opened) and not tail.strip():
                opened = None
                is_fence_edge = True

        if opened is not None or is_fence_edge:
            pass2.append(line)
            continue

        is_heading = bool(_ATX_RE.match(line) and line.lstrip("#").strip())
        if is_heading and pass2 and pass2[-1].strip():
            pass2.append("")
            heading_space_fixes += 1
        pass2.append(line)
        if is_heading and idx + 1 < len(pass1) and pass1[idx + 1].strip():
            pass2.append("")
            heading_space_fixes += 1

    while pass2 and not pass2[0].strip():
        pass2.pop(0)
    while pass2 and not pass2[-1].strip():
        pass2.pop()

    metrics = {
        "blank_line_fixes": blank_fixes,
        "bullet_fixes": bullet_fixes,
        "heading_space_fixes": heading_space_fixes,
        "heading_level_fixes": heading_level_fixes,
        "warnings": warnings,
    }
    return pass2, metrics


def _check_mermaid_blocks(lines: List[str]) -> Tuple[int, List[str]]:
    """Inspect ```mermaid blocks for empty diagrams, missing diagram headers, or inline HTML tags."""
    supported_heads = (
        "flowchart",
        "graph",
        "sequencediagram",
        "statediagram",
        "classdiagram",
        "erdiagram",
        "xychart-beta",
    )
    in_mermaid = False
    cur_block: List[str] = []
    block_count = 0
    warnings: List[str] = []

    for line in lines:
        s = line.strip()
        if not in_mermaid:
            if s.lower().startswith("```mermaid"):
                in_mermaid = True
                cur_block = []
        else:
            if s.startswith("```"):
                block_count += 1
                non_empty = [x.strip() for x in cur_block if x.strip() and not x.strip().startswith("%%")]
                if not non_empty:
                    warnings.append(f"第 {block_count} 个 Mermaid 图块为空")
                else:
                    first_tok = non_empty[0].split()[0].lower()
                    if not any(first_tok.startswith(h) for h in supported_heads):
                        warnings.append(f"第 {block_count} 个 Mermaid 图块首行类型受限或缺失（发现: {non_empty[0][:24]}）")
                if any("<br" in x.lower() or "<div" in x.lower() or "<span" in x.lower() for x in non_empty):
                    warnings.append(f"第 {block_count} 个 Mermaid 图块含 HTML 标签，建议改用纯文本标签")
                in_mermaid = False
                cur_block = []
            else:
                cur_block.append(line)
    return block_count, warnings


def normalize_markdown_text(raw: str) -> Tuple[str, Dict[str, Any]]:
    """Return structurally normalized Markdown text and structural repair telemetry."""
    lines = raw.splitlines()
    fm_lines, body_lines = _split_frontmatter_lines(lines)
    unclosed = _detect_unclosed_fence(body_lines)
    mermaid_count, mermaid_warnings = _check_mermaid_blocks(body_lines)
    if unclosed is not None:
        return raw, {
            "unclosed_fence": unclosed,
            "total_fixes": 0,
            "blank_line_fixes": 0,
            "bullet_fixes": 0,
            "heading_space_fixes": 0,
            "heading_level_fixes": 0,
            "mermaid_blocks_checked": mermaid_count,
            "warnings": [f"存在未闭合代码围栏 ({unclosed})，已跳过结构改写以防止误伤正文", *mermaid_warnings],
            "changed": False,
        }

    norm_body, metrics = _normalize_body_lines(body_lines)
    metrics["warnings"].extend(mermaid_warnings)
    metrics["mermaid_blocks_checked"] = mermaid_count
    out_lines: List[str] = []
    if fm_lines:
        out_lines.extend(fm_lines)
        if norm_body:
            out_lines.append("")
    out_lines.extend(norm_body)
    rendered = "\n".join(out_lines).rstrip() + "\n" if out_lines else "\n"
    total_fixes = (
        metrics["blank_line_fixes"]
        + metrics["bullet_fixes"]
        + metrics["heading_space_fixes"]
        + metrics["heading_level_fixes"]
    )
    metrics["unclosed_fence"] = None
    metrics["total_fixes"] = total_fixes
    metrics["changed"] = rendered != raw
    return rendered, metrics


def note_format(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    note_ref: str,
    check_only: bool = False,
) -> Tuple[Dict[str, Any], List[str]]:
    """Check or atomically format a Vault note referenced by handle without leaking physical paths."""
    rec = note_reg.resolve_handle(vault_ref, note_ref)
    rel = str(rec["note_rel_path"])
    fpath = vault_dir / rel
    if not fpath.is_file():
        raise VaultStoreError("E_NOT_FOUND", f"句柄 {note_ref} 对应笔记不存在", exit_code=1)

    raw = fpath.read_text(encoding="utf-8")
    rendered, metrics = normalize_markdown_text(raw)
    if metrics["unclosed_fence"] and not check_only:
        raise VaultStoreError(
            "E_PARAM_CONFLICT",
            f"笔记 {note_ref} 存在未闭合代码围栏 ({metrics['unclosed_fence']})，请先闭合后格式化",
            exit_code=1,
        )

    wrote = False
    if not check_only and metrics["changed"]:
        _atomic_write_md(fpath, rendered)
        wrote = True

    try:
        meta, _, _ = extract_frontmatter(rendered if wrote else raw)
    except FrontmatterError:
        meta = {}
    pub = _public_note(kek=kek, app_id=app_id, vault_ref=vault_ref, note_ref=note_ref, rel=rel, meta=meta)
    pub.update(
        {
            "check_only": bool(check_only),
            "wrote": wrote,
            "changed": bool(metrics["changed"]),
            "total_fixes": int(metrics["total_fixes"]),
            "blank_line_fixes": int(metrics["blank_line_fixes"]),
            "bullet_fixes": int(metrics["bullet_fixes"]),
            "heading_space_fixes": int(metrics["heading_space_fixes"]),
            "heading_level_fixes": int(metrics["heading_level_fixes"]),
            "mermaid_blocks_checked": int(metrics.get("mermaid_blocks_checked", 0)),
            "unclosed_fence": metrics["unclosed_fence"],
            "warnings": metrics["warnings"],
        }
    )
    return pub, [rel, *_scan_vault_markdowns(vault_dir)]
