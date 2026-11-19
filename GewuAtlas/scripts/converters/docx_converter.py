#!/usr/bin/env python3
"""
docx_converter.py - 智能 Word (.docx) 文档解析与 Markdown 结构化转换引擎 (V1 生产级)

核心特性与契约对齐：
1. 1~6 级标题深度解析 (Heading 1~6, 标题 1~6, outlineLvl)
2. 行内样式精确还原：加粗 (**)、斜体 (*)、删除线 (~~)、行内代码 (`)、超链接 [text](url)
3. 代码块防护：连续等宽字体/代码样式段落自动聚合并识别语言包裹 ```lang ... ```，保留缩进
4. 表格转 GFM (Advanced Tables 兼容)：合并单元格平铺、多行折叠为 <br>、特殊符号 | 转义
5. 多模态图片原位抽取：从 word/media 提取高清原图存入 99-Assets/10-Attachments/，以 ![[img]] 双链内嵌
6. 多层级列表：按 ilvl 缩进还原有序与无序列表层级
7. 引用块与 Callout：Quote 样式转译为 Obsidian 原生引用
8. KaTeX 符号安全：转义裸 $ 符号为 \\$，防止数学公式误渲染
9. 遵循 AGENTS.md 规范：生成标准 Frontmatter，支持 DropZone 自动扫描与归档
"""

__version__ = "1.1"
import os
import re
import sys
import shutil
import argparse
from pathlib import Path
from datetime import datetime

import md_table
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
    import docx
    from docx.text.paragraph import Paragraph
    from docx.table import Table
    from docx.oxml.ns import qn
except ImportError:
    print("[-] 缺少 python-docx 依赖，请在虚拟环境中安装：pip install python-docx")
    sys.exit(1)


# ==============================================================================
# 语言探测与辅助函数
# ==============================================================================
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
    if any(k in clean for k in ("const ", "let ", "function(", "=>", "document.")):
        return "javascript"
    if clean.startswith(("<html", "<?xml", "<!DOCTYPE")):
        return "html"
    return ""

def escape_dollar(text: str) -> str:
    """转义非公式的裸美元符号，防止 KaTeX 渲染异常"""
    return text.replace("$", "\\$")

def get_mime_extension(content_type: str) -> str:
    """根据 MIME 类型返回文件扩展名"""
    m = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/gif": ".gif",
        "image/svg+xml": ".svg",
        "image/webp": ".webp",
        "image/tiff": ".tiff",
    }
    return m.get(content_type.lower(), ".png")


# ==============================================================================
# 多级标题层级序号引擎 (Heading Hierarchical Numbering Engine)
# ==============================================================================
class HeadingNumberEngine:
    """
    智能多级标题层级序号计算引擎：
    1. 优先解析 Word 原生 numbering.xml 及样式 numPr，还原多级列表模版（如 1., 1.1, 2.3.1）
    2. 若文档未定义 numbering 模版，回退为全自动 1~6 级层级序号计数器
    3. 自动规避：若原标题文本中已包含数字序号（如 1.1、一、等），杜绝重复编号
    """
    def __init__(self, doc):
        self.doc = doc
        self.num_to_abstract = {}
        self.abstract_levels = {}
        self.style_numpr = {}
        self.counters = {}
        self.auto_counters = [0] * 10
        self._parse_numbering()
        self._parse_styles()

    def _parse_numbering(self):
        try:
            num_part = self.doc.part.numbering_part
            if num_part is None:
                return
            for num in num_part._element.findall(qn("w:num")):
                numId_str = num.get(qn("w:numId"))
                abs_elem = num.find(qn("w:abstractNumId"))
                if numId_str and abs_elem is not None:
                    self.num_to_abstract[int(numId_str)] = int(abs_elem.get(qn("w:val")))
            for abs_num in num_part._element.findall(qn("w:abstractNum")):
                abs_id_str = abs_num.get(qn("w:abstractNumId"))
                if abs_id_str is not None:
                    abs_id = int(abs_id_str)
                    self.abstract_levels[abs_id] = {}
                    for lvl in abs_num.findall(qn("w:lvl")):
                        ilvl_str = lvl.get(qn("w:ilvl"))
                        if ilvl_str is not None:
                            ilvl = int(ilvl_str)
                            lvlText_elem = lvl.find(qn("w:lvlText"))
                            numFmt_elem = lvl.find(qn("w:numFmt"))
                            t = lvlText_elem.get(qn("w:val")) if lvlText_elem is not None else ""
                            f = numFmt_elem.get(qn("w:val")) if numFmt_elem is not None else "decimal"
                            self.abstract_levels[abs_id][ilvl] = (t, f)
        except Exception:
            pass

    def _parse_styles(self):
        try:
            for s in self.doc.styles:
                pPr = s._element.find(qn("w:pPr"))
                if pPr is not None:
                    numPr = pPr.find(qn("w:numPr"))
                    if numPr is not None:
                        i_elem = numPr.find(qn("w:ilvl"))
                        n_elem = numPr.find(qn("w:numId"))
                        if i_elem is not None and n_elem is not None:
                            self.style_numpr[s.name] = (int(n_elem.get(qn("w:val"))), int(i_elem.get(qn("w:val"))))
        except Exception:
            pass

    def get_heading_prefix(self, p: Paragraph, hlvl: int, raw_text: str) -> str:
        # 如果标题文本中已存在序号标识，杜绝二次编号
        if re.match(r"^(\d+(\.\d+)*|[一二三四五六七八九十百]+[、.]|[(（][一二三四五六七八九十]+[)）])\s*", raw_text):
            return ""

        numId, ilvl = None, None
        pPr = p._p.find(qn("w:pPr"))
        if pPr is not None:
            numPr = pPr.find(qn("w:numPr"))
            if numPr is not None:
                i_elem = numPr.find(qn("w:ilvl"))
                n_elem = numPr.find(qn("w:numId"))
                if i_elem is not None and n_elem is not None:
                    numId = int(n_elem.get(qn("w:val")))
                    ilvl = int(i_elem.get(qn("w:val")))

        if numId is None and p.style and p.style.name in self.style_numpr:
            numId, ilvl = self.style_numpr[p.style.name]

        # 模式 1：从 Word 原生 numbering.xml 读取并按多级列表模版动态计算
        if numId is not None and ilvl is not None:
            if numId not in self.counters:
                self.counters[numId] = [0] * 10
            self.counters[numId][ilvl] += 1
            for d in range(ilvl + 1, 10):
                self.counters[numId][d] = 0

            abs_id = self.num_to_abstract.get(numId)
            if abs_id is not None and abs_id in self.abstract_levels:
                lvl_info = self.abstract_levels[abs_id].get(ilvl)
                if lvl_info:
                    lvlText, numFmt = lvl_info
                    if numFmt == "decimal" or not numFmt:
                        res = lvlText
                        for i in range(1, 10):
                            res = res.replace(f"%{i}", str(self.counters[numId][i-1]))
                        return res.strip()
            return ".".join(str(self.counters[numId][k]) for k in range(ilvl + 1))

        # 模式 2：回退自动多级层级序号
        idx = hlvl - 1
        if 0 <= idx < 10:
            self.auto_counters[idx] += 1
            for d in range(idx + 1, 10):
                self.auto_counters[d] = 0
            if idx == 0:
                return f"{self.auto_counters[0]}."
            else:
                return ".".join(str(self.auto_counters[k]) for k in range(idx + 1))

        return ""


# ==============================================================================
# 核心转换器实现
# ==============================================================================
class DocxToMarkdownConverter:
    def __init__(self, docx_path: Path, output_md_path: Path, attachments_dir: Path):
        self.docx_path = Path(docx_path)
        self.output_md_path = Path(output_md_path)
        self.attachments_dir = Path(attachments_dir)
        self.doc_slug = self.docx_path.stem
        self.doc = docx.Document(str(self.docx_path))
        self.heading_engine = HeadingNumberEngine(self.doc)
        self.image_counter = 0
        self.markdown_sections = []
        
        # 确保目录存在
        self.attachments_dir.mkdir(parents=True, exist_ok=True)
        self.output_md_path.parent.mkdir(parents=True, exist_ok=True)

    def extract_image_part(self, r_id: str) -> str:
        """从关系 ID 抽取图片并保存到纯扁平附件库"""
        try:
            rel = self.doc.part.rels[r_id]
            img_part = self.doc.part.related_parts[r_id]
            ext = get_mime_extension(img_part.content_type)
            self.image_counter += 1
            img_name = f"{self.doc_slug}_img_{self.image_counter:02d}{ext}"
            img_path = self.attachments_dir / img_name
            cnt = 1
            while img_path.exists():
                if img_path.read_bytes() == img_part.blob:
                    return img_name
                img_name = f"{self.doc_slug}_img_{self.image_counter:02d}_{cnt}{ext}"
                img_path = self.attachments_dir / img_name
                cnt += 1
            img_path.write_bytes(img_part.blob)
            return img_name
        except Exception as e:
            # 兼容外部图片或未能解析的图片
            return ""

    def format_run_text(self, text: str, bold: bool, italic: bool, is_mono: bool, strike: bool) -> str:
        """格式化单段行内文字，保留前后空格并精准包裹 Markdown 语法"""
        if not text:
            return ""
        
        # 保护裸美元符号
        text = escape_dollar(text)
        
        l_spaces = len(text) - len(text.lstrip(" "))
        r_spaces = len(text) - len(text.rstrip(" "))
        core = text.strip(" ")
        if not core:
            return text

        if is_mono:
            core = f"`{core}`"
        if strike:
            core = f"~~{core}~~"
        if bold and italic:
            core = f"***{core}***"
        elif bold:
            core = f"**{core}**"
        elif italic:
            core = f"*{core}*"

        return (" " * l_spaces) + core + (" " * r_spaces)

    def process_paragraph_runs(self, p: Paragraph) -> str:
        """解析段落内的文字运行、超链接与内嵌图片"""
        parts = []
        
        for child in p._p:
            tag = child.tag
            
            # 1. 普通文字运行 <w:r>
            if tag.endswith("}r"):
                # 检查是否包含图片 <w:drawing>
                img_found = False
                for blip in child.iter():
                    if blip.tag.endswith("blip"):
                        r_id = blip.get(qn("r:embed"))
                        if r_id:
                            img_filename = self.extract_image_part(r_id)
                            if img_filename:
                                parts.append(f"\n\n![[{img_filename}]]\n> [!NOTE] 图示解析与要素拆解\n> - **核心意图**：\n> - **图内要素与指标**：\n> - **逻辑机制 / 数据矩阵**：\n\n")
                                img_found = True
                if img_found:
                    continue

                # 提取文字与样式
                r_elem = child
                r_obj = None
                for r in p.runs:
                    if r._r == r_elem:
                        r_obj = r
                        break
                
                t_elems = [t for t in child.iter() if t.tag.endswith("}t")]
                text = "".join(t.text or "" for t in t_elems)
                if not text:
                    continue

                bold = r_obj.bold if r_obj else False
                italic = r_obj.italic if r_obj else False
                font_name = (r_obj.font.name or "").lower() if r_obj and r_obj.font else ""
                is_mono = font_name in ("consolas", "courier new", "menlo", "monaco", "lucida console")
                
                # 删除线检查
                strike = False
                if r_elem.find(qn("w:rPr")) is not None:
                    if r_elem.find(qn("w:rPr")).find(qn("w:strike")) is not None:
                        strike = True

                parts.append(self.format_run_text(text, bold, italic, is_mono, strike))

            # 2. 超链接 <w:hyperlink>
            elif tag.endswith("}hyperlink"):
                r_id = child.get(qn("r:id"))
                t_elems = [t for t in child.iter() if t.tag.endswith("}t")]
                link_text = "".join(t.text or "" for t in t_elems).strip()
                
                url = ""
                if r_id and r_id in self.doc.part.rels:
                    url = self.doc.part.rels[r_id].target_ref

                if url and link_text:
                    parts.append(f"[{link_text}]({url})")
                elif link_text:
                    parts.append(link_text)

        return "".join(parts).strip()

    def get_heading_level(self, p: Paragraph) -> int:
        """识别段落是否为 1~6 级标题"""
        style_name = p.style.name if p.style else ""
        style_lower = style_name.lower()

        # 1. 英文样式 Heading 1 ~ 6
        m_en = re.match(r"^heading\s*(\d+)$", style_lower)
        if m_en:
            lvl = int(m_en.group(1))
            return min(lvl, 6)

        # 2. 中文样式 标题 1 ~ 6
        m_zh = re.match(r"^标题\s*(\d+)$", style_name)
        if m_zh:
            lvl = int(m_zh.group(1))
            return min(lvl, 6)

        # 3. Title 样式视为 1 级标题
        if style_lower in ("title", "文档标题"):
            return 1

        # 4. XML outlineLvl
        pPr = p._p.find(qn("w:pPr"))
        if pPr is not None:
            outline = pPr.find(qn("w:outlineLvl"))
            if outline is not None:
                val = outline.get(qn("w:val"))
                if val is not None and val.isdigit():
                    return min(int(val) + 1, 6)

        # 5. 基于文本模式自动嗅探（如 1.1、一、等）
        raw_text = p.text.strip()
        if len(raw_text) <= 50 and not raw_text.endswith(("。", "；", "！", "？", ";")):
            m_num = re.match(r"^(\d+(\.\d+)*)\s*(.*)$", raw_text)
            if m_num:
                depth = len(m_num.group(1).split("."))
                if 1 <= depth <= 6:
                    # 检查是否加粗
                    if any(r.bold for r in p.runs if r.text.strip()):
                        return depth
            if re.match(r"^第[一二三四五六七八九十百0-9]+[章节篇部]\s*", raw_text):
                return 1
            if re.match(r"^[一二三四五六七八九十]+[、.]\s*", raw_text) and any(r.bold for r in p.runs):
                return 2

        return 0

    def is_list_paragraph(self, p: Paragraph):
        """探测是否为列表段落及层级"""
        style_name = p.style.name if p.style else ""
        style_lower = style_name.lower()

        ilvl = 0
        pPr = p._p.find(qn("w:pPr"))
        if pPr is not None:
            numPr = pPr.find(qn("w:numPr"))
            if numPr is not None:
                ilvl_elem = numPr.find(qn("w:ilvl"))
                if ilvl_elem is not None:
                    val = ilvl_elem.get(qn("w:val"))
                    if val is not None and val.isdigit():
                        ilvl = int(val)

        if "bullet" in style_lower or "项目符号" in style_name:
            return "bullet", ilvl
        if "number" in style_lower or "编号" in style_name:
            return "number", ilvl

        if pPr is not None and pPr.find(qn("w:numPr")) is not None:
            # 有编号属性但无特定样式名
            return "bullet", ilvl

        # 文本正则识别列表项（如 - xxx, • xxx, 1. xxx）
        raw_text = p.text.strip()
        if raw_text.startswith(("- ", "* ", "+ ", "• ", "· ")):
            return "bullet", ilvl
        if re.match(r"^\d+\.\s+", raw_text):
            return "number", ilvl

        return None, 0

    def is_code_paragraph(self, p: Paragraph) -> bool:
        """探测段落是否属于代码块"""
        style_name = (p.style.name or "").lower() if p.style else ""
        if any(k in style_name for k in ("code", "代码", "preformatted", "source code")):
            return True
        
        runs = [r for r in p.runs if r.text.strip()]
        if runs:
            mono_count = 0
            for r in runs:
                fn = (r.font.name or "").lower() if r.font else ""
                if fn in ("consolas", "courier new", "menlo", "monaco", "lucida console"):
                    mono_count += 1
            if mono_count == len(runs):
                return True
        return False

    def is_quote_paragraph(self, p: Paragraph) -> bool:
        """探测是否为引用块"""
        style_name = (p.style.name or "").lower() if p.style else ""
        return any(k in style_name for k in ("quote", "引用", "引文"))

    def convert_table_to_gfm(self, table: Table) -> str:
        """将 Word 表格转译为高质量 GitHub Flavored Markdown 表格 (支持合并单元格)"""
        if not table.rows:
            return ""

        grid = []
        for row in table.rows:
            seen_cells = set()
            row_vals = []
            for cell in row.cells:
                tc_id = id(cell._tc)
                if tc_id not in seen_cells:
                    seen_cells.add(tc_id)
                    # 单元格内容处理：多段落压平为一行并以「；」串联，管道符转义（支持图文与纯图）
                    paras = []
                    for cp in cell.paragraphs:
                        p_out = self.process_paragraph_runs(cp).strip()
                        if p_out:
                            paras.append(p_out)
                    row_vals.append(md_table.escape_cell("\n".join(paras)))
                else:
                    # 合并单元格的延续
                    row_vals.append("")
            grid.append(row_vals)

        if not grid:
            return ""

        header = grid[0]
        # 如果第一行为空，补默认列名
        if not any(header):
            header = [f"列 {i+1}" for i in range(max(len(r) for r in grid))]

        return md_table.emit_gfm(header, grid[1:])

    def resolve_document_title(self) -> str:
        explicit = (self.doc.core_properties.title or "").strip()
        if explicit and explicit.casefold() not in {"word document", "microsoft word document"}:
            return explicit
        for paragraph in self.doc.paragraphs:
            style = (paragraph.style.name if paragraph.style else "").strip().casefold()
            if paragraph.text.strip() and style in {"title", "文档标题"}:
                return paragraph.text.strip()
        for paragraph in self.doc.paragraphs:
            if paragraph.text.strip() and self.get_heading_level(paragraph) == 1:
                return paragraph.text.strip()
        return self.doc_slug

    def convert(self) -> Path:
        """执行端到端全量转换"""
        print(f"[*] 开始解析 Word 文档: {self.docx_path.name}")
        
        today_str = datetime.now().strftime("%Y-%m-%d")
        doc_title = self.doc_slug
        status_val = get_status_for_domain(VAULT_ROOT, out_path=self.output_md_path)
        
        # python-docx synthesizes "Word Document" when the core part is absent.
        # Do not let that placeholder override a title present in the source body.
        doc_title = self.resolve_document_title()

        # 注入标准 Frontmatter
        safe_title = fm_scalar(doc_title)
        frontmatter = f"""---
doc_type: library
title: "{safe_title}"
source_type: docx
channel: "行业研报"
summary: "{safe_title} 结构化 Word 办公文档转译与排版"
original_file: "{fm_scalar(self.docx_path.name)}"
processed: false
status: {status_val}
updated: {today_str}
tags: [docx, document, office]
extracted_to: []
---

# {doc_title}

> [!NOTE] 原件归档提示
> 本文由 Word 文档原生结构化转译。物理原件已冷归档至 `98-Archives/{month_tag()}/{self.docx_path.name}`，所有表格兼容 Obsidian Advanced Tables 规范，内嵌高清图已纯扁平托管。

"""
        self.markdown_sections.append(frontmatter)

        # 遍历文档子元素 (保持严格的段落与表格文档流顺序)
        pending_code_lines = []
        pending_code_blanks = []
        has_emitted_first_title = False

        def flush_code_block():
            nonlocal pending_code_lines, pending_code_blanks
            if pending_code_lines:
                raw_code = "\n".join(pending_code_lines)
                lang = detect_code_language(raw_code)
                code_block = f"```{lang}\n{raw_code}\n```\n"
                self.markdown_sections.append(code_block)
                pending_code_lines = []
            pending_code_blanks = []

        for child in self.doc.element.body:
            tag = child.tag
            
            # 1. 段落元素
            if tag.endswith("}p"):
                p = Paragraph(child, self.doc)
                raw_text = p.text.strip()

                # 代码段落收集与缓冲（支持代码块内部空行）
                if self.is_code_paragraph(p):
                    if pending_code_blanks:
                        pending_code_lines.extend(pending_code_blanks)
                        pending_code_blanks = []
                    # 保留原代码物理缩进
                    pending_code_lines.append(p.text)
                    continue
                elif pending_code_lines and not raw_text:
                    # 代码块内部空行缓冲
                    pending_code_blanks.append("")
                    continue
                else:
                    flush_code_block()

                if not raw_text:
                    # 检查是否有独立图片
                    formatted_p = self.process_paragraph_runs(p)
                    if formatted_p:
                        self.markdown_sections.append(formatted_p + "\n")
                    continue

                # 标题解析
                hlvl = self.get_heading_level(p)
                if hlvl > 0:
                    clean_title = self.process_paragraph_runs(p)
                    # 去除前缀可能重复的 markdown #
                    clean_title = re.sub(r"^#+\s*", "", clean_title)
                    
                    # 关键特性：注入多级层级序号
                    num_prefix = self.heading_engine.get_heading_prefix(p, hlvl, clean_title)
                    if num_prefix:
                        full_heading = f"{num_prefix} {clean_title}"
                    else:
                        full_heading = clean_title

                    # 若首个 H1 与文档主标题完全一致（且未带序号），跳过重复渲染
                    if hlvl == 1 and clean_title.strip() == doc_title.strip() and not num_prefix and not has_emitted_first_title:
                        has_emitted_first_title = True
                        continue
                    has_emitted_first_title = True
                    prefix = "#" * hlvl
                    self.markdown_sections.append(f"\n{prefix} {full_heading}\n")
                    continue

                # 列表解析
                ltype, ilvl = self.is_list_paragraph(p)
                if ltype:
                    clean_content = self.process_paragraph_runs(p)
                    # 剔除行首冗余的项目符号或编号字符
                    clean_content = re.sub(r"^[•·\-\*\+]\s*", "", clean_content)
                    indent = "  " * ilvl
                    marker = "-" if ltype == "bullet" else "1."
                    self.markdown_sections.append(f"{indent}{marker} {clean_content}")
                    continue

                # 引用段落解析
                if self.is_quote_paragraph(p):
                    clean_content = self.process_paragraph_runs(p)
                    self.markdown_sections.append(f"> {clean_content}\n")
                    continue

                # 普通段落解析
                formatted_p = self.process_paragraph_runs(p)
                if formatted_p:
                    self.markdown_sections.append(f"{formatted_p}\n")

            # 2. 表格元素
            elif tag.endswith("}tbl"):
                flush_code_block()
                tbl = Table(child, self.doc)
                gfm_tbl = self.convert_table_to_gfm(tbl)
                if gfm_tbl:
                    self.markdown_sections.append(f"\n{gfm_tbl}\n")

        flush_code_block()

        # 合并输出
        final_md = "\n".join(self.markdown_sections)
        # 去除连续三行以上的多余空行
        final_md = re.sub(r"\n{3,}", "\n\n", final_md).strip() + "\n"

        self.output_md_path.write_text(final_md, encoding="utf-8")
        print(f"[✓] 转译完成！Markdown 已生成至: {self.output_md_path}")
        print(f"    - 抽取内嵌图片数: {self.image_counter} 张")
        print(f"    - 附件存储于: {self.attachments_dir.name}/")
        return self.output_md_path


# ==============================================================================
# CLI 入口与 DropZone 自动化流水线
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="Word (.docx) 智能结构化转译引擎 (V1)")
    parser.add_argument("docx_file", nargs="?", help="要转换的 Word (.docx) 文件路径 (与 -i/--input 等效)")
    parser.add_argument("-i", "--input", help="要转换的 Word (.docx) 文件路径")
    parser.add_argument("-o", "--output", help="输出 Markdown 目标路径 (默认: 20-AI-Zone/20-Memory/21-Library/{slug}.md 或 {input}.md)")
    parser.add_argument("--attachments", default="99-Assets/10-Attachments", help="多媒体附件存储目录 (默认: 99-Assets/10-Attachments)")
    parser.add_argument("--dropzone", action="store_true", help="扫描 00-DropZone/ 下所有 Word 文件并执行静默归档转换")
    parser.add_argument("--archive", action="store_true", help="转换完成后将原 Word 文件移动冷归档至 99-Assets/98-Archives/")
    parser.add_argument("--dry-run", action="store_true", help="演练模式：仅打印计划操作与路径，不修改文件")

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

        docx_files = []
        for p in sorted(dropzone_dir.rglob("*.docx")):
            rel_parts = p.relative_to(dropzone_dir).parts
            if any(part == "91-Temp" or part.startswith(".") or part.startswith("_") for part in rel_parts):
                continue
            if p.is_file():
                docx_files.append(p)

        if not docx_files:
            print("[*] 00-DropZone/ 未发现待处理的 .docx 文件（已自动跳过 91-Temp/ 与 _ 草稿区）。")
            return

        print(f"[*] 发现 {len(docx_files)} 个 Word 文件，准备执行流转...")
        for docx_p in docx_files:
            slug = docx_p.stem
            out_md = library_dir / f"{slug}.md"
            dest_docx = archive_dir / docx_p.name

            if args.dry_run:
                print(f"  [DryRun] 计划处理: {docx_p.relative_to(vault_root)}")
                print(f"    - 伴生卡片: 20-AI-Zone/20-Memory/21-Library/{slug}.md")
                print(f"    - 原件冷归档: 98-Archives/{month_tag()}/{docx_p.name}")
                continue

            print(f"\n[*] 正在转换: {docx_p.name}")
            converter = DocxToMarkdownConverter(docx_p, out_md, att_dir)
            converter.convert()
            archive_dir.mkdir(parents=True, exist_ok=True)
            n = 1
            while dest_docx.exists() and dest_docx.resolve() != docx_p.resolve():
                dest_docx = archive_dir / f"{docx_p.stem}_{n}{docx_p.suffix}"
                n += 1
            shutil.move(str(docx_p), str(dest_docx))
            print(f"  [✓] 原件已安全冷归档至: 98-Archives/{month_tag()}/{dest_docx.name}")
            print(f"  [✓] 伴生卡片已生成: 20-AI-Zone/20-Memory/21-Library/{slug}.md")
        return

    # 2. 单文件独立转换模式
    target_input = args.input if args.input else args.docx_file
    if not target_input:
        parser.print_help()
        sys.exit(1)

    docx_in = Path(target_input).resolve()
    if not docx_in.exists():
        print(f"[-] 文件不存在: {docx_in}")
        sys.exit(1)

    in_dropzone = False
    try:
        rel = docx_in.relative_to(vault_root)
        if len(rel.parts) > 0 and rel.parts[0] == "00-DropZone":
            in_dropzone = True
    except ValueError:
        pass

    if args.output:
        out_p = Path(args.output).resolve()
    else:
        if in_dropzone:
            out_p = vault_root / "20-AI-Zone/20-Memory/21-Library" / f"{docx_in.stem}.md"
        else:
            out_p = docx_in.with_suffix(".md")

    should_archive = args.archive or in_dropzone
    if os.environ.get("GEWU_ATLAS_MANAGED_FMT") == "1":
        should_archive = False
    att_p = vault_root / args.attachments

    if args.dry_run:
        print(f"[*] [DryRun] 待处理文件: {docx_in.name}")
        print(f"    - 输入路径: {docx_in}")
        print(f"    - 目标伴生卡片: {out_p}")
        if should_archive:
            print(f"    - 计划冷归档原件至: 98-Archives/{month_tag()}/{docx_in.name}")
        return

    if out_p.exists():
        print(f"[COLLISION] 目标卡片已存在: {out_p}", file=sys.stderr)
        sys.exit(1)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    converter = DocxToMarkdownConverter(docx_in, out_p, att_p)
    converter.convert()

    if should_archive:
        archive_dir = monthly_archive_dir(vault_root / "99-Assets/98-Archives")
        archive_dir.mkdir(parents=True, exist_ok=True)
        dest_docx = archive_dir / docx_in.name
        n = 1
        while dest_docx.exists() and dest_docx.resolve() != docx_in.resolve():
            dest_docx = archive_dir / f"{docx_in.stem}_{n}{docx_in.suffix}"
            n += 1
        shutil.move(str(docx_in), str(dest_docx))
        print(f"  [✓] 原件已安全冷归档至: 98-Archives/{month_tag()}/{dest_docx.name}")
        print(f"  [✓] 伴生卡片已生成: {out_p}")


if __name__ == "__main__":
    main()
