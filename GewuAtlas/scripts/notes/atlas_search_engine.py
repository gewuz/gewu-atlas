#!/usr/bin/env python3
"""KEK-blinded full-featured search engine for GewuAtlas.

Ports 116-gwf-v2-R6 `search/{terms,bm25,cache,graph,formatter,cli}.py`:
- Query term protocol (`"phrase"`, multi-delimiter CJK/ASCII splitting, whole-CJK fallback);
- KEK AEAD-encrypted mtime incremental cache stored under `.local/state/search_cache/` (zero leakage into Vaults);
- Weighted BM25 (title=8, aliases=6, summary=3, tags=2, body=1, k1=1.2, b=0.75, Lucene IDF);
- Runtime link graph derivation: `Auth_1-Hop = ln(1 + InDegree)`, Top-50 `Cluster_2-Hop`, 1-hop `direct_links`,
  triad/tag `two_hop_bridges`, and 3-hop `graph_metrics`;
- Frontmatter-stripped, density-aware, natural-sentence-aligned KWIC snippets & nearest Markdown section headings;
- Access gating (`attic` archived penetration, `private` penetration, `workbench` skipping unless explicitly requested)
  and field projection (`--fields`).
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from facade.atlas_domain_scope import read_domain_manifest
from notes.atlas_note_engine import (
    BM25_B,
    BM25_K1,
    BM25_WEIGHTS,
    _public_note,
    _scan_vault_markdowns,
    _zone_for_rel,
)
from notes.atlas_yamlmini import FrontmatterError, extract_frontmatter, extract_wikilinks
from storage.note_kek_store import KekNoteRegistry
from storage.vault_kek_store import (
    VaultStoreError,
    _atomic_write_0600,
    _check_no_symlink,
    aead_xchacha20_poly1305_decrypt,
    aead_xchacha20_poly1305_encrypt,
    hkdf_sha256,
)

_CACHE_MAGIC = b"GWSC2"
_CACHE_VERSION = 2
_NONCE_LEN = 24
_TAG_LEN = 16

_SEP = re.compile(r"[,;\s，；、]+")
_PHRASE = re.compile(r'"([^"]+)"')
_FM_LINE = re.compile(r"(?m)^---[ \t]*$")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_SENT_END = "。！？；!?;"
_REF_SECTION_SPLIT_RE = re.compile(r"(?m)^#{2,3}\s*(?:参考来源与知识库索引|参考来源|溯源与参考来源|知识库关联卡片)\b")
_REF_HEADING_PREFIX_RE = re.compile(
    r"^(?:(?:\d+[\.\、\s]+|[一二三四五六七八九十]+[\、\.\s]+))?(?:参考来源与知识库索引|参考来源|溯源与参考来源|知识库关联卡片)\b"
)
_WIKILINK_PIPE_TARGET_RE = re.compile(r"\[\[[^\]|]+\|([^\]]+)\]\]")


def _split_reference_footer(body: str) -> Tuple[str, str]:
    txt = body or ""
    m = _REF_SECTION_SPLIT_RE.search(txt)
    if not m:
        return txt, ""
    return txt[: m.start()].rstrip(), txt[m.start() :]


def _clean_reference_footer_for_bm25(footer: str) -> str:
    if not footer:
        return ""
    return _WIKILINK_PIPE_TARGET_RE.sub(r"\1", footer)


def _is_reference_heading(heading: str) -> bool:
    return bool(_REF_HEADING_PREFIX_RE.match((heading or "").strip()))

ALLOWED_FIELDS = (
    "vault_ref",
    "vault_title",
    "note_ref",
    "title",
    "summary",
    "zone",
    "doc_type",
    "type",
    "status",
    "stage",
    "is_private",
    "score",
    "score_raw",
    "bm25_score",
    "section",
    "snippet",
    "passage",
    "passage_truncated",
    "in_links_top3",
    "out_links_top3",
    "direct_links",
    "two_hop_bridges",
    "graph_metrics",
    "tags",
    "aliases",
    "map",
    "updated",
    "project_id",
)

ALLOWED_LINK_FIELDS = (
    "note_ref",
    "title",
    "type",
    "status",
    "stage",
    "summary",
    "tags",
    "aliases",
    "updated",
)
_DEFAULT_LINK_FIELDS = ("note_ref", "title", "type", "summary")


def _meaningful(t: str) -> bool:
    return any(c.isalnum() for c in t)


def split_terms(raw: str) -> List[str]:
    quoted: List[str] = []

    def _keep(m: re.Match) -> str:
        t = m.group(1).strip()
        if t and _meaningful(t):
            quoted.append(t)
        return " "

    rest = _PHRASE.sub(_keep, raw)
    tail = [t for t in (s.strip() for s in _SEP.split(rest)) if t and _meaningful(t)]
    return quoted + tail


def build_terms(query: str, terms_arg: Optional[str] = None) -> List[str]:
    if terms_arg:
        explicit = split_terms(terms_arg)
        if explicit:
            return explicit
    if _SEP.search(query):
        return split_terms(query)
    q = query.strip()
    return [q] if q and _meaningful(q) else []


def normalize_fields(fields_arg: Optional[str]) -> Optional[List[str]]:
    if not fields_arg or not fields_arg.strip():
        return None
    wanted = [f.strip() for f in fields_arg.split(",") if f.strip()]
    wanted = ["snippet" if f == "kwic" else f for f in wanted]
    bad = [f for f in wanted if f not in ALLOWED_FIELDS]
    if bad:
        raise VaultStoreError("E_PARAM_INVALID", f"--fields 包含未定义字段: {', '.join(bad)}", exit_code=1)
    return wanted


def normalize_link_fields(link_fields_arg: Optional[str]) -> Optional[List[str]]:
    if not link_fields_arg or not link_fields_arg.strip():
        return None
    wanted = [f.strip() for f in link_fields_arg.split(",") if f.strip()]
    bad = [f for f in wanted if f not in ALLOWED_LINK_FIELDS]
    if bad:
        raise VaultStoreError("E_PARAM_INVALID", f"--link_fields 包含未定义字段: {', '.join(bad)}", exit_code=1)
    return wanted


def _clip(text: Any, width: int) -> str:
    flat = " ".join(str(text or "").split())
    if width > 0 and len(flat) > width:
        return flat[: max(1, width - 1)] + "…"
    return flat


def _cache_key(kek: bytes, app_id: str, vault_ref: str) -> bytes:
    return hkdf_sha256(kek, b"gewu-atlas-search-cache/v1", f"{app_id}|{vault_ref}".encode("utf-8"))


# Opt-in process RAM hot-cache for BM25 corpus (enabled ONLY by AtlasHotKernel).
_HOT_SEARCH_ENABLED = False
_HOT_SEARCH_MAX_ENTRIES = 8
_HOT_SEARCH_CACHE: Dict[str, Tuple[int, int, bytes, Dict[str, Dict[str, Any]]]] = {}
_HOT_SEARCH_HITS = 0
_HOT_SEARCH_MISSES = 0


def set_hot_cache_enabled(enabled: bool, *, max_entries: int = 8) -> None:
    global _HOT_SEARCH_ENABLED, _HOT_SEARCH_MAX_ENTRIES
    _HOT_SEARCH_ENABLED = bool(enabled)
    _HOT_SEARCH_MAX_ENTRIES = max(1, int(max_entries))
    if not _HOT_SEARCH_ENABLED:
        clear_hot_cache()


def clear_hot_cache() -> None:
    global _HOT_SEARCH_HITS, _HOT_SEARCH_MISSES
    _HOT_SEARCH_CACHE.clear()
    _HOT_SEARCH_HITS = 0
    _HOT_SEARCH_MISSES = 0


def get_hot_cache_stats() -> Dict[str, Any]:
    return {
        "enabled": _HOT_SEARCH_ENABLED,
        "cached_vaults": len(_HOT_SEARCH_CACHE),
        "hits": _HOT_SEARCH_HITS,
        "misses": _HOT_SEARCH_MISSES,
    }


def _hot_search_put(c_key: str, mtime_ns: int, size: int, k_dig: bytes, docs: Dict[str, Dict[str, Any]]) -> None:
    if c_key in _HOT_SEARCH_CACHE:
        _HOT_SEARCH_CACHE.pop(c_key, None)
    elif len(_HOT_SEARCH_CACHE) >= _HOT_SEARCH_MAX_ENTRIES:
        oldest = next(iter(_HOT_SEARCH_CACHE))
        _HOT_SEARCH_CACHE.pop(oldest, None)
    _HOT_SEARCH_CACHE[c_key] = (mtime_ns, size, k_dig, copy.deepcopy(docs))


def _load_cache(cache_file: Path, key: bytes) -> Dict[str, Dict[str, Any]]:
    global _HOT_SEARCH_HITS, _HOT_SEARCH_MISSES
    if not cache_file.exists():
        if _HOT_SEARCH_ENABLED:
            _HOT_SEARCH_CACHE.pop(str(cache_file), None)
        return {}
    _check_no_symlink(cache_file)
    c_key = str(cache_file)
    k_dig = b""
    st_mtime = -1
    st_size = -1
    if _HOT_SEARCH_ENABLED:
        import hashlib as _hashlib

        k_dig = _hashlib.sha256(key).digest()
        try:
            st = cache_file.stat()
            st_mtime = int(st.st_mtime_ns)
            st_size = int(st.st_size)
            hit = _HOT_SEARCH_CACHE.get(c_key)
            if hit is not None and hit[0] == st_mtime and hit[1] == st_size and hit[2] == k_dig:
                _HOT_SEARCH_HITS += 1
                _hot_search_put(c_key, st_mtime, st_size, k_dig, hit[3])
                return copy.deepcopy(hit[3])
            _HOT_SEARCH_MISSES += 1
        except OSError:
            pass
    try:
        blob = cache_file.read_bytes()
        if len(blob) < len(_CACHE_MAGIC) + _NONCE_LEN + _TAG_LEN + 1 or blob[:5] != _CACHE_MAGIC:
            return {}
        nonce = blob[5 : 5 + _NONCE_LEN]
        ct = blob[5 + _NONCE_LEN : -_TAG_LEN]
        tag = blob[-_TAG_LEN:]
        pt = aead_xchacha20_poly1305_decrypt(key, nonce, ct, b"search-cache", tag)
        doc = json.loads(zlib.decompress(pt).decode("utf-8"))
        if isinstance(doc, dict) and doc.get("v") == _CACHE_VERSION and isinstance(doc.get("docs"), dict):
            docs = doc["docs"]
            if _HOT_SEARCH_ENABLED and st_mtime >= 0:
                _hot_search_put(c_key, st_mtime, st_size, k_dig, docs)
            return docs
    except Exception:
        return {}
    return {}


def _save_cache(cache_file: Path, key: bytes, docs: Dict[str, Dict[str, Any]]) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    nonce = os.urandom(_NONCE_LEN)
    payload = zlib.compress(
        json.dumps({"v": _CACHE_VERSION, "docs": docs}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        level=1,
    )
    ct, tag = aead_xchacha20_poly1305_encrypt(key, nonce, payload, b"search-cache")
    _atomic_write_0600(cache_file, _CACHE_MAGIC + nonce + ct + tag)
    if _HOT_SEARCH_ENABLED:
        import hashlib as _hashlib

        try:
            st = cache_file.stat()
            _hot_search_put(str(cache_file), int(st.st_mtime_ns), int(st.st_size), _hashlib.sha256(key).digest(), docs)
        except OSError:
            pass


def _reconcile_docs(
    *,
    vault_dir: Path,
    rels: List[str],
    cache_file: Optional[Path],
    key: Optional[bytes],
    no_cache: bool = False,
) -> Dict[str, Dict[str, Any]]:
    cached = _load_cache(cache_file, key) if (cache_file and key) else {}
    rel_set = set(rels)
    next_cache: Dict[str, Dict[str, Any]] = {}
    dirty = len(cached) != len(rels)

    for rel in rels:
        fp = vault_dir / rel
        if not fp.is_file():
            continue
        st = fp.stat()
        prev = cached.get(rel)
        if (
            prev
            and int(prev.get("mtime_ns", -1)) == int(st.st_mtime_ns)
            and int(prev.get("ctime_ns", -1)) == int(st.st_ctime_ns)
            and int(prev.get("size", -1)) == int(st.st_size)
            and int(prev.get("ino", -1)) == int(st.st_ino)
        ):
            next_cache[rel] = prev
            continue
        dirty = True
        raw = fp.read_text(encoding="utf-8", errors="replace")
        try:
            meta, body, has_fm = extract_frontmatter(raw)
        except FrontmatterError:
            meta, body, has_fm = {"doc_type": "invalid", "title": fp.stem}, raw, False
        aliases = [str(a).strip() for a in (meta.get("aliases") or []) if isinstance(a, str) and str(a).strip()]
        tags = [str(t).strip() for t in (meta.get("tags") or []) if isinstance(t, str) and str(t).strip()]
        raw_map = meta.get("map") if meta.get("map") is not None else meta.get("moc")
        if isinstance(raw_map, str):
            map_vals = [raw_map.strip()] if raw_map.strip() else []
        elif isinstance(raw_map, list):
            map_vals = [str(x).strip() for x in raw_map if str(x).strip()]
        else:
            map_vals = []
        # Scan full raw text (frontmatter map/provenance + body, code-stripped) so MOC InDegree reflects map backlinks.
        out_links = extract_wikilinks(raw)
        next_cache[rel] = {
            "mtime_ns": int(st.st_mtime_ns),
            "ctime_ns": int(st.st_ctime_ns),
            "size": int(st.st_size),
            "ino": int(st.st_ino),
            "has_fm": bool(has_fm),
            "meta": {
                "doc_type": str(meta.get("doc_type") or ""),
                "type": str(meta.get("type") or ""),
                "title": str(meta.get("title") or fp.stem),
                "summary": str(meta.get("summary") or ""),
                "status": str(meta.get("status") or ""),
                "stage": str(meta.get("stage") or ""),
                "private": bool(meta.get("private") is True),
                "project_id": str(meta.get("project_id") or ""),
                "tags": tags,
                "aliases": aliases,
                "map": map_vals,
                "updated": str(meta.get("updated") or ""),
            },
            "out_links": out_links,
            "body": body,
            "raw": raw,
        }

    for k in cached:
        if k not in rel_set:
            dirty = True
            break

    if dirty and cache_file and key and not no_cache:
        _save_cache(cache_file, key, next_cache)
    return next_cache


def _normalize_target(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    return base[:-3] if base.endswith(".md") else base


def _build_resolver(docs: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    resolver: Dict[str, str] = {}
    dup: Set[str] = set()

    def add(name: str, rel: str) -> None:
        if not name or not isinstance(name, str):
            return
        if name in resolver:
            if resolver[name] != rel:
                dup.add(name)
            return
        resolver[name] = rel

    for rel in docs:
        add(Path(rel).stem, rel)
    for rel, d in docs.items():
        if d["meta"].get("status") == "active":
            for a in d["meta"].get("aliases") or []:
                add(a, rel)
    for name in dup:
        resolver.pop(name, None)
    return resolver


def _resolve_name(resolver: Dict[str, str], name: str) -> Optional[str]:
    return resolver.get(name) or resolver.get(_normalize_target(name))


def _build_in_links(
    docs: Dict[str, Dict[str, Any]],
    resolver: Dict[str, str],
    is_accessible_fn: Callable[[str, Dict[str, Any]], bool],
) -> Dict[str, Set[str]]:
    in_links: Dict[str, Set[str]] = {rel: set() for rel in docs}
    for rel, d in docs.items():
        st = str(d["meta"].get("status") or "").lower()
        if st and st != "active":
            continue
        if not is_accessible_fn(rel, d):
            continue
        for name in d.get("out_links") or []:
            tgt = _resolve_name(resolver, name)
            if tgt and tgt != rel and tgt in in_links:
                in_links[tgt].add(rel)
    return in_links


def _one_hop(rel: str, docs: Dict[str, Dict[str, Any]], in_links: Dict[str, Set[str]], resolver: Dict[str, str]) -> Set[str]:
    d = docs.get(rel)
    if d is None:
        return set()
    out: Set[str] = set()
    for name in d.get("out_links") or []:
        tgt = _resolve_name(resolver, name)
        if tgt and tgt in docs:
            out.add(tgt)
    nb = out | in_links.get(rel, set())
    nb.discard(rel)
    return nb


def _cluster_affinity(rel: str, docs: Dict[str, Dict[str, Any]], in_links: Dict[str, Set[str]], resolver: Dict[str, str], cand_set: Set[str]) -> float:
    p = 0
    for n in _one_hop(rel, docs, in_links, resolver):
        for t in _one_hop(n, docs, in_links, resolver):
            if t != rel and t in cand_set:
                p += 1
    return math.log(1.0 + p)


def _two_hop_bridges(
    rel: str,
    docs: Dict[str, Dict[str, Any]],
    in_links: Dict[str, Set[str]],
    resolver: Dict[str, str],
    is_accessible_fn: Callable[[str, Dict[str, Any]], bool],
    limit: int = 3,
) -> List[Tuple[str, float]]:
    hit = docs.get(rel)
    if hit is None:
        return []
    one = _one_hop(rel, docs, in_links, resolver)
    hit_tags = {t for t in (hit["meta"].get("tags") or []) if isinstance(t, str)}
    cand: Dict[str, float] = {}
    for n in one:
        for t in _one_hop(n, docs, in_links, resolver):
            if t == rel or t in one:
                continue
            td = docs.get(t)
            if not td or not is_accessible_fn(t, td):
                continue
            st = str(td["meta"].get("status") or "").lower()
            if st and st != "active":
                continue
            overlap = len(hit_tags & {x for x in (td["meta"].get("tags") or []) if isinstance(x, str)})
            cand[t] = cand.get(t, 0.0) + 1.0 + overlap * 0.5
    ranked = sorted(cand.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:limit]


def _three_hop_metrics(rel: str, docs: Dict[str, Dict[str, Any]], in_links: Dict[str, Set[str]], resolver: Dict[str, str]) -> Dict[str, Any]:
    d = docs.get(rel)
    if d is None:
        return {"in_degree": 0, "out_degree": 0, "authority": 0.0, "reach_3hop": 0}
    outs: Set[str] = set()
    for name in d.get("out_links") or []:
        tgt = _resolve_name(resolver, name)
        if tgt and tgt != rel and tgt in docs:
            outs.add(tgt)
    ind = len(in_links.get(rel, set()))
    seen = {rel}
    frontier = {rel}
    for _ in range(3):
        nxt: Set[str] = set()
        for n in frontier:
            nxt |= _one_hop(n, docs, in_links, resolver)
        nxt -= seen
        if not nxt:
            break
        seen |= nxt
        frontier = nxt
    return {
        "in_degree": ind,
        "out_degree": len(outs),
        "authority": round(math.log(1.0 + ind), 4),
        "reach_3hop": len(seen) - 1,
    }


def _frontmatter_end(text: str) -> int:
    if not text.startswith("---"):
        return 0
    m = _FM_LINE.search(text)
    if m is None or m.start() != 0:
        return 0
    m2 = _FM_LINE.search(text[m.end() :])
    if m2 is None:
        return 0
    return m.end() + m2.end()


def _density_pick(lowered: str, terms_l: List[str], lo: int, hi: int) -> int:
    best_key = None
    best_span = None
    s = lo
    while s < hi:
        e = lowered.find("\n\n", s, hi)
        e = hi if e < 0 else e
        distinct = total = 0
        for t in terms_l:
            c = lowered.count(t, s, e)
            if c:
                distinct += 1
                total += c
        if distinct and (best_key is None or (distinct, total) > best_key):
            best_key = (distinct, total)
            best_span = (s, e)
        s = e + 2
    if best_span is None:
        return -1
    pos = -1
    for t in terms_l:
        p = lowered.find(t, best_span[0], best_span[1])
        if p >= 0 and (pos < 0 or p < pos):
            pos = p
    return pos


def _sent_end_at(text: str, i: int) -> bool:
    ch = text[i]
    if ch in _SENT_END:
        return True
    return ch == "." and (i + 1 >= len(text) or text[i + 1] in " \t\r\n\"')")


def _align_left(text: str, lo: int, pos: int) -> int:
    i = text.rfind("\n\n", lo, pos)
    if i >= 0:
        return i + 2
    i = text.rfind("\n", lo, pos)
    if i >= 0:
        return i + 1
    j = pos - 1
    while j >= lo:
        if _sent_end_at(text, j):
            return j + 1
        j -= 1
    return lo


def _align_right(text: str, pos: int, hi: int) -> int:
    i = text.find("\n\n", pos, hi)
    if i >= 0:
        return i
    i = text.find("\n", pos, hi)
    if i >= 0:
        return i
    for j in range(pos, hi):
        if _sent_end_at(text, j):
            return j + 1
    return min(hi, len(text))


def _section_at_offset(raw_text: str, fm_end: int, offset: int) -> str:
    heading = ""
    fence = None
    pos = 0
    for line in raw_text.splitlines(keepends=True):
        nxt = pos + len(line)
        if pos >= offset:
            break
        if pos >= fm_end:
            fm = _FENCE_RE.match(line)
            if fm:
                mk = fm.group(1)
                if fence is None:
                    fence = mk[0]
                elif mk[0] == fence and not line[fm.end() :].strip():
                    fence = None
            elif fence is None:
                hm = _HEADING_RE.match(line.rstrip("\r\n"))
                if hm:
                    heading = hm.group(1).strip()
        pos = nxt
    return heading


def _first_hit(lowered: str, terms_l: List[str], lo: int, hi: int) -> int:
    pos = -1
    for t in terms_l:
        p = lowered.find(t, lo, hi)
        if p >= 0 and (pos < 0 or p < pos):
            pos = p
    return pos


def extract_kwic_and_section(raw_text: str, terms: List[str], width: int = 160) -> Tuple[str, str]:
    eff_width = max(40, int(width or 160))
    fm_end = _frontmatter_end(raw_text)
    lowered = raw_text.lower()
    terms_l = list(dict.fromkeys(t.lower() for t in terms if t))
    body_start = fm_end
    while body_start < len(raw_text) and raw_text[body_start] in "\r\n":
        body_start += 1

    ref_m = _REF_SECTION_SPLIT_RE.search(raw_text, body_start)
    core_end = ref_m.start() if (ref_m and ref_m.start() > body_start) else len(raw_text)

    pos = _first_hit(lowered, terms_l, body_start, core_end)
    in_core = pos >= 0
    if in_core and len(terms_l) > 1:
        p2 = _density_pick(lowered, terms_l, body_start, core_end)
        if p2 >= 0:
            pos = p2
    if not in_core:
        pos = _first_hit(lowered, terms_l, core_end, len(raw_text))
        if pos >= 0 and len(terms_l) > 1:
            p2 = _density_pick(lowered, terms_l, core_end, len(raw_text))
            if p2 >= 0:
                pos = p2
    if pos < 0:
        pos = _first_hit(lowered, terms_l, 0, len(raw_text))
    if pos < 0:
        pos = body_start if body_start < len(raw_text) else 0

    half = max(20, eff_width // 2)
    lo = max(body_start if pos >= body_start else 0, pos - half)
    hi_bound = core_end if (in_core and pos < core_end) else len(raw_text)
    hi = min(hi_bound, pos + half)
    s_idx = _align_left(raw_text, lo, pos)
    e_idx = _align_right(raw_text, pos, hi)
    snip = " ".join(raw_text[s_idx:e_idx].split())
    if not snip:
        snip = " ".join(raw_text[body_start:core_end].split())[:eff_width]
    if len(snip) > eff_width + 20:
        snip = snip[:eff_width].rstrip() + "…"
    section = _section_at_offset(raw_text, fm_end, pos)
    return snip, section


def extract_passage_and_section(
    raw_text: str,
    terms: List[str],
    *,
    doc_type: str = "",
    max_chars: int = 1200,
) -> Tuple[str, str, bool]:
    """Extract doc_type-aware passage around the densest query hit:
    - atomic/slip -> full note body excluding trailing reference index chapter (cap 4000 chars)
    - moc/artifact -> complete matched Markdown section, prioritizing prose chapters over reference index (cap 2000 chars)
    - other docs (library/slice/workbench/etc.) -> complete matched paragraph (cap max_chars, default 1200)
    """
    fm_end = _frontmatter_end(raw_text)
    body = raw_text[fm_end:].strip()
    if not body:
        return "", "", False

    dt = (doc_type or "").strip().lower()
    terms_l = list(dict.fromkeys(t.lower() for t in terms if t))

    # 1. Atomic & Slip: return full core body (excluding trailing reference index chapter) so definition/mechanism/boundary stay clean
    if dt in ("atomic", "slip"):
        core_body, _ = _split_reference_footer(body)
        eff_body = core_body.strip() or body
        cap = max(2000, int(max_chars or 1200), 4000)
        if len(eff_body) <= cap:
            return eff_body, "", False
        cut = eff_body[:cap].rfind("\n\n")
        chunk = (eff_body[:cut] if cut > cap // 2 else eff_body[:cap]).rstrip() + "…"
        return chunk, "", True

    sections: List[Tuple[str, str]] = []
    cur_heading = ""
    cur_lines: List[str] = []
    fence: Optional[str] = None
    for line in body.splitlines():
        fm = _FENCE_RE.match(line)
        if fm:
            mk = fm.group(1)
            if fence is None:
                fence = mk[0]
            elif mk[0] == fence and not line[fm.end() :].strip():
                fence = None
            cur_lines.append(line)
            continue
        if fence is None:
            hm = _HEADING_RE.match(line)
            if hm:
                if cur_lines:
                    sec_txt = "\n".join(cur_lines).strip()
                    if sec_txt:
                        sections.append((cur_heading, sec_txt))
                cur_heading = hm.group(1).strip()
                cur_lines = [line]
                continue
        cur_lines.append(line)
    if cur_lines:
        sec_txt = "\n".join(cur_lines).strip()
        if sec_txt:
            sections.append((cur_heading, sec_txt))
    if not sections:
        sections = [("", body)]

    best_idx = 0
    best_key = (-1, -1, -1)
    for idx, (hd, txt) in enumerate(sections):
        low = txt.lower()
        distinct = sum(1 for t in terms_l if t in low)
        total = sum(low.count(t) for t in terms_l)
        non_ref_hit = 1 if (distinct > 0 and not _is_reference_heading(hd)) else 0
        cand_key = (non_ref_hit, distinct, total)
        if cand_key > best_key:
            best_key = cand_key
            best_idx = idx

    heading, sec_txt = sections[best_idx]

    # 2. MOC & Artifact: return complete matched section (cap 2000 chars)
    if dt in ("moc", "artifact"):
        sec_cap = max(2000, int(max_chars or 1200))
        if len(sec_txt) <= sec_cap:
            return sec_txt, heading, False
        paras = [p.strip() for p in re.split(r"\n\s*\n", sec_txt) if p.strip()]
        if not paras:
            return sec_txt[:sec_cap].rstrip() + "…", heading, True
        p_best = max(
            range(len(paras)),
            key=lambda i: (sum(1 for t in terms_l if t in paras[i].lower()), sum(paras[i].lower().count(t) for t in terms_l)),
        )
        if len(paras[p_best]) > sec_cap:
            return paras[p_best][:sec_cap].rstrip() + "…", heading, True
        lo_i = hi_i = p_best
        cur_len = len(paras[p_best])
        while True:
            grew = False
            if hi_i + 1 < len(paras) and cur_len + 2 + len(paras[hi_i + 1]) <= sec_cap:
                hi_i += 1
                cur_len += 2 + len(paras[hi_i])
                grew = True
            if lo_i - 1 >= 0 and cur_len + 2 + len(paras[lo_i - 1]) <= sec_cap:
                lo_i -= 1
                cur_len += 2 + len(paras[lo_i])
                grew = True
            if not grew:
                break
        return "\n\n".join(paras[lo_i : hi_i + 1]), heading, (lo_i > 0 or hi_i < len(paras) - 1)

    # 3. Other documents (library / slice / workbench / journal): return the complete matched paragraph
    eff_max = max(200, int(max_chars or 1200))
    paras = [p.strip() for p in re.split(r"\n\s*\n", sec_txt) if p.strip()]
    content_indices = [i for i, p in enumerate(paras) if not _HEADING_RE.match(p.strip()) or "\n" in p]
    cand_indices = content_indices or list(range(len(paras)))
    if not cand_indices:
        return sec_txt[:eff_max].rstrip(), heading, len(sec_txt) > eff_max
    p_best = max(
        cand_indices,
        key=lambda i: (sum(1 for t in terms_l if t in paras[i].lower()), sum(paras[i].lower().count(t) for t in terms_l)),
    )
    p_txt = paras[p_best]
    if len(p_txt) <= eff_max:
        return p_txt, heading, False
    p_low = p_txt.lower()
    hit_pos = _first_hit(p_low, terms_l, 0, len(p_txt))
    if hit_pos < 0:
        hit_pos = 0
    lo = max(0, hit_pos - eff_max // 2)
    hi = min(len(p_txt), lo + eff_max)
    s_i = _align_left(p_txt, lo, hit_pos)
    e_i = _align_right(p_txt, hit_pos, hi)
    chunk = p_txt[s_i:e_i].strip()
    if len(chunk) > eff_max:
        chunk = chunk[:eff_max].rstrip() + "…"
    return chunk, heading, True


def _ranked_directed_links(
    rel: str,
    docs: Dict[str, Dict[str, Any]],
    in_links: Dict[str, Set[str]],
    resolver: Dict[str, str],
    is_accessible_fn: Callable[[str, Dict[str, Any]], bool],
    score_norm_map: Dict[str, float],
    mapping: Dict[str, str],
    limit: int = 3,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return Top-N incoming and Top-N outgoing links ranked by query score then graph authority."""
    d = docs.get(rel)
    if d is None:
        return [], []

    def _rank_pool(pool: Set[str]) -> List[Dict[str, Any]]:
        scored: List[Tuple[float, int, str]] = []
        for nb in pool:
            if nb == rel or nb not in docs:
                continue
            nd = docs[nb]
            if not is_accessible_fn(nb, nd):
                continue
            in_deg = len(in_links.get(nb, set()))
            if nb in score_norm_map:
                eff_s = round(float(score_norm_map[nb]), 2)
            else:
                eff_s = round(min(49.0, math.log(1.0 + in_deg) * 15.0), 2)
            scored.append((eff_s, in_deg, nb))
        scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
        out_list: List[Dict[str, Any]] = []
        for eff_s, _deg, nb in scored[:limit]:
            nm = docs[nb]["meta"]
            out_list.append(
                {
                    "note_ref": mapping.get(nb),
                    "title": nm.get("title") or Path(nb).stem,
                    "doc_type": nm.get("doc_type") or nm.get("type") or "",
                    "score": eff_s,
                    "updated": nm.get("updated") or "",
                }
            )
        return out_list

    in_pool = set(in_links.get(rel, set()))
    out_pool: Set[str] = set()
    for name in d.get("out_links") or []:
        tgt = _resolve_name(resolver, name)
        if tgt and tgt != rel and tgt in docs:
            out_pool.add(tgt)
    return _rank_pool(in_pool), _rank_pool(out_pool)


def note_search(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    query: str,
    terms_arg: Optional[str] = None,
    zone_filter: Optional[str] = None,
    project_id_filter: Optional[str] = None,
    doc_type_filter: Optional[str] = None,
    epistemic_type_filter: Optional[str] = None,
    status_filter: Optional[str] = None,
    exclude_zones: Optional[List[str]] = None,
    exclude_project_ids: Optional[List[str]] = None,
    include_archived: bool = False,
    include_private: bool = False,
    exclude_pending: bool = False,
    exclude_draft: bool = False,
    mode: Optional[str] = None,
    deep: bool = False,
    hops: Optional[int] = None,
    boost: str = "hybrid",
    alpha: float = 0.15,
    beta: float = 0.08,
    min_score: int = 0,
    snippet_width: int = 160,
    passage_max_chars: int = 1200,
    summary_width: int = 0,
    no_snippet: bool = False,
    no_cache: bool = False,
    with_content: bool = False,
    fields_arg: Optional[str] = None,
    link_fields_arg: Optional[str] = None,
    cache_dir: Optional[Path] = None,
    top_k: int = 10,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    q = (query or "").strip()
    if not q and not (terms_arg and terms_arg.strip()):
        raise VaultStoreError("E_PARAM_MISSING", "note-search 需要 --query", exit_code=1)

    raw_mode = (mode or "").strip().lower()
    if raw_mode and raw_mode not in ("locate", "passage", "deep"):
        raise VaultStoreError("E_PARAM_INVALID", f"--mode 仅支持 locate|passage|deep，收到: {mode}", exit_code=1)
    eff_mode = raw_mode or ("deep" if deep else "passage")
    legacy_override = (
        (hops is not None)
        or bool(deep)
        or bool(fields_arg)
        or bool(link_fields_arg)
        or bool(no_snippet)
        or bool(with_content)
    )

    boost_mode = (boost or "hybrid").strip().lower()
    if boost_mode not in ("hybrid", "text"):
        raise VaultStoreError("E_PARAM_INVALID", f"--boost 仅支持 hybrid|text，收到: {boost}", exit_code=1)
    if alpha < 0 or beta < 0:
        raise VaultStoreError("E_PARAM_INVALID", "--alpha 与 --beta 不能为负数", exit_code=1)
    if hops is not None and hops not in (0, 1, 2, 3):
        raise VaultStoreError("E_PARAM_INVALID", f"--hops 仅支持 0|1|2|3，收到: {hops}", exit_code=1)
    if min_score < 0 or min_score > 100:
        raise VaultStoreError("E_PARAM_INVALID", "--min_score 必须在 0 到 100 之间", exit_code=1)
    eff_hops = int(hops) if hops is not None else (3 if deep else (1 if (legacy_override or eff_mode == "deep") else 0))

    terms = build_terms(q, terms_arg)
    if not terms:
        return [], []
    terms_l = list(dict.fromkeys(t.lower() for t in terms if t))
    wanted_fields = normalize_fields(fields_arg)
    wanted_link_fields = normalize_link_fields(link_fields_arg)

    from storage.note_index_cache import scandir_vault_markdowns

    disk_stats = scandir_vault_markdowns(vault_dir)
    rels = sorted(disk_stats.keys())
    mapping = note_reg.sync_paths(vault_ref, rels)

    cfile = (cache_dir / f"{vault_ref}.sc1") if cache_dir else None
    ckey = _cache_key(kek, app_id, vault_ref)
    docs = _reconcile_docs(vault_dir=vault_dir, rels=rels, cache_file=cfile, key=ckey, no_cache=no_cache)
    if not docs:
        return [], rels

    # Precompute private projects
    private_projects: Set[str] = set()
    pdir = vault_dir / "30-Projects"
    if pdir.is_dir():
        for pd in pdir.iterdir():
            if pd.is_dir() and not pd.name.startswith("."):
                m = read_domain_manifest(pd, pd.name)
                if m.get("private") is True:
                    private_projects.add(pd.name)

    z_want = (zone_filter or "").strip().lower() or None
    dt_want = (doc_type_filter or "").strip().lower() or None
    et_want = (epistemic_type_filter or "").strip().upper() or None
    st_want = (status_filter or "").strip().lower() or None
    pid_want = (project_id_filter or "").strip() or None
    ex_zones = {z.strip().lower() for z in (exclude_zones or []) if z and z.strip()}
    ex_pids = {p.strip() for p in (exclude_project_ids or []) if p and p.strip()}

    def _is_accessible(rel: str, d: Dict[str, Any]) -> bool:
        zone = _zone_for_rel(rel)
        if zone == "workbench" and z_want != "workbench" and dt_want != "workbench":
            return False
        if zone == "attic" and not include_archived:
            return False
        st = str(d["meta"].get("status") or "").lower()
        stg = str(d["meta"].get("stage") or "").lower()
        if st == "deprecated" and not include_archived:
            return False
        if st == "pending" and exclude_pending and st_want != "pending":
            return False
        if stg == "draft" and exclude_draft and z_want != "workbench":
            return False
        if not include_private:
            if d["meta"].get("private") is True or zone == "journal" or rel.startswith("05-Journal/"):
                return False
            parts = Path(rel).parts
            if len(parts) >= 2 and parts[0] == "30-Projects" and parts[1] in private_projects:
                return False
        return True

    scoped: Dict[str, Dict[str, Any]] = {}
    for rel, d in docs.items():
        if not _is_accessible(rel, d):
            continue
        zone = _zone_for_rel(rel)
        if ex_zones and zone in ex_zones:
            continue
        if ex_pids:
            parts = Path(rel).parts
            rel_pid = parts[1] if (len(parts) >= 2 and parts[0] == "30-Projects") else str(d["meta"].get("project_id") or "")
            if rel_pid and rel_pid in ex_pids:
                continue
        if z_want and zone != z_want:
            continue
        if dt_want and str(d["meta"].get("doc_type") or "").lower() != dt_want:
            continue
        if et_want and str(d["meta"].get("type") or "").upper() != et_want:
            continue
        if st_want and str(d["meta"].get("status") or "").lower() != st_want:
            continue
        if pid_want and str(d["meta"].get("project_id") or "") != pid_want:
            continue
        scoped[rel] = d

    if not scoped:
        return [], rels

    # Weighted BM25 over scoped pool (excluding trailing reference index chapter from dl to avoid slice-slug dilution)
    fields_map: Dict[str, Dict[str, str]] = {}
    ref_footer_map: Dict[str, str] = {}
    lengths: Dict[str, int] = {}
    for rel, d in scoped.items():
        m = d["meta"]
        raw_body = str(d.get("body") or "")
        core_body, ref_footer = _split_reference_footer(raw_body)
        fm = {
            "title": str(m.get("title") or Path(rel).stem),
            "aliases": " ".join(m.get("aliases") or []),
            "summary": str(m.get("summary") or ""),
            "tags": " ".join(m.get("tags") or []),
            "body": core_body if core_body.strip() else raw_body,
        }
        fields_map[rel] = fm
        if ref_footer:
            ref_footer_map[rel] = _clean_reference_footer_for_bm25(ref_footer).lower()
        lengths[rel] = sum(len(v) for v in fm.values())

    n_docs = len(scoped)
    avgdl = (sum(lengths.values()) / n_docs) or 1.0
    df = {t: 0 for t in terms_l}
    tf_map: Dict[str, Dict[str, float]] = {}

    for rel, fm in fields_map.items():
        lowered = {k: v.lower() for k, v in fm.items()}
        ref_low = ref_footer_map.get(rel, "")
        tf: Dict[str, float] = {}
        for t in terms_l:
            freq = sum(BM25_WEIGHTS[k] * txt.count(t) for k, txt in lowered.items() if txt)
            if ref_low:
                freq += 0.2 * ref_low.count(t)
            if freq > 0:
                tf[t] = freq
                df[t] += 1
        if tf:
            tf_map[rel] = tf

    if not tf_map:
        return [], rels

    bm25_scores: Dict[str, float] = {}
    for rel, tf in tf_map.items():
        dl = lengths[rel]
        s = 0.0
        for t, f in tf.items():
            idf = math.log(1.0 + (n_docs - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * (f * (BM25_K1 + 1.0)) / (f + BM25_K1 * (1.0 - BM25_B + BM25_B * dl / avgdl))
        if s > 0:
            bm25_scores[rel] = s

    if not bm25_scores:
        return [], rels

    # Runtime graph enrichment (C_Q Top-50 reranking, matching 116-gwf-v2-R6)
    need_graph = boost_mode == "hybrid" or eff_hops >= 1 or eff_mode == "deep"
    resolver = _build_resolver(docs) if need_graph else {}
    in_links = _build_in_links(docs, resolver, _is_accessible) if need_graph else {}
    top50_rels = {r for r, _ in sorted(bm25_scores.items(), key=lambda kv: (-kv[1], kv[0]))[:50]}

    fused: List[Tuple[float, float, str]] = []
    for rel, bscore in bm25_scores.items():
        if boost_mode == "hybrid" and rel in top50_rels:
            auth = math.log(1.0 + len(in_links.get(rel, set())))
            cluster = _cluster_affinity(rel, docs, in_links, resolver, top50_rels)
            gain = 1.0 + float(alpha) * auth + float(beta) * cluster
        else:
            gain = 1.0
        fused.append((bscore * gain, bscore, rel))

    fused.sort(key=lambda item: (-item[0], item[2]))
    max_score = fused[0][0] if fused else 1.0
    score_norm_map = {rel: (round((total_s / max_score) * 100.0, 2) if max_score > 0 else 0.0) for total_s, _bs, rel in fused}
    link_keys = wanted_link_fields if wanted_link_fields is not None else list(_DEFAULT_LINK_FIELDS)

    hits: List[Dict[str, Any]] = []
    for total_s, bscore, rel in fused:
        norm_score = score_norm_map[rel]
        if min_score > 0 and norm_score < min_score:
            continue
        d = docs[rel]
        m = d["meta"]
        base_pub = _public_note(kek=kek, app_id=app_id, vault_ref=vault_ref, note_ref=mapping[rel], rel=rel, meta=m)
        title_val = _clip(base_pub.get("title"), summary_width) if summary_width > 0 else base_pub.get("title")
        summary_val = _clip(base_pub.get("summary"), summary_width) if summary_width > 0 else base_pub.get("summary")

        if not legacy_override and eff_mode == "locate":
            pub: Dict[str, Any] = {
                "vault_ref": vault_ref,
                "note_ref": mapping[rel],
                "title": title_val,
                "score": norm_score,
                "score_raw": round(total_s, 4),
                "bm25_score": round(bscore, 4),
                "doc_type": base_pub.get("doc_type") or "",
                "summary": summary_val or "",
                "updated": base_pub.get("updated") or "",
                "status": base_pub.get("status") or "",
                "zone": base_pub.get("zone") or "",
            }
            if base_pub.get("project_id"):
                pub["project_id"] = base_pub["project_id"]
            hits.append(pub)
            if len(hits) >= max(1, top_k):
                break
            continue

        if not legacy_override and eff_mode == "passage":
            passage_txt, sec_hd, is_trunc = extract_passage_and_section(
                str(d.get("raw") or ""),
                terms,
                doc_type=str(base_pub.get("doc_type") or ""),
                max_chars=passage_max_chars,
            )
            pub = {
                "vault_ref": vault_ref,
                "note_ref": mapping[rel],
                "title": title_val,
                "score": norm_score,
                "score_raw": round(total_s, 4),
                "bm25_score": round(bscore, 4),
                "doc_type": base_pub.get("doc_type") or "",
                "updated": base_pub.get("updated") or "",
                "status": base_pub.get("status") or "",
                "zone": base_pub.get("zone") or "",
                "passage": passage_txt,
                "passage_truncated": is_trunc,
            }
            if sec_hd:
                pub["section"] = sec_hd
            if base_pub.get("project_id"):
                pub["project_id"] = base_pub["project_id"]
            if m.get("type"):
                pub["type"] = str(m["type"])
            if m.get("stage"):
                pub["stage"] = str(m["stage"])
            hits.append(pub)
            if len(hits) >= max(1, top_k):
                break
            continue

        if not legacy_override and eff_mode == "deep":
            in_top3, out_top3 = _ranked_directed_links(
                rel, docs, in_links, resolver, _is_accessible, score_norm_map, mapping, limit=3
            )
            pub = {
                "vault_ref": vault_ref,
                "note_ref": mapping[rel],
                "title": title_val,
                "score": norm_score,
                "score_raw": round(total_s, 4),
                "bm25_score": round(bscore, 4),
                "doc_type": base_pub.get("doc_type") or "",
                "summary": summary_val or "",
                "updated": base_pub.get("updated") or "",
                "status": base_pub.get("status") or "",
                "zone": base_pub.get("zone") or "",
                "in_links_top3": in_top3,
                "out_links_top3": out_top3,
            }
            if base_pub.get("project_id"):
                pub["project_id"] = base_pub["project_id"]
            if m.get("type"):
                pub["type"] = str(m["type"])
            if m.get("stage"):
                pub["stage"] = str(m["stage"])
            hits.append(pub)
            if len(hits) >= max(1, top_k):
                break
            continue

        # Legacy fine-grained override path (--hops / --deep / --fields / --no_snippet / --with_content)
        pub = base_pub
        if summary_width > 0:
            pub["title"] = title_val
            pub["summary"] = summary_val
        pub["type"] = str(m.get("type") or "")
        if m.get("stage"):
            pub["stage"] = str(m.get("stage") or "")
        pub["aliases"] = list(m.get("aliases") or [])
        parts = Path(rel).parts
        pub["is_private"] = bool(
            (m.get("private") is True)
            or (len(parts) >= 2 and parts[0] == "30-Projects" and parts[1] in private_projects)
        )
        if m.get("map"):
            pub["map"] = list(m.get("map") or [])
        pub["bm25_score"] = round(bscore, 4)
        pub["score_raw"] = round(total_s, 4)
        pub["score"] = norm_score

        if not no_snippet:
            eff_snip_w = max(snippet_width, 600) if with_content else snippet_width
            snip, sec = extract_kwic_and_section(str(d.get("raw") or ""), terms, width=eff_snip_w)
            pub["snippet"] = snip
            if sec:
                pub["section"] = sec
        if with_content:
            pub["content_preview"] = str(d.get("body") or "").strip()[:1200]
        if eff_mode == "deep":
            in_top3, out_top3 = _ranked_directed_links(
                rel, docs, in_links, resolver, _is_accessible, score_norm_map, mapping, limit=3
            )
            pub["in_links_top3"] = in_top3
            pub["out_links_top3"] = out_top3

        # Direct 1-hop links (KEK-blinded handles only)
        if eff_hops >= 1:
            dlinks: List[Dict[str, Any]] = []
            for nb in sorted(_one_hop(rel, docs, in_links, resolver))[:8]:
                nd = docs[nb]
                if not _is_accessible(nb, nd):
                    continue
                nm = nd["meta"]
                link_full: Dict[str, Any] = {
                    "note_ref": mapping.get(nb),
                    "title": _clip(nm.get("title") or Path(nb).stem, summary_width) if summary_width > 0 else (nm.get("title") or Path(nb).stem),
                    "type": nm.get("type") or nm.get("doc_type"),
                    "status": nm.get("status") or "",
                    "stage": nm.get("stage") or "",
                    "summary": _clip(nm.get("summary") or "", summary_width) if summary_width > 0 else (nm.get("summary") or ""),
                    "tags": list(nm.get("tags") or []),
                    "aliases": list(nm.get("aliases") or []),
                    "updated": nm.get("updated") or "",
                }
                dlinks.append({k: link_full[k] for k in link_keys if k in link_full})
            pub["direct_links"] = dlinks

        if eff_hops >= 2:
            bridges: List[Dict[str, Any]] = []
            for b_rel, b_score in _two_hop_bridges(rel, docs, in_links, resolver, _is_accessible, limit=3):
                bd = docs[b_rel]
                bridges.append(
                    {
                        "note_ref": mapping.get(b_rel),
                        "title": bd["meta"].get("title") or Path(b_rel).stem,
                        "type": bd["meta"].get("type") or bd["meta"].get("doc_type"),
                        "bridge_score": round(b_score, 2),
                    }
                )
            pub["two_hop_bridges"] = bridges
        if eff_hops >= 3:
            pub["graph_metrics"] = _three_hop_metrics(rel, docs, in_links, resolver)

        if wanted_fields is not None:
            core_keys = {"vault_ref", "note_ref", "title", "score", "score_raw", "bm25_score"}
            keep = set(wanted_fields) | core_keys
            pub = {k: v for k, v in pub.items() if k in keep}

        hits.append(pub)
        if len(hits) >= max(1, top_k):
            break
    return hits, rels


def note_search_all_vaults(
    *,
    vault_entries: List[Tuple[str, str, Path]],
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    query: str,
    terms_arg: Optional[str] = None,
    zone_filter: Optional[str] = None,
    doc_type_filter: Optional[str] = None,
    epistemic_type_filter: Optional[str] = None,
    status_filter: Optional[str] = None,
    exclude_zones: Optional[List[str]] = None,
    include_archived: bool = False,
    include_private: bool = False,
    exclude_pending: bool = False,
    exclude_draft: bool = False,
    mode: Optional[str] = None,
    deep: bool = False,
    hops: Optional[int] = None,
    boost: str = "hybrid",
    alpha: float = 0.15,
    beta: float = 0.08,
    min_score: int = 0,
    snippet_width: int = 160,
    passage_max_chars: int = 1200,
    summary_width: int = 0,
    no_snippet: bool = False,
    no_cache: bool = False,
    with_content: bool = False,
    fields_arg: Optional[str] = None,
    link_fields_arg: Optional[str] = None,
    cache_dir: Optional[Path] = None,
    top_k: int = 10,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    merged_hits: List[Dict[str, Any]] = []
    sealed: List[str] = []
    per_vault_k = max(5, top_k)
    for vref, vtitle, vdir in vault_entries:
        if not vdir.is_dir():
            continue
        hits, rels = note_search(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            query=query,
            terms_arg=terms_arg,
            zone_filter=zone_filter,
            doc_type_filter=doc_type_filter,
            epistemic_type_filter=epistemic_type_filter,
            status_filter=status_filter,
            exclude_zones=exclude_zones,
            include_archived=include_archived,
            include_private=include_private,
            exclude_pending=exclude_pending,
            exclude_draft=exclude_draft,
            mode=mode,
            deep=deep,
            hops=hops,
            boost=boost,
            alpha=alpha,
            beta=beta,
            min_score=min_score,
            snippet_width=snippet_width,
            passage_max_chars=passage_max_chars,
            summary_width=summary_width,
            no_snippet=no_snippet,
            no_cache=no_cache,
            with_content=with_content,
            fields_arg=fields_arg,
            link_fields_arg=link_fields_arg,
            cache_dir=cache_dir,
            top_k=per_vault_k,
        )
        sealed.extend(rels)
        for h in hits:
            h["vault_title"] = vtitle
            merged_hits.append(h)

    score_key = "score_raw" if (boost or "hybrid").strip().lower() == "hybrid" else "bm25_score"
    merged_hits.sort(
        key=lambda item: (
            -float(item.get(score_key, item.get("score", item.get("bm25_score", 0.0)))),
            str(item.get("vault_ref", "")),
            str(item.get("note_ref", "")),
        )
    )
    if merged_hits:
        global_max = max((float(item.get(score_key, 0.0)) for item in merged_hits), default=0.0)
        if global_max > 0:
            for item in merged_hits:
                raw_v = float(item.get(score_key, 0.0))
                item["score"] = round((raw_v / global_max) * 100.0, 2)
    return merged_hits[: max(1, top_k)], sealed
