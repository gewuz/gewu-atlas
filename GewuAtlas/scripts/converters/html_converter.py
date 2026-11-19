#!/usr/bin/env python3
"""
html_converter.py - 智能本地 HTML (.html / .htm) 文档解析与 Markdown 结构化转换引擎 (V1)

核心功能与架构：
1. 本地离线 HTML 解析：支持单页归档文件、浏览器另存为页面与 SingleFile 导出的 HTML。
2. 语义容器精准脱水：优先识别 <article>、<main> 或通过 Readability 算法定位正文，剔除导航栏、页脚与无关广告。
3. Base64 与多模态图片抽取：提取 data:image/... 内嵌图片并转存至 99-Assets/10-Attachments/，就地替换为 ![[img]] 双链。
4. KaTeX 安全转义：自动转义裸 $ 符号为 \\$，防止数学公式误渲染。
5. DropZone 自动化流转与冷归档：
   - 自动递归扫描 00-DropZone/ 下所有 .html / .htm 文档（严格跳过 91-Temp/）。
   - 伴生卡片落盘至 20-AI-Zone/20-Memory/21-Library/{slug}.md。
   - 原件安全移动冷归档至 98-Archives 当月子目录 (YYMM，如 2610)。
"""

__version__ = "1.1"
import os
import re
import sys
import base64
import shutil
import argparse
from html.parser import HTMLParser
from pathlib import Path
from datetime import datetime

import md_table
from archive_month import monthly_archive_dir, month_tag

_CONVERTERS_DIR = Path(__file__).resolve().parent
if str(_CONVERTERS_DIR) not in sys.path:
    sys.path.insert(0, str(_CONVERTERS_DIR))
from core.project import get_status_for_domain

# 自包含契约：缺 bs4/readability/html2text 时降级为标准库保真转译，严禁直接退出
HAVE_RICH_DEPS = True
try:
    from bs4 import BeautifulSoup
    from readability import Document
    import html2text
except ImportError:
    HAVE_RICH_DEPS = False


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


def clean_filename(name: str) -> str:
    """清理文件名中的非法字符"""
    clean = re.sub(r'[\\/*?:"<>|]', "", name)
    clean = clean.strip().replace(" ", "_")
    return clean[:60] if clean else "html_doc"


def clean_title_noise(title: str) -> str:
    """剔除常见网站标题后缀"""
    noise_patterns = [
        r"\s*[-_|]\s*(知乎|掘金|微信公众平台|CSDN博客|简书|博客园|Medium|TechCrunch|GitHub).*$",
        r"\s*[-_|]\s*(SegmentFault 思否|InfoQ|36氪|钛媒体|少数派|架构师前线).*$"
    ]
    cleaned = title.strip()
    for pat in noise_patterns:
        cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def escape_dollar(text: str) -> str:
    """转义裸美元符号，防止 KaTeX 公式解析崩溃"""
    parts = text.split("```")
    for i in range(0, len(parts), 2):
        inline_parts = parts[i].split("`")
        for j in range(0, len(inline_parts), 2):
            inline_parts[j] = re.sub(r'(?<!\\)\$', r'\\$', inline_parts[j])
        parts[i] = "`".join(inline_parts)
    return "```".join(parts)


def save_base64_image(data_uri: str, dest_dir: Path, prefix: str, idx: int) -> str:
    """将 Base64 Data URI 解码并存储至附件库"""
    try:
        header, encoded = data_uri.split(",", 1)
        ext = ".png"
        if "image/jpeg" in header or "image/jpg" in header:
            ext = ".jpg"
        elif "image/webp" in header:
            ext = ".webp"
        elif "image/gif" in header:
            ext = ".gif"
        elif "image/svg+xml" in header:
            ext = ".svg"

        data = base64.b64decode(encoded)
        if len(data) < 200:
            return None

        filename = f"{prefix}_img_{idx:02d}{ext}"
        dest_path = dest_dir / filename
        cnt = 1
        while dest_path.exists():
            if dest_path.read_bytes() == data:
                return filename
            filename = f"{prefix}_img_{idx:02d}_{cnt}{ext}"
            dest_path = dest_dir / filename
            cnt += 1
        dest_path.write_bytes(data)
        return filename
    except Exception as e:
        print(f"    [!] Base64 图片解码失败: {e}")
        return None


def convert_html_file(
    html_path: Path,
    output_md_path: Path,
    attachments_dir: Path
) -> Path:
    """本地 HTML 文件结构化脱水转换核心逻辑"""
    html_path = Path(html_path).resolve()
    output_md_path = Path(output_md_path).resolve()
    attachments_dir = Path(attachments_dir).resolve()

    attachments_dir.mkdir(parents=True, exist_ok=True)
    output_md_path.parent.mkdir(parents=True, exist_ok=True)

    slug = html_path.stem
    today_str = datetime.now().strftime("%Y-%m-%d")

    # 尝试多种编码读取 HTML
    raw_html = ""
    for enc in ["utf-8", "gbk", "gb18030", "latin-1"]:
        try:
            raw_html = html_path.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue

    if not raw_html:
        raise ValueError(f"无法读取文件编码: {html_path}")

    soup = BeautifulSoup(raw_html, "html.parser")

    # 1. 提取标题与元数据
    doc_title = None
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        doc_title = og_title["content"].strip()
    elif soup.title and soup.title.string:
        doc_title = soup.title.string.strip()

    if not doc_title:
        h1 = soup.find("h1")
        if h1 and h1.get_text():
            doc_title = h1.get_text().strip()

    doc_title = clean_title_noise(doc_title) if doc_title else slug

    # 2. 剥离全局噪音标签
    for tag in soup(["script", "style", "noscript", "svg", "button", "input", "form", "iframe"]):
        tag.decompose()

    # 3. 智能定位正文语义容器
    container = soup.find("article") or soup.find("main") or \
                soup.find(id=re.compile(r"(article|content|post-body|main-article)", re.I)) or \
                soup.find(class_=re.compile(r"(article|content|post-body|main-article)", re.I))

    if not container:
        # 使用 Readability 算法兜底提取正文
        try:
            doc = Document(raw_html)
            summary_html = doc.summary(html_partial=True)
            container = BeautifulSoup(summary_html, "html.parser")
        except Exception:
            container = soup.body if soup.body else soup

    # 剥离容器内部导航与页脚
    for sub in container(["header", "footer", "nav", "aside"]):
        sub.decompose()

    # 4. 本地化提取 Base64 图片与本地相对图片
    img_idx = 1
    extracted_imgs = []
    prefix = clean_filename(slug)

    for img in container.find_all("img"):
        src = (
            img.get("data-src") or
            img.get("data-original") or
            img.get("data-actualsrc") or
            img.get("src")
        )
        if not src:
            img.decompose()
            continue

        if src.startswith("data:image"):
            saved_name = save_base64_image(src, attachments_dir, prefix, img_idx)
            if saved_name:
                extracted_imgs.append(saved_name)
                img.replace_with(soup.new_string(f"\n\n![[{saved_name}]]\n> [!NOTE] 图示解析与要素拆解\n> - **核心意图**：\n> - **图内要素与指标**：\n> - **逻辑机制 / 数据矩阵**：\n\n"))
                img_idx += 1
            else:
                img.decompose()
        else:
            # 尝试查找本地同级或子目录图片（防逃逸校验）
            raw_target = src.split("?")[0].strip()
            candidate = (html_path.parent / raw_target).resolve()
            is_safe = False
            try:
                candidate.relative_to(html_path.parent.resolve())
                is_safe = True
            except ValueError:
                try:
                    candidate.relative_to(VAULT_ROOT.resolve())
                    is_safe = True
                except ValueError:
                    is_safe = False

            if is_safe and candidate.exists() and candidate.is_file():
                ext = candidate.suffix
                dest_img_name = f"{prefix}_img_{img_idx:02d}{ext}"
                dest_path = attachments_dir / dest_img_name
                cnt = 1
                while dest_path.exists():
                    dest_img_name = f"{prefix}_img_{img_idx:02d}_{cnt}{ext}"
                    dest_path = attachments_dir / dest_img_name
                    cnt += 1
                shutil.copy2(str(candidate), str(dest_path))
                extracted_imgs.append(dest_img_name)
                img.replace_with(soup.new_string(f"\n\n![[{dest_img_name}]]\n> [!NOTE] 图示解析与要素拆解\n> - **核心意图**：\n> - **图内要素与指标**：\n> - **逻辑机制 / 数据矩阵**：\n\n"))
                img_idx += 1

    # 5. 表格先按 DOM 结构转 GFM 并剥离，防止 html2text 的非转义管道符与单元格折行破坏表格
    table_map = md_table.extract_html_tables(container)

    # 6. 转为 Markdown 文本
    converter = html2text.HTML2Text()
    converter.ignore_links = False
    converter.ignore_images = True  # 图片已就地替换为 ![[img]]
    converter.body_width = 0
    converter.single_line_break = False

    raw_md = md_table.restore_tables(converter.handle(str(container)), table_map)
    # 安全转义裸美元符号
    safe_md = escape_dollar(raw_md)
    # 清理多余空行
    clean_body = re.sub(r"\n{3,}", "\n\n", safe_md).strip()

    # 7. 构造标准 YAML Frontmatter
    safe_title = doc_title.replace('"', '\\"').replace("\n", " ").strip()
    status_val = get_status_for_domain(VAULT_ROOT, out_path=output_md_path)
    frontmatter = f"""---
doc_type: library
title: "{safe_title}"
source_type: article
channel: "网页剪藏"
summary: "{safe_title} 离线 HTML 网页文档结构化脱水转译"
original_file: "{html_path.name}"
processed: false
status: {status_val}
updated: {today_str}
tags: [html, article, webclip]
extracted_to: []
---

# {doc_title}

> [!NOTE] 离线网页转译提示
> 本文由本地 HTML 文档结构化清洗脱水转译。物理原件已冷归档至 `98-Archives/{month_tag()}/{html_path.name}`，内嵌图片已抽离至 `99-Assets/10-Attachments/`。

"""
    n_win = write_in_windows(output_md_path, frontmatter, clean_body, label=output_md_path.name)
    print(f"[✓] HTML 转译完成！Markdown 已生成至: {output_md_path}")
    print(f"    - 分窗数量: {n_win}")
    print(f"    - 抽取内嵌图片数: {len(extracted_imgs)} 张")
    return output_md_path


# ==============================================================================
# 标准库降级路径 (自包含契约：无第三方依赖环境下的保真转译)
# ==============================================================================
_BLOCK_TAGS = {"p", "div", "section", "article", "main", "li", "blockquote",
               "ul", "ol", "table", "tr", "td", "th", "header", "footer",
               "nav", "aside", "figure", "figcaption", "dd", "dt", "dl"}
_SKIP_TAGS = ("script", "style", "noscript")


class _BasicHTMLText(HTMLParser):
    """stdlib 降级抽取器：块级标签断行、h1-h6 映射为 Markdown 标题、剥离脚本样式噪音。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def _newline(self):
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_starttag(self, tag, attrs):
        if self._skip:
            return
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._newline()
            self.parts.append("\n" + "#" * int(tag[1]) + " ")
        elif tag == "br":
            self._newline()
        elif tag in _BLOCK_TAGS:
            self._newline()

    def handle_endtag(self, tag):
        if self._skip:
            if tag in _SKIP_TAGS:
                self._skip -= 1
            return
        if tag == "title":
            self._in_title = False
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6", *_BLOCK_TAGS):
            self._newline()

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return
        self.parts.append(data)


def convert_html_file_basic(html_path: Path, output_md_path: Path, attachments_dir: Path = None) -> Path:
    """标准库降级转换：缺 bs4/readability/html2text 时的保真脱水。

    能力边界：块级文本抽取 + 标题层级映射 + KaTeX 转义；
    不含语义容器定位、Base64 图片抽取与表格 GFM 转换（安装依赖后自动恢复完整管线）。
    """
    html_path = Path(html_path).resolve()
    output_md_path = Path(output_md_path).resolve()
    output_md_path.parent.mkdir(parents=True, exist_ok=True)

    raw_html = html_path.read_text(encoding="utf-8", errors="replace")
    parser = _BasicHTMLText()
    parser.feed(raw_html)
    parser.close()

    slug = html_path.stem
    title_raw = parser.title.strip()
    doc_title = clean_title_noise(title_raw) if title_raw else slug
    safe_title = doc_title.replace('"', '\\"').replace("\n", " ").strip()
    status_val = get_status_for_domain(VAULT_ROOT, out_path=output_md_path)
    today_str = datetime.now().strftime("%Y-%m-%d")

    body = "".join(parser.parts)
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", escape_dollar(body)).strip()

    frontmatter = f"""---
doc_type: library
title: "{safe_title}"
source_type: article
channel: "网页剪藏"
summary: "{safe_title} 离线 HTML 网页文档保真转译 (降级模式: 无语义容器定位与图片抽取)"
original_file: "{html_path.name}"
processed: false
status: {status_val}
updated: {today_str}
tags: [html, article, webclip]
extracted_to: []
---

# {doc_title}

> [!NOTE] 离线网页转译提示
> 本文由本地 HTML 文档在标准库降级模式下保真转译。物理原件已冷归档，安装 beautifulsoup4 / readability-lxml / html2text 后自动恢复语义容器定位、Base64 图片抽取与表格 GFM 转换。

"""
    n_win = write_in_windows(output_md_path, frontmatter, body, label=output_md_path.name)
    print("[✓] HTML 转译完成 (降级模式: 未安装 bs4/readability/html2text)")
    print(f"    - 输出: {output_md_path}")
    print(f"    - 分窗数量: {n_win}")
    return output_md_path


def write_in_windows(out_path, header: str, body: str, window_chars: int = 3000, label: str = "") -> int:
    """按字符窗口流式追加落盘（对齐 PDF 滑动窗口组装）。

    切点优先对齐标题行与空行，且不切在 ``` 代码块内部，避免劈开表格、代码与图片引用。
    返回窗口数；短篇（不足一窗）等价于整篇一次写入。
    """
    lines = body.split("\n")
    windows, buf, size, in_fence = [], [], 0, False

    def flush():
        nonlocal buf, size
        if buf:
            windows.append(buf)
        buf, size = [], 0

    for line in lines:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and size >= window_chars and line.startswith("#"):
            flush()
        buf.append(line)
        size += len(line) + 1
        if not in_fence and size >= window_chars and not line.strip():
            flush()
    flush()

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header)
        for i, win in enumerate(windows, 1):
            f.write("\n".join(win).rstrip("\n") + "\n\n")
            print(f"    [窗口 {i}/{len(windows)}] {label} 追加 {sum(len(x) + 1 for x in win)} 字符")
    return len(windows)


def main():
    parser = argparse.ArgumentParser(description="智能本地 HTML (.html / .htm) 文档解析与 Markdown 结构化转换引擎 (V1)")
    parser.add_argument("html_file", nargs="?", help="要转换的本地 HTML 文件路径 (与 -i/--input 等效)")
    parser.add_argument("-i", "--input", help="要转换的本地 HTML 文件路径")
    parser.add_argument("-o", "--output", help="输出 Markdown 目标路径 (默认: 20-AI-Zone/20-Memory/21-Library/{slug}.md 或 {input}.md)")
    parser.add_argument("--attachments", default="99-Assets/10-Attachments", help="多媒体附件存储目录 (默认: 99-Assets/10-Attachments)")
    parser.add_argument("--dropzone", action="store_true", help="扫描 00-DropZone/ 下所有 HTML 文档并执行静默归档转换")
    parser.add_argument("--archive", action="store_true", help="转换完成后将原 HTML 文件移动冷归档至 99-Assets/98-Archives/")
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

        html_files = []
        for p in sorted(dropzone_dir.rglob("*")):
            rel_parts = p.relative_to(dropzone_dir).parts
            if any(part == "91-Temp" or part.startswith(".") or part.startswith("_") for part in rel_parts):
                continue
            if p.is_file() and p.suffix.lower() in (".html", ".htm"):
                html_files.append(p)

        if not html_files:
            print("[*] 00-DropZone/ 未发现待处理的 HTML 文件（已自动跳过 91-Temp/ 与 _ 草稿区）。")
            return

        print(f"[*] 发现 {len(html_files)} 个 HTML 文件，准备执行流转...")
        convert_fn = convert_html_file if HAVE_RICH_DEPS else convert_html_file_basic
        for html_p in html_files:
            slug = html_p.stem
            out_md = library_dir / f"{slug}.md"
            dest_html = archive_dir / html_p.name

            if args.dry_run:
                print(f"  [DryRun] 计划处理: {html_p.relative_to(vault_root)}")
                print(f"    - 伴生卡片: 20-AI-Zone/20-Memory/21-Library/{slug}.md")
                print(f"    - 原件冷归档: 98-Archives/{month_tag()}/{html_p.name}")
                continue

            print(f"\n[*] 正在转换: {html_p.name}")
            convert_fn(html_p, out_md, att_dir)
            archive_dir.mkdir(parents=True, exist_ok=True)
            n = 1
            while dest_html.exists() and dest_html.resolve() != html_p.resolve():
                dest_html = archive_dir / f"{html_p.stem}_{n}{html_p.suffix}"
                n += 1
            shutil.move(str(html_p), str(dest_html))
            print(f"  [✓] 原件已安全冷归档至: 98-Archives/{month_tag()}/{dest_html.name}")
            print(f"  [✓] 伴生卡片已生成: 20-AI-Zone/20-Memory/21-Library/{slug}.md")
        return

    # 2. 单文件独立转换模式
    target_input = args.input if args.input else args.html_file
    if not target_input:
        parser.print_help()
        sys.exit(1)

    html_in = Path(target_input).resolve()
    if not html_in.exists():
        print(f"[-] 文件不存在: {html_in}")
        sys.exit(1)

    in_dropzone = False
    try:
        rel = html_in.relative_to(vault_root)
        if len(rel.parts) > 0 and rel.parts[0] == "00-DropZone":
            in_dropzone = True
    except ValueError:
        pass

    if args.output:
        out_p = Path(args.output).resolve()
    else:
        if in_dropzone:
            out_p = vault_root / "20-AI-Zone/20-Memory/21-Library" / f"{html_in.stem}.md"
        else:
            out_p = html_in.with_suffix(".md")

    should_archive = args.archive or in_dropzone
    if os.environ.get("GEWU_ATLAS_MANAGED_FMT") == "1":
        should_archive = False
    att_p = vault_root / args.attachments

    if args.dry_run:
        print(f"[*] [DryRun] 待处理文件: {html_in.name}")
        print(f"    - 输入路径: {html_in}")
        print(f"    - 目标伴生卡片: {out_p}")
        if should_archive:
            print(f"    - 计划冷归档原件至: 98-Archives/{month_tag()}/{html_in.name}")
        return

    if out_p.exists():
        print(f"[COLLISION] 目标卡片已存在: {out_p}", file=sys.stderr)
        sys.exit(1)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    convert = convert_html_file if HAVE_RICH_DEPS else convert_html_file_basic
    convert(html_in, out_p, att_p)

    if should_archive:
        archive_dir = monthly_archive_dir(vault_root / "99-Assets/98-Archives")
        archive_dir.mkdir(parents=True, exist_ok=True)
        dest_html = archive_dir / html_in.name
        n = 1
        while dest_html.exists() and dest_html.resolve() != html_in.resolve():
            dest_html = archive_dir / f"{html_in.stem}_{n}{html_in.suffix}"
            n += 1
        shutil.move(str(html_in), str(dest_html))
        print(f"  [✓] 原件已安全冷归档至: 98-Archives/{month_tag()}/{dest_html.name}")
        print(f"  [✓] 伴生卡片已生成: {out_p}")


if __name__ == "__main__":
    main()
