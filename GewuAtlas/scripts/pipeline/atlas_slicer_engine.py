#!/usr/bin/env python3
"""116 adaptive slicing through Atlas handles, governed writes and approval modes.

Pure decisions live in atlas_slice_planner. Code/table/paragraph units are never
truncated to force a character budget; oversize cards are explicitly reported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from facade.atlas_domain_scope import infer_zone_and_project_rel, read_domain_manifest, resolve_domain_scope, resolve_zone_dir
from notes.atlas_note_engine import (
    _atomic_write_md,
    _first_meaningful_sentence,
    _scan_vault_markdowns,
    _slugify_filename,
    _today,
)
from notes.atlas_yamlmini import extract_frontmatter, fm_scalar, replace_frontmatter_field
from notes.atlas_markdown_blocks import code_line_mask
from pipeline.atlas_slice_planner import plan_adaptive, generate_compact_prefix, sanitize_filename
from storage.note_kek_store import KekNoteRegistry, validate_note_rel_path
from storage.project_kek_store import KekProjectRegistry
from storage.vault_kek_store import VaultStoreError

_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*$")


@dataclass
class HeadingNode:
    level: int
    title: str
    heading_line: str = ""
    own_lines: List[str] = field(default_factory=list)
    children: List["HeadingNode"] = field(default_factory=list)
    subtree_chars: int = 0
    own_chars: int = 0

    def full_markdown(self) -> str:
        parts: List[str] = []
        if self.heading_line:
            parts.append(self.heading_line)
        body = "\n".join(self.own_lines).strip("\r\n")
        if body:
            parts.append(body)
        for ch in self.children:
            sub = ch.full_markdown()
            if sub:
                parts.append(sub)
        return "\n\n".join(parts).strip("\r\n")

    def compute_sizes(self) -> int:
        own_text = "\n".join(self.own_lines).strip("\r\n")
        self.own_chars = len(own_text) + (len(self.heading_line) if self.heading_line else 0)
        self.subtree_chars = self.own_chars + sum(ch.compute_sizes() for ch in self.children)
        return self.subtree_chars


def _parse_heading_ast(body: str, doc_title: str) -> HeadingNode:
    root = HeadingNode(level=0, title=doc_title)
    stack: List[HeadingNode] = [root]
    for line, is_code in zip(body.splitlines(), code_line_mask(body)):
        if is_code:
            stack[-1].own_lines.append(line)
            continue
        hm = _HEADING_RE.match(line)
        if hm:
            lvl = len(hm.group(1))
            htitle = hm.group(2).strip()
            node = HeadingNode(level=lvl, title=htitle, heading_line=line)
            while len(stack) > 1 and stack[-1].level >= lvl:
                stack.pop()
            stack[-1].children.append(node)
            stack.append(node)
            continue
        stack[-1].own_lines.append(line)

    root.compute_sizes()
    return root


def _compact_prefix(doc_stem: str, custom_prefix: Optional[str], max_len: int = 18) -> str:
    if custom_prefix and custom_prefix.strip():
        clean = re.sub(r'[\\/*?:"<>|#`\[\]\s]+', "-", custom_prefix.strip()).strip("-_")
        return (clean[:max_len] or "sec")
    return _slugify_filename(doc_stem)[:max_len] or "doc"


class SliceBatch:
    """Paths added under the ingest lock; public handles are resolved together at batch end."""
    def __init__(self, vault_dir):
        self.paths = set(_scan_vault_markdowns(vault_dir))
        self.names = {Path(path).name.casefold() for path in self.paths}


def note_slice_adaptive(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    proj_reg: KekProjectRegistry,
    app_id: str,
    note_ref: str,
    project_ref: Optional[str] = None,
    scope: str = "auto",
    min_chars: int = 300,
    max_chars: int = 4000,
    target_chars: Optional[int] = None,
    custom_prefix: Optional[str] = None,
    idempotent: bool = False,
    batch: Optional[SliceBatch] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    if min_chars < 50:
        min_chars = 50
    if max_chars <= min_chars:
        max_chars = min_chars * 4

    if target_chars is None:
        target_chars = min(max_chars, max(min_chars, 2000))
    if not min_chars <= target_chars <= max_chars:
        raise VaultStoreError("E_PARAM_INVALID", "target_chars需在min_chars与max_chars之间", exit_code=1)

    rec = note_reg.resolve_handle(vault_ref, note_ref)
    src_rel = str(rec["note_rel_path"])
    src_norm = validate_note_rel_path(src_rel)
    cur = vault_dir.resolve()
    for part in Path(src_norm).parts:
        cur = cur / part
        if cur.exists() and cur.is_symlink():
            raise VaultStoreError("E_SECURITY_VIOLATION", "拒绝访问符号链接源笔记路径", exit_code=2)
    src_path = cur.resolve()
    if vault_dir.resolve() not in src_path.parents or not src_path.is_file():
        raise VaultStoreError("E_NOT_FOUND", f"句柄 {note_ref} 对应源笔记不存在", exit_code=1)

    txt = src_path.read_text(encoding="utf-8")
    meta, body, has_fm = extract_frontmatter(txt)
    if not has_fm:
        meta = {}
    if src_rel.startswith("05-Journal/") or "/05-Journal/" in f"/{src_rel}" or str(meta.get("doc_type") or "").strip().lower() == "journal":
        raise VaultStoreError("E_JOURNAL_PROTECTED", "日记（05-Journal）不能直接切片", exit_code=1)
    if "/97-Attic/" in f"/{src_rel}/" or str(meta.get("status") or "").strip().lower() in ("deprecated", "archived"):
        raise VaultStoreError("E_PARAM_CONFLICT", "已废弃或归档笔记不能执行切片", exit_code=1)
    doc_title = str(meta.get("title") or src_path.stem).strip()
    doc_stem = src_path.stem

    dom = resolve_domain_scope(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        proj_reg=proj_reg,
        explicit_project_ref=project_ref,
        scope=scope,
        source_rel_path=src_rel,
        source_meta=meta,
    )
    droot = vault_dir / str(dom["project_rel_path"]) if dom.get("project_rel_path") else vault_dir / "20-AI-Zone"
    d_manifest = read_domain_manifest(droot, str(dom.get("project_id") or "ai-zone"))
    if str(d_manifest.get("status") or "active").strip().lower() in ("archived", "deprecated"):
        raise VaultStoreError("E_DOMAIN_FROZEN", f"目标域已冻结（status={d_manifest.get('status')}），禁止切片写入", exit_code=1)
    slices_base = resolve_zone_dir(vault_dir, dom, "slices")
    cluster_dir = slices_base / doc_stem
    if cluster_dir.exists() and any(cluster_dir.glob("*.md")):
        if idempotent:
            all_md = [] if batch else _scan_vault_markdowns(vault_dir)
            n_map = None if batch else note_reg.sync_paths(vault_ref, all_md)
            def lookup(path):
                rel = path.relative_to(vault_dir).as_posix()
                return rel if batch else n_map.get(rel)
            md_files = sorted(cluster_dir.glob("*.md"))
            toc_fp = next((p for p in md_files if p.stem.startswith("TOC-")), None)
            slice_fps = [p for p in md_files if p != toc_fp]
            toc_ref = lookup(toc_fp) if toc_fp else None
            return {
                "vault_ref": vault_ref,
                "source_note_ref": note_ref,
                "toc_note_ref": toc_ref,
                "slice_count": len(slice_fps),
                "slices": [{"note_ref": lookup(p), "title": p.stem} for p in slice_fps],
                "already_sliced": True,
                "path_sealed": True,
            }, all_md
        raise VaultStoreError("E_PARAM_CONFLICT", "目标切片目录已存在切片（避免覆盖已有切片）", exit_code=1)

    plan = plan_adaptive(body, doc_title, min_chars, max_chars, target_chars)
    cards = plan["cards"]
    if not cards:
        raise VaultStoreError("E_PARAM_INVALID", "源文档没有可切片正文", exit_code=1)
    slice_status = "active" if dom["default_mode"] == "auto" else "pending"
    target_pid = str(dom["project_id"])
    existing = batch.names if batch else {Path(path).name.casefold() for path in _scan_vault_markdowns(vault_dir)}
    toc_title = f"TOC-{doc_stem}"
    toc_rel = validate_note_rel_path(f"{cluster_dir.relative_to(vault_dir).as_posix()}/{toc_title}.md")
    base_pfx = generate_compact_prefix(doc_stem, custom_prefix)

    def _build_slice_plan(use_pfx: str) -> Tuple[List[str], List[Tuple[str, str, str, str]], List[str]]:
        c_rels: List[str] = []
        m_rows: List[Tuple[str, str, str, str]] = []
        for index, card in enumerate(cards, 1):
            card["slice_id"] = f"slice_{index:02d}"
            card["stem"] = f"{use_pfx}-{index:02d}-{sanitize_filename(card['title'])}"
            card["rel"] = validate_note_rel_path(f"{cluster_dir.relative_to(vault_dir).as_posix()}/{card['stem']}.md")
            c_rels.append(card["rel"])
            m_rows.append((card["slice_id"], card["stem"], card["title"], card["summary"]))
        pl = [*c_rels, toc_rel]
        return c_rels, m_rows, [Path(p).name.casefold() for p in pl]

    pfx = base_pfx
    created_rels, slice_meta_rows, names = _build_slice_plan(pfx)
    # Auto-disambiguate prefix with short hash when no explicit --prefix was given
    if (len(names) != len(set(names)) or existing.intersection(names)) and not (custom_prefix and custom_prefix.strip()):
        import hashlib
        h4 = hashlib.sha256(src_rel.encode("utf-8")).hexdigest()[:4]
        for attempt in range(5):
            sfx = h4 if attempt == 0 else f"{h4}{attempt}"
            cand_pfx = f"{base_pfx[:12]}-{sfx}"
            created_rels, slice_meta_rows, names = _build_slice_plan(cand_pfx)
            if len(names) == len(set(names)) and not existing.intersection(names):
                pfx = cand_pfx
                break
    if len(names) != len(set(names)) or existing.intersection(names):
        raise VaultStoreError("E_PARAM_CONFLICT", "切片或TOC名称与现有卡片冲突；未覆盖，请指定唯一prefix或调整源文档标题", exit_code=1)

    for index, card in enumerate(cards):
        previous = f"[[{cards[index-1]['stem']}]]" if index else "*(首节)*"
        following = f"[[{cards[index+1]['stem']}]]" if index + 1 < len(cards) else "*(尾节)*"
        slice_md = (
            "---\n"
            "doc_type: slice\n"
            f'title: "{fm_scalar(card["title"])}"\n'
            f'summary: "{fm_scalar(card["summary"])}"\n'
            f"status: {slice_status}\n"
            f'slice_id: "{card["slice_id"]}"\n'
            f'source_doc: "[[{fm_scalar(doc_stem)}]]"\n'
            f'chapter_path: "{fm_scalar(card["chapter_path"])}"\n'
            f'map: ["[[{fm_scalar(toc_title)}]]"]\n'
            f'has_code: {str(card["has_code"]).lower()}\n'
            f'project_id: "{fm_scalar(target_pid)}"\n'
            'tags: ["切片"]\n'
            f"updated: {_today()}\n"
            "---\n\n"
            f"> 导航：[[{toc_title}|返回骨架目录]] | 原文：[[{doc_stem}]] | 上一节：{previous} | 下一节：{following}\n\n"
            f"{card['content']}\n"
        )
        _atomic_write_md(vault_dir / card["rel"], slice_md)

    toc_body_blocks = []
    def link(card):
        return f"- `{card['slice_id']}` [[{card['stem']}|{card.get('display_title', card['title'])}]] — {card['summary']}"
    preamble = [card for card in cards if card.get("is_preamble")]
    if preamble:
        toc_body_blocks += ["## 前言与背景导读", *[link(card) for card in preamble]]
    by_head = {card["head_idx"]: card for card in cards}
    def render(idx):
        h = plan["headings"][idx]
        state = plan["node_status"].get(idx, "inline")
        if state == "bundle_member":
            return
        card = by_head.get(idx)
        title = card.get("display_title", h["title"]) if card else h["title"]
        toc_body_blocks.append("#" * min(6, h["level"] + 1) + " " + title)
        if card:
            toc_body_blocks.append(link(card))
        if state == "split":
            lead = by_head.get(f"lead_{idx}")
            if lead:
                toc_body_blocks.append(link(lead))
        if state == "leaf_split":
            toc_body_blocks.extend(link(c) for c in cards if c.get("split_node_idx") == idx and not c.get("is_lead_overview"))
        if state in ("split", "inline"):
            for child in h["children"]:
                render(child)
    if plan["headings"]:
        for root in plan["roots"]:
            render(root)
    else:
        toc_body_blocks += ["## 核心内容切片导读", *[link(c) for c in cards]]
    toc_lines = [
        "---", "doc_type: toc", f'title: "{fm_scalar(toc_title)}"',
        f'summary: "{fm_scalar(doc_title[:120])} 自适应章节切片骨架（共 {len(cards)} 片）"',
        f"status: {slice_status}", f'source_doc: "[[{fm_scalar(doc_stem)}]]"',
        f'project_id: "{fm_scalar(target_pid)}"', f"total_slices: {len(cards)}",
        'tags: ["切片", "TOC"]', f"updated: {_today()}", "---", "",
        f"# {doc_title} · 切片目录骨架", "", f"> 原文档：[[{doc_stem}]]", "",
        *toc_body_blocks, "",
    ]
    _atomic_write_md(vault_dir / toc_rel, "\n".join(toc_lines))
    all_created = [*created_rels, toc_rel]

    source_processed = False
    src_dt = str(meta.get("doc_type") or "").strip().lower()
    if has_fm and src_dt in ("library", "artifact"):
        if meta.get("processed") is not True:
            updated_src = replace_frontmatter_field(txt, "processed", "true")
            _atomic_write_md(src_path, updated_src)
        source_processed = True

    if batch:
        batch.paths.update(all_created)
        batch.names.update(names)
        all_md = []
        mapping = {rel: rel for rel in all_created}
    else:
        all_md = _scan_vault_markdowns(vault_dir)
        mapping = note_reg.sync_paths(vault_ref, all_md)
    slice_refs = [
        {"note_ref": mapping[srel], "slice_id": sid, "title": stitle}
        for srel, (sid, _stem, stitle, _ssum) in zip(created_rels, slice_meta_rows)
    ]
    res: Dict[str, Any] = {
        "vault_ref": vault_ref,
        "source_note_ref": note_ref,
        "source_processed": source_processed,
        "mode": "adaptive",
        "min_chars": min_chars,
        "max_chars": max_chars,
        "target_chars": target_chars,
        "budget_exceeded": [{"note_ref": mapping[c["rel"]], "chars": len(c["content"]), "reason": "indivisible_content_or_attached_context"} for c in cards if c["budget_exceeded"]],
        "domain_scope": dom["scope"],
        "project_ref": dom.get("project_ref"),
        "project_id": target_pid,
        "toc_note_ref": mapping[toc_rel],
        "slice_count": len(slice_refs),
        "slices": slice_refs,
        "path_sealed": True,
    }
    sealed = [src_rel, *all_created, *all_md]
    if dom.get("project_rel_path"):
        sealed.append(str(dom["project_rel_path"]))
    return res, sealed
