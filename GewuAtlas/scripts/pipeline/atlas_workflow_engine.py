#!/usr/bin/env python3
"""DropZone routing and Artifact adoption/finalization workflows for GewuAtlas (Slice 5)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from facade.atlas_domain_scope import (
    infer_zone_and_project_rel,
    resolve_domain_scope,
    resolve_zone_dir,
    scan_project_rels,
)
from notes.atlas_note_engine import (
    _append_journal_event,
    _atomic_write_md,
    _cascade_note_moves,
    _preflight_note_moves,
    _first_meaningful_sentence,
    _public_note,
    _render_note_md,
    _scan_vault_markdowns,
    _slugify_filename,
    _today,
)
from notes.atlas_yamlmini import (
    FrontmatterError,
    extract_frontmatter,
    extract_wikilinks,
    fm_scalar,
    replace_frontmatter_field,
    validate_note_meta,
)
from pipeline.atlas_extractor_engine import (
    _assert_not_journal,
    _domain_state_for_rel,
    _require_unfrozen_domain,
    _safe_existing_vault_file,
)
from storage.note_kek_store import KekNoteRegistry, validate_note_rel_path
from storage.project_kek_store import KekProjectRegistry
from collab.ticket_bridge import ensure_doc_ticket, record_artifact_ticket_audit, update_doc_ticket_progress
from storage.vault_kek_store import VaultStoreError


def _require_admission_storage(root: Path, *paths: Path) -> None:
    """Admit artifact storage only on verified local macOS APFS volume without symlink traversal (116 core/safety.py)."""
    import ctypes
    import sys

    if sys.platform != "darwin":
        return
    info = ctypes.create_string_buffer(4096)
    statfs = ctypes.CDLL(None, use_errno=True).statfs
    statfs.argtypes = (ctypes.c_char_p, ctypes.c_void_p)
    err = "成果入舱仅开放于已验收的本机 APFS 卷"
    if statfs(bytes(root), info) != 0 or info.raw[72:88].split(b"\0", 1)[0] != b"apfs":
        raise VaultStoreError("E_STORAGE_GUARD", err, exit_code=1)
    device = root.stat().st_dev
    for p in paths:
        cur = p
        if cur.is_symlink():
            raise VaultStoreError("E_STORAGE_GUARD", err, exit_code=1)
        while not cur.exists():
            if cur == cur.parent:
                break
            cur = cur.parent
        if cur.stat().st_dev != device:
            raise VaultStoreError("E_STORAGE_GUARD", err, exit_code=1)


def _derive_artifact_sources(body: str, own_title: str, existing: Any = None) -> List[str]:
    if isinstance(existing, list) and existing:
        out: List[str] = []
        for item in existing:
            s = str(item or "").strip()
            if s and s not in out:
                out.append(s)
        if out:
            return out
    seen: List[str] = []
    for stem in extract_wikilinks(body or ""):
        clean = str(stem or "").strip()
        if not clean or clean == own_title:
            continue
        link = f"[[{clean}]]"
        if link not in seen:
            seen.append(link)
    return seen


def _infer_heading_title(body: str, fallback: str) -> str:
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("#"):
            cand = s.lstrip("#").strip()
            if cand:
                return cand
    return fallback


def _build_domain_alias_index(vault_dir: Path, vault_ref: str, proj_reg: KekProjectRegistry) -> Dict[str, str]:
    """Map lowercased project_id / alias strings -> project_ref for DropZone subfolder routing."""
    idx: Dict[str, str] = {}
    prels = scan_project_rels(vault_dir)
    pmap = proj_reg.sync_projects(vault_ref, prels)
    for prel in prels:
        pdir = vault_dir / prel
        mpath = next(iter(sorted(pdir.glob("Manifest-*.md"))), None)
        meta: Dict[str, Any] = {}
        if mpath is not None and mpath.is_file():
            try:
                meta, _, _ = extract_frontmatter(mpath.read_text(encoding="utf-8"))
            except FrontmatterError:
                meta = {}
        status = str(meta.get("status") or "active").strip().lower()
        if status == "archived":
            continue
        pref = pmap[prel]
        pid = str(meta.get("project_id") or pdir.name).strip()
        keys = [pid, pdir.name]
        aliases = meta.get("aliases") or []
        if isinstance(aliases, list):
            keys.extend(str(a).strip() for a in aliases if isinstance(a, str) and a.strip())
        for k in keys:
            if k:
                idx.setdefault(k.lower(), pref)
    return idx


def _hydrate_library_markdown(
    raw: str,
    *,
    fallback_stem: str,
    dom: Dict[str, Any],
) -> Tuple[str, str, Dict[str, Any]]:
    """Ensure routed DropZone note carries valid library Frontmatter & summary."""
    try:
        meta, body, has_fm = extract_frontmatter(raw)
    except FrontmatterError:
        meta, body, has_fm = {}, raw, False

    title = str(meta.get("title") or "").strip() or _infer_heading_title(body, fallback_stem)
    summary = str(meta.get("summary") or "").strip() or _first_meaningful_sentence(body, title)
    status = "active" if dom["default_mode"] == "auto" else "pending"
    tags = meta.get("tags")
    if not isinstance(tags, list) or not tags:
        tags = ["Library"]
    aliases = meta.get("aliases") if isinstance(meta.get("aliases"), list) else []

    if has_fm:
        rendered = replace_frontmatter_field(raw, "doc_type", "library")
        rendered = replace_frontmatter_field(rendered, "title", f'"{fm_scalar(title)}"')
        rendered = replace_frontmatter_field(rendered, "summary", f'"{fm_scalar(summary)}"')
        rendered = replace_frontmatter_field(rendered, "status", status)
        rendered = replace_frontmatter_field(rendered, "project_id", f'"{fm_scalar(dom["project_id"])}"')
        rendered = replace_frontmatter_field(rendered, "updated", _today())
        if not meta.get("tags"):
            rendered = replace_frontmatter_field(rendered, "tags", '["Library"]')
    else:
        rendered = _render_note_md(
            doc_type="library",
            title=title,
            summary=summary,
            status=status,
            tags=[str(t) for t in tags],
            aliases=[str(a) for a in aliases],
            updated=_today(),
            body=body if body.strip() else f"# {title}",
        )
        rendered = replace_frontmatter_field(rendered, "project_id", f'"{fm_scalar(dom["project_id"])}"')

    fmeta, _, _ = extract_frontmatter(rendered)
    return rendered, title, fmeta


def _route_single_dropzone_note(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    proj_reg: KekProjectRegistry,
    kek: bytes,
    app_id: str,
    note_ref: str,
    rel: str,
    alias_index: Dict[str, str],
    explicit_project_ref: Optional[str],
    scope: str,
    on_conflict: str,
    preflight_only: bool = False,
) -> Tuple[str, Dict[str, Any], List[str]]:
    """Route a single DropZone markdown note -> ('routed'|'held'|'collision', record, sealed_paths)."""
    src = vault_dir / rel
    if not src.is_file():
        return "held", {"note_ref": note_ref, "reason": "missing_source"}, [rel]

    raw = src.read_text(encoding="utf-8")
    try:
        src_meta, _, _ = extract_frontmatter(raw)
    except FrontmatterError:
        src_meta = {}

    parts = Path(rel).parts  # ("00-DropZone", ...)
    inferred_pref = explicit_project_ref
    effective_scope = (scope or "auto").strip().lower()

    if not inferred_pref and effective_scope != "ai-zone" and len(parts) > 2:
        top_sub = parts[1].strip().lower()
        if top_sub in alias_index:
            inferred_pref = alias_index[top_sub]
        elif top_sub in ("ai-zone", "aizone", "global"):
            effective_scope = "ai-zone"
        else:
            return (
                "held",
                {
                    "note_ref": note_ref,
                    "title": str(src_meta.get("title") or src.stem),
                    "reason": "unmatched_subdomain",
                },
                [rel],
            )

    dom = resolve_domain_scope(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        proj_reg=proj_reg,
        explicit_project_ref=inferred_pref,
        scope="project" if (inferred_pref and effective_scope == "auto") else effective_scope,
        source_rel_path=rel,
        source_meta=src_meta,
    )
    rendered, title, fmeta = _hydrate_library_markdown(raw, fallback_stem=src.stem, dom=dom)
    errs = validate_note_meta(fmeta)
    if errs:
        return "held", {"note_ref": note_ref, "title": title, "reason": "; ".join(errs)}, [rel]

    target_dir = resolve_zone_dir(vault_dir, dom, "library")
    stem = _slugify_filename(title)
    new_rel = validate_note_rel_path(f"{target_dir.relative_to(vault_dir).as_posix()}/{stem}.md")
    dst = vault_dir / new_rel

    if dst.exists() and dst.resolve() != src.resolve():
        if on_conflict == "overwrite":
            pass
        elif on_conflict == "rename":
            idx = 2
            while (vault_dir / f"{target_dir.relative_to(vault_dir).as_posix()}/{stem}-{idx}.md").exists():
                idx += 1
            new_rel = validate_note_rel_path(f"{target_dir.relative_to(vault_dir).as_posix()}/{stem}-{idx}.md")
            dst = vault_dir / new_rel
        else:
            return "collision", {"note_ref": note_ref, "title": title, "reason": "target_exists"}, [rel, new_rel]

    _preflight_note_moves(vault_dir, {rel: new_rel})
    if preflight_only:
        return "planned", {}, [rel, new_rel]
    _atomic_write_md(dst, rendered)
    if src.resolve() != dst.resolve():
        src.unlink()
        note_reg.move_handle(vault_ref, note_ref, new_rel)
    _cascade_note_moves(vault_dir, {src.relative_to(vault_dir).as_posix(): new_rel})
    note_reg.sync_paths(vault_ref, _scan_vault_markdowns(vault_dir))
    _append_journal_event(vault_dir, note_reg, vault_ref, dst.stem, f"DropZone 路由归档至 {dom['project_id']} 文献库")

    pub = _public_note(kek=kek, app_id=app_id, vault_ref=vault_ref, note_ref=note_ref, rel=new_rel, meta=fmeta)
    sealed = [rel, new_rel]
    if dom.get("project_rel_path"):
        sealed.append(str(dom["project_rel_path"]))
    return "routed", pub, sealed


def note_route(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    proj_reg: KekProjectRegistry,
    kek: bytes,
    app_id: str,
    note_ref: Optional[str] = None,
    project_ref: Optional[str] = None,
    scope: str = "auto",
    on_conflict: str = "abort",
    ws_root: Optional[Path] = None,
    ticket_id: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Route DropZone notes (single handle or inbox sweep) into target domain 21-Library."""
    policy = (on_conflict or "abort").strip().lower()
    if policy not in ("abort", "rename", "overwrite"):
        raise VaultStoreError("E_PARAM_INVALID", f"不支持的冲突策略: {on_conflict}", exit_code=1)

    rels = _scan_vault_markdowns(vault_dir)
    mapping = note_reg.sync_paths(vault_ref, rels)
    alias_index = _build_domain_alias_index(vault_dir, vault_ref, proj_reg)

    candidates: List[Tuple[str, str]] = []
    if note_ref:
        rec = note_reg.resolve_handle(vault_ref, note_ref)
        candidates.append((note_ref, str(rec["note_rel_path"])))
    else:
        for rel in rels:
            parts = Path(rel).parts
            if len(parts) < 2 or parts[0] != "00-DropZone":
                continue
            if "91-Temp" in parts or any(p.startswith(("_", "[HELD]", "[COLLISION]")) for p in parts[1:]):
                continue
            candidates.append((mapping[rel], rel))

    routed: List[Dict[str, Any]] = []
    held: List[Dict[str, Any]] = []
    collisions: List[Dict[str, Any]] = []
    sealed: List[str] = list(rels)

    # Validate the whole candidate batch before ticket or first note mutation.
    for nref, rel in candidates:
        _route_single_dropzone_note(
            vault_dir=vault_dir, vault_ref=vault_ref, note_reg=note_reg,
            proj_reg=proj_reg, kek=kek, app_id=app_id, note_ref=nref,
            rel=rel, alias_index=alias_index, explicit_project_ref=project_ref,
            scope=scope, on_conflict=policy, preflight_only=True,
        )

    pre_ticket: Optional[Dict[str, Any]] = None
    if ws_root is not None and ticket_id:
        pre_ticket = ensure_doc_ticket(
            ws_root,
            vault_dir,
            task_key=f"route:{vault_ref}:{project_ref or 'inbox'}",
            title=f"DropZone 批量分拣 ({len(candidates)} 项)",
            summary=f"DropZone route 批处理：处理 {len(candidates)} 项",
            ticket_id=ticket_id,
        )

    for nref, rel in candidates:
        outcome, item, extra_sealed = _route_single_dropzone_note(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            note_reg=note_reg,
            proj_reg=proj_reg,
            kek=kek,
            app_id=app_id,
            note_ref=nref,
            rel=rel,
            alias_index=alias_index,
            explicit_project_ref=project_ref,
            scope=scope,
            on_conflict=policy,
        )
        sealed.extend(extra_sealed)
        if outcome == "routed":
            routed.append(item)
        elif outcome == "collision":
            if note_ref:
                raise VaultStoreError("E_PARAM_CONFLICT", "目标文献库已存在同名笔记（可通过 --on_conflict rename|overwrite 处理）", exit_code=1)
            collisions.append(item)
        else:
            held.append(item)

    all_final = _scan_vault_markdowns(vault_dir)
    sealed.extend(all_final)

    doc_ticket: Optional[Dict[str, Any]] = None
    if ws_root is not None and (candidates or held or collisions or pre_ticket is not None):
        route_key = (
            f"route-single:{vault_ref}:{note_ref}"
            if (note_ref and len(candidates) <= 1 and not held and not collisions and pre_ticket is None)
            else f"route:{vault_ref}:{project_ref or 'inbox'}"
        )
        tinfo = pre_ticket or ensure_doc_ticket(
            ws_root,
            vault_dir,
            task_key=route_key,
            title=f"DropZone 分拣 ({len(candidates)} 项)",
            summary=f"DropZone route 处理：处理 {len(candidates)} 项",
            ticket_id=ticket_id,
        )
        all_clean = len(held) == 0 and len(collisions) == 0
        doc_ticket = update_doc_ticket_progress(
            ws_root,
            vault_dir,
            ticket_info=tinfo,
            note=f"note-route 结果：routed={len(routed)}, held={len(held)}, collisions={len(collisions)}",
            mark_done=all_clean,
        )
        if doc_ticket.get("ticket_rel_path"):
            sealed.append(str(doc_ticket["ticket_rel_path"]))

    out_data: Dict[str, Any] = {
        "vault_ref": vault_ref,
        "routed_count": len(routed),
        "held_count": len(held),
        "collision_count": len(collisions),
        "routed": routed,
        "held": held,
        "collisions": collisions,
        "path_sealed": True,
    }
    if doc_ticket is not None:
        out_data["doc_ticket"] = {k: v for k, v in doc_ticket.items() if k != "ticket_rel_path"}
    return out_data, sealed


def _drop_frontmatter_key(raw: str, key: str) -> str:
    lines = raw.splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        return raw
    end_idx = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end_idx is None:
        return raw
    prefix = f"{key}:"
    filtered = [ln for idx, ln in enumerate(lines[:end_idx]) if idx == 0 or not ln.lstrip().startswith(prefix)]
    return "\n".join([*filtered, *lines[end_idx:]]) + ("\n" if raw.endswith("\n") else "")


def artifact_adopt(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    proj_reg: KekProjectRegistry,
    kek: bytes,
    app_id: str,
    note_ref: str,
    project_ref: Optional[str] = None,
    scope: str = "auto",
    target_zone: str = "artifacts",
    title_override: Optional[str] = None,
    summary_override: Optional[str] = None,
    on_conflict: str = "abort",
    by_actor: str = "",
    ws_root: Optional[Path] = None,
    ticket_id: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Adopt a workbench draft or strong source document into domain 10-Artifacts/ or 21-Library/ (116 tier admission)."""
    tz = (target_zone or "artifacts").strip().lower()
    if tz not in ("artifacts", "library"):
        raise VaultStoreError("E_PARAM_INVALID", f"--target_zone 仅支持 artifacts / library，收到: {target_zone}", exit_code=1)
    policy = (on_conflict or "abort").strip().lower()
    if policy not in ("abort", "rename", "overwrite"):
        raise VaultStoreError("E_PARAM_INVALID", f"不支持的冲突策略: {on_conflict}", exit_code=1)

    rec = note_reg.resolve_handle(vault_ref, note_ref)
    old_rel = str(rec["note_rel_path"])
    src = _safe_existing_vault_file(vault_dir, old_rel)

    raw = src.read_text(encoding="utf-8")
    try:
        meta, body, has_fm = extract_frontmatter(raw)
    except FrontmatterError:
        meta, body, has_fm = {}, raw, False

    _assert_not_journal(old_rel, meta)
    if str(meta.get("status") or "").strip().lower() in ("deprecated", "archived"):
        raise VaultStoreError("E_PARAM_CONFLICT", "已废弃或归档笔记不可采收入正式分区", exit_code=1)

    src_pid, src_root, _ = _domain_state_for_rel(vault_dir, old_rel)
    _require_unfrozen_domain(vault_dir, {"project_id": src_pid, "domain_root": src_root})

    dom = resolve_domain_scope(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        proj_reg=proj_reg,
        explicit_project_ref=project_ref,
        scope=scope,
        source_rel_path=old_rel,
        source_meta=meta,
    )
    _require_unfrozen_domain(vault_dir, dom)

    title = (title_override or "").strip() or str(meta.get("title") or "").strip() or _infer_heading_title(body, src.stem)
    raw_summary = str(meta.get("summary") or "").strip()
    if (summary_override or "").strip():
        summary = summary_override.strip()
    elif raw_summary and raw_summary != title:
        summary = raw_summary
    else:
        summary = _first_meaningful_sentence(body, title)
    stem = _slugify_filename(title)
    dest_dir = resolve_zone_dir(vault_dir, dom, tz)
    dest_rel_dir = dest_dir.relative_to(vault_dir).as_posix()
    target_label = "Artifacts" if tz == "artifacts" else "Library"

    if tz == "artifacts":
        art_pid_tok = str(dom["project_id"]) if dom.get("scope") == "project" else "AI-Zone"
        existing_art: Optional[Path] = None
        if dest_dir.is_dir():
            for cand in sorted(dest_dir.glob("*.md")):
                if cand.is_symlink() or not cand.is_file():
                    continue
                if cand.stem == stem:
                    existing_art = cand
                    break
                try:
                    cm, _, cfm = extract_frontmatter(cand.read_text(encoding="utf-8"))
                    if cfm and str(cm.get("title") or "").strip().casefold() == title.casefold():
                        existing_art = cand
                        break
                except Exception:
                    continue
        if existing_art is not None and existing_art.resolve() != src.resolve():
            if policy == "abort":
                raise VaultStoreError("E_PARAM_CONFLICT", f"目标 {target_label} 目录已存在同名条目（可通过 --on_conflict rename|overwrite 处理）", exit_code=1)
            if policy == "overwrite":
                new_rel = validate_note_rel_path(existing_art.relative_to(vault_dir).as_posix())
            else:
                import re as _re
                pat = _re.compile(r"^Art-" + _re.escape(art_pid_tok) + r"-(\d+)-")
                mx = max((int(m.group(1)) for f in dest_dir.glob("*.md") for m in [pat.match(f.name)] if m), default=0)
                seq = mx + 1
                while (dest_dir / f"Art-{art_pid_tok}-{seq:03d}-{stem}.md").exists():
                    seq += 1
                new_rel = validate_note_rel_path(f"{dest_rel_dir}/Art-{art_pid_tok}-{seq:03d}-{stem}.md")
        else:
            import re as _re
            if stem.startswith(f"Art-{art_pid_tok}-"):
                art_stem = stem
            else:
                pat = _re.compile(r"^Art-" + _re.escape(art_pid_tok) + r"-(\d+)-")
                mx = max((int(m.group(1)) for f in (dest_dir.glob("*.md") if dest_dir.is_dir() else []) for m in [pat.match(f.name)] if m), default=0)
                seq = mx + 1
                while (dest_dir / f"Art-{art_pid_tok}-{seq:03d}-{stem}.md").exists():
                    seq += 1
                art_stem = f"Art-{art_pid_tok}-{seq:03d}-{stem}"
            new_rel = validate_note_rel_path(f"{dest_rel_dir}/{art_stem}.md")
        dst = vault_dir / new_rel
    else:
        new_rel = validate_note_rel_path(f"{dest_rel_dir}/{stem}.md")
        dst = vault_dir / new_rel
        if dst.exists() and dst.resolve() != src.resolve():
            if policy == "abort":
                raise VaultStoreError("E_PARAM_CONFLICT", f"目标 {target_label} 目录已存在同名条目（可通过 --on_conflict rename|overwrite 处理）", exit_code=1)
            if policy == "rename":
                idx = 2
                while (vault_dir / f"{dest_rel_dir}/{stem}-{idx}.md").exists():
                    idx += 1
                new_rel = validate_note_rel_path(f"{dest_rel_dir}/{stem}-{idx}.md")
                dst = vault_dir / new_rel

    doc_type = "artifact" if tz == "artifacts" else "library"
    default_tag = "Artifact" if tz == "artifacts" else "Library"
    tags = meta.get("tags")
    if not isinstance(tags, list) or not tags:
        tags = [default_tag]

    actor = (by_actor or "").strip() or "ai"
    if has_fm:
        rendered = replace_frontmatter_field(raw, "doc_type", doc_type)
        rendered = replace_frontmatter_field(rendered, "title", f'"{fm_scalar(title)}"')
        rendered = replace_frontmatter_field(rendered, "summary", f'"{fm_scalar(summary)}"')
        rendered = replace_frontmatter_field(rendered, "status", "active")
        if tz == "artifacts":
            rendered = replace_frontmatter_field(rendered, "stage", "draft")
            if not str(meta.get("type") or "").strip():
                rendered = replace_frontmatter_field(rendered, "type", "SYN")
            rendered = _drop_frontmatter_key(rendered, "sources")
        else:
            rendered = _drop_frontmatter_key(rendered, "stage")
        rendered = replace_frontmatter_field(rendered, "project_id", f'"{fm_scalar(dom["project_id"])}"')
        rendered = replace_frontmatter_field(rendered, "updated", _today())
        if "processed" not in meta:
            rendered = replace_frontmatter_field(rendered, "processed", "false")
        if not meta.get("tags"):
            rendered = replace_frontmatter_field(rendered, "tags", f'["{default_tag}"]')
    else:
        rendered = _render_note_md(
            doc_type=doc_type,
            title=title,
            summary=summary,
            status="active",
            tags=[str(t) for t in tags],
            aliases=[],
            updated=_today(),
            body=body if body.strip() else f"# {title}",
        )
        if tz == "artifacts":
            rendered = replace_frontmatter_field(rendered, "stage", "draft")
            rendered = replace_frontmatter_field(rendered, "type", "SYN")
        rendered = replace_frontmatter_field(rendered, "processed", "false")
        rendered = replace_frontmatter_field(rendered, "project_id", f'"{fm_scalar(dom["project_id"])}"')

    if not str(meta.get("created_by") or "").strip():
        rendered = replace_frontmatter_field(rendered, "created_by", f'"{fm_scalar(actor)}"')
    rendered = replace_frontmatter_field(rendered, "updated_by", f'"{fm_scalar(actor)}"')
    if "[[" in rendered:
        from notes.atlas_wikilink_tools import build_vault_link_index, normalize_inline_wikilinks

        pre_rels = _scan_vault_markdowns(vault_dir)
        pre_map = note_reg.list_vault_handles(vault_ref)
        if set(pre_rels) != set(pre_map.keys()):
            pre_map = note_reg.sync_paths(vault_ref, pre_rels)
        if pre_map:
            idx = build_vault_link_index(vault_dir, pre_map)
            rendered, _ = normalize_inline_wikilinks(rendered, source_rel=new_rel, index=idx)

    fmeta, _, _ = extract_frontmatter(rendered)
    errs = validate_note_meta(fmeta, rel_path=new_rel)
    if errs:
        raise VaultStoreError("E_PARAM_INVALID", f"沉淀前元数据校验失败: {'; '.join(errs)}", exit_code=1)

    _preflight_note_moves(vault_dir, {src.relative_to(vault_dir).as_posix(): new_rel})
    _require_admission_storage(vault_dir, src, dst)
    pre_ticket: Optional[Dict[str, Any]] = None
    if ticket_id and ws_root is not None:
        pre_ticket = ensure_doc_ticket(
            ws_root,
            vault_dir,
            task_key=f"artifact:{note_ref}",
            title=f"{target_label} adopt: {title}",
            project_id=str(dom.get("project_id") or ""),
            ticket_id=ticket_id,
        )

    _atomic_write_md(dst, rendered)
    if src.resolve() != dst.resolve():
        src.unlink()
        note_reg.move_handle(vault_ref, note_ref, new_rel)
    cascaded = _cascade_note_moves(vault_dir, {src.relative_to(vault_dir).as_posix(): new_rel})
    note_reg.sync_paths(vault_ref, _scan_vault_markdowns(vault_dir))
    _append_journal_event(vault_dir, note_reg, vault_ref, dst.stem, f"采收工作台材料为 {target_label} ({dom['project_id']})")

    file_sha256 = hashlib.sha256(dst.read_bytes()).hexdigest()
    doc_ticket: Optional[Dict[str, Any]] = None
    if pre_ticket is not None and ws_root is not None:
        audit_note = f"artifact-adopt({tz}): ref={note_ref} title=「{title}」 scope={dom.get('scope') or ''} ({dom.get('project_id') or ''}) sha256={file_sha256}"
        doc_ticket = update_doc_ticket_progress(
            ws_root,
            vault_dir,
            ticket_info=pre_ticket,
            note=audit_note,
            mark_done=False,
        )

    pub = _public_note(kek=kek, app_id=app_id, vault_ref=vault_ref, note_ref=note_ref, rel=new_rel, meta=fmeta)
    pub["target_zone"] = tz
    if tz == "artifacts":
        pub["stage"] = "draft"
    pub["wikilink_cascaded_files"] = cascaded
    pub["artifact_sha256"] = file_sha256
    if doc_ticket is not None:
        pub["doc_ticket"] = {k: v for k, v in doc_ticket.items() if k != "ticket_rel_path"}
    sealed = [old_rel, new_rel]
    if dom.get("project_rel_path"):
        sealed.append(str(dom["project_rel_path"]))
    return pub, sealed


def artifact_finalize(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    note_ref: str,
    by_actor: str = "",
    ws_root: Optional[Path] = None,
    ticket_id: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Promote an artifact note from stage=draft -> stage=final for 05-Artifacts base views."""
    rec = note_reg.resolve_handle(vault_ref, note_ref)
    rel = str(rec["note_rel_path"])
    fpath = _safe_existing_vault_file(vault_dir, rel)

    raw = fpath.read_text(encoding="utf-8")
    try:
        meta, fbody, _ = extract_frontmatter(raw)
    except FrontmatterError as exc:
        raise VaultStoreError("E_PARAM_INVALID", f"成果 Frontmatter 解析失败: {exc}", exit_code=1) from exc
    _assert_not_journal(rel, meta)
    zone, _ = infer_zone_and_project_rel(rel)
    if str(meta.get("doc_type") or "").strip().lower() != "artifact" and zone != "artifacts":
        raise VaultStoreError("E_PARAM_CONFLICT", "仅 Artifacts 成果卡可执行 artifact-finalize（请先调用 artifact-adopt）", exit_code=1)
    if str(meta.get("status") or "").strip().lower() in ("deprecated", "archived"):
        raise VaultStoreError("E_PARAM_CONFLICT", "已废弃或归档成果不可定稿", exit_code=1)

    pid, droot, _ = _domain_state_for_rel(vault_dir, rel)
    _require_unfrozen_domain(vault_dir, {"project_id": pid, "domain_root": droot})

    cur_title = str(meta.get("title") or fpath.stem).strip()
    cur_summary = str(meta.get("summary") or "").strip()
    actor = (by_actor or "").strip() or str(meta.get("updated_by") or "").strip() or "ai"
    reviewer = "human" if actor.lower() == "human" else (str(meta.get("reviewed_by") or "").strip() or "ai")

    rendered = replace_frontmatter_field(raw, "doc_type", "artifact")
    if not cur_summary or cur_summary == cur_title:
        improved = _first_meaningful_sentence(fbody, cur_title)
        if improved and improved != cur_title:
            rendered = replace_frontmatter_field(rendered, "summary", f'"{fm_scalar(improved)}"')
    rendered = replace_frontmatter_field(rendered, "status", "active")
    rendered = replace_frontmatter_field(rendered, "stage", "final")
    rendered = replace_frontmatter_field(rendered, "updated", _today())
    if not str(meta.get("type") or "").strip():
        rendered = replace_frontmatter_field(rendered, "type", "SYN")
    rendered = _drop_frontmatter_key(rendered, "sources")
    if "processed" not in meta:
        rendered = replace_frontmatter_field(rendered, "processed", "false")
    if not str(meta.get("created_by") or "").strip():
        rendered = replace_frontmatter_field(rendered, "created_by", f'"{fm_scalar(actor)}"')
    rendered = replace_frontmatter_field(rendered, "updated_by", f'"{fm_scalar(actor)}"')
    rendered = replace_frontmatter_field(rendered, "reviewed_by", reviewer)
    if "[[" in rendered:
        from notes.atlas_wikilink_tools import build_vault_link_index, normalize_inline_wikilinks

        pre_rels = _scan_vault_markdowns(vault_dir)
        pre_map = note_reg.list_vault_handles(vault_ref)
        if set(pre_rels) != set(pre_map.keys()):
            pre_map = note_reg.sync_paths(vault_ref, pre_rels)
        if pre_map:
            idx = build_vault_link_index(vault_dir, pre_map)
            rendered, _ = normalize_inline_wikilinks(rendered, source_rel=rel, index=idx)
    fmeta, _, _ = extract_frontmatter(rendered)
    errs = validate_note_meta(fmeta, rel_path=rel)
    if errs:
        raise VaultStoreError("E_PARAM_INVALID", f"定稿前元数据校验失败: {'; '.join(errs)}", exit_code=1)

    pre_ticket: Optional[Dict[str, Any]] = None
    if ticket_id and ws_root is not None:
        pre_ticket = ensure_doc_ticket(
            ws_root,
            vault_dir,
            task_key=f"artifact:{note_ref}",
            title=f"Artifact finalize: {str(fmeta.get('title') or fpath.stem)}",
            project_id=str(fmeta.get("project_id") or "AI-Zone"),
            ticket_id=ticket_id,
        )

    _atomic_write_md(fpath, rendered)
    _append_journal_event(vault_dir, note_reg, vault_ref, fpath.stem, "Artifact 成果定稿 (stage=final)")

    file_sha256 = hashlib.sha256(fpath.read_bytes()).hexdigest()
    doc_ticket: Optional[Dict[str, Any]] = None
    if pre_ticket is not None and ws_root is not None:
        dom_scope = "project" if infer_zone_and_project_rel(rel)[1] else "ai-zone"
        pid_lbl = str(fmeta.get("project_id") or "AI-Zone")
        audit_note = f"artifact-finalize: ref={note_ref} title=「{str(fmeta.get('title') or fpath.stem)}」 scope={dom_scope} ({pid_lbl}) sha256={file_sha256}"
        doc_ticket = update_doc_ticket_progress(
            ws_root,
            vault_dir,
            ticket_info=pre_ticket,
            note=audit_note,
            mark_done=False,
        )

    pub = _public_note(kek=kek, app_id=app_id, vault_ref=vault_ref, note_ref=note_ref, rel=rel, meta=fmeta)
    pub["stage"] = "final"
    pub["artifact_sha256"] = file_sha256
    if doc_ticket is not None:
        pub["doc_ticket"] = {k: v for k, v in doc_ticket.items() if k != "ticket_rel_path"}
    return pub, [rel]


def _trash_dst(vault_dir: Path, filename: str) -> Path:
    """Allocate a non-overwriting target path inside Vault .trash/."""
    trash = vault_dir / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    cand = trash / filename
    idx = 0
    while cand.exists():
        idx += 1
        cand = trash / f"{filename}.{idx}"
    return cand


def _collect_attachment_refs(vault_dir: Path) -> set[str]:
    """Scan .md/.canvas/.base across active Vault subtrees for referenced attachment names/stems."""
    import json as _json
    import os as _os
    import re as _re
    from urllib.parse import unquote

    wikilink_re = _re.compile(r"!?\[\[([^\]]+)\]\]")
    md_img_re = _re.compile(r"!?\[[^\]]*\]\(\s*(<[^>]+>|[^)\s]+)")
    html_img_re = _re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', _re.IGNORECASE)
    refs: set[str] = set()

    def _add(raw: str) -> None:
        cleaned = unquote(raw.split("|")[0].split("#")[0].split("?")[0].strip().strip("<>"))
        if cleaned:
            refs.add(cleaned)
            refs.add(Path(cleaned).name)
            refs.add(Path(cleaned).stem)

    skip = {".git", ".venv", ".trash", "node_modules"}
    for dirpath, dirnames, filenames in _os.walk(vault_dir):
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        for fname in filenames:
            if not fname.endswith((".md", ".canvas", ".base")):
                continue
            fpath = Path(dirpath) / fname
            text = fpath.read_text(encoding="utf-8", errors="replace")
            if fname.endswith(".canvas"):
                try:
                    data = _json.loads(text)
                    for node in data.get("nodes", []):
                        if isinstance(node, dict) and node.get("type") == "file" and isinstance(node.get("file"), str):
                            _add(node["file"])
                except ValueError:
                    pass
            for pat in (wikilink_re, md_img_re, html_img_re):
                for m in pat.finditer(text):
                    _add(m.group(1))
    return refs


def clean_attachments(
    *,
    vault_dir: Path,
    vault_ref: str,
    apply: bool = False,
    trash_reg: Optional[Any] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Detect (and optionally soft-delete to .trash/) orphan attachments & OS junk, and sweep >30d expired trash."""
    import os as _os
    import shutil as _shutil

    refs = _collect_attachment_refs(vault_dir)
    att_dir = vault_dir / "99-Assets/10-Attachments"
    orphans: List[Path] = []
    kept = 0
    if att_dir.is_dir():
        for p in sorted(att_dir.rglob("*")):
            if p.is_symlink() or not p.is_file() or p.name.startswith("."):
                continue
            rel_att = p.relative_to(att_dir).as_posix()
            rel_vault = p.relative_to(vault_dir).as_posix()
            if p.name in refs or p.stem in refs or rel_att in refs or rel_vault in refs:
                kept += 1
            else:
                orphans.append(p)

    junk_paths: List[Path] = []
    skip_dirs = {".git", ".venv", ".trash", "node_modules"}
    for dirpath, dirnames, filenames in _os.walk(vault_dir):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for d in list(dirnames):
            if d == "__pycache__":
                junk_paths.append(Path(dirpath) / d)
                dirnames.remove(d)
        for fname in filenames:
            if fname == ".DS_Store":
                junk_paths.append(Path(dirpath) / fname)

    trashed_orphans = 0
    trashed_junk = 0
    trash_refs: List[str] = []
    sealed: List[str] = []
    for p in orphans:
        orig_rel = p.relative_to(vault_dir).as_posix()
        sealed.append(orig_rel)
        if apply and p.exists():
            sz = p.stat().st_size
            dst = _trash_dst(vault_dir, p.name)
            _shutil.move(str(p), str(dst))
            trashed_orphans += 1
            if trash_reg is not None:
                trec = trash_reg.record_trashed(
                    vault_ref=vault_ref,
                    trash_rel=dst.relative_to(vault_dir).as_posix(),
                    orig_rel=orig_rel,
                    orig_zone="assets",
                    display_name=p.stem,
                    kind="file",
                    size_bytes=sz,
                )
                trash_refs.append(trec["trash_ref"])
    for jp in junk_paths:
        orig_rel = jp.relative_to(vault_dir).as_posix()
        sealed.append(orig_rel)
        if apply and jp.exists():
            sz = jp.stat().st_size if jp.is_file() else 0
            dst = _trash_dst(vault_dir, jp.name)
            _shutil.move(str(jp), str(dst))
            trashed_junk += 1
            if trash_reg is not None:
                trec = trash_reg.record_trashed(
                    vault_ref=vault_ref,
                    trash_rel=dst.relative_to(vault_dir).as_posix(),
                    orig_rel=orig_rel,
                    orig_zone="junk",
                    display_name=jp.name,
                    kind="dir" if dst.is_dir() else "file",
                    size_bytes=sz,
                )
                trash_refs.append(trec["trash_ref"])

    return {
        "vault_ref": vault_ref,
        "apply": bool(apply),
        "kept_attachment_count": kept,
        "orphan_attachment_count": len(orphans),
        "junk_count": len(junk_paths),
        "trashed_attachment_count": trashed_orphans,
        "trashed_junk_count": trashed_junk,
        "trash_refs": trash_refs,
        "path_sealed": True,
    }, sealed


def archive_list(
    *,
    vault_dir: Path,
    vault_ref: str,
    proj_reg: KekProjectRegistry,
    project_ref: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Cold archive inventory across 98-Archives (ports 116 ops/packager.py with KEK path blinding)."""
    target_pid: Optional[str] = None
    if project_ref:
        dom = resolve_domain_scope(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            proj_reg=proj_reg,
            explicit_project_ref=project_ref,
            scope="project",
        )
        target_pid = str(dom["project_id"])

    items: List[Dict[str, Any]] = []
    sealed: List[str] = []
    total_bytes = 0

    for p in sorted(vault_dir.rglob("*")):
        if p.is_symlink() or not p.is_file() or p.name.startswith("."):
            continue
        rel = p.relative_to(vault_dir).as_posix()
        parts = Path(rel).parts
        if any(pt.startswith(".") for pt in parts) or "98-Archives" not in parts or "Tickets" in parts:
            continue
        if parts[0] == "30-Projects" and len(parts) >= 2:
            scope_label = "project"
            pid = parts[1]
        else:
            scope_label = "ai-zone"
            pid = "AI-Zone"
        if target_pid and pid != target_pid:
            continue
        idx_98 = parts.index("98-Archives")
        yymm = parts[idx_98 + 1] if idx_98 + 1 < len(parts) - 1 else ""
        sz = int(p.stat().st_size)
        total_bytes += sz
        sealed.append(rel)
        items.append(
            {
                "name": p.name,
                "domain_scope": scope_label,
                "project_id": pid,
                "month": yymm,
                "size_bytes": sz,
            }
        )

    return {
        "vault_ref": vault_ref,
        "project_id_filter": target_pid,
        "count": len(items),
        "total_bytes": total_bytes,
        "items": items[:200],
        "path_sealed": True,
    }, sealed


def _normalize_month_filter(raw: Optional[str]) -> Optional[str]:
    s = (raw or "").strip()
    if not s:
        return None
    if len(s) == 7 and s[4] == "-" and s[:4].isdigit() and s[5:].isdigit():
        return s[2:4] + s[5:7]
    if len(s) == 6 and s.isdigit():
        return s[2:6]
    if len(s) == 4 and s.isdigit():
        return s
    raise VaultStoreError("E_PARAM_INVALID", "--month 格式应为 YYYY-MM 或 YYMM", exit_code=1)


def archive_pack(
    *,
    vault_dir: Path,
    vault_ref: str,
    proj_reg: KekProjectRegistry,
    project_ref: Optional[str] = None,
    month: Optional[str] = None,
    dry_run: bool = False,
    purge: bool = False,
) -> Tuple[Dict[str, Any], List[str]]:
    """Bundle monthly cold-archive files in 98-Archives into deterministic tar.gz packets."""
    import sys
    import time
    scripts_dir = str(Path(__file__).resolve().parents[1])
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from distribution.archive_io import FileSource, write_archive

    target_pid: Optional[str] = None
    if project_ref:
        dom = resolve_domain_scope(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            proj_reg=proj_reg,
            explicit_project_ref=project_ref,
            scope="project",
        )
        target_pid = str(dom["project_id"])

    month_filter = _normalize_month_filter(month)
    grouped: Dict[Tuple[Path, str, str, str], List[Tuple[Path, str]]] = {}
    sealed: List[str] = []

    for p in sorted(vault_dir.rglob("*")):
        if p.is_symlink() or not p.is_file() or p.name.startswith("."):
            continue
        if p.name.endswith(".tar.gz"):
            continue
        rel = p.relative_to(vault_dir).as_posix()
        parts = Path(rel).parts
        if any(pt.startswith(".") for pt in parts) or "98-Archives" not in parts or "Tickets" in parts:
            continue
        if parts[0] == "30-Projects" and len(parts) >= 2:
            scope_label = "project"
            pid = parts[1]
        else:
            scope_label = "ai-zone"
            pid = "AI-Zone"
        if target_pid and pid != target_pid:
            continue
        idx_98 = parts.index("98-Archives")
        arc_root = vault_dir / Path(*parts[: idx_98 + 1])
        yymm = parts[idx_98 + 1] if idx_98 + 1 < len(parts) - 1 else "undated"
        if month_filter and yymm != month_filter:
            continue
        sub_rel = Path(*parts[idx_98 + 1 :]).as_posix()
        grouped.setdefault((arc_root, scope_label, pid, yymm), []).append((p, sub_rel))
        sealed.append(rel)

    bundles: List[Dict[str, Any]] = []
    packed_files = 0
    purged_files = 0
    stamp = time.strftime("%Y%m%d%H%M%S")

    for (arc_root, scope_label, pid, yymm), entries in sorted(grouped.items(), key=lambda kv: (kv[0][1], kv[0][2], kv[0][3])):
        sources: Dict[str, FileSource] = {}
        dir_set: set[str] = set()
        raw_bytes = 0
        for fpath, sub_rel in entries:
            sources[sub_rel] = FileSource.from_path(fpath)
            raw_bytes += int(fpath.stat().st_size)
            parent = Path(sub_rel).parent.as_posix()
            while parent and parent != ".":
                dir_set.add(parent)
                parent = Path(parent).parent.as_posix()

        bundle_name = f"archive-{pid}-{yymm}.tar.gz"
        bundle_path = arc_root / bundle_name
        if bundle_path.exists():
            bundle_name = f"archive-{pid}-{yymm}-{stamp}.tar.gz"
            bundle_path = arc_root / bundle_name
        sealed.append(bundle_path.relative_to(vault_dir).as_posix())

        tar_size = 0
        if not dry_run:
            manifest = {
                "schema": "gewu-atlas.cold-archive/1",
                "vault_ref": vault_ref,
                "domain_scope": scope_label,
                "project_id": pid,
                "month": yymm,
                "file_count": len(entries),
                "raw_bytes": raw_bytes,
            }
            write_archive(
                bundle_path,
                sources,
                sorted(dir_set),
                "ARCHIVE_MANIFEST.json",
                manifest,
                compressed=True,
            )
            tar_size = int(bundle_path.stat().st_size)
            if purge and bundle_path.is_file():
                for fpath, _ in entries:
                    if fpath.is_file() and not fpath.is_symlink():
                        fpath.unlink()
                        purged_files += 1

        packed_files += len(entries)
        bundles.append(
            {
                "bundle_name": bundle_name,
                "domain_scope": scope_label,
                "project_id": pid,
                "month": yymm,
                "file_count": len(entries),
                "raw_bytes": raw_bytes,
                "tar_bytes": tar_size,
                "purged_sources": purge and not dry_run,
            }
        )

    return {
        "vault_ref": vault_ref,
        "dry_run": dry_run,
        "purge": purge,
        "month_filter": month_filter,
        "project_id_filter": target_pid,
        "bundle_count": len(bundles),
        "packed_file_count": packed_files,
        "purged_file_count": purged_files,
        "bundles": bundles,
        "path_sealed": True,
    }, sealed
