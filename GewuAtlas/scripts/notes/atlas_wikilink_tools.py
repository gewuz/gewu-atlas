#!/usr/bin/env python3
"""Unified wikilink lookup, table-safe inline resolution, suggestions, and batch repair."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from facade.atlas_domain_scope import infer_zone_and_project_rel
from notes.atlas_markdown_blocks import code_line_mask
from notes.atlas_yamlmini import FrontmatterError, extract_frontmatter, resolve_wikilink
from storage.vault_kek_store import VaultStoreError

_ATTACHMENT_EXTS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".svg",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".html",
        ".htm",
        ".csv",
        ".zip",
        ".tar",
        ".gz",
        ".bin",
    }
)
_INLINE_TICK_RE = re.compile(r"(?<!`)(?P<tick>`+)(?!`)(?P<code>.*?)(?<!`)(?P=tick)(?!`)", re.S)
_WIKILINK_TOKEN_RE = re.compile(r"(?<!!)\[\[([^\[\]\r\n]+)\]\]")
_FM_HEADER_RE = re.compile(r"\A---[^\S\n]*\r?\n.*?\r\n---[^\S\n]*(?:\r?\n|$)", re.S)


def slugify_note_stem(title: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "-", str(title or "")).strip(" .-_")
    cleaned = re.sub(r"\s+", "-", cleaned)
    if not cleaned:
        cleaned = f"note-{time.strftime('%Y%m%d%H%M%S')}"
    return cleaned[:80]


def split_wikilink_inner(inner: str) -> Tuple[str, str, str]:
    """Split `target#anchor|alias` or table-escaped `target#anchor\\|alias` cleanly."""
    if "|" in inner:
        target_part, _, alias = inner.partition("|")
        if target_part.endswith("\\"):
            target_part = target_part[:-1]
    else:
        target_part, alias = inner, ""
    if "#" in target_part:
        target, _, tail = target_part.partition("#")
        anchor = "#" + tail.strip()
    else:
        target, anchor = target_part, ""
    return target.strip(), anchor, alias.strip()


def rewrite_markdown_prose(
    text: str,
    rewriter: Callable[[str], str],
    *,
    include_frontmatter: bool = True,
) -> str:
    """Rewrite prose outside code fences/indented code blocks and inline backticks."""
    header = ""
    match = _FM_HEADER_RE.match(text or "")
    body = text or ""
    if match:
        header = rewriter(match.group(0)) if include_frontmatter else match.group(0)
        body = body[match.end() :]

    out: List[str] = []
    prose: List[str] = []

    def flush() -> None:
        if not prose:
            return
        content = "".join(prose)
        start = 0
        for item in _INLINE_TICK_RE.finditer(content):
            out.append(rewriter(content[start : item.start()]))
            out.append(item.group(0))
            start = item.end()
        out.append(rewriter(content[start:]))
        prose.clear()

    for line, is_code in zip(body.splitlines(keepends=True), code_line_mask(body)):
        if is_code:
            flush()
            out.append(line)
        else:
            prose.append(line)
    flush()
    return header + "".join(out)


def _rewrite_line_outside_backticks(line: str, fn: Callable[[str], str]) -> str:
    start = 0
    parts: List[str] = []
    for item in _INLINE_TICK_RE.finditer(line):
        parts.append(fn(line[start : item.start()]))
        parts.append(item.group(0))
        start = item.end()
    parts.append(fn(line[start:]))
    return "".join(parts)


def build_vault_link_index(vault_dir: Path, mapping: Dict[str, str]) -> Dict[str, Any]:
    """Build single-pass index of note handles, stems, titles, aliases, and disambiguated keys."""
    by_ref: Dict[str, str] = {}
    by_stem: Dict[str, List[str]] = {}
    by_stem_fold: Dict[str, List[str]] = {}
    by_title: Dict[str, List[str]] = {}
    by_slug: Dict[str, List[str]] = {}
    by_alias: Dict[str, List[str]] = {}
    meta_by_rel: Dict[str, Dict[str, Any]] = {}

    for rel, nref in sorted(mapping.items()):
        fp = vault_dir / rel
        stem = Path(rel).stem
        title = stem
        aliases: List[str] = []
        if fp.is_file():
            try:
                raw = fp.read_text(encoding="utf-8", errors="replace")
                meta, _, _ = extract_frontmatter(raw)
                title = str(meta.get("title") or stem).strip() or stem
                raw_aliases = meta.get("aliases")
                if isinstance(raw_aliases, list):
                    aliases = [str(a).strip() for a in raw_aliases if str(a).strip()]
                elif isinstance(raw_aliases, str) and raw_aliases.strip():
                    aliases = [raw_aliases.strip()]
            except (OSError, FrontmatterError):
                pass
        zone, prel = infer_zone_and_project_rel(rel)
        by_ref[nref] = rel
        by_stem.setdefault(stem, []).append(rel)
        by_stem_fold.setdefault(stem.casefold(), []).append(rel)
        by_title.setdefault(title.casefold(), []).append(rel)
        by_slug.setdefault(slugify_note_stem(title).casefold(), []).append(rel)
        for al in aliases:
            by_alias.setdefault(al.casefold(), []).append(rel)
        meta_by_rel[rel] = {
            "note_ref": nref,
            "title": title,
            "stem": stem,
            "aliases": aliases,
            "zone": zone,
            "project_rel": prel,
        }

    for rel, info in meta_by_rel.items():
        stem = str(info["stem"])
        link_key = stem if len(by_stem.get(stem, ())) == 1 else rel[:-3] if rel.endswith(".md") else rel
        info["link_key"] = link_key
        info["wikilink"] = f"[[{stem}|{info['title']}]]"

    return {
        "known": set(mapping.keys()),
        "by_ref": by_ref,
        "by_stem": by_stem,
        "by_stem_fold": by_stem_fold,
        "by_title": by_title,
        "by_slug": by_slug,
        "by_alias": by_alias,
        "meta_by_rel": meta_by_rel,
    }


def _pick_scoped(cands: List[str], src_project_rel: Optional[str], meta_by_rel: Dict[str, Dict[str, Any]]) -> List[str]:
    uniq = sorted(dict.fromkeys(cands))
    if len(uniq) <= 1 or not src_project_rel:
        return uniq
    scoped = [rel for rel in uniq if meta_by_rel.get(rel, {}).get("project_rel") == src_project_rel]
    return scoped if scoped else uniq


def _lookup_buckets(raw_target: str, *, source_rel: str, index: Dict[str, Any]) -> Tuple[List[str], str]:
    key = raw_target.strip()
    if not key:
        return [], "empty"
    if key.endswith(".md"):
        key = key[:-3]
    _, src_prel = infer_zone_and_project_rel(source_rel)
    meta_by_rel = index["meta_by_rel"]

    if key in index["by_ref"]:
        return [index["by_ref"][key]], "note_ref"

    fold = key.casefold()
    slug_fold = slugify_note_stem(key).casefold()
    for bucket_name, bucket, lookup_key in (
        ("stem_fold", index["by_stem_fold"], fold),
        ("title", index["by_title"], fold),
        ("alias", index["by_alias"], fold),
        ("slug", index["by_slug"], slug_fold),
        ("slug_stem", index["by_stem_fold"], slug_fold),
    ):
        hits = bucket.get(lookup_key, [])
        if hits:
            return _pick_scoped(hits, src_prel, meta_by_rel), bucket_name
    return [], "missing"


def resolve_target_against_index(
    raw_target: str,
    *,
    source_rel: str,
    index: Dict[str, Any],
) -> Tuple[Optional[str], str]:
    """Return unique target rel_path only if `raw_target` is broken/handle/title needing normalization."""
    cleaned = raw_target.strip()
    if not cleaned:
        return None, "empty"
    if re.match(r"^(?:[A-Za-z][A-Za-z0-9+.-]*://|mailto:|tel:|urn:|data:|javascript:|obsidian:)", cleaned, re.I) or cleaned.startswith("//"):
        return None, "uri"
    suffix = Path(cleaned).suffix.lower()
    if suffix and suffix != ".md" and suffix in _ATTACHMENT_EXTS:
        return None, "attachment"

    existing = resolve_wikilink(cleaned, source_rel, index["known"])
    if len(existing) == 1:
        return None, "already_valid"

    cands, reason = _lookup_buckets(cleaned, source_rel=source_rel, index=index)
    if len(cands) == 1:
        return cands[0], reason
    if len(cands) > 1:
        return None, "ambiguous"
    return None, "missing"


def suggest_targets_for_broken(
    raw_target: str,
    *,
    source_rel: str,
    index: Dict[str, Any],
    limit: int = 3,
) -> List[Dict[str, str]]:
    """Suggest candidate notes (without leaking physical paths) for broken/ambiguous wikilinks."""
    cands, reason = _lookup_buckets(raw_target, source_rel=source_rel, index=index)
    out: List[Dict[str, str]] = []
    meta_by_rel = index["meta_by_rel"]
    seen_refs: set[str] = set()

    for rel in cands[:limit]:
        info = meta_by_rel[rel]
        seen_refs.add(info["note_ref"])
        out.append(
            {
                "note_ref": info["note_ref"],
                "title": info["title"],
                "note_name": info["stem"],
                "wikilink": info["wikilink"],
                "match": reason,
            }
        )
    if len(out) >= limit:
        return out

    q = raw_target.strip().casefold()
    q_slug = slugify_note_stem(raw_target).casefold()
    if not q:
        return out
    _, src_prel = infer_zone_and_project_rel(source_rel)
    scored: List[Tuple[int, str, str]] = []
    for rel, info in meta_by_rel.items():
        if rel == source_rel or info["note_ref"] in seen_refs:
            continue
        t_fold = info["title"].casefold()
        s_fold = info["stem"].casefold()
        # Never fuzzy-suggest a TOC card (`TOC-<source>`) as the replacement for a missing source document (`<source>`).
        if not q.startswith("toc-") and not q_slug.startswith("toc-"):
            if s_fold == f"toc-{q_slug}" or t_fold == f"toc-{q}" or s_fold.startswith(f"toc-{q_slug}"):
                continue
        if q in t_fold or t_fold in q or q_slug in s_fold or s_fold in q_slug:
            score = 100 - abs(len(t_fold) - len(q))
            if src_prel and info.get("project_rel") == src_prel:
                score += 20
            scored.append((score, rel, "fuzzy"))
    scored.sort(key=lambda it: (-it[0], it[1]))
    for _, rel, match_kind in scored[: max(0, limit - len(out))]:
        info = meta_by_rel[rel]
        out.append(
            {
                "note_ref": info["note_ref"],
                "title": info["title"],
                "note_name": info["stem"],
                "wikilink": info["wikilink"],
                "match": match_kind,
            }
        )
    return out


def _format_wikilink(
    *,
    link_key: str,
    anchor: str,
    title: str,
    explicit_alias: str,
    reason: str,
    in_table: bool,
) -> str:
    if explicit_alias:
        alias = explicit_alias
    elif reason == "note_ref" or (title and title != link_key):
        alias = title
    else:
        alias = ""

    if not alias or alias == link_key:
        return f"[[{link_key}{anchor}]]"
    sep = r"\|" if in_table else "|"
    return f"[[{link_key}{anchor}{sep}{alias}]]"


def normalize_inline_wikilinks(
    text: str,
    *,
    source_rel: str,
    index: Dict[str, Any],
    keep_table_safe: bool = True,
) -> Tuple[str, List[Dict[str, str]]]:
    """Resolve `[[nte_XX]]`, `[[title]]`, alias, or wrong-slug wikilinks in prose & FM safely."""
    rewrites: List[Dict[str, str]] = []
    meta_by_rel = index["meta_by_rel"]

    def replace_segment(seg: str, *, in_table: bool) -> str:
        def _repl(m: re.Match[str]) -> str:
            raw_inner = m.group(1)
            target, anchor, alias = split_wikilink_inner(raw_inner)
            dst_rel, reason = resolve_target_against_index(target, source_rel=source_rel, index=index)
            if dst_rel is None:
                return m.group(0)
            info = meta_by_rel[dst_rel]
            replacement = _format_wikilink(
                link_key=str(info["link_key"]),
                anchor=anchor,
                title=str(info["title"]),
                explicit_alias=alias,
                reason=reason,
                in_table=in_table,
            )
            if replacement != m.group(0):
                rewrites.append(
                    {
                        "from": m.group(0),
                        "to": replacement,
                        "target_ref": str(info["note_ref"]),
                        "match": reason,
                    }
                )
            return replacement

        return _WIKILINK_TOKEN_RE.sub(_repl, seg)

    header = ""
    body = text or ""
    match = _FM_HEADER_RE.match(body)
    if match:
        raw_hdr = match.group(0)
        hdr_lines: List[str] = []
        for ln in raw_hdr.splitlines(keepends=True):
            if ln.strip() == "---" or ln.startswith(("title:", "summary:", "aliases:")):
                hdr_lines.append(ln)
            else:
                hdr_lines.append(replace_segment(ln, in_table=False))
        header = "".join(hdr_lines)
        body = body[match.end() :]

    out_lines: List[str] = []
    for line, is_code in zip(body.splitlines(keepends=True), code_line_mask(body)):
        if is_code:
            out_lines.append(line)
            continue
        is_tbl = keep_table_safe and line.lstrip().startswith("|")
        out_lines.append(_rewrite_line_outside_backticks(line, lambda seg, tbl=is_tbl: replace_segment(seg, in_table=tbl)))

    return header + "".join(out_lines), rewrites
