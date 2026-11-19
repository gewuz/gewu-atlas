#!/usr/bin/env python3
"""
pptx_converter.py - 智能 PowerPoint (.pptx) 演示文稿解析与 Markdown 结构化转换引擎 (V1 纯文本轻量级生产版)

核心特性与契约对齐：
1. 单页 Slide 切片架构：逐页提取幻灯片大纲、标题、指标卡片与核心要点
2. 语义卡片智能拆解与重构 (Semantic Chunking & Card Reconstruction)：
   - 彻底解决组合形状 (Group Shapes) 盲目拼接导致的长句灾难
   - 智能识别大字指标卡片（如 2万亿+、199.48 EB、66%），保持指标与说明一体化
   - 识别有序壁垒/步骤卡片（如 01. 标准优势・规则制定者）
   - 识别里程碑卡片（如 2024 · 《建设指引》发布）
   - 识别领域与说明键值对（如 政务领域：打造城市级可信数据空间...）
   - 识别维度大纲（如 DIMENSION 01 · 政策强驱动）
3. 原生表格转 GFM (Advanced Tables 兼容)：原生支持竞品对比矩阵转换，表头与对齐线自动闭合
4. 独家演讲者备注提取 (Speaker Notes)：抽取底层备忘要点，渲染为 Obsidian 提示 Callout
5. 章节与目录自动识别：自动识别封面、目录、章节转场（Chapter 01~04）并注入结构化导读 Callout
6. KaTeX 符号安全防护：转义裸 $ 符号为 \\$，防止数学公式误渲染
7. 零外部重型依赖：100% 纯 Python 解析，秒级处理超大演示文稿（支持 DropZone 自动扫描与原件归档）
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
    import pptx
    from pptx.util import Inches, Pt
    from pptx.enum.shapes import MSO_SHAPE_TYPE
except ImportError:
    print("[-] 缺少 python-pptx 依赖，请在虚拟环境中安装：pip install python-pptx")
    sys.exit(1)


# ==============================================================================
# 辅助函数
# ==============================================================================
def escape_dollar(text: str) -> str:
    """转义裸美元符号，防止 KaTeX 渲染异常"""
    return text.replace("$", "\\$")

def is_metric_number(text: str) -> bool:
    """探测是否为大字指标数值（如 2万亿+、3000亿、66%、199.48 EB、24亿美元）"""
    clean = text.strip()
    if any(clean.endswith(k) for k in (
        "万亿+", "千亿+", "亿+", "万+", "%", "EB", "TB", "PB", "元", "次",
        "个", "家", "亿元/年", "增长拉动", "年+", "万亿次", " +", "亿元", "亿美元", "B+"
    )):
        return True
    if re.match(r"^[\$￥¥]?\d+(\.\d+)?\s*(万|亿|%|k|M|B|G|T|B\+|亿元|亿美元|年|\+)?\+?$", clean):
        # 排除 2020~2035 年份
        if re.match(r"^20\d{2}$", clean):
            return False
        return True
    return False

def is_noise_text(text: str) -> bool:
    """过滤幻灯片常见页脚噪音、版权字样、纯图标占位符"""
    t = text.strip()
    if not t:
        return True
    if t in ["⚖", "↗", "$", "⛨", "✦", "…"]:
        return True
    if t.startswith("©") or t.startswith("(c)"):
        return True
    return False

def convert_table_to_gfm(shape) -> str:
    """将 PPT 原生表格转译为高质量 GitHub Flavored Markdown 表格"""
    try:
        table = shape.table
        grid = []
        for row in table.rows:
            row_vals = []
            for cell in row.cells:
                cell_text = md_table.escape_cell(cell.text)
                row_vals.append(escape_dollar(cell_text))
            grid.append(row_vals)

        if not grid:
            return ""

        header = grid[0]
        if not any(header):
            header = [f"列 {i+1}" for i in range(max(len(r) for r in grid))]

        return md_table.emit_gfm(header, grid[1:])
    except Exception:
        return ""

def extract_slide_title(slide, idx: int, total_slides: int) -> str:
    """多维启发式识别幻灯片标题，完美兼容封面、目录、章节转场与正文页"""
    if slide.shapes.title and slide.shapes.title.text.strip():
        t = slide.shapes.title.text.strip().replace("\n", " ")
        if t:
            return t

    texts = []
    for s in slide.shapes:
        if s.has_text_frame and s.text_frame.text.strip():
            texts.append(s.text_frame.text.strip())
        elif s.shape_type == MSO_SHAPE_TYPE.GROUP:
            def rec(g):
                for sub in g.shapes:
                    if sub.shape_type == MSO_SHAPE_TYPE.GROUP:
                        rec(sub)
                    elif sub.has_text_frame and sub.text_frame.text.strip():
                        texts.append(sub.text_frame.text.strip())
            rec(s)

    # 封面页
    if idx == 1:
        candidates = [t for t in texts if len(t.strip()) >= 2 and not re.match(r"^\d{4}(?:[-/年\s]|$)", t.strip())]
        if candidates:
            return f"封面：{candidates[0]}"
        return "封面：演示文稿"

    # 目录页
    if any(k in t.upper() for t in texts for k in ("CONTENTS", "目录", "TABLE OF CONTENTS")):
        return "目录 (Contents)"

    # 章节转场页
    if any("CHAPTER" in t.upper() for t in texts):
        ch_num = ""
        ch_name = ""
        for t in texts:
            if re.match(r"^\d{2}$", t):
                ch_num = t
            elif len(t) <= 10 and not any(k in t.upper() for k in ("CHAPTER", "BRIEF")) and not t.isdigit():
                ch_name = t
        if ch_num and ch_name:
            return f"Chapter {ch_num}: {ch_name}"
        elif ch_name:
            return f"Chapter: {ch_name}"

    # 尾页 / 结束
    if idx == total_slides and not texts:
        return "尾页 / 结束"

    # 候选文本：根据字号与垂直位置降序排序
    candidates = []
    for s in slide.shapes:
        if s.has_text_frame and s.text_frame.text.strip():
            max_pt = 0
            for p in s.text_frame.paragraphs:
                for r in p.runs:
                    if r.font and r.font.size:
                        max_pt = max(max_pt, r.font.size.pt)
            candidates.append((max_pt, s.top, s.text_frame.text.strip().splitlines()[0]))

    if candidates:
        candidates.sort(key=lambda x: (-x[0], x[1]))
        return candidates[0][2]

    return f"第 {idx:02d} 页"


# ==============================================================================
# 语义重构与列表解析引擎 (Semantic Reconstructor)
# ==============================================================================
def parse_items_to_bullets(items: list, title: str = "") -> list:
    """
    将幻灯片中解构出的有序文本元数据，智能聚合为层级清晰、语义一体的 Markdown 要点。
    彻底避免死板长句拼接与孤立信息破碎。
    """
    bullets = []
    i = 0
    n = len(items)
    while i < n:
        t = items[i].strip()
        if not t or t == title or is_noise_text(t):
            i += 1
            continue

        # 1. 编号卡片/步骤：['01', '标题', ...]
        if re.match(r"^\d{2}$", t) and i + 1 < n:
            num = t
            t1 = items[i+1].strip()
            # 商业模式类型复合卡片：['01', '软件授权与系统集成', 'License + 集成', '描述...', '小结...']
            if i + 4 < n and len(items[i+2]) <= 20 and len(items[i+3]) > 20:
                t2 = items[i+2].strip()
                t3 = items[i+3].strip()
                t4 = items[i+4].strip()
                bullets.append(f"- **{num}. {t1} ({t2})**：{escape_dollar(t3)}（{escape_dollar(t4)}）")
                i += 5
                continue
            elif i + 2 < n and len(t1) <= 25 and len(items[i+2]) > 10:
                t2 = items[i+2].strip()
                bullets.append(f"- **{num}. {t1}**：{escape_dollar(t2)}")
                i += 3
                continue
            else:
                bullets.append(f"- **{num}. {t1}**")
                i += 2
                continue

        # 2. 里程碑卡片：['2024', '《建设指引》发布', '国家发改委...']
        if re.match(r"^\d{4}$", t) and i + 2 < n and len(items[i+1]) <= 20:
            bullets.append(f"- **{t} · {items[i+1]}**：{escape_dollar(items[i+2])}")
            i += 3
            continue

        # 3. 核心维度卡片：['DIMENSION 01', '政策强驱动']
        if t.startswith("DIMENSION ") and i + 1 < n and len(items[i+1]) <= 15:
            bullets.append(f"- **{t} · {items[i+1]}**")
            i += 2
            continue

        # 4. 指标卡片 A 型：[短标签, 大字指标, 详细说明] (如 Slide 5)
        if len(t) <= 15 and i + 1 < n and is_metric_number(items[i+1]):
            lbl = t
            met = items[i+1].strip()
            if i + 2 < n and len(items[i+2]) > 15:
                desc = items[i+2].strip()
                bullets.append(f"- **{met}**：{lbl} —— {escape_dollar(desc)}")
                i += 3
                continue
            else:
                bullets.append(f"- **{met}**：{lbl}")
                i += 2
                continue

        # 5. 指标卡片 B 型：[大字指标, 短标签, 复合说明] (如 Slide 4, 6, 8)
        if is_metric_number(t):
            met = t
            if i + 1 < n and len(items[i+1]) <= 25:
                lbl = items[i+1].strip()
                if i + 2 < n and any(k in items[i+2] for k in ("增长率", "符合增长率", "年均", "预计", "数据要素", "倍增")):
                    desc = items[i+2].strip()
                    bullets.append(f"- **{met}**：{lbl} —— {escape_dollar(desc)}")
                    i += 3
                    continue
                else:
                    bullets.append(f"- **{met}**：{lbl}")
                    i += 2
                    continue
            else:
                bullets.append(f"- **{met}**")
                i += 1
                continue

        # 6. 键值对/领域卡片：[短标题, 完整描述] (如 Slide 6, 30)
        if len(t) <= 15 and not any(t.endswith(p) for p in ("。", "，", "、", "：", ":", "；")) and "→" not in t and "——" not in t:
            if i + 1 < n and len(items[i+1]) > 15:
                bullets.append(f"- **{t}**：{escape_dollar(items[i+1])}")
                i += 2
                continue

        # 7. 文本已自带破折号的独立条目 (如 统一语义层 —— 屏蔽物理存储差异)
        if "——" in t:
            parts = [p.strip() for p in t.split("——") if p.strip()]
            if len(parts) == 2 and len(parts[0]) <= 15:
                bullets.append(f"- **{parts[0]}**：{escape_dollar(parts[1])}")
            else:
                bullets.append(f"- {escape_dollar(t)}")
            i += 1
            continue

        # 8. 默认独立条目
        bullets.append(f"- {escape_dollar(t)}")
        i += 1

    return bullets


# ==============================================================================
# 核心转换器
# ==============================================================================
class PptxToMarkdownConverter:
    def __init__(self, pptx_path: Path, output_md_path: Path, attachments_dir: Path):
        self.pptx_path = Path(pptx_path)
        self.output_md_path = Path(output_md_path)
        self.attachments_dir = Path(attachments_dir)
        self.doc_slug = self.pptx_path.stem
        self.prs = pptx.Presentation(str(self.pptx_path))
        self.markdown_sections = []
        self.img_counter = 1

        self.attachments_dir.mkdir(parents=True, exist_ok=True)
        self.output_md_path.parent.mkdir(parents=True, exist_ok=True)

    def process_slide(self, slide, idx: int, total: int) -> str:
        """解析单页幻灯片文本、卡片、表格、图表、内嵌图片与备注"""
        slide_lines = []
        title = extract_slide_title(slide, idx, total)
        slide_lines.append(f"## Slide {idx:02d}: {title}\n")

        # 1. 章节转场页
        if any(title.startswith(f"Chapter {n}") for n in ["01", "02", "03", "04"]):
            briefs = []
            for s in slide.shapes:
                if getattr(s, "has_text_frame", False):
                    for p in s.text_frame.paragraphs:
                        for line in p.text.splitlines():
                            txt = line.strip()
                            if len(txt) > 20 and not is_noise_text(txt):
                                briefs.append(escape_dollar(txt))
                elif getattr(s, "shape_type", None) == MSO_SHAPE_TYPE.GROUP or hasattr(s, "shapes"):
                    def rec_b(sub):
                        for sub2 in getattr(sub, "shapes", []):
                            if getattr(sub2, "shape_type", None) == MSO_SHAPE_TYPE.GROUP or hasattr(sub2, "shapes"):
                                rec_b(sub2)
                            elif getattr(sub2, "has_text_frame", False):
                                for p in sub2.text_frame.paragraphs:
                                    for line in p.text.splitlines():
                                        txt = line.strip()
                                        if len(txt) > 20 and not is_noise_text(txt):
                                            briefs.append(escape_dollar(txt))
                    rec_b(s)
            if briefs:
                slide_lines.append("> [!NOTE] 章节导读\n" + "\n".join(f"> {b}" for b in briefs) + "\n")
        # 2. 尾页
        elif title == "尾页 / 结束":
            slide_lines.append("> [!NOTE] 演示结束\n> 感谢观看与审阅。\n")
        # 3. 标准内容页 (含封面与目录，提取真实文本)
        else:
            # (1) 原生表格处理
            tables = [s for s in slide.shapes if getattr(s, "has_table", False)]
            for tbl in tables:
                gfm = convert_table_to_gfm(tbl)
                if gfm:
                    slide_lines.append("### 差异化对比矩阵\n" + gfm + "\n")

            # (1.5) 业务图表解构 (Charts)
            charts = [s for s in slide.shapes if getattr(s, "has_chart", False)]
            for ch_shp in charts:
                try:
                    ch = ch_shp.chart
                    ch_title = ""
                    if getattr(ch, "has_title", False) and ch.chart_title and getattr(ch.chart_title, "has_text_frame", False):
                        ch_title = ch.chart_title.text_frame.text.strip()
                    ch_type = str(getattr(ch, "chart_type", "CHART")).split(".")[-1]
                    slide_lines.append(f"> [!NOTE] 业务图表：{ch_title or '数据图表'} (类型: {ch_type})\n")
                except Exception:
                    pass

            # (1.6) 内嵌图片提取至 attachments_dir
            slide_imgs = []
            def rec_images(shp):
                if getattr(shp, "shape_type", None) == MSO_SHAPE_TYPE.GROUP or hasattr(shp, "shapes"):
                    for sub in getattr(shp, "shapes", []):
                        rec_images(sub)
                elif getattr(shp, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE or hasattr(shp, "image"):
                    try:
                        img = shp.image
                        blob = img.blob
                        ext = (getattr(img, "ext", "png") or "png").lower()
                        if ext == "jpeg":
                            ext = "jpg"
                        img_name = f"{self.doc_slug}_slide{idx:02d}_{self.img_counter}.{ext}"
                        dest_img = self.attachments_dir / img_name
                        cnt = 1
                        while dest_img.exists():
                            img_name = f"{self.doc_slug}_slide{idx:02d}_{self.img_counter}_{cnt}.{ext}"
                            dest_img = self.attachments_dir / img_name
                            cnt += 1
                        dest_img.write_bytes(blob)
                        self.img_counter += 1
                        slide_imgs.append(img_name)
                    except Exception:
                        pass

            for s in slide.shapes:
                rec_images(s)

            if slide_imgs:
                for iname in slide_imgs:
                    slide_lines.append(f"![[{iname}]]\n")

            # (2) 递归解构所有形状中的纯净文本元数据 (含 SmartArt / Group 递归解构)
            leaves = []
            def rec_shapes(shp):
                if getattr(shp, "shape_type", None) == MSO_SHAPE_TYPE.GROUP or hasattr(shp, "shapes"):
                    for sub in getattr(shp, "shapes", []):
                        rec_shapes(sub)
                elif getattr(shp, "has_text_frame", False) and shp.text_frame.text.strip():
                    for p in shp.text_frame.paragraphs:
                        for line in p.text.splitlines():
                            txt = line.strip()
                            if txt and not is_noise_text(txt):
                                leaves.append(txt)

            for s in slide.shapes:
                if not getattr(s, "has_table", False):
                    rec_shapes(s)

            # (3) 语义重构为要点
            bullets = parse_items_to_bullets(leaves, title)

            # 去重但保持先后顺序
            seen = set()
            dedup_bullets = []
            for b in bullets:
                if b not in seen:
                    seen.add(b)
                    dedup_bullets.append(b)

            if dedup_bullets:
                slide_lines.append("### 幻灯片要点\n" + "\n".join(dedup_bullets) + "\n")

            # (4) 演讲者备注提取
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                notes_text = slide.notes_slide.notes_text_frame.text.strip()
                if notes_text:
                    safe_notes = escape_dollar(notes_text)
                    slide_lines.append("> [!TIP] 演讲备注与补充说明\n" + "\n".join(f"> {line}" for line in safe_notes.splitlines()) + "\n")

        return "\n".join(slide_lines).strip()

    def convert(self) -> Path:
        """执行端到端全量 PPT 解析与转译"""
        print(f"[*] 开始解析 PowerPoint 演示文稿 (语义重构纯文本轻量模式): {self.pptx_path.name}")
        total_slides = len(self.prs.slides)
        print(f"    - 总幻灯片数: {total_slides} 页")

        today_str = datetime.now().strftime("%Y-%m-%d")
        doc_title = self.doc_slug

        # 智能提取主标题
        if total_slides > 0:
            s1 = self.prs.slides[0]
            s1_title = extract_slide_title(s1, 1, total_slides)
            if s1_title.startswith("封面："):
                doc_title = s1_title.replace("封面：", "").strip()

        status_val = get_status_for_domain(VAULT_ROOT, out_path=self.output_md_path)
        safe_title = fm_scalar(doc_title)
        frontmatter = f"""---
doc_type: library
title: "{safe_title}"
source_type: presentation
channel: "官方白皮书"
summary: "PPT演示文稿结构化转译，共 {total_slides} 页大纲、指标卡片与对比矩阵"
original_file: "{fm_scalar(self.pptx_path.name)}"
total_slides: {total_slides}
processed: false
status: {status_val}
updated: {today_str}
tags: [presentation, ppt, pptx, slide]
extracted_to: []
---

# {doc_title}

> [!NOTE] 演示文稿速览
> 本文由 PowerPoint (.pptx) 结构化转译。全篇共 {total_slides} 页幻灯片，覆盖核心章节大纲、指标卡片、差异化对比矩阵与演讲备注。物理原件已冷归档至 `98-Archives/{month_tag()}/{self.pptx_path.name}`。

"""
        self.markdown_sections.append(frontmatter)

        for idx, slide in enumerate(self.prs.slides, start=1):
            slide_md = self.process_slide(slide, idx, total_slides)
            self.markdown_sections.append(slide_md)
            print(f"  [✓] Slide {idx:02d}/{total_slides:02d} 解析完成")

        final_md = "\n\n".join(self.markdown_sections)
        final_md = re.sub(r"\n{3,}", "\n\n", final_md).strip() + "\n"

        self.output_md_path.write_text(final_md, encoding="utf-8")
        print(f"\n[★] PowerPoint 演示文稿转译完成！")
        print(f"    - 输出文档: {self.output_md_path}")
        return self.output_md_path


# ==============================================================================
# CLI 入口与调度
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="PowerPoint (.pptx) 智能结构化转译引擎 (V1 语义重构生产版)")
    parser.add_argument("pptx_file", nargs="?", help="要转换的 PowerPoint (.pptx) 文件路径 (与 -i/--input 等效)")
    parser.add_argument("-i", "--input", help="要转换的 PowerPoint (.pptx) 文件路径")
    parser.add_argument("-o", "--output", help="输出 Markdown 目标路径 (默认: 20-AI-Zone/20-Memory/21-Library/{slug}.md 或 {input}.md)")
    parser.add_argument("--attachments", default="99-Assets/10-Attachments", help="多媒体附件存储目录 (默认: 99-Assets/10-Attachments)")
    parser.add_argument("--dropzone", action="store_true", help="扫描 00-DropZone/ 下所有 PPTX 文件并执行静默归档转换")
    parser.add_argument("--archive", action="store_true", help="转换完成后将原 PPTX 文件移动冷归档至 99-Assets/98-Archives/")
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

        pptx_files = []
        for p in sorted(dropzone_dir.rglob("*.pptx")):
            rel_parts = p.relative_to(dropzone_dir).parts
            if any(part == "91-Temp" or part.startswith(".") or part.startswith("_") for part in rel_parts):
                continue
            if p.is_file():
                pptx_files.append(p)

        if not pptx_files:
            print("[*] 00-DropZone/ 未发现待处理的 .pptx 文件（已自动跳过 91-Temp/ 与 _ 草稿区）。")
            return

        print(f"[*] 发现 {len(pptx_files)} 个 PPTX 文件，准备执行流转...")
        for pptx_p in pptx_files:
            slug = pptx_p.stem
            out_md = library_dir / f"{slug}.md"
            dest_pptx = archive_dir / pptx_p.name

            if args.dry_run:
                print(f"  [DryRun] 计划处理: {pptx_p.relative_to(vault_root)}")
                print(f"    - 伴生卡片: 20-AI-Zone/20-Memory/21-Library/{slug}.md")
                print(f"    - 原件冷归档: 98-Archives/{month_tag()}/{pptx_p.name}")
                continue

            print(f"\n[*] 正在转换: {pptx_p.name}")
            converter = PptxToMarkdownConverter(pptx_p, out_md, att_dir)
            converter.convert()
            archive_dir.mkdir(parents=True, exist_ok=True)
            n = 1
            while dest_pptx.exists() and dest_pptx.resolve() != pptx_p.resolve():
                dest_pptx = archive_dir / f"{pptx_p.stem}_{n}{pptx_p.suffix}"
                n += 1
            shutil.move(str(pptx_p), str(dest_pptx))
            print(f"  [✓] 原件已安全冷归档至: 98-Archives/{month_tag()}/{dest_pptx.name}")
            print(f"  [✓] 伴生卡片已生成: 20-AI-Zone/20-Memory/21-Library/{slug}.md")
        return

    # 2. 单文件独立转换模式
    target_input = args.input if args.input else args.pptx_file
    if not target_input:
        parser.print_help()
        sys.exit(1)

    pptx_in = Path(target_input).resolve()
    if not pptx_in.exists():
        print(f"[-] 文件不存在: {pptx_in}")
        sys.exit(1)

    in_dropzone = False
    try:
        rel = pptx_in.relative_to(vault_root)
        if len(rel.parts) > 0 and rel.parts[0] == "00-DropZone":
            in_dropzone = True
    except ValueError:
        pass

    if args.output:
        out_p = Path(args.output).resolve()
    else:
        if in_dropzone:
            out_p = vault_root / "20-AI-Zone/20-Memory/21-Library" / f"{pptx_in.stem}.md"
        else:
            out_p = pptx_in.with_suffix(".md")

    should_archive = args.archive or in_dropzone
    if os.environ.get("GEWU_ATLAS_MANAGED_FMT") == "1":
        should_archive = False
    att_p = vault_root / args.attachments

    if args.dry_run:
        print(f"[*] [DryRun] 待处理文件: {pptx_in.name}")
        print(f"    - 输入路径: {pptx_in}")
        print(f"    - 目标伴生卡片: {out_p}")
        if should_archive:
            print(f"    - 计划冷归档原件至: 98-Archives/{month_tag()}/{pptx_in.name}")
        return

    if out_p.exists():
        print(f"[COLLISION] 目标卡片已存在: {out_p}", file=sys.stderr)
        sys.exit(1)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    converter = PptxToMarkdownConverter(pptx_in, out_p, att_p)
    converter.convert()

    if should_archive:
        archive_dir = monthly_archive_dir(vault_root / "99-Assets/98-Archives")
        archive_dir.mkdir(parents=True, exist_ok=True)
        dest_pptx = archive_dir / pptx_in.name
        n = 1
        while dest_pptx.exists() and dest_pptx.resolve() != pptx_in.resolve():
            dest_pptx = archive_dir / f"{pptx_in.stem}_{n}{pptx_in.suffix}"
            n += 1
        shutil.move(str(pptx_in), str(dest_pptx))
        print(f"  [✓] 原件已安全冷归档至: 98-Archives/{month_tag()}/{dest_pptx.name}")
        print(f"  [✓] 伴生卡片已生成: {out_p}")


if __name__ == "__main__":
    main()
