#!/usr/bin/env python3
"""Domain-aware scope & path resolver mirroring V2 fractal topology (AI-Zone vs 30-Projects/<dir>)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from notes.atlas_yamlmini import FrontmatterError, extract_frontmatter, get_vault_layout_spec
from storage.project_kek_store import KekProjectRegistry
from storage.vault_kek_store import VaultStoreError

# Relative compartment paths shared identically by 20-AI-Zone and every 30-Projects/<dir>
DOMAIN_COMPARTMENTS: Dict[str, str] = {
    "artifacts": "10-Artifacts",
    "library": "20-Memory/21-Library",
    "atomics": "20-Memory/22-Atomics",
    "moc": "20-Memory/23-MOC",
    "slices": "20-Memory/24-Slices",
    "attic": "20-Memory/97-Attic",
    "slip": "25-Slip",
    "workbench": "30-Workbench",
}


def get_project_fractal_subdirs() -> Tuple[str, ...]:
    try:
        return tuple(get_vault_layout_spec()["project_fractal_dirs"])
    except FrontmatterError as exc:
        raise VaultStoreError("E_DICT_INVALID", str(exc), exit_code=1) from exc


PROJECT_FRACTAL_SUBDIRS = get_project_fractal_subdirs()


def scan_project_rels(vault_dir: Path) -> List[str]:
    p_root = vault_dir / "30-Projects"
    if not p_root.is_dir():
        return []
    out: List[str] = []
    for child in sorted(p_root.iterdir()):
        if child.is_symlink() or not child.is_dir() or child.name.startswith("."):
            continue
        out.append(f"30-Projects/{child.name}")
    return out


def read_domain_manifest(domain_root: Path, fallback_pid: str) -> Dict[str, Any]:
    for cand in sorted(domain_root.glob("*.md")):
        if cand.is_symlink() or not cand.is_file() or cand.name.startswith("."):
            continue
        if cand.name.lower().startswith("manifest"):
            try:
                meta, _, _ = extract_frontmatter(cand.read_text(encoding="utf-8"))
                return {
                    "project_id": str(meta.get("project_id") or fallback_pid),
                    "default_mode": str(meta.get("default_mode") or "auto"),
                    "status": str(meta.get("status") or "active"),
                    "title": str(meta.get("title") or fallback_pid),
                    "private": bool(meta.get("private") is True),
                }
            except FrontmatterError:
                pass
    return {"project_id": fallback_pid, "default_mode": "auto", "status": "active", "title": fallback_pid, "private": False}


def infer_zone_and_project_rel(rel_path: str) -> tuple[str, Optional[str]]:
    """Return (logical_zone, project_rel_path_or_None) for any Vault relative Markdown path."""
    norm = rel_path.replace("\\", "/")
    parts = norm.split("/")
    if len(parts) >= 2 and parts[0] == "30-Projects":
        prel = f"30-Projects/{parts[1]}"
        inner = "/".join(parts[2:])
        for z, sub in sorted(DOMAIN_COMPARTMENTS.items(), key=lambda kv: -len(kv[1])):
            if inner == sub or inner.startswith(sub + "/"):
                return z, prel
        return "projects", prel
    if norm == "00-DropZone" or norm.startswith("00-DropZone/"):
        return "dropzone", None
    if norm == "05-Journal" or norm.startswith("05-Journal/"):
        return "journal", None
    if norm.startswith("20-AI-Zone/"):
        inner = norm[len("20-AI-Zone/"):]
        for z, sub in sorted(DOMAIN_COMPARTMENTS.items(), key=lambda kv: -len(kv[1])):
            if inner == sub or inner.startswith(sub + "/"):
                return z, None
    return "other", None


def resolve_domain_scope(
    *,
    vault_dir: Path,
    vault_ref: str,
    proj_reg: KekProjectRegistry,
    explicit_project_ref: Optional[str] = None,
    scope: str = "auto",
    source_rel_path: Optional[str] = None,
    source_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve target domain root (Project dir vs AI-Zone) following V2 3-tier awareness."""
    norm_scope = (scope or "auto").strip().lower()
    if norm_scope not in ("auto", "project", "ai-zone"):
        raise VaultStoreError("E_PARAM_INVALID", "--scope 仅支持 auto / project / ai-zone", exit_code=1)

    existing_rels = scan_project_rels(vault_dir)
    rel_to_ref = proj_reg.sync_projects(vault_ref, existing_rels)

    def _pack_project(prel: str) -> Dict[str, Any]:
        pdir = vault_dir / prel
        if not pdir.is_dir():
            raise VaultStoreError("E_NOT_FOUND", f"课题目录不存在: {rel_to_ref.get(prel, '')}", exit_code=1)
        m = read_domain_manifest(pdir, pdir.name)
        return {
            "scope": "project",
            "project_ref": rel_to_ref[prel],
            "project_rel_path": prel,
            "project_id": m["project_id"],
            "default_mode": m["default_mode"] if m["default_mode"] in ("auto", "supervised") else "auto",
            "domain_root": pdir,
        }

    # 1. Explicit --project_ref wins unless caller literally forced --scope ai-zone
    if explicit_project_ref and norm_scope != "ai-zone":
        rec = proj_reg.resolve_handle(vault_ref, explicit_project_ref.strip())
        return _pack_project(str(rec["project_rel_path"]))

    if norm_scope == "ai-zone":
        m = read_domain_manifest(vault_dir / "20-AI-Zone", "ai-zone")
        return {
            "scope": "ai-zone",
            "project_ref": None,
            "project_rel_path": None,
            "project_id": "ai-zone",
            "default_mode": m["default_mode"] if m["default_mode"] in ("auto", "supervised") else "auto",
            "domain_root": vault_dir / "20-AI-Zone",
        }

    # 2. Source path self-awareness: 30-Projects/<dir>/... stays inside its own project!
    if source_rel_path:
        _, src_prel = infer_zone_and_project_rel(source_rel_path)
        if src_prel and src_prel in rel_to_ref:
            return _pack_project(src_prel)

    # 3. Source frontmatter project_id lookup
    src_pid = str((source_meta or {}).get("project_id") or "").strip()
    if src_pid and src_pid.lower() not in ("ai-zone", "aizone"):
        for prel in existing_rels:
            m = read_domain_manifest(vault_dir / prel, Path(prel).name)
            if m["project_id"] == src_pid and m.get("status") != "archived":
                return _pack_project(prel)

    if norm_scope == "project":
        raise VaultStoreError("E_PARAM_MISSING", "--scope project 需要传入 --project_ref <prj_01> 或选择课题内笔记", exit_code=1)

    # 4. Fallback to AI-Zone public hub
    m = read_domain_manifest(vault_dir / "20-AI-Zone", "ai-zone")
    return {
        "scope": "ai-zone",
        "project_ref": None,
        "project_rel_path": None,
        "project_id": "ai-zone",
        "default_mode": m["default_mode"] if m["default_mode"] in ("auto", "supervised") else "auto",
        "domain_root": vault_dir / "20-AI-Zone",
    }


def resolve_zone_dir(vault_dir: Path, domain: Dict[str, Any], zone: str) -> Path:
    z = (zone or "").strip().lower()
    if z == "dropzone":
        return vault_dir / "00-DropZone"
    if z == "journal":
        return vault_dir / "05-Journal"
    if z in DOMAIN_COMPARTMENTS:
        return Path(domain["domain_root"]) / DOMAIN_COMPARTMENTS[z]
    if z == "projects" and domain["scope"] == "project":
        return Path(domain["domain_root"])
    if z == "projects":
        return vault_dir / "30-Projects"
    raise VaultStoreError("E_PARAM_INVALID", f"未知分区: {zone}", exit_code=1)


def resolve_archive_dir(vault_dir: Path, domain: Dict[str, Any]) -> Path:
    """Resolve cold-archive base dir; projects share Vault-level 99-Assets/98-Archives."""
    if domain.get("scope") == "project":
        return vault_dir / "99-Assets" / "98-Archives"
    return Path(domain["domain_root"]) / "99-Assets" / "98-Archives"
