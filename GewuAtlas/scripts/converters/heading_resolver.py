#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
heading_resolver.py - 文档级标题层级裁定引擎 (Heading Resolver)

设计支点：标题层级是「整篇文档的属性」，不是「单行文本的属性」。
故本模块在全文采集完毕后运行一趟，用三档优先链定层，取代逐行启发式：

  档 1 书签驱动  PDF 内嵌 outline（doc.get_toc()）是作者显式标注的权威层级，
                 命中并锚定成功的块置 locked，任何启发式不得改写；书签覆盖达标时，
                 未锚定候选一律降为正文（图注/落款/页眉由此消失，无需逐个打补丁）。
  档 2 联合证据   书签缺失或覆盖不足时，把候选切成「编号文法组」与「字号组」放进
                 同一张榜单，按组内中位字号降序共同定层——层级从文档自身证据推出，
                 不硬编码，也不让字号层永远压在文法层之下。

  版式熔断      落款行、页码 Folio 行、数字串伪标题（IP/版本号）、跨页复现的页眉
                 骨架（数字抹成 # 后同骨架出现在 ≥4 页）在定层前先行出局。

收口：启发式档位做整篇保序压缩（distinct 层级升序 → 1..k），同级兄弟永不分层；
      逐行 prev_level+1 钳位退役。
"""

import re
from typing import Dict, List, Optional, Tuple

# ── 编号形态族 ────────────────────────────────────────────────
# (族名, 匹配式, 同族内用于细分深组的段数提取式)
FAMILIES: List[Tuple[str, "re.Pattern", Optional["re.Pattern"]]] = [
    ("bian hao",   re.compile(r"^第\s*[一二三四五六七八九十百0-9]+\s*[编卷册]\s*[、.．]?\s*\S"), None),
    ("zhang jie",  re.compile(r"^第\s*[一二三四五六七八九十百0-9]+\s*[章节篇部条项讲]\s*[、.．]?"), None),
    ("cn dot",     re.compile(r"^[一二三四五六七八九十百]+\s*[、.．]\s*\S"), None),
    ("cn paren",   re.compile(r"^[（(]\s*[一二三四五六七八九十百]+\s*[）)]\s*\S"), None),
    ("gb sec",     re.compile(r"^\d+\s*[　 ]\s*[^\d\s]\S*"), None),
    ("dun hao",    re.compile(r"^\d+\s*[、.．]\s*[^\d]"), None),
    ("multi dot",  re.compile(r"^(?P<nums>\d{1,2}(?:\.\d{1,2}){1,5})(?![\d.])\s*[、.．]?\s*[^\d.]"),
     re.compile(r"^\d+(?:\.(\d+))+")),
    ("paren num",  re.compile(r"^[（(]\s*\d+\s*[）)]\s*\S"), None),
    ("cfr sect",   re.compile(r"^§\s*\d{1,4}(?:\.\d{1,2}){0,5}"),
     re.compile(r"^§\s*\d+(?:\.\d+)+")),
    ("ecfr letter", re.compile(r"^\(?[a-z]\)\.?\s*[^\d]"), None),
    ("ecfr roman",  re.compile(r"^\(?[ivxlcdm]+\.?\)?\s+[^\d]"), None),
]

# 同族内需要按点分深度继续下钻的族
DEEP_FAMILIES = {"multi dot", "cfr sect"}

_SENTENCE_END = ("。", "！", "？", "；", "…", ";")
_CJK_PAUSE = ("，", "、", "：", ":", "。", "；", "！", "？")
# 字号组入选门槛：中位字号须超出正文 8%，否则视为正文噪音
_FONT_GAIN = 1.08

# 行首数字串合法性护栏：各段 ≤2 位、首段 ≤30，且编号后不得紧跟中文逗号（正文断行特征）
_SAFE_MULTIDOT = re.compile(r"^\d{1,2}(?:\.\d{1,2}){1,5}(?![\d.])")

# 标题前导编号剥离式（裸题名比对用）：只认「编号 + 明确分隔」的形态，
# 拒绝裸首字母，避免把英文单词的头部当成编号削掉。
_NUM_PREFIX = re.compile(
    r"^(?:"
    r"第[一二三四五六七八九十百0-9]+[章节篇部条项讲编卷册][、.．]?"
    r"|\d{1,4}(?:\.\d{1,2}){0,5}[、.．]?"
    r"|[（(]\d{1,4}[）)]"
    r"|[一二三四五六七八九十百]{1,4}[、.．]"
    r"|[（(][一二三四五六七八九十百]{1,4}[）)]"
    r"|[ivxlcdm]{1,5}[.)]"
    r")[\s　]*"
)


def normalize_title(text: str) -> str:
    """书签标题与正文块标题的比对键：去空白、全角转半角括号、去尾点、大小写折叠。

    大小写折叠是锚定前提：arXiv 类排版把标题渲成小型大写（正文「INTRODUCTION」
    对书签「Introduction」），不折叠则整篇锚定失败、退回启发式推定。
    """
    t = re.sub(r"[\s　]+", "", text or "")
    t = t.replace("（", "(").replace("）", ")").replace("．", ".")
    return t.rstrip("。.、，:：").lower()


def detect_family(text: str) -> Optional[Tuple[str, Optional[int]]]:
    """返回 (族名, 点分深度)；无族命中返回 None。"""
    clean = (text or "").strip()
    if not clean:
        return None
    for name, pat, deep in FAMILIES:
        if pat.match(clean):
            depth = None
            if name in DEEP_FAMILIES and deep:
                m = deep.match(clean)
                depth = m.group(0).count(".") if m else None
            return (name, depth)
    return None


def is_spurious_numeric_heading(text: str) -> bool:
    """
    拦掉 IP / 版本号 / 标准年号被当成多级编号：
    合法多级编号须匹配 _SAFE_MULTIDOT（每段 ≤2 位），且编号后紧跟空白或顿号/点，
    而非中文逗号（正文断行的强特征）。
    """
    clean = (text or "").strip()
    m = re.match(r"^(\d+(?:\.\d+)+)", clean)
    if not m:
        return False
    num = m.group(1)
    if not _SAFE_MULTIDOT.match(num):
        return True  # 出现 3 位以上数段（127.0.0.1 / 2026.3.7）
    rest = clean[len(num):]
    if rest[:1] in _CJK_PAUSE:
        return True  # 编号后直接接中文标点，是正文不是标题
    return False


def is_colophon_line(text: str) -> bool:
    """封面落款与发布/实施日期行不作标题（版式特征，非关键词猜测）。"""
    clean = (text or "").strip()
    if re.search(r"(发布|实施|Published|Issued|Date)\s*\d{4}", clean):
        return True
    if re.search(r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]?\s*\d{0,2}日?\s*(发布|实施)$", clean):
        return True
    return bool(re.search(r"(技术委员会|管理委员会|监督管理局|标准化委员会)$", clean))


def is_page_marker(text: str) -> bool:
    """纯页码/卷册 Folio 行（1、iv、IX.）不是标题：整行只剩编号或罗马页符。"""
    clean = normalize_title(text)
    if not clean:
        return True
    if re.fullmatch(r"\d{1,4}[.．]?", clean):
        return True
    return bool(re.fullmatch(r"[ivxlcdm]{1,9}[.．]?", clean, re.I))


def demote_running_frames(cands: List[Dict], min_pages: int = 4) -> int:
    """
    页眉/页脚复现行熔断：把数字抹成 # 得到「版式骨架」，同一骨架在 ≥4 个不同页码
    复现即判为每页重复的页眉/页脚（如「1 CFR Ch. I (1–1–19 Edition) § 2.3」），
    整族降级。判据是复现版式，不看词表，故对任意语种同样有效。
    """
    pages_by_skel: Dict[str, set] = {}
    for c in cands:
        clean = normalize_title(c["text"])
        skel = re.sub(r"\d+", "#", clean)
        if skel == clean:
            continue  # 无数字的行不在此熔断，交给字号与文法档位
        pages_by_skel.setdefault(skel, set()).add(c["page"])
    hits = 0
    for c in cands:
        if c.get("locked") or c.get("demote"):
            continue
        clean = normalize_title(c["text"])
        skel = re.sub(r"\d+", "#", clean)
        if skel != clean and len(pages_by_skel.get(skel, ())) >= min_pages:
            c["level"] = None
            c["demote"] = True
            hits += 1
    return hits


def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def strip_leading_number(text: str) -> str:
    """剥掉标题前导编号，得到裸题名比对键。

    版式型 PDF（Word 导出的 GB/T 标准）常把小节号做成「列表标签」，MuPDF 的文字流
    只吐得出题名（正文里 5.2 那一行只剩「功能架构」）。书签仍带完整编号，
    故比对须能在「去编号」这一层对齐，否则整级标题会从层级树里消失。
    """
    t = normalize_title(text)
    m = _NUM_PREFIX.match(t)
    if m:
        return t[m.end():]
    # 附录段字母胶合：小型大写排版把「A Experimental Setup」吐成「AEXPERIMENTAL …」，
    # 剥掉段首孤立大写字母再比对；仅当胶合词后还有后续词（空白）时动手，
    # 以免误削 ABSTRACT 这类单词标题的首字母。
    raw = (text or "").strip()
    m = re.match(r"^[A-Z](?=[A-Z]+\s)", raw)
    if m:
        return normalize_title(raw[1:])
    return t


def _build_indexes(toc):
    """一轮书签两条索引（完整题名 / 裸题名），共用同一批 entry 对象以共享 used 标记。"""
    entries, by_norm, by_bare = [], {}, {}
    for lvl, title, pg in toc:
        e = {"level": lvl, "page": pg, "title": title, "used": False}
        entries.append(e)
        for key, bucket in ((normalize_title(title), by_norm), (strip_leading_number(title), by_bare)):
            if len(key) >= 2:
                bucket.setdefault(key, []).append(e)
    return by_norm, by_bare


def anchor_bookmarks(cands: List[Dict], toc: List[Tuple[int, str, int]]) -> Tuple[int, int]:
    """
    档 1：把候选块锚定到内嵌书签。同一标题在多篇里重复出现，按文档序消费，
    页码容差 ±1（导出版本的 outline 目标页常有偏移）。返回 (锚定候选数, 消费书签数)。

    两轮比对：先按完整题名精确/前缀锚定，再对漏网候选按「裸题名」锚定
    （正文丢了列表编号的场合），后者顺带把书签的权威编号回填进标题文本。
    """
    if not toc:
        return 0, 0
    by_norm, by_bare = _build_indexes(toc)
    locked = 0
    consumed = 0

    def _hit(cand, entries, from_bare=False):
        nonlocal locked, consumed
        for e in entries:
            if not e["used"] and abs(e["page"] - cand["page"]) <= 1:
                e["used"] = True
                consumed += 1
                cand["level"] = e["level"]
                cand["locked"] = True
                cand["source"] = "toc"
                if from_bare:
                    # 正文丢了编号：以书签题名为准回填，同级兄弟的编号形态保持一致
                    cand["text"] = re.sub(r"[\s　]+", " ", e["title"]).strip()
                locked += 1
                return True
        return False

    for cand in cands:
        if cand.get("locked"):
            continue
        key = normalize_title(cand["text"])
        entries = by_norm.get(key)
        if not entries:
            # 前缀锚定：书签与正文常有排版差（如「5.2.2 功能架构」对「5.2.2功能架构」）
            entries = next((es for k, es in by_norm.items()
                            if min(len(k), len(key)) >= 4 and (key.startswith(k) or k.startswith(key))), None)
        if entries and _hit(cand, entries):
            continue
        bare = strip_leading_number(cand["text"])
        if len(bare) >= 2:
            _hit(cand, by_bare.get(bare, []), from_bare=True)
    return locked, consumed


def infer_by_evidence(cands: List[Dict], body_size: float) -> None:
    """
    档 2（联合证据定层）：把未锁定候选切成「编号文法组」与「字号组」，两类组放进
    同一张榜单，按组内中位字号降序、同字号按文档先后，依次分配连续层级。

    旧实现把字号档叠在文法档之后（used_max 起跳），使无编号的章级标题一律沉到
    §/5.x 之下；层级高低本就该由整篇证据共同排序，两类组平权。
    点分深度族（multi dot）在同族内按段数下钻，占用的层数会抬高后续组的基线。
    """
    loose = [c for c in cands if not c.get("locked") and not c.get("demote")]
    if not loose:
        return
    groups: Dict[Tuple[str, object], List[Dict]] = {}
    for c in loose:
        fam = detect_family(c["text"])
        if fam:
            c["family"], c["dot_depth"] = fam
            key = ("fam", fam[0])
        else:
            key = ("font", round(c["size"] * 2) / 2)
        groups.setdefault(key, []).append(c)
    # 字号等同正文的组不构成标题证据，交收口降为正文
    groups = {k: v for k, v in groups.items()
              if k[0] == "fam" or _median([c["size"] for c in v]) > body_size * _FONT_GAIN}
    ranked = sorted(groups.items(),
                    key=lambda kv: (-_median([c["size"] for c in kv[1]]),
                                    min(c["seq"] for c in kv[1])))
    level = 0
    for (kind, tag), items in ranked:
        base = level + 1
        span = (sorted({c.get("dot_depth") or 1 for c in items})
                if kind == "fam" and tag in DEEP_FAMILIES else [1])
        depth_rank = {d: i for i, d in enumerate(span)}
        for c in items:
            extra = depth_rank.get(c.get("dot_depth") or 1, 0) if len(span) > 1 else 0
            c["level"] = min(6, base + extra)
            c["source"] = "family" if kind == "fam" else "font"
        level = base + len(span) - 1


def compress_levels(cands: List[Dict]) -> int:
    """整篇保序压缩：distinct 层级升序映射到 1..k，兄弟永不分层。返回改写数。"""
    lvls = sorted({c["level"] for c in cands if c.get("level")})
    if not lvls or lvls == list(range(1, len(lvls) + 1)):
        return 0
    remap = {v: i + 1 for i, v in enumerate(lvls)}
    fixes = 0
    for c in cands:
        if c.get("level") and remap[c["level"]] != c["level"]:
            c["level"] = remap[c["level"]]
            fixes += 1
    return fixes


def resolve(raw_blocks: List[Tuple], toc: List[Tuple[int, str, int]],
            body_size: float) -> Tuple[List[Tuple], Dict]:
    """
    入口：把 ("heading_candidate", meta) 块就地裁定为 ("heading", (lvl, text)) 或 ("text", text)。
    raw_blocks 元组形如 (y0, x0, kind, payload)；候选 payload 为 dict。
    """
    cands: List[Dict] = []
    for idx, item in enumerate(raw_blocks):
        if len(item) >= 4 and item[2] == "heading_candidate":
            meta = dict(item[3])
            meta["idx"] = idx
            meta.setdefault("seq", len(cands))
            meta.setdefault("locked", False)
            meta.setdefault("level", None)
            cands.append(meta)

    report = {"total": len(cands), "toc": len(toc), "locked": 0, "family": 0,
              "font": 0, "demoted": 0, "compressed": 0, "framed": 0, "coverage": 0.0,
              "unanchored": len(toc)}
    if not cands:
        return raw_blocks, report

    report["locked"], consumed = anchor_bookmarks(cands, toc)
    # 可信度以「书签被锚定的比例」衡量：候选里混着图注与落款，用候选数作分母会低估书签
    report["coverage"] = consumed / len(toc) if toc else 0.0

    # 版式熔断：落款行与页码 Folio 行不是章节；书签已锚定的权威层级不受影响
    for c in cands:
        if c.get("locked"):
            continue
        if is_colophon_line(c["text"]) or is_page_marker(c["text"]):
            c["level"] = None
            c["demote"] = True
    report["framed"] = demote_running_frames(cands)

    # 书签覆盖达标时，未锚定候选一律降为正文：垃圾标题由此消失，无需逐个打补丁
    if toc and report["coverage"] >= 0.6:
        for c in cands:
            if not c.get("locked"):
                c["level"] = None
                c["demote"] = True
    else:
        infer_by_evidence(cands, body_size)
        report["family"] = sum(1 for c in cands if c.get("source") == "family")
        report["font"] = sum(1 for c in cands if c.get("source") == "font")

    # 数字串伪标题无条件熔断（无论走哪一档）；书签已锚定的权威层级不受影响（与版式熔断同一豁免）
    for c in cands:
        if c.get("locked"):
            continue
        if is_spurious_numeric_heading(c["text"]):
            c["level"] = None
            c["demote"] = True

    resolved = [c for c in cands if c.get("level")]
    report["demoted"] = len(cands) - len(resolved)
    report["unanchored"] = max(0, len(toc) - consumed)
    # 书签权威时层级照抄作者声明（含作者自己的层级留白），压缩只服务于启发式档位
    if not (toc and report["coverage"] >= 0.6):
        report["compressed"] = compress_levels(resolved)

    out = list(raw_blocks)
    for c in cands:
        i = c["idx"]
        if c.get("level"):
            out[i] = (out[i][0], out[i][1], "heading", (c["level"], c["text"]))
        elif c.get("fallback") is not None:
            out[i] = c["fallback"]  # 回落原形态，list_block 的列表结构不丢
        else:
            out[i] = (out[i][0], out[i][1], "text", c["text"])
    return out, report
