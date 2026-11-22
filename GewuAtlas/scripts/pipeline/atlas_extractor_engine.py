#!/usr/bin/env python3
"""Atomic knowledge card distillation, provenance verification, ledger repair, and approval for GewuAtlas.

Ports 116-gwf-v2-R6 `atomic_extractor.py` & `core/provenance.py` onto GewuAtlas opaque handles,
KEK registries, cooperative write locks, frozen/private domain boundaries, and rollback-safe writes.
"""

from __future__ import annotations

import hashlib
import json
import re
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
    _parse_list_arg,
    _public_note,
    _scan_vault_markdowns,
    _slugify_filename,
    _today,
)
from notes.atlas_yamlmini import (
    FrontmatterError,
    apply_provenance_frontmatter,
    derive_provenance_fields,
    drop_frontmatter_field,
    extract_frontmatter,
    extract_wikilinks,
    fm_scalar,
    is_exempt_rel_path,
    normalize_provenance_groups,
    normalize_provenance_refs,
    replace_frontmatter_field,
    validate_note_meta,
)
from storage.note_kek_store import NOTE_HANDLE_RE, KekNoteRegistry, validate_note_rel_path
from storage.project_kek_store import KekProjectRegistry
from storage.vault_kek_store import VaultStoreError

ALLOWED_EPISTEMIC_TYPES = ("OBJ", "SUB", "SYN", "HYP")
_SLICE_LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
_JUNK_CODE_TITLE_RE = re.compile(r"^(?:TC|SAC|GB|ISO|IEC|RFC|IEEE)[\s/_-]?\d+", re.IGNORECASE)
_FROZEN_STATUSES = frozenset({"archived", "deprecated"})
_SOURCE_DOC_TYPES = frozenset({"library", "artifact"})
_TOC_DOC_TYPES = frozenset({"toc", "slice_toc"})
_EXTRACT_INPUT_TYPES = frozenset({"library", "artifact", "toc", "slice_toc", "slice"})


def _is_junk_code_title(title: str) -> bool:
    clean = (title or "").strip()
    if not clean:
        return True
    if _JUNK_CODE_TITLE_RE.match(clean):
        return True
    if len(clean) < 4 and not any("\u4e00" <= ch <= "\u9fff" for ch in clean):
        return True
    return False


def _safe_existing_vault_file(vault_dir: Path, rel: str) -> Path:
    norm = validate_note_rel_path(rel)
    root_res = vault_dir.resolve()
    cur = root_res
    for part in Path(norm).parts:
        cur = cur / part
        if cur.exists() and cur.is_symlink():
            raise VaultStoreError("E_SECURITY_VIOLATION", "拒绝访问符号链接笔记路径", exit_code=2)
    target = cur.resolve()
    if target != root_res and root_res not in target.parents:
        raise VaultStoreError("E_SECURITY_VIOLATION", "笔记路径越界 Vault 根目录", exit_code=2)
    if not target.is_file():
        raise VaultStoreError("E_NOT_FOUND", "目标笔记文件不存在", exit_code=1)
    return target


def _domain_state_for_rel(vault_dir: Path, rel: str) -> Tuple[str, Path, Dict[str, Any]]:
    _, prel = infer_zone_and_project_rel(rel)
    if prel:
        pdir = vault_dir / prel
        meta = read_domain_manifest(pdir, Path(prel).name)
        return str(meta["project_id"]), pdir, meta
    adir = vault_dir / "20-AI-Zone"
    meta = read_domain_manifest(adir, "ai-zone")
    return "ai-zone", adir, meta


def _require_unfrozen_domain(vault_dir: Path, dom: Dict[str, Any]) -> None:
    pid = str(dom.get("project_id") or "ai-zone")
    droot = Path(dom["domain_root"])
    manifest = read_domain_manifest(droot, pid)
    if str(manifest.get("status") or "").strip().lower() in _FROZEN_STATUSES:
        raise VaultStoreError("E_DOMAIN_FROZEN", f"目标域 {pid} 已冻结/归档，禁止萃取或台账写入", exit_code=1)


def _assert_not_journal(rel: str, meta: Optional[Dict[str, Any]] = None) -> None:
    dt = str((meta or {}).get("doc_type") or "").strip().lower()
    if rel.startswith("05-Journal/") or dt == "journal":
        raise VaultStoreError("E_JOURNAL_PROTECTED", "日记（05-Journal）不参与原子萃取或审核定性", exit_code=1)


def _parse_frontmatter_strict(raw: str, label: str = "目标笔记") -> Tuple[Dict[str, Any], str]:
    try:
        meta, body, has_fm = extract_frontmatter(raw)
    except FrontmatterError as exc:
        raise VaultStoreError("E_PARAM_INVALID", f"{label} YAML Frontmatter 损坏: {exc}", exit_code=1) from exc
    if not has_fm:
        raise VaultStoreError("E_PARAM_INVALID", f"{label}缺少 YAML Frontmatter", exit_code=1)
    return meta, body


def _next_atom_seq(atomics_dir: Path, pid: str) -> int:
    pat = re.compile(r"^Atom-" + re.escape(pid) + r"-(\d+)-")
    mx = 0
    if atomics_dir.is_dir():
        for f in atomics_dir.glob("*.md"):
            m = pat.match(f.name)
            if m:
                mx = max(mx, int(m.group(1)))
    return mx + 1


def _strip_leading_heading(body: str) -> str:
    lines = (body or "").strip().splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and re.match(r"^#(?:[ \t]|$)", lines[0].lstrip()):
        lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines).strip()


def _strip_nested_frontmatter(body: str) -> str:
    text = (body or "").strip()
    if text.lstrip().startswith("---"):
        try:
            _, inner, has_fm = extract_frontmatter(text)
            if has_fm:
                return inner.strip()
        except FrontmatterError:
            pass
    return text


def _pick_auto_title(body: str, fallback: str) -> str:
    lines = [ln.strip() for ln in (body or "").splitlines() if ln.strip()]
    for ln in lines:
        if ln.startswith("#"):
            cand = ln.lstrip("#").strip()
            if not _is_junk_code_title(cand):
                return cand
    for ln in lines:
        if ln.startswith("#"):
            return ln.lstrip("#").strip() or fallback
        return ln[:50]
    return fallback


class _EvidenceResolver:
    """116-aligned strict provenance resolver bound to GewuAtlas KEK handles and private/frozen guards."""

    def __init__(
        self,
        *,
        vault_dir: Path,
        vault_ref: str,
        note_reg: KekNoteRegistry,
        target_pid: str,
        allow_pending_local: bool = False,
        all_rels: Optional[List[str]] = None,
    ) -> None:
        self.vault_dir = vault_dir
        self.vault_ref = vault_ref
        self.note_reg = note_reg
        self.target_pid = target_pid
        self.allow_pending_local = allow_pending_local
        self.all_rels = all_rels if all_rels is not None else _scan_vault_markdowns(vault_dir)
        self.stem_index: Dict[str, List[str]] = {}
        self.rel_set = set(self.all_rels)
        self._meta_cache: Dict[str, Dict[str, Any]] = {}
        self._manifest_cache: Dict[str, Dict[str, Any]] = {}
        self._rel_to_ref: Optional[Dict[str, str]] = None

        for rel in self.all_rels:
            if not (rel.startswith("20-AI-Zone/") or rel.startswith("30-Projects/")):
                continue
            if is_exempt_rel_path(rel) or infer_zone_and_project_rel(rel)[0] == "workbench":
                continue
            stem = Path(rel).stem
            self.stem_index.setdefault(stem, []).append(rel)

    def metadata(self, rel: str) -> Dict[str, Any]:
        if rel not in self._meta_cache:
            path = _safe_existing_vault_file(self.vault_dir, rel)
            raw = path.read_text(encoding="utf-8", errors="replace")
            meta, _ = _parse_frontmatter_strict(raw, label="证据文档")
            self._meta_cache[rel] = meta
        return self._meta_cache[rel]

    def _domain_manifest(self, rel: str) -> Tuple[str, Dict[str, Any]]:
        pid, droot, m = _domain_state_for_rel(self.vault_dir, rel)
        key = str(droot)
        if key not in self._manifest_cache:
            self._manifest_cache[key] = m
        return pid, self._manifest_cache[key]

    def resolve_ref(self, ref_or_link: str, allowed_types: Sequence[str]) -> str:
        raw = str(ref_or_link or "").strip()
        if not raw:
            raise VaultStoreError("E_PARAM_INVALID", "证据引用不能为空", exit_code=1)
        inner_raw = raw[2:-2].strip() if raw.startswith("[[") and raw.endswith("]]") else raw

        if NOTE_HANDLE_RE.fullmatch(inner_raw):
            rec = self.note_reg.resolve_handle(self.vault_ref, inner_raw)
            rel = str(rec["note_rel_path"])
        else:
            norm_links = normalize_provenance_refs(raw)
            name = norm_links[0][2:-2]
            if ".." in Path(name).parts:
                raise VaultStoreError("E_PARAM_INVALID", "证据引用禁止包含路径穿越", exit_code=1)
            if "/" in name:
                cand = f"{name}.md"
                hits = [cand] if cand in self.rel_set else []
            else:
                hits = self.stem_index.get(name, [])
            if not hits:
                raise VaultStoreError("E_NOT_FOUND", f"证据目标不存在: {name}", exit_code=1)
            if len(hits) != 1:
                raise VaultStoreError("E_PARAM_CONFLICT", f"证据引用存在同名歧义，请使用逻辑句柄消歧: {name}", exit_code=1)
            rel = hits[0]

        _safe_existing_vault_file(self.vault_dir, rel)
        zone, _ = infer_zone_and_project_rel(rel)
        if not (rel.startswith("20-AI-Zone/") or rel.startswith("30-Projects/")) or zone == "workbench" or is_exempt_rel_path(rel):
            raise VaultStoreError("E_PARAM_INVALID", "证据目标不能位于日记、工作台或豁免区", exit_code=1)
        _assert_not_journal(rel)

        meta = self.metadata(rel)
        dt = str(meta.get("doc_type") or "").strip().lower()
        st = str(meta.get("status") or "").strip().lower()
        if dt not in allowed_types:
            raise VaultStoreError("E_PARAM_INVALID", f"证据类型不合法: 期望 {'/'.join(allowed_types)}，实际 {dt or '未知'}", exit_code=1)

        note_pid, manifest = self._domain_manifest(rel)
        local_pending = (
            self.allow_pending_local
            and note_pid == self.target_pid
            and st == "pending"
            and dt in ("library", "toc", "slice_toc", "slice")
        )
        if st != "active" and not local_pending:
            raise VaultStoreError("E_PARAM_CONFLICT", f"证据状态不合法（{st or '未知'}），非同监督域 pending 仅允许使用 active 证据", exit_code=1)

        if note_pid != self.target_pid:
            if bool(meta.get("private") is True) or bool(manifest.get("private") is True):
                raise VaultStoreError("E_PARAM_CONFLICT", "不得穿透引用其他私有域证据", exit_code=1)
        return rel

    def link_for(self, rel: str) -> str:
        stem = Path(rel).stem
        if len(self.stem_index.get(stem, [])) == 1:
            return f"[[{stem}]]"
        no_ext = rel[:-3] if rel.endswith(".md") else rel
        return f"[[{no_ext}]]"

    def handle_for(self, rel: str) -> str:
        if self._rel_to_ref is None:
            cur = self.note_reg.list_vault_handles(self.vault_ref)
            if not set(self.all_rels).issubset(set(cur.keys())):
                cur = self.note_reg.sync_paths(self.vault_ref, list(self.all_rels))
            self._rel_to_ref = cur
        return str(self._rel_to_ref.get(rel) or "")

    def format_wikilink_with_handle(self, link_or_rel: str) -> Tuple[str, str]:
        raw = str(link_or_rel or "").strip()
        if not raw:
            return "", ""
        if raw in self.rel_set:
            rel = raw
        else:
            inner = raw[2:-2].strip() if raw.startswith("[[") and raw.endswith("]]") else raw
            inner = inner.split("|", 1)[0].split("#", 1)[0].strip()
            if inner.lower().endswith(".md"):
                inner = inner[:-3]
            if "/" in inner:
                cand = f"{inner}.md"
                rel = cand if cand in self.rel_set else ""
            else:
                hits = self.stem_index.get(inner, [])
                rel = hits[0] if len(hits) == 1 else ""
        if not rel:
            return (raw if raw.startswith("[[") else f"[[{raw}]]"), ""
        link_raw = self.link_for(rel)
        target_key = link_raw[2:-2]
        meta = self.metadata(rel)
        title = str(meta.get("title") or Path(rel).stem).strip()
        wl = f"[[{target_key}|{title}]]" if (title and title != target_key) else f"[[{target_key}]]"
        return wl, self.handle_for(rel)

    def render_reference_index_bullets(self, groups: Sequence[Dict[str, Any]]) -> List[str]:
        bullets: List[str] = []
        for grp in groups:
            src_wl, src_ref = self.format_wikilink_with_handle(str(grp.get("source") or ""))
            details: List[str] = []
            if src_ref:
                details.append(f"`{src_ref}`")
            toc_raw = str(grp.get("toc") or "").strip()
            if toc_raw:
                toc_wl, toc_ref = self.format_wikilink_with_handle(toc_raw)
                details.append(f"目录：{toc_wl}" + (f" `{toc_ref}`" if toc_ref else ""))
            slice_items: List[str] = []
            for sl_raw in (grp.get("slices") or []):
                sl_wl, sl_ref = self.format_wikilink_with_handle(str(sl_raw))
                slice_items.append(f"{sl_wl}" + (f" `{sl_ref}`" if sl_ref else ""))
            if slice_items:
                details.append(f"关联切片：{'、'.join(slice_items)}")
            suffix = f"（{'，'.join(details)}）" if details else ""
            bullets.append(f"  - {src_wl}{suffix}")
        return bullets

    def extract_body_provenance_refs(self, body_or_raw: str) -> Tuple[List[str], List[str], List[str]]:
        src_refs: List[str] = []
        toc_refs: List[str] = []
        slc_refs: List[str] = []
        for raw_target in extract_wikilinks(body_or_raw or ""):
            inner = str(raw_target or "").strip()
            if not inner:
                continue
            if "/" in inner:
                cand = f"{inner}.md"
                hits = [cand] if cand in self.rel_set else []
            else:
                hits = self.stem_index.get(inner, [])
            if len(hits) != 1:
                continue
            rel = hits[0]
            try:
                dt = str(self.metadata(rel).get("doc_type") or "").strip().lower()
            except Exception:
                continue
            link_val = self.link_for(rel)
            if dt in _SOURCE_DOC_TYPES and link_val not in src_refs:
                src_refs.append(link_val)
            elif dt in _TOC_DOC_TYPES and link_val not in toc_refs:
                toc_refs.append(link_val)
            elif dt == "slice" and link_val not in slc_refs:
                slc_refs.append(link_val)
        return src_refs, toc_refs, slc_refs

    def origin_rel(self, rel: str) -> str:
        meta = self.metadata(rel)
        dt = str(meta.get("doc_type") or "").strip().lower()
        if dt in _SOURCE_DOC_TYPES:
            return rel
        src_doc = str(meta.get("source_doc") or meta.get("source") or "").strip()
        if not src_doc:
            raise VaultStoreError("E_PARAM_INVALID", f"证据缺少 source_doc 溯源链接: {Path(rel).stem}", exit_code=1)
        return self.resolve_ref(src_doc, tuple(_SOURCE_DOC_TYPES))

    def build_groups(
        self,
        *,
        source_rels: Sequence[str] = (),
        toc_rels: Sequence[str] = (),
        slice_rels: Sequence[str] = (),
        explicit: Optional[List[Dict[str, Any]]] = None,
        primary_origin_rel: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        groups: List[Dict[str, Any]] = []

        def add(source_rel: str, toc_rel: Optional[str] = None, slice_rel: Optional[str] = None) -> None:
            s_link = self.link_for(source_rel)
            t_link = self.link_for(toc_rel) if toc_rel is not None else ""
            grp = next((g for g in groups if g["source"] == s_link and g["toc"] == t_link), None)
            if grp is None:
                grp = {"source": s_link, "toc": t_link, "slices": []}
                groups.append(grp)
            if slice_rel is not None:
                sl_link = self.link_for(slice_rel)
                if sl_link not in grp["slices"]:
                    grp["slices"].append(sl_link)

        if explicit is not None:
            if not isinstance(explicit, list) or not explicit:
                raise VaultStoreError("E_PARAM_INVALID", "--provenance 必须为非空列表", exit_code=1)
            for raw_item in explicit:
                if isinstance(raw_item, str):
                    try:
                        item = normalize_provenance_groups([raw_item])[0]
                    except ValueError as exc:
                        raise VaultStoreError("E_PARAM_INVALID", str(exc), exit_code=1) from exc
                elif isinstance(raw_item, dict):
                    item = raw_item
                else:
                    raise VaultStoreError("E_PARAM_INVALID", "--provenance 每项必须为对象或格式化字符串", exit_code=1)
                allowed_keys = {"source", "source_ref", "toc", "toc_ref", "slices", "slice_refs"}
                unknown = set(item) - allowed_keys
                if unknown:
                    raise VaultStoreError("E_PARAM_INVALID", f"--provenance 包含未定义字段: {', '.join(sorted(unknown))}", exit_code=1)
                s_val = str(item.get("source_ref") or item.get("source") or "").strip()
                if not s_val:
                    raise VaultStoreError("E_PARAM_INVALID", "--provenance 每个证据组必须包含 source_ref/source", exit_code=1)
                src_r = self.resolve_ref(s_val, tuple(_SOURCE_DOC_TYPES))
                t_val = str(item.get("toc_ref") or item.get("toc") or "").strip()
                toc_r = self.resolve_ref(t_val, tuple(_TOC_DOC_TYPES)) if t_val else None
                if toc_r is not None and self.origin_rel(toc_r) != src_r:
                    raise VaultStoreError("E_PARAM_INVALID", "TOC 归属与 source 不一致", exit_code=1)
                add(src_r, toc_rel=toc_r)
                raw_slices = item.get("slice_refs") if item.get("slice_refs") is not None else item.get("slices", [])
                if raw_slices is None:
                    raw_slices = []
                if not isinstance(raw_slices, list):
                    raise VaultStoreError("E_PARAM_INVALID", "证据组 slice_refs/slices 必须为列表", exit_code=1)
                for sl_item in raw_slices:
                    sl_r = self.resolve_ref(str(sl_item), ("slice",))
                    if self.origin_rel(sl_r) != src_r:
                        raise VaultStoreError("E_PARAM_INVALID", f"切片归属与 source 不一致: {Path(sl_r).stem}", exit_code=1)
                    if toc_r is not None:
                        mapped_tocs: List[str] = []
                        for m_ref in normalize_provenance_refs(self.metadata(sl_r).get("map", [])):
                            m_stem = m_ref[2:-2].rsplit("/", 1)[-1]
                            if m_stem.startswith("TOC-"):
                                mapped_tocs.append(self.resolve_ref(m_ref, tuple(_TOC_DOC_TYPES)))
                        if toc_r not in mapped_tocs:
                            raise VaultStoreError("E_PARAM_INVALID", f"切片未关联指定 TOC: {Path(sl_r).stem}", exit_code=1)
                    add(src_r, toc_rel=toc_r, slice_rel=sl_r)

            if primary_origin_rel is not None:
                p_link = self.link_for(primary_origin_rel)
                if p_link not in [g["source"] for g in groups]:
                    raise VaultStoreError("E_PARAM_INVALID", "萃取入口不属于显式 provenance 证据来源", exit_code=1)
        else:
            for s_rel in source_rels:
                s_resolved = self.resolve_ref(s_rel, tuple(_SOURCE_DOC_TYPES))
                add(s_resolved)
            for t_rel in toc_rels:
                t_resolved = self.resolve_ref(t_rel, tuple(_TOC_DOC_TYPES))
                add(self.origin_rel(t_resolved), toc_rel=t_resolved)
            for sl_rel in slice_rels:
                sl_resolved = self.resolve_ref(sl_rel, ("slice",))
                s_resolved = self.origin_rel(sl_resolved)
                mapped = False
                for m_ref in normalize_provenance_refs(self.metadata(sl_resolved).get("map", [])):
                    m_stem = m_ref[2:-2].rsplit("/", 1)[-1]
                    if not m_stem.startswith("TOC-"):
                        continue
                    toc_resolved = self.resolve_ref(m_ref, tuple(_TOC_DOC_TYPES))
                    if self.origin_rel(toc_resolved) != s_resolved:
                        raise VaultStoreError("E_PARAM_INVALID", f"切片 map 与 source_doc 归属不一致: {Path(sl_resolved).stem}", exit_code=1)
                    add(s_resolved, toc_rel=toc_resolved, slice_rel=sl_resolved)
                    mapped = True
                if not mapped:
                    add(s_resolved, slice_rel=sl_resolved)

        if not groups:
            raise VaultStoreError("E_PARAM_INVALID", "未提供有效证据来源", exit_code=1)

        # Collapse source/toc placeholder if concrete group for same source already has toc or slices
        filtered = [
            g
            for g in groups
            if g["toc"] or g["slices"] or not any(h["source"] == g["source"] and (h["toc"] or h["slices"]) for h in groups)
        ]
        try:
            return derive_provenance_fields(filtered)["provenance"]
        except ValueError as exc:
            raise VaultStoreError("E_PARAM_INVALID", f"provenance 派生校验失败: {exc}", exit_code=1) from exc


def _append_to_extracted_to(raw_txt: str, card_link: str, *, label: str = "台账文档") -> str:
    meta, _ = _parse_frontmatter_strict(raw_txt, label=label)
    cur = meta.get("extracted_to")
    if cur is None:
        merged: List[str] = []
    elif isinstance(cur, list) and all(isinstance(x, str) for x in cur):
        merged = list(cur)
    else:
        raise VaultStoreError(
            "E_PARAM_INVALID",
            f"{label} extracted_to 字段格式损坏，请先执行 --mode repair-toc-ledger 恢复",
            exit_code=1,
        )
    if card_link not in merged:
        merged.append(card_link)
    arr_yaml = ", ".join(f'"{fm_scalar(x)}"' for x in merged)
    updated = replace_frontmatter_field(raw_txt, "extracted_to", f"[{arr_yaml}]")
    updated = replace_frontmatter_field(updated, "updated", _today())
    return updated


def _write_files_tx(vault_dir: Path, changes: Dict[Path, str]) -> None:
    """Write multiple Markdown files under cooperative locks with pre-write version checks & rollback."""
    originals: Dict[Path, Optional[bytes]] = {}
    expected_hashes: Dict[Path, Optional[str]] = {}
    for path in changes:
        if path.exists():
            if path.is_symlink():
                raise VaultStoreError("E_SECURITY_VIOLATION", "拒绝覆盖符号链接目标", exit_code=2)
            raw_b = path.read_bytes()
            originals[path] = raw_b
            expected_hashes[path] = hashlib.sha256(raw_b).hexdigest()
        else:
            originals[path] = None
            expected_hashes[path] = None

    written: List[Path] = []
    try:
        for path, new_text in changes.items():
            exp = expected_hashes[path]
            if exp is None and path.exists():
                raise VaultStoreError("E_PARAM_CONFLICT", "目标卡片在写入前已存在", exit_code=1)
            _atomic_write_md(path, new_text, expected_sha256=exp)
            written.append(path)
    except Exception as exc:
        for path in reversed(written):
            try:
                orig = originals[path]
                if orig is None:
                    if path.exists():
                        path.unlink()
                else:
                    _atomic_write_md(path, orig.decode("utf-8"))
            except Exception:
                pass
        if isinstance(exc, VaultStoreError):
            raise
        raise VaultStoreError("E_WRITE_LOCK", f"多文件台账事务写入失败已回滚: {exc}", exit_code=1) from exc


_REF_INDEX_HEADING_RE = re.compile(r"(?m)^##\s*(?:参考来源与知识库索引|参考来源|溯源与参考来源)\b")


def _ensure_reference_index_section(
    body_text: str,
    groups: Sequence[Dict[str, Any]],
    *,
    resolver: Optional[_EvidenceResolver] = None,
) -> str:
    """Append or complete the final '## 参考来源与知识库索引' chapter in Format A (Obsidian wikilink + nte_XX handle)."""
    if resolver is not None:
        bullets = resolver.render_reference_index_bullets(groups)
    else:
        bullets = []
        for grp in groups:
            src_s = str(grp.get("source") or "").strip()
            toc_s = str(grp.get("toc") or "").strip()
            slc_s = [str(x).strip() for x in (grp.get("slices") or []) if str(x).strip()]
            details: List[str] = []
            if toc_s:
                details.append(f"目录：{toc_s}")
            if slc_s:
                details.append(f"关联切片：{'、'.join(slc_s)}")
            suffix = f"（{'，'.join(details)}）" if details else ""
            bullets.append(f"  - {src_s}{suffix}")
    base = (body_text or "").rstrip()
    m = _REF_INDEX_HEADING_RE.search(base)
    if not m:
        section = "\n".join(
            [
                "## 参考来源与知识库索引",
                "",
                "- **一手文献与切片来源**：",
                *bullets,
            ]
        )
        return f"{base}\n\n{section}".strip()
    footer = base[m.start() :]
    existing_stems = {_link_stem(w) for w in extract_wikilinks(footer) if _link_stem(w)}
    missing_bullets: List[str] = []
    for grp, bullet in zip(groups, bullets):
        needed_stems = {_link_stem(str(grp.get("source") or ""))}
        if grp.get("toc"):
            needed_stems.add(_link_stem(str(grp["toc"])))
        for sl in (grp.get("slices") or []):
            needed_stems.add(_link_stem(str(sl)))
        needed_stems.discard("")
        if not needed_stems.issubset(existing_stems):
            missing_bullets.append(bullet)
    if not missing_bullets:
        return base
    if "- **一手文献与切片来源**：" in footer:
        return f"{base}\n" + "\n".join(missing_bullets)
    return f"{base}\n- **一手文献与切片来源**：\n" + "\n".join(missing_bullets)


def _create_single_atomic_card(
    *,
    vault_dir: Path,
    atomics_dir: Path,
    pid: str,
    dom: Dict[str, Any],
    seq: int,
    title: str,
    summary: str,
    body_text: str,
    epistemic_type: str,
    tags: List[str],
    aliases: List[str],
    by_actor: str,
    groups: List[Dict[str, Any]],
    resolver: Optional[_EvidenceResolver] = None,
) -> Tuple[str, str, str]:
    clean_title = re.sub(r"[\u200b-\u200d\ufeff]+", "", title).strip() or title.strip()
    clean_summary = re.sub(r"[\u200b-\u200d\ufeff]+", "", (summary or clean_title)).strip() or clean_title
    slug = _slugify_filename(clean_title)
    fname = f"Atom-{pid}-{seq:03d}-{slug}.md"
    rel = validate_note_rel_path(f"{atomics_dir.relative_to(vault_dir).as_posix()}/{fname}")
    target = vault_dir / rel
    if target.exists():
        raise VaultStoreError("E_PARAM_CONFLICT", "目标 Atomics 分区已存在同名原子卡片", exit_code=1)

    is_auto = dom["default_mode"] == "auto"
    status = "active" if is_auto else "pending"
    reviewed_by = "ai" if is_auto else ""
    merged_tags = list(dict.fromkeys(["Atom", "萃取", *tags]))
    tags_yaml = ", ".join(f'"{fm_scalar(t)}"' for t in merged_tags)
    aliases_yaml = ", ".join(f'"{fm_scalar(a)}"' for a in aliases)
    clean_body = _strip_nested_frontmatter(_strip_leading_heading(body_text))
    core_prose = clean_body or f"溯源自 {groups[0]['source']}。"
    body_with_refs = _ensure_reference_index_section(core_prose, groups, resolver=resolver)
    actor = (by_actor or "").strip()

    lines = [
        "---",
        "doc_type: atomic",
        f'title: "{fm_scalar(clean_title)}"',
        f'summary: "{fm_scalar(clean_summary)}"',
        f"status: {status}",
        f"review_mode: {dom['default_mode']}",
        f"type: {epistemic_type}",
        f'project_id: "{fm_scalar(pid)}"',
        f"tags: [{tags_yaml}]",
        f"aliases: [{aliases_yaml}]",
        'supersedes: ""',
        f"updated: {_today()}",
    ]
    if actor:
        lines.append(f'created_by: "{fm_scalar(actor)}"')
        lines.append(f'updated_by: "{fm_scalar(actor)}"')
    if reviewed_by:
        lines.append(f"reviewed_by: {reviewed_by}")
    lines.extend(
        [
            "---",
            "",
            f"# {clean_title}",
            "",
            f"> 核心命题：{clean_summary}",
            "",
            body_with_refs,
            "",
        ]
    )
    try:
        rendered = apply_provenance_frontmatter("\n".join(lines), groups)
    except ValueError as exc:
        raise VaultStoreError("E_PARAM_INVALID", f"provenance 写入校验失败: {exc}", exit_code=1) from exc
    if "[[" in rendered and resolver is not None:
        from notes.atlas_wikilink_tools import build_vault_link_index, normalize_inline_wikilinks

        pre_map = resolver.note_reg.list_vault_handles(resolver.vault_ref)
        if not set(resolver.all_rels).issubset(set(pre_map.keys())):
            pre_map = resolver.note_reg.sync_paths(resolver.vault_ref, list(resolver.all_rels))
        if pre_map:
            idx = build_vault_link_index(vault_dir, pre_map)
            rendered, _ = normalize_inline_wikilinks(rendered, source_rel=rel, index=idx)
    fmeta, _, _ = extract_frontmatter(rendered)
    errs = validate_note_meta(fmeta, rel_path=rel)
    if errs:
        raise VaultStoreError("E_PARAM_INVALID", "; ".join(errs), exit_code=1)
    return rel, Path(rel).stem, rendered


def _plan_same_domain_ledger_updates(
    *,
    vault_dir: Path,
    resolver: _EvidenceResolver,
    groups: List[Dict[str, Any]],
    target_domain_root: Path,
    card_links: Sequence[str],
    mark_toc_extracted: bool = False,
) -> Dict[Path, str]:
    """Unidirectional DAG mode: never mutate library/artifact/toc `extracted_to`; only mark TOC `extracted=true` when requested."""
    _ = card_links
    if not mark_toc_extracted:
        return {}
    domain_res = target_domain_root.resolve()
    updates: Dict[Path, str] = {}
    for group in groups:
        if not group.get("toc"):
            continue
        toc_rel = resolver.resolve_ref(group["toc"], tuple(_TOC_DOC_TYPES))
        c_path = _safe_existing_vault_file(vault_dir, toc_rel)
        if c_path != domain_res and domain_res not in c_path.parents:
            continue
        if c_path in updates:
            continue
        txt = c_path.read_text(encoding="utf-8")
        meta, _ = _parse_frontmatter_strict(txt, label="证据台账")
        dt = str(meta.get("doc_type") or "").strip().lower()
        if dt in _TOC_DOC_TYPES and meta.get("extracted") is not True:
            updates[c_path] = replace_frontmatter_field(txt, "extracted", "true")
    return updates


def _link_stem(raw_ref: str) -> str:
    txt = str(raw_ref or "").strip()
    if txt.startswith("[[") and txt.endswith("]]"):
        txt = txt[2:-2]
    txt = txt.split("|", 1)[0].split("#", 1)[0].strip()
    if txt.lower().endswith(".md"):
        txt = txt[:-3]
    return txt.rsplit("/", 1)[-1].strip()


def _collect_atomic_backref_counts(vault_dir: Path, all_md: Sequence[str]) -> Tuple[Dict[str, set[str]], Dict[str, set[str]]]:
    """Dynamically compute derived atomic cards per source/TOC stem without writing back `extracted_to`."""
    by_source_stem: Dict[str, set[str]] = {}
    by_slice_stem: Dict[str, set[str]] = {}
    slice_to_toc_stems: Dict[str, set[str]] = {}

    # First pass: collect slice -> toc map & atomic -> sources/slices/toc backrefs
    for rel in all_md:
        if is_exempt_rel_path(rel):
            continue
        txt = (vault_dir / rel).read_text(encoding="utf-8", errors="replace")
        try:
            meta, _, has_fm = extract_frontmatter(txt)
        except FrontmatterError:
            continue
        if not has_fm:
            continue
        dt = str(meta.get("doc_type") or "").strip().lower()
        stem = Path(rel).stem
        if dt == "slice":
            raw_maps = meta.get("map")
            if isinstance(raw_maps, list):
                for m in raw_maps:
                    ts = _link_stem(str(m))
                    if ts:
                        slice_to_toc_stems.setdefault(stem, set()).add(ts)
            elif isinstance(raw_maps, str) and raw_maps.strip():
                ts = _link_stem(raw_maps)
                if ts:
                    slice_to_toc_stems.setdefault(stem, set()).add(ts)
        elif dt == "atomic":
            if str(meta.get("status") or "").strip().lower() in _FROZEN_STATUSES:
                continue
            src_vals: List[str] = []
            for key in ("sources", "source"):
                val = meta.get(key)
                if isinstance(val, list):
                    src_vals.extend(str(x) for x in val)
                elif isinstance(val, str) and val.strip():
                    src_vals.append(val)
            for sv in src_vals:
                ss = _link_stem(sv)
                if ss:
                    by_source_stem.setdefault(ss, set()).add(stem)
            for slv in (meta.get("slices") if isinstance(meta.get("slices"), list) else []):
                sls = _link_stem(str(slv))
                if sls:
                    by_slice_stem.setdefault(sls, set()).add(stem)
            for tv in (meta.get("toc") if isinstance(meta.get("toc"), list) else []):
                ts = _link_stem(str(tv))
                if ts:
                    by_source_stem.setdefault(ts, set()).add(stem)
            for body_link in extract_wikilinks(txt):
                bs = _link_stem(body_link)
                if bs and bs != stem:
                    by_source_stem.setdefault(bs, set()).add(stem)
                    by_slice_stem.setdefault(bs, set()).add(stem)

    # Propagate slice -> toc associations into by_source_stem keyed by toc stem
    for sl_stem, atom_stems in by_slice_stem.items():
        for toc_stem in slice_to_toc_stems.get(sl_stem, ()):
            by_source_stem.setdefault(toc_stem, set()).update(atom_stems)

    return by_source_stem, by_slice_stem


def extract_discovery(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    proj_reg: KekProjectRegistry,
    kek: bytes,
    app_id: str,
    mode: str,
    project_ref: Optional[str] = None,
    include_done: bool = False,
) -> Tuple[Dict[str, Any], List[str]]:
    """Discover pending atomic cards (`list-pending`) or unextracted TOC/library inputs (`unextracted`)."""
    all_md = _scan_vault_markdowns(vault_dir)
    n_map = note_reg.sync_paths(vault_ref, all_md)
    by_source_stem: Dict[str, set[str]] = {}
    if mode == "unextracted":
        by_source_stem, _ = _collect_atomic_backref_counts(vault_dir, all_md)

    pid_filter: Optional[str] = None
    if project_ref:
        dom = resolve_domain_scope(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            proj_reg=proj_reg,
            explicit_project_ref=project_ref,
            scope="project",
        )
        pid_filter = str(dom["project_id"])

    frozen_pids: set[str] = set()
    for prel in scan_project_rels(vault_dir):
        m = read_domain_manifest(vault_dir / prel, Path(prel).name)
        if str(m.get("status") or "").strip().lower() in _FROZEN_STATUSES:
            frozen_pids.add(str(m["project_id"]))

    items: List[Dict[str, Any]] = []
    unstarted_count = 0
    in_progress_count = 0
    done_count = 0

    for rel in all_md:
        if is_exempt_rel_path(rel):
            continue
        fname = Path(rel).name
        if mode == "unextracted" and ("companion" in fname.lower() or "伴生" in fname or fname.startswith("_")):
            continue
        txt = (vault_dir / rel).read_text(encoding="utf-8", errors="replace")
        try:
            meta, _, has_fm = extract_frontmatter(txt)
        except FrontmatterError:
            continue
        if not has_fm:
            continue
        dt = str(meta.get("doc_type") or "").strip().lower()
        st = str(meta.get("status") or "").strip().lower()
        mpid = str(meta.get("project_id") or "")
        if pid_filter and mpid != pid_filter:
            continue

        if mode == "list-pending":
            if st != "pending" or dt != "atomic":
                continue
        elif mode == "unextracted":
            if dt not in ("toc", "slice_toc", "library"):
                continue
            if st in _FROZEN_STATUSES or mpid in frozen_pids:
                continue
            stem = Path(rel).stem
            derived_atoms = set(by_source_stem.get(stem, ()))
            if dt in _TOC_DOC_TYPES and meta.get("source_doc"):
                src_s = _link_stem(str(meta.get("source_doc")))
                if src_s:
                    derived_atoms.update(by_source_stem.get(src_s, ()))
            legacy_ext = meta.get("extracted_to") if isinstance(meta.get("extracted_to"), list) else []
            for lx in legacy_ext:
                ls = _link_stem(str(lx))
                if ls:
                    derived_atoms.add(ls)
            if dt in _TOC_DOC_TYPES:
                is_done = bool(meta.get("extracted") is True or meta.get("processed") is True)
            else:
                is_done = bool(meta.get("processed") is True)
            if is_done:
                done_count += 1
                if not include_done:
                    continue
                state = "done"
            elif derived_atoms:
                in_progress_count += 1
                state = "in_progress"
            else:
                unstarted_count += 1
                state = "unstarted"
        else:
            raise VaultStoreError("E_PARAM_INVALID", f"不支持的发现模式: {mode}", exit_code=1)

        pub = _public_note(kek=kek, app_id=app_id, vault_ref=vault_ref, note_ref=n_map[rel], rel=rel, meta=meta)
        if mode == "unextracted":
            pub["processed"] = bool(meta.get("processed", False))
            pub["extracted"] = bool(meta.get("extracted", False))
            pub["extracted_count"] = len(derived_atoms)
            pub["extraction_state"] = state
            if dt in _TOC_DOC_TYPES and meta.get("total_slices") is not None:
                pub["total_slices"] = meta.get("total_slices")
        items.append(pub)

    payload: Dict[str, Any] = {
        "vault_ref": vault_ref,
        "mode": mode,
        "count": len(items),
        "items": items,
        "path_sealed": True,
    }
    if mode == "unextracted":
        payload["unstarted_count"] = unstarted_count
        payload["in_progress_count"] = in_progress_count
        payload["done_count"] = done_count
    return payload, all_md


def mark_source_done(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    proj_reg: KekProjectRegistry,
    kek: bytes,
    app_id: str,
    note_ref: str,
    project_ref: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    rec = note_reg.resolve_handle(vault_ref, note_ref)
    rel = str(rec["note_rel_path"])
    fpath = _safe_existing_vault_file(vault_dir, rel)
    raw = fpath.read_text(encoding="utf-8")
    meta, _ = _parse_frontmatter_strict(raw, label="收口目标")
    _assert_not_journal(rel, meta)

    dt = str(meta.get("doc_type") or "").strip().lower()
    if dt not in ("library", "artifact", "toc", "slice_toc"):
        raise VaultStoreError("E_PARAM_INVALID", "mark-done 仅支持 library、artifact 或 toc", exit_code=1)
    zone, _ = infer_zone_and_project_rel(rel)
    expected_zone = "library" if dt == "library" else ("artifacts" if dt == "artifact" else "slices")
    if zone != expected_zone:
        raise VaultStoreError("E_PARAM_INVALID", f"收口目标所属分区与类型不符（{zone} vs {dt}）", exit_code=1)

    dom = resolve_domain_scope(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        proj_reg=proj_reg,
        explicit_project_ref=project_ref,
        scope="auto",
        source_rel_path=rel,
        source_meta=meta,
    )
    own_pid, own_root, _ = _domain_state_for_rel(vault_dir, rel)
    if project_ref and str(dom["project_id"]) != own_pid:
        raise VaultStoreError("E_PARAM_CONFLICT", "收口目标与指定 --project_ref 不一致", exit_code=1)
    _require_unfrozen_domain(vault_dir, {"project_id": own_pid, "domain_root": own_root})

    is_toc = dt in _TOC_DOC_TYPES or fpath.name.startswith("TOC-")
    changes: Dict[Path, str] = {}
    sealed = [rel]
    parent_processed = False
    pending_sibling_tocs = 0

    rendered = raw
    if is_toc:
        rendered = replace_frontmatter_field(rendered, "extracted", "true")
    else:
        rendered = replace_frontmatter_field(rendered, "processed", "true")
    rendered = replace_frontmatter_field(rendered, "updated", _today())
    changes[fpath] = rendered

    if is_toc and meta.get("source_doc"):
        src_links = normalize_provenance_refs(meta.get("source_doc"))
        src_stem = src_links[0][2:-2].rsplit("/", 1)[-1]
        cand_rels = [
            f"{own_root.relative_to(vault_dir).as_posix()}/20-Memory/21-Library/{src_stem}.md",
            f"{own_root.relative_to(vault_dir).as_posix()}/10-Artifacts/{src_stem}.md",
        ]
        parent_path: Optional[Path] = None
        parent_rel: Optional[str] = None
        for cr in cand_rels:
            cp = vault_dir / cr
            if cp.is_file():
                parent_path = _safe_existing_vault_file(vault_dir, cr)
                parent_rel = cr
                break
        if parent_path is not None and parent_rel is not None:
            p_raw = parent_path.read_text(encoding="utf-8")
            p_meta, _ = _parse_frontmatter_strict(p_raw, label="关联原文")
            p_dt = str(p_meta.get("doc_type") or "").strip().lower()
            if p_dt not in _SOURCE_DOC_TYPES:
                raise VaultStoreError("E_PARAM_INVALID", "关联原文必须是本域 library 或 artifact", exit_code=1)
            slices_dir = own_root / "20-Memory" / "24-Slices"
            if slices_dir.is_dir():
                for sib in sorted(slices_dir.rglob("TOC-*.md")):
                    if sib.is_symlink() or not sib.is_file() or sib.resolve() == fpath.resolve():
                        continue
                    try:
                        sm, _, s_fm = extract_frontmatter(sib.read_text(encoding="utf-8"))
                    except FrontmatterError:
                        continue
                    if not s_fm or not sm.get("source_doc"):
                        continue
                    try:
                        s_links = normalize_provenance_refs(sm.get("source_doc"))
                    except ValueError:
                        continue
                    if s_links and s_links[0][2:-2].rsplit("/", 1)[-1] == src_stem:
                        if sm.get("extracted") is not True and sm.get("processed") is not True:
                            pending_sibling_tocs += 1
            # Parent library/artifact is marked processed=true during note-slice; never mutate parent during TOC mark-done.
            parent_processed = bool(p_meta.get("processed") is True)

    _write_files_tx(vault_dir, changes)
    nmeta, _, _ = extract_frontmatter(changes[fpath])
    pub = _public_note(kek=kek, app_id=app_id, vault_ref=vault_ref, note_ref=note_ref, rel=rel, meta=nmeta)
    pub["processed"] = bool(nmeta.get("processed", False))
    pub["extracted"] = bool(nmeta.get("extracted", False))
    if is_toc:
        pub["parent_processed"] = parent_processed
        pub["pending_sibling_tocs"] = pending_sibling_tocs
    return pub, sealed


def backfill_provenance(
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
    by_actor: str = "",
) -> Tuple[Dict[str, Any], List[str]]:
    """116-aligned deterministic provenance backfill without altering card body or knowledge status."""
    rec = note_reg.resolve_handle(vault_ref, note_ref)
    rel = str(rec["note_rel_path"])
    fpath = _safe_existing_vault_file(vault_dir, rel)
    raw = fpath.read_text(encoding="utf-8")
    meta, _ = _parse_frontmatter_strict(raw, label="回填原子卡")
    _assert_not_journal(rel, meta)

    if str(meta.get("doc_type") or "").strip().lower() != "atomic":
        raise VaultStoreError("E_PARAM_INVALID", "backfill-provenance 仅支持 atomic 原子卡", exit_code=1)

    own_pid, own_root, _ = _domain_state_for_rel(vault_dir, rel)
    dom = resolve_domain_scope(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        proj_reg=proj_reg,
        explicit_project_ref=project_ref,
        scope=scope,
        source_rel_path=rel,
        source_meta=meta,
    )
    if str(dom["project_id"]) != own_pid:
        raise VaultStoreError("E_PARAM_CONFLICT", "原子卡不属于指定目标域", exit_code=1)
    _require_unfrozen_domain(vault_dir, {"project_id": own_pid, "domain_root": own_root})

    all_md = _scan_vault_markdowns(vault_dir)
    resolver = _EvidenceResolver(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        note_reg=note_reg,
        target_pid=own_pid,
        allow_pending_local=(dom["default_mode"] == "supervised" and str(meta.get("status") or "") == "pending"),
        all_rels=all_md,
    )
    raw_sources = meta.get("sources") or meta.get("source")
    raw_tocs = meta.get("toc")
    raw_slices = meta.get("slices")
    explicit = meta.get("provenance") if isinstance(meta.get("provenance"), list) else None
    if explicit is None and not (raw_sources or raw_tocs or raw_slices):
        b_sources, b_tocs, b_slices = resolver.extract_body_provenance_refs(raw)
    else:
        b_sources = normalize_provenance_refs(raw_sources) if explicit is None else []
        b_tocs = normalize_provenance_refs(raw_tocs) if explicit is None else []
        b_slices = normalize_provenance_refs(raw_slices) if explicit is None else []
    groups = resolver.build_groups(
        source_rels=b_sources,
        toc_rels=b_tocs,
        slice_rels=b_slices,
        explicit=explicit,
    )

    updated = apply_provenance_frontmatter(raw, groups)
    if by_actor.strip():
        updated = replace_frontmatter_field(updated, "updated_by", f'"{fm_scalar(by_actor.strip())}"')
    updated = replace_frontmatter_field(updated, "updated", _today())
    fmeta, fbody, _ = extract_frontmatter(updated)
    new_body = _ensure_reference_index_section(fbody, groups, resolver=resolver)
    if new_body.strip() != fbody.strip():
        fm_match = re.match(r"\A---[^\S\n]*\r?\n.*?\r?\n---[^\S\n]*(?:\r?\n)?", updated, re.S)
        if fm_match:
            updated = f"{fm_match.group(0).rstrip()}\n\n{new_body.lstrip()}\n"
            fmeta, _, _ = extract_frontmatter(updated)
    errs = validate_note_meta(fmeta, rel_path=rel)
    if errs:
        raise VaultStoreError("E_PARAM_INVALID", "; ".join(errs), exit_code=1)

    _write_files_tx(vault_dir, {fpath: updated})
    pub = _public_note(kek=kek, app_id=app_id, vault_ref=vault_ref, note_ref=note_ref, rel=rel, meta=fmeta)
    pub["provenance_group_count"] = len(groups)
    return pub, [rel]


def repair_toc_ledger(
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
) -> Tuple[Dict[str, Any], List[str]]:
    """116-aligned deterministic TOC extracted_to rebuild from verified atomic provenance in same domain."""
    rec = note_reg.resolve_handle(vault_ref, note_ref)
    rel = str(rec["note_rel_path"])
    fpath = _safe_existing_vault_file(vault_dir, rel)
    raw = fpath.read_text(encoding="utf-8")
    meta, _ = _parse_frontmatter_strict(raw, label="TOC 台账")
    _assert_not_journal(rel, meta)

    dt = str(meta.get("doc_type") or "").strip().lower()
    if dt not in _TOC_DOC_TYPES:
        raise VaultStoreError("E_PARAM_INVALID", "repair-toc-ledger 仅支持 toc 骨架文档", exit_code=1)

    own_pid, own_root, _ = _domain_state_for_rel(vault_dir, rel)
    dom = resolve_domain_scope(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        proj_reg=proj_reg,
        explicit_project_ref=project_ref,
        scope=scope,
        source_rel_path=rel,
        source_meta=meta,
    )
    if str(dom["project_id"]) != own_pid:
        raise VaultStoreError("E_PARAM_CONFLICT", "TOC 不属于指定目标域", exit_code=1)
    _require_unfrozen_domain(vault_dir, {"project_id": own_pid, "domain_root": own_root})

    all_md = _scan_vault_markdowns(vault_dir)
    resolver = _EvidenceResolver(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        note_reg=note_reg,
        target_pid=own_pid,
        allow_pending_local=(dom["default_mode"] == "supervised"),
        all_rels=all_md,
    )
    target_link = resolver.link_for(rel)
    atomics_dir = own_root / "20-Memory" / "22-Atomics"
    repaired_links: List[str] = []
    if atomics_dir.is_dir():
        for card_path in sorted(atomics_dir.glob("*.md")):
            if card_path.is_symlink() or not card_path.is_file():
                continue
            c_rel = card_path.relative_to(vault_dir).as_posix()
            if is_exempt_rel_path(c_rel):
                continue
            c_raw = card_path.read_text(encoding="utf-8")
            c_meta, _ = _parse_frontmatter_strict(c_raw, label=f"原子卡 {card_path.stem}")
            if str(c_meta.get("doc_type") or "").strip().lower() != "atomic":
                continue
            c_explicit = c_meta.get("provenance") if isinstance(c_meta.get("provenance"), list) else None
            c_sources = c_meta.get("sources") or c_meta.get("source")
            c_tocs = c_meta.get("toc")
            c_slices = c_meta.get("slices")
            if c_explicit is None and not (c_sources or c_tocs or c_slices):
                cb_sources, cb_tocs, cb_slices = resolver.extract_body_provenance_refs(c_raw)
            else:
                cb_sources = normalize_provenance_refs(c_sources) if c_explicit is None else []
                cb_tocs = normalize_provenance_refs(c_tocs) if c_explicit is None else []
                cb_slices = normalize_provenance_refs(c_slices) if c_explicit is None else []
            c_groups = resolver.build_groups(
                source_rels=cb_sources,
                toc_rels=cb_tocs,
                slice_rels=cb_slices,
                explicit=c_explicit,
            )
            if any(g.get("toc") == target_link for g in c_groups):
                repaired_links.append(f"[[{card_path.stem}]]")

    updated = drop_frontmatter_field(raw, "extracted_to")
    if updated != raw:
        updated = replace_frontmatter_field(updated, "updated", _today())
        _write_files_tx(vault_dir, {fpath: updated})

    nmeta, _, _ = extract_frontmatter(updated)
    pub = _public_note(kek=kek, app_id=app_id, vault_ref=vault_ref, note_ref=note_ref, rel=rel, meta=nmeta)
    pub["repaired_count"] = len(repaired_links)
    pub["derived_atomic_count"] = len(repaired_links)
    return pub, [rel]


def _parse_provenance_arg(raw_arg: Any) -> List[Any]:
    """Parse --provenance supporting native JSON list/dict, JSON string, or pipe-formatted group strings."""
    fmt_hint = (
        '请传入 JSON 列表 \'[{"source_ref":"nte_XX","toc_ref":"nte_YY","slice_refs":["nte_ZZ"]}]\' '
        '或管道格式 \'"source=[[...]] | toc=[[...]] | slices=[[...]]"\''
    )
    if isinstance(raw_arg, dict):
        return [raw_arg]
    if isinstance(raw_arg, list):
        if not raw_arg:
            raise VaultStoreError("E_PARAM_INVALID", f"--provenance 必须为非空列表；{fmt_hint}", exit_code=1)
        return raw_arg
    raw = str(raw_arg or "").strip()
    if not raw:
        raise VaultStoreError("E_PARAM_INVALID", "--provenance 不能为空", exit_code=1)
    if raw[0] in "[{":
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise VaultStoreError("E_PARAM_INVALID", f"--provenance JSON 解析失败（{exc}）；{fmt_hint}", exit_code=1) from exc
        if isinstance(loaded, dict):
            return [loaded]
        if isinstance(loaded, list) and loaded:
            return loaded
        raise VaultStoreError("E_PARAM_INVALID", f"--provenance 必须为非空列表或证据组对象；{fmt_hint}", exit_code=1)

    if "source=" in raw or "|" in raw:
        items: List[str] = []
        buf = ""
        depth = 0
        for ch in raw:
            if ch == "[":
                depth += 1
            elif ch == "]" and depth > 0:
                depth -= 1
            if depth == 0 and ch in (";", "\n"):
                if buf.strip():
                    items.append(buf.strip())
                buf = ""
                continue
            buf += ch
        if buf.strip():
            items.append(buf.strip())
        try:
            return normalize_provenance_groups(items)
        except ValueError as exc:
            raise VaultStoreError("E_PARAM_INVALID", f"--provenance 格式错误（{exc}）；{fmt_hint}", exit_code=1) from exc

    if NOTE_HANDLE_RE.fullmatch(raw) or (raw.startswith("[[") and raw.endswith("]]")):
        return [{"source_ref": raw}]
    raise VaultStoreError("E_PARAM_INVALID", f"--provenance 格式错误；{fmt_hint}", exit_code=1)


def note_extract(
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
    title: str = "",
    summary: str = "",
    epistemic_type: str = "SYN",
    tags: Any = None,
    aliases: Any = None,
    content: str = "",
    review_mode: str = "inherit",
    slice_refs: Optional[str] = None,
    toc_refs: Optional[str] = None,
    provenance_arg: Optional[str] = None,
    by_actor: str = "",
    auto_approve: bool = False,
) -> Tuple[Dict[str, Any], List[str]]:
    """Distill one or more Atomic Knowledge Cards from a source note (slice/TOC/library/artifact)."""
    ep = (epistemic_type or "SYN").strip().upper()
    if ep not in ALLOWED_EPISTEMIC_TYPES:
        raise VaultStoreError("E_PARAM_INVALID", f"--epistemic_type 仅支持 {'/'.join(ALLOWED_EPISTEMIC_TYPES)}", exit_code=1)

    if provenance_arg and (slice_refs or toc_refs):
        raise VaultStoreError("E_PARAM_INVALID", "--provenance 与 --slice_refs/--toc_refs 不可混用", exit_code=1)

    rec = note_reg.resolve_handle(vault_ref, note_ref)
    src_rel = str(rec["note_rel_path"])
    src_path = _safe_existing_vault_file(vault_dir, src_rel)
    src_raw = src_path.read_text(encoding="utf-8")
    try:
        src_meta, src_body, has_fm = extract_frontmatter(src_raw)
    except FrontmatterError as exc:
        raise VaultStoreError("E_PARAM_INVALID", f"源笔记 YAML 损坏: {exc}", exit_code=1) from exc

    _assert_not_journal(src_rel, src_meta)
    src_zone, _ = infer_zone_and_project_rel(src_rel)
    if src_zone in ("workbench", "dropzone", "attic") or is_exempt_rel_path(src_rel):
        raise VaultStoreError("E_PARAM_INVALID", f"atomic-extract 仅接受正式库、成果或切片/TOC 入口，不支持 {src_zone}", exit_code=1)
    src_dt = str(src_meta.get("doc_type") or "").strip().lower()
    if has_fm and src_dt and src_dt not in _EXTRACT_INPUT_TYPES:
        raise VaultStoreError("E_PARAM_INVALID", f"atomic-extract 不支持 doc_type={src_dt}", exit_code=1)

    dom = resolve_domain_scope(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        proj_reg=proj_reg,
        explicit_project_ref=project_ref,
        scope=scope,
        source_rel_path=src_rel,
        source_meta=src_meta,
    )
    if review_mode not in ("inherit", "auto", "supervised"):
        raise VaultStoreError("E_PARAM_INVALID", "review_mode 仅支持 inherit/auto/supervised", exit_code=1)
    dom = dict(dom)
    if review_mode == "supervised":
        dom["default_mode"] = "supervised"
    if auto_approve and dom["default_mode"] == "supervised":
        raise VaultStoreError(
            "E_SUPERVISED_APPROVAL_REQUIRED",
            "supervised 域严禁使用 --auto_approve 跳过人类审批；请向人类呈报候选及其 content_sha256 后调用 atomic-approve",
            exit_code=1,
        )
    _require_unfrozen_domain(vault_dir, dom)

    pid = str(dom["project_id"])
    atomics_dir = resolve_zone_dir(vault_dir, dom, "atomics")
    atomics_dir.mkdir(parents=True, exist_ok=True)
    seq = _next_atom_seq(atomics_dir, pid)
    tag_list = _parse_list_arg(tags)
    alias_list = _parse_list_arg(aliases)
    actor = (by_actor or "").strip()

    all_md = _scan_vault_markdowns(vault_dir)
    resolver = _EvidenceResolver(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        note_reg=note_reg,
        target_pid=pid,
        allow_pending_local=(dom["default_mode"] == "supervised"),
        all_rels=all_md,
    )

    is_toc = src_dt in _TOC_DOC_TYPES
    is_slice = src_dt == "slice"
    # Verify entry handle against evidence resolver (rejects cross-domain private or wrong status)
    if is_toc:
        resolver.resolve_ref(note_ref, tuple(_TOC_DOC_TYPES))
    elif is_slice:
        resolver.resolve_ref(note_ref, ("slice",))
    else:
        resolver.resolve_ref(note_ref, tuple(_SOURCE_DOC_TYPES))
    primary_origin_rel = resolver.origin_rel(src_rel)

    explicit_groups: Optional[List[Dict[str, Any]]] = None
    if provenance_arg:
        explicit_groups = _parse_provenance_arg(provenance_arg)

    parsed_slice_refs = _parse_list_arg(slice_refs)
    parsed_toc_refs = _parse_list_arg(toc_refs)

    if explicit_groups is not None:
        groups = resolver.build_groups(explicit=explicit_groups, primary_origin_rel=primary_origin_rel)
    else:
        src_inputs = [primary_origin_rel]
        toc_inputs = list(parsed_toc_refs)
        if is_toc and src_rel not in toc_inputs:
            toc_inputs.append(src_rel)
        slice_inputs = list(parsed_slice_refs)
        if is_slice and src_rel not in slice_inputs:
            slice_inputs.append(src_rel)
        groups = resolver.build_groups(source_rels=src_inputs, toc_rels=toc_inputs, slice_rels=slice_inputs)

    if not title.strip() or not content.strip():
        if not is_toc and not is_slice and len(src_body) > 3000:
            raise VaultStoreError(
                "E_PARAM_MISSING",
                f"长篇文献（>{len(src_body)} 字）请先经 note-slice --note_ref {note_ref} --adaptive true 生成 TOC 与切片，"
                "再读取切片提炼并传入 --title '<原子命题>' --summary '<一句断言>' --content '<围绕一个主张写清依据、推理和适用边界，段落自定>'（可用 --action atomic-extract --template 查看写法）",
                exit_code=1,
            )
        raise VaultStoreError(
            "E_PARAM_MISSING",
            "atomic-extract 禁止直接复制切片或原文，须传入提炼后的 --title 与 --content，一卡一主张，按类型说明依据、推理和适用边界（可用 --action atomic-extract --template 查看完整示例）",
            exit_code=1,
        )

    warnings: List[str] = []
    planned_cards: List[Tuple[str, str, str]] = []
    ledger_groups: List[Dict[str, Any]] = []
    slice_ledger_updates: Dict[Path, str] = {}
    mark_toc_done = False

    resolved_title = title.strip()
    if is_toc and not parsed_slice_refs:
        warnings.append("提示：从 TOC 萃取时建议同时附带 --slice_refs 精确绑定来源切片，以保障证据溯源精确性。")

    card_body = content.strip()
    card_summary = summary.strip() or _first_meaningful_sentence(card_body, resolved_title)

    c_rel, c_stem, c_text = _create_single_atomic_card(
        vault_dir=vault_dir,
        atomics_dir=atomics_dir,
        pid=pid,
        dom=dom,
        seq=seq,
        title=resolved_title,
        summary=card_summary,
        body_text=card_body,
        epistemic_type=ep,
        tags=tag_list,
        aliases=alias_list,
        by_actor=actor,
        groups=groups,
        resolver=resolver,
    )
    planned_cards.append((c_rel, c_stem, c_text))
    ledger_groups.extend(groups)

    tx_changes: Dict[Path, str] = {}
    for c_rel, _c_stem, c_text in planned_cards:
        tx_changes[vault_dir / c_rel] = c_text
    tx_changes.update(slice_ledger_updates)

    ledger_updates = _plan_same_domain_ledger_updates(
        vault_dir=vault_dir,
        resolver=resolver,
        groups=ledger_groups,
        target_domain_root=Path(dom["domain_root"]),
        card_links=[f"[[{c_stem}]]" for _c_rel, c_stem, _c_text in planned_cards],
        mark_toc_extracted=mark_toc_done,
    )
    tx_changes.update(ledger_updates)
    _write_files_tx(vault_dir, tx_changes)

    post_md = _scan_vault_markdowns(vault_dir)
    n_map = note_reg.sync_paths(vault_ref, post_md)
    extracted_items: List[Dict[str, Any]] = []
    for c_rel, _c_stem, _c_text in planned_cards:
        c_ref = n_map[c_rel]
        c_raw = (vault_dir / c_rel).read_bytes()
        c_meta, _, _ = extract_frontmatter(c_raw.decode("utf-8"))
        pub = _public_note(kek=kek, app_id=app_id, vault_ref=vault_ref, note_ref=c_ref, rel=c_rel, meta=c_meta)
        pub["content_sha256"] = hashlib.sha256(c_raw).hexdigest()
        pub["epistemic_type"] = str(c_meta.get("type") or ep)
        pub["review_mode"] = dom["default_mode"]
        pub["reviewed_by"] = str(c_meta.get("reviewed_by") or "")
        extracted_items.append(pub)
        _append_journal_event(vault_dir, note_reg, vault_ref, Path(c_rel).stem, f"原子熔炉萃取卡片「{pub['title']}」({ep})")

    res: Dict[str, Any] = {
        "vault_ref": vault_ref,
        "source_note_ref": note_ref,
        "domain_scope": dom["scope"],
        "project_ref": dom.get("project_ref"),
        "project_id": pid,
        "review_mode": dom["default_mode"],
        "approval_required": dom["default_mode"] == "supervised",
        "auto_approved": bool(auto_approve and dom["default_mode"] == "auto"),
        "extracted_count": len(extracted_items),
        "extracted": extracted_items,
        "provenance": ledger_groups if len(planned_cards) == 1 else [],
        "warnings": warnings,
        "path_sealed": True,
    }
    sealed = [src_rel, *[c_rel for c_rel, _, _ in planned_cards], *[p.relative_to(vault_dir).as_posix() for p in ledger_updates], *post_md]
    if dom.get("project_rel_path"):
        sealed.append(str(dom["project_rel_path"]))
    return res, sealed


def note_approve(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    note_ref: str,
    summary_override: Optional[str] = None,
    proj_reg: Optional[KekProjectRegistry] = None,
    project_ref: Optional[str] = None,
    reviewer: str = "auto",
    expected_sha256: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Approve a pending atomic card -> status=active + reviewed_by=human|ai with version/frozen checks."""
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise VaultStoreError("E_VERSION_REQUIRED", "请先 note-get 获取 content_sha256，批准时传入 expected_sha256", exit_code=1)
    rec = note_reg.resolve_handle(vault_ref, note_ref)
    rel = str(rec["note_rel_path"])
    fpath = _safe_existing_vault_file(vault_dir, rel)

    raw_bytes = fpath.read_bytes()
    if hashlib.sha256(raw_bytes).hexdigest() != expected_sha256:
        raise VaultStoreError("E_VERSION_CONFLICT", "候选版本已变化，旧批准失效", exit_code=1)
    raw = raw_bytes.decode("utf-8")
    meta, _ = _parse_frontmatter_strict(raw, label="待审原子卡")
    _assert_not_journal(rel, meta)

    if str(meta.get("status") or "").strip().lower() in _FROZEN_STATUSES:
        raise VaultStoreError("E_PARAM_CONFLICT", "已废弃或归档笔记不可审核转正", exit_code=1)
    if str(meta.get("doc_type") or "").strip().lower() != "atomic":
        raise VaultStoreError("E_PARAM_INVALID", "atomic-approve 仅适用于原子卡", exit_code=1)

    is_synthetic_mock_note = (
        proj_reg is None
        and "/" not in rel
        and not (vault_dir / "20-AI-Zone").exists()
        and not (vault_dir / "30-Projects").exists()
    )
    if not is_synthetic_mock_note:
        zone, _ = infer_zone_and_project_rel(rel)
        if zone != "atomics" or is_exempt_rel_path(rel):
            raise VaultStoreError("E_PARAM_INVALID", "atomic-approve 仅适用于 22-Atomics 分区内的正式原子卡", exit_code=1)
        own_pid, own_root, _ = _domain_state_for_rel(vault_dir, rel)
        _require_unfrozen_domain(vault_dir, {"project_id": own_pid, "domain_root": own_root})
    else:
        own_pid, own_root = "global", vault_dir / "20-AI-Zone"

    dom = (
        resolve_domain_scope(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            proj_reg=proj_reg,
            explicit_project_ref=project_ref,
            scope="project" if project_ref else "auto",
            source_rel_path=rel,
            source_meta=meta,
        )
        if proj_reg
        else {"default_mode": "supervised", "project_id": own_pid}
    )
    if project_ref and str(dom.get("project_id")) != own_pid:
        raise VaultStoreError("E_PARAM_CONFLICT", "原子卡不属于指定 --project_ref 课题域", exit_code=1)

    effective = "supervised" if meta.get("review_mode") == "supervised" else dom["default_mode"]
    if reviewer not in ("auto", "ai", "human"):
        raise VaultStoreError("E_PARAM_INVALID", "reviewer 仅支持 auto/ai/human", exit_code=1)
    actor = ("ai" if effective == "auto" else "human") if reviewer == "auto" else reviewer
    if actor == "ai" and effective != "auto":
        raise VaultStoreError("E_PRE_REVIEW_REQUIRED", "supervised 候选必须取得人类确认，不能 AI 自批", exit_code=1)

    rendered = replace_frontmatter_field(raw, "status", "active")
    rendered = replace_frontmatter_field(rendered, "reviewed_by", actor)
    rendered = replace_frontmatter_field(rendered, "updated", _today())
    if summary_override and summary_override.strip():
        rendered = replace_frontmatter_field(rendered, "summary", f'"{fm_scalar(summary_override.strip())}"')

    fmeta, _, _ = extract_frontmatter(rendered)
    errs = validate_note_meta(fmeta)
    if errs:
        raise VaultStoreError("E_PARAM_INVALID", f"审核转正校验失败: {'; '.join(errs)}", exit_code=1)

    _atomic_write_md(fpath, rendered, expected_sha256=expected_sha256)
    journal_status = "recorded"
    try:
        _append_journal_event(vault_dir, note_reg, vault_ref, fpath.stem, f"审核转正（{note_ref}，reviewed_by={actor}）")
    except Exception:
        # The card is already committed. Never present this as a retryable approval failure.
        journal_status = "unconfirmed"

    pub = _public_note(kek=kek, app_id=app_id, vault_ref=vault_ref, note_ref=note_ref, rel=rel, meta=fmeta)
    pub["approval_committed"] = True
    pub["journal_status"] = journal_status
    pub["retry_approval"] = False
    pub["approved_input_sha256"] = expected_sha256
    pub["content_sha256"] = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    pub["reviewed_by"] = actor
    pub["review_mode"] = effective
    return pub, [rel]
