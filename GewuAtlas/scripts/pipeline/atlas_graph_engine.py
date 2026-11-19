#!/usr/bin/env python3
"""Slice 3 Zettelkasten graph topology, journal logging, and wikilink binder for GewuAtlas."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import re
import shutil
from typing import Any, Dict, List, Optional, Tuple

from facade.atlas_domain_scope import (
    infer_zone_and_project_rel,
    read_domain_manifest,
    resolve_domain_scope,
    resolve_zone_dir,
)
from notes.atlas_note_engine import (
    _JOURNAL_DATE_RE,
    _JOURNAL_HEADING_AI,
    _JOURNAL_HEADING_HUMAN,
    _append_journal_event,
    _atomic_write_md,
    _insert_ai_log_line,
    _locate_journal_note,
    _render_journal_note,
    _render_note_md,
    _scan_vault_markdowns,
    _slugify_filename,
    _today,
    _zone_for_rel,
)
from notes.atlas_yamlmini import (
    FrontmatterError,
    extract_frontmatter,
    extract_wikilinks,
    resolve_wikilink,
    fm_scalar,
    is_exempt_rel_path,
    replace_frontmatter_field,
    validate_note_meta,
)
from pipeline.atlas_extractor_engine import (
    _assert_not_journal,
    _domain_state_for_rel,
    _parse_frontmatter_strict,
    _require_unfrozen_domain,
    _safe_existing_vault_file,
    _write_files_tx,
)
from storage.note_kek_store import KekNoteRegistry, validate_note_rel_path
from storage.project_kek_store import KekProjectRegistry
from storage.vault_kek_store import VaultStoreError

_MOC_START = "<!-- atlas:moc-index:start -->"
_MOC_END = "<!-- atlas:moc-index:end -->"
_MOC_ZONE_ORDER = ("atomics", "library", "artifacts", "slices", "projects", "workbench", "slip", "dropzone")
_MOC_ZONE_LABELS = {
    "atomics": "常青原子卡",
    "library": "客观文献卷宗",
    "artifacts": "成果研报",
    "slices": "知识切片",
    "projects": "课题项目",
    "workbench": "工作台资料",
    "slip": "阅读批注",
    "dropzone": "待归位收集",
}
_MOC_MERMAID_INIT = (
    '%%{init: {"flowchart": {"htmlLabels": true, "curve": "basis", '
    '"nodeSpacing": 40, "rankSpacing": 48}}}%%'
)
_EPISTEMIC_LAYERS = (
    ("Layer 1 核心拓扑与理论基石", ("OBJ",)),
    ("Layer 2 工程实现与战术优化", ("SUB", "SYN")),
    ("Layer 3 前沿假说与演进探索", ("HYP",)),
)


_ATOM_TITLE_PREFIX_RE = re.compile(r"^(?:Atom|MOC|Art|Slip)-[A-Za-z0-9-]+-\d+-")


def _next_moc_seq(moc_dir: Path, pid: str) -> int:
    pat = re.compile(r"^MOC-" + re.escape(pid) + r"-(\d+)-")
    mx = 0
    if moc_dir.is_dir():
        for f in moc_dir.glob("*.md"):
            m = pat.match(f.name)
            if m:
                mx = max(mx, int(m.group(1)))
    return mx + 1
_FROZEN_NOTE_STATUSES = frozenset({"archived", "deprecated"})


def _moc_short_label(title: str, stem: str, hyp: bool = False) -> str:
    """Shorten and SVG-escape node labels for Layer 4 Mermaid mindmaps (116 moc_weaver aligned)."""
    clean = str(title or stem).strip().strip("\"'") or stem
    clean = _ATOM_TITLE_PREFIX_RE.sub("", clean) or clean
    if hyp:
        clean = "[待验证] " + clean
    limit = 15 if hyp else 16
    if len(clean) > limit:
        clean = clean[: limit - 1] + "…"
    return clean.replace('"', "#quot;").replace("<", "#lt;").replace(">", "#gt;")


def _safe_existing_note(vault_dir: Path, rel: str) -> Path:
    return _safe_existing_vault_file(vault_dir, rel)


def _validate_journal_date(raw_date: Optional[str]) -> str:
    d = (raw_date or "").strip() or _today()
    m = _JOURNAL_DATE_RE.fullmatch(d)
    if not m:
        raise VaultStoreError("E_PARAM_INVALID", "--date 必须为 YYYY-MM-DD 格式", exit_code=1)
    try:
        datetime.strptime(d, "%Y-%m-%d")
    except ValueError as exc:
        raise VaultStoreError("E_PARAM_INVALID", f"无效日期: {d}", exit_code=1) from exc
    return d


def _split_journal_sections(body: str) -> Tuple[str, str]:
    lines = body.splitlines()
    in_fence = False
    cur_section: Optional[str] = None
    human_lines: List[str] = []
    ai_lines: List[str] = []
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
        elif not in_fence:
            if stripped == _JOURNAL_HEADING_HUMAN:
                cur_section = "human"
                continue
            if stripped == _JOURNAL_HEADING_AI:
                cur_section = "ai"
                continue
            if stripped.startswith("# ") and not stripped.startswith("##"):
                cur_section = None
        if cur_section == "human":
            human_lines.append(ln)
        elif cur_section == "ai":
            ai_lines.append(ln)
    return "\n".join(human_lines).strip(), "\n".join(ai_lines).strip()


def journal_today(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    date: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    d = _validate_journal_date(date)
    existing_path, existing_rel, is_archived = _locate_journal_note(vault_dir, d)
    created = False
    if existing_path is None or existing_rel is None:
        jrel = f"05-Journal/{d}.md"
        jpath = vault_dir / jrel
        _atomic_write_md(jpath, _render_journal_note(d))
        existing_path = jpath
        existing_rel = jrel
        created = True

    raw = existing_path.read_text(encoding="utf-8")
    _, body, _ = extract_frontmatter(raw)
    human_sec, ai_sec = _split_journal_sections(body)
    rels = _scan_vault_markdowns(vault_dir)
    mapping = note_reg.sync_paths(vault_ref, rels)
    return {
        "vault_ref": vault_ref,
        "journal_ref": mapping[existing_rel],
        "date": d,
        "created": created,
        "archived": is_archived,
        "human_section": human_sec,
        "ai_section": ai_sec,
        "content": raw,
        "path_sealed": True,
    }, rels


def journal_show(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    date: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    d = _validate_journal_date(date)
    existing_path, existing_rel, is_archived = _locate_journal_note(vault_dir, d)
    if existing_path is None or existing_rel is None:
        raise VaultStoreError("E_NOT_FOUND", f"未找到日期 {d} 的日志，可用 journal-today 创建", exit_code=1)

    raw = existing_path.read_text(encoding="utf-8")
    _, body, _ = extract_frontmatter(raw)
    human_sec, ai_sec = _split_journal_sections(body)
    rels = _scan_vault_markdowns(vault_dir)
    mapping = note_reg.sync_paths(vault_ref, rels)
    return {
        "vault_ref": vault_ref,
        "journal_ref": mapping[existing_rel],
        "date": d,
        "archived": is_archived,
        "human_section": human_sec,
        "ai_section": ai_sec,
        "content": raw,
        "path_sealed": True,
    }, rels


def journal_archive(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    days: int = 14,
    dry_run: bool = False,
) -> Tuple[Dict[str, Any], List[str]]:
    if days < 0:
        raise VaultStoreError("E_PARAM_INVALID", "--level/--days 不能为负数", exit_code=1)
    today_d = datetime.strptime(_today(), "%Y-%m-%d").date()
    cutoff = today_d - timedelta(days=days)
    jdir = vault_dir / "05-Journal"
    sealed: List[str] = []
    archived_items: List[Dict[str, Any]] = []
    skipped_recent = 0

    if jdir.is_dir():
        for p in sorted(jdir.iterdir()):
            if p.is_symlink() or not p.is_file() or p.suffix.lower() != ".md":
                continue
            m = _JOURNAL_DATE_RE.fullmatch(p.stem)
            if not m:
                continue
            try:
                fdate = datetime.strptime(p.stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            if fdate >= cutoff:
                skipped_recent += 1
                continue
            yyyy, mm, _ = m.groups()
            old_rel = p.relative_to(vault_dir).as_posix()
            dst_dir = jdir / yyyy / mm
            dst_path = dst_dir / p.name
            new_rel = dst_path.relative_to(vault_dir).as_posix()
            sealed.extend([old_rel, new_rel])
            if dst_path.exists():
                raise VaultStoreError("E_COLLISION", f"日志归档目标月份目录已存在同名文件: {p.stem}", exit_code=1)
            if not dry_run:
                raw = p.read_text(encoding="utf-8")
                meta, body, has_fm = extract_frontmatter(raw)
                if not has_fm:
                    rendered = _render_note_md(
                        doc_type="journal",
                        title=f"{p.stem} 日志",
                        summary="",
                        status="",
                        tags=["Journal"],
                        aliases=[],
                        updated=p.stem,
                        body=body,
                    )
                else:
                    rendered = raw
                rendered = replace_frontmatter_field(rendered, "doc_type", "journal")
                rendered = replace_frontmatter_field(rendered, "date", f'"{p.stem}"')
                rendered = replace_frontmatter_field(rendered, "private", "true")
                if not str(meta.get("title") or "").strip():
                    rendered = replace_frontmatter_field(rendered, "title", f'"{p.stem} 日志"')
                dst_dir.mkdir(parents=True, exist_ok=True)
                _atomic_write_md(dst_path, rendered)
                p.unlink()
            archived_items.append({"date": p.stem, "ym": f"{yyyy}/{mm}"})

    rels = _scan_vault_markdowns(vault_dir)
    mapping = note_reg.sync_paths(vault_ref, rels)
    sealed.extend(rels)
    for item in archived_items:
        yyyy, mm = item["ym"].split("/")
        arc_rel = f"05-Journal/{yyyy}/{mm}/{item['date']}.md"
        if not dry_run and arc_rel in mapping:
            item["journal_ref"] = mapping[arc_rel]
    return {
        "vault_ref": vault_ref,
        "dry_run": dry_run,
        "cutoff_date": cutoff.strftime("%Y-%m-%d"),
        "days": days,
        "archived_count": len(archived_items),
        "skipped_recent_count": skipped_recent,
        "archived": archived_items,
        "path_sealed": True,
    }, sealed


def journal_append(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    content: str,
    target_note_ref: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    entry_text = content.strip()
    if not entry_text:
        raise VaultStoreError("E_PARAM_MISSING", "journal-append 需要 --content <日志内容>", exit_code=1)

    linked_stem: Optional[str] = None
    linked_ref: Optional[str] = None
    if target_note_ref:
        rec = note_reg.resolve_handle(vault_ref, target_note_ref)
        target_rel = str(rec["note_rel_path"])
        _safe_existing_note(vault_dir, target_rel)
        linked_stem = Path(target_rel).stem
        linked_ref = target_note_ref

    d = _today()
    hhmm = datetime.now().astimezone().strftime("%H:%M")
    line = f"- {hhmm} {entry_text}"
    if linked_stem:
        line += f"（[[{linked_stem}]]）"

    existing_path, existing_rel, is_archived = _locate_journal_note(vault_dir, d)
    if existing_path is None or existing_rel is None:
        jrel = f"05-Journal/{d}.md"
        jpath = vault_dir / jrel
        rendered = _render_journal_note(d, ai_line=line)
    else:
        jrel = existing_rel
        jpath = existing_path
        old = jpath.read_text(encoding="utf-8")
        rendered = _insert_ai_log_line(old, line, d)
    _atomic_write_md(jpath, rendered)

    rels = _scan_vault_markdowns(vault_dir)
    mapping = note_reg.sync_paths(vault_ref, rels)
    return {
        "vault_ref": vault_ref,
        "journal_ref": mapping[jrel],
        "date": d,
        "archived": is_archived,
        "linked_ref": linked_ref,
        "appended": True,
        "path_sealed": True,
    }, rels


def note_link(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    note_ref: str,
    target_ref: str,
    alias: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    if not target_ref:
        raise VaultStoreError("E_PARAM_MISSING", "note-link 需要 --target_ref <nte_02>", exit_code=1)
    if note_ref == target_ref:
        raise VaultStoreError("E_PARAM_INVALID", "note-link 不支持笔记自我关联", exit_code=1)

    src_rel = str(note_reg.resolve_handle(vault_ref, note_ref)["note_rel_path"])
    dst_rel = str(note_reg.resolve_handle(vault_ref, target_ref)["note_rel_path"])
    src_path = _safe_existing_note(vault_dir, src_rel)
    dst_path = _safe_existing_note(vault_dir, dst_rel)

    src_txt = src_path.read_text(encoding="utf-8")
    dst_txt = dst_path.read_text(encoding="utf-8")
    try:
        src_meta, src_body, has_fm = extract_frontmatter(src_txt)
        dst_meta, _, _ = extract_frontmatter(dst_txt)
    except FrontmatterError as exc:
        raise VaultStoreError("E_PARAM_INVALID", f"Frontmatter 解析失败: {exc}", exit_code=1) from exc

    _assert_not_journal(src_rel, src_meta)
    if _zone_for_rel(src_rel) == "attic":
        raise VaultStoreError("E_PARAM_CONFLICT", "97-Attic 废弃归档笔记禁止写入 note-link 关联", exit_code=1)
    src_pid, src_root, _ = _domain_state_for_rel(vault_dir, src_rel)
    _require_unfrozen_domain(vault_dir, {"project_id": src_pid, "domain_root": src_root})

    dst_stem = dst_path.stem
    dst_title = str(dst_meta.get("title") or dst_stem)
    alias_clean = (alias or dst_title).strip()
    # The requested handle identifies an exact file, even when titles collide.
    target = dst_rel[:-3] if dst_rel.endswith(".md") else dst_rel
    wikilink = f"[[{target}|{alias_clean}]]"
    paths = _scan_vault_markdowns(vault_dir)
    added = not any(resolve_wikilink(t, src_rel, paths) == [dst_rel] for t in extract_wikilinks(src_body))
    rendered = src_txt
    if added:
        link_line = f"- 关联卡片： {wikilink}"
        if "## 关联延伸" in src_body:
            rendered = rendered.rstrip() + f"\n{link_line}\n"
        else:
            rendered = rendered.rstrip() + f"\n\n## 关联延伸\n\n{link_line}\n"
        if has_fm:
            rendered = replace_frontmatter_field(rendered, "updated", fm_scalar(_today()))
        _atomic_write_md(src_path, rendered)

    rels = _scan_vault_markdowns(vault_dir)
    note_reg.sync_paths(vault_ref, rels)
    return {
        "vault_ref": vault_ref,
        "note_ref": note_ref,
        "target_ref": target_ref,
        "target_title": dst_title,
        "wikilink_added": added,
        "wikilink": wikilink,
        "path_sealed": True,
    }, rels


def fix_vault_links(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    proj_reg: Optional[KekProjectRegistry] = None,
    note_ref: Optional[str] = None,
    note_refs: Any = None,
    project_ref: Optional[str] = None,
    dry_run: bool = False,
) -> Tuple[Dict[str, Any], List[str]]:
    """Batch-normalize broken inline/frontmatter wikilinks (`[[nte_XX]]`, `[[title]]`, wrong slug) in one transaction."""
    from notes.atlas_note_engine import _parse_list_arg
    from notes.atlas_wikilink_tools import (
        build_vault_link_index,
        normalize_inline_wikilinks,
        split_wikilink_inner,
    )

    all_rels = _scan_vault_markdowns(vault_dir)
    mapping = note_reg.sync_paths(vault_ref, all_rels)
    index = build_vault_link_index(vault_dir, mapping)

    explicit_refs: List[str] = []
    if note_ref:
        explicit_refs.append(note_ref.strip())
    if note_refs is not None:
        for r in _parse_list_arg(note_refs):
            if r not in explicit_refs:
                explicit_refs.append(r)

    target_rels: List[str] = []
    if explicit_refs:
        for r in explicit_refs:
            rec = note_reg.resolve_handle(vault_ref, r)
            rel = str(rec["note_rel_path"])
            if rel.startswith("05-Journal/"):
                raise VaultStoreError("E_JOURNAL_PROTECTED", "05-Journal 日记原文受保护，禁止批量改写", exit_code=1)
            if rel not in target_rels:
                target_rels.append(rel)
    else:
        proj_prefix: Optional[str] = None
        if project_ref and proj_reg is not None:
            dom = resolve_domain_scope(
                vault_dir=vault_dir,
                vault_ref=vault_ref,
                proj_reg=proj_reg,
                explicit_project_ref=project_ref,
                scope="project",
            )
            proj_prefix = str(dom["project_rel_path"]) + "/"
        for rel in all_rels:
            if rel.startswith("05-Journal/"):
                continue
            if proj_prefix and not rel.startswith(proj_prefix):
                continue
            target_rels.append(rel)

    fixed_files: List[Dict[str, Any]] = []
    fixed_link_total = 0
    remaining_broken: List[Dict[str, Any]] = []
    frozen_cache: Dict[str, bool] = {}

    for rel in target_rels:
        fpath = vault_dir / rel
        if not fpath.is_file():
            continue
        raw_txt = fpath.read_text(encoding="utf-8")
        try:
            meta, _, has_fm = extract_frontmatter(raw_txt)
        except FrontmatterError:
            meta, has_fm = {}, False
        nref = mapping.get(rel) or ""
        title_val = str(meta.get("title") or fpath.stem)

        updated_txt, rewrites = normalize_inline_wikilinks(raw_txt, source_rel=rel, index=index)
        if rewrites:
            _, prel = infer_zone_and_project_rel(rel)
            if prel and prel not in frozen_cache:
                m = read_domain_manifest(vault_dir / prel, Path(prel).name)
                frozen_cache[prel] = m.get("status") in ("deprecated", "archived")
            if prel and frozen_cache.get(prel):
                if explicit_refs:
                    _require_unfrozen_domain(vault_dir, {"project_id": Path(prel).name, "domain_root": vault_dir / prel})
                continue
            if not dry_run:
                final_txt = updated_txt
                if has_fm:
                    final_txt = replace_frontmatter_field(final_txt, "updated", fm_scalar(_today()))
                _atomic_write_md(fpath, final_txt)
            fixed_link_total += len(rewrites)
            fixed_files.append(
                {
                    "note_ref": nref,
                    "title": title_val,
                    "note_name": fpath.stem,
                    "rewritten_count": len(rewrites),
                    "rewrites": rewrites[:20],
                }
            )
            raw_for_check = updated_txt
        else:
            raw_for_check = raw_txt

        for tok in extract_wikilinks(raw_for_check):
            t_name, _, _ = split_wikilink_inner(tok)
            if t_name and not resolve_wikilink(t_name, rel, mapping):
                remaining_broken.append({"source_note_ref": nref, "target_name": t_name})

    return {
        "vault_ref": vault_ref,
        "mode": "fix",
        "dry_run": dry_run,
        "scanned_files": len(target_rels),
        "fixed_file_count": len(fixed_files),
        "fixed_link_count": fixed_link_total,
        "remaining_broken_count": len(remaining_broken),
        "files": fixed_files[:50],
        "remaining_broken": remaining_broken[:30],
        "path_sealed": True,
    }, all_rels


def graph_report(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    zone_filter: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    rels = _scan_vault_markdowns(vault_dir)
    mapping = note_reg.sync_paths(vault_ref, rels)
    ref_to_rel = {ref: rel for rel, ref in mapping.items()}
    parsed: Dict[str, Dict[str, Any]] = {}

    for rel in rels:
        nref = mapping[rel]
        stem = Path(rel).stem
        txt = (vault_dir / rel).read_text(encoding="utf-8")
        try:
            meta, body, _ = extract_frontmatter(txt)
        except FrontmatterError:
            meta, body = {}, txt
        parsed[nref] = {
            "note_ref": nref,
            "zone": _zone_for_rel(rel),
            "doc_type": str(meta.get("doc_type") or ""),
            "title": str(meta.get("title") or stem),
            "status": str(meta.get("status") or ""),
            "links": extract_wikilinks(body),
        }

    out_edges: Dict[str, List[str]] = {nref: [] for nref in parsed}
    in_edges: Dict[str, List[str]] = {nref: [] for nref in parsed}
    edge_count = 0

    for nref, info in parsed.items():
        seen_dst: set[str] = set()
        for stem in info["links"]:
            candidates = resolve_wikilink(stem, ref_to_rel[nref], mapping)
            dst_ref = mapping[candidates[0]] if len(candidates) == 1 else None
            if dst_ref and dst_ref != nref and dst_ref not in seen_dst:
                seen_dst.add(dst_ref)
                out_edges[nref].append(dst_ref)
                in_edges[dst_ref].append(nref)
                edge_count += 1

    z_want = (zone_filter or "").strip().lower() or None
    nodes: List[Dict[str, Any]] = []
    orphan_refs: List[str] = []
    inbox_triage: List[Dict[str, str]] = []
    hub_candidates: List[Tuple[int, str, str]] = []

    for nref in sorted(parsed):
        info = parsed[nref]
        outs = sorted(out_edges[nref])
        ins = sorted(in_edges[nref])
        deg = len(outs) + len(ins)
        if info["zone"] != "journal":
            if deg == 0 and info["zone"] != "attic":
                orphan_refs.append(nref)
            if info["status"] == "pending" or info["zone"] in ("dropzone", "slices"):
                inbox_triage.append(
                    {
                        "note_ref": nref,
                        "title": info["title"],
                        "zone": info["zone"],
                        "status": info["status"] or "unspecified",
                    }
                )
        if deg > 0:
            hub_candidates.append((deg, nref, info["title"]))
        if z_want and info["zone"] != z_want:
            continue
        nodes.append(
            {
                "note_ref": nref,
                "title": info["title"],
                "zone": info["zone"],
                "doc_type": info["doc_type"],
                "status": info["status"],
                "out_degree": len(outs),
                "in_degree": len(ins),
                "out_refs": outs,
                "in_refs": ins,
            }
        )

    hub_candidates.sort(key=lambda t: (-t[0], t[1]))
    hubs = [{"note_ref": r, "title": t, "degree": d} for d, r, t in hub_candidates[:10]]
    return {
        "vault_ref": vault_ref,
        "total_notes": len(parsed),
        "returned_nodes": len(nodes),
        "edge_count": edge_count,
        "orphan_count": len(orphan_refs),
        "orphan_refs": orphan_refs,
        "inbox_triage_count": len(inbox_triage),
        "inbox_triage": inbox_triage,
        "hubs": hubs,
        "nodes": nodes,
        "path_sealed": True,
    }, rels


def _ensure_private_cache(
    vault_dir: Path,
    rel: str,
    cache: Dict[str, bool],
) -> bool:
    """Return True if note belongs to a project domain whose manifest marks private: true."""
    _, prel = infer_zone_and_project_rel(rel)
    if not prel:
        return False
    if prel not in cache:
        manifest = read_domain_manifest(vault_dir / prel, Path(prel).name)
        cache[prel] = bool(manifest.get("private") is True)
    return cache[prel]


def moc_sync(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    title: str = "",
    name: Optional[str] = None,
    tag_filter: Optional[str] = None,
    topic_filter: Optional[str] = None,
    prism_filter: Optional[str] = None,
    zone_filter: Optional[str] = None,
    target_note_ref: Optional[str] = None,
    proj_reg: Optional[KekProjectRegistry] = None,
    project_ref: Optional[str] = None,
    scope: str = "auto",
    by_actor: str = "",
    force: bool = False,
    dry_run: bool = False,
) -> Tuple[Dict[str, Any], List[str]]:
    """Weave an epistemic L1..L4 MOC (preserving human commentary outside managed markers) with 116-aligned guards & TX rollback."""
    rels = _scan_vault_markdowns(vault_dir)
    mapping = note_reg.sync_paths(vault_ref, rels)

    tag_want = (tag_filter or "").strip().casefold() or None
    topic_raw = (topic_filter or prism_filter or "").strip()
    topic_want = topic_raw.casefold() or None
    zone_want = (zone_filter or "").strip().lower() or None
    actor = (by_actor or "").strip()

    existing_moc_rel: Optional[str] = None
    existing_moc_txt = ""
    moc_meta: Dict[str, Any] = {}
    existing_read_status = "unread"

    if target_note_ref:
        rec = note_reg.resolve_handle(vault_ref, target_note_ref)
        existing_moc_rel = str(rec["note_rel_path"])
        moc_path = _safe_existing_note(vault_dir, existing_moc_rel)
        existing_moc_txt = moc_path.read_text(encoding="utf-8")
        moc_meta, _ = _parse_frontmatter_strict(existing_moc_txt, label="目标 MOC")
        _assert_not_journal(existing_moc_rel, moc_meta)
        if is_exempt_rel_path(existing_moc_rel) or _zone_for_rel(existing_moc_rel) == "attic":
            raise VaultStoreError("E_PARAM_INVALID", "系统文件或 97-Attic 笔记不能执行 moc-sync", exit_code=1)
        if str(moc_meta.get("doc_type") or "").strip().lower() != "moc" and _zone_for_rel(existing_moc_rel) != "moc":
            raise VaultStoreError("E_PARAM_INVALID", f"句柄 {target_note_ref} 并非 MOC 主题地图卡片", exit_code=1)
        if str(moc_meta.get("status") or "").strip().lower() in _FROZEN_NOTE_STATUSES:
            raise VaultStoreError("E_PARAM_CONFLICT", "已归档或已废弃 MOC 不允许重新织网", exit_code=1)
        if moc_meta.get("read_status") in ("read", "unread"):
            existing_read_status = str(moc_meta["read_status"])

    dom: Optional[Dict[str, Any]] = None
    if proj_reg is not None:
        dom = resolve_domain_scope(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            proj_reg=proj_reg,
            explicit_project_ref=project_ref,
            scope="project" if project_ref and scope == "auto" else scope,
            source_rel_path=existing_moc_rel,
            source_meta=moc_meta if existing_moc_rel else None,
        )

    if dom is not None and dom.get("scope") == "project" and dom.get("project_rel_path"):
        own_pid = str(dom["project_id"])
        own_root = vault_dir / str(dom["project_rel_path"])
        dom_manifest = read_domain_manifest(own_root, own_pid)
        moc_base_rel = resolve_zone_dir(vault_dir, dom, "moc").relative_to(vault_dir).as_posix()
        proj_prefix: Optional[str] = str(dom["project_rel_path"]) + "/"
    elif existing_moc_rel:
        own_pid, own_root, dom_manifest = _domain_state_for_rel(vault_dir, existing_moc_rel)
        _, prel = infer_zone_and_project_rel(existing_moc_rel)
        moc_base_rel = f"{prel}/20-Memory/23-MOC" if prel else "20-AI-Zone/20-Memory/23-MOC"
        proj_prefix = (prel + "/") if prel else None
    else:
        own_pid, own_root, dom_manifest = _domain_state_for_rel(vault_dir, "20-AI-Zone/20-Memory/23-MOC/index.md")
        moc_base_rel = (
            resolve_zone_dir(vault_dir, dom, "moc").relative_to(vault_dir).as_posix()
            if dom is not None
            else "20-AI-Zone/20-Memory/23-MOC"
        )
        proj_prefix = None

    _require_unfrozen_domain(vault_dir, {"project_id": own_pid, "domain_root": own_root})
    dom_is_private = bool(dom_manifest.get("private") is True)

    explicit_title = (title or "").strip() or (name or "").strip()
    if existing_moc_rel and target_note_ref:
        resolved_title = explicit_title or str(moc_meta.get("title") or Path(existing_moc_rel).stem)
    else:
        filter_seed = topic_raw or (tag_filter or "").strip()
        if not explicit_title and not filter_seed:
            raise VaultStoreError(
                "E_PARAM_MISSING",
                "moc-sync 新建地图必须显式给出主题名称（--title/--name）或筛选主题（--topic/--prism/--tag）；域只是容器，算子不做域名兜底",
                exit_code=1,
            )
        resolved_title = explicit_title or f"{filter_seed} 主题地图"
        clean_theme = re.sub(r"^MOC-", "", resolved_title, flags=re.I)
        theme_slug = _slugify_filename(clean_theme)
        moc_dir = vault_dir / moc_base_rel
        matched_existing: Optional[Path] = None
        if moc_dir.is_dir():
            for cand in sorted(moc_dir.glob("*.md")):
                if cand.is_symlink() or not cand.is_file():
                    continue
                if cand.stem in (theme_slug, _slugify_filename(resolved_title)):
                    matched_existing = cand
                    break
                try:
                    cm, _, cfm = extract_frontmatter(cand.read_text(encoding="utf-8"))
                    if cfm and str(cm.get("title") or "").strip().casefold() == resolved_title.casefold():
                        matched_existing = cand
                        break
                except Exception:
                    continue
        if matched_existing is not None:
            candidate_rel = validate_note_rel_path(matched_existing.relative_to(vault_dir).as_posix())
            _safe_existing_note(vault_dir, candidate_rel)
            if not force:
                raise VaultStoreError(
                    "E_PARAM_CONFLICT",
                    f"同名 MOC 已存在（{matched_existing.stem}）：请传入 --note_ref 同步受管索引，或显式传 --force true 允许覆盖同步",
                    exit_code=1,
                )
            existing_moc_rel = candidate_rel
            existing_moc_txt = matched_existing.read_text(encoding="utf-8")
            moc_meta, _ = _parse_frontmatter_strict(existing_moc_txt, label="同名 MOC")
            if str(moc_meta.get("status") or "").strip().lower() in _FROZEN_NOTE_STATUSES:
                raise VaultStoreError("E_PARAM_CONFLICT", "已归档或已废弃 MOC 不允许重新织网", exit_code=1)
            if moc_meta.get("read_status") in ("read", "unread"):
                existing_read_status = str(moc_meta["read_status"])

    grouped: Dict[str, List[Tuple[str, str, str, str, str]]] = {z: [] for z in _MOC_ZONE_ORDER}
    included_refs: List[str] = []
    included_stems: set[str] = set()
    all_atomic_rels: List[Tuple[str, Dict[str, Any]]] = []
    private_proj_cache: Dict[str, bool] = {}

    for rel in rels:
        if existing_moc_rel and rel == existing_moc_rel:
            continue
        if "/91-Temp/" in f"/{rel}/":
            continue
        if proj_prefix:
            if not rel.startswith(proj_prefix):
                continue
        else:
            if not rel.startswith("20-AI-Zone/"):
                continue
        zone = _zone_for_rel(rel)
        if zone in ("journal", "attic", "moc"):
            continue
        note_path = vault_dir / rel
        if note_path.is_symlink() or not note_path.is_file():
            continue
        txt = note_path.read_text(encoding="utf-8")
        try:
            meta, body, has_fm = extract_frontmatter(txt)
        except FrontmatterError:
            continue
        if not has_fm:
            continue

        is_atomic_card = zone == "atomics" and str(meta.get("doc_type") or "").strip().lower() == "atomic"
        if is_atomic_card:
            all_atomic_rels.append((rel, meta))
        if zone_want and zone != zone_want:
            continue
        if meta.get("private") is True:
            continue
        if not proj_prefix and _ensure_private_cache(vault_dir, rel, private_proj_cache):
            continue

        status = str(meta.get("status") or "").strip().lower()
        if is_atomic_card:
            if status != "active":
                continue
        elif status and status != "active":
            continue

        tags = [str(t).strip() for t in (meta.get("tags") or []) if str(t).strip()]
        if tag_want and not any(t.casefold() == tag_want for t in tags):
            continue
        aliases = [str(a).strip() for a in (meta.get("aliases") or []) if str(a).strip()]
        stem = Path(rel).stem
        note_title = str(meta.get("title") or stem).strip()
        summary = str(meta.get("summary") or note_title).strip()
        if topic_want:
            hay = " ".join([note_title, summary, stem, body, *tags, *aliases]).casefold()
            if topic_want not in hay:
                continue
        ep_type = str(meta.get("type") or "").strip().upper() or "SYN"
        grouped.setdefault(zone, []).append((note_title, stem, summary, ep_type, rel))
        included_refs.append(mapping[rel])
        included_stems.add(stem)

    warnings: List[str] = []
    if explicit_title and not tag_want and not topic_want and not zone_want:
        warnings.append(
            f"未设 --topic/--tag 筛选，本次索引收录当前域内全部 {len(included_refs)} 张活跃卡片，请核对主题名与收录范围。"
        )

    index_lines: List[str] = [_MOC_START, f"## 自动索引（更新日期：{_today()}）"]
    filter_parts: List[str] = []
    if topic_raw:
        filter_parts.append(f"主题关键词={topic_raw}")
    if tag_filter and tag_filter.strip():
        filter_parts.append(f"标签={tag_filter.strip()}")
    if zone_want:
        filter_parts.append(f"分区={zone_want}")
    if filter_parts:
        index_lines.extend(["", f"> [!NOTE] 筛选条件：{' | '.join(filter_parts)}"])

    section_counts: Dict[str, int] = {}
    for z in _MOC_ZONE_ORDER:
        if grouped.get(z):
            section_counts[z] = len(grouped[z])

    # Epistemic layering for atomics (116 moc_weaver Layer 1..3 + Layer 4 Mermaid)
    atomic_entries = sorted(grouped.get("atomics", []), key=lambda item: item[0])
    layer_buckets: List[List[Tuple[str, str, str, str, str]]] = [[], [], []]
    for item in atomic_entries:
        ep = item[3]
        if ep == "OBJ":
            layer_buckets[0].append(item)
        elif ep == "HYP":
            layer_buckets[2].append(item)
        else:
            layer_buckets[1].append(item)

    node_records: List[Tuple[str, str, int]] = []
    if atomic_entries:
        for gi, (layer_title, _types) in enumerate(_EPISTEMIC_LAYERS):
            index_lines.append("")
            index_lines.append(f"### {layer_title}")
            index_lines.append("")
            if not layer_buckets[gi]:
                index_lines.append("*(暂无)*")
                continue
            for note_title, stem, summary, _ep, _rel in layer_buckets[gi]:
                display = f"[[{stem}|{note_title}]]" if note_title != stem else f"[[{stem}]]"
                index_lines.append(f"- {display}：{summary}")
                node_records.append((f"n{len(node_records)}", _moc_short_label(note_title, stem, hyp=(gi == 2)), gi))

        index_lines.extend(["", "### Layer 4 全景逻辑流向脑图", "", "```mermaid", _MOC_MERMAID_INIT, "flowchart LR"])
        root_lbl = _moc_short_label(resolved_title, Path(resolved_title).stem)
        index_lines.append(f'    ROOT["{root_lbl}"]')
        for gi, (layer_title, _types) in enumerate(_EPISTEMIC_LAYERS):
            members = [(nid, nlbl) for nid, nlbl, lidx in node_records if lidx == gi]
            if not members:
                continue
            short_layer = layer_title.split(" ", 2)[-1]
            index_lines.append(f'    subgraph L{gi}["{short_layer}"]')
            for nid, nlbl in members:
                index_lines.append(f'        {nid}["{nlbl}"]')
            index_lines.append("    end")
        for nid, _nlbl, _lidx in node_records:
            index_lines.append(f"    ROOT --> {nid}")
        index_lines.append("```")
    else:
        index_lines.extend(["", "### Layer 4 全景逻辑流向脑图", "", "*(暂无常青原子卡，跳过脑图)*"])

    for zone in _MOC_ZONE_ORDER:
        if zone == "atomics":
            continue
        entries = grouped.get(zone, [])
        if not entries:
            continue
        entries.sort(key=lambda item: item[0])
        label = _MOC_ZONE_LABELS.get(zone, zone)
        index_lines.append("")
        index_lines.append(f"### {label}")
        index_lines.append("")
        for note_title, stem, summary, _ep, _rel in entries:
            display = f"[[{stem}|{note_title}]]" if note_title != stem else f"[[{stem}]]"
            index_lines.append(f"- {display}：{summary}")

    if not section_counts:
        index_lines.extend(["", "- 暂无符合筛选条件的活跃卡片。"])
    index_lines.append("")
    index_lines.append(_MOC_END)
    managed_block = "\n".join(index_lines)

    obj_c = len(layer_buckets[0])
    sub_syn_c = len(layer_buckets[1])
    hyp_c = len(layer_buckets[2])
    unmapped_c = sum(
        1 for a_rel, a_meta in all_atomic_rels
        if Path(a_rel).stem not in included_stems and not a_meta.get("map")
    )
    gaps_hint: List[str] = []
    if obj_c == 0:
        gaps_hint.append("缺少 OBJ 客观事实/实证基准卡，建议补充一手基准数据或协议规范以夯实逻辑底座")
    if sub_syn_c == 0:
        gaps_hint.append("缺少 SUB/SYN 机制权衡或综合演进卡，建议补充架构对比与适用边界分析")
    if hyp_c > 0:
        gaps_hint.append(f"存在 {hyp_c} 张待证假说（HYP）尚未实证闭环，建议设计验证实验或补充反例证据")
    if unmapped_c > 0:
        gaps_hint.append(f"同域尚有 {unmapped_c} 张未归属任何 MOC 的孤岛原子卡，可运行 moc-sync --mode roam 检视是否应纳入或另建图谱")
    closure_signals: Dict[str, Any] = {
        "obj_count": obj_c,
        "sub_syn_count": sub_syn_c,
        "hyp_count": hyp_c,
        "unmapped_atomic_count": unmapped_c,
        "gaps_hint": gaps_hint,
    }
    gap_bullets = "\n".join(f"- {g}" for g in gaps_hint) if gaps_hint else "- （结构分层完整，请 Agent 进一步核查是否缺少反例边界、工程失效案例或上游理论衔接）"
    closing_section = (
        "## 逻辑闭环评估与扩展研究建议\n\n"
        "### 1. 逻辑闭环自检\n"
        f"- 结构体检信号：OBJ 事实底座={obj_c}、SUB/SYN 机制权衡={sub_syn_c}、HYP 待证假说={hyp_c}、同域未入图原子卡={unmapped_c}。\n"
        "- （待 Agent 结合已读卡片，说明当前论证链条哪些环节已闭环、哪些环节仍存在逻辑断层）\n\n"
        "### 2. 形成逻辑闭环还缺哪些内容（Knowledge Gaps）\n"
        f"{gap_bullets}\n\n"
        "### 3. 建议扩展研究内容（Next Research Directions）\n"
        "- （待 Agent 提出 2~4 条具体可执行的扩展研究或文献采集倡议，供人类确认后启动下一轮调研）\n"
    )

    raw_summary = f"{resolved_title}，自动聚合 {len(included_refs)} 张活跃卡片"
    summary_txt = raw_summary if len(raw_summary) <= 200 else raw_summary[:197] + "…"

    if existing_moc_rel and existing_moc_txt:
        target_rel = existing_moc_rel
        target_path = vault_dir / target_rel
        updated_txt = replace_frontmatter_field(existing_moc_txt, "updated", fm_scalar(_today()))
        updated_txt = replace_frontmatter_field(updated_txt, "summary", f'"{fm_scalar(summary_txt)}"')
        if explicit_title:
            updated_txt = replace_frontmatter_field(updated_txt, "title", f'"{fm_scalar(resolved_title)}"')
        if "read_status" not in moc_meta:
            updated_txt = replace_frontmatter_field(updated_txt, "read_status", existing_read_status)
        if str(moc_meta.get("type") or "").strip().upper() != "SYN":
            updated_txt = replace_frontmatter_field(updated_txt, "type", "SYN")
        if dom_is_private and moc_meta.get("private") is not True:
            updated_txt = replace_frontmatter_field(updated_txt, "private", "true")
            updated_txt = replace_frontmatter_field(updated_txt, "private_since", _today())
        if actor:
            updated_txt = replace_frontmatter_field(updated_txt, "updated_by", f'"{fm_scalar(actor)}"')
        if _MOC_START in updated_txt and _MOC_END in updated_txt:
            pre, rest = updated_txt.split(_MOC_START, 1)
            _, post = rest.split(_MOC_END, 1)
            post_clean = post.lstrip("\n")
            if "逻辑闭环评估" not in post_clean:
                post_clean = (post_clean.rstrip() + "\n\n" + closing_section) if post_clean.strip() else closing_section
            rendered = pre.rstrip() + "\n\n" + managed_block + "\n\n" + post_clean
        else:
            rendered = updated_txt.rstrip() + "\n\n" + managed_block + "\n\n" + closing_section
    else:
        moc_pid_tok = own_pid if proj_prefix else "AI-Zone"
        clean_theme_new = re.sub(r"^MOC-", "", resolved_title, flags=re.I)
        theme_slug_new = _slugify_filename(clean_theme_new)
        moc_seq = _next_moc_seq(vault_dir / moc_base_rel, moc_pid_tok)
        stem = f"MOC-{moc_pid_tok}-{moc_seq:03d}-{theme_slug_new}"
        target_rel = validate_note_rel_path(f"{moc_base_rel}/{stem}.md")
        target_path = vault_dir / target_rel
        moc_tags = ["MOC"]
        if tag_filter and tag_filter.strip() and tag_filter.strip() not in moc_tags:
            moc_tags.append(tag_filter.strip())
        rendered = _render_note_md(
            doc_type="moc",
            title=resolved_title,
            summary=summary_txt,
            status="active",
            tags=moc_tags,
            aliases=[],
            updated=_today(),
            body=f"# {resolved_title}\n\n{managed_block}\n\n{closing_section}",
        )
        rendered = replace_frontmatter_field(rendered, "read_status", "unread")
        rendered = replace_frontmatter_field(rendered, "type", "SYN")
        if proj_prefix:
            rendered = replace_frontmatter_field(rendered, "project_id", f'"{fm_scalar(own_pid)}"')
        if dom_is_private:
            rendered = replace_frontmatter_field(rendered, "private", "true")
            rendered = replace_frontmatter_field(rendered, "private_since", _today())
        if actor:
            rendered = replace_frontmatter_field(rendered, "created_by", f'"{fm_scalar(actor)}"')
            rendered = replace_frontmatter_field(rendered, "updated_by", f'"{fm_scalar(actor)}"')

    fmeta, _, _ = extract_frontmatter(rendered)
    meta_errs = validate_note_meta(fmeta, rel_path=target_rel)
    if meta_errs:
        raise VaultStoreError("E_PARAM_INVALID", f"MOC 元数据校验失败: {'; '.join(meta_errs)}", exit_code=1)

    tx_changes: Dict[Path, str] = {target_path: rendered}
    own_moc_link = f"[[{Path(target_rel).stem}]]"
    map_added_count = 0
    map_removed_count = 0

    for a_rel, a_meta in all_atomic_rels:
        a_path = _safe_existing_note(vault_dir, a_rel)
        a_stem = Path(a_rel).stem
        raw_maps = a_meta.get("map")
        cur_maps = (
            [str(m).strip() for m in raw_maps if str(m).strip()]
            if isinstance(raw_maps, list)
            else ([str(raw_maps).strip()] if isinstance(raw_maps, str) and str(raw_maps).strip() else [])
        )
        if a_stem in included_stems:
            next_maps = cur_maps if own_moc_link in cur_maps else [*cur_maps, own_moc_link]
        else:
            next_maps = [m for m in cur_maps if m != own_moc_link]
        if next_maps != cur_maps:
            if len(next_maps) > len(cur_maps):
                map_added_count += 1
            else:
                map_removed_count += 1
            a_txt = a_path.read_text(encoding="utf-8")
            a_updated = replace_frontmatter_field(a_txt, "map", json.dumps(next_maps, ensure_ascii=False))
            tx_changes[a_path] = a_updated

    effective_private = bool(fmeta.get("private") is True)
    effective_read_status = str(fmeta.get("read_status") or existing_read_status)

    if dry_run:
        return {
            "vault_ref": vault_ref,
            "moc_ref": mapping.get(target_rel),
            "title": resolved_title,
            "domain_scope": "project" if proj_prefix else "ai-zone",
            **({"project_id": own_pid} if proj_prefix else {}),
            "dry_run": True,
            "processing_stage": "dry_run_preview",
            "semantic_synthesis": "not_evaluated",
            "read_status": effective_read_status,
            "private": effective_private,
            "matched_count": len(included_refs),
            "included_refs": sorted(included_refs),
            "map_added_count": map_added_count,
            "map_removed_count": map_removed_count,
            "will_write_files": len(tx_changes),
            "section_counts": section_counts,
            "closure_signals": closure_signals,
            "tag_filter": tag_filter.strip() if tag_filter and tag_filter.strip() else None,
            "topic_filter": topic_raw or None,
            "zone_filter": zone_want,
            "warnings": warnings,
            "path_sealed": True,
        }, rels

    _write_files_tx(vault_dir, tx_changes)

    final_rels = _scan_vault_markdowns(vault_dir)
    final_map = note_reg.sync_paths(vault_ref, final_rels)
    moc_ref = final_map[target_rel]
    _append_journal_event(vault_dir, note_reg, vault_ref, Path(target_rel).stem, f"同步 MOC 索引（纳入 {len(included_refs)} 张）")
    final_rels = _scan_vault_markdowns(vault_dir)

    return {
        "vault_ref": vault_ref,
        "moc_ref": moc_ref,
        "title": resolved_title,
        "domain_scope": "project" if proj_prefix else "ai-zone",
        **({"project_id": own_pid} if proj_prefix else {}),
        "dry_run": False,
        "processing_stage": "index_synced",
        "semantic_synthesis": "not_evaluated",
        "read_status": effective_read_status,
        "private": effective_private,
        "matched_count": len(included_refs),
        "included_refs": sorted(included_refs),
        "map_added_count": map_added_count,
        "map_removed_count": map_removed_count,
        "written_file_count": len(tx_changes),
        "section_counts": section_counts,
        "closure_signals": closure_signals,
        "tag_filter": tag_filter.strip() if tag_filter and tag_filter.strip() else None,
        "topic_filter": topic_raw or None,
        "zone_filter": zone_want,
        "warnings": warnings,
        "path_sealed": True,
    }, final_rels


_GENERIC_ROAM_TAGS = {"atomic", "project", "note", "moc", "obj", "sub", "syn", "hyp", "workbench", "library"}


def moc_roam_discover(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    proj_reg: Optional[Any] = None,
    project_ref: Optional[str] = None,
    scope: str = "auto",
    min_cluster_size: int = 3,
) -> Tuple[Dict[str, Any], List[str]]:
    """Read-only autonomous roaming discovery: cluster unmapped active atomics by shared tags and wikilinks."""
    rels = _scan_vault_markdowns(vault_dir)
    mapping = note_reg.sync_paths(vault_ref, rels)
    dom = resolve_domain_scope(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        proj_reg=proj_reg,
        explicit_project_ref=project_ref,
        scope=scope,
    )
    dom_prefix = Path(dom["domain_root"]).relative_to(vault_dir).as_posix().rstrip("/") + "/"
    dom_pid = str(dom["project_id"])

    unmapped: Dict[str, Dict[str, Any]] = {}
    stem_to_rel: Dict[str, str] = {}
    for rel in rels:
        if not rel.startswith(dom_prefix) or _zone_for_rel(rel) != "atomics":
            continue
        fp = _safe_existing_note(vault_dir, rel)
        try:
            raw = fp.read_text(encoding="utf-8")
            meta, _, has_fm = extract_frontmatter(raw)
        except Exception:
            continue
        if not has_fm or str(meta.get("status") or "").strip().lower() != "active":
            continue
        raw_map = meta.get("map")
        has_map = bool(raw_map and ((isinstance(raw_map, list) and any(str(x).strip() for x in raw_map)) or (isinstance(raw_map, str) and raw_map.strip())))
        if has_map:
            continue
        tags = [str(t).strip() for t in (meta.get("tags") or []) if str(t).strip() and str(t).strip().lower() not in _GENERIC_ROAM_TAGS]
        unmapped[rel] = {
            "rel": rel,
            "stem": Path(rel).stem,
            "note_ref": mapping.get(rel),
            "title": str(meta.get("title") or Path(rel).stem),
            "summary": str(meta.get("summary") or ""),
            "type": str(meta.get("type") or "SUB").upper(),
            "tags": tags,
            "links": set(extract_wikilinks(raw)),
        }
        stem_to_rel[Path(rel).stem] = rel

    adj: Dict[str, set[str]] = {r: set() for r in unmapped}
    by_tag: Dict[str, List[str]] = {}
    for r, info in unmapped.items():
        for t in info["tags"]:
            by_tag.setdefault(t.lower(), []).append(r)
        for lk in info["links"]:
            tgt = stem_to_rel.get(Path(lk).stem)
            if tgt and tgt != r:
                adj[r].add(tgt)
                adj[tgt].add(r)
    for members in by_tag.values():
        if len(members) >= 2:
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    adj[members[i]].add(members[j])
                    adj[members[j]].add(members[i])

    visited: set[str] = set()
    candidate_clusters: List[Dict[str, Any]] = []
    proto_clusters: List[Dict[str, Any]] = []
    eff_min = max(2, int(min_cluster_size or 3))

    for r in sorted(unmapped):
        if r in visited:
            continue
        comp: List[str] = []
        stack = [r]
        visited.add(r)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nb in sorted(adj[cur]):
                if nb not in visited:
                    visited.add(nb)
                    stack.append(nb)
        if len(comp) < 2:
            continue
        tag_freq: Dict[str, int] = {}
        ep_counts = {"OBJ": 0, "SUB": 0, "SYN": 0, "HYP": 0}
        for cr in comp:
            info = unmapped[cr]
            ep = info["type"] if info["type"] in ep_counts else "SUB"
            ep_counts[ep] += 1
            for t in info["tags"]:
                tag_freq[t] = tag_freq.get(t, 0) + 1
        comp_sorted = sorted(comp, key=lambda x: (-len(adj[x]), x))
        hub = unmapped[comp_sorted[0]]
        top_tag = max(tag_freq.items(), key=lambda kv: (kv[1], kv[0]))[0] if tag_freq else ""
        entry = {
            "suggested_topic": f"{top_tag}知识地图" if top_tag else f"{hub['title']}主题地图",
            "suggested_tag": top_tag or None,
            "size": len(comp),
            "hub_note_ref": hub["note_ref"],
            "hub_title": hub["title"],
            "epistemic_breakdown": ep_counts,
            "member_refs": [unmapped[cr]["note_ref"] for cr in comp_sorted if unmapped[cr].get("note_ref")],
            "members": [
                {"note_ref": unmapped[cr]["note_ref"], "title": unmapped[cr]["title"], "type": unmapped[cr]["type"], "summary": unmapped[cr]["summary"]}
                for cr in comp_sorted
            ],
        }
        if len(comp) >= eff_min:
            candidate_clusters.append(entry)
        else:
            proto_clusters.append(entry)

    candidate_clusters.sort(key=lambda c: (-int(c["size"]), str(c["suggested_topic"]))) 
    return {
        "vault_ref": vault_ref,
        "mode": "roam",
        "domain_scope": dom["scope"],
        "project_id": dom_pid,
        "min_cluster_size": eff_min,
        "unmapped_atomic_count": len(unmapped),
        "candidate_count": len(candidate_clusters),
        "candidate_clusters": candidate_clusters,
        "proto_clusters": proto_clusters,
        "path_sealed": True,
    }, rels
