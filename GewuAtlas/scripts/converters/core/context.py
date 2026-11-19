#!/usr/bin/env python3
"""Domain context inference helper for GewuAtlas multimodal converters."""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def resolve_domain_id(root: Path, file_path=None, explicit_pid: Optional[str] = None) -> str:
    if explicit_pid:
        low = explicit_pid.strip().lower()
        if low in ("ai-zone", "aizone", "20-ai-zone"):
            return "AI-Zone"
        return explicit_pid.strip()
    if file_path and not str(file_path).startswith(("http://", "https://")):
        p = Path(file_path)
        if not p.is_absolute():
            p = root / p
        try:
            rel = p.resolve().relative_to(root.resolve())
        except ValueError:
            return "AI-Zone"
        parts = rel.parts
        if len(parts) >= 2 and parts[0] == "30-Projects":
            return parts[1]
        if len(parts) >= 2 and parts[0] == "00-DropZone":
            cand = root / "30-Projects" / parts[1]
            if cand.is_dir():
                return parts[1]
    return "AI-Zone"
