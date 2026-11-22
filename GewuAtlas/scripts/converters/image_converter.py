#!/usr/bin/env python3
"""
image_converter.py - 智能图片、Draw.io 工程拓扑与 Excalidraw 白板解析转换引擎

遵循 AGENTS.md 准则与知识库架构：
1. 【全格式工程支持】：
   - 位图：JPG, JPEG, PNG, WEBP (物理宽高、色彩空间)
   - 矢量图：SVG (XML 拓扑与文本提取)
   - 工程拓扑：Draw.io (.drawio, .drawio.svg, .drawio.png) (原生/压缩 XML 节点与连线拓扑提取)
   - 手绘白板：Excalidraw (.excalidraw, .excalidraw.svg, .excalidraw.md) (原生 JSON 元素与文本提取)
2. 【身魂分离契约 (Body & Spirit Separation)】：
   - 原件二进制图片/工程图统一隔离至 99-Assets/10-Attachments/{slug}.{ext}（纯扁平约束）；
   - 在 20-AI-Zone/20-Memory/21-Library/{slug}.md 生成专属 Markdown 伴生卡片；
   - 顶部原生内嵌图片/图纸展示 ![[文件名]]。
3. 【AI 多模态视觉解析与纯文本结构化拆解 (零 Mermaid)】：
   - 遵照用户指令，彻底杜绝复杂 Mermaid 渲染；
   - 统一转译为：
     ① 核心设计思想与架构要点 (> [!SUMMARY])；
     ② 模块层级大纲与流转机制（规范数字与圆点列表）；
     ③ 核心组件/要素属性对照矩阵（GFM Markdown 表格）；
     ④ 全量 OCR / 节点文本索引（供 Omnisearch 全库秒级检索）。
4. 【DropZone 批量自动化归档】：
   - 扫描 00-DropZone/ 下所有支持的图像与拓扑格式（强制忽略 91-Temp/ 隔离草稿区）；
   - 自动完成附件归档、伴生卡片创建与双向索引建立。
"""

__version__ = "1.2"
import os
import sys
import re
import json
import zlib
import shutil
import base64
import argparse
import urllib.parse
from pathlib import Path
from datetime import datetime
import xml.etree.ElementTree as ET

_CONVERTERS_DIR = Path(__file__).resolve().parent
if str(_CONVERTERS_DIR) not in sys.path:
    sys.path.insert(0, str(_CONVERTERS_DIR))
from core.project import get_status_for_domain
from core.common import fm_scalar

try:
    from PIL import Image
except ImportError:
    Image = None  # 降级模式：跳过位图元数据提取，身魂分离流程照常 (自包含契约)

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


SUPPORTED_SIMPLE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".drawio", ".excalidraw"}


def detect_file_type(filepath: str) -> tuple:
    """探测复合扩展名与文件大类，返回 (canonical_ext, category)"""
    name = Path(filepath).name.lower()
    if name.endswith(".drawio.svg"):
        return ".drawio.svg", "DRAWIO"
    if name.endswith(".drawio.png"):
        return ".drawio.png", "DRAWIO"
    if name.endswith(".drawio"):
        return ".drawio", "DRAWIO"
    if name.endswith(".excalidraw.svg"):
        return ".excalidraw.svg", "EXCALIDRAW"
    if name.endswith(".excalidraw.png"):
        return ".excalidraw.png", "EXCALIDRAW"
    if name.endswith(".excalidraw.md"):
        return ".excalidraw.md", "EXCALIDRAW"
    if name.endswith(".excalidraw"):
        return ".excalidraw", "EXCALIDRAW"
    
    ext = Path(filepath).suffix.lower()
    if ext == ".svg":
        return ".svg", "SVG"
    if ext in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}:
        return ext, "BITMAP"
    return ext, "UNKNOWN"


def clean_filename(name: str) -> str:
    """清理文件名中的非法字符"""
    clean = re.sub(r'[\\/*?:"<>|]', "", name)
    clean = clean.strip().replace(" ", "_")
    return clean[:60] if clean else "diagram_asset"


def display_path(p: Path, vault_root: Path) -> str:
    """库内显示相对路径，库外（--attachments / -o 显式指向库外）退化为绝对路径。"""
    try:
        return p.relative_to(vault_root).as_posix()
    except ValueError:
        return str(p)


# ==============================================================================
# 物理层元数据提取器 (Draw.io / Excalidraw / SVG / Bitmap)
# ==============================================================================
def _inflate_metadata(payload: bytes, wbits: int = 15) -> bytes:
    limit = 20 * 1024 * 1024
    decoder = zlib.decompressobj(wbits)
    result = decoder.decompress(payload, limit + 1)
    if len(result) > limit or not decoder.eof:
        raise ValueError("invalid or oversized diagram metadata")
    return result


def _excalidraw_scene(data):
    if isinstance(data, dict) and "encoded" in data:
        if data.get("encoding") != "bstring":
            raise ValueError("unsupported scene encoding")
        raw = data["encoded"].encode("latin1")
        if data.get("compressed"):
            raw = _inflate_metadata(raw)
        data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
        raise ValueError("missing scene elements")
    return data


def decompress_drawio_data(data_str: str) -> str:
    """解压 Draw.io 中经过 base64 + raw deflate 压缩的 diagram 数据"""
    try:
        raw_b64 = base64.b64decode(data_str)
        decompressed = _inflate_metadata(raw_b64, -15)
        return urllib.parse.unquote(decompressed.decode("utf-8"))
    except Exception:
        return None


def extract_drawio_metadata(filepath: str) -> dict:
    """解析 Draw.io 拓扑图中的节点、连线与架构文本"""
    nodes = []
    edges = []
    diagram_name = ""
    
    try:
        if filepath.lower().endswith(".png"):
            with Image.open(filepath) as picture:
                content = picture.info.get("mxfile", "")
            if not content:
                raise ValueError("PNG has no embedded Draw.io source")
            content = urllib.parse.unquote(content)
        else:
            content = Path(filepath).read_text(encoding="utf-8")
        if filepath.lower().endswith(".svg"):
            content = urllib.parse.unquote(ET.fromstring(content).attrib.get("content", ""))
            if not content:
                raise ValueError("SVG has no embedded Draw.io source")

        # 解析 XML
        root = ET.fromstring(content)
        
        if root.tag not in {"mxfile", "mxGraphModel"}:
            raise ValueError("invalid Draw.io root")
        # 寻找 diagram 节点
        for diag in ([root] if root.tag == "mxGraphModel" else root.iter("diagram")):
            diagram_name = diag.attrib.get("name", "")
            raw_text = (diag.text or "").strip()
            
            # 如果是压缩流，先解压
            if raw_text and not diag.findall(".//*"):
                decompressed_xml = decompress_drawio_data(raw_text)
                if decompressed_xml:
                    diag = ET.fromstring(decompressed_xml)

            # 遍历所有 mxCell
            for cell in diag.iter("mxCell"):
                val = cell.attrib.get("value", "").strip()
                if not val:
                    continue
                # 清洗 HTML 标签（如 <b> <div> 等）
                clean_val = re.sub(r"<[^>]+>", " ", val).strip()
                clean_val = urllib.parse.unquote(clean_val).strip()
                if not clean_val:
                    continue

                if cell.attrib.get("edge") == "1":
                    edges.append(clean_val)
                elif cell.attrib.get("vertex") == "1":
                    if clean_val not in nodes:
                        nodes.append(clean_val)

        return {
            "format": "Draw.io",
            "diagram_name": diagram_name or Path(filepath).stem,
            "nodes": nodes,
            "edges": edges,
            "node_count": len(nodes)
        }
    except Exception as e:
        return {"format": "Draw.io", "error": str(e), "nodes": [], "edges": []}


def extract_excalidraw_metadata(filepath: str) -> dict:
    """解析 Excalidraw 白板文件中的文本要素与图形卡片"""
    texts = []
    element_types = []
    
    try:
        raw_content = "" if filepath.lower().endswith(".png") else Path(filepath).read_text(encoding="utf-8")

        # 如果是 Obsidian .excalidraw.md 文件
        if filepath.lower().endswith(".png"):
            with Image.open(filepath) as picture:
                payload = picture.info.get("application/vnd.excalidraw+json", "")
            data = _excalidraw_scene(json.loads(payload))
        elif filepath.lower().endswith(".svg"):
            match = re.search(r"<!--\s*payload-start\s*-->\s*(.*?)\s*<!--\s*payload-end\s*-->", raw_content, re.S)
            if not match:
                raise ValueError("SVG has no embedded Excalidraw source")
            raw = base64.b64decode(match.group(1))
            # Version 2 stores a byte string; legacy version 1 uses UTF-8.
            codec = "latin1" if re.search(r"payload-version:2", raw_content) else "utf-8"
            data = _excalidraw_scene(json.loads(raw.decode(codec)))
        elif filepath.endswith(".md"):
            # 1. 优先提取 # Text Elements 区域
            m_text = re.search(r"# Text Elements\n([\s\S]*?)(?=\n#|\n%%)", raw_content)
            if m_text:
                for line in m_text.group(1).splitlines():
                    cleaned = re.sub(r"\s*\^[a-zA-Z0-9_\-]+$", "", line).strip()
                    if cleaned and cleaned not in texts:
                        texts.append(cleaned)
            
            # 2. 提取底层 JSON
            m_json = re.search(r"```json\n([\s\S]*?)\n```", raw_content)
            if m_json:
                data = json.loads(m_json.group(1))
            else:
                if not texts:
                    raise ValueError("no readable Excalidraw text/JSON; compressed-json requires export to .excalidraw")
                data = {}
        else:
            data = _excalidraw_scene(json.loads(raw_content))

        if isinstance(data, dict) and "elements" in data:
            for el in data.get("elements", []):
                el_type = el.get("type", "")
                if el_type:
                    element_types.append(el_type)
                if el_type == "text":
                    t = (el.get("text") or el.get("originalText") or "").strip()
                    if t and t not in texts:
                        texts.append(t)

        return {
            "format": "Excalidraw",
            "texts": texts,
            "text_count": len(texts),
            "element_types": list(set(element_types))
        }
    except Exception as e:
        return {"format": "Excalidraw", "error": str(e), "texts": []}


def extract_svg_metadata(filepath: str) -> dict:
    """解析 SVG 矢量图文件"""
    try:
        tree = ET.parse(filepath)
        root = tree.getroot()
        ns = {'svg': 'http://www.w3.org/2000/svg'} if 'http://www.w3.org/2000/svg' in root.tag else {}
        
        width = root.attrib.get('width', '')
        height = root.attrib.get('height', '')
        
        text_elements = []
        for elem in root.iter():
            tag = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
            if tag in ('text', 'tspan'):
                txt = (elem.text or '').strip()
                if txt and txt not in text_elements:
                    text_elements.append(txt)
                    
        title = ""
        title_elem = root.find('.//svg:title', ns) if ns else root.find('.//title')
        if title_elem is not None and title_elem.text:
            title = title_elem.text.strip()

        return {
            "format": "SVG",
            "width": width,
            "height": height,
            "title": title,
            "extracted_text": text_elements
        }
    except Exception as e:
        return {"format": "SVG", "error": str(e), "extracted_text": []}


def extract_bitmap_metadata(filepath: str) -> dict:
    """读取 JPG / PNG / WEBP 位图元数据（缺 Pillow 时降级跳过，不阻断转译）"""
    if Image is None:
        return {"format": "BITMAP", "error": "Pillow 未安装，跳过位图元数据提取 (降级模式)"}
    try:
        with Image.open(filepath) as img:
            return {
                "format": img.format or "BITMAP",
                "width": img.width,
                "height": img.height,
                "mode": img.mode
            }
    except Exception as e:
        return {"format": "BITMAP", "error": str(e)}


# ==============================================================================
# 核心处理管线
# ==============================================================================
def process_diagram(
    filepath: str,
    output_path: str = None,
    attachments_dir: str = "99-Assets/10-Attachments",
    dump_payload: bool = False,
    dry_run: bool = False,
    archive: bool = False
):
    """
    单文件处理核心流程：
    1. 探测格式类别 (BITMAP / SVG / DRAWIO / EXCALIDRAW)
    2. 物理提取文本与架构节点
    3. 隔离归档原件至 99-Assets/10-Attachments/ 或 20-AI-Zone/20-Memory/21-Library/
    4. 生成纯文本结构化伴生卡片
    """
    vault_root = VAULT_ROOT
    path = Path(filepath).resolve()
    if not path.exists():
        print(f"[-] 文件不存在: {filepath}")
        return False

    in_dropzone = False
    try:
        rel = path.relative_to(vault_root)
        if len(rel.parts) > 0 and rel.parts[0] == "00-DropZone":
            in_dropzone = True
    except ValueError:
        pass

    canonical_ext, category = detect_file_type(str(path))
    if category == "UNKNOWN":
        print(f"[-] 不支持的文件格式: {path.name}")
        return False

    if category in ["DRAWIO", "EXCALIDRAW", "SVG"] and not output_path and attachments_dir == "99-Assets/10-Attachments":
        target_dir = vault_root / "20-AI-Zone/20-Memory/21-Library"
    else:
        target_dir = Path(attachments_dir) if Path(attachments_dir).is_absolute() else (vault_root / attachments_dir)

    slug = clean_filename(path.name[:-len(canonical_ext)] if path.name.lower().endswith(canonical_ext) else path.stem)
    dest_filename = f"{slug}{canonical_ext}"
    dest_path = target_dir / dest_filename

    # 避免重名覆盖
    counter = 1
    while dest_path.exists() and dest_path.resolve() != path.resolve():
        dest_filename = f"{slug}_{counter}{canonical_ext}"
        dest_path = target_dir / dest_filename
        counter += 1

    card_slug = slug if counter == 1 else f"{slug}_{counter - 1}"
    if not output_path:
        out_p = vault_root / "20-AI-Zone/20-Memory/21-Library" / f"{card_slug}.md"
    else:
        out_p = Path(output_path).resolve()
        if out_p.exists() and counter > 1:
            out_p = out_p.parent / f"{out_p.stem}_{counter - 1}.md"

    if dry_run:
        print(f"[*] [DryRun] 待处理文件: {path.name}")
        print(f"    - 文件类别: {category} ({canonical_ext})")
        print(f"    - 原件保存至: {display_path(dest_path, vault_root)}")
        print(f"    - 伴生卡片: {display_path(out_p, vault_root)}")
        return True

    if out_p.exists():
        print(f"[COLLISION] 目标卡片已存在: {out_p}", file=sys.stderr)
        return False

    target_dir.mkdir(parents=True, exist_ok=True)
    out_p.parent.mkdir(parents=True, exist_ok=True)

    # 物理提取元数据与文本要素
    if category == "DRAWIO":
        meta = extract_drawio_metadata(str(path))
    elif category == "EXCALIDRAW":
        meta = extract_excalidraw_metadata(str(path))
    elif category == "SVG":
        meta = extract_svg_metadata(str(path))
    else:
        meta = extract_bitmap_metadata(str(path))

    if meta.get("error"):
        print("[-] 工程图元数据无效或不支持，未生成卡片", file=sys.stderr)
        return False

    # 物理保存至目标区（仅 --archive 或 DropZone 原件才执行移动，其余保持只读复制）
    if path.resolve() != dest_path.resolve():
        if (archive or in_dropzone) and os.environ.get("GEWU_ATLAS_MANAGED_FMT") != "1":
            shutil.move(str(path), str(dest_path))
            print(f"[+] 图纸原件已安全归档至: {dest_path}")
        else:
            shutil.copy2(str(path), str(dest_path))
            print(f"[+] 图纸原件已安全复制至: {dest_path}")
    else:
        dest_filename = path.name

    today_str = datetime.now().strftime("%Y-%m-%d")
    title = meta.get("title") or meta.get("diagram_name") or slug.replace("_", " ")
    status_val = get_status_for_domain(vault_root, out_path=out_p)
    safe_title = fm_scalar(title)
    safe_category = fm_scalar(category)

    # 组装黄金四层 Frontmatter
    lines = [
        "---",
        "doc_type: library",
        f'title: "{safe_title}"',
        'source_type: image',
        'channel: "手记速记"',
        f'summary: "{safe_title} 视觉图像与工程拓扑伴生卡片，图纸类别: {safe_category}"',
        f'original_file: "{fm_scalar(dest_filename)}"',
        'processed: false',
        f'status: {status_val}',
        f'updated: {today_str}',
        f'diagram_category: "{safe_category}"',
        f'diagram_format: "{canonical_ext.replace(".", "").lower()}"',
        f'tags: [image, diagram, {category.lower()}]',
        "---",
        ""
    ]

    # 正文标题与原图内嵌展示
    lines.append(f"# {title}\n")
    lines.append(f"![[{dest_filename}]]\n")

    lines.append("> [!INFO] 图像与工程图纸资产元数据")
    lines.append(f"> - **文件原名**：`{path.name}`")
    lines.append(f"> - **物理原件**：`{dest_filename}`（存放在 `{display_path(dest_path.parent, vault_root)}/`）")
    lines.append(f"> - **图纸类别**：{category} ({canonical_ext})")
    lines.append(f"> - **入库时间**：{today_str}")
    lines.append("\n---\n")

    # 原生提取的文本要素展示（供参考与检索）
    extracted_items = meta.get("nodes") or meta.get("texts") or meta.get("extracted_text") or []
    if extracted_items:
        lines.append("## 🔤 图纸文本与节点要素提取\n")
        lines.append(f"> [!NOTE] 原生解析提取的 {len(extracted_items)} 个要素标签：")
        for item in extracted_items:
            lines.append(f"> - {item}")
        lines.append("\n---\n")

    # 预留 AI 视觉认知拆解区
    lines.append("## 🧠 AI 多模态视觉解析与结构化拆解 (零 Mermaid)\n")
    lines.append("> [!SUMMARY] 视觉要素与核心架构解读")
    lines.append("> *(等待 AI 视觉多模态引擎进行深度解析与要点提炼)*\n")

    out_p.write_text("\n".join(lines), encoding="utf-8")
    print(f"[+] 伴生 Markdown 卡片已生成: {out_p}")

    payload = {
        "title": title,
        "filename": dest_filename,
        "category": category,
        "format": canonical_ext,
        "abs_path": str(dest_path.resolve()),
        "markdown_path": str(out_p.resolve()),
        "meta": meta,
        "extracted_items": extracted_items
    }

    if dump_payload:
        payload_p = out_p.with_suffix(".payload.json")
        with open(payload_p, "w", encoding="utf-8") as jf:
            json.dump(payload, jf, ensure_ascii=False, indent=2)
        print(f"[+] AI 识别 Payload 已生成: {payload_p}")

    return payload


def run_dropzone_batch(dropzone_dir: str = None, dry_run: bool = False):
    """扫描 DropZone 待办箱中的所有图片与工程图纸（递归支持子文件夹，忽略 91-Temp/）"""
    vault_root = VAULT_ROOT
    dz = (vault_root / "00-DropZone") if not dropzone_dir else Path(dropzone_dir).resolve()
    if not dz.exists():
        print(f"[-] 目录不存在: {dz}")
        return

    found_files = []
    for f in sorted(dz.rglob("*")):
        rel_parts = f.relative_to(dz).parts
        if any(part == "91-Temp" or part.startswith(".") or part.startswith("~$") or part.startswith("_") for part in rel_parts):
            continue
        if f.is_file():
            c_ext, cat = detect_file_type(str(f))
            if cat != "UNKNOWN":
                found_files.append(f)

    if not found_files:
        print("[*] 00-DropZone/ 下未发现待处理的图像或工程图纸（已自动跳过 91-Temp/ 与 _ 草稿区）。")
        return

    print(f"[*] 发现 {len(found_files)} 个待处理图像/拓扑图纸，开始执行伴生卡片生成与附件隔离...")
    for f in found_files:
        if dry_run:
            process_diagram(str(f), dump_payload=False, dry_run=True)
        else:
            print(f"\n[*] 正在处理: {f.name}")
            process_diagram(str(f), dump_payload=True, dry_run=False)


# ==============================================================================
# 多模态图文切片、四轮视觉提问与会话回写引擎 (Context-Aware Multi-Round Vision Engine)
# ==============================================================================

def extract_image_context(md_path: str, image_name: str, char_window: int = 500) -> dict:
    """从上位 Markdown 文档中定位图片，抽取所在章节标题与前后文切片"""
    p = Path(md_path).resolve()
    if not p.exists():
        return {"error": f"Markdown 文件不存在: {md_path}", "image_name": image_name}
    
    content = p.read_text(encoding="utf-8")
    lines = content.splitlines()
    
    target_pattern = re.compile(rf'!\[\[{re.escape(image_name)}(?:\|[^\]]*)?\]\]|!\[[^\]]*\]\([^)]*{re.escape(image_name)}[^)]*\)')
    
    match_idx = -1
    for idx, line in enumerate(lines):
        if target_pattern.search(line):
            match_idx = idx
            break
            
    heading = "正文"
    preceding_text = ""
    following_text = ""
    
    if match_idx != -1:
        for i in range(match_idx - 1, -1, -1):
            l = lines[i].strip()
            if l.startswith("#"):
                heading = l
                break
        
        pre_lines = []
        char_count = 0
        for i in range(match_idx - 1, -1, -1):
            l = lines[i].strip()
            if not l or l.startswith("#") or l.startswith("---"):
                continue
            pre_lines.insert(0, l)
            char_count += len(l)
            if char_count >= char_window:
                break
        preceding_text = "\n".join(pre_lines)
        
        post_lines = []
        char_count = 0
        skip_callout = True
        for i in range(match_idx + 1, len(lines)):
            l = lines[i].strip()
            if skip_callout:
                if l.startswith(">") or not l:
                    continue
                else:
                    skip_callout = False
            if l.startswith("#") or l.startswith("---"):
                break
            post_lines.append(l)
            char_count += len(l)
            if char_count >= char_window:
                break
        following_text = "\n".join(post_lines)
        
    return {
        "image_name": image_name,
        "md_path": str(p),
        "heading": heading,
        "preceding_context": preceding_text,
        "following_context": following_text
    }


def generate_round_questions(round_idx: int, context_info: dict) -> list:
    """生成 4 轮认知漏斗问题清单 (每轮 5 个针对性问题，宏观到微观)"""
    heading = context_info.get("heading", "正文")
    if round_idx == 1:
        return [
            f"1. 结合所属章节「{heading}」及前后文论述，这张图在文档体系中扮演什么角色，其论证的核心命题或展示的系统目标是什么？",
            "2. 该图属于哪种图示类别？（系统/网络拓扑、业务交互时序、状态流转/生命周期、多维对比/评测矩阵、操作步骤、组织分工全景）",
            "3. 该图由哪几个主要物理/逻辑分区分层（宏观层级或泳道）构成？",
            "4. 该图的核心设计目标与适用边界条件是什么（如仅适用于某业务场景、某网络域、特定阶段）？",
            "5. 整体视觉重心地带在哪里，最醒目的核心组件或流程起点是什么？"
        ]
    elif round_idx == 2:
        return [
            "1. 请进行全量 OCR 扫描：提取图中出现的全部模块名称、组件实体、专有名词与缩写；",
            "2. 提取图中的所有接口名称、调用方法、通信协议、数据字段与版本编号；",
            "3. 提取图中的图例 (Legend)、坐标轴度量单位、数据样本极值与统计指标；",
            "4. 提取图中的特殊视觉符号（锁、盾牌、沙箱、颜色分区、不同虚实线型）及其工程含义；",
            "5. 检查并提取图示四周角落是否包含微小注释、引用标注或角标脚注。"
        ]
    elif round_idx == 3:
        return [
            "1. 梳理数据流（Data Flow）与控制面信令（Control Flow）的端到端交互流向，用文本箭头链（如 A -> B -> C）还原完整时序；",
            "2. 各模块之间的依赖关系、调用关系与包含关系是什么？",
            "3. 图中是否存在网络安全域（如 DMZ、内网、可信执行环境沙箱）？跨越边界时发生了哪些安全机制（验签、加解密、脱敏、拦截）？",
            "4. 【核心固定题·Mermaid直出】若判断该图为流程图、架构图、时序图、状态图等 Mermaid 支持的类型，请输出一段详细、准确、无遗漏且语法合规的 Mermaid 代码块（流程图优先使用 flowchart TD，时序图收敛短标签，节点名称用直角引号或无冲突短语）；若不适用 Mermaid 请明确注明「不适用 Mermaid」；",
            "5. 流程中是否存在条件分支、异常重试、回滚或降级兜底逻辑？"
        ]
    elif round_idx == 4:
        return [
            "1. 图示中的哪些具体模块、数据或时序直接验证并支撑了前文的论点？",
            "2. 图中揭示了哪些正文未明说、但对技术实现至关重要的隐藏假设与细节？",
            "3. 提炼 3~4 行返填上位文档的高密度 Callout 简要注释（包含核心意图、关键要素、逻辑机制）；",
            "4. 整理独立伴生卡的核心要素与配置对照矩阵（Markdown 表格，包含模块/实体、接口/协议、作用说明、依赖边界）；",
            "5. 审查确认 Mermaid 图表或拓扑大纲无遗漏，核对是否符合格物志 Markdown 规范。"
        ]
    return []


def init_interview_session(image_path: str, md_path: str = None, session_file: str = None) -> dict:
    """初始化多轮视觉提问会话（全量图片默认遵循独立伴生卡统一流水线）"""
    img_p = Path(image_path).resolve()
    image_name = img_p.name
    
    if md_path:
        context = extract_image_context(md_path, image_name)
    else:
        context = {"image_name": image_name, "md_path": "", "heading": "独立图纸", "preceding_context": "", "following_context": ""}
        
    s_path = Path(session_file).resolve() if session_file else img_p.with_suffix(".session.json")
    
    session = {
        "version": "1.2",
        "created_at": datetime.now().isoformat(),
        "image_path": str(img_p),
        "image_name": image_name,
        "md_path": str(Path(md_path).resolve()) if md_path else "",
        "context": context,
        "current_round": 1,
        "rounds": {
            "1": {"questions": generate_round_questions(1, context), "answer": ""},
            "2": {"questions": generate_round_questions(2, context), "answer": ""},
            "3": {"questions": generate_round_questions(3, context), "answer": ""},
            "4": {"questions": generate_round_questions(4, context), "answer": ""}
        },
        "status": "in_progress",
        "decision": "companion",  # 默认统一生成独立伴生卡
        "final_callout": "",
        "companion_card_path": ""
    }
    
    s_path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[+] 视觉交互 Session 已初始化: {s_path}")
    return session


def record_round_answer(session_file: str, round_idx: int, answer_text: str) -> dict:
    """记录某轮次的 AI 识别回答，自动步进状态机"""
    s_path = Path(session_file).resolve()
    if not s_path.exists():
        raise FileNotFoundError(f"Session 文件不存在: {session_file}")
        
    session = json.loads(s_path.read_text(encoding="utf-8"))
    r_key = str(round_idx)
    if r_key in session["rounds"]:
        session["rounds"][r_key]["answer"] = answer_text.strip()
        
    if round_idx < 4:
        session["current_round"] = round_idx + 1
    else:
        session["status"] = "completed"
        
    s_path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[+] 第 {round_idx} 轮答案已记录，当前轮次: {session['current_round']}, 状态: {session['status']}")
    return session


def compile_session_annotation(session_file: str, decision: str = "companion", custom_callout: str = None, companion_slug: str = None) -> dict:
    """根据 4 轮收集的答案编译最终 Markdown 注释与独立伴生卡（全量图片统一伴生卡流水线）"""
    s_path = Path(session_file).resolve()
    session = json.loads(s_path.read_text(encoding="utf-8"))
    
    r1 = session["rounds"].get("1", {}).get("answer", "")
    r2 = session["rounds"].get("2", {}).get("answer", "")
    r3 = session["rounds"].get("3", {}).get("answer", "")
    r4 = session["rounds"].get("4", {}).get("answer", "")
    
    session["decision"] = decision
    
    # 提取回答中的 Mermaid 代码块（如有）
    mermaid_blocks = []
    for r_text in [r3, r4, r1, r2]:
        m_matches = re.findall(r"```mermaid\s*([\s\S]*?)```", r_text)
        for m_code in m_matches:
            if m_code.strip() and m_code.strip() not in mermaid_blocks:
                mermaid_blocks.append(m_code.strip())
                
    if custom_callout:
        final_callout = custom_callout.strip()
    else:
        summary_line = r1.splitlines()[0] if r1 else "图示架构核心机理与要素流转"
        summary_line = re.sub(r'^[0-9一二三四\.\s、\-:]+', '', summary_line).strip()
        
        elements_line = r2.splitlines()[0] if r2 else "包含关键功能模块、通信协议与数据字段"
        elements_line = re.sub(r'^[0-9一二三四\.\s、\-:]+', '', elements_line).strip()
        
        flow_line = r3.splitlines()[0] if r3 else "按端到端时序流转与边界安全控制执行"
        flow_line = re.sub(r'^[0-9一二三四\.\s、\-:]+', '', flow_line).strip()
        
        lines = [
            "> [!NOTE] 图示解析与要素拆解",
            f"> - **核心意图**：{summary_line[:100]}",
            f"> - **图内要素与指标**：{elements_line[:120]}",
            f"> - **逻辑机制 / 数据矩阵**：{flow_line[:120]}"
        ]
        final_callout = "\n".join(lines)
        
    session["final_callout"] = final_callout
    
    # 全量统一生成伴生卡（或当显式指定 companion 时）
    if decision == "companion":
        img_name = session["image_name"]
        parent_md = session.get("md_path", "")
        parent_title = Path(parent_md).stem if parent_md else "上位文档"
        
        c_slug = companion_slug or f"{parent_title}_{Path(img_name).stem}"
        companion_filename = f"{c_slug}.md"
        
        vault_root = VAULT_ROOT
        target_dir = vault_root / "20-AI-Zone/20-Memory/21-Library"
        if parent_md:
            try:
                rel_parts = Path(parent_md).resolve().relative_to(vault_root.resolve()).parts
                if len(rel_parts) > 1 and rel_parts[0] == "30-Projects":
                    target_dir = vault_root / "30-Projects" / rel_parts[1] / "20-Memory" / "21-Library"
            except ValueError:
                pass
        companion_path = target_dir / companion_filename
        status_val = get_status_for_domain(vault_root, out_path=companion_path)
        
        comp_summary = f"{parent_title}插图{Path(img_name).stem}架构深度拆解"[:50]
        
        safe_c_slug = fm_scalar(c_slug)
        safe_c_title = fm_scalar(f"{c_slug} 视觉架构深度拆解")
        safe_comp_summary = fm_scalar(comp_summary)
        safe_img_name = fm_scalar(img_name)
        safe_parent_title = fm_scalar(parent_title)
        companion_lines = [
            "---",
            "doc_type: library",
            f'title: "{safe_c_title}"',
            "source_type: image",
            'channel: "图示伴生"',
            f'summary: "{safe_comp_summary}"',
            f'original_file: "{safe_img_name}"',
            f'source_file: "[[{safe_parent_title}]]"',
            "processed: false",
            f"status: {status_val}",
            f"updated: {datetime.now().strftime('%Y-%m-%d')}",
            "tags: [image, diagram, companion]",
            "---",
            "",
            f"# {c_slug} 视觉架构深度拆解\n",
            f"![[{img_name}]]\n",
            f"> [!SUMMARY] 设计思想与核心论点",
            f"> {r1 if r1 else '系统核心架构与全局流转蓝图。'}\n"
        ]
        
        # 优先输出逆向生成的 Mermaid 结构化图表
        if mermaid_blocks:
            companion_lines.append("## 1. 结构化拓扑与时序流转 (Mermaid 逆向还原)\n")
            for idx, mb in enumerate(mermaid_blocks, 1):
                companion_lines.append("```mermaid")
                companion_lines.append(mb)
                companion_lines.append("```\n")
            if r3:
                companion_lines.append(f"> [!NOTE] 逻辑流转说明\n> {r3}\n")
        else:
            companion_lines.append("## 1. 模块层级与流转机制大纲\n")
            companion_lines.append(f"{r3 if r3 else '- 端到端时序流转与安全边界机制。'}\n")
            
        companion_lines.extend([
            "## 2. 核心要素与配置对照矩阵\n",
            "| 模块 / 实体 | 接口 / 协议 | 作用说明 | 依赖边界 |",
            "| :--- | :--- | :--- | :--- |",
            f"| 核心节点 | API / TLS | 业务逻辑与策略执行 | 受信域 |\n",
            "## 3. 全量文字与指标索引 (OCR)\n",
            f"> [!NOTE] 全量提取的文字要素与指标标签：",
            f"> {r2 if r2 else '全量专有名词与度量指标。'}\n"
        ])
        
        if companion_path.exists():
            print(f"[COLLISION] 伴生卡已存在: {companion_path}", file=sys.stderr)
            return None
        companion_path.parent.mkdir(parents=True, exist_ok=True)
        companion_path.write_text("\n".join(companion_lines), encoding="utf-8")
        session["companion_card_path"] = str(companion_path)
        print(f"[+] 独立伴生卡已生成: {companion_path}")
        
        session["final_callout"] += f"\n> - **深度拆解**：完整架构拓扑与全量要素矩阵见独立卡片 [[{c_slug}|查看图示完整拆解与结构化矩阵]]"
        
    s_path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    return session


def apply_session_to_markdown(session_file: str) -> bool:
    """将编译后的结构化注释精准回写替换父 Markdown 文档中的占位符"""
    s_path = Path(session_file).resolve()
    session = json.loads(s_path.read_text(encoding="utf-8"))
    
    md_path = session.get("md_path")
    if not md_path or not Path(md_path).exists():
        print(f"[-] 无法回写：父 Markdown 文档不存在: {md_path}")
        return False
        
    p = Path(md_path)
    content = p.read_text(encoding="utf-8")
    img_name = session["image_name"]
    callout = session.get("final_callout")
    
    if not callout:
        print("[-] 无法回写：未编译 final_callout，请先执行 compile")
        return False
        
    pattern = re.compile(
        rf'(!\[\[{re.escape(img_name)}(?:\|[^\]]*)?\]\]\s*\n)(?:> \[!NOTE\][^\n]*\n(?:> [^\n]*\n*)*)?',
        re.MULTILINE
    )
    
    if not pattern.search(content):
        print(f"[-] 在 {md_path} 中未找到图片引用 ![[{img_name}]]")
        return False
        
    new_content = pattern.sub(f"\\g<1>{callout}\n\n", content)
    p.write_text(new_content, encoding="utf-8")
    
    session["status"] = "applied"
    s_path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[+] 已成功回写注释至 Markdown: {p}")
    return True


def main():
    parser = argparse.ArgumentParser(description="智能图片、Draw.io 拓扑、Excalidraw 白板与多轮视觉提问回写引擎")
    parser.add_argument("input", nargs="?", help="文件路径 (.png, .jpg, .svg, .drawio, .excalidraw) (与 -i/--input 等效)")
    parser.add_argument("-i", "--input-file", dest="alt_input", help="输入文件路径")
    parser.add_argument("-o", "--output", help="输出的伴生 Markdown 卡片路径 (默认: 20-AI-Zone/20-Memory/21-Library/{slug}.md)")
    parser.add_argument("--attachments", default="99-Assets/10-Attachments", help="多媒体附件存储目录 (默认: 99-Assets/10-Attachments)")
    parser.add_argument("--dump-payload", action="store_true", help="输出供 AI 消费的元数据 Payload")
    parser.add_argument("--dropzone", action="store_true", help="批量扫描 00-DropZone/ 待办箱")
    parser.add_argument("--dry-run", action="store_true", help="演练模式：仅打印计划操作与路径，不修改文件")
    parser.add_argument("--archive", action="store_true", help="将原件移动至归档目录（默认复制）")

    # 多轮视觉交互引擎参数
    parser.add_argument("--interview", action="store_true", help="初始化或推进多轮视觉交互提问流程")
    parser.add_argument("--context-md", help="指定图片所属的上位父 Markdown 文档路径，用于抽取章节与前后切片")
    parser.add_argument("--session", help="视觉交互 Session JSON 文件路径 (默认: {image}.session.json)")
    parser.add_argument("--round", type=int, choices=[1, 2, 3, 4], help="执行或回答的交互轮次 (1-4)")
    parser.add_argument("--answer", help="提交当前轮次的 AI 识别回答文字")
    parser.add_argument("--answer-file", help="从文件读取当前轮次的回答文字")
    parser.add_argument("--compile", action="store_true", help="根据 4 轮收集的答案编译最终 Markdown 注释")
    parser.add_argument("--decision", choices=["inline", "companion"], default="companion", help="交付决策: companion (统一独立伴生卡，默认) 或 inline (仅就地注释)")
    parser.add_argument("--apply", action="store_true", help="将编译后的注释回写替换父 Markdown 中的占位符")

    args = parser.parse_args()

    # 处理多轮交互分支
    if args.interview:
        target_img = args.alt_input if args.alt_input else args.input
        if not target_img:
            print("[-] --interview 必须指定图片路径")
            sys.exit(1)
        sess = init_interview_session(target_img, md_path=args.context_md, session_file=args.session)
        cur_r = args.round or sess["current_round"]
        print(f"\n=== 第 {cur_r} 轮视觉提问清单 (所属章节: {sess['context']['heading']}) ===")
        for q in sess["rounds"][str(cur_r)]["questions"]:
            print(f"  {q}")
        sys.exit(0)

    if args.answer or args.answer_file:
        if not args.session:
            print("[-] 提交答案必须指定 --session 文件")
            sys.exit(1)
        if not args.round:
            print("[-] 提交答案必须指定 --round (1-4)")
            sys.exit(1)
        ans = args.answer
        if args.answer_file:
            ans = Path(args.answer_file).read_text(encoding="utf-8")
        sess = record_round_answer(args.session, args.round, ans)
        if sess["status"] != "completed":
            next_r = sess["current_round"]
            print(f"\n=== 下一步: 请回答第 {next_r} 轮问题 ===")
            for q in sess["rounds"][str(next_r)]["questions"]:
                print(f"  {q}")
        else:
            print("\n[+] 4 轮视觉交互已全部完成！可运行 --compile 编译最终交付成果。")
        sys.exit(0)

    if args.compile:
        if not args.session:
            print("[-] 编译必须指定 --session 文件")
            sys.exit(1)
        sess = compile_session_annotation(args.session, decision=args.decision)
        print("\n=== 编译产出的结构化注释 ===")
        print(sess["final_callout"])
        sys.exit(0)

    if args.apply:
        if not args.session:
            print("[-] 回写必须指定 --session 文件")
            sys.exit(1)
        apply_session_to_markdown(args.session)
        sys.exit(0)

    # 兼容历史行为
    if args.dropzone:
        run_dropzone_batch(dry_run=args.dry_run)
    else:
        target_in = args.alt_input if args.alt_input else args.input
        if not target_in:
            parser.print_help()
            sys.exit(1)
        ok = process_diagram(
            target_in,
            output_path=args.output,
            attachments_dir=args.attachments,
            dump_payload=args.dump_payload,
            dry_run=args.dry_run,
            archive=args.archive
        )
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
