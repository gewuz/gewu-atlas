#!/usr/bin/env python3
"""
excel_converter.py - 智能 Excel (.xlsx / .xls) 文档解析与 Markdown 结构化转换引擎

遵循 AGENTS.md 准则与知识库架构：
1. 【多 Sheet 全量识别与大纲化】：
   - 原生遍历识别所有 Sheet（包括隐藏状态、总行数、总列数）。
   - 顶部 YAML Frontmatter 收录全部 Sheet 清单与数据规模。
   - 自动生成带统计信息的 Sheet 一键直达锚点导航（Table of Contents）。
   - 每个 Sheet 对应二级大纲 (## 📊 Sheet: {SheetName})。
2. 【复杂合并单元格物理重构 (Unmerge & Forward-fill)】：
   - 跨全列大标题 (Banner Title)：自动探测整行单值大标题并抽取为表内说明，不污染数据列。
   - 跨行合并 (Rowspan)：自动执行向下补全继承 (Forward-fill)，消除空行，保障行独立性与检索排序。
   - 跨列合并 (Colspan) 与多级表头：自动向下级联/拼接复合表头（例如 Q1月度收入_1月已实现）。
3. 【数据规模智能分流 (Scale Triage)】：
   - 小表 (<= 50 行)：全量提取，AI/引擎提炼业务要点洞察总结 (Callout) 并格式化为 Obsidian GFM 表格。
   - 大表 (> 50 行)：提取表头 + 统计指标 + 前 10 行样本，提炼架构与业务特征解读 (Callout) 与预览表，
     同时在 99-Assets/10-Attachments/ 生成伴生 CSV 底表，彻底防止超长表格卡死 Obsidian。
4. 【Obsidian 语法与安全防护】：
   - 单元格内换行 (Alt+Enter) 自动转为 <br>。
   - 裸 $ 符号转义为 \\$（杜绝 KaTeX 误渲染）。
   - 管道符 | 转义为 \\|。
5. 【DropZone 批量自动化归档】：
   - 扫描 00-DropZone/ 下的 Excel 文档（忽略 91-Temp/）。
   - 原件安全移入 99-Assets/10-Attachments/，输出 Markdown 笔记至 20-AI-Zone/20-Memory/21-Library/。
"""

__version__ = "1.1"
import os
import sys
import re
import csv
import json
import shutil
import argparse
from pathlib import Path
from datetime import datetime, date

import md_table
from archive_month import monthly_archive_dir, month_tag

_CONVERTERS_DIR = Path(__file__).resolve().parent
if str(_CONVERTERS_DIR) not in sys.path:
    sys.path.insert(0, str(_CONVERTERS_DIR))
from core.project import get_status_for_domain
from core.common import fm_scalar

try:
    import openpyxl
    from openpyxl.utils import get_column_letter
except ImportError:
    print("[-] 错误: 未安装 openpyxl。请在虚拟环境中安装: pip install openpyxl")
    sys.exit(1)

def find_vault_root() -> Path:
    managed = os.environ.get("GEWU_ATLAS_VAULT_ROOT", "").strip()
    if managed:
        return Path(managed).resolve()
    current = Path(__file__).resolve()
    for p in [current] + list(current.parents):
        if (p / "AGENTS.md").exists() or (p / ".obsidian").exists():
            return p
    return Path.cwd().resolve()

VAULT_ROOT = find_vault_root()


def escape_cell_value(val) -> str:
    """转义单元格内容，保障 Markdown 与 KaTeX 安全"""
    if val is None:
        return ""
    if isinstance(val, (datetime, date)):
        if isinstance(val, datetime):
            return val.strftime("%Y-%m-%d %H:%M")
        return val.strftime("%Y-%m-%d")
    if isinstance(val, float):
        if val.is_integer():
            s = str(int(val))
        else:
            s = f"{val:.4f}".rstrip("0").rstrip(".")
    else:
        s = str(val).strip()
        
    # 处理单元格内换行（Alt+Enter）：压平为一行，列表项以「；」串联
    s = md_table.flatten_cell(s)
    # 转义 Markdown 管道符
    s = s.replace("|", "\\|")
    # 转义 KaTeX 美元符号
    s = s.replace("$", "\\$")
    return s


def parse_sheet_matrix(ws, ws_formula=None):
    """
    解析单张工作表，解开合并单元格并执行针对文本的 Forward-fill（数值列仅保留左上角首值防重复求和），
    当 data_only 下公式缓存为空时自动从 ws_formula 提取原公式文本保真回退。
    返回标准化 2D 矩阵 (list of list of strings), 隐藏行数, 隐藏列数
    """
    max_row = ws.max_row or 0
    max_col = ws.max_column or 0
    if max_row == 0 or max_col == 0:
        return [], 0, 0

    # 统计隐藏行与隐藏列
    hidden_rows = sum(1 for r in range(1, max_row + 1) if r in ws.row_dimensions and ws.row_dimensions[r].hidden)
    from openpyxl.utils import get_column_letter
    hidden_cols = sum(1 for c in range(1, max_col + 1) if get_column_letter(c) in ws.column_dimensions and ws.column_dimensions[get_column_letter(c)].hidden)

    # 初始化空网格 (1-based index 映射到 0-based)
    grid = [[None for _ in range(max_col)] for _ in range(max_row)]

    # 读取原始单元格数据（公式缓存丢失时自动回退提取公式原文）
    for r in range(1, max_row + 1):
        for c in range(1, max_col + 1):
            val = ws.cell(row=r, column=c).value
            if val is None and ws_formula is not None:
                try:
                    f_val = ws_formula.cell(row=r, column=c).value
                    if f_val is not None and str(f_val).startswith("="):
                        val = str(f_val)
                except Exception:
                    pass
            grid[r - 1][c - 1] = val

    # 获取所有合并单元格并执行 Forward-fill
    # 策略：文本与维度字段向下继承保持行完整；数值单元格非左上角置空，杜绝重复累加导致的大盘指标虚高
    merged_ranges = list(ws.merged_cells.ranges)
    for mr in merged_ranges:
        min_col, min_row, max_c, max_r = mr.min_col, mr.min_row, mr.max_col, mr.max_row
        top_left_val = grid[min_row - 1][min_col - 1]
        is_num = isinstance(top_left_val, (int, float))
        for r in range(min_row, max_r + 1):
            for c in range(min_col, max_c + 1):
                if r == min_row and c == min_col:
                    continue
                if is_num:
                    grid[r - 1][c - 1] = ""
                else:
                    grid[r - 1][c - 1] = top_left_val

    # 移除末尾全空行
    while grid and all(v is None or str(v).strip() == "" for v in grid[-1]):
        grid.pop()
    if not grid:
        return [], hidden_rows, hidden_cols

    # 裁剪左右全空列
    valid_cols = set()
    for row in grid:
        for c_idx, v in enumerate(row):
            if v is not None and str(v).strip() != "":
                valid_cols.add(c_idx)
    if not valid_cols:
        return [], hidden_rows, hidden_cols
    min_valid_col = min(valid_cols)
    max_valid_col = max(valid_cols)

    trimmed_grid = []
    for row in grid:
        trimmed_grid.append(row[min_valid_col : max_valid_col + 1])

    return trimmed_grid, hidden_rows, hidden_cols


def extract_headers_and_data(grid):
    """
    分析网格结构，识别独立大标题（Banner Title）、多级复合表头与真实数据行
    """
    if not grid:
        return None, [], []

    sheet_title = None
    start_row = 0

    # 1. 检查第 1 行是否为横跨全表的单值大标题（或者整行值完全相同）
    first_row_non_empty = [v for v in grid[0] if v is not None and str(v).strip() != ""]
    unique_first_row = set(str(v).strip() for v in first_row_non_empty)
    if len(unique_first_row) == 1 and len(grid) > 2:
        sheet_title = list(unique_first_row)[0]
        start_row = 1

    remaining = grid[start_row:]
    if not remaining:
        return sheet_title, [], []

    # 2. 判断表头层级（单行表头 vs 2 级复合表头）
    if len(remaining) <= 2:
        headers = [escape_cell_value(v) or f"列_{i+1}" for i, v in enumerate(remaining[0])]
        data_rows = remaining[1:]
        return sheet_title, headers, data_rows

    row0 = remaining[0]
    row1 = remaining[1]
    
    # 判断是否存在跨列合并或多级表头（row0 有连续相同非空值，且与 row1 不完全一致）
    has_colspan = False
    for i in range(len(row0) - 1):
        if row0[i] is not None and row0[i] == row0[i+1] and str(row0[i]).strip() != "":
            has_colspan = True
            break

    # 复合表头条件：存在跨列合并，或者第一行与第二行呈现父子结构
    if (has_colspan or any(r0 != r1 for r0, r1 in zip(row0, row1))) and len(remaining) >= 3:
        # 如果 row0 与 row1 大部分单元格不同，且 row1 看上去不是数据行而是具体列名
        is_sub_header = False
        for c_idx in range(len(row0)):
            val0 = str(row0[c_idx] or "").strip()
            val1 = str(row1[c_idx] or "").strip()
            if val0 != val1 and any(keyword in val1 for keyword in ["已实现", "预估", "明细", "月", "日", "率", "比", "状态", "金额", "数量", "备注"]):
                is_sub_header = True
                break
        
        if has_colspan or is_sub_header:
            headers = []
            for c_idx in range(len(row0)):
                p_val = escape_cell_value(row0[c_idx])
                s_val = escape_cell_value(row1[c_idx])
                if p_val and s_val and p_val != s_val:
                    headers.append(f"{p_val}_{s_val}")
                elif s_val:
                    headers.append(s_val)
                elif p_val:
                    headers.append(p_val)
                else:
                    headers.append(f"列_{c_idx+1}")
            data_rows = remaining[2:]
            return sheet_title, headers, data_rows

    # 单行表头
    headers = [escape_cell_value(v) or f"列_{i+1}" for i, v in enumerate(row0)]
    data_rows = remaining[1:]
    return sheet_title, headers, data_rows


def format_gfm_table(headers, rows, max_preview=None):
    """生成合规的 GitHub Flavored Markdown (GFM) 表格"""
    if not headers:
        return ""

    num_cols = len(headers)
    lines = []
    
    header_line = "| " + " | ".join(headers) + " |"
    sep_line = "| " + " | ".join([":---"] * num_cols) + " |"
    lines.append(header_line)
    lines.append(sep_line)

    slice_rows = rows if max_preview is None else rows[:max_preview]
    for r in slice_rows:
        row_vals = []
        for c_idx in range(num_cols):
            val = r[c_idx] if c_idx < len(r) else ""
            row_vals.append(escape_cell_value(val))
        lines.append("| " + " | ".join(row_vals) + " |")

    return "\n".join(lines)


def generate_smart_summary(sheet_name: str, sheet_title: str, headers: list, data_rows: list, scale: str, hidden_rows: int = 0, hidden_cols: int = 0) -> str:
    """
    智能分析表格数据特征，生成高价值业务解读与洞察 Callout
    """
    row_count = len(data_rows)
    col_count = len(headers)

    if scale == "small":
        lines = ["> [!SUMMARY] 业务要点解读与洞察"]
        
        numeric_sums = {}
        for r in data_rows:
            for idx, val in enumerate(r):
                if idx < len(headers) and isinstance(val, (int, float)):
                    h = headers[idx]
                    numeric_sums[h] = numeric_sums.get(h, 0.0) + float(val)

        if numeric_sums:
            primary_metric = max(numeric_sums.items(), key=lambda x: x[1])
            lines.append(f"> 1. **核心大盘指标**：本表收录 {row_count} 条核心业务记录，其中【{primary_metric[0]}】累计值达 **{primary_metric[1]:,.2f}**。")
        else:
            lines.append(f"> 1. **数据规模**：共提取 {row_count} 行有效记录，覆盖 {col_count} 个核心配置与业务维度。")

        first_col_vals = list(set(str(r[0]).strip() for r in data_rows if r and r[0] is not None))
        if 1 < len(first_col_vals) <= 6:
            lines.append(f"> 2. **业务覆盖范围**：涵盖 `{'`、`'.join(first_col_vals)}` 等核心维度，各板块记录完整。")

        notes_found = []
        for r in data_rows:
            for v in r:
                if isinstance(v, str) and ("超" in v or "增" in v or "突破" in v or "预警" in v or "利用率" in v or "发布" in v):
                    clean_note = v.replace("\n", " ").replace("\r", " ")
                    if len(clean_note) < 60 and clean_note not in notes_found:
                        notes_found.append(clean_note)
                        if len(notes_found) >= 2:
                            break
            if len(notes_found) >= 2:
                break

        if notes_found:
            lines.append(f"> 3. **重点动态与洞察**：{'；'.join(notes_found)}。")
        else:
            lines.append(f"> 3. **版式重构**：已完成多级复合表头级联与跨行合并单元格向下继承补全 (Forward-fill)，支持原地排序与全文检索。")

        if hidden_rows > 0 or hidden_cols > 0:
            lines.append(f"> 4. **隐藏结构感知**：检测到 {hidden_rows} 行隐藏行、{hidden_cols} 列隐藏列（数据已完整提取保留）。")

        return "\n".join(lines)
    else:
        lines = ["> [!INFO] 数据集特征与架构解读"]
        lines.append(f"> - **数据全貌**：本工作表记录海量流水明细，共包含 **{row_count}** 条有效记录，覆盖 **{col_count}** 个数据字段。")
        
        key_fields = "、".join([f"`{h}`" for h in headers[:8]])
        if len(headers) > 8:
            key_fields += f" 等共 {col_count} 个字段"
        lines.append(f"> - **核心架构字段**：{key_fields}。")
        
        date_fields = [h for h in headers if "时间" in h or "日期" in h or "date" in h.lower()]
        status_fields = [h for h in headers if "状态" in h or "status" in h.lower()]
        amt_fields = [h for h in headers if "金额" in h or "价格" in h or "单价" in h or "amount" in h.lower() or "price" in h.lower()]

        features = []
        if date_fields:
            features.append(f"支持按 `{date_fields[0]}` 时间线回溯")
        if amt_fields:
            features.append(f"包含 `{amt_fields[0]}` 等财务指标")
        if status_fields:
            features.append(f"具备 `{status_fields[0]}` 履约追踪机制")
            
        if features:
            lines.append(f"> - **业务特征**：{'；'.join(features)}。")
            
        if hidden_rows > 0 or hidden_cols > 0:
            lines.append(f"> - **隐藏结构感知**：检测到 {hidden_rows} 行隐藏行、{hidden_cols} 列隐藏列（明细均已保留入底表）。")

        lines.append("> - **防卡顿外挂机制**：正文呈现前 10 行样本预览；完整底表已隔离导出为纯文本 CSV 附件，便于极速调用。")
        return "\n".join(lines)


def process_excel(filepath: str, threshold: int = 50, output_csv_dir: str = "99-Assets/10-Attachments"):
    """
    核心处理入口：提取所有 Sheet，区隔小表与大表，生成结构化 Payload
    """
    path = Path(filepath)
    wb_formula = None
    if path.suffix.lower() == ".csv":
        wb = openpyxl.Workbook()
        ws = wb.active; ws.title = "CSV"
        encoding = os.environ.get("GEWU_ATLAS_TEXT_ENCODING", "utf-8-sig")
        with path.open(encoding=encoding, newline="") as stream:
            for row in csv.reader(stream):
                ws.append(row)
    elif path.suffix.lower() == ".xls":
        import xlrd
        legacy = xlrd.open_workbook(filepath)
        wb = openpyxl.Workbook(); wb.remove(wb.active)
        for sheet in legacy.sheets():
            ws = wb.create_sheet(sheet.name)
            for values in sheet.get_rows():
                ws.append([xlrd.xldate_as_datetime(c.value, legacy.datemode) if c.ctype == xlrd.XL_CELL_DATE else c.value for c in values])
        legacy.release_resources()
    else:
        wb = openpyxl.load_workbook(filepath, data_only=True)
        try:
            wb_formula = openpyxl.load_workbook(filepath, data_only=False)
        except Exception:
            wb_formula = None
    
    sheets_meta = []
    total_data_rows = 0

    os.makedirs(output_csv_dir, exist_ok=True)
    slug = path.stem.replace(" ", "_")

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        is_hidden = (ws.sheet_state == "hidden")
        
        ws_formula = None
        if wb_formula and sheet_name in wb_formula.sheetnames:
            ws_formula = wb_formula[sheet_name]

        grid, hidden_rows, hidden_cols = parse_sheet_matrix(ws, ws_formula=ws_formula)
        if not grid:
            continue

        sheet_title, headers, data_rows = extract_headers_and_data(grid)
        row_count = len(data_rows)
        col_count = len(headers)
        total_data_rows += row_count

        scale = "small" if row_count <= threshold else "large"
        
        sheet_info = {
            "sheet_name": sheet_name,
            "is_hidden": is_hidden,
            "hidden_rows": hidden_rows,
            "hidden_cols": hidden_cols,
            "title": sheet_title,
            "row_count": row_count,
            "col_count": col_count,
            "headers": headers,
            "scale": scale,
            "data_rows": data_rows,
            "csv_filename": None
        }

        if scale == "large":
            csv_filename = f"{slug}_{sheet_name.replace(' ', '_')}.csv"
            csv_path = Path(output_csv_dir) / csv_filename
            with open(csv_path, "w", encoding="utf-8-sig", newline="") as cf:
                writer = csv.writer(cf)
                writer.writerow(headers)
                for r in data_rows:
                    writer.writerow([str(v) if v is not None else "" for v in r])
            sheet_info["csv_filename"] = csv_filename

        sheets_meta.append(sheet_info)

    payload = {
        "filename": path.name,
        "stem": path.stem,
        "total_sheets": len(sheets_meta),
        "total_data_rows": total_data_rows,
        "threshold": threshold,
        "sheets": sheets_meta
    }
    return payload


def generate_markdown(payload, original_file_name: str = None, out_path: Path = None) -> str:
    """
    根据 Payload 渲染最终结构化 Markdown 笔记（遵循 AGENTS.md）
    """
    stem = payload["stem"]
    total_sheets = payload["total_sheets"]
    total_rows = payload["total_data_rows"]
    today_str = datetime.now().strftime("%Y-%m-%d")
    status_val = get_status_for_domain(VAULT_ROOT, out_path=out_path)
    
    sheet_names = [s["sheet_name"] for s in payload["sheets"]]
    orig_name = original_file_name if original_file_name else f"{stem}.xlsx"

    safe_stem = fm_scalar(stem)
    lines = [
        "---",
        "doc_type: library",
        f'title: "{safe_stem}"',
        'source_type: excel',
        'channel: "行业研报"',
        f'summary: "{safe_stem} 多工作表数据结构化转译与统计分析，共 {total_sheets} 个工作表、{total_rows} 条记录"',
        f'original_file: "{fm_scalar(orig_name)}"',
        'processed: false',
        f'status: {status_val}',
        f'updated: {today_str}',
        'tags: [excel, spreadsheet, data]',
        f'sheet_count: {total_sheets}',
        f'total_rows: {total_rows}',
        f'sheets: {json.dumps(sheet_names, ensure_ascii=False)}',
        'extracted_to: []',
        "---",
        ""
    ]

    lines.append(f"# {stem}\n")
    lines.append("> [!ABSTRACT] 工作簿全景概览与直达导航")
    lines.append(f"> 本工作簿共包含 **{total_sheets}** 个工作表，合计 **{total_rows}** 条业务数据记录。物理原件已冷归档至 `98-Archives/{month_tag()}/{orig_name}`。")
    lines.append("> ")
    for idx, s in enumerate(payload["sheets"], 1):
        s_name = s["sheet_name"]
        scale_tag = "小表 (完整渲染)" if s["scale"] == "small" else "大表 (抽样预览+底表外挂)"
        lines.append(f"> {idx}. [[#📊 Sheet {idx}: {s_name}]] — {s['col_count']} 列 × {s['row_count']} 行（{scale_tag}）")
    lines.append("\n---\n")

    for idx, s in enumerate(payload["sheets"], 1):
        s_name = s["sheet_name"]
        scale = s["scale"]
        r_count = s["row_count"]
        c_count = s["col_count"]
        headers = s["headers"]
        data_rows = s["data_rows"]
        title = s.get("title")

        lines.append(f"## 📊 Sheet {idx}: {s_name}\n")
        if title:
            lines.append(f"**表内主标题**：*{title}*\n")

        summary_callout = generate_smart_summary(
            s_name, title, headers, data_rows, scale,
            hidden_rows=s.get("hidden_rows", 0),
            hidden_cols=s.get("hidden_cols", 0)
        )
        lines.append(summary_callout)
        lines.append("")

        if scale == "small":
            table_md = format_gfm_table(headers, data_rows)
            lines.append(table_md)
            lines.append("")
        else:
            csv_file = s.get("csv_filename")
            lines.append("### 🔍 前 10 行抽样预览")
            table_md = format_gfm_table(headers, data_rows, max_preview=10)
            lines.append(table_md)
            lines.append("")

            lines.append("> [!TIP] 完整底表数据下载与查阅")
            lines.append(f"> - 物理原件 Excel 留底：`{orig_name}`（存放于 `98-Archives/{month_tag()}/`）")
            if csv_file:
                lines.append(f"> - 伴生纯文本 CSV：[[99-Assets/10-Attachments/{csv_file}|下载 {s_name} 底表 (CSV)]]")
            lines.append("")

        lines.append("---\n")

    return "\n".join(lines)


def run_pipeline(
    input_path: str,
    output_path: str = None,
    threshold: int = 50,
    dump_json: bool = False,
    archive: bool = False,
    dry_run: bool = False,
    attachments: str = None
):
    """单文件处理管线"""
    vault_root = VAULT_ROOT
    path = Path(input_path).resolve()
    if not path.exists():
        print(f"[-] 文件不存在: {input_path}")
        return False

    in_dropzone = False
    try:
        rel = path.relative_to(vault_root)
        if len(rel.parts) > 0 and rel.parts[0] == "00-DropZone":
            in_dropzone = True
    except ValueError:
        pass

    if output_path:
        out_p = Path(output_path).resolve()
    else:
        if in_dropzone:
            out_p = vault_root / "20-AI-Zone/20-Memory/21-Library" / f"{path.stem}.md"
        else:
            out_p = path.with_suffix(".md")

    should_archive = archive or in_dropzone
    if os.environ.get("GEWU_ATLAS_MANAGED_FMT") == "1":
        should_archive = False
    att_dir = Path(attachments).resolve() if attachments else (vault_root / "99-Assets/10-Attachments")
    archive_dir = monthly_archive_dir(vault_root / "99-Assets/98-Archives")

    if dry_run:
        print(f"[*] [DryRun] 待处理文件: {path.name}")
        print(f"    - 输入路径: {path}")
        print(f"    - 目标伴生卡片: {out_p}")
        if should_archive:
            print(f"    - 计划冷归档原件至: {archive_dir}/{path.name}")
        return True

    if out_p.exists():
        print(f"[COLLISION] 目标卡片已存在: {out_p}", file=sys.stderr)
        return False

    out_p.parent.mkdir(parents=True, exist_ok=True)
    payload = process_excel(str(path), threshold=threshold, output_csv_dir=str(att_dir))

    if dump_json:
        json_out = str(out_p.with_suffix(".json"))
        with open(json_out, "w", encoding="utf-8") as jf:
            json.dump(payload, jf, ensure_ascii=False, indent=2)
        print(f"[+] 已导出中间 JSON Payload: {json_out}")

    md_content = generate_markdown(payload, original_file_name=path.name, out_path=out_p)
    out_p.write_text(md_content, encoding="utf-8")
    print(f"[+] 成功转换并生成 Markdown 笔记: {out_p}")

    if should_archive:
        archive_dir.mkdir(parents=True, exist_ok=True)
        dest_excel = archive_dir / path.name
        n = 1
        while dest_excel.exists() and dest_excel.resolve() != path.resolve():
            dest_excel = archive_dir / f"{path.stem}_{n}{path.suffix}"
            n += 1
        shutil.move(str(path), str(dest_excel))
        print(f"  [✓] 原件已安全冷归档至: 98-Archives/{month_tag()}/{dest_excel.name}")

    return payload


def run_dropzone_batch(dropzone_dir: str = None, threshold: int = 50, dry_run: bool = False):
    """扫描 DropZone 自动化处理批量 Excel 文档"""
    vault_root = VAULT_ROOT
    dz = (vault_root / "00-DropZone") if not dropzone_dir else Path(dropzone_dir).resolve()
    if not dz.exists():
        print(f"[-] DropZone 目录不存在: {dz}")
        return

    excel_files = []
    for f in sorted(dz.rglob("*")):
        rel_parts = f.relative_to(dz).parts
        if any(part == "91-Temp" or part.startswith(".") or part.startswith("_") for part in rel_parts):
            continue
        if f.is_file() and f.suffix.lower() in (".xlsx", ".xls") and not f.name.startswith("~$"):
            excel_files.append(f)

    if not excel_files:
        print("[*] 00-DropZone/ 下未发现待处理 Excel 文件（已自动跳过 91-Temp/ 与 _ 草稿区）。")
        return

    archive_dir = monthly_archive_dir(vault_root / "99-Assets/98-Archives")
    library_dir = vault_root / "20-AI-Zone/20-Memory/21-Library"

    print(f"[*] 发现 {len(excel_files)} 个待处理 Excel 文档，准备执行流转...")
    for ef in excel_files:
        slug = ef.stem
        out_md = library_dir / f"{slug}.md"

        if dry_run:
            print(f"  [DryRun] 计划处理: {ef.relative_to(vault_root)}")
            print(f"    - 伴生卡片: 20-AI-Zone/20-Memory/21-Library/{slug}.md")
            print(f"    - 原件冷归档: 98-Archives/{month_tag()}/{ef.name}")
            continue

        print(f"\n[*] 正在转换: {ef.name}")
        payload = run_pipeline(str(ef), str(out_md), threshold=threshold, dump_json=False, archive=True, dry_run=False)
        print(f"  [✓] 伴生卡片已生成: 20-AI-Zone/20-Memory/21-Library/{slug}.md")


def main():
    parser = argparse.ArgumentParser(description="智能 Excel (.xlsx / .xls) 文档解析与 Markdown 结构化转换引擎 (V1)")
    parser.add_argument("input", nargs="?", help="输入的 Excel 文件路径 (与 -i/--input 等效)")
    parser.add_argument("-i", "--input-file", dest="alt_input", help="输入的 Excel 文件路径")
    parser.add_argument("-o", "--output", help="输出 Markdown 目标路径 (默认: 20-AI-Zone/20-Memory/21-Library/{slug}.md 或 {input}.md)")
    parser.add_argument("--attachments", help="大表底表 CSV 或伴生附件输出目录 (默认: 99-Assets/10-Attachments)")
    parser.add_argument("--threshold", type=int, default=50, help="大小表行数划分阈值 (默认: 50)")
    parser.add_argument("--dump-json", action="store_true", help="是否导出供 AI 消费的中间 JSON Payload")
    parser.add_argument("--dropzone", action="store_true", help="扫描 00-DropZone/ 下所有 Excel 文档并执行静默归档转换")
    parser.add_argument("--archive", action="store_true", help="转换完成后将原 Excel 文件移动冷归档至 99-Assets/98-Archives/")
    parser.add_argument("--dry-run", action="store_true", help="演练模式：仅打印计划操作与路径，不修改文件")

    args = parser.parse_args()

    if args.dropzone:
        run_dropzone_batch(threshold=args.threshold, dry_run=args.dry_run)
    else:
        target_in = args.alt_input if args.alt_input else args.input
        if not target_in:
            parser.print_help()
            sys.exit(1)
        ok = run_pipeline(
            target_in,
            args.output,
            threshold=args.threshold,
            dump_json=args.dump_json,
            archive=args.archive,
            dry_run=args.dry_run,
            attachments=args.attachments
        )
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
