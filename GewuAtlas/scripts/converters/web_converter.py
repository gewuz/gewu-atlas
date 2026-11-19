#!/usr/bin/env python3
"""
web_converter.py - 智能网页深度剪藏与转译引擎 (本地 Python 原生版)

架构与核心契约：
【本地 Python 原生抓取引擎】
- 伪装浏览器请求头 (UA/Referer)，使用 Readability-lxml 提取正文，BeautifulSoup 去噪，html2text 转 Markdown；
- 绕过知乎、微信公众号等防盗链下载内嵌插图至 99-Assets/10-Attachments/。

【语义清洗与黄金四层 YAML 注入】
- 剥离网站后缀牛皮癣与乱码；
- KaTeX 美元符号安全防护 ($ -> \\$)；
- 自动识别渠道 (微信公众号/知乎/arXiv/博客等) 并生成标准 YAML Frontmatter；
- 命名契约：严格采用 Web-{YYYYMMDD}-{标题slug}.md 存储至 20-AI-Zone/20-Memory/21-Library/。
"""

__version__ = "1.1"
import os
import re
import sys
import ssl
import shutil
import socket
import ipaddress
import urllib.parse
import urllib.request
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

try:
    from bs4 import BeautifulSoup
    from readability import Document
    import html2text
except ImportError:
    print("[-] 警告: 缺少 bs4/readability/html2text。请在虚拟环境中安装: pip install beautifulsoup4 readability-lxml html2text")


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

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

SSL_CONTEXT = ssl.create_default_context()


def decode_html(raw, content_type=""):
    """BOM, HTTP charset, then HTML meta; do not silently assume UTF-8."""
    if raw.startswith(b'\xef\xbb\xbf'):
        return raw.decode('utf-8-sig')
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        return raw.decode('utf-16')
    charset = re.search(r'charset\s*=\s*["\']?([A-Za-z0-9._-]+)', content_type, re.I)
    if not charset:
        for tag in re.findall(rb'<meta\b[^>]*>', raw[:8192], re.I):
            charset = re.search(r'charset\s*=\s*["\']?([A-Za-z0-9._-]+)', tag.decode('ascii', errors='ignore'), re.I)
            if charset:
                break
    encoding = charset.group(1) if charset else 'utf-8'
    try:
        return raw.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        print('[WARN] HTML编码声明无效或字节不匹配: '+encoding, file=sys.stderr)
        return raw.decode('utf-8', errors='replace')


def validate_url_security(url: str):
    from url_ingest_helper import check_url
    check_url(VAULT_ROOT, url)


def safe_open(request, *, timeout):
    # Revalidate every redirect, including image requests. Initial validation alone is insufficient.
    from url_ingest_helper import check_url
    class SafeRedirect(urllib.request.HTTPRedirectHandler):
        http_error_308 = urllib.request.HTTPRedirectHandler.http_error_302
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            check_url(VAULT_ROOT, newurl)
            return super().redirect_request(req, fp, 307 if code == 308 else code, msg, headers, newurl)
    check_url(VAULT_ROOT, request.full_url)
    opener = urllib.request.build_opener(SafeRedirect(), urllib.request.HTTPSHandler(context=SSL_CONTEXT))
    response = opener.open(request, timeout=timeout)
    try: check_url(VAULT_ROOT, response.geturl())
    except Exception:
        response.close()
        raise
    return response


def clean_filename(name: str) -> str:
    """清理文件名中的非法字符"""
    clean = re.sub(r'[\\/*?:"<>|]', "", name)
    clean = clean.strip().replace(" ", "_")
    return clean[:50] if clean else "webclip"


def clean_title_noise(title: str) -> str:
    """剔除常见网站标题后缀"""
    noise_patterns = [
        r"\s*[-_|]\s*(知乎|掘金|微信公众平台|CSDN博客|简书|博客园|Medium|TechCrunch|GitHub).*$",
        r"\s*[-_|]\s*(SegmentFault 思否|InfoQ|36氪|钛媒体|少数派|架构师前线|少数派 sspai).*$"
    ]
    cleaned = title.strip()
    for pat in noise_patterns:
        cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def infer_channel(url: str) -> str:
    """根据 URL 域名推断采集渠道"""
    if not url:
        return "网页剪藏"
    domain = urllib.parse.urlparse(url).netloc.lower()
    if "weixin.qq.com" in domain or "mp.weixin" in domain:
        return "微信公众号"
    if "zhihu.com" in domain:
        return "知乎"
    if "arxiv.org" in domain:
        return "arXiv"
    if "sina.com" in domain or "weibo.com" in domain:
        return "新浪"
    if "toutiao.com" in domain:
        return "今日头条"
    if "github.com" in domain:
        return "GitHub"
    if "juejin.cn" in domain:
        return "掘金"
    if "csdn.net" in domain:
        return "CSDN"
    if "medium.com" in domain:
        return "Medium"
    if "infoq.cn" in domain:
        return "InfoQ"
    return "网络媒体"


def escape_dollar(text: str) -> str:
    """转义裸美元符号，防止 KaTeX 公式解析崩溃"""
    parts = text.split("```")
    for i in range(0, len(parts), 2):
        inline_parts = parts[i].split("`")
        for j in range(0, len(inline_parts), 2):
            inline_parts[j] = re.sub(r'(?<!\\)\$', r'\\$', inline_parts[j])
        parts[i] = "`".join(inline_parts)
    return "```".join(parts)


# ==============================================================================
# 本地 Python 抓取与脱水引擎
# ==============================================================================

def scrape_with_local_python(url: str) -> dict:
    """
    本地 Python 伪装抓取 + Readability-lxml 正文脱水
    """
    validate_url_security(url)
    print(f"  [Local Engine] 启动本地 Python 请求拉取: {url}")
    headers = dict(DEFAULT_HEADERS)
    headers["Referer"] = url
    req = urllib.request.Request(url, headers=headers)

    raw_bytes = None
    encoding = "utf-8"
    try:
        with safe_open(req, timeout=15) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw_bytes = resp.read()
            if "charset=" in content_type:
                encoding = content_type.split("charset=")[-1].strip()
    except Exception as e:
        print(f"  [-] 本地抓取失败: {e}")
        return None

    html_str = decode_html(raw_bytes, content_type)

    soup = BeautifulSoup(html_str, "html.parser")

    # 提取标题
    title = ""
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    elif soup.title and soup.title.string:
        title = soup.title.string.strip()

    # 提取描述
    desc = ""
    og_desc = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
    if og_desc and og_desc.get("content"):
        desc = og_desc["content"].strip()

    # 剥离全局噪音
    for tag in soup(["script", "style", "noscript", "svg", "button", "input", "form", "iframe"]):
        tag.decompose()

    # 智能定位正文
    container = soup.find("article") or soup.find("main") or \
                soup.find(id=re.compile(r"(article|content|post-body|main-article)", re.I)) or \
                soup.find(class_=re.compile(r"(article|content|post-body|main-article)", re.I))

    if not container:
        try:
            doc = Document(html_str)
            summary_html = doc.summary(html_partial=True)
            container = BeautifulSoup(summary_html, "html.parser")
        except Exception:
            container = soup.body if soup.body else soup

    for sub in container(["header", "footer", "nav", "aside"]):
        sub.decompose()

    # 表格先按 DOM 结构转 GFM 并剥离，防止 html2text 的非转义管道符与单元格折行破坏表格
    table_map = md_table.extract_html_tables(container)

    converter = html2text.HTML2Text()
    converter.ignore_links = False
    converter.ignore_images = False
    converter.body_width = 0
    converter.single_line_break = False

    md = md_table.restore_tables(converter.handle(str(container)), table_map)
    print(f"  [✓] 本地 Python 脱水完成 (获取正文 {len(md)} 字符)")
    return {
        "title": title,
        "description": desc,
        "markdown": md,
        "metadata": {},
        "engine": "local_python"
    }


# ==============================================================================
# 图片本地化与 Obsidian 双链替换
# ==============================================================================

def download_and_replace_images(
    markdown_text: str,
    base_url: str,
    slug_prefix: str,
    attachments_dir: Path
) -> str:
    """
    扫描 Markdown 中的所有外链图片并下载至 99-Assets/10-Attachments/，替换为 ![[img]] 双链
    """
    attachments_dir.mkdir(parents=True, exist_ok=True)
    img_pattern = re.compile(r'!\[(.*?)\]\((https?://[^\s\)]+)\)')
    matches = img_pattern.findall(markdown_text)
    if not matches:
        return markdown_text

    print(f"  [*] 发现 {len(matches)} 张外链插图，正在执行本地化下载...")
    headers = dict(DEFAULT_HEADERS)
    if base_url:
        parsed_base = urllib.parse.urlparse(base_url)
        headers["Referer"] = f"{parsed_base.scheme}://{parsed_base.netloc}"

    replaced_md = markdown_text
    downloaded_count = 0

    for idx, (alt_text, img_url) in enumerate(matches, 1):
        parsed = urllib.parse.urlparse(img_url)
        ext = os.path.splitext(parsed.path)[1].lower()
        if not ext or len(ext) > 5 or ext not in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"]:
            if "wx_fmt=png" in img_url or "format=png" in img_url:
                ext = ".png"
            elif "wx_fmt=gif" in img_url:
                ext = ".gif"
            elif "wx_fmt=webp" in img_url:
                ext = ".webp"
            else:
                ext = ".jpg"

        dest_name = f"{slug_prefix}_img_{idx:02d}{ext}"
        dest_path = attachments_dir / dest_name

        if not dest_path.exists():
            try:
                validate_url_security(img_url)
            except ValueError as e:
                print(f"  [-] 图片 URL 安全校验拦截，保留原链接: {e}")
                continue
            req = urllib.request.Request(img_url, headers=headers)
            try:
                with safe_open(req, timeout=10) as resp:
                    img_data = resp.read()
                    if len(img_data) >= 200:
                        dest_path.write_bytes(img_data)
                        downloaded_count += 1
                    else:
                        continue
            except Exception as e:
                # 图片下载失败保持原 Markdown 链接，不影响正文完整性
                continue

        # 就地替换为 Obsidian 原生双链
        orig_markdown_ref = f"![{alt_text}]({img_url})"
        obsidian_ref = f"![[{dest_name}]]"
        if os.environ.get("GEWU_ATLAS_ANNOTATE_IMAGES") == "1":
            obsidian_ref += "\n> [!NOTE] 图示解析与要素拆解\n> （待 Agent 读取图片后回填）"
        replaced_md = replaced_md.replace(orig_markdown_ref, obsidian_ref)

    print(f"  [✓] 成功本地化保存 {downloaded_count} 张插图至 {attachments_dir.name}/")
    return replaced_md


# ==============================================================================
# 文章生成与 Frontmatter 组装
# ==============================================================================

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


def clip_web_url(
    target_url: str,
    output_path: Path = None,
    attachments_dir: Path = None
) -> Path:
    """
    网页剪藏主入口：
    本地 Python 抓取 -> 图片本地化 -> 语义清洗与规范 Markdown
    """
    vault_root = VAULT_ROOT
    today_date = datetime.now().strftime("%Y%m%d")
    today_iso = datetime.now().strftime("%Y-%m-%d")

    if not attachments_dir:
        attachments_dir = vault_root / "99-Assets/10-Attachments"

    print(f"\n======================================================================")
    print(f"[*] 启动网页深度剪藏: {target_url}")
    print(f"======================================================================")

    # 1. 执行本地 Python 抓取
    scrape_data = scrape_with_local_python(target_url)

    if not scrape_data:
        raise RuntimeError(f"抓取网页失败 (本地 Python 引擎不可用): {target_url}")

    # 2. 语义清洗与规范标题
    raw_title = scrape_data.get("title") or "网页剪藏文章"
    clean_title = clean_title_noise(raw_title)
    if not clean_title:
        clean_title = "网页剪藏文章"

    title_slug = clean_filename(clean_title)
    channel = infer_channel(target_url)

    # 3. 伴生卡片标准命名: Web-YYYYMMDD-{标题slug}.md
    default_filename = f"Web-{today_date}-{title_slug}.md"
    if output_path:
        final_out_md = Path(output_path).resolve()
    else:
        final_out_md = vault_root / "20-AI-Zone/20-Memory/21-Library" / default_filename

    if final_out_md.exists():
        print(f"[COLLISION] 目标卡片已存在: {final_out_md}", file=sys.stderr)
        return False
    final_out_md.parent.mkdir(parents=True, exist_ok=True)

    # 4. 图片本地化与 KaTeX 转义
    raw_md = scrape_data.get("markdown", "")
    safe_md = escape_dollar(raw_md)
    localized_md = download_and_replace_images(
        safe_md,
        base_url=target_url,
        slug_prefix=f"Web-{today_date}-{title_slug}",
        attachments_dir=attachments_dir
    )

    # 清理多余空行
    clean_body = re.sub(r"\n{3,}", "\n\n", localized_md).strip()

    # 5. 提取或生成一句话摘要 (50字以内)
    raw_desc = scrape_data.get("description", "").strip()
    if raw_desc and len(raw_desc) > 10:
        summary_text = raw_desc[:60].replace("\n", " ").replace('"', '\\"')
    else:
        # 取正文前 60 字
        clean_sample = re.sub(r"[#*`\[\]\(\)\n]", "", clean_body)[:60]
        summary_text = f"{clean_title} 核心内容深度剪藏：{clean_sample}"

    # 6. 注入黄金四层 YAML Frontmatter
    status_val = get_status_for_domain(vault_root, out_path=final_out_md)
    safe_title = fm_scalar(clean_title)
    safe_summary = fm_scalar(summary_text)
    safe_channel = fm_scalar(channel)
    frontmatter = f"""---
doc_type: library
title: "{safe_title}"
source_type: article
channel: "{safe_channel}"
summary: "{safe_summary}"
original_url: "{fm_scalar(target_url)}"
source: "{fm_scalar(target_url)}"
processed: false
status: {status_val}
updated: {today_iso}
tags: ["webclip", "article", "{safe_channel}"]
extracted_to: []
---

# {clean_title}

> [!NOTE] 网页深度剪藏说明
> 本文采编自 [{channel} 原文链接]({target_url})。抓取引擎：`{scrape_data['engine']}`。内嵌插图已离线化托管于 `99-Assets/10-Attachments/`。

"""
    n_win = write_in_windows(final_out_md, frontmatter, clean_body, label=default_filename)
    print(f"\n[✓] 网页剪藏已成功落盘！")
    print(f"    - 伴生卡片: {final_out_md}")
    print(f"    - 分窗数量: {n_win}")
    print(f"    - 原文来源: {target_url} ({channel})")
    print(f"    - 使用引擎: {scrape_data['engine']}")
    return final_out_md


# ==============================================================================
# CLI 入口与 DropZone / 批量 URL 处理
# ==============================================================================

def extract_urls_from_file(file_path: Path) -> list:
    """从 .url 快捷文件或 .urls / 纯文本文件中提取所有有效 HTTP/HTTPS URL"""
    content = file_path.read_text(encoding="utf-8", errors="ignore")
    # Windows .url 文件: URL=https://...
    m = re.search(r'URL=(https?://[^\r\n\s]+)', content, re.I)
    if m:
        return [m.group(1).strip()]
    # 提取所有包含的 http(s) URL 并去重
    found = re.findall(r'https?://[^\s\)\"\'<>]+', content)
    return list(dict.fromkeys(found))


def main():
    parser = argparse.ArgumentParser(description="智能网页深度剪藏与转译引擎 (三级降级防御架构版)")
    parser.add_argument("url", nargs="?", help="要剪藏的在线网页 URL 或 .url 文件 (与 -i/--input 等效)")
    parser.add_argument("-i", "--input", help="要剪藏的在线网页 URL 或包含 URL 的文件")
    parser.add_argument("-o", "--output", help="输出 Markdown 目标路径 (默认: 20-AI-Zone/20-Memory/21-Library/Web-YYYYMMDD-{slug}.md)")
    parser.add_argument("--project-id",help="URL清单的目标域")
    parser.add_argument("--by", help="URL清单内容票的真实执行身份")
    parser.add_argument("--urls-file", help="DropZone纯URL Markdown清单（委托url-ingest，不读取日记）")
    parser.add_argument("--attachments", default="99-Assets/10-Attachments", help="多媒体附件存储目录")
    parser.add_argument("--dropzone", action="store_true", help="扫描 00-DropZone/ 下所有 .url / .urls 快捷方式或网址列表并执行静默归档剪藏")
    parser.add_argument("--dry-run", action="store_true", help="演练模式：仅打印计划操作与路径，不发起真实抓取")

    args = parser.parse_args()
    vault_root = VAULT_ROOT
    today_date = datetime.now().strftime("%Y%m%d")

    if args.urls_file:
        command=['bash',str(vault_root/'.gewuz'),'url-ingest',args.urls_file,'--project-id',args.project_id or 'AI-Zone']
        if args.by:
            command += ['--by',args.by]
        if args.dry_run:
            command.append('--dry-run')
        import subprocess
        return subprocess.call(command)

    # 2. DropZone 待办区扫描 (.url / .urls 资产快捷方式与网址列表)
    if args.dropzone:
        dropzone_dir = vault_root / "00-DropZone"
        archive_dir = monthly_archive_dir(vault_root / "99-Assets/98-Archives")

        if not dropzone_dir.exists():
            print(f"[-] 目录不存在: {dropzone_dir}")
            return

        url_files = []
        for p in sorted(dropzone_dir.rglob("*")):
            rel_parts = p.relative_to(dropzone_dir).parts
            if any(part == "91-Temp" or part.startswith(".") or part.startswith("_") for part in rel_parts):
                continue
            if p.is_file() and p.suffix.lower() in [".url", ".urls"]:
                url_files.append(p)

        if not url_files:
            print("[*] 00-DropZone/ 未发现待处理的 .url / .urls 快捷文件（已自动跳过 91-Temp/ 与 _ 草稿区）。")
            return

        print(f"[*] 发现 {len(url_files)} 个 .url / .urls 文件，准备执行批量抓取...")
        failures = 0
        for uf in url_files:
            target_urls = extract_urls_from_file(uf)
            if not target_urls:
                failures += 1
                print(f"  [!] 跳过未发现有效 URL 的文件: {uf.name}")
                continue

            dest_archive = archive_dir / uf.name

            if args.dry_run:
                print(f"  [DryRun] 计划处理: {uf.relative_to(vault_root)}")
                for u in target_urls:
                    print(f"    - 目标 URL: {u}")
                print(f"    - 原件冷归档: 98-Archives/{month_tag()}/{uf.name}")
                continue

            try:
                success = True
                for u in target_urls:
                    success = bool(clip_web_url(u)) and success
                if not success:
                    failures += 1
                    continue
                archive_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(uf), str(dest_archive))
                print(f"  [✓] 原快捷方式已安全冷归档至: 98-Archives/{month_tag()}/{uf.name}")
            except Exception as e:
                failures += 1
                print(f"  [!] 抓取处理失败: {uf.name} ({e})")
        return 1 if failures else 0

    # 3. 单 URL 独立剪藏模式
    target_in = args.input if args.input else args.url
    if not target_in:
        parser.print_help()
        sys.exit(1)

    target_url = target_in.strip()
    if target_url.endswith((".url", ".urls")) and Path(target_url).exists():
        extracted = extract_urls_from_file(Path(target_url))
        if extracted:
            target_url = extracted[0]

    if not target_url.startswith("http://") and not target_url.startswith("https://"):
        print(f"[-] 错误: 输入必须是合法的 HTTP/HTTPS URL，收到: {target_url}")
        sys.exit(1)

    if args.dry_run:
        print(f"[*] [DryRun] 待剪藏 URL: {target_url}")
        print(f"    - 推断渠道: {infer_channel(target_url)}")
        print(f"    - 抓取策略: 本地 Python 抓取引擎")
        print(f"    - 计划落盘至: 20-AI-Zone/20-Memory/21-Library/Web-{today_date}-{{slug}}.md")
        return

    out_p = Path(args.output).resolve() if args.output else None
    att_p = vault_root / args.attachments
    try:
        return 0 if clip_web_url(target_url, output_path=out_p, attachments_dir=att_p) else 1
    except ValueError as ve:
        print(f"[SECURITY_BLOCKED] {ve}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[-] 网页剪藏异常: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    sys.exit(main())
