"""Pure 116 adaptive slicing planner, adapted to Atlas Markdown safety.

No filesystem, registry or network access. Budget is a soft limit for indivisible
paragraphs/code/tables; over-budget cards are reported rather than truncated.
"""
from __future__ import annotations
import re
from typing import Any, Dict, List, Optional, Tuple
from notes.atlas_markdown_blocks import code_line_mask

CODE_FENCE_RE = re.compile(r"^(```+|~~~+)")
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.+)$")
APPENDIX_RE = re.compile(r"^(?:附录\s*[A-Za-z]|[A-Za-z]\.\d+)")

def scan_code_fence_intervals(lines):
    intervals = []
    start = None
    for idx, protected in enumerate(code_line_mask("".join(lines))):
        if protected and start is None:
            start = idx
        elif not protected and start is not None:
            intervals.append((start, idx - 1))
            start = None
    if start is not None:
        intervals.append((start, len(lines) - 1))
    return intervals

def sanitize_filename(title: str) -> str:
    """清理文件名中的非法字符与多余空格，限制最长 80 字符。"""
    clean = re.sub(r'[\\/*?:"<>|#`\[\]]', "", title).strip()
    clean = re.sub(r"\s+", "_", clean)
    return clean[:80] if clean else "未命名章节"

def generate_compact_prefix(doc_stem: str, custom_prefix: Optional[str] = None, max_len: int = 18) -> str:
    """提取紧凑短前缀；唯一性由写入前的全库防撞检查保证。

    1. 若传入 custom_prefix，清洗非法字符后截断至 max_len 返回；
    2. 若匹配 arXiv/arxiv 标准编号格式（如 arXiv-2401.05707），直接返回该编号（16字符）；
    3. 若按 [- _] 分隔存在多段，取前两段（会议/机构 + 年份一作）：
       - 清洗会议后缀（如 Findings）；
       - 提取第二段首个英数或中文主词（避免长副标题混入）；
       - 若超长尝试缩写 20xx -> xx；
    4. 兜底截取 doc_stem[:max_len]。
    """
    if custom_prefix:
        c = re.sub(r'[^A-Za-z0-9_\-\u4e00-\u9fa5]', '', custom_prefix.strip())
        return c[:max_len] if c else (doc_stem[:max_len] if doc_stem else "Doc")

    s = (doc_stem or "Doc").strip()

    # 规则 1: arXiv-xxxx.xxxxx 或 arxiv-xxxx.xxxxx
    m_arxiv = re.match(r'^(arxiv)[-_](\d{4}\.\d{4,5})', s, re.IGNORECASE)
    if m_arxiv:
        prefix_stem = s[:len(m_arxiv.group(1))]
        return f"{prefix_stem}-{m_arxiv.group(2)}"

    parts = [p for p in re.split(r'[-_]', s) if p]
    if len(parts) >= 2:
        p0, p1 = parts[0], parts[1]
        p0_clean = re.sub(r'Findings$', '', p0, flags=re.IGNORECASE)
        # 若 p1 同时包含中英文过渡，优先取前段主词
        sub_tokens = re.findall(r'[A-Za-z0-9]+|[\u4e00-\u9fa5]+', p1)
        p1_cand = sub_tokens[0] if sub_tokens else p1

        cand = f"{p0_clean}-{p1_cand}"
        if len(cand) <= max_len:
            return cand
        # 尝试将 20xx 压成 xx
        p0_short_year = re.sub(r'20(\d{2})', r'\1', p0_clean)
        cand_short = f"{p0_short_year}-{p1_cand}"
        if len(cand_short) <= max_len:
            return cand_short
        return cand[:max_len]
    elif len(parts) == 1:
        return parts[0][:max_len]
    return s[:max_len]

def get_code_interval_for_line(idx: int, intervals: List[Tuple[int, int]]) -> Optional[Tuple[int, int]]:
    """判断指定行号是否落在代码块区间内；若是返回 (start, end)，否则返回 None。"""
    for s, e in intervals:
        if s <= idx <= e:
            return (s, e)
        if idx < s:
            break
    return None

def extract_paragraphs(lines: List[str], line_range: Tuple[int, int],
                       intervals: List[Tuple[int, int]]) -> List[Dict[str, Any]]:
    """将指定行范围 [start, end) 解构为段落单元列表。
    代码块内部的空行绝对不作为分段边界，整个代码块被视为不可分割的原子段落。
    """
    start_idx, end_idx = line_range
    paras: List[Dict[str, Any]] = []
    curr_start: Optional[int] = None
    curr_end: Optional[int] = None
    has_code = False
    i = start_idx

    while i < end_idx:
        c_int = get_code_interval_for_line(i, intervals)
        if c_int:
            if curr_start is None:
                curr_start = i
            has_code = True
            curr_end = min(c_int[1] + 1, end_idx)
            i = curr_end
            continue

        stripped = lines[i].strip()
        if not stripped:
            if curr_start is not None:
                text = "".join(lines[curr_start:i]).strip("\r\n")
                if text:
                    paras.append({
                        "start": curr_start,
                        "end": i,
                        "text": text,
                        "chars": len(text),
                        "has_code": has_code
                    })
                curr_start = None
                has_code = False
            i += 1
        else:
            if curr_start is None:
                curr_start = i
            i += 1
            curr_end = i

    if curr_start is not None and curr_end is not None:
        text = "".join(lines[curr_start:curr_end]).strip("\r\n")
        if text:
            paras.append({
                "start": curr_start,
                "end": curr_end,
                "text": text,
                "chars": len(text),
                "has_code": has_code
            })

    return paras

def chunk_paragraphs(paras: List[Dict[str, Any]], target_chars: int = 300,
                     max_chars: int = 4000) -> List[Dict[str, Any]]:
    """段落贪心聚合切片：
    - 一段超过 target_chars (300字)，独立切出成卡；
    - 未超 target_chars，向后累加到下一段，直至累积达到 >= target_chars 切出成卡；
    - 尾部残余如果与前一切片合并 <= max_chars (4000) 则并入前一切片，否则独立成卡；
    - 全文总字数不足 target_chars 时整合成单一切片保底。
    """
    chunks: List[Dict[str, Any]] = []
    curr_paras: List[Dict[str, Any]] = []
    curr_chars = 0

    for p in paras:
        # Flush before a paragraph would overflow the budget. Never split an
        # indivisible paragraph/table/code block merely to satisfy a count.
        if curr_paras and curr_chars + p["chars"] > max_chars:
            chunks.append({"start": curr_paras[0]["start"], "end": curr_paras[-1]["end"],
                           "paras": curr_paras, "total_chars": curr_chars,
                           "has_code": any(x["has_code"] for x in curr_paras)})
            curr_paras = []
            curr_chars = 0
        curr_paras.append(p)
        curr_chars += p["chars"]
        if curr_chars >= target_chars:
            chunks.append({
                "start": curr_paras[0]["start"],
                "end": curr_paras[-1]["end"],
                "paras": curr_paras,
                "total_chars": curr_chars,
                "has_code": any(x["has_code"] for x in curr_paras)
            })
            curr_paras = []
            curr_chars = 0

    if curr_paras:
        if chunks and (chunks[-1]["total_chars"] + curr_chars <= max_chars):
            chunks[-1]["end"] = curr_paras[-1]["end"]
            chunks[-1]["paras"].extend(curr_paras)
            chunks[-1]["total_chars"] += curr_chars
            chunks[-1]["has_code"] = chunks[-1]["has_code"] or any(x["has_code"] for x in curr_paras)
        else:
            chunks.append({
                "start": curr_paras[0]["start"],
                "end": curr_paras[-1]["end"],
                "paras": curr_paras,
                "total_chars": curr_chars,
                "has_code": any(x["has_code"] for x in curr_paras)
            })

    return chunks

def extract_title_from_text(lines: List[str], start_line: int, end_line: int,
                            fallback: str = "正文片段") -> str:
    """从行范围中提炼首句关键断言作为切片标题。"""
    for idx in range(start_line, end_line):
        line = lines[idx].strip()
        if not line or CODE_FENCE_RE.match(line):
            continue
        cleaned = re.sub(r"^[>\-\*\+\d\.\s#`]+", "", line).strip()
        if not cleaned:
            continue
        m = re.split(r"[。！？；\n]", cleaned)
        first_clause = m[0].strip() if m else cleaned
        candidate = sanitize_filename(first_clause)
        if len(candidate) >= 2:
            return candidate[:30]
    return fallback

def generate_automated_summary(display_title: str,
                               extent_text: str,
                               bundle_subtitles: Optional[List[str]] = None,
                               doc_stem: str = "",
                               max_chars: int = 80) -> str:
    """纯脚本离线生成轻量高密度切片导读摘要 (30~80字)：
    1. 从切片正文提取干净首句（跳过代码栅格、标题、表格行、图片与导航行）；
    2. 若为同级聚合切片，提取子节主题序列并智能拼接首句；
    3. 单节切片以首句或规范主题直接陈述，杜绝导读废话，免除人工/智能体回填。
    """
    lines = extent_text.strip().splitlines()
    body_paras: List[str] = []
    in_code = False
    for line in lines:
        sline = line.strip()
        if sline.startswith(('```', '~~~')):
            in_code = not in_code
            continue
        if in_code or not sline:
            continue
        if sline.startswith(('#', '|', '---', '![')):
            continue
        if sline.startswith('> 导航：') or sline.startswith('导航：') or sline.startswith('> 导读说明：'):
            continue
        # 允许 callout 内部正文参与摘要（剥离 > 和 [!NOTE] 等标头）
        if sline.startswith('>'):
            sline = re.sub(r'^>\s*(\[![\w-]+\])?\s*', '', sline).strip()
            if not sline:
                continue
        # 移除行首列表或数字标号
        cleaned = re.sub(r'^(?:[-*+]|\d+[.)、])\s*', '', sline)
        cleaned = re.sub(r'\[\[([^\|\]]+)(?:\|[^\]]+)?\]\]', r'\1', cleaned)
        cleaned = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', cleaned)
        cleaned = re.sub(r'[*_`~]', '', cleaned)
        if 'http' in cleaned or 'www.' in cleaned or '....' in cleaned:
            continue
        cleaned = cleaned.strip()
        if len(cleaned) >= 6:
            body_paras.append(cleaned)
            if len(''.join(body_paras)) >= 120:
                break

    lead_sentence = ""
    if body_paras:
        joined = ' '.join(body_paras)
        parts = [p.strip() for p in re.split(r'[。！？!?；;\n]', joined) if p.strip()]
        if parts:
            cand = parts[0]
            if len(cand) < 15 and len(parts) > 1 and len(cand) + len(parts[1]) <= max_chars:
                cand = f"{cand}，{parts[1]}"
            if 10 <= len(cand) <= max_chars and not cand.startswith(('表', '图')):
                lead_sentence = cand if cand.endswith(('。', '…')) else cand + '。'

    # 聚合切片导读
    if bundle_subtitles:
        cleaned_subs: List[str] = []
        for t in bundle_subtitles:
            c = re.sub(r'^(?:章节：|\d+(\.\d+)*\s*|[一二三四五六七八九十]+[、.]\s*)', '', t).strip()
            c = re.sub(r'（[^）]+）|\([^\)]+\)', '', c).strip()
            if c and c not in cleaned_subs:
                cleaned_subs.append(c)
        if cleaned_subs:
            sub_str = "、".join(cleaned_subs[:4])
            if len(cleaned_subs) > 4:
                sub_str += f" 等 {len(cleaned_subs)} 个小节"
            if lead_sentence and len(f"涵盖{sub_str}。{lead_sentence}") <= max_chars:
                return f"涵盖{sub_str}。{lead_sentence}"
            return f"涵盖{sub_str}等相关规范与说明。"

    # 单节切片导读
    if lead_sentence:
        return lead_sentence

    clean_disp = re.sub(r'^(?:章节：|\d+(\.\d+)*\s*|[一二三四五六七八九十]+[、.]\s*)', '', display_title).strip()
    return f"{clean_disp}相关规范与正文说明。"

def build_heading_tree(lines: List[str],
                       code_intervals: List[Tuple[int, int]]) -> Tuple[str, List[Dict[str, Any]], List[int]]:
    """扫描全文并构建层级大纲树。

    基于前置扫描的代码栅格区间，防御代码内部的 # 穿透，仅行首符合 ^#{1,6}\\s+ 的才被识别为标题。
    :return: (preamble, headings, root_indices)
    """
    headings: List[Dict[str, Any]] = []

    for idx, line in enumerate(lines):
        if get_code_interval_for_line(idx, code_intervals):
            continue
        stripped = line.strip()
        m = HEADING_RE.match(line.rstrip("\r\n"))
        if m:
            lvl = len(m.group(1))
            title = m.group(2).strip()
            # 附录顶级标头校准：防止附录二级标题误嵌套进前一章一级标题
            if APPENDIX_RE.match(title) and lvl > 1:
                lvl = 1
            headings.append({
                "idx": len(headings),
                "start": idx,
                "level": lvl,
                "title": title,
                "children": [],
                "parent": None,
                "end": len(lines),
            })

    # 计算 Extent 闭包边界：至下一个同级或更浅标题行前止
    for i, h in enumerate(headings):
        for j in range(i + 1, len(headings)):
            if headings[j]["level"] <= h["level"]:
                h["end"] = headings[j]["start"]
                break

    # 栈式构建父子关系与根节点列表
    stack: List[Dict[str, Any]] = []
    roots: List[int] = []
    for h in headings:
        while stack and stack[-1]["level"] >= h["level"]:
            stack.pop()
        if stack:
            h["parent"] = stack[-1]["idx"]
            stack[-1]["children"].append(h["idx"])
        else:
            roots.append(h["idx"])
        stack.append(h)

    # 计算节点自身引言与子树总字数、祖先链
    for h in headings:
        extent_text = "".join(lines[h["start"]:h["end"]]).strip()
        h["total_chars"] = len(extent_text)
        h["has_code"] = any(s < h["end"] and e >= h["start"] for s, e in code_intervals)

        if h["children"]:
            first_child_start = headings[h["children"][0]]["start"]
            h["lead_text"] = "".join(lines[h["start"] + 1:first_child_start]).strip()
        else:
            h["lead_text"] = "".join(lines[h["start"] + 1:h["end"]]).strip()
        h["lead_chars"] = len(h["lead_text"])

        # 构建 chapter_path 祖先链
        path_nodes = []
        curr: Optional[Dict[str, Any]] = h
        while curr:
            path_nodes.append(curr["title"])
            curr = headings[curr["parent"]] if curr["parent"] is not None else None
        path_nodes.reverse()
        h["chapter_path"] = " > ".join(path_nodes)

    first_start = headings[0]["start"] if headings else len(lines)
    preamble = "".join(lines[:first_start]).strip()

    return preamble, headings, roots

def pack_sibling_nodes(batch: List[int], headings: List[Dict[str, Any]],
                       target_c: int, max_c: int, min_c: int) -> List[List[int]]:
    """同级兄弟节点目标预算贪心聚合算法：
    以 target_c (如 2000 字) 为目标预算聚合中小型小节，杜绝过度切碎成微卡；
    当累计字数达到 target_c 且单节不是碎尾巴时结包，单包绝不超过 max_c (4000 字)。
    """
    packs: List[List[int]] = []
    curr_pack: List[int] = []
    curr_chars = 0

    for idx in batch:
        tc = headings[idx]["total_chars"]
        if not curr_pack:
            curr_pack.append(idx)
            curr_chars = tc
            continue

        if curr_chars + tc <= max_c:
            if curr_chars < target_c:
                curr_pack.append(idx)
                curr_chars += tc
            else:
                # 当前包已达到目标预算 target_chars
                # 若当前小节很小 (< 500 字)，且并入后仍不超过 max_c，则顺带吸收，避免残留孤立碎片
                if tc < 500:
                    curr_pack.append(idx)
                    curr_chars += tc
                else:
                    packs.append(curr_pack)
                    curr_pack = [idx]
                    curr_chars = tc
        else:
            packs.append(curr_pack)
            curr_pack = [idx]
            curr_chars = tc

    if curr_pack:
        # 最后一包若过小 (< 800 字) 且并入前包不超过 max_c，则合并入前包
        if packs and (sum(headings[x]["total_chars"] for x in packs[-1]) + curr_chars <= max_c):
            packs[-1].extend(curr_pack)
        else:
            packs.append(curr_pack)

    return packs

def decide_adaptive_nodes(headings: List[Dict[str, Any]], roots: List[int],
                          rules: Dict[str, Any]) -> Tuple[Dict[int, str], List[Dict[str, Any]]]:
    """自适应探测状态机：自顶向下字数预算评估 + 目标预算兄弟节点聚合打包。

    状态定义：
    - 'slice'        : 单一节点字数合规，整棵子树独立切片成卡；
    - 'bundle_head'  : 同级连续小节聚合打包的首节点，代表复合切片；
    - 'bundle_member': 同级连续小节聚合打包的伴生成员，正文并入复合切片；
    - 'split'        : 字数超标且有子标题，向下深挖探测子节点，引言留 TOC；
    - 'inline'       : 仅作为结构性大纲容器，正文已聚合或下沉。
    :return: (节点状态字典, 复合打包切片列表)
    """
    node_status: Dict[int, str] = {}
    bundles: List[Dict[str, Any]] = []
    min_c = rules.get("min_chars", 300)
    target_c = rules.get("target_chars", 2000)
    max_c = rules.get("max_chars", 4000)

    def process_siblings(sibling_indices: List[int]):
        content_batch: List[int] = []

        def flush_batch():
            nonlocal content_batch
            if not content_batch:
                return

            packs = pack_sibling_nodes(content_batch, headings, target_c, max_c, min_c)
            for p in packs:
                if len(p) == 1:
                    node_status[p[0]] = "slice"
                else:
                    first_h = headings[p[0]]
                    last_h = headings[p[-1]]
                    bundle_info = {
                        "indices": p,
                        "head_idx": p[0],
                        "tail_idx": p[-1],
                        "title": f"{first_h['title']}_至_{last_h['title']}",
                        "display_title": f"{first_h['title']} 至 {last_h['title']}",
                        "sub_titles": [headings[x]["title"] for x in p],
                        "level": first_h["level"],
                        "chapter_path": first_h["chapter_path"],
                        "start": first_h["start"],
                        "end": last_h["end"],
                        "total_chars": sum(headings[x]["total_chars"] for x in p),
                        "has_code": any(headings[x]["has_code"] for x in p),
                    }
                    bundles.append(bundle_info)
                    node_status[p[0]] = "bundle_head"
                    for member_idx in p[1:]:
                        node_status[member_idx] = "bundle_member"

            content_batch = []

        for idx in sibling_indices:
            h = headings[idx]
            tc = h["total_chars"]
            has_children = len(h["children"]) > 0

            # 仅当总字数 > max_chars 且拥有子节点时向下深挖
            if tc > max_c and has_children:
                flush_batch()
                node_status[idx] = "split"
                process_siblings(h["children"])
            elif tc > max_c:
                # 无子节点的超长叶子：无法向下深挖，段落级兜底切分，保留不可分段落并报告超预算
                flush_batch()
                node_status[idx] = "leaf_split"
            else:
                content_batch.append(idx)

        flush_batch()

    process_siblings(roots)
    return node_status, bundles

def plan_adaptive(content: str, doc_stem: str, min_chars: int = 300, max_chars: int = 4000, target_chars: int = 2000):
    lines = content.splitlines(keepends=True)

    # 0. 前置识别全文所有代码栅格区间
    code_intervals = scan_code_fence_intervals(lines)

    # 1. 扫描大纲树
    preamble, headings, roots = build_heading_tree(lines, code_intervals)

    cards: List[Dict[str, Any]] = []
    short_preamble = ""
    short_leads = []
    node_status = {}
    bundles = []

    if not headings:
        # 分支 A：全文无标题纯文本，基于段落贪心聚合切片
        paras = extract_paragraphs(lines, (0, len(lines)), code_intervals)
        total_chars = sum(p["chars"] for p in paras)
        if total_chars == 0:
            return {"cards": [], "headings": headings, "roots": roots, "node_status": {}, "bundles": []}

        chunks = chunk_paragraphs(paras, target_chars=min_chars, max_chars=max_chars)
        for i, chk in enumerate(chunks, 1):
            title = extract_title_from_text(lines, chk["start"], chk["end"], fallback=f"{doc_stem}_第{i}节")
            cards.append({
                "start": chk["start"],
                "end": chk["end"],
                "title": title,
                "display_title": title,
                "chapter_path": f"{doc_stem} > {title}",
                "has_code": chk["has_code"],
                "is_bundle": False,
                "head_idx": -i,
            })
    else:
        # 分支 B：全文有标题（含前置正文+中间突发标题的非规范文档）
        first_heading_start = headings[0]["start"]
        short_preamble = ""
        if first_heading_start > 0:
            preamble_paras = extract_paragraphs(lines, (0, first_heading_start), code_intervals)
            preamble_chars = sum(p["chars"] for p in preamble_paras)
            if preamble_chars >= 100 or any(start < first_heading_start for start, _ in code_intervals):
                # 前言正文具有实质性内容，切片成卡，杜绝孤儿正文丢失或内联在 TOC 目录中
                preamble_chunks = chunk_paragraphs(preamble_paras, target_chars=min_chars, max_chars=max_chars)
                for idx, chk in enumerate(preamble_chunks, 1):
                    fallback_title = "前言与背景" if len(preamble_chunks) == 1 else f"前言与背景_第{idx}节"
                    title = extract_title_from_text(lines, chk["start"], chk["end"], fallback=fallback_title)
                    cards.append({
                        "start": chk["start"],
                        "end": chk["end"],
                        "title": title,
                        "display_title": title,
                        "chapter_path": f"{doc_stem} > {title}",
                        "has_code": chk["has_code"],
                        "is_bundle": False,
                        "head_idx": -100 - idx,
                        "is_preamble": True,
                    })
            elif preamble_chars > 0:
                short_preamble = "".join(lines[:first_heading_start]).strip("\r\n")

        rules = {"min_chars": min_chars, "target_chars": target_chars, "max_chars": max_chars}
        node_status, bundles = decide_adaptive_nodes(headings, roots, rules)
        bundle_by_head = {b["head_idx"]: b for b in bundles}

        short_leads = []
        for idx, st in node_status.items():
            if st == "slice":
                h = headings[idx]
                cards.append({
                    "start": h["start"],
                    "end": h["end"],
                    "title": h["title"],
                    "display_title": h["title"],
                    "chapter_path": h["chapter_path"],
                    "has_code": h.get("has_code", False),
                    "is_bundle": False,
                    "head_idx": idx,
                })
            elif st == "split":
                h = headings[idx]
                if h["children"] and h["lead_chars"] > 0:
                    first_child_start = headings[h["children"][0]]["start"]
                    has_table = "|" in h["lead_text"]
                    has_code = any(s < first_child_start and e >= h["start"] for s, e in code_intervals)
                    if h["lead_chars"] >= 100 or has_table or has_code:
                        cards.append({
                            "start": h["start"],
                            "end": first_child_start,
                            "title": f"{h['title']}_概述",
                            "display_title": f"{h['title']} 概述",
                            "chapter_path": f"{h['chapter_path']} > 概述",
                            "has_code": has_code,
                            "is_bundle": False,
                            "head_idx": f"lead_{idx}",
                            "is_lead_overview": True,
                            "split_node_idx": idx,
                        })
                    else:
                        short_leads.append((first_child_start, h["lead_text"]))
            elif st == "leaf_split":
                h = headings[idx]
                # 超长叶子无子标题可深挖，复用段落贪心聚合兜底切分，以 max_chars 为预算，不截断不可分段落
                leaf_paras = extract_paragraphs(lines, (h["start"], h["end"]), code_intervals)
                # Keep a standalone heading attached to its first evidence
                # paragraph, including when that paragraph is over budget.
                if len(leaf_paras) > 1 and all(not line.strip() or HEADING_RE.match(line) for line in leaf_paras[0]["text"].splitlines()):
                    first, following = leaf_paras[0], leaf_paras[1]
                    following["start"] = first["start"]
                    following["text"] = "".join(lines[first["start"]:following["end"]]).strip("\r\n")
                    following["chars"] = len(following["text"])
                    following["has_code"] = first["has_code"] or following["has_code"]
                    leaf_paras = leaf_paras[1:]
                leaf_chunks = chunk_paragraphs(leaf_paras, target_chars=target_chars, max_chars=max_chars)
                for ci, chk in enumerate(leaf_chunks, 1):
                    leaf_title = h["title"] if ci == 1 else f"{h['title']}_第{ci}节"
                    leaf_display = h["title"] if ci == 1 else f"{h['title']} 第{ci}节"
                    leaf_path = h["chapter_path"] if ci == 1 else f"{h['chapter_path']} > 第{ci}节"
                    cards.append({
                        "start": chk["start"],
                        "end": chk["end"],
                        "title": leaf_title,
                        "display_title": leaf_display,
                        "chapter_path": leaf_path,
                        "has_code": chk["has_code"],
                        "is_bundle": False,
                        "head_idx": f"leafsplit_{idx}_{ci}",
                        "split_node_idx": idx,
                    })
        for b in bundles:
            cards.append({
                "start": b["start"],
                "end": b["end"],
                "title": b["title"],
                "display_title": b["display_title"],
                "chapter_path": b["chapter_path"],
                "has_code": b.get("has_code", False),
                "is_bundle": True,
                "head_idx": b["head_idx"],
                "bundle_info": b,
            })

    if not cards:
        # 兜底：文档内容未切出卡，进行全篇段落保底成卡
        paras = extract_paragraphs(lines, (0, len(lines)), code_intervals)
        if not paras:
            return {"cards": [], "headings": headings, "roots": roots, "node_status": {}, "bundles": []}
        cards.append({
            "start": 0,
            "end": len(lines),
            "title": doc_stem,
            "display_title": doc_stem,
            "chapter_path": doc_stem,
            "has_code": bool(code_intervals),
            "is_bundle": False,
            "head_idx": 0,
        })

    cards.sort(key=lambda x: x["start"])

    # 方案 A 彻底全量切片化沉淀：短前言与短前导正文自动并入后续首个切片卡，杜绝正文遗漏与 TOC 目录内联文本噪音
    if short_preamble and cards:
        cards[0]["prepend_text"] = (short_preamble + "\n\n" + cards[0].get("prepend_text", "")).strip("\r\n")

    for fc_start, ltext in short_leads:
        target_card = next((c for c in cards if c["start"] >= fc_start), None)
        if target_card:
            target_card["prepend_text"] = (ltext + "\n\n" + target_card.get("prepend_text", "")).strip("\r\n")
        elif cards:
            cards[-1]["prepend_text"] = (cards[-1].get("prepend_text", "") + "\n\n" + ltext).strip("\r\n")

    for card in cards:
        content = "".join(lines[card["start"]:card["end"]]).strip("\r\n")
        if card.get("prepend_text"):
            content = card["prepend_text"] + "\n\n" + content
        card["content"] = content
        card["budget_exceeded"] = len(content) > max_chars
        card["summary"] = generate_automated_summary(
            card.get("display_title", card["title"]), content,
            card.get("bundle_info", {}).get("sub_titles"), doc_stem)[:200]
    return {"cards": cards, "headings": headings, "roots": roots,
            "node_status": node_status, "bundles": bundles}
