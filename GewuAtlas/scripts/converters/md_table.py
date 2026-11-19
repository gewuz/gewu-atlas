#!/usr/bin/env python3
# ==============================================================================
# md_table.py - 全格式统一的 Markdown 表格产出算子 (fmt 底座共享件)
# 契约（详见 fmt-md.md §1.4）：GFM 表头 + 分隔行；单元格内换行压平为一行；
# 单元格内的字面管道符一律转义为 \|；全空列剔除；表格前后各留一空行。
# 供 docx / pptx / excel / pdf / html / web 六类转换器共同调用。
# ==============================================================================
import re

BULLET = re.compile(r"^[-*+]\s+")


def flatten_cell(text: str) -> str:
    """把单元格内的多行内容压平成单行：列表项以「；」串联，普通折行直接相接。"""
    if not text:
        return ""
    parts, bullets = [], []
    for raw in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        s = BULLET.sub("", raw.strip())
        if not s:
            continue
        parts.append((s, bool(BULLET.match(raw.strip()))))
    out = ""
    for s, is_item in parts:
        if not out:
            out = s
            continue
        if is_item:
            out += ("" if out.endswith(("。", "；", "！", "？", "：", "…")) else "；") + s
        elif s.startswith("【") and out.endswith(("。", "！", "？")):
            out += " " + s
        elif out[-1].isascii() and out[-1].isalnum():
            out += " " + s
        elif out.endswith(("，", "：", "；", "。", "！", "？", "、", "…")):
            out += s
        else:
            # 结构化来源（docx 段落 / html <br> / Excel 换行）里的两行是并列段落，以「；」串联
            out += "；" + s
    return out


def escape_cell(text: str) -> str:
    """单元格安全化：压平换行 + 转义字面管道符（保护 [[link|alias]] 双链管道别名）。"""
    flat = flatten_cell(text)
    placeholders = []

    def repl(m):
        idx = len(placeholders)
        placeholders.append(m.group(0))
        return f"__WIKILINK_ALIAS_PH_{idx}__"

    protected = re.sub(r'\[\[[^\]\n]+?\|[^\]\n]+?\]\]', repl, flat)
    escaped = protected.replace("|", "\\|")
    for idx, ph in enumerate(placeholders):
        escaped = escaped.replace(f"__WIKILINK_ALIAS_PH_{idx}__", ph)
    return escaped


def emit_gfm(headers, rows) -> str:
    """按契约输出 GFM 表格；单元格内容视为已转义，这里只做列对齐与空列剔除。"""
    grid = [list(headers)] + [list(r) for r in rows]
    grid = [[("" if c is None else str(c)) for c in r] for r in grid]
    if not grid or not grid[0] and len(grid) == 1:
        return ""
    ncol = max(len(r) for r in grid) or 0
    for r in grid:
        r.extend([""] * (ncol - len(r)))
    keep = [i for i in range(ncol) if any(r[i] for r in grid)]
    if not keep:
        return ""
    grid = [[r[i] for i in keep] for r in grid]
    lines = [
        "| " + " | ".join(grid[0]) + " |",
        "|" + "|".join([" --- "] * len(keep)) + "|",
    ]
    for r in grid[1:]:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------- HTML 表格
TABLE_TOKEN = "@@GEWU_TABLE_{}@@"


def table_from_html(tag) -> str:
    """把 BeautifulSoup 的 <table> 节点转成 GFM（无 char 偏移，按 tr/td 结构切分）。"""
    rows = []
    for tr in tag.find_all("tr"):
        if tr.find_parent("table") is not tag:
            continue
        cells = [escape_cell(c.get_text("\n", strip=True)) for c in tr.find_all(["th", "td"])
                 if c.find_parent("tr") is tr]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    header = rows[0] if len(rows) > 1 else [""] * len(rows[0])
    if any(rows[0]):
        header, body = rows[0], rows[1:]
    else:
        header, body = [f"列 {i + 1}" for i in range(len(rows[0]))], rows
    return emit_gfm(header, body)


def extract_html_tables(soup) -> dict:
    """就地剥离 <table> 并替换为占位符段落，返回 {占位符: GFM} 映射。"""
    mapping = {}
    for idx, tag in enumerate([t for t in soup.find_all("table") if t.find_parent("table") is None]):
        gfm = table_from_html(tag)
        token = TABLE_TOKEN.format(idx)
        if gfm:
            mapping[token] = gfm
        tag.replace_with(soup.new_string(f"\n\n{token}\n\n"))
    return mapping


def restore_tables(text: str, mapping: dict) -> str:
    """把 html2text 输出中的占位符还原为 GFM 表格块（前后各留一空行）。"""
    for token, gfm in mapping.items():
        text = text.replace(token, f"\n{gfm}\n")
    text = re.sub(r"\n[ \t]+\n", "\n\n", text)
    return re.sub(r"\n{3,}", "\n\n", text)
