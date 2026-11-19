#!/usr/bin/env python3
"""Session-isolated breaker, balanced capsule compaction, read-ledger, and deterministic truth guards for GewuAtlas."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

_MAX_SESSIONS = 64
_MAX_READ_REFS = 500
_WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+)(?:#[^\]\|]*)?(?:\|[^\]]*)?\]\]")
_GATE_TAG_RE = re.compile(r"\[(?:QA_GATE|DELIVERY_GATE|DEBUG_TRACE)[^\]]*\]")


def session_fingerprint(raw_session: str) -> str:
    key = (raw_session or "stateless").strip() or "stateless"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def load_session_breaker(path: Path, session_fp: str) -> Dict[str, Any]:
    """Load per-session breaker slot with backward compatibility for legacy flat v1 files."""
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        doc = json.loads(path.read_text("utf-8"))
    except Exception:
        return {}
    if not isinstance(doc, dict):
        return {}
    if doc.get("version") == 2 and isinstance(doc.get("sessions"), dict):
        slot = doc["sessions"].get(session_fp)
        return dict(slot) if isinstance(slot, dict) else {}
    # Legacy v1 flat format fallback (only when session_fp matches stateless)
    if "fp" in doc and session_fp == session_fingerprint("stateless"):
        return dict(doc)
    return {}


def save_session_breaker(path: Path, session_fp: str, slot_data: Dict[str, Any]) -> None:
    """Atomically save per-session breaker slot with LRU cap to prevent cross-agent collision."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    sessions: Dict[str, Any] = {}
    if path.is_file() and not path.is_symlink():
        try:
            existing = json.loads(path.read_text("utf-8"))
            if isinstance(existing, dict) and existing.get("version") == 2 and isinstance(existing.get("sessions"), dict):
                sessions = dict(existing["sessions"])
        except Exception:
            sessions = {}
    sessions.pop(session_fp, None)
    sessions[session_fp] = slot_data
    while len(sessions) > _MAX_SESSIONS:
        oldest_key = next(iter(sessions))
        sessions.pop(oldest_key, None)
    payload = {"version": 2, "sessions": sessions}
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, sort_keys=True)
            fh.flush()
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def maybe_compact_capsule(env: Dict[str, Any], *, consecutive_same_ok: int) -> None:
    """Compact repetitive reminder text on consecutive identical successful calls while preserving anti-lost navigation.

    - Never compacts GUARD / BLOCKED / non-terminal / first-step capsules.
    - Never alters structuredContent.capsule fields so machine parsers remain 100% compatible.
    """
    if consecutive_same_ok < 2 or not env.get("ok"):
        return
    sc = env.get("structuredContent")
    if not isinstance(sc, dict):
        return
    cap = sc.get("capsule")
    if not isinstance(cap, dict):
        return
    if cap.get("mode") != "TRIAD" or not cap.get("terminal"):
        return
    anchor = str(cap.get("anchor") or "")
    if "BLOCKED" in anchor or "🚫" in anchor:
        return
    state = str(cap.get("state") or "").strip()
    orbits = [str(o).strip() for o in (cap.get("orbit_options") or []) if str(o).strip()]
    primary_next = orbits[0] if orbits else "继续下一步或向人类汇报"
    tags = _GATE_TAG_RE.findall(state + " " + anchor)
    # Deduplicate tags preserving order
    uniq_tags: List[str] = []
    for t in tags:
        if t not in uniq_tags:
            uniq_tags.append(t)
    tag_suffix = (" " + " ".join(uniq_tags)) if uniq_tags else ""
    compact = f"🧭 [COMPACT] {state} | 🚀 下步: {primary_next}{tag_suffix}"
    env["reminder"] = compact


def _read_ledger_path(state_dir: Path, session_fp: str) -> Path:
    d = state_dir / "session-reads"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d / f"{session_fp}.json"


def record_session_reads(state_dir: Path, session_fp: str, vault_ref: str, note_refs: Iterable[str]) -> None:
    """Record opaque note_refs actually read in this session (zero physical paths)."""
    clean = [str(r).strip() for r in note_refs if str(r).strip()]
    if not clean:
        return
    path = _read_ledger_path(state_dir, session_fp)
    doc: Dict[str, List[str]] = {}
    if path.is_file() and not path.is_symlink():
        try:
            loaded = json.loads(path.read_text("utf-8"))
            if isinstance(loaded, dict):
                doc = {str(k): list(v) for k, v in loaded.items() if isinstance(v, list)}
        except Exception:
            doc = {}
    cur = doc.get(vault_ref, [])
    cur_set = set(cur)
    for r in clean:
        if r not in cur_set:
            cur.append(r)
            cur_set.add(r)
    if len(cur) > _MAX_READ_REFS:
        cur = cur[-_MAX_READ_REFS:]
    doc[vault_ref] = cur
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False)
            fh.flush()
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def load_session_reads(state_dir: Path, session_fp: str, vault_ref: str) -> Set[str]:
    path = state_dir / "session-reads" / f"{session_fp}.json"
    if not path.is_file() or path.is_symlink():
        return set()
    try:
        loaded = json.loads(path.read_text("utf-8"))
        if isinstance(loaded, dict) and isinstance(loaded.get(vault_ref), list):
            return {str(x).strip() for x in loaded[vault_ref] if str(x).strip()}
    except Exception:
        pass
    return set()


def _char_ngrams(text: str, n: int = 5) -> Set[str]:
    norm = re.sub(r"\s+", "", re.sub(r"[#*`>\-\[\]\(\)|]", "", text or "")).lower()
    if len(norm) < n:
        return {norm} if norm else set()
    return {norm[i : i + n] for i in range(len(norm) - n + 1)}


def _strip_provenance_footer(body: str) -> str:
    return re.split(r"\n##\s*(?:溯源|参考来源|来源与关联|Provenance)", body or "", maxsplit=1)[0]


def text_overlap_metrics(a: str, b: str, n: int = 5) -> Tuple[float, float]:
    sa = _char_ngrams(a, n=n)
    sb = _char_ngrams(b, n=n)
    if not sa or not sb:
        return 0.0, 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    min_sz = min(len(sa), len(sb))
    return (inter / union) if union else 0.0, (inter / min_sz) if min_sz else 0.0


def text_jaccard_similarity(a: str, b: str, n: int = 5) -> float:
    return text_overlap_metrics(a, b, n=n)[0]


def _extract_provenance_stems(fm: Dict[str, Any]) -> List[str]:
    stems: List[str] = []
    for key in ("slices", "sources", "provenance"):
        val = fm.get(key)
        if not val:
            continue
        raw_s = json.dumps(val, ensure_ascii=False) if not isinstance(val, str) else val
        for m in _WIKILINK_RE.finditer(raw_s):
            t = Path(m.group(1).strip()).stem
            if t and t not in stems:
                stems.append(t)
    return stems


def audit_note_truth_guards(
    *,
    vdir: Path,
    note_ref: str,
    fm: Dict[str, Any],
    body: str,
    handle_map: Dict[str, str],
    session_reads: Set[str],
) -> Tuple[List[str], List[str]]:
    """Return (copy_paste_issues, unread_cited_refs) for a verified deliverable note."""
    copy_issues: List[str] = []
    unread_cited: List[str] = []
    doc_type = str(fm.get("doc_type") or "").strip().lower()

    stem_to_rel_ref: Dict[str, Tuple[str, str]] = {}
    for rel, nref in handle_map.items():
        stem_to_rel_ref[Path(rel).stem] = (rel, nref)

    # 1. Anti-copy-paste guard for distilled atomic cards against their source slices
    if doc_type == "atomic":
        core_body = _strip_provenance_footer(body)
        clean_body = re.sub(r"\s+", "", core_body)
        if len(clean_body) >= 120:
            prov_stems = _extract_provenance_stems(fm)
            for pstem in prov_stems:
                hit = stem_to_rel_ref.get(pstem)
                if not hit:
                    continue
                srel, sref = hit
                if "/24-Slices/" not in srel:
                    continue
                spath = vdir / srel
                if not spath.is_file() or spath.is_symlink():
                    continue
                try:
                    from notes.atlas_yamlmini import extract_frontmatter

                    _, sbody, _ = extract_frontmatter(spath.read_text(encoding="utf-8", errors="replace"))
                except Exception:
                    continue
                jacc, contain = text_overlap_metrics(core_body, _strip_provenance_footer(sbody), n=5)
                if jacc >= 0.70 or (contain >= 0.85 and jacc >= 0.50):
                    copy_issues.append(
                        f"原子卡 {note_ref} 正文与源切片 {sref} 重合度过高（Jaccard={jacc:.0%}, 包含率={contain:.0%}），疑似直接照抄原文，请蒸馏提炼"
                    )
                    break

    # 2. Read-before-write audit for cited vault notes (only when session has recorded reads)
    if doc_type in ("artifact", "atomic") and session_reads:
        for m in _WIKILINK_RE.finditer(body or ""):
            tstem = Path(m.group(1).strip()).stem
            hit = stem_to_rel_ref.get(tstem)
            if not hit:
                continue
            _, tref = hit
            if tref != note_ref and tref not in session_reads and tref not in unread_cited:
                unread_cited.append(tref)

    return copy_issues, unread_cited
