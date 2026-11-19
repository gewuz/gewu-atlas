#!/usr/bin/env python3
"""Bookmark annotation & lightweight task slip engine (`25-Slip`, `doc_type: slip`) for GewuAtlas.

Implements Agent-first fill-in-the-blank operators with minimal hard safety boundaries:
- Fence/inline-code/frontmatter-aware `@@...@@` marker scanning;
- `slip-<YYMMDD>-<4hex>-<slug>.md` naming norm inside `<domain>/25-Slip/`;
- Bidirectional wikilinks (`source -> [[slip|original_marker]]` + `slip -> [[source]]`);
- Exact original bookmark text preserved in Frontmatter `aliases` & link display alias;
- Nested slip support with ancestor cycle detection & bounded depth (`MAX_SLIP_DEPTH`);
- Optimistic locking via `expected_sha256`;
- Human-approved delete (`apply=False` -> INPUT_REQUIRED preview; `apply=True` -> strip inbound
  links cleanly + move slip to `.trash/` via `vfs_trash`).
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from facade.atlas_domain_scope import (
    infer_zone_and_project_rel,
    read_domain_manifest,
    resolve_domain_scope,
    resolve_zone_dir,
    scan_project_rels,
)
from notes.atlas_note_engine import (
    _append_journal_event,
    _atomic_write_md,
    _first_meaningful_sentence,
    _note_seal,
    _parse_list_arg,
    _public_note,
    _scan_vault_markdowns,
    _slugify_filename,
    _today,
    _update_frontmatter_preserving_extras,
    note_get,
    note_list,
)
from pipeline.atlas_vfs_engine import KekTrashRegistry, _is_rel_frozen, vfs_trash
from notes.atlas_yamlmini import (
    ALLOWED_SLIP_KINDS,
    FrontmatterError,
    extract_frontmatter,
    extract_wikilinks,
    fm_scalar,
    replace_frontmatter_field,
    validate_note_meta,
)
from storage.note_kek_store import KekNoteRegistry, validate_note_rel_path
from storage.project_kek_store import KekProjectRegistry
from collab.ticket_bridge import ensure_slip_ticket, update_slip_ticket_progress
from storage.vault_kek_store import VaultStoreError

MAX_SLIP_DEPTH = 5

_FENCE_LINE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")
_HEADING_RE = re.compile(r"^[ \t]*(#{1,6})\s+(.+?)\s*$")
_URL_RE = re.compile(r"https?://[^\s<>()\[\]\"'`@＠=＝]+", re.IGNORECASE)
_EQ_CHARS = {"=", "＝"}
_AT_CHARS = {"@", "＠"}


def _strip_marker_delimiters(raw: str) -> str:
    s = (raw or "").strip()
    if len(s) >= 5 and s[0] in _EQ_CHARS and s[1] in _EQ_CHARS and s[2] in _AT_CHARS and s[-1] in _EQ_CHARS and s[-2] in _EQ_CHARS:
        return s[3:-2].strip()
    if len(s) >= 4 and s[0] in _AT_CHARS and s[1] in _AT_CHARS and s[-1] in _AT_CHARS and s[-2] in _AT_CHARS:
        return s[2:-2].strip()
    return s
_FRONTMATTER_RE = re.compile(r"\A---[^\S\n]*\r?\n.*?\r?\n---[^\S\n]*(?:\r?\n|$)", re.S)

_DELETE_IMPERATIVE_RE = re.compile(
    r"(?:^(?:请|帮我|帮忙)?(?:删除|删掉|移除|清理|作废|trash\b|remove\b|delete\b)|(?:删除|删掉|移除|清理)(?:这[个条张份]|上述|上面|相关|当前|旧|此)?\s*(?:slip|书签|批注|卡片|笔记))",
    re.IGNORECASE,
)
_DELETE_EXPLAIN_RE = re.compile(r"(?:什么[是叫]|原理|怎么理解|为什么|区别|概念|介绍|解释.*删除)", re.IGNORECASE)
_SEARCH_KW_RE = re.compile(r"(?:查找|搜索|检索|找出|列出|找一下|search\b|find\b).*?(?:slip|书签|笔记|相关)?", re.IGNORECASE)
_DOWNLOAD_KW_RE = re.compile(r"(?:下载|抓取|采集|拉取|保存(?:原文|网页|文档)?|ingest|download|fetch)", re.IGNORECASE)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _body_start_offset(text: str) -> int:
    m = _FRONTMATTER_RE.match(text)
    return m.end() if m else 0


def _infer_kind_hint(prompt: str) -> Tuple[str, List[str]]:
    urls = [u.rstrip(".,;:!?。，；：！？") for u in _URL_RE.findall(prompt)]
    clean = prompt.strip()
    if urls and (_DOWNLOAD_KW_RE.search(clean) or len(urls) >= 1 and len(clean) < 160):
        return "download", urls
    if _DELETE_IMPERATIVE_RE.search(clean) and not _DELETE_EXPLAIN_RE.search(clean):
        return "delete", urls
    if _SEARCH_KW_RE.search(clean):
        return "search", urls
    if any(k in clean for k in ("记笔记", "建卡", "沉淀", "整理成")):
        return "note", urls
    return "explain", urls


def _marker_id_for(occurrence: int, raw_block: str) -> str:
    digest = hashlib.sha256(f"{occurrence}:{raw_block.strip()}".encode("utf-8")).hexdigest()[:6]
    return f"slp_{digest}"


def _extract_markers_from_markdown(text: str) -> List[Dict[str, Any]]:
    """Extract `==@...@==` (primary, half/full-width `=`/`＝` & `@`/`＠`) and legacy `@@...@@` markers outside frontmatter/code/escapes."""
    body_start = _body_start_offset(text)
    n = len(text)
    pos = body_start
    line_num = text[:body_start].count("\n") + 1
    in_fence_char: Optional[str] = None
    in_fence_len = 0
    current_heading = ""
    markers: List[Dict[str, Any]] = []
    seen_counts: Dict[str, int] = {}

    def _is_eq2_at(idx: int) -> bool:
        return idx + 2 < n and text[idx] in _EQ_CHARS and text[idx + 1] in _EQ_CHARS and text[idx + 2] in _AT_CHARS

    def _is_eq2(idx: int) -> bool:
        return idx + 1 < n and text[idx] in _EQ_CHARS and text[idx + 1] in _EQ_CHARS

    def _is_at2(idx: int) -> bool:
        return idx + 1 < n and text[idx] in _AT_CHARS and text[idx + 1] in _AT_CHARS

    def _find_closing_backticks(idx: int, tick_len: int) -> Optional[int]:
        j = idx
        while j < n:
            if text[j] == "\n":
                if j + 1 < n and text[j + 1] == "\n":
                    return None
                if text[j : j + 2] == "\r\n" and j + 3 < n and text[j + 2 : j + 4] == "\r\n":
                    return None
            if text[j] == "`":
                k = j
                while k < n and text[k] == "`":
                    k += 1
                if k - j == tick_len:
                    return k
                j = k
                continue
            j += 1
        return None

    def _emit_marker(m_start: int, m_end: int, m_line: int, m_heading: str, inner_raw: str) -> None:
        raw_block = text[m_start:m_end]
        prompt = inner_raw.strip()
        if not prompt:
            return
        before = text[max(body_start, m_start - 300) : m_start].strip()
        after = text[m_end : min(n, m_end + 200)].strip()
        kind_hint, urls = _infer_kind_hint(prompt)
        alias_single = re.sub(r"[\r\n]+", " ", prompt).strip()
        occ = seen_counts.get(raw_block, 0)
        seen_counts[raw_block] = occ + 1
        markers.append(
            {
                "marker_id": _marker_id_for(occ, raw_block),
                "start": m_start,
                "end": m_end,
                "line": m_line,
                "section_heading": m_heading,
                "raw_block": raw_block,
                "prompt": prompt,
                "alias_preview": alias_single,
                "kind_hint": kind_hint,
                "urls_found": urls,
                "context_before": before[-240:] if len(before) > 240 else before,
                "context_after": after[:180] if len(after) > 180 else after,
            }
        )

    marker_kind: Optional[str] = None
    marker_start = -1
    marker_line = 1
    marker_heading = ""

    while pos < n:
        at_line_start = pos == body_start or text[pos - 1] == "\n"
        if at_line_start:
            line_end = text.find("\n", pos)
            if line_end < 0:
                line_end = n
            line_str = text[pos:line_end].rstrip("\r")
            fm = _FENCE_LINE_RE.match(line_str)
            if fm and marker_kind is None:
                fence_tok = fm.group(1)
                if in_fence_char is None:
                    in_fence_char = fence_tok[0]
                    in_fence_len = len(fence_tok)
                    pos = line_end + (1 if line_end < n else 0)
                    line_num += 1
                    continue
                if fence_tok[0] == in_fence_char and len(fence_tok) >= in_fence_len and not fm.group(2).strip():
                    in_fence_char = None
                    in_fence_len = 0
                    pos = line_end + (1 if line_end < n else 0)
                    line_num += 1
                    continue
            if in_fence_char is not None:
                pos = line_end + (1 if line_end < n else 0)
                line_num += 1
                continue
            hm = _HEADING_RE.match(line_str)
            if hm and marker_kind is None:
                current_heading = hm.group(2).strip()

        ch = text[pos]
        if ch == "\n":
            line_num += 1
            pos += 1
            continue

        if ch == "\\":
            if _is_eq2_at(pos + 1):
                pos += 4
                continue
            if _is_at2(pos + 1):
                pos += 3
                continue

        if ch == "`":
            k = pos
            while k < n and text[k] == "`":
                k += 1
            closed = _find_closing_backticks(k, k - pos)
            if closed is not None:
                line_num += text[pos:closed].count("\n")
                pos = closed
                continue
            pos = k
            continue

        if marker_kind is None:
            if _is_eq2_at(pos):
                marker_kind = "hl"
                marker_start = pos
                marker_line = line_num
                marker_heading = current_heading
                pos += 3
                continue
            if _is_at2(pos):
                marker_kind = "at"
                marker_start = pos
                marker_line = line_num
                marker_heading = current_heading
                pos += 2
                continue
        elif marker_kind == "hl" and _is_eq2(pos):
            end_pos = pos + 2
            _emit_marker(marker_start, end_pos, marker_line, marker_heading, text[marker_start + 3 : pos])
            marker_kind = None
            pos = end_pos
            continue
        elif marker_kind == "at" and _is_at2(pos):
            end_pos = pos + 2
            _emit_marker(marker_start, end_pos, marker_line, marker_heading, text[marker_start + 2 : pos])
            marker_kind = None
            pos = end_pos
            continue

        pos += 1

    return markers


def _slugify_slip_part(title: str) -> str:
    clean = re.sub(r"^(?:slip[-_\s]+)+", "", (title or "").strip(), flags=re.IGNORECASE)
    # Strip both filesystem-unsafe and Obsidian wikilink-reserved symbols (# ^ | [ ])
    clean = re.sub(r'[\\/:*?"<>|\r\n\t#^\[\]]+', "-", clean).strip(" .-_")
    clean = re.sub(r"\s+", "-", clean)
    clean = re.sub(r"-{2,}", "-", clean).strip("-")
    if not clean:
        clean = "bookmark"
    return _slugify_filename(clean)[:48].strip("-") or "bookmark"


def _build_unique_slip_stem(title: str, slip_dir: Path, pid: str = "AI-Zone") -> str:
    clean_pid = str(pid or "AI-Zone").strip() or "AI-Zone"
    slug = _slugify_slip_part(title)
    pat = re.compile(r"^Slip-" + re.escape(clean_pid) + r"-(\d+)-")
    mx = 0
    if slip_dir.is_dir():
        for f in slip_dir.glob("*.md"):
            m = pat.match(f.name)
            if m:
                mx = max(mx, int(m.group(1)))
    seq = mx + 1
    while (slip_dir / f"Slip-{clean_pid}-{seq:03d}-{slug}.md").exists():
        seq += 1
    return f"Slip-{clean_pid}-{seq:03d}-{slug}"


def _wikilink_display_alias(raw_prompt: str) -> str:
    single = re.sub(r"[\r\n]+", " ", raw_prompt).strip()
    return single.replace("|", "｜").replace("[", "(").replace("]", ")")


def _compute_nesting_depth(vault_dir: Path, rel_path: str, stem_to_rel: Dict[str, str]) -> int:
    depth = 0
    seen: set[str] = {rel_path}
    cur_rel = rel_path
    while depth <= MAX_SLIP_DEPTH + 2:
        fpath = vault_dir / cur_rel
        if not fpath.is_file():
            break
        try:
            meta, _b, _h = extract_frontmatter(fpath.read_text(encoding="utf-8"))
        except FrontmatterError:
            break
        if str(meta.get("doc_type") or "").strip().lower() != "slip":
            break
        depth += 1
        parent_raw = str(meta.get("parent_slip") or "").strip()
        if not parent_raw:
            break
        links = extract_wikilinks(parent_raw)
        if not links:
            break
        p_stem = links[0]
        next_rel = stem_to_rel.get(p_stem)
        if not next_rel or next_rel in seen:
            break
        seen.add(next_rel)
        cur_rel = next_rel
    return depth


def _strip_wikilinks_fence_safe(text: str, target_stems: Sequence[str]) -> Tuple[str, int]:
    """Remove `[[target_stem...]]` outside code fences & inline code, plus clean YAML frontmatter refs."""
    if not target_stems:
        return text, 0
    stem_alt = "|".join(re.escape(s) for s in target_stems)
    pat = re.compile(rf"\[\[(?:{stem_alt})(?:[|#][^\]]*)?\]\]")

    total_removed = 0
    fm_match = _FRONTMATTER_RE.match(text)
    if fm_match:
        fm_block = fm_match.group(0)
        fm_new, n_fm = pat.subn("", fm_block)
        if n_fm > 0:
            # Clean empty quoted scalar remnants like `parent_slip: ""` stays valid YAML
            total_removed += n_fm
            text = fm_new + text[fm_match.end() :]

    body_start = _body_start_offset(text)
    prefix = text[:body_start]
    body = text[body_start:]

    out_lines: List[str] = []
    in_fence_char: Optional[str] = None
    in_fence_len = 0

    for line in body.splitlines(keepends=True):
        raw_no_nl = line.rstrip("\r\n")
        nl = line[len(raw_no_nl) :]
        fm = _FENCE_LINE_RE.match(raw_no_nl)
        if fm:
            tok = fm.group(1)
            if in_fence_char is None:
                in_fence_char = tok[0]
                in_fence_len = len(tok)
                out_lines.append(line)
                continue
            if tok[0] == in_fence_char and len(tok) >= in_fence_len and not fm.group(2).strip():
                in_fence_char = None
                in_fence_len = 0
                out_lines.append(line)
                continue
        if in_fence_char is not None:
            out_lines.append(line)
            continue

        # Process non-fenced line preserving inline code spans
        segments: List[str] = []
        i = 0
        m_len = len(raw_no_nl)
        line_removed = 0
        while i < m_len:
            if raw_no_nl[i] == "`":
                k = i
                while k < m_len and raw_no_nl[k] == "`":
                    k += 1
                tick_len = k - i
                # Find matching closing backticks on same line
                j = k
                found = -1
                while j < m_len:
                    if raw_no_nl[j] == "`":
                        t2 = j
                        while t2 < m_len and raw_no_nl[t2] == "`":
                            t2 += 1
                        if t2 - j == tick_len:
                            found = t2
                            break
                        j = t2
                        continue
                    j += 1
                if found >= 0:
                    segments.append(raw_no_nl[i:found])
                    i = found
                    continue
                segments.append(raw_no_nl[i:k])
                i = k
                continue

            next_tick = raw_no_nl.find("`", i)
            chunk = raw_no_nl[i:] if next_tick < 0 else raw_no_nl[i:next_tick]
            cleaned, n_hit = pat.subn("", chunk)
            line_removed += n_hit
            segments.append(cleaned)
            i = m_len if next_tick < 0 else next_tick

        if line_removed > 0:
            total_removed += line_removed
            rebuilt = "".join(segments)
            # Clean orphaned list bullet lines left completely empty after stripping a link
            if re.fullmatch(r"\s*(?:[-*+]|\d+\.)\s*", rebuilt):
                continue
            rebuilt = re.sub(r"[ \t]{2,}", " ", rebuilt).rstrip()
            out_lines.append(rebuilt + nl)
        else:
            out_lines.append(line)

    return prefix + "".join(out_lines), total_removed


def _find_target_marker(
    text: str,
    *,
    marker_text: Optional[str] = None,
    marker_id: Optional[str] = None,
) -> Dict[str, Any]:
    markers = _extract_markers_from_markdown(text)
    if not markers:
        raise VaultStoreError("E_NOT_FOUND", "源笔记中未扫描到任何有效的 ==@指令== 或 @@...@@ 书签标记块", exit_code=1)

    if marker_id:
        hits = [m for m in markers if m["marker_id"] == marker_id.strip()]
        if not hits:
            raise VaultStoreError("E_NOT_FOUND", f"未找到 marker_id={marker_id} 对应的书签标记块", exit_code=1)
        return hits[0]

    if marker_text:
        needle = marker_text.strip()
        needle_inner = _strip_marker_delimiters(needle)
        hits = [m for m in markers if m["raw_block"] == needle or m["prompt"] == needle_inner]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise VaultStoreError(
                "E_COLLISION",
                f"源笔记存在 {len(hits)} 个内容完全相同的书签标记块，请附带 --marker_id 精确指定",
                exit_code=1,
            )
        raise VaultStoreError("E_NOT_FOUND", "未找到与 --marker_text 匹配的 ==@指令== 或 @@...@@ 书签标记块", exit_code=1)

    if len(markers) == 1:
        return markers[0]
    raise VaultStoreError(
        "E_PARAM_MISSING",
        f"源笔记包含 {len(markers)} 个书签标记块，请传入 --marker_id 或 --marker_text 指定目标块",
        exit_code=1,
    )


def _atomic_create_exclusive(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        if target.exists():
            target.unlink()
        raise


def _resolve_link_stems(
    *,
    raw_links: Any,
    note_reg: KekNoteRegistry,
    vault_ref: str,
) -> List[str]:
    items = _parse_list_arg(raw_links)
    stems: List[str] = []
    for item in items:
        tok = item.strip()
        if not tok:
            continue
        if tok.startswith("[[") and tok.endswith("]]"):
            tok = tok[2:-2].split("|")[0].split("#")[0].strip()
        if re.fullmatch(r"nte_\d+(?:_[a-f0-9]+)?", tok):
            rec = note_reg.resolve_handle(vault_ref, tok)
            stem = Path(str(rec["note_rel_path"])).stem
        else:
            stem = tok[:-3] if tok.endswith(".md") else tok
        if stem and stem not in stems:
            stems.append(stem)
    return stems


def _require_slip_note(vault_dir: Path, rel_path: str, note_ref: str) -> Tuple[Dict[str, Any], str, bool, str]:
    fpath = vault_dir / rel_path
    if not fpath.is_file():
        raise VaultStoreError("E_NOT_FOUND", f"句柄 {note_ref} 对应书签文件不存在", exit_code=1)
    frozen = _is_rel_frozen(vault_dir, rel_path)
    if frozen:
        raise VaultStoreError("E_HALT_FROZEN", f"域 '{frozen}' 已冻结，禁止变更其书签", exit_code=1)
    txt = fpath.read_text(encoding="utf-8")
    meta, body, has_fm = extract_frontmatter(txt)
    if str(meta.get("doc_type") or "").strip().lower() != "slip":
        raise VaultStoreError("E_PARAM_INVALID", f"笔记 {note_ref} 不是 slip 类型（doc_type={meta.get('doc_type')}）", exit_code=1)
    return meta, body, has_fm, txt


def slip_scan(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    proj_reg: KekProjectRegistry,
    kek: bytes,
    app_id: str,
    ws_root: Path,
    note_ref: Optional[str] = None,
    project_ref: Optional[str] = None,
    zone_filter: Optional[str] = None,
    subdir: Optional[str] = None,
    file_name: Optional[str] = None,
    create_ticket: bool = False,
    ticket_id: Optional[str] = None,
    batch_key: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    all_rels = _scan_vault_markdowns(vault_dir)
    mapping = note_reg.sync_paths(vault_ref, all_rels)
    stem_to_rel = {Path(r).stem: r for r in all_rels}
    proj_rels = proj_reg.sync_projects(vault_ref, scan_project_rels(vault_dir))

    proj_prefix: Optional[str] = None
    domain_pid = ""
    dom: Optional[Dict[str, Any]] = None
    if project_ref:
        dom = resolve_domain_scope(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            proj_reg=proj_reg,
            explicit_project_ref=project_ref,
            scope="project",
        )
        proj_prefix = str(dom["project_rel_path"]) + "/"
        domain_pid = str(dom.get("project_id") or "")

    clean_subdir = str(subdir or "").strip().strip("/")
    if clean_subdir:
        sparts = Path(clean_subdir).parts
        if ".." in sparts or any(p.startswith(".") for p in sparts):
            raise VaultStoreError("E_PARAM_INVALID", "--subdir 包含非法路径段", exit_code=1)
    clean_fname = str(file_name or "").strip()
    if clean_fname and ("/" in clean_fname or "\\" in clean_fname or ".." in clean_fname):
        raise VaultStoreError("E_PARAM_INVALID", "--file_name 仅接受纯文件名", exit_code=1)

    target_rels: List[str] = []
    if note_ref:
        rec = note_reg.resolve_handle(vault_ref, note_ref)
        rel = str(rec["note_rel_path"])
        if not (vault_dir / rel).is_file():
            raise VaultStoreError("E_NOT_FOUND", f"句柄 {note_ref} 对应文档不存在", exit_code=1)
        target_rels = [rel]
    else:
        zf = (zone_filter or "").strip().lower()
        subdir_prefix: Optional[str] = None
        if clean_subdir and zf:
            dom_for_sub = dom or resolve_domain_scope(vault_dir=vault_dir, vault_ref=vault_ref, proj_reg=proj_reg, explicit_project_ref=None, scope="ai-zone")
            zdir = resolve_zone_dir(vault_dir, dom_for_sub, zf)
            subdir_prefix = (zdir / clean_subdir).relative_to(vault_dir).as_posix().rstrip("/") + "/"
        for rel in all_rels:
            if proj_prefix and not rel.startswith(proj_prefix):
                continue
            z, _prel = infer_zone_and_project_rel(rel)
            if zf and z != zf:
                continue
            if subdir_prefix:
                if not rel.startswith(subdir_prefix):
                    continue
            elif clean_subdir:
                if f"/{clean_subdir}/" not in f"/{rel}":
                    continue
            if clean_fname:
                p_rel = Path(rel)
                if p_rel.name.lower() != clean_fname.lower() and p_rel.stem.lower() != clean_fname.lower():
                    continue
            target_rels.append(rel)

    notes_out: List[Dict[str, Any]] = []
    total_markers = 0
    for rel in target_rels:
        fpath = vault_dir / rel
        txt = fpath.read_text(encoding="utf-8")
        markers = _extract_markers_from_markdown(txt)
        if not markers:
            continue
        try:
            meta, _body, _has_fm = extract_frontmatter(txt)
        except FrontmatterError:
            meta = {"doc_type": "invalid", "title": fpath.stem}

        z, prel = infer_zone_and_project_rel(rel)
        is_slip = str(meta.get("doc_type") or "").strip().lower() == "slip"
        depth = _compute_nesting_depth(vault_dir, rel, stem_to_rel) if is_slip else 0
        item: Dict[str, Any] = {
            "note_ref": mapping[rel],
            "note_name": fpath.stem,
            "title": str(meta.get("title") or fpath.stem),
            "doc_type": str(meta.get("doc_type") or ""),
            "zone": z,
            "domain_scope": "project" if prel else "ai-zone",
            "is_nested_slip": is_slip,
            "nesting_depth": depth,
            "can_nest": depth < MAX_SLIP_DEPTH,
            "source_sha256": _sha256_text(txt),
            "handle_seal": _note_seal(kek, app_id, vault_ref, mapping[rel], rel),
            "marker_count": len(markers),
            "markers": markers,
        }
        if prel:
            pref = proj_rels.get(prel)
            if pref:
                item["project_ref"] = pref
            m_dom = read_domain_manifest(vault_dir / prel, Path(prel).name)
            if m_dom.get("project_id"):
                item["project_id"] = str(m_dom["project_id"])
                if not domain_pid:
                    domain_pid = str(m_dom["project_id"])
        notes_out.append(item)
        total_markers += len(markers)

    resolved_ticket_id = ""
    if (create_ticket or ticket_id) and (total_markers > 0 or ticket_id):
        tkey = (batch_key or f"slip:{vault_ref}:{note_ref or project_ref or 'vault'}").strip()
        tinfo = ensure_slip_ticket(
            ws_root,
            vault_dir,
            task_key=tkey,
            title=f"Slip 书签批注批处理（{total_markers} 处待处理）",
            summary=f"扫描 {len(notes_out)} 篇文档，检出 {total_markers} 个 @@ 书签标记块",
            project_id=domain_pid,
            ticket_id=ticket_id,
        )
        resolved_ticket_id = str(tinfo.get("ticket_id") or "")

    out: Dict[str, Any] = {
        "vault_ref": vault_ref,
        "scanned_files": len(target_rels),
        "matched_files": len(notes_out),
        "marker_count": total_markers,
        "notes": notes_out,
        "path_sealed": True,
    }
    if resolved_ticket_id:
        out["ticket_id"] = resolved_ticket_id
    return out, all_rels


def slip_commit(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    proj_reg: KekProjectRegistry,
    kek: bytes,
    app_id: str,
    ws_root: Path,
    title: str,
    content: str,
    summary: Optional[str] = None,
    tags: Any = None,
    aliases: Any = None,
    slip_kind: Optional[str] = None,
    status: str = "active",
    source_note_ref: Optional[str] = None,
    marker_text: Optional[str] = None,
    marker_id: Optional[str] = None,
    expected_sha256: Optional[str] = None,
    source_url: Optional[str] = None,
    links: Any = None,
    project_ref: Optional[str] = None,
    scope: str = "auto",
    ticket_id: Optional[str] = None,
    batch_key: Optional[str] = None,
    mark_done: bool = False,
) -> Tuple[Dict[str, Any], List[str]]:
    clean_title = (title or "").strip()
    if not clean_title:
        raise VaultStoreError("E_PARAM_MISSING", "slip-commit 需要 --title <标题>", exit_code=1)
    clean_body_input = (content or "").strip()
    if not clean_body_input:
        raise VaultStoreError("E_PARAM_MISSING", "slip-commit 需要 --content <书签正文>", exit_code=1)

    kind_val = (slip_kind or "explain").strip().lower()
    if kind_val not in ALLOWED_SLIP_KINDS:
        raise VaultStoreError("E_PARAM_INVALID", f"不支持的 slip_kind: {kind_val}", exit_code=1)
    st_val = (status or "active").strip().lower()
    if st_val not in ("active", "pending"):
        raise VaultStoreError("E_PARAM_INVALID", "--status 仅支持 active 或 pending", exit_code=1)

    all_rels = _scan_vault_markdowns(vault_dir)
    note_reg.sync_paths(vault_ref, all_rels)
    stem_to_rel = {Path(r).stem: r for r in all_rels}

    src_rel: Optional[str] = None
    src_stem: Optional[str] = None
    src_text: Optional[str] = None
    src_is_slip = False
    parent_depth = 0
    matched_marker: Optional[Dict[str, Any]] = None

    if source_note_ref:
        rec = note_reg.resolve_handle(vault_ref, source_note_ref)
        src_rel = str(rec["note_rel_path"])
        src_path = vault_dir / src_rel
        if not src_path.is_file():
            raise VaultStoreError("E_NOT_FOUND", f"源笔记 {source_note_ref} 不存在", exit_code=1)
        frozen_src = _is_rel_frozen(vault_dir, src_rel)
        if frozen_src:
            raise VaultStoreError("E_HALT_FROZEN", f"源笔记所属域 '{frozen_src}' 已冻结，禁止创建并回写书签", exit_code=1)
        src_text = src_path.read_text(encoding="utf-8")
        if expected_sha256 and _sha256_text(src_text) != expected_sha256.strip().lower():
            raise VaultStoreError("E_CONFLICT", "源文档在扫描后已被修改（SHA256 不匹配），请重新 slip-scan 后再提交", exit_code=1)
        src_stem = src_path.stem
        try:
            src_meta, _sb, _sh = extract_frontmatter(src_text)
        except FrontmatterError:
            src_meta = {}
        src_is_slip = str(src_meta.get("doc_type") or "").strip().lower() == "slip"
        if src_is_slip:
            parent_depth = _compute_nesting_depth(vault_dir, src_rel, stem_to_rel)
            if parent_depth >= MAX_SLIP_DEPTH:
                raise VaultStoreError(
                    "E_PARAM_INVALID",
                    f"Slip 嵌套层级已达上限 ({MAX_SLIP_DEPTH})，禁止继续嵌套",
                    exit_code=1,
                )
        if marker_text or marker_id:
            matched_marker = _find_target_marker(src_text, marker_text=marker_text, marker_id=marker_id)

    dom = resolve_domain_scope(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        proj_reg=proj_reg,
        explicit_project_ref=project_ref,
        scope=scope,
        source_rel_path=src_rel,
    )
    target_dir = resolve_zone_dir(vault_dir, dom, "slip")
    target_rel_dir = target_dir.relative_to(vault_dir).as_posix()
    frozen_dst = _is_rel_frozen(vault_dir, f"{target_rel_dir}/placeholder.md")
    if frozen_dst:
        raise VaultStoreError("E_HALT_FROZEN", f"目标域 '{frozen_dst}' 已冻结", exit_code=1)

    slip_stem = _build_unique_slip_stem(clean_title, target_dir, pid=str(dom.get("project_id") or "AI-Zone"))
    slip_rel = validate_note_rel_path(f"{target_rel_dir}/{slip_stem}.md")
    slip_path = vault_dir / slip_rel

    # Alias handling: preserve exact original @@...@@ inner bookmark text for both Frontmatter & wikilink alias
    raw_bookmark_prompt = ""
    if matched_marker is not None:
        raw_bookmark_prompt = matched_marker["prompt"]
    elif marker_text:
        raw_bookmark_prompt = _strip_marker_delimiters(marker_text)

    alias_list: List[str] = []
    if raw_bookmark_prompt:
        single_alias = re.sub(r"[\r\n]+", " ", raw_bookmark_prompt).strip()
        if single_alias:
            alias_list.append(single_alias)
    for extra_a in _parse_list_arg(aliases):
        if extra_a not in alias_list:
            alias_list.append(extra_a)

    tag_list = _parse_list_arg(tags)
    if "Slip" not in tag_list:
        tag_list.insert(0, "Slip")

    summary_val = (summary or "").strip() or _first_meaningful_sentence(clean_body_input, clean_title)
    linked_stems = _resolve_link_stems(raw_links=links, note_reg=note_reg, vault_ref=vault_ref)

    # Ensure bidirectional backlinks in slip Markdown body so Obsidian/health-check/note-get all observe them
    body_parts: List[str] = []
    if src_stem and f"[[{src_stem}" not in clean_body_input:
        label = "上级书签" if src_is_slip else "来源文档"
        body_parts.append(f"> {label}：[[{src_stem}]]")
    body_parts.append(clean_body_input)
    extra_ref_lines = [f"- [[{s}]]" for s in linked_stems if f"[[{s}" not in clean_body_input and s != src_stem]
    if extra_ref_lines:
        body_parts.append("## 关联资料\n\n" + "\n".join(extra_ref_lines))
    final_body = "\n\n".join(body_parts).strip()

    resolved_ticket_id = str(ticket_id or "").strip()
    if ticket_id or batch_key:
        tkey = (batch_key or f"slip:{vault_ref}:{source_note_ref or 'batch'}").strip()
        tinfo = ensure_slip_ticket(
            ws_root,
            vault_dir,
            task_key=tkey,
            title=f"Slip 书签批注：{clean_title}",
            summary=summary_val,
            project_id=str(dom.get("project_id") or ""),
            ticket_id=ticket_id or None,
        )
        resolved_ticket_id = str(tinfo.get("ticket_id") or "")
        update_slip_ticket_progress(
            ws_root,
            vault_dir,
            ticket_info=tinfo,
            note=f"已生成书签 {slip_stem}（kind={kind_val}）",
            mark_done=mark_done,
        )

    tags_yaml = ", ".join(f'"{fm_scalar(t)}"' for t in tag_list)
    aliases_yaml = ", ".join(f'"{fm_scalar(a)}"' for a in alias_list)
    fm_lines = [
        "---",
        "doc_type: slip",
        f'title: "{fm_scalar(clean_title)}"',
        f'summary: "{fm_scalar(summary_val)}"',
        f"status: {st_val}",
        f"slip_kind: {kind_val}",
        f"tags: [{tags_yaml}]",
        f"aliases: [{aliases_yaml}]",
    ]
    if src_stem:
        fm_lines.append(f'source: "[[{fm_scalar(src_stem)}]]"')
        fm_lines.append(f'source_note: "[[{fm_scalar(src_stem)}]]"')
    if src_is_slip and src_stem:
        fm_lines.append(f'parent_slip: "[[{fm_scalar(src_stem)}]]"')
    if source_url:
        fm_lines.append(f'source_url: "{fm_scalar(source_url.strip())}"')
    if dom.get("project_id"):
        fm_lines.append(f'project_id: "{fm_scalar(dom["project_id"])}"')
    if resolved_ticket_id:
        fm_lines.append(f'ticket_id: "{fm_scalar(resolved_ticket_id)}"')
    fm_lines.extend([f"updated: {_today()}", "---", "", final_body, ""])
    rendered = "\n".join(fm_lines)

    meta, _body, _has_fm = extract_frontmatter(rendered)
    errs = validate_note_meta(meta)
    if errs:
        raise VaultStoreError("E_PARAM_INVALID", "; ".join(errs), exit_code=1)

    _atomic_create_exclusive(slip_path, rendered)

    # Replace matched @@...@@ block in source note with aliased wikilink [[slip_stem|original_bookmark]]
    source_wikilink = ""
    if src_rel and src_text is not None and matched_marker is not None:
        display_alias = _wikilink_display_alias(raw_bookmark_prompt or clean_title)
        source_wikilink = f"[[{slip_stem}|{display_alias}]]"
        updated_src = (
            src_text[: matched_marker["start"]]
            + source_wikilink
            + src_text[matched_marker["end"] :]
        )
        _atomic_write_md(vault_dir / src_rel, updated_src)

    _append_journal_event(vault_dir, note_reg, vault_ref, slip_stem, f"生成书签「{clean_title}」({kind_val})")
    new_rels = _scan_vault_markdowns(vault_dir)
    new_map = note_reg.sync_paths(vault_ref, new_rels)
    slip_ref = new_map[slip_rel]

    pub = _public_note(kek=kek, app_id=app_id, vault_ref=vault_ref, note_ref=slip_ref, rel=slip_rel, meta=meta)
    pub["note_name"] = slip_stem
    pub["slip_kind"] = kind_val
    pub["aliases"] = alias_list
    pub["backlink_added"] = bool(src_stem)
    pub["nesting_depth"] = (parent_depth + 1) if src_is_slip else 1
    if source_note_ref:
        pub["source_note_ref"] = source_note_ref
    if source_wikilink:
        pub["source_wikilink"] = source_wikilink
    if resolved_ticket_id:
        pub["ticket_id"] = resolved_ticket_id

    sealed = [slip_rel, *list(new_map.keys())]
    if src_rel:
        sealed.append(src_rel)
    if dom.get("project_rel_path"):
        sealed.append(str(dom["project_rel_path"]))
    return pub, sealed


def slip_list(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    proj_reg: KekProjectRegistry,
    kek: bytes,
    app_id: str,
    project_ref: Optional[str] = None,
    status_filter: Optional[str] = None,
    slip_kind_filter: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    items, rels = note_list(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        note_reg=note_reg,
        kek=kek,
        app_id=app_id,
        zone_filter="slip",
        doc_type_filter="slip",
        status_filter=status_filter,
        proj_reg=proj_reg,
        project_ref=project_ref,
    )
    ref_to_rel = {note_reg.resolve_handle(vault_ref, it["note_ref"])["note_rel_path"]: it for it in items}
    # Enrich each slip item with aliases / slip_kind / note_name safely
    enriched: List[Dict[str, Any]] = []
    kf = (slip_kind_filter or "").strip().lower()
    for rel, item in ref_to_rel.items():
        fpath = vault_dir / rel
        if not fpath.is_file():
            continue
        try:
            meta, _b, _h = extract_frontmatter(fpath.read_text(encoding="utf-8"))
        except FrontmatterError:
            meta = {}
        sk = str(meta.get("slip_kind") or "explain").strip().lower()
        if kf and sk != kf:
            continue
        row = dict(item)
        row["note_name"] = fpath.stem
        row["slip_kind"] = sk
        row["aliases"] = meta.get("aliases") or []
        if meta.get("parent_slip"):
            row["is_nested_slip"] = True
        enriched.append(row)
    return enriched, rels


def slip_get(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_ref: str,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
) -> Tuple[Dict[str, Any], List[str]]:
    rec = note_reg.resolve_handle(vault_ref, note_ref)
    rel = str(rec["note_rel_path"])
    meta, _body, _has_fm, _txt = _require_slip_note(vault_dir, rel, note_ref)
    pub, all_rels = note_get(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        note_ref=note_ref,
        note_reg=note_reg,
        kek=kek,
        app_id=app_id,
    )
    mapping = note_reg.list_vault_handles(vault_ref)
    stem_to_ref = {Path(r).stem: nref for r, nref in mapping.items()}
    pub["note_name"] = Path(rel).stem
    pub["slip_kind"] = str(meta.get("slip_kind") or "explain")
    if meta.get("source_url"):
        pub["source_url"] = str(meta["source_url"])
    for key, out_key in (("source_note", "source_note_ref"), ("parent_slip", "parent_slip_ref")):
        raw = str(meta.get(key) or "").strip()
        if raw:
            links = extract_wikilinks(raw)
            if links and links[0] in stem_to_ref:
                pub[out_key] = stem_to_ref[links[0]]
    return pub, all_rels


def slip_update(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_ref: str,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    title: Optional[str] = None,
    summary: Optional[str] = None,
    tags: Any = None,
    aliases: Any = None,
    slip_kind: Optional[str] = None,
    status: Optional[str] = None,
    content: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    rec = note_reg.resolve_handle(vault_ref, note_ref)
    rel = str(rec["note_rel_path"])
    fpath = vault_dir / rel
    meta, body, has_fm, old_txt = _require_slip_note(vault_dir, rel, note_ref)

    new_title = title.strip() if title is not None else str(meta.get("title") or fpath.stem)
    new_summary = summary.strip() if summary is not None else str(meta.get("summary") or new_title)
    new_tags = _parse_list_arg(tags) if tags is not None else list(meta.get("tags") or ["Slip"])
    new_aliases = _parse_list_arg(aliases) if aliases is not None else list(meta.get("aliases") or [])
    new_status = status.strip().lower() if status is not None else str(meta.get("status") or "active")
    new_kind = slip_kind.strip().lower() if slip_kind is not None else str(meta.get("slip_kind") or "explain")
    if new_kind not in ALLOWED_SLIP_KINDS:
        raise VaultStoreError("E_PARAM_INVALID", f"不支持的 slip_kind: {new_kind}", exit_code=1)

    tags_yaml = ", ".join(f'"{fm_scalar(t)}"' for t in new_tags)
    aliases_yaml = ", ".join(f'"{fm_scalar(a)}"' for a in new_aliases)
    updates: List[Tuple[str, str]] = [
        ("doc_type", "slip"),
        ("title", f'"{fm_scalar(new_title)}"'),
        ("summary", f'"{fm_scalar(new_summary or new_title)}"'),
        ("status", new_status),
        ("slip_kind", new_kind),
        ("tags", f"[{tags_yaml}]"),
        ("aliases", f"[{aliases_yaml}]"),
        ("updated", _today()),
    ]
    if has_fm:
        rendered = _update_frontmatter_preserving_extras(old_txt, updates, content, new_title)
    else:
        body_final = content.strip() if content is not None else body.strip()
        rendered = "\n".join(
            [
                "---",
                *(f"{k}: {v}" for k, v in updates),
                "---",
                "",
                body_final or f"# {new_title}",
                "",
            ]
        )

    nmeta, _, _ = extract_frontmatter(rendered)
    errs = validate_note_meta(nmeta)
    if errs:
        raise VaultStoreError("E_PARAM_INVALID", "; ".join(errs), exit_code=1)
    _atomic_write_md(fpath, rendered)
    pub = _public_note(kek=kek, app_id=app_id, vault_ref=vault_ref, note_ref=note_ref, rel=rel, meta=nmeta)
    pub["note_name"] = fpath.stem
    pub["slip_kind"] = new_kind
    pub["aliases"] = new_aliases
    return pub, [rel]


def slip_delete(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    trash_reg: KekTrashRegistry,
    ws_root: Path,
    note_ref: Optional[str] = None,
    note_refs: Any = None,
    source_note_ref: Optional[str] = None,
    marker_text: Optional[str] = None,
    marker_id: Optional[str] = None,
    apply: bool = False,
    ticket_id: Optional[str] = None,
    batch_key: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    refs_list: List[str] = []
    if note_ref:
        refs_list.append(note_ref.strip())
    for item in _parse_list_arg(note_refs):
        if item not in refs_list:
            refs_list.append(item)
    if not refs_list:
        raise VaultStoreError("E_PARAM_MISSING", "slip-delete 需要 --note_ref 或 --note_refs", exit_code=1)

    all_rels = _scan_vault_markdowns(vault_dir)
    mapping = note_reg.sync_paths(vault_ref, all_rels)
    sealed: List[str] = list(all_rels)

    targets: List[Dict[str, Any]] = []
    target_rels: set[str] = set()
    target_stems: List[str] = []
    for nref in refs_list:
        rec = note_reg.resolve_handle(vault_ref, nref)
        rel = str(rec["note_rel_path"])
        meta, _b, _h, _txt = _require_slip_note(vault_dir, rel, nref)
        stem = Path(rel).stem
        targets.append(
            {
                "note_ref": nref,
                "note_name": stem,
                "title": str(meta.get("title") or stem),
                "aliases": meta.get("aliases") or [],
            }
        )
        target_rels.add(rel)
        if stem not in target_stems:
            target_stems.append(stem)

    inbound_refs: List[Dict[str, Any]] = []
    total_inbound = 0
    for other_rel in all_rels:
        if other_rel in target_rels:
            continue
        op = vault_dir / other_rel
        otxt = op.read_text(encoding="utf-8")
        cleaned_preview, n_hits = _strip_wikilinks_fence_safe(otxt, target_stems)
        if n_hits > 0:
            try:
                ometa, _ob, _oh = extract_frontmatter(otxt)
            except FrontmatterError:
                ometa = {}
            inbound_refs.append(
                {
                    "note_ref": mapping[other_rel],
                    "note_name": op.stem,
                    "title": str(ometa.get("title") or op.stem),
                    "link_occurrences": n_hits,
                }
            )
            total_inbound += n_hits

    trigger_marker: Optional[Dict[str, Any]] = None
    src_rel: Optional[str] = None
    if source_note_ref and (marker_text or marker_id):
        srec = note_reg.resolve_handle(vault_ref, source_note_ref)
        src_rel = str(srec["note_rel_path"])
        frozen_src = _is_rel_frozen(vault_dir, src_rel)
        if frozen_src:
            raise VaultStoreError("E_HALT_FROZEN", f"源笔记域 '{frozen_src}' 已冻结", exit_code=1)
        trigger_marker = _find_target_marker(
            (vault_dir / src_rel).read_text(encoding="utf-8"),
            marker_text=marker_text,
            marker_id=marker_id,
        )

    if not apply:
        return {
            "vault_ref": vault_ref,
            "mode": "preview",
            "requires_approval": True,
            "target_count": len(targets),
            "targets": targets,
            "inbound_reference_files": len(inbound_refs),
            "inbound_references": inbound_refs,
            "total_inbound_links": total_inbound,
            "trigger_marker_present": trigger_marker is not None,
            "path_sealed": True,
        }, sealed

    # Approved apply=True: strip trigger marker if present, strip all inbound [[slip-...]] links, then trash slip files
    cleaned_files = 0
    removed_links = 0
    for item in inbound_refs:
        ref_rel = str(note_reg.resolve_handle(vault_ref, item["note_ref"])["note_rel_path"])
        fpath = vault_dir / ref_rel
        old_txt = fpath.read_text(encoding="utf-8")
        new_txt, n_rm = _strip_wikilinks_fence_safe(old_txt, target_stems)
        if n_rm > 0:
            if new_txt.startswith("---"):
                new_txt = replace_frontmatter_field(new_txt, "updated", _today())
            _atomic_write_md(fpath, new_txt)
            cleaned_files += 1
            removed_links += n_rm

    marker_removed = False
    if src_rel and trigger_marker is not None and src_rel not in target_rels:
        sp = vault_dir / src_rel
        stxt = sp.read_text(encoding="utf-8")
        cur_m = _find_target_marker(stxt, marker_id=trigger_marker["marker_id"], marker_text=trigger_marker["raw_block"])
        updated_src = stxt[: cur_m["start"]] + stxt[cur_m["end"] :]
        updated_src = re.sub(r"[ \t]+\n", "\n", updated_src)
        _atomic_write_md(sp, updated_src)
        marker_removed = True

    trash_refs: List[str] = []
    for t in targets:
        res_tr, tr_sealed = vfs_trash(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            note_reg=note_reg,
            trash_reg=trash_reg,
            note_ref=t["note_ref"],
            force=True,
            apply=True,
        )
        sealed.extend(tr_sealed)
        if res_tr.get("trash_ref"):
            trash_refs.append(str(res_tr["trash_ref"]))
        _append_journal_event(vault_dir, note_reg, vault_ref, "", f"已批准清理书签「{t['title']}」与入向双链")

    final_rels = _scan_vault_markdowns(vault_dir)
    note_reg.sync_paths(vault_ref, final_rels)

    resolved_ticket_id = ""
    if ticket_id or batch_key:
        tkey = (batch_key or f"slip-delete:{vault_ref}:{targets[0]['note_ref']}").strip()
        tinfo = ensure_slip_ticket(
            ws_root,
            vault_dir,
            task_key=tkey,
            title=f"Slip 删除清理（{len(targets)} 项）",
            summary=f"已批准软删 {len(targets)} 条书签，清理 {removed_links} 处入向双链",
            ticket_id=ticket_id or None,
        )
        resolved_ticket_id = str(tinfo.get("ticket_id") or "")
        update_slip_ticket_progress(
            ws_root,
            vault_dir,
            ticket_info=tinfo,
            note=f"已批准清理 {len(targets)} 条书签（trash_refs={','.join(trash_refs)}），清理 {cleaned_files} 篇引用文档",
            mark_done=True,
        )

    out: Dict[str, Any] = {
        "vault_ref": vault_ref,
        "mode": "deleted",
        "requires_approval": False,
        "deleted_count": len(targets),
        "trash_refs": trash_refs,
        "cleaned_files_count": cleaned_files,
        "removed_links_count": removed_links,
        "trigger_marker_removed": marker_removed,
        "path_sealed": True,
    }
    if resolved_ticket_id:
        out["ticket_id"] = resolved_ticket_id
    return out, sealed
