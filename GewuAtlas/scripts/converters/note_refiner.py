#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ==============================================================================
# note_refiner.py - Markdown 与纯文本六路自适应升阶引擎 (Note Refiner)
#
# 职责：
# 1. 六路自适应分流：
#    - 路径 0: 完备 Library 直通 (doc_type: library) -> 21-Library/
#    - 路径 A: 完备 Atomics 直通 (doc_type: atomic) -> 22-Atomics/
#    - 路径 M: 完备 MOC 直通 (doc_type: moc) -> 23-MOC/
#    - 路径 J: 周期日记手记识别 (Daily Journal) -> 21-Library/ (source_type: journal)
#    - 路径 T: 访谈录音速记识别 (Audio Transcript) -> 21-Library/ (source_type: transcript)
#    - 路径 F: 闪念备忘与长文分流 (Fleeting Memo & Article) -> 21-Library/ (source_type: document)
# 2. 纯文本 TXT 蜕变：结构化排版美化为规范 Markdown 后流转入库；
# 3. 分形域支持：支持 AI-Zone 与 30-Projects/<id> 同构域模式。
# ==============================================================================

import argparse
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

_CONVERTERS_DIR = Path(__file__).resolve().parent
if str(_CONVERTERS_DIR) not in sys.path:
    sys.path.insert(0, str(_CONVERTERS_DIR))

from core.common import get_vault_root, extract_frontmatter, today, trash_file, fm_scalar
from core.project import get_project_id_for_dir, get_status_for_domain, resolve_project_dir
from archive_month import monthly_archive_dir

VALID_ATOMIC_TYPES = {"OBJ", "SUB", "SYN", "HYP"}
VALID_ATOMIC_STATUS = {"pending", "active", "deprecated"}


def serialize_frontmatter(meta: dict) -> str:
    """序列化字典为标准 YAML Frontmatter"""
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, list):
            items = []
            for item in v:
                s_item = str(item)
                if any(c in s_item for c in ':,[]{}#"\'\\') or not s_item:
                    items.append(f'"{fm_scalar(s_item)}"')
                else:
                    items.append(s_item)
            lines.append(f"{k}: [{', '.join(items)}]")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            lines.append(f"{k}: {v}")
        elif v is None:
            lines.append(f"{k}: \"\"")
        else:
            s = str(v)
            if any(c in s for c in ':\n"[]{}#\\') or s.strip() != s or not s:
                lines.append(f'{k}: "{fm_scalar(s)}"')
            else:
                lines.append(f"{k}: {s}")
    lines.append("---")
    return "\n".join(lines)


def domain_info(root: Path, project_id: str = None):
    """返回 (base_dir, is_ai_zone)。课题目录必须已存在，严禁静默创建无 Manifest 幽灵目录。"""
    if project_id and str(project_id).strip().lower() not in ("ai-zone", "aizone", "20-ai-zone"):
        resolved = resolve_project_dir(root, str(project_id).strip())
        if resolved is None:
            raise FileNotFoundError(f"目标课题目录不存在，禁止静默创建无 Manifest 幽灵目录: {project_id}")
        return resolved, False
    return root / "20-AI-Zone", True


def detect_encoding_and_read(filepath: Path, encoding=None) -> str:
    """Explicit encoding wins; ambiguous automatic decoding is reported."""
    raw = filepath.read_bytes()
    if encoding:
        return raw.decode(encoding)
    for enc in ("utf-8-sig", "utf-16"):
        if enc == "utf-16" and not raw.startswith((b'\xff\xfe', b'\xfe\xff')):
            continue
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    choices = []
    for enc in ("gb18030", "cp1252", "latin1"):
        try:
            choices.append((enc, raw.decode(enc)))
        except UnicodeDecodeError:
            pass
    if len({value for _, value in choices}) > 1:
        print("[WARN] 文本编码不唯一，默认兼容 " + choices[0][0] + "；可用 fmt --encoding 指定", file=sys.stderr)
    return choices[0][1]


def is_transcript_content(filename: str, content: str) -> bool:
    """根据文件名与正文特征判断是否为访谈/录音/语音速记"""
    keywords = ["速记", "转写", "录音", "通话", "访谈", "对话", "口述", "transcript", "interview", "播客"]
    fn_lower = filename.lower()
    for kw in keywords:
        if kw in fn_lower:
            return True
    head_text = content[:400].lower()
    dialogue_indicators = ["主持人：", "主持人:", "嘉宾：", "嘉宾:", "发言人1", "发言人2", "通话记录", "语音转写", "录音整理"]
    for ind in dialogue_indicators:
        if ind in head_text:
            return True
    return False


def is_fleeting_memo_content(filename: str, content: str) -> bool:
    """根据特征判断是否为即时灵感随手记或会议备忘"""
    keywords = ["随笔", "随手记", "灵感", "备忘", "草稿", "memo", "jotting", "想法"]
    fn_lower = filename.lower()
    for kw in keywords:
        if kw in fn_lower:
            return True
    head_text = content[:300].lower()
    memo_indicators = ["今天下班", "聊了很久", "会议随手记", "随手记一下", "个人思考", "构思：", "构思:"]
    for ind in memo_indicators:
        if ind in head_text:
            return True
    return False


def is_daily_journal_content(filename: str, content: str, meta: dict) -> bool:
    """判断是否为周期日记或手记文档 (如 2026-10-31.md)"""
    if meta.get("doc_type") == "journal" or meta.get("source_type") == "journal":
        return True
    name_no_ext = Path(filename).stem
    if re.match(r'^\d{4}-\d{2}-\d{2}$', name_no_ext):
        return True
    if re.match(r'^\d{4}年\d{1,2}月\d{1,2}日$', name_no_ext):
        return True
    head_lines = content.splitlines()[:5]
    for line in head_lines:
        s = line.strip().lstrip("#").strip()
        if re.match(r'^\d{4}-\d{2}-\d{2}$', s) or re.match(r'^\d{4}年\d{1,2}月\d{1,2}日$', s):
            return True
    return False


def detect_channel(filename: str, content: str) -> str:
    """智能推断素材采集来源渠道"""
    combined = (filename + "\n" + content[:1000]).lower()
    if "微信" in combined or "mp.weixin.qq.com" in combined or "公众号" in combined:
        return "微信公众号"
    if "知乎" in combined or "zhihu.com" in combined:
        return "知乎"
    if "新浪" in combined or "sina.com.cn" in combined:
        return "新浪"
    if "今日头条" in combined or "toutiao.com" in combined:
        return "今日头条"
    if "arxiv" in combined or "arxiv.org" in combined:
        return "arXiv"
    if "gemini" in combined:
        return "gemini"
    if "豆包" in combined:
        return "豆包"
    if "chatgpt" in combined or "openai" in combined:
        return "chatgpt"
    if "claude" in combined or "anthropic" in combined:
        return "claude"
    if "kimi" in combined:
        return "kimi"
    if is_transcript_content(filename, content):
        return "播客访谈"
    if is_fleeting_memo_content(filename, content):
        return "手记速记"
    return "手记速记"


def beautify_txt_to_markdown(raw_text: str, title: str) -> str:
    """将粗糙的纯文本排版美化为规范 Markdown"""
    lines = [l.strip() for l in raw_text.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()

    md_lines = []
    if not (lines and lines[0].startswith("# ")):
        md_lines.append(f"# {title}\n")

    for line in lines:
        if not line:
            md_lines.append("")
            continue
        if re.match(r'^#{1,6}\s', line):
            md_lines.append(line)
            continue
        num_match = re.match(r'^(\d+)[.、)）]\s*(.+)', line)
        if num_match:
            md_lines.append(f"{num_match.group(1)}. {num_match.group(2)}")
            continue
        bullet_match = re.match(r'^[-*•]\s*(.+)', line)
        if bullet_match:
            md_lines.append(f"- {bullet_match.group(1)}")
            continue
        md_lines.append(line)

    return "\n".join(md_lines)


def validate_library_yaml_completeness(meta: dict) -> tuple[bool, list[str]]:
    required = ["doc_type", "title", "summary", "status", "tags", "updated", "source_type", "channel"]
    missing = [f for f in required if f not in meta or meta[f] is None or str(meta[f]).strip() == ""]
    if meta.get("doc_type") != "library":
        missing.append("doc_type!=library")
    return (len(missing) == 0, missing)


def validate_atomic_yaml_completeness(meta: dict) -> tuple[bool, list[str]]:
    required = ["doc_type", "title", "summary", "status", "tags", "updated", "type", "source"]
    missing = [f for f in required if f not in meta or meta[f] is None or str(meta[f]).strip() == ""]
    dt = meta.get("doc_type")
    if dt not in ("atomic", "memory"):
        missing.append("doc_type!=atomic")
    if meta.get("type") not in VALID_ATOMIC_TYPES:
        missing.append(f"invalid_type({meta.get('type')})")
    return (len(missing) == 0, missing)


# ==============================================================================
# 六路自适应分流执行器
# ==============================================================================

def process_fast_track_library(filepath: Path, meta: dict, body: str, base_dir: Path, dry_run: bool = False, encoding=None) -> dict:
    """路径 0：完备 Library 直通"""
    dest_dir = base_dir / "20-Memory" / "21-Library"
    dest_path = dest_dir / filepath.name
    if dest_path.exists() and dest_path.resolve() != filepath.resolve():
        cnt = 1
        while dest_path.exists() and dest_path.resolve() != filepath.resolve():
            dest_path = dest_dir / f"{filepath.stem}_{cnt}.md"
            cnt += 1
    print(f"  [★ 命中完善Library直通车] 完备合规元数据，直接归档至: {dest_path.name}")
    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)
        if filepath.resolve() != dest_path.resolve():
            shutil.move(str(filepath), str(dest_path))
    return {"type": "fast_track_library", "target": str(dest_path)}


def process_fast_track_atomic(filepath: Path, meta: dict, body: str, base_dir: Path, dry_run: bool = False, encoding=None) -> dict:
    """路径 A：完备 Atomics 直通"""
    dest_dir = base_dir / "20-Memory" / "22-Atomics"
    clean_title = meta.get("title") or filepath.stem
    safe_name = re.sub(r'[/\\:*?"<>|]', '-', str(clean_title)).strip() or filepath.stem
    dest_path = dest_dir / f"{safe_name}.md"
    if dest_path.exists() and dest_path.resolve() != filepath.resolve():
        cnt = 1
        while dest_path.exists() and dest_path.resolve() != filepath.resolve():
            dest_path = dest_dir / f"{safe_name}_{cnt}.md"
            cnt += 1

    # 标准化 doc_type 为 atomic
    updated_meta = dict(meta)
    updated_meta["doc_type"] = "atomic"
    if "updated" not in updated_meta:
        updated_meta["updated"] = today()
    if base_dir.parent.name == "30-Projects" and not updated_meta.get("project_id"):
        updated_meta["project_id"] = get_project_id_for_dir(base_dir)

    print(f"  [★ 命中完善Atomics直通车] 完备原子卡，直接移入: {dest_path.name}")
    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(serialize_frontmatter(updated_meta) + "\n\n" + body.lstrip(), encoding="utf-8")
        if filepath.resolve() != dest_path.resolve():
            trash_file(filepath)
    return {"type": "fast_track_atomic", "target": str(dest_path)}


def process_fast_track_moc(filepath: Path, meta: dict, body: str, base_dir: Path, dry_run: bool = False, encoding=None) -> dict:
    """路径 M：完备 MOC 直通"""
    dest_dir = base_dir / "20-Memory" / "23-MOC"
    dest_path = dest_dir / filepath.name
    if dest_path.exists() and dest_path.resolve() != filepath.resolve():
        cnt = 1
        while dest_path.exists() and dest_path.resolve() != filepath.resolve():
            dest_path = dest_dir / f"{filepath.stem}_{cnt}.md"
            cnt += 1
    print(f"  [★ 命中完善MOC直通车] 完备内容地图，直接移入: {dest_path.name}")
    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)
        updated_meta = dict(meta)
        if base_dir.parent.name == "30-Projects" and not updated_meta.get("project_id"):
            updated_meta["project_id"] = get_project_id_for_dir(base_dir)
            dest_path.write_text(serialize_frontmatter(updated_meta) + "\n\n" + body.lstrip(), encoding="utf-8")
            if filepath.resolve() != dest_path.resolve():
                trash_file(filepath)
        elif filepath.resolve() != dest_path.resolve():
            shutil.move(str(filepath), str(dest_path))
    return {"type": "fast_track_moc", "target": str(dest_path)}


def process_daily_journal(filepath: Path, meta: dict, body: str, base_dir: Path, dry_run: bool = False, encoding=None) -> dict:
    """路径 J：周期日记手记识别"""
    dest_dir = base_dir / "20-Memory" / "21-Library"
    dest_path = dest_dir / filepath.name
    if dest_path.exists():
        cnt = 1
        while dest_path.exists():
            dest_path = dest_dir / f"{filepath.stem}_{cnt}.md"
            cnt += 1

    status_val = get_status_for_domain(get_vault_root(), out_path=dest_path)
    updated_meta = dict(meta)
    updated_meta["doc_type"] = "library"
    updated_meta["source_type"] = "journal"
    updated_meta["channel"] = "手记速记"
    updated_meta["title"] = meta.get("title") or filepath.stem
    updated_meta["status"] = status_val
    updated_meta["processed"] = False
    updated_meta["updated"] = today()
    if base_dir.parent.name == "30-Projects" and not updated_meta.get("project_id"):
        updated_meta["project_id"] = get_project_id_for_dir(base_dir)
    if "tags" not in updated_meta:
        updated_meta["tags"] = ["Journal", "手记速记"]

    first_line = next((l.strip() for l in body.splitlines() if l.strip() and not l.startswith("#")), filepath.stem)
    updated_meta["summary"] = meta.get("summary") or first_line[:50]

    print(f"  [✓ 命中周期日记手记] 规范化归档至: {dest_path.name}")
    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(serialize_frontmatter(updated_meta) + "\n\n" + body.lstrip(), encoding="utf-8")
        trash_file(filepath, get_vault_root())
    return {"type": "daily_journal", "target": str(dest_path)}


def process_md_file(filepath: Path, base_dir: Path, dry_run: bool = False, encoding=None) -> dict:
    """处理 Markdown 笔记文档 (.md)"""
    content = detect_encoding_and_read(filepath, encoding)
    meta, body, has_fm = extract_frontmatter(content)
    if meta.get("doc_type") == "journal":
        print("[BLOCKED] journal records require explicit selection before library ingestion", file=sys.stderr)
        return {}
    
    in_dropzone = "00-DropZone" in filepath.resolve().parts

    # 1. 完善 Library 直通
    is_lib_ok, _ = validate_library_yaml_completeness(meta)
    if is_lib_ok:
        return process_fast_track_library(filepath, meta, body, base_dir, dry_run=dry_run)

    # 2. 完善 Atomics 直通 (DropZone 输入严禁直通正式 Atomics 舱，必须经由萃取流转)
    if not in_dropzone and (meta.get("doc_type") in ("atomic", "memory") or (meta.get("type") in VALID_ATOMIC_TYPES and "source" in meta)):
        is_atom_ok, _ = validate_atomic_yaml_completeness(meta)
        if is_atom_ok:
            return process_fast_track_atomic(filepath, meta, body, base_dir, dry_run=dry_run)

    # 3. 完善 MOC 直通 (DropZone 输入严禁直通正式 MOC 舱)
    if not in_dropzone and (meta.get("doc_type") == "moc" or filepath.name.startswith("MOC-")):
        return process_fast_track_moc(filepath, meta, body, base_dir, dry_run=dry_run)

    # 4. 周期日记手记
    if is_daily_journal_content(filepath.name, content, meta):
        return process_daily_journal(filepath, meta, body, base_dir, dry_run=dry_run)

    # 5. 基础素材分流归档（录音速记 vs 备忘速记 vs 普通文章）
    is_transcript = is_transcript_content(filepath.name, content)
    is_memo = is_fleeting_memo_content(filepath.name, content)

    if is_transcript:
        source_type = "transcript"
        channel = meta.get("channel") or "播客访谈"
    elif is_memo:
        source_type = "document"
        channel = meta.get("channel") or "手记速记"
    else:
        source_type = meta.get("source_type") or "document"
        channel = meta.get("channel") or detect_channel(filepath.name, content)

    dest_dir = base_dir / "20-Memory" / "21-Library"
    dest_path = dest_dir / filepath.name
    if dest_path.exists():
        cnt = 1
        while dest_path.exists():
            dest_path = dest_dir / f"{filepath.stem}_{cnt}.md"
            cnt += 1

    clean_title = meta.get("title") or filepath.stem
    first_line = next((l.strip() for l in body.splitlines() if l.strip() and not l.startswith("#")), clean_title)
    summary_text = meta.get("summary") or first_line[:50]
    status_val = get_status_for_domain(get_vault_root(), out_path=dest_path)

    updated_meta = dict(meta)
    updated_meta["doc_type"] = "library"
    updated_meta["title"] = clean_title
    updated_meta["source_type"] = source_type
    updated_meta["channel"] = channel
    updated_meta["summary"] = summary_text
    updated_meta["processed"] = False
    updated_meta["status"] = status_val
    updated_meta["updated"] = today()
    if base_dir.parent.name == "30-Projects" and not updated_meta.get("project_id"):
        updated_meta["project_id"] = get_project_id_for_dir(base_dir)
    if "tags" not in updated_meta:
        updated_meta["tags"] = [source_type, channel.lower()]
    if "extracted_to" not in updated_meta:
        updated_meta["extracted_to"] = []

    final_content = serialize_frontmatter(updated_meta) + "\n\n" + body.lstrip()
    print(f"  [✓ 基础素材清洗落库] 识别为 {source_type} ({channel}) -> {dest_path.name}")
    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(final_content, encoding="utf-8")
        trash_file(filepath, get_vault_root())
    return {"type": "library_raw", "target": str(dest_path)}


def process_txt_file(filepath: Path, base_dir: Path, archive_dir: Path, dry_run: bool = False, encoding=None) -> dict:
    """处理纯文本文件 (.txt, .rtf)：美化为 Markdown + 原件冷归档 + 注入黄金四层元数据落库"""
    if filepath.suffix.lower() == ".rtf":
        from striprtf.striprtf import rtf_to_text
        raw = filepath.read_bytes().decode(encoding or "latin-1")
        raw_text = rtf_to_text(raw, encoding=encoding or "cp1252", errors="strict")
    else:
        raw_text = detect_encoding_and_read(filepath, encoding)
    title = filepath.stem
    md_body = beautify_txt_to_markdown(raw_text, title)

    is_transcript = is_transcript_content(filepath.name, raw_text)
    is_memo = is_fleeting_memo_content(filepath.name, raw_text)

    if is_transcript:
        source_type = "transcript"
        channel = "播客访谈"
    elif is_memo:
        source_type = "document"
        channel = "手记速记"
    else:
        source_type = "document"
        channel = detect_channel(filepath.name, raw_text)

    first_line = next((l.strip() for l in md_body.splitlines() if l.strip() and not l.startswith("#")), title)
    summary_text = first_line[:50]

    dest_dir = base_dir / "20-Memory" / "21-Library"
    dest_path = dest_dir / f"{filepath.stem}.md"
    if dest_path.exists():
        cnt = 1
        while dest_path.exists():
            dest_path = dest_dir / f"{filepath.stem}_{cnt}.md"
            cnt += 1

    status_val = get_status_for_domain(get_vault_root(), out_path=dest_path)
    meta = {
        "doc_type": "library",
        "title": title,
        "summary": summary_text,
        "status": status_val,
        "tags": [source_type, channel.lower()],
        "updated": today(),
        "source_type": source_type,
        "channel": channel,
        "processed": False,
        "original_file": filepath.name,
        "source_file": "",
        "extracted_to": []
    }
    if base_dir.parent.name == "30-Projects":
        meta["project_id"] = get_project_id_for_dir(base_dir)

    print(f"  [★ TXT文本蜕变] 纯文本结构化美化 -> {dest_path.name} (归档原件至 98-Archives)")
    if not dry_run:
        archive_dir.mkdir(parents=True, exist_ok=True)
        dst_orig = archive_dir / filepath.name
        n = 1
        while dst_orig.exists() and dst_orig.resolve() != filepath.resolve():
            dst_orig = archive_dir / f"{filepath.stem}_{n}{filepath.suffix}"
            n += 1
        shutil.copy2(filepath, dst_orig)

        dest_dir.mkdir(parents=True, exist_ok=True)
        final_content = serialize_frontmatter(meta) + "\n\n" + md_body.lstrip()
        dest_path.write_text(final_content, encoding="utf-8")
        trash_file(filepath, get_vault_root())

    return {"type": "txt_transmuted", "target": str(dest_path)}


def refine_file(filepath: Path, project_id: str = None, dry_run: bool = False, encoding=None) -> dict:
    root = get_vault_root()
    if filepath.resolve().is_relative_to(root.resolve()/"05-Journal"):
        print("[BLOCKED] 日记原文不自动入库，请显式挑选内容另存为素材",file=sys.stderr)
        return {}
    base_dir, is_ai_zone = domain_info(root, project_id)
    archive_dir = monthly_archive_dir((base_dir if is_ai_zone else root) / "99-Assets" / "98-Archives")

    ext = filepath.suffix.lower()
    if ext in (".md", ".markdown"):
        return process_md_file(filepath, base_dir, dry_run=dry_run, encoding=encoding)
    elif ext in (".txt", ".rtf"):
        return process_txt_file(filepath, base_dir, archive_dir, dry_run=dry_run, encoding=encoding)
    else:
        print(f"[WARN] note_refiner 不支持此扩展名: {filepath.name}")
        return {}


def main():
    parser = argparse.ArgumentParser(description="Markdown 与纯文本六路自适应升阶引擎 (Note Refiner)")
    parser.add_argument("-i", "--input", help="指定单个笔记或纯文本路径处理")
    parser.add_argument("--project-id", help="课题域 ID (缺省入库 AI-Zone)")
    parser.add_argument("--dry-run", action="store_true", help="演练模式，不写盘")
    parser.add_argument("--encoding", help="显式源文本编码，如 cp1252 或 gb18030")
    args = parser.parse_args()

    root = get_vault_root()
    if args.input:
        src = Path(args.input)
        if not src.is_absolute():
            src = root / src
        if not src.is_file():
            print(f"[x] 文件不存在: {args.input}", file=sys.stderr)
            return 1
        try:
            res = refine_file(src, project_id=args.project_id, dry_run=args.dry_run, encoding=args.encoding)
        except FileNotFoundError as exc:
            print(f"[BLOCKED] {exc}", file=sys.stderr)
            return 1
        if res:
            print(f"[PASS] 升阶完成: {res.get('target')}")
            return 0
        return 1

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
