#!/usr/bin/env python3
"""
pdf_converter.py - PDF 智能解析与 Markdown 结构化转换引擎 (V5 全模态双引擎版)

全面支持与满足用户核心准则与 AGENTS.md 规范：
【引擎 A：长篇研报与学术论文 (Document/Paper Pipeline)】
1. 【不留目录】：自动识别并彻底剥离目录页与前置跳页。
2. 【不分页，不留页码】：剔除顶部/底部页码与 <!-- Page --> 分页符，生成连续流畅正文。
3. 【1~6 级标题深度识别】：融合数字层级（1. / 1.1 / 1.1.1 / 1.1.1.1）与字号加粗特征，精准划分 H1~H6。
4. 【代码块与特殊符号全保护 (Code Block Shield)】：
   - 物理几何 + 等宽字型 (Monospace) + 语法特征三重探测。
   - 彻底杜绝代码内 # 误判为标题、- 误判为无序列表、| 误判为表格。
   - 保持代码缩进与空行，自动识别语言包裹 ```lang 代码栅格。
5. 【段落智能重流 (Paragraph Reflow)】：消除 PDF 换行硬回车，中文无缝平滑拼接，修复杂乱跨页断词。
6. 【跨页表格无缝缝合 (Multi-Page Stitching)】：自动识别跨页表格断点，缝合为一个完整表格。
7. 【复杂表格智能平铺转 Markdown (Forward-Fill)】：自动补齐跨行合并单元格，输出 100% 可检索、兼容 Advanced Tables 的标准 Markdown 表格。
8. 【滑动窗口 (Window=2) 流式组装】：按 2000~4000 字符切片，严格保护表格与代码块完整性，以窗口大小为 2 进行上下文精修与流式追加写入。

【引擎 B：PPT 幻灯片与演示类 PDF (Presentation Pipeline)】
1. 【自动横屏与高宽比探测】：识别 16:9 / 4:3 演示比例（如 960x540）。
2. 【每页 150 DPI 高清全屏渲染】：自动导出至 99-Assets/10-Attachments/{slug}_slide_{idx}.jpg 并原位内嵌。
3. 【每页核心要点与大字指标萃取】：过滤页脚与页码杂音，将短语、大字指标自动精修为标准列表。
4. 【竞品对比与结构化矩阵识别】：自动将横向卡片与对比矩阵转为标准 GFM 文本表格。
5. 【KaTeX 安全转义】：转义孤立美元符号（$ -> \\$），防止数学公式渲染混乱。
"""

__version__ = "1.1"
import os
import re
import sys
import json
import shutil
import argparse
import tempfile
from pathlib import Path
from datetime import datetime
from collections import Counter

import md_table
import heading_resolver
from archive_month import monthly_archive_dir, month_tag

_CONVERTERS_DIR = Path(__file__).resolve().parent
if str(_CONVERTERS_DIR) not in sys.path:
    sys.path.insert(0, str(_CONVERTERS_DIR))
from core.project import get_status_for_domain
from core.common import fm_scalar

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

try:
    import fitz  # PyMuPDF
except ImportError:
    print("[-] 错误: 未安装 PyMuPDF。请运行: pip install pymupdf")
    sys.exit(1)

def is_cjk(ch):
    """判断字符是否属于中日韩统一表意文字"""
    return "\u4e00" <= ch <= "\u9fff"

def is_presentation_pdf(doc) -> bool:
    """自动判断是否为 PPT/Keynote 导出的横屏演示类 PDF"""
    if len(doc) == 0:
        return False
    landscape_count = sum(1 for p in doc if p.rect.width / p.rect.height >= 1.2)
    return (landscape_count / len(doc)) >= 0.75

def is_toc_page(page):
    """判断页面是否为目录页 (Table of Contents)"""
    text = page.get_text()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return False
        
    if any(re.match(r"^(目\s*录|Table of Contents)$", l) for l in lines[:3]):
        return True
        
    toc_dot_lines = sum(1 for l in lines if re.search(r"([.·…]{3,}\s*\d+|\.{4,}\s*\d+)", l))
    if len(lines) >= 4 and (toc_dot_lines / len(lines)) >= 0.35:
        return True
        
    return False

def is_page_number(text, bbox, page_height):
    """判断文本块是否为页眉/页脚页码"""
    clean = text.strip()
    is_num_pattern = bool(re.match(r"^([-—~]\s*)?\d+(\s*[-—~])?$", clean) or re.match(r"^第\s*\d+\s*页$", clean))
    if is_num_pattern:
        if bbox[1] < 55 or bbox[3] > page_height - 60:
            return True
    return False

def detect_repeating_header_footer(doc):
    """跨页重复页眉页脚检测（TICKET-20261102T1141）：
    按「规范化文本 + 固定纵向位置」几何特征聚类，
    出现页数 ≥ max(5, 总页数 40%) 的页顶/页底块判定为页眉/页脚，返回 (page_num, y0桶) 剔除集合。
    纯版式信号非语义猜测：正文行不可能在多数页面以相同 y 坐标重复出现；短文档不触发防误剥。
    扩展检测区域：覆盖页面顶部 35% 和底部 15%，以捕获非传统页眉（如 NDI 文档编号页）。
    """
    n_pages = len(doc)
    if n_pages < 5:
        return {}
    min_pages = max(5, int(n_pages * 0.4))
    # (norm_text, y桶, 区域) -> set(page_num)
    clusters = {}
    for pno, page in enumerate(doc):
        ph = page.rect.height
        for b in page.get_text("dict").get("blocks", []):
            if b.get("type") != 0:
                continue
            lines = b.get("lines", [])
            if not lines:
                continue
            bbox = b.get("bbox", [0, 0, 0, 0])
            y0, y1 = bbox[1], bbox[3]
            # 扩展区域：顶部 35%（捕获非传统页眉）+ 底部 15%（捕获页脚）
            region = "top" if y0 < ph * 0.35 else ("bottom" if y1 > ph * 0.85 else None)
            if not region:
                continue
            text = "".join(s.get("text", "") for l in lines for s in l.get("spans", []))
            norm = re.sub(r"\s+", "", text)
            if not norm:
                continue
            key = (norm, round(y0 / 5.0), region)
            clusters.setdefault(key, set()).add(pno)
    # 剔除集合：key -> True；供主循环按 (page_num, norm_text, y0桶, region) 查询
    to_remove = {k for k, pages in clusters.items() if len(pages) >= min_pages}
    return to_remove

def _is_header_footer_block(text, bbox, page_num, page_height, remove_set):
    """主循环侧查询：块是否命中跨页重复页眉/页脚剔除集合"""
    if not remove_set:
        return False
    y0, y1 = bbox[1], bbox[3]
    # 扩展区域：顶部 35% + 底部 15%（与 detect_repeating_header_footer 保持一致）
    region = "top" if y0 < page_height * 0.35 else ("bottom" if y1 > page_height * 0.85 else None)
    if not region:
        return False
    norm = re.sub(r"\s+", "", text)
    if not norm:
        return False
    return (norm, round(y0 / 5.0), region) in remove_set

def detect_body_font_size(doc):
    """统计全文字号分布直方图，返回最高频出现的字号作为基准正文字号"""
    sizes = Counter()
    for page in doc:
        blocks = page.get_text("dict").get("blocks", [])
        for b in blocks:
            if b.get("type") == 0:
                for line in b.get("lines", []):
                    for span in line.get("spans", []):
                        txt = span.get("text", "").strip()
                        if txt:
                            sz = round(span.get("size", 12.0), 1)
                            sizes[sz] += len(txt)
    if sizes:
        dominant_size, _ = sizes.most_common(1)[0]
        return dominant_size
    return 12.0

def detect_code_language(code_text: str) -> str:
    """自动探测代码段的编程语言"""
    clean = code_text.strip()
    if clean.startswith("#!/") or "grep " in clean or "docker run" in clean or "awk " in clean:
        return "bash"
    if "#include " in clean or "#define " in clean or "std::" in clean or "int main(" in clean:
        return "cpp"
    if "import " in clean or "def " in clean or "class " in clean:
        return "python"
    if any(k in clean.upper() for k in ("SELECT ", "CREATE TABLE ", "INSERT INTO ", "UPDATE ", "LEFT JOIN ")):
        return "sql"
    if (clean.startswith("{") and clean.endswith("}")) or (clean.startswith("[") and clean.endswith("]")):
        return "json"
    return ""

def is_code_syntax(text: str) -> bool:
    """基于特征词语法探测是否为代码片段"""
    clean = text.strip()
    if clean.startswith(("#!/", "#include <", "#include \"", "#define ")):
        return True
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) >= 2:
        if any(l.strip().startswith(("import ", "from ", "def ", "class ", "return ")) for l in lines) and any((":" in l or "(" in l or "=" in l or "{" in l) for l in lines):
            return True
        if any(l.strip().startswith(("SELECT ", "FROM ", "WHERE ", "INSERT INTO ", "CREATE TABLE ")) for l in lines):
            return True
        if any(l.strip().startswith(("docker ", "kubectl ", "npm ", "git ", "pip ", "set -")) for l in lines):
            return True
    return False

# ============ 列项标记字典（GB/T 1.1-2020 列项 a)/1)/——/·，TICKET-20261102T1103） ============
# 标题层级判定已上收至 heading_resolver 的文档级三档裁定（TICKET-20261102T1636），
# 此处仅保留与标题无关的列项版式识别。
LIST_LETTER_RE = re.compile(r"^([a-z])\)(?:\s+(.*))?$")
LIST_NUM_RE = re.compile(r"^(\d{1,2})\)(?:\s+(.*))?$")
# 句子终止标点：候选标题熔断用（正文断行特征）
_SENTENCE_TAIL = ("。", "！", "？", "；", "…", ";", "，", ",")

LIST_DASH_RE = re.compile(r"^(?:——|·)\s*(.*)$")

def format_markdown_table_with_forward_fill(data):
    """将表格数据转换为标准 Markdown 表格并执行向下的 Forward-Fill"""
    if not data or len(data) < 1:
        return ""
        
    cleaned_rows = []
    max_cols = 0
    for r in data:
        row = [md_table.flatten_cell(str(c)) if c is not None else "" for c in r]
        if any(row):
            cleaned_rows.append(row)
            if len(row) > max_cols:
                max_cols = len(row)
                
    if not cleaned_rows:
        return ""
        
    for r in cleaned_rows:
        while len(r) < max_cols:
            r.append("")
            
    header = cleaned_rows[0]
    data_rows = cleaned_rows[1:]
    
    if data_rows:
        for col_idx in range(min(2, max_cols)):
            last_val = ""
            for r in data_rows:
                if r[col_idx] and r[col_idx] != "--":
                    last_val = r[col_idx]
                else:
                    r[col_idx] = last_val

    esc = lambda s: s.replace("|", "\\|")
    return md_table.emit_gfm([esc(h) for h in header],
                             [[esc(c) for c in r] for r in data_rows])

def refine_chunk_text(target_text: str, context_text: str = "") -> str:
    """毛坯房物理重流：保持 100% 文字原样，保护代码块与表格，消除多余空行。

    语义格式（如枚举列表拆解、标题序号半角空格、加粗提炼等）交由智能体基于 Window=2 精修。
    """
    lines = target_text.splitlines()
    refined_lines = []
    in_code_block = False

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("```"):
            in_code_block = not in_code_block
            refined_lines.append(line)
            continue

        if in_code_block:
            refined_lines.append(line)
            continue

        if not stripped:
            refined_lines.append("")
            continue

        # 保留图片占位、标题与表格
        if stripped.startswith(("#", "![[", "|")):
            refined_lines.append(stripped)
            continue

        refined_lines.append(stripped)

    result = "\n".join(refined_lines)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    return result

MAX_PDF_BYTES = 512 * 1024 * 1024


def _open_validated_pdf(pdf_path: Path, max_bytes: int = MAX_PDF_BYTES):
    """Validate input PDF existence, size, and readability before opening with PyMuPDF."""
    p = Path(pdf_path).resolve()
    if not p.exists() or not p.is_file():
        raise ValueError(f"PDF 输入文件不存在或非常规文件: {p}")
    size = p.stat().st_size
    if size <= 0:
        raise ValueError(f"PDF 输入文件为空: {p}")
    if size > max_bytes:
        raise ValueError(f"PDF 输入文件超过大小上限 ({max_bytes // (1024 * 1024)} MiB): {p}")
    try:
        doc = fitz.open(p)
    except Exception as exc:
        raise ValueError(f"无法解析 PDF 文件 ({p.name}): {exc}") from exc
    if len(doc) == 0:
        doc.close()
        raise ValueError(f"PDF 不含任何页面: {p}")
    return doc


def _cleanup_created_files(paths) -> None:
    for item in paths:
        try:
            if item.exists():
                item.unlink()
        except OSError:
            pass


# ==============================================================================
# 引擎 B：PPT 幻灯片与演示类 PDF 专用处理流水线
# ==============================================================================
def convert_presentation_to_markdown(
    pdf_path: Path,
    output_md_path: Path,
    attachments_dir: Path,
    dpi: int = 150,
    skip_decorative: bool = False,
    decorative_size_kb: int = 5,
    decorative_dim_px: int = 100,
    annotate_images: bool = False
):
    """
    针对横向幻灯片 (PPT/BP/Slide) 执行专属解析：
    每页截取超清全图 + 抽取文字卡片与指标 + 竞品表格转 Markdown
    """
    pdf_path = Path(pdf_path).resolve()
    output_md_path = Path(output_md_path).resolve()
    attachments_dir = Path(attachments_dir).resolve()
    if output_md_path.exists():
        print(f"[COLLISION] 目标卡片已存在: {output_md_path}", file=sys.stderr)
        return None
    doc = _open_validated_pdf(pdf_path)
    doc_slug = pdf_path.stem
    attachments_dir.mkdir(parents=True, exist_ok=True)
    output_md_path.parent.mkdir(parents=True, exist_ok=True)
    created_attachments = []
    tmp_md = None
    try:
        print(f"[*] 启动【PPT 演示文稿专用流转引擎】...")
        print(f"    - 输入文件: {pdf_path.name}")
        print(f"    - 总幻灯片数: {len(doc)} 页")

        today_str = datetime.now().strftime("%Y-%m-%d")
        status_val = get_status_for_domain(VAULT_ROOT, out_path=output_md_path)

        # 标题探测
        first_title = doc_slug

        safe_title = fm_scalar(first_title)
        frontmatter = f"""---
doc_type: library
title: "{safe_title}"
source_type: presentation
channel: "官方白皮书"
summary: "PPT演示文稿结构化转译，共 {len(doc)} 页高清切片与关键要点"
original_file: "{fm_scalar(pdf_path.name)}"
total_slides: {len(doc)}
processed: false
status: {status_val}
updated: {today_str}
tags: [presentation, ppt, pdf, slide]
extracted_to: []
---

# {first_title}

> [!NOTE] 演示文档速览
> 本文由 PPT 演示文稿 PDF 原生转译。全篇共 {len(doc)} 页幻灯片，每页附带 {dpi} DPI 高清视觉截图与结构化提炼要点，支持全文检索与高级表格编辑。

"""
        sections = [frontmatter]

        for p_idx, page in enumerate(doc):
            # 1. 渲染当前页高清截图并存入附件库
            slide_filename = f"{doc_slug}_slide_{p_idx+1:02d}.jpg"
            slide_filepath = attachments_dir / slide_filename
            if not slide_filepath.exists():
                pix = page.get_pixmap(dpi=dpi)
                pix.save(slide_filepath, jpg_quality=85)
                created_attachments.append(slide_filepath)

            # 2. 收集文字与字号层级
            blocks = page.get_text("dict").get("blocks", [])
            text_blocks = []
            header_spans = []
            all_spans = []

            for b in blocks:
                if b.get("type") == 0:
                    lines = []
                    for l in b.get("lines", []):
                        line_str = "".join(s.get("text", "") for s in l.get("spans", [])).strip()
                        if line_str and not line_str.isdigit() and not line_str.startswith("©") and not line_str.startswith("(c)"):
                            lines.append(line_str)
                            for s in l.get("spans", []):
                                t = s.get("text", "").strip()
                                if t and not t.isdigit() and not t.startswith("©") and not t.startswith("(c)"):
                                    all_spans.append((s.get("size", 0), s.get("bbox", [0,0,0,0])[1], t))
                                    if s.get("bbox", [0,0,0,0])[1] < 120 and s.get("size", 0) >= 14:
                                        header_spans.append((s.get("size", 0), s.get("bbox", [0,0,0,0])[1], t))
                    if lines:
                        text_blocks.append((b.get("bbox", [0,0,0,0]), lines))

            # 3. 确定标题
            if header_spans:
                header_spans.sort(key=lambda x: (-x[0], x[1]))
                title = header_spans[0][2]
            elif all_spans:
                all_spans.sort(key=lambda x: (-x[0], x[1]))
                title = all_spans[0][2]
            elif p_idx == 0:
                title = "封面 / 标题页"
            else:
                title = "视觉展示页"

            # 4. 判断是否为矩阵对比表格 (例如 Slide 23 竞品对比)
            is_table_slide = False
            table_lines = []
            if any(any("核心维度" in l or "维度" in l for l in tb[1]) for tb in text_blocks):
                rows = []
                for tb in text_blocks:
                    lines = tb[1]
                    if len(lines) >= 3 and any(k in lines[0] for k in ("核心维度", "标准", "产品", "工程", "商业", "标杆")):
                        rows.append(lines)
                if len(rows) >= 3:
                    is_table_slide = True
                    max_c = max(len(r) for r in rows)
                    header = rows[0]
                    sep = ["---"] * max_c
                    table_lines.append("| " + " | ".join(header) + " |")
                    table_lines.append("| " + " | ".join(sep) + " |")
                    for r in rows[1:]:
                        while len(r) < max_c:
                            r.append("")
                        table_lines.append("| " + " | ".join(r) + " |")

            # 5. 组装当前幻灯片 Markdown
            slide_parts = [f"## Slide {p_idx+1:02d}: {title}\n"]
            slide_parts.append(f"![[{slide_filename}]]\n")

            if is_table_slide:
                slide_parts.append("### 差异化对比矩阵\n")
                slide_parts.append("\n".join(table_lines) + "\n")
            else:
                points = []
                seen = set()
                for tb in text_blocks:
                    content = " ".join(tb[1])
                    if content == title or content in seen:
                        continue
                    seen.add(content)
                    # 转义孤立的美元符号，防止 KaTeX 渲染报错
                    safe_content = content.replace("$", "\\$")
                    if len(tb[1]) == 1:
                        line = tb[1][0].replace("$", "\\$")
                        if line.endswith(("万亿+", "亿+", "%", "EB", "年", "C O N T E N T S", "C H A P T E R")):
                            points.append(f"- **{line}**")
                        else:
                            points.append(f"- {line}")
                    else:
                        first_l = tb[1][0].replace("$", "\\$")
                        rest_l = " ".join(tb[1][1:]).replace("$", "\\$")
                        if len(first_l) <= 15:
                            points.append(f"- **{first_l}**：{rest_l}")
                        else:
                            points.append(f"- {safe_content}")

                if points:
                    slide_parts.append("### 幻灯片要点\n" + "\n".join(points) + "\n")

            sections.append("\n".join(slide_parts))
            print(f"  [✓] Slide {p_idx+1:02d}/{len(doc):02d} 转译完成: {title[:35]}")

        if output_md_path.exists():
            _cleanup_created_files(created_attachments)
            print(f"[COLLISION] 目标卡片已存在: {output_md_path}", file=sys.stderr)
            return None
        fd, tmp_name = tempfile.mkstemp(prefix=f".{output_md_path.stem}-", suffix=".tmp", dir=output_md_path.parent)
        tmp_md = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n\n".join(sections) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_md, output_md_path)
        tmp_md = None
        print(f"\n[★] PPT 幻灯片解析与截图转译完成！")
        print(f"    - 输出文档: {output_md_path}")
        print(f"    - 幻灯片截图存储于: {attachments_dir.name}/")
        return output_md_path
    except Exception:
        if tmp_md is not None and tmp_md.exists():
            tmp_md.unlink()
        _cleanup_created_files(created_attachments)
        raise
    finally:
        if not getattr(doc, "is_closed", False):
            doc.close()

# ==============================================================================
# 引擎 A：长篇研报与学术论文通用流水线
# ==============================================================================
def convert_document_to_markdown(
    pdf_path: Path,
    output_md_path: Path,
    attachments_dir: Path,
    dpi: int = 200,
    target_chunk_size: int = 3000,
    skip_decorative: bool = False,
    decorative_size_kb: int = 5,
    decorative_dim_px: int = 100,
    annotate_images: bool = False
):
    """长篇技术文档/论文解析（含输入校验、临时文件原子落盘与异常附件回滚）"""
    pdf_path = Path(pdf_path).resolve()
    attachments_dir = Path(attachments_dir).resolve()
    output_md_path = Path(output_md_path).resolve()
    if output_md_path.exists():
        print(f"[COLLISION] 目标卡片已存在: {output_md_path}", file=sys.stderr)
        return None

    doc = _open_validated_pdf(pdf_path)
    attachments_dir.mkdir(parents=True, exist_ok=True)
    output_md_path.parent.mkdir(parents=True, exist_ok=True)
    created_attachments = []
    tmp_md_box = []
    try:
        return _convert_document_with_open_doc(
            doc=doc,
            pdf_path=pdf_path,
            output_md_path=output_md_path,
            attachments_dir=attachments_dir,
            created_attachments=created_attachments,
            tmp_md_box=tmp_md_box,
            dpi=dpi,
            target_chunk_size=target_chunk_size,
            skip_decorative=skip_decorative,
            decorative_size_kb=decorative_size_kb,
            decorative_dim_px=decorative_dim_px,
            annotate_images=annotate_images,
        )
    except Exception:
        for t in tmp_md_box:
            if t is not None and t.exists():
                t.unlink()
        _cleanup_created_files(created_attachments)
        raise
    finally:
        if not getattr(doc, "is_closed", False):
            doc.close()


def _convert_document_with_open_doc(
    *,
    doc,
    pdf_path: Path,
    output_md_path: Path,
    attachments_dir: Path,
    created_attachments: list,
    tmp_md_box: list,
    dpi: int = 200,
    target_chunk_size: int = 3000,
    skip_decorative: bool = False,
    decorative_size_kb: int = 5,
    decorative_dim_px: int = 100,
    annotate_images: bool = False
):
    doc_slug = pdf_path.stem
    body_font_size = detect_body_font_size(doc)
    
    print(f"[*] 启动【长篇研报与学术论文流转引擎】: {pdf_path.name}")
    print(f"[*] 总页数: {len(doc)}, 基准正文字号: {body_font_size}pt")
    
    # 阶段 1: 全局表格预扫描
    page_tables = {}
    for p_idx, page in enumerate(doc):
        page_tables[p_idx] = []
        try:
            for t in page.find_tables().tables:
                page_tables[p_idx].append({
                    "bbox": t.bbox,
                    "col_count": t.col_count,
                    "data": [list(row) for row in t.extract()]
                })
        except Exception:
            page_tables[p_idx] = []

    table_data_map = {}
    is_continuation = set()
    for p_idx in range(len(doc)):
        tables = page_tables.get(p_idx, [])
        for t_idx, t_info in enumerate(tables):
            key = (p_idx, t_idx)
            if key in is_continuation:
                continue
            curr_data = [list(r) for r in t_info["data"]]
            if p_idx + 1 < len(doc) and t_info["bbox"][3] > doc[p_idx].rect.height - 110:
                next_tables = page_tables.get(p_idx + 1, [])
                if next_tables:
                    t_next = next_tables[0]
                    if t_next["bbox"][1] < 110 and t_next["col_count"] == t_info["col_count"]:
                        next_key = (p_idx + 1, 0)
                        next_data = t_next["data"]
                        if next_data:
                            if next_data[0] == curr_data[0]:
                                curr_data.extend(next_data[1:])
                            else:
                                curr_data.extend(next_data)
                        is_continuation.add(next_key)
                        print(f"  [☍] 成功检测并缝合跨页表格: Page {p_idx + 1} -> Page {p_idx + 2} (合并为 {len(curr_data)} 行)")
            table_data_map[key] = curr_data

    # 阶段 1.5: 跨页重复页眉页脚预扫描（TICKET-20261102T1141）
    hf_remove_set = detect_repeating_header_footer(doc)
    if hf_remove_set:
        print(f"  [☒] 检测到 {len(hf_remove_set)} 类跨页重复页眉/页脚，将在主循环中剔除")

    # 阶段 2: 逐页解析
    raw_blocks = []
    image_global_idx = 0
    skipped_toc_pages = 0
    cover_max = (0.0, "")  # 封面页最大字号块，作文档标题兜底

    for p_idx, page in enumerate(doc):
        page_num = p_idx + 1
        page_height = page.rect.height

        if p_idx < 4 and is_toc_page(page):
            skipped_toc_pages += 1
            print(f"  [-] 自动跳过目录页: Page {page_num}")
            continue

        page_elements = []

        code_boxes = []
        for d in page.get_drawings():
            fill = d.get("fill")
            rect = d.get("rect")
            if fill and isinstance(fill, (list, tuple)) and len(fill) >= 3:
                if all(0.85 <= c <= 0.99 for c in fill[:3]) and rect.width > 120 and rect.height > 20:
                    code_boxes.append(rect)

        table_bboxes = []
        for t_idx, t_info in enumerate(page_tables.get(p_idx, [])):
            key = (p_idx, t_idx)
            table_bboxes.append(t_info["bbox"])
            if key in is_continuation:
                continue
            t_data = table_data_map.get(key, t_info["data"])
            md_table = format_markdown_table_with_forward_fill(t_data)
            if md_table:
                page_elements.append((t_info["bbox"][1], t_info["bbox"][0], "table", md_table))

        try:
            for img_info in page.get_images():
                xref = img_info[0]
                rects = page.get_image_rects(xref)
                if not rects:
                    continue
                img_bbox = rects[0]
                if img_bbox.width < 25 or img_bbox.height < 25:
                    continue
                
                # 装饰性图标过滤（TICKET-20261102T1212）
                if skip_decorative:
                    is_decorative = False
                    # 尺寸阈值检查
                    if img_bbox.width < decorative_dim_px and img_bbox.height < decorative_dim_px:
                        # 尝试提取图片数据检查文件大小
                        try:
                            base_img_check = doc.extract_image(xref)
                            img_data_size_kb = len(base_img_check.get("image", b"")) / 1024
                            if img_data_size_kb < decorative_size_kb:
                                is_decorative = True
                                print(f"  [☒] 过滤装饰性图标: {img_bbox.width:.0f}x{img_bbox.height:.0f}px, {img_data_size_kb:.1f}KB (来自 Page {page_num})")
                        except Exception:
                            pass
                    if is_decorative:
                        continue
                
                image_global_idx += 1

                # 升级：优先基于页面视口矩形进行 200 DPI 高清光栅化合成渲染，彻底杜绝 smask 蒙版丢失与透明背景黑化
                img_filename = f"{doc_slug}_img_{image_global_idx}.png"
                img_path = attachments_dir / img_filename
                cnt = 1
                while img_path.exists():
                    img_filename = f"{doc_slug}_img_{image_global_idx}_{cnt}.png"
                    img_path = attachments_dir / img_filename
                    cnt += 1
                rendered_ok = False
                try:
                    pix = page.get_pixmap(clip=img_bbox, dpi=200)
                    if pix.alpha:
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    pix.save(str(img_path))
                    created_attachments.append(img_path)
                    rendered_ok = True
                except Exception as render_err:
                    print(f"  [!] 页面视口裁剪渲染降级 ({render_err})，回退到原始流提取")

                if not rendered_ok:
                    base_img = doc.extract_image(xref)
                    fallback_ext = base_img.get("ext", "png")
                    img_filename = f"{doc_slug}_img_{image_global_idx}.{fallback_ext}"
                    img_path = attachments_dir / img_filename
                    cnt = 1
                    while img_path.exists():
                        img_filename = f"{doc_slug}_img_{image_global_idx}_{cnt}.{fallback_ext}"
                        img_path = attachments_dir / img_filename
                        cnt += 1
                    with open(img_path, "wb") as f_img:
                        f_img.write(base_img["image"])
                    created_attachments.append(img_path)

                # 图片引用：根据 --annotate-images 旗标决定是否生成空 callout 插槽
                if annotate_images:
                    img_ref = f"![[{img_filename}]]\n> [!NOTE] 图示解析与要素拆解\n> - **核心意图**：\n> - **图内要素与指标**：\n> - **逻辑机制 / 数据矩阵**："
                else:
                    img_ref = f"![[{img_filename}]]"
                page_elements.append((img_bbox.y0, img_bbox.x0, "image", img_ref))
                print(f"  [+] 提取内嵌插图: {img_filename} (来自 Page {page_num})")
        except Exception as e:
            print(f"  [!] Page {page_num} 图片抽取警告: {e}")

        try:
            p_dict = page.get_text("dict")
            for b in p_dict.get("blocks", []):
                if b.get("type") == 0:
                    bbox = fitz.Rect(b.get("bbox", (0, 0, 0, 0)))
                    cx = (bbox[0] + bbox[2]) / 2
                    cy = (bbox[1] + bbox[3]) / 2
                    in_table = any(tb[0] <= cx <= tb[2] and tb[1] <= cy <= tb[3] for tb in table_bboxes)
                    if in_table:
                        continue
                    
                    lines = b.get("lines", [])
                    if not lines:
                        continue
                        
                    raw_plain_text = "".join(s.get("text", "") for l in lines for s in l.get("spans", [])).strip()
                    if not raw_plain_text or is_page_number(raw_plain_text, bbox, page_height):
                        continue

                    # 跨页重复页眉/页脚剔除（TICKET-20261102T1141）
                    if _is_header_footer_block(raw_plain_text, bbox, page_num, page_height, hf_remove_set):
                        continue

                    span0 = lines[0]["spans"][0] if lines[0].get("spans") else {}
                    font_name = span0.get("font", "").lower()
                    flags = span0.get("flags", 0)
                    is_mono = bool(flags & 8) or any(f in font_name for f in ("mono", "cour", "consolas", "menlo", "dejavu", "typewriter", "fixed"))
                    in_code_box = any(bbox.intersects(box) for box in code_boxes)
                    is_syntax = is_code_syntax(raw_plain_text)

                    if is_mono or in_code_box or is_syntax:
                        block_lines = ["".join(s.get("text", "") for s in l.get("spans", [])) for l in lines]
                        code_block_text = "\n".join(block_lines)
                        page_elements.append((bbox.y0, bbox.x0, "code", code_block_text))
                        continue

                    font_size = span0.get("size", 12.0)
                    is_bold = bool(flags & 16) or ("bold" in font_name) or ("black" in font_name)
                    if page_num == 1 and font_size > cover_max[0] and len(raw_plain_text) <= 120:
                        cover_max = (font_size, raw_plain_text)

                    # 行级列项预判：GB/T 列项版式＝首行为 a)/1)/—— 标记、后续行为内容
                    # 先算出「非标题归宿」，候选被裁定为正文时按原形态回落，不丢列表结构
                    block_lines = ["".join(s.get("text", "") for s in l.get("spans", [])).strip() for l in lines]
                    block_lines = [bl for bl in block_lines if bl]
                    _first_is_marker = block_lines and bool(LIST_LETTER_RE.match(block_lines[0]) or LIST_NUM_RE.match(block_lines[0]) or LIST_DASH_RE.match(block_lines[0]))
                    n_list_lines = sum(1 for bl in block_lines if LIST_LETTER_RE.match(bl) or LIST_NUM_RE.match(bl) or LIST_DASH_RE.match(bl))
                    # 光杆标记行（整行恰为 a)/1)/——/·）≥2 是极强版式信号，不依赖首行与命中率（TICKET-20261102T1122）
                    _bare_marker_count = sum(1 for bl in block_lines if re.fullmatch(r"[a-z]\)|\d{1,2}\)|——|·", bl))
                    if block_lines and n_list_lines >= 2 and (n_list_lines >= len(block_lines) * 0.6 or _first_is_marker or _bare_marker_count >= 2):
                        fallback = (bbox.y0, bbox.x0, "list_block", block_lines)
                    else:
                        fallback = (bbox.y0, bbox.x0, "text", raw_plain_text)

                    # 层级是文档级属性：本处只投递候选，定层交予 doc.close() 之后的全局裁定趟
                    # （TICKET-20261102T1636，退役 get_heading_level / dict_heading_level 逐行启发式）
                    # 行数上限 4：MuPDF 会把短标题块碎成字符级行对象（如「参/考/文/献」4 行），
                    # 过紧会把文末「参考文献」这类碎块挡在候选门外；80 字上限仍拦住真段落。
                    if len(block_lines) <= 4 and len(raw_plain_text) <= 80 and not raw_plain_text.endswith(_SENTENCE_TAIL):
                        page_elements.append((bbox.y0, bbox.x0, "heading_candidate", dict(
                            fallback=fallback, text=raw_plain_text, size=font_size,
                            bold=is_bold, page=page_num)))
                    else:
                        page_elements.append(fallback)
        except Exception as e:
            print(f"  [!] Page {page_num} 文本分析异常: {e}")

        page_elements.sort(key=lambda x: (x[0], x[1]))

        merged_page_elements = []
        for elem in page_elements:
            if elem[2] == "code" and merged_page_elements and merged_page_elements[-1][2] == "code":
                prev = merged_page_elements[-1]
                merged_page_elements[-1] = (prev[0], prev[1], "code", prev[3] + "\n\n" + elem[3])
            else:
                merged_page_elements.append(elem)

        for elem in merged_page_elements:
            if elem[2] == "code":
                code_content = elem[3]
                lang = detect_code_language(code_content)
                formatted_code = f"```{lang}\n{code_content}\n```"
                raw_blocks.append((elem[0], elem[1], "code", formatted_code))
            elif elem[2] == "list_block":
                raw_blocks.append((elem[0], elem[1], "list_block", elem[3]))
            else:
                raw_blocks.append(elem)

    # 阶段 2.6: 文档级标题层级两档裁定（书签驱动 / 联合证据定层，TICKET-20261102T1636）
    # 书签是作者显式标注的权威层级，优先于任何字号/编号启发式
    bookmarks = [(lvl, title, pg) for lvl, title, pg in doc.get_toc() if title and title.strip()]
    doc_meta_title = ((doc.metadata or {}).get("title") or "").strip()
    raw_blocks, hdr = heading_resolver.resolve(raw_blocks, bookmarks, body_font_size)
    print(f"[+] 标题层级裁定：候选 {hdr['total']} · 书签 {hdr['locked']}/{hdr['toc']} 锚定"
          f"（覆盖 {hdr['coverage'] * 100:.0f}%·未锚定 {hdr['unanchored']}）"
          f" · 文法 {hdr['family']} · 字号 {hdr['font']}"
          f" · 降级正文 {hdr['demoted']}（页眉页脚 {hdr['framed']}）· 保序压缩 {hdr['compressed']}")
    if hdr["toc"] and hdr["coverage"] < 0.6:
        print(f"  [WARN] 内嵌书签覆盖不足 60%（{hdr['locked']}/{hdr['toc']}），层级已退回编号文法与字号聚类推定，标题树须人工复核")
    elif hdr["unanchored"]:
        print(f"  [WARN] 有 {hdr['unanchored']} 条书签未落到标题块（多为图注/表内标题或跨页断行），须比对骨架复核")
    if hdr["total"] and not hdr["locked"]:
        print("  [WARN] 本件无可用书签锚定，层级由启发式推定，二档精修时须逐节校核")

    # 阶段 3: 跨页断词与段落自然拼合
    merged_items = []
    curr_para = ""
    for blk_idx, item in enumerate(raw_blocks):
        itype = item[2]
        content = item[3]

        # 编号字典列项块：行级 list_block → Markdown 嵌套列表
        # 支持三种行形态：intro 前导行（落普通段落）、光杆标记行、标记+内容同行；
        # 标记项的后续连续非标记行全部并入该项（多行续内容，TICKET-20261102T1122）
        if itype == "list_block":
            if curr_para:
                merged_items.append({"type": "para", "content": curr_para})
                curr_para = ""
            blines = content  # list_block 的 content 是行数组
            def _is_marker(s):
                return bool(LIST_LETTER_RE.match(s) or LIST_NUM_RE.match(s) or LIST_DASH_RE.match(s))
            def _join_cjk(a, b):
                if not a: return b
                if not b: return a
                sep = " " if (a[-1].isascii() and a[-1].isalnum() and b[0].isascii() and b[0].isalnum()) else ""
                return a + sep + b
            intro_lines = []
            i = 0
            seen_marker = False
            while i < len(blines):
                c = blines[i].strip()
                m1 = LIST_LETTER_RE.match(c) or LIST_NUM_RE.match(c)
                m3 = LIST_DASH_RE.match(c)
                if m1 or m3:
                    seen_marker = True
                    if intro_lines:
                        merged_items.append({"type": "para", "content": "".join(intro_lines)})
                        intro_lines = []
                    body = (m1.group(2) or "") if m1 else (m3.group(1) or "")
                    depth = 2 if (m1 and LIST_NUM_RE.match(c)) else 1
                    j = i + 1
                    while j < len(blines) and not _is_marker(blines[j].strip()):
                        body = _join_cjk(body, blines[j].strip())
                        j += 1
                    merged_items.append({"type": "list_item", "marker": c[:4], "depth": depth, "content": body or c})
                    i = j
                else:
                    if not seen_marker:
                        intro_lines.append(c)  # 标记序列前导（如「上述流程如下：」）
                    else:
                        merged_items.append({"type": "para", "content": c})  # 序列后孤儿行兜底为段落
                    i += 1
            if intro_lines:
                merged_items.append({"type": "para", "content": "".join(intro_lines)})
            continue
        if itype == "text" and not content.strip():
            continue

        if itype == "text":
            if not curr_para:
                curr_para = content
            else:
                last_ch = curr_para[-1]
                first_ch = content[0]
                if last_ch not in ("。", "！", "？", "；", "…", "”", "’") and (is_cjk(last_ch) or is_cjk(first_ch)):
                    curr_para += content
                elif last_ch in ("。", "！", "？") and content.startswith(("1.", "2.", "3.", "（", "(", "①", "- ", "•")):
                    merged_items.append({"type": "para", "content": curr_para})
                    curr_para = content
                else:
                    if last_ch in ("，", "、", "：", ":", "（", "("):
                        curr_para += content
                    elif not is_cjk(last_ch) and not is_cjk(first_ch):
                        curr_para += " " + content
                    else:
                        if len(curr_para) < 100:
                            curr_para += content
                        else:
                            merged_items.append({"type": "para", "content": curr_para})
                            curr_para = content
        else:
            if curr_para:
                merged_items.append({"type": "para", "content": curr_para})
                curr_para = ""
            if itype == "heading":
                merged_items.append({"type": "heading", "level": content[0], "content": content[1]})
            elif itype == "table":
                merged_items.append({"type": "table", "content": content})
            elif itype == "image":
                merged_items.append({"type": "image", "content": content})
            elif itype == "code":
                merged_items.append({"type": "code", "content": content})

    if curr_para:
        merged_items.append({"type": "para", "content": curr_para})

    chunks = []
    curr_chunk_items = []
    curr_chunk_chars = 0

    for i, it in enumerate(merged_items):
        item_text_len = len(it.get("content", ""))
        is_safe_cut = (it["type"] == "heading" and it.get("level", 4) <= 3)
        if curr_chunk_chars >= 2200 and (is_safe_cut or curr_chunk_chars >= 3500):
            chunks.append(curr_chunk_items)
            curr_chunk_items = [it]
            curr_chunk_chars = item_text_len
        else:
            curr_chunk_items.append(it)
            curr_chunk_chars += item_text_len

    if curr_chunk_items:
        if len(chunks) > 0 and curr_chunk_chars < 800:
            chunks[-1].extend(curr_chunk_items)
        else:
            chunks.append(curr_chunk_items)

    print(f"[+] 文本语义切片完成，共切分 {len(chunks)} 个语意块，准备执行滑动窗口组装...")

    # 文档标题多源优先：内嵌元数据 > 语义化文件名 > 封面最大字号块 > 文件名 stem
    # 文件名是人类命名的语义锚点，须压过版式推断（封面常混入 TC609 这类机构大字标）；
    # 仅当文件名是纯编号/哈希（去数字后不足 4 字符）时才退回封面字号证据。
    _generic_meta = ("", "国家标准", "Microsoft Word", "Untitled", "无标题")
    _meta_ok = doc_meta_title not in _generic_meta and len(doc_meta_title) >= 6
    _slug_semantic = len(re.sub(r"[\d_\-.–—\s]+", "", doc_slug)) >= 4
    if _meta_ok:
        final_title = doc_meta_title
    elif _slug_semantic:
        final_title = doc_slug
    else:
        final_title = cover_max[1] or doc_slug
    today_str = datetime.now().strftime("%Y-%m-%d")
    status_val = get_status_for_domain(VAULT_ROOT, out_path=output_md_path)
    tags_list = ["paper", "pdf", "research"]
    has_low_coverage = bool(hdr.get("toc") and hdr.get("coverage", 1.0) < 0.6)
    has_no_bookmarks = bool(hdr.get("total") and not hdr.get("locked"))
    if has_low_coverage or has_no_bookmarks:
        status_val = "pending"
        tags_list.append("needs-review")
        reason = "内嵌书签覆盖不足 60%" if has_low_coverage else "无可用书签锚定（全凭启发式推定）"
        print(f"  [INFO] 书签缺失/低覆盖保护（{reason}）：产物卡片自动置位 status: pending 与 tags: [..., needs-review]，已移交待审卡片盒")
    
    safe_title = fm_scalar(final_title)
    tags_str = ", ".join(tags_list)
    frontmatter = f"""---
doc_type: library
title: "{safe_title}"
source_type: paper
channel: "行业研报"
summary: "{safe_title} 结构化长篇文献转译与深度排版"
original_file: "{fm_scalar(pdf_path.name)}"
processed: false
status: {status_val}
updated: {today_str}
tags: [{tags_str}]
extracted_to: []
---

"""
    if output_md_path.exists():
        _cleanup_created_files(created_attachments)
        print(f"[COLLISION] 目标卡片已存在: {output_md_path}", file=sys.stderr)
        return None
    fd, tmp_name = tempfile.mkstemp(prefix=f".{output_md_path.stem}-", suffix=".tmp", dir=output_md_path.parent)
    tmp_md = Path(tmp_name)
    tmp_md_box.append(tmp_md)
    with os.fdopen(fd, "w", encoding="utf-8") as f_out:
        f_out.write(frontmatter)
        prev_chunk_text = ""
        for idx, c in enumerate(chunks, 1):
            chunk_lines = []
            for it in c:
                if it["type"] == "heading":
                    hashes = "#" * it["level"]
                    chunk_lines.append(f"{hashes} {it['content']}\n")
                elif it["type"] == "list_item":
                    indent = "  " * (it.get("depth", 1) - 1)
                    marker = it.get("marker", "").strip()
                    if marker:
                        chunk_lines.append(f"{indent}- {marker} {it['content']}".rstrip() + "\n")
                    else:
                        chunk_lines.append(f"{indent}- {it['content']}\n")
                elif it["type"] in ("table", "image", "code"):
                    chunk_lines.append(f"{it['content']}\n")
                else:
                    chunk_lines.append(f"{it['content']}\n")

            raw_chunk_text = "\n".join(chunk_lines).strip()
            refined_chunk_text = refine_chunk_text(raw_chunk_text, context_text=prev_chunk_text).replace("\x00", "")
            f_out.write(refined_chunk_text + "\n\n")
            print(f"  [✓] Chunk {idx:02d}/{len(chunks):02d} 流式组装完成 (+{len(refined_chunk_text)} 字符)")
            prev_chunk_text = refined_chunk_text
        f_out.flush()
        os.fsync(f_out.fileno())
    os.replace(tmp_md, output_md_path)
    tmp_md_box.clear()

    print("")
    print(f"[✓] 长篇文档结构化转换完成！")
    print(f"    - 输出文档: {output_md_path}")
    print(f"    - 剥离目录页: {skipped_toc_pages} 页")
    print(f"    - 提取高价值图片: {image_global_idx} 项 (已存入 {attachments_dir.name}/)")
    return output_md_path

def convert_pdf_to_markdown(
    pdf_path: Path,
    output_md_path: Path,
    attachments_dir: Path,
    mode: str = "auto",
    dpi: int = 200,
    skip_decorative: bool = False,
    decorative_size_kb: int = 5,
    decorative_dim_px: int = 100,
    annotate_images: bool = False
):
    """主分流入口：自动探测或强制指定模式 (auto, paper, ppt)"""
    pdf_path = Path(pdf_path).resolve()
    doc = _open_validated_pdf(pdf_path)
    try:
        is_ppt = False
        if mode == "ppt":
            is_ppt = True
        elif mode == "paper":
            is_ppt = False
        else:
            # auto 模式自动嗅探
            is_ppt = is_presentation_pdf(doc)
    finally:
        if not getattr(doc, "is_closed", False):
            doc.close()

    if is_ppt:
        return convert_presentation_to_markdown(
            pdf_path=pdf_path,
            output_md_path=output_md_path,
            attachments_dir=attachments_dir,
            dpi=dpi,
            skip_decorative=skip_decorative,
            decorative_size_kb=decorative_size_kb,
            decorative_dim_px=decorative_dim_px,
            annotate_images=annotate_images
        )
    else:
        return convert_document_to_markdown(
            pdf_path=pdf_path,
            output_md_path=output_md_path,
            attachments_dir=attachments_dir,
            dpi=dpi,
            skip_decorative=skip_decorative,
            decorative_size_kb=decorative_size_kb,
            decorative_dim_px=decorative_dim_px,
            annotate_images=annotate_images
        )

def main():
    parser = argparse.ArgumentParser(description="PDF 智能解析与 Markdown 结构化转换引擎 (V5 全模态版)")
    parser.add_argument("pdf_file", nargs="?", help="要转换的 PDF 文件路径 (与 -i/--input 等效)")
    parser.add_argument("-i", "--input", help="要转换的 PDF 文件路径")
    parser.add_argument("-o", "--output", help="输出 Markdown 目标路径 (默认: 20-AI-Zone/20-Memory/21-Library/{slug}.md 或 {input}.md)")
    parser.add_argument("--attachments", default="99-Assets/10-Attachments", help="多媒体附件存储目录 (默认: 99-Assets/10-Attachments)")
    parser.add_argument("--mode", choices=["auto", "paper", "ppt"], default="auto", help="解析模式：auto(自适应), paper(长篇研报), ppt(幻灯片/扫描PPT)")
    parser.add_argument("--dpi", type=int, default=200, help="图片渲染分辨率 (默认: 200，PPT默认150)")
    parser.add_argument("--dropzone", action="store_true", help="扫描 00-DropZone/ 下所有 PDF 并执行静默归档转换")
    parser.add_argument("--archive", action="store_true", help="转换完成后将原 PDF 移动冷归档至 99-Assets/98-Archives/")
    parser.add_argument("--dry-run", action="store_true", help="演练模式：仅打印计划操作与路径，不修改文件")
    parser.add_argument("--skip-decorative", action="store_true", help="过滤装饰性图标（默认关闭）")
    parser.add_argument("--decorative-size", type=int, default=5, help="装饰性图标文件大小阈值（KB，默认 5）")
    parser.add_argument("--decorative-dim", type=int, default=100, help="装饰性图标尺寸阈值（像素，默认 100x100）")
    parser.add_argument("--annotate-images", action="store_true", help="为提取的图片生成空 callout 插槽，供智能体后续回填识别信息")
    
    args = parser.parse_args()
    vault_root = VAULT_ROOT
    
    # 1. DropZone 批量流转模式
    if args.dropzone:
        dropzone_dir = vault_root / "00-DropZone"
        archive_dir = monthly_archive_dir(vault_root / "99-Assets/98-Archives")
        library_dir = vault_root / "20-AI-Zone/20-Memory/21-Library"
        att_dir = vault_root / args.attachments
        
        if not dropzone_dir.exists():
            print(f"[-] 目录不存在: {dropzone_dir}")
            return

        pdf_files = []
        for p in sorted(dropzone_dir.rglob("*.pdf")):
            # 严格忽略 91-Temp/、_ 前缀隔离草稿区与隐藏文件
            rel_parts = p.relative_to(dropzone_dir).parts
            if any(part == "91-Temp" or part.startswith(".") or part.startswith("_") for part in rel_parts):
                continue
            if p.is_file():
                pdf_files.append(p)
                
        if not pdf_files:
            print("[*] 00-DropZone/ 未发现待处理的 PDF 文件（已自动跳过 91-Temp/ 与 _ 草稿区）。")
            return
            
        print(f"[*] 发现 {len(pdf_files)} 个 PDF 文件，准备执行流转...")
        collisions = 0
        for pdf_p in pdf_files:
            slug = pdf_p.stem
            out_md = library_dir / f"{slug}.md"
            dest_pdf = archive_dir / pdf_p.name

            if args.dry_run:
                print(f"  [DryRun] 计划处理: {pdf_p.relative_to(vault_root)}")
                print(f"    - 伴生卡片: 20-AI-Zone/20-Memory/21-Library/{slug}.md")
                print(f"    - 原件冷归档: 98-Archives/{month_tag()}/{pdf_p.name}")
                continue

            # 碰撞零覆盖：归档落点已存在同名原件时跳过该文件并记失败，严禁静默顶替
            if dest_pdf.exists():
                print(f"[COLLISION] 冷归档落点已存在同名原件，跳过处理: {dest_pdf}", file=sys.stderr)
                print(f"  请先经 vfs trash 清理旧归档或对源文件更名后重试。", file=sys.stderr)
                collisions += 1
                continue

            print(f"\n[*] 正在转换: {pdf_p.name}")
            conv_res = convert_pdf_to_markdown(
                pdf_p, out_md, att_dir,
                mode=args.mode, dpi=args.dpi,
                skip_decorative=args.skip_decorative,
                decorative_size_kb=args.decorative_size,
                decorative_dim_px=args.decorative_dim,
                annotate_images=args.annotate_images
            )
            if conv_res is None:
                collisions += 1
                continue
            archive_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(pdf_p), str(dest_pdf))
            print(f"  [✓] 原件已安全冷归档至: 98-Archives/{month_tag()}/{pdf_p.name}")
            print(f"  [✓] 伴生卡片已生成: 20-AI-Zone/20-Memory/21-Library/{slug}.md")
        if collisions:
            sys.exit(1)
        return

    # 2. 单文件独立转换模式
    target_input = args.input if args.input else args.pdf_file
    if not target_input:
        parser.print_help()
        sys.exit(1)
        
    pdf_in = Path(target_input).resolve()
    if not pdf_in.exists():
        print(f"[-] 文件不存在: {pdf_in}")
        sys.exit(1)
        
    in_dropzone = False
    try:
        rel = pdf_in.relative_to(vault_root)
        if len(rel.parts) > 0 and rel.parts[0] == "00-DropZone":
            in_dropzone = True
    except ValueError:
        pass

    if args.output:
        out_p = Path(args.output).resolve()
    else:
        if in_dropzone:
            out_p = vault_root / "20-AI-Zone/20-Memory/21-Library" / f"{pdf_in.stem}.md"
        else:
            out_p = pdf_in.with_suffix(".md")
        
    should_archive = args.archive or in_dropzone
    if os.environ.get("GEWU_ATLAS_MANAGED_FMT") == "1":
        should_archive = False
    att_p = vault_root / args.attachments

    if args.dry_run:
        print(f"[*] [DryRun] 待处理文件: {pdf_in.name}")
        print(f"    - 输入路径: {pdf_in}")
        print(f"    - 目标伴生卡片: {out_p}")
        print(f"    - 解析模式: {args.mode}, DPI: {args.dpi}")
        if should_archive:
            print(f"    - 计划冷归档原件至: 98-Archives/{month_tag()}/{pdf_in.name}")
        return

    out_p.parent.mkdir(parents=True, exist_ok=True)

    # 碰撞零覆盖：归档落点已存在同名原件时前置拦截，严禁转换完成后静默顶替
    if should_archive:
        _archive_dir = monthly_archive_dir(vault_root / "99-Assets/98-Archives")
        if (_archive_dir / pdf_in.name).exists():
            print(f"[COLLISION] 冷归档落点已存在同名原件，严禁静默覆盖: {_archive_dir / pdf_in.name}",
                  file=sys.stderr)
            print(f"  请先经 vfs trash 清理旧归档或对源文件更名后重试。", file=sys.stderr)
            sys.exit(1)

    conv_res = convert_pdf_to_markdown(
        pdf_in, out_p, att_p,
        mode=args.mode, dpi=args.dpi,
        skip_decorative=args.skip_decorative,
        decorative_size_kb=args.decorative_size,
        decorative_dim_px=args.decorative_dim,
        annotate_images=args.annotate_images
    )
    if conv_res is None:
        sys.exit(1)

    if should_archive:
        archive_dir = monthly_archive_dir(vault_root / "99-Assets/98-Archives")
        archive_dir.mkdir(parents=True, exist_ok=True)
        dest_pdf = archive_dir / pdf_in.name
        shutil.move(str(pdf_in), str(dest_pdf))
        print(f"  [✓] 原件已安全冷归档至: 98-Archives/{month_tag()}/{pdf_in.name}")
        print(f"  [✓] 伴生卡片已生成: {out_p}")

if __name__ == "__main__":
    main()
