#!/usr/bin/env python3
"""Project/domain helpers for GewuAtlas multimodal converters."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Tuple

from core.common import read_frontmatter

PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9._\-\u4e00-\u9fff]*$")
RESERVED = {"all", "journal", "ai-zone", "projects"}


def resolve_project_dir(root: Path, pid: str) -> Optional[Path]:
    """Resolve an existing project directory under 30-Projects/ by folder name or Manifest project_id."""
    clean = str(pid or "").strip()
    if not clean:
        return None
    p_root = root / "30-Projects"
    if not p_root.is_dir():
        return None
    direct = p_root / clean
    if direct.is_dir() and not direct.is_symlink():
        return direct
    target_low = clean.lower()
    for child in sorted(p_root.iterdir()):
        if child.is_symlink() or not child.is_dir() or child.name.startswith("."):
            continue
        if child.name.lower() == target_low:
            return child
        for cand in sorted(child.glob("*.md")):
            if cand.is_file() and not cand.is_symlink() and cand.name.lower().startswith("manifest"):
                try:
                    meta, _, _ = read_frontmatter(cand)
                    if str(meta.get("project_id") or "").strip().lower() == target_low:
                        return child
                except Exception:
                    pass
                break
    return None


def get_project_id_for_dir(proj_dir: Path) -> str:
    """Return the canonical project_id from Manifest-*.md if present, else folder name."""
    if proj_dir.is_dir():
        for cand in sorted(proj_dir.glob("*.md")):
            if cand.is_file() and not cand.is_symlink() and cand.name.lower().startswith("manifest"):
                try:
                    meta, _, _ = read_frontmatter(cand)
                    mpid = str(meta.get("project_id") or "").strip()
                    if mpid:
                        return mpid
                except Exception:
                    pass
                break
    return proj_dir.name


def manifest_path(root: Path, pid: str) -> Path:
    if not pid or pid == "AI-Zone":
        return root / "20-AI-Zone" / "Manifest-AI-Zone.md"
    proj_dir = resolve_project_dir(root, pid) or (root / "30-Projects" / pid)
    if proj_dir.is_dir():
        for cand in sorted(proj_dir.glob("*.md")):
            if cand.is_file() and not cand.is_symlink() and cand.name.lower().startswith("manifest"):
                return cand
    return proj_dir / f"Manifest-{pid}.md"


def validate_project_id(pid: str) -> Tuple[bool, str]:
    if not pid:
        return False, "project_id 不能为空"
    if not PROJECT_ID_RE.match(pid):
        return False, f"非法 project_id 字符集: {pid}"
    if pid != "AI-Zone" and pid.lower() in RESERVED:
        return False, f"保留字不可作为 project_id: {pid}"
    return True, ""


def get_domain_mode(root: Path, pid: Optional[str] = None) -> str:
    env_mode = os.environ.get("GEWU_ATLAS_DOMAIN_MODE", "").strip()
    p = pid if pid and pid != "AI-Zone" else "AI-Zone"
    mp = manifest_path(root, p)
    if mp.is_file():
        meta, _, _ = read_frontmatter(mp)
        mode = meta.get("default_mode")
        if mode in ("auto", "supervised"):
            return str(mode)
    if env_mode in ("auto", "supervised"):
        return env_mode
    return "auto"


def is_domain_frozen(root: Path, pid: Optional[str] = None) -> bool:
    p = pid if pid and pid != "AI-Zone" else "AI-Zone"
    mp = manifest_path(root, p)
    if not mp.is_file():
        return False
    meta, _, _ = read_frontmatter(mp)
    return meta.get("status") == "deprecated"


def get_status_for_domain(root: Path, pid: Optional[str] = None, out_path: Optional[Path] = None) -> str:
    target_pid = pid
    if not target_pid and out_path:
        out_p = Path(out_path).resolve()
        r = Path(root).resolve()
        try:
            rel = out_p.relative_to(r)
            if len(rel.parts) > 1 and rel.parts[0] == "30-Projects":
                target_pid = rel.parts[1]
            elif len(rel.parts) > 0 and rel.parts[0] == "20-AI-Zone":
                target_pid = "AI-Zone"
        except ValueError:
            pass
    mode = get_domain_mode(root, target_pid)
    return "active" if mode == "auto" else "pending"
