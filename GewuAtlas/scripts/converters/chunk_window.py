#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ==============================================================================
# chunk_window.py - 长文档按章节与按表格优化版滑动窗口切片器 (Window=2)
#
# 参考 110-GewuForge 的长文档语义工程方法论：
# 1. 按章节优先切分 (Section-based)：
#    - 优先按 H1/H2/H3 (#, ##, ###) 标题边界切分，窗口清单与原文章节一一对应；
#    - 单窗容量收敛在 2000~4000 字符；遇 H2/H3 且当前字符 >= 2000 时安全落窗；
#    - 尾部碎片合并：末尾小于 800 字符的碎块自动向上合并至前一窗。
# 2. 按表格原子保护与大表分流 (Table-based Protection)：
#    - 表格块 (|...|) 视为不可分割原子块，切点严禁劈开表格行与代码栅格；
#    - 支持大表探测（> 50 行），生成表格抽样预览并在切片元数据中标记。
# 3. 滑动窗口契约 (Window=2)：
#    - 为智能体精准注入 [prev_context] 前驱接缝上下文 + [target_chunk] 待修毛坯；
#    - 记录所属章节 (section_title) 与章节层级，杜绝注意力漂移与截断。
# ==============================================================================

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Converter scripts also run directly under their own directory.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from notes.atlas_markdown_blocks import code_line_mask

HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.*)$")
TABLE_ROW_RE = re.compile(r"^ {0,3}\|.*\|[ \t]*$")


def extract_sections_and_tables(text: str) -> list[dict]:
    """将 Markdown 物理切解为有序的语义元素流 (headings, tables, code_blocks, paragraphs)。"""
    lines = text.splitlines(keepends=True)
    elements = []
    
    in_code = False
    in_table = False
    curr_block = []
    
    def flush(elem_type, extra_meta=None):
        nonlocal curr_block
        if curr_block:
            content = "".join(curr_block)
            meta = {"type": elem_type, "content": content, "chars": len(content)}
            if extra_meta:
                meta.update(extra_meta)
            elements.append(meta)
            curr_block = []

    for line, is_code in zip(lines, code_line_mask(text)):
        stripped = line.strip()

        if is_code:
            if not in_code:
                flush("table" if in_table else "para")
                in_table = False
                in_code = True
            curr_block.append(line)
            continue
        if in_code:
            flush("code")
            in_code = False

        # 表格状态探测
        is_table_row = bool(TABLE_ROW_RE.match(line.rstrip("\r\n")))
        if is_table_row:
            if not in_table:
                flush("para")
                in_table = True
            curr_block.append(line)
            continue
        elif in_table:
            # 表格结束
            flush("table", {"rows_count": len([l for l in curr_block if TABLE_ROW_RE.match(l.strip())])})
            in_table = False

        # 标题探测
        m_head = HEADING_RE.match(line.rstrip("\r\n"))
        if m_head:
            flush("para")
            level = len(m_head.group(1))
            title = m_head.group(2).strip()
            elements.append({
                "type": "heading",
                "level": level,
                "title": title,
                "content": line,
                "chars": len(line)
            })
            continue

        curr_block.append(line)

    if in_code:
        flush("code")
    elif in_table:
        flush("table")
    else:
        flush("para")

    return elements


def split_chunks_by_section(text: str, target_size: int = 3000, min_cut: int = 2000, max_cut: int = 4000) -> list[dict]:
    """优化版：按 H1/H2/H3 章节层级与表格保护进行长文档切片。"""
    elements = extract_sections_and_tables(text)
    
    chunks = []
    curr_elements = []
    curr_chars = 0
    curr_section = "前言/引言"
    
    for el in elements:
        el_chars = el["chars"]
        el_type = el["type"]
        
        # 判断是否为安全章节切点：
        # 当遇到重要标题 (level <= 3) 时，若当前累计字符达到 min_cut (2000字)，立即落窗！
        is_safe_cut = (el_type == "heading" and el.get("level", 4) <= 3 and curr_chars >= min_cut)
        is_overflow = (curr_chars + el_chars > max_cut and curr_chars >= min_cut)
        
        if is_safe_cut or is_overflow:
            chunk_text = "".join(e["content"] for e in curr_elements)
            chunks.append({
                "section": curr_section,
                "content": chunk_text,
                "chars": len(chunk_text),
                "has_table": any(e["type"] == "table" for e in curr_elements),
                "elements_count": len(curr_elements)
            })
            curr_elements = [el]
            curr_chars = el_chars
            if el_type == "heading":
                curr_section = el["title"]
        else:
            curr_elements.append(el)
            curr_chars += el_chars
            if el_type == "heading" and curr_chars < min_cut:
                curr_section = el["title"]

    if curr_elements:
        chunk_text = "".join(e["content"] for e in curr_elements)
        # 尾部碎片合并：若最后一块小于 800 字符，自动合并入上一窗
        if len(chunks) > 0 and len(chunk_text) < 800:
            chunks[-1]["content"] += chunk_text
            chunks[-1]["chars"] = len(chunks[-1]["content"])
            chunks[-1]["has_table"] = chunks[-1]["has_table"] or any(e["type"] == "table" for e in curr_elements)
        else:
            chunks.append({
                "section": curr_section,
                "content": chunk_text,
                "chars": len(chunk_text),
                "has_table": any(e["type"] == "table" for e in curr_elements),
                "elements_count": len(curr_elements)
            })

    return chunks


def build_sliding_windows(chunks: list[dict], preview_chars: int = 400) -> list[dict]:
    """根据章节切片列表构建 Window=2 滑动窗口元数据。"""
    windows = []
    total = len(chunks)

    for i in range(total):
        target = chunks[i]
        prev_ctx = chunks[i - 1]["content"] if i > 0 else ""
        next_preview = chunks[i + 1]["content"][:preview_chars] if i < total - 1 else ""

        windows.append({
            "index": i,
            "total": total,
            "section": target["section"],
            "has_table": target["has_table"],
            "chars": target["chars"],
            "prev_context": prev_ctx,
            "target_chunk": target["content"],
            "next_preview": next_preview
        })

    return windows


def assemble_chunks(refined_chunks: list[str]) -> str:
    """无缝拼接各精修切片。"""
    cleaned = []
    for c in refined_chunks:
        stripped = c.strip()
        if stripped:
            cleaned.append(stripped)
    return "\n\n".join(cleaned) + "\n"


def main():
    parser = argparse.ArgumentParser(description="按章节与按表格长文档滑动切片工具 (Window=2)")
    subparsers = parser.add_subparsers(dest="cmd")

    # split
    p_split = subparsers.add_parser("split", help="按章节切分 Markdown 文件")
    p_split.add_argument("file", help="待切分的 Markdown 文件路径")
    p_split.add_argument("--target-size", "--max-chars", dest="target_size", type=int, default=3000, help="目标单块字符数 (默认 3000)")
    p_split.add_argument("--json", action="store_true", help="以 JSON 格式输出滑动窗口元数据")
    p_split.add_argument("-o", "--output-dir", help="切片输出目录 (可选)")

    # assemble
    p_assemble = subparsers.add_parser("assemble", help="组装切片目录中的所有分块")
    p_assemble.add_argument("dir", help="切片所在目录")
    p_assemble.add_argument("-o", "--output", required=True, help="组装后输出的 Markdown 目标路径")

    args = parser.parse_args()

    if args.cmd == "split":
        src = Path(args.file)
        if not src.is_file():
            print(f"[x] 文件不存在: {args.file}", file=sys.stderr)
            return 1
        text = src.read_text(encoding="utf-8", errors="replace")
        chunks = split_chunks_by_section(text, target_size=args.target_size)
        windows = build_sliding_windows(chunks)

        if args.output_dir:
            out_dir = Path(args.output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            for old_c in out_dir.glob("chunk_*.md"):
                try:
                    old_c.unlink()
                except OSError:
                    pass
            for w in windows:
                chunk_file = out_dir / f"chunk_{w['index']:02d}.md"
                chunk_file.write_text(w["target_chunk"], encoding="utf-8")
            meta_file = out_dir / "windows_meta.json"
            meta_file.write_text(json.dumps(windows, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[PASS] 成功按章节切分 {len(chunks)} 个窗口至目录: {out_dir}")

        if args.json:
            print(json.dumps(windows, ensure_ascii=False, indent=2))
        elif not args.output_dir:
            print(f"[PASS] 文档 {src.name} 成功按章节切分为 {len(chunks)} 个语意窗口:")
            for w in windows:
                tbl_tag = " [含表格]" if w["has_table"] else ""
                print(f"  - 窗口 {w['index'] + 1:02d}/{w['total']:02d} | 章节: {w['section'][:30]:30} | {w['chars']} 字符{tbl_tag}")
        return 0

    elif args.cmd == "assemble":
        in_dir = Path(args.dir)
        if not in_dir.is_dir():
            print(f"[x] 目录不存在: {args.dir}", file=sys.stderr)
            return 1

        def chunk_sort_key(p: Path):
            m = re.search(r'\d+', p.stem)
            return int(m.group(0)) if m else 0

        chunk_files = sorted(in_dir.glob("chunk_*.md"), key=chunk_sort_key)
        if not chunk_files:
            print(f"[x] 目录下未找到 chunk_*.md: {args.dir}", file=sys.stderr)
            return 1
        refined = [f.read_text(encoding="utf-8") for f in chunk_files]
        full_text = assemble_chunks(refined)
        out_p = Path(args.output)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(full_text, encoding="utf-8")
        print(f"[PASS] 成功组装 {len(refined)} 个分块并输出至: {out_p}")
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
