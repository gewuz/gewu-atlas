#!/usr/bin/env python3
"""Markdown 结构规范化器 (md -> md 纯结构转译)。

只动 Markdown 骨架：标题层级、空行隔离、列表缩进、表格对齐、代码块闭合、
字面符号保护与尾部噪音。一句话都不改——正文文字保持原样。

用法:
    python3 md_normalizer.py <file.md> [--check] [--no-symbols]

    --check       只报告问题与命中行号，不写盘
    默认行为      原地写回（配合 Git 快照可回滚）
"""

import re
import sys
from pathlib import Path

import md_table

HTML_TAGS = {
    "br", "hr", "img", "a", "b", "i", "u", "s", "p", "div", "span", "sup", "sub",
    "kbd", "code", "pre", "table", "tr", "td", "th", "ul", "ol", "li", "blockquote",
}


def _is_fence(line):
    """代码围栏识别：CommonMark 允许 ``` 与 ~~~ 双格式，全模块状态机统一经此判定。"""
    s = line.lstrip()
    return s.startswith("```") or s.startswith("~~~")


def unclosed_fence(lines):
    opened = None
    for line in lines:
        match = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if not match:
            continue
        marker, tail = match.groups()
        if opened is None:
            if marker[0] != '`' or '`' not in tail:
                opened = marker
        elif marker[0] == opened[0] and len(marker) >= len(opened) and not tail.strip():
            opened = None
    return opened


def split_frontmatter(lines):
    """返回 (frontmatter 行, 正文行, 起始行号)。"""
    if not lines or lines[0].strip() != "---":
        return [], lines, 0
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[: i + 1], lines[i + 1:], i + 1
    return [], lines, 0


def normalize_blank_lines(lines):
    out, fixes = [], 0
    in_fence = False
    for line in lines:
        if _is_fence(line):
            in_fence = not in_fence
        if not in_fence and not line.strip() and out and not out[-1].strip():
            fixes += 1
            continue
        out.append(line.rstrip() if not in_fence else line)
    return out, fixes


def normalize_headings(lines):
    """标题层级：全篇保序压缩、前后空行、跨级只报不改。

    层级判定是「整篇文档的属性」，已由上游（PDF 书签锚定 / 文法推定 / 字号聚类）
    一次性裁定，此处严禁再按行做 `prev+1` 局部钳位——那会静默改写上游裁定结果。
    只做两件事：把全篇实际用到的层级集合压缩为连续序列（锚定原最小层级，不上抬
    片段文档的顶层），并把残余的语序跨级作为需人工裁定的告警显式呈报。
    """
    out, fixes, skips = [], 0, 0
    prev_level = 0
    seen_h1 = 0
    in_fence = False
    levels = set()
    for line in lines:
        if _is_fence(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r"^(#{1,6})\s+.*$", line)
        if m:
            levels.add(len(m.group(1)))
    if not levels:
        return list(lines), 0, 0, 0
    base = min(levels)
    remap = {v: base + i for i, v in enumerate(sorted(levels))}

    in_fence = False
    for line in lines:
        if _is_fence(line):
            in_fence = not in_fence
            out.append(line)
            continue
        m = re.match(r"^(#{1,6})\s+(.*?)\s*$", line) if not in_fence else None
        if not m:
            out.append(line)
            continue
        raw_level, text = len(m.group(1)), m.group(2)
        level = remap[raw_level]
        if level != raw_level:
            fixes += 1
        if level == 1:
            seen_h1 += 1
        if prev_level and level > prev_level + 1:
            skips += 1
        if out and out[-1].strip():
            out.append("")
        out.append("#" * level + " " + text)
        prev_level = level
    return out, fixes, seen_h1, skips


def normalize_lists(lines):
    out, fixes = [], 0
    in_fence = False
    for line in lines:
        if _is_fence(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence or not line.strip():
            out.append(line)
            continue
        new = line.replace("\t", "  ")
        m = re.match(r"^(\s*)([*+])\s+(.*)$", new)
        if m:
            new = f"{m.group(1)}- {m.group(3)}"
            fixes += 1
        indent = len(new) - len(new.lstrip(" "))
        if indent and indent % 2:
            new = " " * (indent + 1) + new.lstrip(" ")
            fixes += 1
        out.append(new)
    return out, fixes


def _is_grid_border(line):
    return bool(re.match(r"^\+(?:[-=]+\+)+$", line.strip()))


def _split_pipe_row(line):
    s = line.strip().lstrip("|")
    if s.endswith("|"):
        s = s[:-1]
    return s.split("|")


def _grid_cell_signature(seq):
    """单元格文字的多重集特征（剔除表格结构符号），用于校验转换零丢失。"""
    return sorted(re.sub(r"[|+\-=:\s；\\]", "", "".join(seq)))


def normalize_grid_tables(lines):
    """把 Pandoc 栅格表（+---+ 边框）重建为标准 GFM 表格。

    按每行自身的 | 分隔符切列（不依赖边框宽度推算字符偏移），
    逐块做字符多重集校验；校验不一致则整块放行，交 Agent 人工重建。
    """
    out, done, manual, i = [], 0, 0, 0
    in_fence = False
    while i < len(lines):
        line = lines[i]
        if _is_fence(line):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if in_fence or not _is_grid_border(line):
            out.append(line)
            i += 1
            continue
        a = i
        while i < len(lines) and (_is_grid_border(lines[i]) or lines[i].strip().startswith("|")):
            i += 1
        seg = lines[a:i]
        ncol = seg[0].strip().count("+") - 1
        rows_ok = all(ln.strip().count("|") == ncol + 1 for ln in seg if ln.strip().startswith("|"))
        groups, cur, seen_sep = [], [], False
        for ln in seg:
            if _is_grid_border(ln):
                if cur:
                    groups.append(cur)
                    cur = []
                if "=" in ln:
                    seen_sep = True
            else:
                cur.append(ln)
        if cur:
            groups.append(cur)
        if not rows_ok or not groups:
            out.extend(seg)
            manual += 1
            continue
        grid = []
        for g in groups:
            row = []
            for c in range(ncol):
                row.append(md_table.flatten_cell("\n".join(_split_pipe_row(ln)[c] for ln in g)))
            grid.append(row)
        header, body = grid[0], grid[1:]
        if not any(header):
            header = [f"列 {c+1}" for c in range(ncol)]
        gfm = md_table.emit_gfm([h.replace("|", "\\|") for h in header],
                                [[c.replace("|", "\\|") for c in r] for r in body])
        if not gfm or _grid_cell_signature(seg) != _grid_cell_signature(gfm.split("\n")):
            out.extend(seg)
            manual += 1
            continue
        out.extend(gfm.split("\n"))
        done += 1
    return out, done, manual


def normalize_tables(lines):
    """表格前后空行 + 分隔行列数与表头对齐。"""
    out, fixes = [], 0
    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if _is_fence(line):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if not in_fence and line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|$", lines[i + 1]):
            cols = line.count("|") - 1
            sep_cells = [c.strip() for c in lines[i + 1].strip().strip("|").split("|")]
            if len(sep_cells) != cols:
                aligned = "| " + " | ".join(["---"] * cols) + " |"
                fixes += 1
            else:
                aligned = lines[i + 1]
            if out and out[-1].strip():
                out.append("")
            out.append(line)
            out.append(aligned)
            j = i + 2
            while j < len(lines) and lines[j].startswith("|"):
                cells = lines[j].count("|") - 1
                # 双链别名 `[[a|b]]` 与行内代码里的竖线不算列分隔，原样保留
                if cells != cols and "[[" not in lines[j] and "`" not in lines[j]:
                    body = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                    body = (body + [""] * cols)[:cols]
                    out.append("| " + " | ".join(body) + " |")
                    fixes += 1
                else:
                    out.append(lines[j])
                j += 1
            if j < len(lines) and lines[j].strip():
                out.append("")
            i = j
            continue
        out.append(line)
        i += 1
    return out, fixes


def protect_symbols(lines):
    """字面符号保护：<占位符> 与 *.后缀 包行内代码（代码块内不动）。"""
    out, fixes = [], 0
    in_fence = False
    for line in lines:
        if _is_fence(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue

        def repl_tag(m):
            nonlocal fixes
            name = m.group(1)
            if name.lower() in HTML_TAGS or "/" in name or not re.match(r"^[\w\s+~.=%#-]{1,24}$", name):
                return m.group(0)
            fixes += 1
            return "`<" + name + ">`"

        new = re.sub(r"(?<!`)`?<([^<>\n|]{1,24})>`?(?!`)", repl_tag, line)

        def repl_suffix(m):
            nonlocal fixes
            fixes += 1
            return "`*." + m.group(1) + "`"

        new = re.sub(r"(?<![\w`])\*\.([A-Za-z0-9]{1,6})(?![\w`])", repl_suffix, new)
        out.append(new)
    return out, fixes


def normalize(path: Path, check_only=False, symbols=True):
    original = path.read_text(encoding="utf-8")
    lines = original.split("\n")
    fm, body, _ = split_frontmatter(lines)
    report = []

    body, n = normalize_blank_lines(body)
    report.append(("连续空行折叠 / 行尾空格", n))
    body, n, h1, skips = normalize_headings(body)
    report.append(("标题层级与空行隔离", n))
    if h1 > 1:
        report.append(("H1 重复（需人工裁定唯一题头）", h1))
    if skips:
        report.append(("标题语序跨级（需人工裁定，未自动改写）", skips))
    body, n = normalize_lists(body)
    report.append(("列表符号与缩进", n))
    body, n, manual = normalize_grid_tables(body)
    report.append(("栅格表重建为 GFM", n))
    if manual:
        report.append(("栅格表校验不一致（需人工重建）", manual))
    body, n = normalize_tables(body)
    report.append(("表格对齐", n))
    if symbols:
        body, n = protect_symbols(body)
        report.append(("字面符号保护", n))

    fence = unclosed_fence(body)
    if fence:
        body.append(fence)
        report.append(("代码块补闭合", 1))

    new_text = "\n".join(fm + body).rstrip("\n") + "\n"
    total = sum(c for _, c in report)
    print(f"\n[*] {path.name}")
    for label, cnt in report:
        if cnt:
            print(f"    - {label}: {cnt} 处")
    print(f"    合计 {total} 处；{len(original)} → {len(new_text)} 字符")

    if check_only:
        print("    [check] 未写盘")
        return total
    if new_text != original:
        path.write_text(new_text, encoding="utf-8")
        print("    [✓] 已写回")
    else:
        print("    [=] 无需改动")
    return total


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    check_only = "--check" in args
    symbols = "--no-symbols" not in args
    files = [Path(a) for a in args if not a.startswith("--")]
    rc = 0
    for f in files:
        if not f.exists():
            print(f"[x] 文件不存在: {f}")
            rc = 1
            continue
        normalize(f, check_only=check_only, symbols=symbols)
    sys.exit(rc)
