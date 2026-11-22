#!/usr/bin/env python3
"""Zero-path KEK-handle knowledge engineering engine for GewuAtlas (CRUD, promote, deprecate, BM25, health-check)."""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from facade.atlas_domain_scope import (
    infer_zone_and_project_rel,
    read_domain_manifest,
    resolve_domain_scope,
    resolve_zone_dir,
)
from notes.atlas_yamlmini import (
    FrontmatterError,
    drop_frontmatter_field,
    extract_frontmatter,
    extract_wikilinks,
    resolve_wikilink,
    fm_scalar,
    get_vault_layout_spec,
    inspect_note_against_dict,
    is_exempt_rel_path,
    load_layout_dict,
    load_metadata_dict,
    load_methodology_dict,
    prune_frontmatter_for_doc_type,
    replace_frontmatter_field,
    validate_dicts_integrity,
    validate_note_meta,
)
from storage.note_kek_store import KekNoteRegistry, validate_note_rel_path
from storage.project_kek_store import KekProjectRegistry
from storage.vault_kek_store import VaultStoreError

ZONES: Dict[str, str] = {
    "dropzone": "00-DropZone",
    "journal": "05-Journal",
    "artifacts": "20-AI-Zone/10-Artifacts",
    "library": "20-AI-Zone/20-Memory/21-Library",
    "atomics": "20-AI-Zone/20-Memory/22-Atomics",
    "moc": "20-AI-Zone/20-Memory/23-MOC",
    "slices": "20-AI-Zone/20-Memory/24-Slices",
    "attic": "20-AI-Zone/20-Memory/97-Attic",
    "slip": "20-AI-Zone/25-Slip",
    "workbench": "20-AI-Zone/30-Workbench",
    "projects": "30-Projects",
}

_RESERVED_SUBDIR_NAMES = frozenset({
    "00-dropzone",
    "05-journal",
    "10-artifacts",
    "10-attachments",
    "20-ai-zone",
    "20-memory",
    "21-library",
    "22-atomics",
    "23-moc",
    "24-slices",
    "25-slip",
    "30-projects",
    "30-workbench",
    "90-system",
    "97-attic",
    "98-archives",
    "99-assets",
})

DEFAULT_DOC_TYPE_BY_ZONE: Dict[str, str] = {
    "dropzone": "memo",
    "journal": "journal",
    "artifacts": "artifact",
    "library": "library",
    "atomics": "atomic",
    "moc": "moc",
    "slices": "slice",
    "attic": "atomic",
    "slip": "slip",
    "workbench": "workbench",
    "projects": "atomic",
}

BM25_WEIGHTS = {"title": 8.0, "aliases": 6.0, "summary": 3.0, "tags": 2.0, "body": 1.0}
BM25_K1 = 1.2
BM25_B = 0.75
_JOURNAL_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_JOURNAL_HEADING_HUMAN = "# 人类记录"
_JOURNAL_HEADING_AI = "# AI日志"


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _slugify_filename(title: str) -> str:
    from notes.atlas_wikilink_tools import slugify_note_stem

    return slugify_note_stem(title)


def _zone_for_rel(note_rel_path: str) -> str:
    z, _ = infer_zone_and_project_rel(note_rel_path)
    return z


def _lock_fd_nb(fd: int) -> None:
    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes as _wintypes
        import msvcrt

        class _OVERLAPPED(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_size_t),
                ("InternalHigh", ctypes.c_size_t),
                ("Offset", _wintypes.DWORD),
                ("OffsetHigh", _wintypes.DWORD),
                ("hEvent", _wintypes.HANDLE),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.LockFileEx.argtypes = [_wintypes.HANDLE, _wintypes.DWORD, _wintypes.DWORD, _wintypes.DWORD, _wintypes.DWORD, ctypes.POINTER(_OVERLAPPED)]
        k32.LockFileEx.restype = _wintypes.BOOL
        ov = _OVERLAPPED()
        if not k32.LockFileEx(_wintypes.HANDLE(msvcrt.get_osfhandle(fd)), 3, 0, 1, 0, ctypes.byref(ov)):
            err = ctypes.get_last_error()
            if err == 33:
                raise BlockingIOError(err, "lock busy")
            raise OSError(err, ctypes.FormatError(err))
        return
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes as _wintypes
        import msvcrt

        class _OVERLAPPED(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_size_t),
                ("InternalHigh", ctypes.c_size_t),
                ("Offset", _wintypes.DWORD),
                ("OffsetHigh", _wintypes.DWORD),
                ("hEvent", _wintypes.HANDLE),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.UnlockFileEx.argtypes = [_wintypes.HANDLE, _wintypes.DWORD, _wintypes.DWORD, _wintypes.DWORD, ctypes.POINTER(_OVERLAPPED)]
        k32.UnlockFileEx.restype = _wintypes.BOOL
        ov = _OVERLAPPED()
        k32.UnlockFileEx(_wintypes.HANDLE(msvcrt.get_osfhandle(fd)), 0, 1, 0, ctypes.byref(ov))
        return
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_UN)


def _atomic_write_bytes(target: Path, data: bytes, *, expected_sha256: Optional[str] = None) -> None:
    """Serialize cooperating writers; compare approved bytes under the write lock.

    External editors that ignore this lock are outside this cooperative guarantee.
    """
    import stat
    import tempfile
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.parent / ".atlas-write.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    tmp = None
    locked = False
    try:
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise VaultStoreError("E_WRITE_LOCK", "写入锁不可用", exit_code=1)
        _lock_fd_nb(lock_fd)
        locked = True
        if expected_sha256 is not None:
            if target.is_symlink() or not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected_sha256:
                raise VaultStoreError("E_VERSION_CONFLICT", "候选已变化，请重新读取并审核当前版本", exit_code=1)
        fd, name = tempfile.mkstemp(prefix=".atlas-", suffix=".tmp", dir=target.parent)
        tmp = Path(name)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BlockingIOError:
        raise VaultStoreError("E_WRITE_BUSY", "另一个写入正在进行；请先核对其回执", exit_code=1)
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink()
        if locked:
            try:
                _unlock_fd(lock_fd)
            except OSError:
                pass
        os.close(lock_fd)


def _atomic_write_md(target: Path, content: str, *, expected_sha256: Optional[str] = None) -> None:
    _atomic_write_bytes(target, content.encode("utf-8"), expected_sha256=expected_sha256)


def _scan_vault_markdowns(vault_dir: Path) -> List[str]:
    skip_tops = set(get_vault_layout_spec()["skip_note_scan_tops"])
    out: List[str] = []
    if not vault_dir.is_dir():
        return out
    vault_res = vault_dir.resolve()
    for cur_root, dirs, files in os.walk(vault_res, followlinks=False):
        cur = Path(cur_root)
        is_root = cur == vault_res
        dirs[:] = [
            d
            for d in sorted(dirs)
            if not d.startswith(".")
            and d not in ("__pycache__", "node_modules")
            and not (is_root and d in skip_tops)
            and not (cur / d).is_symlink()
        ]
        for fname in sorted(files):
            if not fname.endswith(".md"):
                continue
            p = cur / fname
            if p.is_symlink() or not p.is_file():
                continue
            rel = p.relative_to(vault_res).as_posix()
            if rel == "20-AI-Zone/Manifest-AI-Zone.md":
                continue
            # Misnamed PDF originals belong to the raw inbox, not the Markdown registry.
            try:
                with p.open("rb") as stream:
                    if stream.read(1024).lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"%PDF-"):
                        continue
            except OSError:
                continue
            out.append(rel)
    return sorted(out)


def _note_seal(kek: bytes, app_id: str, vault_ref: str, note_ref: str, note_rel_path: str) -> str:
    msg = f"{app_id}|{vault_ref}|{note_ref}|{note_rel_path}".encode("utf-8")
    return "hmac-sha256:" + hmac.new(kek, msg, hashlib.sha256).hexdigest()[:16]


def _render_note_md(
    *,
    doc_type: str,
    title: str,
    summary: str,
    status: str,
    tags: List[str],
    aliases: List[str],
    updated: str,
    body: str,
) -> str:
    tags_yaml = ", ".join(f'"{fm_scalar(t)}"' for t in tags)
    aliases_yaml = ", ".join(f'"{fm_scalar(a)}"' for a in aliases)
    lines = [
        "---",
        f"doc_type: {doc_type}",
        f'title: "{fm_scalar(title)}"',
    ]
    if doc_type != "journal":
        lines.append(f'summary: "{fm_scalar(summary or title)}"')
        lines.append(f"status: {status}")
    lines.extend(
        [
            f"tags: [{tags_yaml}]",
            f"aliases: [{aliases_yaml}]",
            f"updated: {updated}",
            "---",
            "",
            body.strip() if body.strip() else f"# {title}",
            "",
        ]
    )
    return "\n".join(lines)


def _locate_journal_note(vault_dir: Path, date_str: str) -> Tuple[Optional[Path], Optional[str], bool]:
    top_rel = f"05-Journal/{date_str}.md"
    top_path = vault_dir / top_rel
    if top_path.is_file() and not top_path.is_symlink():
        return top_path, top_rel, False
    m = _JOURNAL_DATE_RE.fullmatch(date_str)
    if m:
        yyyy, mm, _ = m.groups()
        arc_rel = f"05-Journal/{yyyy}/{mm}/{date_str}.md"
        arc_path = vault_dir / arc_rel
        if arc_path.is_file() and not arc_path.is_symlink():
            return arc_path, arc_rel, True
    return None, None, False


def _render_journal_note(date_str: str, ai_line: str = "") -> str:
    body = f"{_JOURNAL_HEADING_HUMAN}\n\n\n{_JOURNAL_HEADING_AI}\n"
    if ai_line.strip():
        body += f"\n{ai_line.strip()}\n"
    rendered = _render_note_md(
        doc_type="journal",
        title=f"{date_str} 日志",
        summary="",
        status="",
        tags=["Journal"],
        aliases=[],
        updated=date_str,
        body=body,
    )
    rendered = replace_frontmatter_field(rendered, "date", f'"{date_str}"')
    rendered = replace_frontmatter_field(rendered, "private", "true")
    return rendered


def _insert_ai_log_line(raw_md: str, line: str, updated_date: str) -> str:
    _meta, body, has_fm = extract_frontmatter(raw_md)
    lines = body.splitlines()
    in_fence = False
    ai_idx: Optional[int] = None
    next_h1_idx: Optional[int] = None
    for idx, ln in enumerate(lines):
        stripped = ln.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if stripped == _JOURNAL_HEADING_AI and ai_idx is None:
            ai_idx = idx
            continue
        if ai_idx is not None and stripped.startswith("# ") and not stripped.startswith("##"):
            next_h1_idx = idx
            break

    if ai_idx is not None:
        insert_at = next_h1_idx if next_h1_idx is not None else len(lines)
        while insert_at > ai_idx + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        new_lines = lines[:insert_at] + [line] + lines[insert_at:]
        new_body = "\n".join(new_lines).rstrip() + "\n"
    else:
        base_body = body.rstrip()
        sep = "\n\n" if base_body else ""
        new_body = f"{base_body}{sep}{_JOURNAL_HEADING_AI}\n\n{line}\n"

    if has_fm:
        head_match = re.match(r"^---\n.*?\n---\n*", raw_md, flags=re.S)
        fm_header = head_match.group(0) if head_match else ""
        rebuilt = fm_header.rstrip() + "\n\n" + new_body
        rebuilt = replace_frontmatter_field(rebuilt, "updated", updated_date)
        return rebuilt
    return new_body


def _append_journal_event(
    vault_dir: Path,
    note_reg: KekNoteRegistry,
    vault_ref: str,
    title_stem: str,
    event_msg: str,
    *,
    sync_registry: bool = True,
) -> str:
    d = _today()
    hhmm = datetime.now().astimezone().strftime("%H:%M")
    entry = f"- {hhmm} {event_msg}（[[{title_stem}]]）" if title_stem else f"- {hhmm} {event_msg}"
    existing_path, existing_rel, _ = _locate_journal_note(vault_dir, d)
    if existing_path is None:
        j_rel = f"05-Journal/{d}.md"
        jpath = vault_dir / j_rel
        content = _render_journal_note(d, ai_line=entry)
    else:
        jpath = existing_path
        j_rel = str(existing_rel or jpath.relative_to(vault_dir).as_posix())
        old = jpath.read_text(encoding="utf-8")
        content = _insert_ai_log_line(old, entry, d)
    _atomic_write_md(jpath, content)
    if sync_registry:
        note_reg.sync_paths(vault_ref, _scan_vault_markdowns(vault_dir))
    return j_rel


_RESOLVED_WL_PATH_RE = re.compile(r"\[\[([^\[\]|#\r\n]+/)([^\[\]|#\r\n]+)([#|][^\]\r\n]*)?\]\]")


def _sanitize_resolved_links(rewrites: List[Dict[str, Any]], limit: int = 20) -> List[Dict[str, Any]]:
    """Redact vault-internal directory prefixes in metadata resolved_links while preserving target_ref and stem."""
    out: List[Dict[str, Any]] = []
    for item in rewrites[:limit]:
        if not isinstance(item, dict):
            continue
        clean = dict(item)
        for fld in ("from", "to"):
            val = clean.get(fld)
            if isinstance(val, str) and "/" in val and "[[" in val:
                clean[fld] = _RESOLVED_WL_PATH_RE.sub(lambda m: f"[[{m.group(2)}{m.group(3) or ''}]]", val)
        out.append(clean)
    return out


def _parse_list_arg(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [x.strip() for x in str(raw).split(",") if x.strip()]


def _preflight_note_moves(vault_dir: Path, moves: Dict[str, str]) -> None:
    """Refuse protected references before any content, location or handle mutation."""
    from pipeline.atlas_vfs_engine import _is_rel_frozen, _rewrite_moved_links, _rewrite_prose, _WIKILINK_RE, _split_inner, _rewrite_content_move
    for old, new in moves.items():
        if _is_rel_frozen(vault_dir, old) or _is_rel_frozen(vault_dir, new):
            raise VaultStoreError("E_DOMAIN_FROZEN", "移动涉及冻结域，未修改内容、位置或句柄", exit_code=1)
    moves = {old: new for old, new in moves.items() if old != new}
    if not moves:
        return
    original_paths = _scan_vault_markdowns(vault_dir)
    for rel in original_paths:
        if rel.startswith("05-Journal/") or not _is_rel_frozen(vault_dir, rel):
            continue
        text = (vault_dir / rel).read_text(encoding="utf-8")
        def rewrite(segment):
            def qualify(match):
                target, anchor, alias = _split_inner(match.group(2))
                candidates = resolve_wikilink(target, rel, original_paths)
                if len(candidates) != 1 or candidates[0] not in moves:
                    return match.group(0)
                exact = candidates[0][:-3]
                return f"{match.group(1)}[[{exact}{anchor}{'|' + alias if alias else ''}]]"
            updated = _rewrite_moved_links(_WIKILINK_RE.sub(qualify, segment), rel, moves.get(rel, rel), moves)
            # VFS also maintains the declared YAML scalar/list reference fields.
            for old, new in moves.items():
                old_path, new_path = Path(old), Path(new)
                if old_path.suffix != ".md" or old_path.stem == new_path.stem:
                    continue
                unique = len(resolve_wikilink(old_path.stem, rel, original_paths)) == 1
                updated = _rewrite_content_move(updated, old_path.stem, new_path.stem, "", "", allow_short=unique)
            return updated
        if _rewrite_prose(text, rewrite) != text:
            raise VaultStoreError("E_DOMAIN_FROZEN", "冻结域存在受本次移动影响的引用；未修改内容、位置或句柄", exit_code=1)

    # Canvas file nodes also carry exact vault-relative identities.
    import json
    from pipeline.atlas_vfs_engine import _iter_vault_ref_files
    for path in _iter_vault_ref_files(vault_dir):
        rel = path.relative_to(vault_dir).as_posix()
        if path.suffix != ".canvas" or not _is_rel_frozen(vault_dir, rel):
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        for node in doc.get("nodes", []):
            if node.get("type") == "file" and node.get("file") in moves:
                raise VaultStoreError("E_DOMAIN_FROZEN", "冻结域画布引用受移动影响；未修改内容、位置或句柄", exit_code=1)


def _domain_root_prefix(rel_path: str) -> str:
    parts = Path(rel_path).parts
    if len(parts) >= 2 and parts[0] == "30-Projects":
        return f"30-Projects/{parts[1]}/"
    if parts and parts[0] == "20-AI-Zone":
        return "20-AI-Zone/"
    return ""


def _cascade_note_moves(vault_dir: Path, moves: Dict[str, str], original_paths=None) -> int:
    """Rewrite exact targets once (including .canvas), disambiguating same-domain bare wikilinks."""
    import json as _json
    from collections import Counter
    from pipeline.atlas_vfs_engine import (
        _WIKILINK_RE,
        _is_rel_frozen,
        _iter_vault_ref_files,
        _rewrite_moved_links,
        _rewrite_prose,
        _split_inner,
    )
    moves = {old: new for old, new in moves.items() if old != new}
    if not moves:
        return 0
    current_paths = _scan_vault_markdowns(vault_dir)
    stem_counts = Counter(Path(p).stem for p in current_paths)
    inverse = {new: old for old, new in moves.items()}
    original_paths = original_paths if original_paths is not None else [inverse.get(p, p) for p in current_paths]
    count = 0
    for current_rel in current_paths:
        if current_rel.startswith("05-Journal/") or _is_rel_frozen(vault_dir, current_rel):
            continue
        before_rel = inverse.get(current_rel, current_rel)
        before_dom = _domain_root_prefix(before_rel)
        path = vault_dir / current_rel
        old_text = path.read_text(encoding="utf-8")

        def rewrite(segment):
            def qualify(match):
                target, anchor, alias = _split_inner(match.group(2))
                candidates = resolve_wikilink(target, before_rel, original_paths)
                if len(candidates) > 1 and before_dom:
                    same_dom = [c for c in candidates if _domain_root_prefix(c) == before_dom]
                    if len(same_dom) == 1:
                        candidates = same_dom
                if len(candidates) != 1 or candidates[0] not in moves:
                    return match.group(0)
                new_rel = moves[candidates[0]]
                new_stem = Path(new_rel).stem
                # Preserve Obsidian bare short wikilink when original was bare & new stem is unique (zero path leak).
                if "/" not in target and stem_counts.get(new_stem, 0) == 1:
                    if Path(candidates[0]).stem == new_stem:
                        return match.group(0)
                    return f"{match.group(1)}[[{new_stem}{anchor}{'|' + alias if alias else ''}]]"
                exact = candidates[0][:-3]
                return f"{match.group(1)}[[{exact}{anchor}{'|' + alias if alias else ''}]]"

            return _rewrite_moved_links(_WIKILINK_RE.sub(qualify, segment), before_rel, current_rel, moves)

        new_text = _rewrite_prose(old_text, rewrite)
        if new_text != old_text:
            _atomic_write_md(path, new_text)
            count += 1

    # Keep .canvas file nodes synchronized when notes move or rename
    for ref_file in _iter_vault_ref_files(vault_dir):
        if ref_file.suffix.lower() != ".canvas":
            continue
        ref_rel = ref_file.relative_to(vault_dir).as_posix()
        if _is_rel_frozen(vault_dir, ref_rel):
            continue
        try:
            doc = _json.loads(ref_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        changed_canvas = False
        for node in doc.get("nodes", []):
            if isinstance(node, dict) and node.get("type") == "file" and node.get("file") in moves:
                node["file"] = moves[node["file"]]
                changed_canvas = True
        if changed_canvas:
            _atomic_write_md(ref_file, _json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
            count += 1
    return count


def _cascade_wikilinks(vault_dir: Path, old_stem: str, new_stem: str) -> int:
    if old_stem == new_stem:
        return 0
    pat = re.compile(rf"\[\[{re.escape(old_stem)}([|#][^\]]*)?\]\]")
    updated_files = 0
    for rel in _scan_vault_markdowns(vault_dir):
        p = vault_dir / rel
        txt = p.read_text(encoding="utf-8")
        new_txt, n = pat.subn(lambda m: f"[[{new_stem}{m.group(1) or ''}]]", txt)
        if n > 0:
            _atomic_write_md(p, new_txt)
            updated_files += 1
    return updated_files


def _public_note(
    *,
    kek: bytes,
    app_id: str,
    vault_ref: str,
    note_ref: str,
    rel: str,
    meta: Dict[str, Any],
) -> Dict[str, Any]:
    zone, prel = infer_zone_and_project_rel(rel)
    stem = Path(rel).stem
    title_val = str(meta.get("title") or stem)
    out: Dict[str, Any] = {
        "vault_ref": vault_ref,
        "note_ref": note_ref,
        "zone": zone,
        "domain_scope": "project" if prel else "ai-zone",
        "doc_type": str(meta.get("doc_type") or ""),
        "title": title_val,
        "note_name": stem,
        "wikilink": f"[[{stem}|{title_val}]]",
        "summary": str(meta.get("summary") or ""),
        "status": str(meta.get("status") or ""),
        "tags": meta.get("tags") or [],
        "updated": str(meta.get("updated") or ""),
        "handle_seal": _note_seal(kek, app_id, vault_ref, note_ref, rel),
        "path_sealed": True,
    }
    if meta.get("project_id"):
        out["project_id"] = str(meta["project_id"])
    return out


def note_create(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    zone: str,
    doc_type: str,
    title: str,
    summary: str,
    tags: Any,
    aliases: Any,
    content: str,
    status: str,
    proj_reg: Optional[KekProjectRegistry] = None,
    project_ref: Optional[str] = None,
    private: bool = False,
    subdir: Optional[str] = None,
    by_actor: str = "",
    cache_dir: Optional[Path] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    raw_zone = (zone or "").strip().lower()
    raw_sub = str(subdir or "").strip().strip("/")
    if not raw_zone:
        sub_head = raw_sub.split("/", 1)[0].lower() if raw_sub else ""
        if sub_head.startswith("ws-") or (project_ref and sub_head in ("drafts", "source", "91-temp")):
            z = "workbench"
        else:
            z = "dropzone"
    else:
        z = raw_zone
    if z not in ZONES and z != "workbench":
        raise VaultStoreError("E_PARAM_INVALID", f"不支持的逻辑区 zone: {z}", exit_code=1)
    dt = (doc_type or DEFAULT_DOC_TYPE_BY_ZONE.get(z, "memo")).strip().lower()
    dom: Optional[Dict[str, Any]] = None
    if project_ref and proj_reg is not None:
        dom = resolve_domain_scope(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            proj_reg=proj_reg,
            explicit_project_ref=project_ref,
            scope="project",
        )
    default_st = "pending" if (z == "dropzone" or (dom and dom["default_mode"] == "supervised")) else "active"
    st = "" if dt == "journal" else (status or default_st).strip().lower()
    tag_list = _parse_list_arg(tags) or [dt.capitalize()]
    alias_list = _parse_list_arg(aliases)
    clean_title = title.strip()
    if not clean_title:
        raise VaultStoreError("E_PARAM_MISSING", "note-create 需要 --title", exit_code=1)

    slug = _slugify_filename(clean_title)
    clean_sub = ""
    if subdir is not None and str(subdir).strip():
        raw_sub = str(subdir).strip().strip("/")
        sub_parts = [p for p in raw_sub.split("/") if p]
        if not sub_parts or "\\" in raw_sub or any(p in (".", "..") or p.startswith(".") for p in sub_parts):
            raise VaultStoreError("E_PARAM_INVALID", f"非法子目录参数 --subdir: {subdir}", exit_code=1)
        if any(p.lower() in _RESERVED_SUBDIR_NAMES for p in sub_parts):
            raise VaultStoreError(
                "E_PARAM_INVALID",
                f"--subdir 禁止使用系统保留分区名 {subdir}（工作台目录请使用 workbench-workspace --agent <name>）",
                exit_code=1,
            )
        clean_sub = "/".join(sub_parts)
    if dom is not None:
        base_dir = resolve_zone_dir(vault_dir, dom, z)
        base_rel = base_dir.relative_to(vault_dir).as_posix()
    else:
        base_rel = ZONES[z]
    rel_dir = f"{base_rel}/{clean_sub}" if clean_sub else base_rel
    fname_stem = slug
    if z == "artifacts" and dt == "artifact":
        art_pid_tok = str(dom["project_id"]) if dom is not None else "AI-Zone"
        dest_art_dir = vault_dir / rel_dir
        if dest_art_dir.is_dir():
            for cand in sorted(dest_art_dir.glob("*.md")):
                if cand.is_symlink() or not cand.is_file():
                    continue
                if cand.stem == slug:
                    raise VaultStoreError("E_PARAM_CONFLICT", "目标知识库内已存在同名笔记", exit_code=1)
                try:
                    cm, _, cfm = extract_frontmatter(cand.read_text(encoding="utf-8"))
                    if cfm and str(cm.get("title") or "").strip().casefold() == clean_title.casefold():
                        raise VaultStoreError("E_PARAM_CONFLICT", "目标知识库内已存在同名笔记", exit_code=1)
                except FrontmatterError:
                    continue
        if not slug.startswith(f"Art-{art_pid_tok}-"):
            pat = re.compile(r"^Art-" + re.escape(art_pid_tok) + r"-(\d+)-")
            mx = max((int(m.group(1)) for f in (dest_art_dir.glob("*.md") if dest_art_dir.is_dir() else []) for m in [pat.match(f.name)] if m), default=0)
            seq = mx + 1
            while (dest_art_dir / f"Art-{art_pid_tok}-{seq:03d}-{slug}.md").exists():
                seq += 1
            fname_stem = f"Art-{art_pid_tok}-{seq:03d}-{slug}"
    rel = validate_note_rel_path(f"{rel_dir}/{fname_stem}.md")
    target = vault_dir / rel
    if target.exists():
        raise VaultStoreError("E_PARAM_CONFLICT", "目标知识库内已存在同名笔记", exit_code=1)

    eff_summary = summary.strip() or (_first_meaningful_sentence(content or "", clean_title) if (content or "").strip() else clean_title)
    rendered = _render_note_md(
        doc_type=dt,
        title=clean_title,
        summary=eff_summary,
        status=st,
        tags=tag_list,
        aliases=alias_list,
        updated=_today(),
        body=content or "",
    )
    if dom is not None and dt != "journal":
        rendered = replace_frontmatter_field(rendered, "project_id", f'"{fm_scalar(dom["project_id"])}"')
    actor = (by_actor or "").strip() or "ai"
    if dt == "artifact":
        rendered = replace_frontmatter_field(rendered, "stage", "draft")
        rendered = replace_frontmatter_field(rendered, "type", "SYN")
        rendered = replace_frontmatter_field(rendered, "processed", "false")
        rendered = replace_frontmatter_field(rendered, "created_by", f'"{fm_scalar(actor)}"')
        rendered = replace_frontmatter_field(rendered, "updated_by", f'"{fm_scalar(actor)}"')
    elif (by_actor or "").strip():
        rendered = replace_frontmatter_field(rendered, "created_by", f'"{fm_scalar(actor)}"')
        rendered = replace_frontmatter_field(rendered, "updated_by", f'"{fm_scalar(actor)}"')
    if private:
        rendered = replace_frontmatter_field(rendered, "private", "true")
    rewrites: List[Dict[str, str]] = []
    pre_rels: Optional[List[str]] = None
    if "[[" in rendered:
        from notes.atlas_wikilink_tools import build_vault_link_index, normalize_inline_wikilinks

        pre_rels = _scan_vault_markdowns(vault_dir)
        pre_map = note_reg.list_vault_handles(vault_ref)
        if set(pre_rels) != set(pre_map.keys()):
            pre_map = note_reg.sync_paths(vault_ref, pre_rels)
        if pre_map:
            idx = build_vault_link_index(vault_dir, pre_map)
            rendered, rewrites = normalize_inline_wikilinks(rendered, source_rel=rel, index=idx)
    meta, _body, _has_fm = extract_frontmatter(rendered)
    errs = validate_note_meta(meta)
    if errs:
        raise VaultStoreError("E_PARAM_INVALID", "; ".join(errs), exit_code=1)

    _atomic_write_md(target, rendered)
    j_rel = _append_journal_event(
        vault_dir, note_reg, vault_ref, slug, f"创建笔记「{clean_title}」({z}/{dt})", sync_registry=False
    )
    mapping = note_reg.upsert_paths(vault_ref, [rel, j_rel])
    if cache_dir is not None:
        from storage.note_index_cache import update_single_note_index

        update_single_note_index(vault_dir=vault_dir, vault_ref=vault_ref, rel_paths=[rel, j_rel], kek=kek, app_id=app_id, cache_dir=cache_dir)
    nref = mapping[rel]
    pub = _public_note(kek=kek, app_id=app_id, vault_ref=vault_ref, note_ref=nref, rel=rel, meta=meta)
    if rewrites:
        pub["links_resolved_count"] = len(rewrites)
        pub["resolved_links"] = _sanitize_resolved_links(rewrites, limit=20)
    sealed = [rel, j_rel]
    if dom and dom.get("project_rel_path"):
        sealed.append(str(dom["project_rel_path"]))
    return pub, sealed


def note_list(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    zone_filter: Optional[str] = None,
    doc_type_filter: Optional[str] = None,
    status_filter: Optional[str] = None,
    proj_reg: Optional[KekProjectRegistry] = None,
    project_ref: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    rels = _scan_vault_markdowns(vault_dir)
    mapping = note_reg.sync_paths(vault_ref, rels)
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
    items: List[Dict[str, Any]] = []
    for rel in rels:
        if proj_prefix and not rel.startswith(proj_prefix):
            continue
        z = _zone_for_rel(rel)
        if zone_filter and z != zone_filter.strip().lower():
            continue
        txt = (vault_dir / rel).read_text(encoding="utf-8")
        try:
            meta, _b, _h = extract_frontmatter(txt)
        except FrontmatterError:
            meta = {"doc_type": "invalid", "title": Path(rel).stem}
        if proj_prefix:
            meta.setdefault("project_id", dom["project_id"])
        if doc_type_filter and str(meta.get("doc_type") or "") != doc_type_filter.strip().lower():
            continue
        if status_filter and str(meta.get("status") or "") != status_filter.strip().lower():
            continue
        items.append(
            _public_note(
                kek=kek,
                app_id=app_id,
                vault_ref=vault_ref,
                note_ref=mapping[rel],
                rel=rel,
                meta=meta,
            )
        )
    return items, rels


def _slice_text_lines(
    text: str,
    *,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    head: Optional[int] = None,
    tail: Optional[int] = None,
) -> Dict[str, Any]:
    """Return full text or a 1-based inclusive line slice (supports --start_line/--end_line, --head, --tail)."""
    lines_keepends = text.splitlines(keepends=True)
    total_lines = len(lines_keepends)
    if head is not None and head < 1:
        raise VaultStoreError("E_PARAM_INVALID", "--head 必须 >= 1", exit_code=1)
    if tail is not None and tail < 1:
        raise VaultStoreError("E_PARAM_INVALID", "--tail 必须 >= 1", exit_code=1)
    if head is not None and tail is not None:
        raise VaultStoreError("E_PARAM_INVALID", "--head 与 --tail 不能同时传入", exit_code=1)
    if (head is not None or tail is not None) and (start_line is not None or end_line is not None):
        raise VaultStoreError("E_PARAM_INVALID", "--head / --tail 不能与 --start_line / --end_line 同时传入", exit_code=1)
    if start_line is not None and start_line < 1:
        raise VaultStoreError("E_PARAM_INVALID", "--start_line 必须 >= 1", exit_code=1)
    if end_line is not None and end_line < 1:
        raise VaultStoreError("E_PARAM_INVALID", "--end_line 必须 >= 1", exit_code=1)
    if start_line is not None and end_line is not None and end_line < start_line:
        raise VaultStoreError("E_PARAM_INVALID", "--end_line 不能小于 --start_line", exit_code=1)

    if start_line is None and end_line is None and head is None and tail is None:
        return {
            "raw_text": text,
            "total_lines": total_lines,
            "returned_start_line": 1 if total_lines else 0,
            "returned_end_line": total_lines,
            "truncated": False,
        }

    if head is not None:
        s_idx = 1
        e_idx = min(head, total_lines)
    elif tail is not None:
        s_idx = max(1, total_lines - tail + 1)
        e_idx = total_lines
    else:
        s_idx = start_line if start_line is not None else 1
        e_idx = min(end_line, total_lines) if end_line is not None else total_lines
    if total_lines == 0 or s_idx > total_lines:
        return {
            "raw_text": "",
            "total_lines": total_lines,
            "returned_start_line": 0,
            "returned_end_line": 0,
            "truncated": total_lines > 0,
        }
    return {
        "raw_text": "".join(lines_keepends[s_idx - 1 : e_idx]),
        "total_lines": total_lines,
        "returned_start_line": s_idx,
        "returned_end_line": e_idx,
        "truncated": s_idx > 1 or e_idx < total_lines,
    }


_ATX_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*$")


def _extract_markdown_outline(body: str, *, line_offset: int = 0) -> List[Dict[str, Any]]:
    from notes.atlas_markdown_blocks import code_line_mask

    raw_headings: List[Tuple[int, int, str]] = []
    lines = body.splitlines()
    for idx, (line, is_code) in enumerate(zip(lines, code_line_mask(body)), start=1):
        if is_code:
            continue
        m = _ATX_HEADING_RE.match(line)
        if m:
            raw_headings.append((idx, len(m.group(1)), m.group(2).strip()))
    outline: List[Dict[str, Any]] = []
    total_body_lines = len(lines)
    for pos, (start_idx, lvl, htxt) in enumerate(raw_headings):
        end_idx = total_body_lines
        for next_idx, next_lvl, _ in raw_headings[pos + 1 :]:
            if next_lvl <= lvl:
                end_idx = next_idx - 1
                break
        sec_chars = sum(len(lines[i]) + 1 for i in range(start_idx - 1, max(start_idx - 1, end_idx)))
        outline.append(
            {
                "level": lvl,
                "heading": htxt,
                "line": start_idx + line_offset,
                "start_line": start_idx + line_offset,
                "end_line": max(start_idx, end_idx) + line_offset,
                "chars": sec_chars,
            }
        )
    return outline


def _extract_section_body(body: str, section_query: str) -> Optional[str]:
    from notes.atlas_markdown_blocks import code_line_mask

    q = section_query.strip().casefold()
    if not q:
        return None
    lines = body.splitlines()
    mask = code_line_mask(body)
    headings: List[Tuple[int, int, str]] = []
    for idx, (line, is_code) in enumerate(zip(lines, mask)):
        if is_code:
            continue
        m = _ATX_HEADING_RE.match(line)
        if m:
            headings.append((idx, len(m.group(1)), m.group(2).strip()))
    match_pos: Optional[int] = None
    for pos, (_line_idx, _lvl, htxt) in enumerate(headings):
        if htxt.casefold() == q:
            match_pos = pos
            break
    if match_pos is None:
        for pos, (_line_idx, _lvl, htxt) in enumerate(headings):
            if q in htxt.casefold():
                match_pos = pos
                break
    if match_pos is None:
        return None
    start_idx, start_lvl, _ = headings[match_pos]
    end_idx = len(lines)
    for next_idx, next_lvl, _ in headings[match_pos + 1 :]:
        if next_lvl <= start_lvl:
            end_idx = next_idx
            break
    return "\n".join(lines[start_idx:end_idx]).strip()


def _format_yaml_meta_value(val: Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, list):
        inner = ", ".join(f'"{fm_scalar(str(x))}"' for x in val)
        return f"[{inner}]"
    s = str(val)
    if s.lower() in ("true", "false"):
        return s.lower()
    return f'"{fm_scalar(s)}"'


def _parse_set_meta_arg(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return {str(k).strip(): v for k, v in raw.items() if str(k).strip()}
    s = str(raw).strip()
    if not s:
        return {}
    if s.startswith("{"):
        import json as _json
        try:
            obj = _json.loads(s)
        except Exception as exc:
            raise VaultStoreError("E_PARAM_INVALID", f"--set_meta JSON 解析失败: {exc}", exit_code=1) from exc
        if not isinstance(obj, dict):
            raise VaultStoreError("E_PARAM_INVALID", "--set_meta 必须为 JSON 对象或 key=value 列表", exit_code=1)
        return {str(k).strip(): v for k, v in obj.items() if str(k).strip()}
    out: Dict[str, Any] = {}
    for part in s.split(","):
        p = part.strip()
        if not p:
            continue
        if "=" not in p:
            raise VaultStoreError("E_PARAM_INVALID", f"--set_meta 条目缺少 '=': {p}", exit_code=1)
        k, v = p.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _note_get_with_context(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_ref: str,
    rel: str,
    fpath: Path,
    mapping: Dict[str, str],
    index_entries: Optional[Dict[str, Dict[str, Any]]],
    kek: bytes,
    app_id: str,
    raw: bool = False,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    head: Optional[int] = None,
    tail: Optional[int] = None,
    section: Optional[str] = None,
    outline: bool = False,
    backlink_map: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, Any]:
    raw_bytes = fpath.read_bytes()
    txt = raw_bytes.decode("utf-8")
    meta, body, has_fm = extract_frontmatter(txt)
    body_line_offset = 0
    if has_fm:
        fm_match = re.match(r"\A---[^\S\n]*\r?\n.*?\r?\n---[^\S\n]*(?:\r?\n|$)", txt, re.S)
        if fm_match:
            body_line_offset = txt[: fm_match.end()].count("\n")

    outgoing_refs = []
    for target in extract_wikilinks(body):
        candidates = resolve_wikilink(target, rel, mapping)
        safe_stem = target.rsplit("/", 1)[-1] if "/" in target else target
        outgoing_refs.append({
            "stem": safe_stem,
            "note_ref": mapping[candidates[0]] if len(candidates) == 1 else None,
            "resolution": "resolved" if len(candidates) == 1 else ("ambiguous" if candidates else "missing"),
            "candidate_refs": [mapping[p] for p in candidates],
        })

    if index_entries is not None:
        from storage.note_index_cache import compute_backlinks_from_index

        backlinks = compute_backlinks_from_index(target_rel=rel, entries=index_entries, handle_map=mapping, backlink_map=backlink_map)
    else:
        backlinks = []
        for other_rel in mapping:
            if other_rel == rel or Path(other_rel).name.startswith("Manifest-"):
                continue
            op = vault_dir / other_rel
            if not op.is_file():
                continue
            otxt = op.read_text(encoding="utf-8", errors="replace")
            if any(resolve_wikilink(t, other_rel, mapping) == [rel] for t in extract_wikilinks(otxt)):
                backlinks.append(mapping[other_rel])

    pub = _public_note(kek=kek, app_id=app_id, vault_ref=vault_ref, note_ref=note_ref, rel=rel, meta=meta)
    pub["aliases"] = meta.get("aliases") or []
    body_clean = body.strip()
    if outline:
        pub["outline_only"] = True
        pub["total_lines"] = len(txt.splitlines())
        pub["total_chars"] = len(body_clean)
        pub["toc_outline"] = _extract_markdown_outline(body, line_offset=body_line_offset)
    elif section is not None and str(section).strip():
        sec_text = _extract_section_body(body_clean, str(section))
        if sec_text is None:
            raise VaultStoreError("E_NOT_FOUND", f"未在笔记 {note_ref} 中找到匹配章节: {section}", exit_code=1)
        pub["content"] = sec_text
        pub["section"] = str(section).strip()
    elif not raw and start_line is None and end_line is None and head is None and tail is None and pub.get("zone") == "library" and len(body_clean) > 4000:
        pub["content"] = (
            body_clean[:2000].rstrip()
            + f"\n\n... [长文预览截断，完整正文共 {len(body_clean)} 字；请传 --section <标题>、--head/--tail、--start_line/--end_line 或 --raw true 读取详情]"
        )
        pub["truncated_preview"] = True
        pub["total_chars"] = len(body_clean)
        pub["toc_outline"] = _extract_markdown_outline(body, line_offset=body_line_offset)
    else:
        pub["content"] = body_clean
    pub["content_sha256"] = hashlib.sha256(raw_bytes).hexdigest()
    pub["outgoing_links"] = outgoing_refs
    pub["backlink_refs"] = backlinks
    if not outline and (raw or start_line is not None or end_line is not None or head is not None or tail is not None):
        sliced = _slice_text_lines(txt, start_line=start_line, end_line=end_line, head=head, tail=tail)
        pub["frontmatter"] = meta
        pub["size_bytes"] = len(raw_bytes)
        pub.update(sliced)
        if not raw and (head is not None or tail is not None):
            pub["content"] = sliced["raw_text"]
    return pub


def note_get(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_ref: str,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    raw: bool = False,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    head: Optional[int] = None,
    tail: Optional[int] = None,
    section: Optional[str] = None,
    outline: bool = False,
    cache_dir: Optional[Path] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    from storage.note_index_cache import reconcile_note_index, resolve_note_jit

    rel, fpath = resolve_note_jit(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        note_ref=note_ref,
        note_reg=note_reg,
        kek=kek,
        app_id=app_id,
        cache_dir=cache_dir,
    )
    entries, mapping = reconcile_note_index(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        note_reg=note_reg,
        kek=kek,
        app_id=app_id,
        cache_dir=cache_dir,
    )
    pub = _note_get_with_context(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        note_ref=note_ref,
        rel=rel,
        fpath=fpath,
        mapping=mapping,
        index_entries=entries,
        kek=kek,
        app_id=app_id,
        raw=raw,
        start_line=start_line,
        end_line=end_line,
        head=head,
        tail=tail,
        section=section,
        outline=outline,
    )
    return pub, [rel]


def note_get_batch(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_refs: Any,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    raw: bool = False,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    head: Optional[int] = None,
    tail: Optional[int] = None,
    section: Optional[str] = None,
    outline: bool = False,
    cache_dir: Optional[Path] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Read multiple notes by handle in a single pass sharing one index & link-graph load."""
    refs = list(dict.fromkeys(_parse_list_arg(note_refs)))
    if not refs:
        raise VaultStoreError("E_PARAM_MISSING", "批量读取需要提供至少一个 --note_refs 句柄", exit_code=1)
    from storage.note_index_cache import build_backlink_map, reconcile_note_index, resolve_note_jit

    entries, mapping = reconcile_note_index(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        note_reg=note_reg,
        kek=kek,
        app_id=app_id,
        cache_dir=cache_dir,
    )
    bl_map = build_backlink_map(entries=entries, handle_map=mapping)
    items: List[Dict[str, Any]] = []
    sealed: List[str] = []
    for nref in refs:
        rel, fpath = resolve_note_jit(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            note_ref=nref,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            cache_dir=cache_dir,
        )
        pub = _note_get_with_context(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            note_ref=nref,
            rel=rel,
            fpath=fpath,
            mapping=mapping,
            index_entries=entries,
            kek=kek,
            app_id=app_id,
            raw=raw,
            start_line=start_line,
            end_line=end_line,
            head=head,
            tail=tail,
            section=section,
            outline=outline,
            backlink_map=bl_map,
        )
        items.append(pub)
        sealed.append(rel)
    return {
        "vault_ref": vault_ref,
        "count": len(items),
        "notes": items,
        "path_sealed": True,
    }, sealed


def _update_frontmatter_preserving_extras(
    old_txt: str,
    updates: List[Tuple[str, str]],
    new_body: Optional[str],
    fallback_title: str,
) -> str:
    """Update standard Frontmatter keys in-place while preserving custom Obsidian/.base metadata."""
    lines = old_txt.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return old_txt
    fm_lines = lines[1:end]
    for key, raw_val in updates:
        pat = re.compile(rf"^{re.escape(key)}\s*:")
        out: List[str] = []
        replaced = False
        skip_indented = False
        for ln in fm_lines:
            if skip_indented:
                if ln.startswith((" ", "\t")) or ln.lstrip().startswith("- "):
                    # Skip multiline continuation items (e.g. YAML block lists) under replaced key.
                    continue
                skip_indented = False
            if not replaced and pat.match(ln):
                out.append(f"{key}: {raw_val}")
                replaced = True
                skip_indented = True
                continue
            out.append(ln)
        if not replaced:
            out.append(f"{key}: {raw_val}")
        fm_lines = out
    if new_body is None:
        tail = lines[end + 1 :]
        rendered = "\n".join(["---", *fm_lines, "---", *tail])
        return rendered + ("\n" if old_txt.endswith("\n") or not tail else "")
    cleaned_body = new_body.strip() if new_body.strip() else f"# {fallback_title}"
    return "\n".join(["---", *fm_lines, "---", "", cleaned_body, ""])


def _enforce_note_write_guards(
    *,
    vault_dir: Path,
    rel: str,
    old_body: str,
    new_body: Optional[str],
    allow_managed_block: bool = False,
    op_name: str = "note-update",
) -> None:
    """Shared pre-write guard for note-update & note-patch: frozen domain, 21-Library read-only body, 05-Journal human section, and managed blocks."""
    parts = Path(rel).parts
    if len(parts) >= 2 and parts[0] == "30-Projects":
        proj_id = parts[1]
        pdir = vault_dir / "30-Projects" / proj_id
        if pdir.is_dir():
            p_manifest = read_domain_manifest(pdir, proj_id)
            p_status = str(p_manifest.get("status") or "").strip().lower()
            if p_status in ("archived", "deprecated"):
                raise VaultStoreError(
                    "E_DOMAIN_FROZEN",
                    f"课题域 '{proj_id}' 处于冻结状态 ({p_status})，禁止通过 {op_name} 写入或修改",
                    exit_code=1,
                )

    zone = _zone_for_rel(rel)
    if zone == "library" and new_body is not None and new_body.strip() != old_body.strip():
        raise VaultStoreError(
            "E_READONLY_LIBRARY",
            f"21-Library 原始文献正文只读保真，禁止通过 {op_name} 改写正文；请在切片/原子卡或工作台草稿区派生",
            exit_code=1,
        )

    if rel.startswith("05-Journal/") and new_body is not None:
        human_before = _extract_section_body(old_body, "人类记录")
        if human_before is not None:
            human_after = _extract_section_body(new_body, "人类记录")
            if human_after != human_before:
                raise VaultStoreError(
                    "E_JOURNAL_PROTECTED",
                    f"日记（05-Journal）# 人类记录 区受永久保护，严禁通过 {op_name} 修改",
                    exit_code=1,
                )

    if new_body is not None and not allow_managed_block:
        managed_before = _extract_managed_blocks(old_body)
        for bname, block_exact in managed_before:
            if block_exact not in new_body:
                raise VaultStoreError(
                    "E_MANAGED_BLOCK_PROTECTED",
                    f"更新触碰或破坏了系统受控块（{bname}），已自动拦截；如需显式覆盖请传 --allow_managed_block true",
                    exit_code=1,
                )


def _prepare_note_update(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_ref: str,
    note_reg: KekNoteRegistry,
    title: Optional[str],
    summary: Optional[str],
    tags: Any,
    status: Optional[str],
    content: Optional[str],
    set_meta: Any = None,
    expected_sha256: Optional[str] = None,
    link_index: Optional[Dict[str, Any]] = None,
    allow_managed_block: bool = False,
    op_name: str = "note-update",
) -> Tuple[Path, str, str, Dict[str, Any], List[Dict[str, str]], Optional[Dict[str, Any]], bytes, str]:
    rec = note_reg.resolve_handle(vault_ref, note_ref)
    rel = str(rec["note_rel_path"])
    fpath = vault_dir / rel
    if not fpath.is_file():
        raise VaultStoreError("E_NOT_FOUND", f"句柄 {note_ref} 对应笔记不存在", exit_code=1)
    raw_bytes = fpath.read_bytes()
    pre_sha = hashlib.sha256(raw_bytes).hexdigest()
    if expected_sha256 and pre_sha != expected_sha256:
        raise VaultStoreError("E_VERSION_CONFLICT", f"笔记 {note_ref} 内容哈希已变化，请重新读取后再更新", exit_code=1)
    old_txt = raw_bytes.decode("utf-8")
    meta, body, has_fm = extract_frontmatter(old_txt)
    _enforce_note_write_guards(
        vault_dir=vault_dir,
        rel=rel,
        old_body=body,
        new_body=content,
        allow_managed_block=allow_managed_block,
        op_name=op_name,
    )
    extra_meta = _parse_set_meta_arg(set_meta)
    doc_type = str(extra_meta.get("doc_type") or meta.get("doc_type") or "atomic")
    new_title = title.strip() if title is not None else str(extra_meta.get("title") or meta.get("title") or fpath.stem)
    new_summary = summary.strip() if summary is not None else str(extra_meta.get("summary") or meta.get("summary") or new_title)
    new_tags = _parse_list_arg(tags) if tags is not None else (_parse_list_arg(extra_meta["tags"]) if "tags" in extra_meta else list(meta.get("tags") or []))
    new_status = status.strip().lower() if status is not None else str(extra_meta.get("status") or meta.get("status") or "active")
    if doc_type == "journal":
        new_status = ""

    if has_fm:
        tags_yaml = ", ".join(f'"{fm_scalar(t)}"' for t in new_tags)
        updates: List[Tuple[str, str]] = [
            ("doc_type", doc_type),
            ("title", f'"{fm_scalar(new_title)}"'),
        ]
        if doc_type != "journal":
            updates.append(("summary", f'"{fm_scalar(new_summary or new_title)}"'))
            updates.append(("status", new_status))
        updates.extend([("tags", f"[{tags_yaml}]"), ("updated", _today())])
        drop_keys: List[str] = []
        for mk, mv in extra_meta.items():
            if mk in ("doc_type", "title", "summary", "status", "tags", "updated"):
                continue
            if mv is None:
                drop_keys.append(mk)
            else:
                updates.append((mk, _format_yaml_meta_value(mv)))
        rendered = _update_frontmatter_preserving_extras(old_txt, updates, content, new_title)
        for dk in drop_keys:
            rendered = drop_frontmatter_field(rendered, dk)
    else:
        new_body = content if content is not None else body
        rendered = _render_note_md(
            doc_type=doc_type,
            title=new_title,
            summary=new_summary,
            status=new_status,
            tags=new_tags,
            aliases=list(meta.get("aliases") or []),
            updated=_today(),
            body=new_body,
        )
        for mk, mv in extra_meta.items():
            if mk not in ("doc_type", "title", "summary", "status", "tags", "updated"):
                rendered = replace_frontmatter_field(rendered, mk, _format_yaml_meta_value(mv))
    rewrites: List[Dict[str, str]] = []
    if "[[" in rendered:
        from notes.atlas_wikilink_tools import build_vault_link_index, normalize_inline_wikilinks

        if link_index is None:
            all_map = note_reg.list_vault_handles(vault_ref)
            if all_map:
                link_index = build_vault_link_index(vault_dir, all_map)
        if link_index is not None:
            rendered, rewrites = normalize_inline_wikilinks(rendered, source_rel=rel, index=link_index)
    nmeta, _, _ = extract_frontmatter(rendered)
    errs = validate_note_meta(nmeta)
    if errs:
        raise VaultStoreError("E_PARAM_INVALID", f"{note_ref}: " + "; ".join(errs), exit_code=1)
    return fpath, rel, rendered, nmeta, rewrites, link_index, raw_bytes, pre_sha


def note_update(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_ref: str,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    title: Optional[str],
    summary: Optional[str],
    tags: Any,
    status: Optional[str],
    content: Optional[str],
    set_meta: Any = None,
    expected_sha256: Optional[str] = None,
    allow_managed_block: bool = False,
    cache_dir: Optional[Path] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    fpath, rel, rendered, nmeta, rewrites, _, _, pre_sha = _prepare_note_update(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        note_ref=note_ref,
        note_reg=note_reg,
        title=title,
        summary=summary,
        tags=tags,
        status=status,
        content=content,
        set_meta=set_meta,
        expected_sha256=expected_sha256,
        allow_managed_block=allow_managed_block,
        op_name="note-update",
    )
    _atomic_write_md(fpath, rendered, expected_sha256=expected_sha256 or pre_sha)
    if cache_dir is not None:
        from storage.note_index_cache import update_single_note_index

        update_single_note_index(vault_dir=vault_dir, vault_ref=vault_ref, rel_paths=[rel], kek=kek, app_id=app_id, cache_dir=cache_dir)
    pub = _public_note(kek=kek, app_id=app_id, vault_ref=vault_ref, note_ref=note_ref, rel=rel, meta=nmeta)
    if rewrites:
        pub["links_resolved_count"] = len(rewrites)
        pub["resolved_links"] = _sanitize_resolved_links(rewrites, limit=20)
    return pub, [rel]


def note_update_batch(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    batch_json: Any = None,
    note_refs: Any = None,
    title: Optional[str] = None,
    summary: Optional[str] = None,
    tags: Any = None,
    status: Optional[str] = None,
    content: Optional[str] = None,
    set_meta: Any = None,
    allow_managed_block: bool = False,
    cache_dir: Optional[Path] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Transactional batch note update: pre-validate all notes in memory, write atomically, single index sync."""
    import json as _json

    items_spec: List[Dict[str, Any]] = []
    if batch_json is not None and str(batch_json).strip():
        if isinstance(batch_json, list):
            raw_items = batch_json
        else:
            try:
                raw_items = _json.loads(str(batch_json))
            except Exception as exc:
                raise VaultStoreError("E_PARAM_INVALID", f"--batch_json JSON 解析失败: {exc}", exit_code=1) from exc
        if not isinstance(raw_items, list) or not raw_items:
            raise VaultStoreError("E_PARAM_INVALID", "--batch_json 必须为非空 JSON 数组", exit_code=1)
        for entry in raw_items:
            if not isinstance(entry, dict) or not str(entry.get("note_ref") or "").strip():
                raise VaultStoreError("E_PARAM_INVALID", "--batch_json 每项必须包含 note_ref", exit_code=1)
            items_spec.append(entry)
    else:
        refs = list(dict.fromkeys(_parse_list_arg(note_refs)))
        if not refs:
            raise VaultStoreError("E_PARAM_MISSING", "批量更新需要 --batch_json 或 --note_refs", exit_code=1)
        for r in refs:
            items_spec.append({
                "note_ref": r,
                "title": title,
                "summary": summary,
                "tags": tags,
                "status": status,
                "content": content,
                "set_meta": set_meta,
            })

    # Phase 1: pre-validate all hashes, schemas, and rendered outputs in memory before any disk write
    prepared: List[Tuple[str, Path, str, str, Dict[str, Any], List[Dict[str, str]], str, bytes]] = []
    seen_refs: set[str] = set()
    shared_link_idx: Optional[Dict[str, Any]] = None
    for spec in items_spec:
        nref = str(spec["note_ref"]).strip()
        if nref in seen_refs:
            raise VaultStoreError("E_PARAM_INVALID", f"--batch_json 包含重复 note_ref: {nref}", exit_code=1)
        seen_refs.add(nref)
        exp_sha = str(spec.get("expected_sha256") or "").strip() or None
        fpath, rel, rendered, nmeta, rewrites, shared_link_idx, raw_bytes, pre_sha = _prepare_note_update(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            note_ref=nref,
            note_reg=note_reg,
            title=spec.get("title"),
            summary=spec.get("summary"),
            tags=spec.get("tags"),
            status=spec.get("status"),
            content=spec.get("content"),
            set_meta=spec.get("set_meta"),
            expected_sha256=exp_sha,
            link_index=shared_link_idx,
            allow_managed_block=bool(spec.get("allow_managed_block", allow_managed_block)),
            op_name="note-update",
        )
        prepared.append((nref, fpath, rel, rendered, nmeta, rewrites, exp_sha or pre_sha, raw_bytes))

    # Phase 2: atomic writes with full byte-exact pre-image rollback on partial failure
    updated_notes: List[Dict[str, Any]] = []
    changed_rels: List[str] = []
    written_pre_images: List[Tuple[Path, bytes]] = []
    try:
        for nref, fpath, rel, rendered, nmeta, rewrites, effective_sha, pre_bytes in prepared:
            _atomic_write_md(fpath, rendered, expected_sha256=effective_sha)
            written_pre_images.append((fpath, pre_bytes))
            changed_rels.append(rel)
            pub = _public_note(kek=kek, app_id=app_id, vault_ref=vault_ref, note_ref=nref, rel=rel, meta=nmeta)
            if rewrites:
                pub["links_resolved_count"] = len(rewrites)
                pub["resolved_links"] = _sanitize_resolved_links(rewrites, limit=20)
            updated_notes.append(pub)
    except Exception as exc:
        rollback_errors: List[str] = []
        for rb_path, rb_bytes in reversed(written_pre_images):
            try:
                _atomic_write_bytes(rb_path, rb_bytes)
            except Exception as rb_exc:
                rollback_errors.append(f"{rb_path.name}: {rb_exc}")
        if rollback_errors:
            raise VaultStoreError(
                "E_ROLLBACK_FAILED",
                f"批量更新失败且回滚未完全成功: {'; '.join(rollback_errors)}",
                exit_code=2,
            ) from exc
        raise

    # Phase 3: single incremental index cache update
    if cache_dir is not None and changed_rels:
        from storage.note_index_cache import update_single_note_index

        update_single_note_index(vault_dir=vault_dir, vault_ref=vault_ref, rel_paths=changed_rels, kek=kek, app_id=app_id, cache_dir=cache_dir)

    return {
        "vault_ref": vault_ref,
        "count": len(updated_notes),
        "updated_refs": [u["note_ref"] for u in updated_notes],
        "notes": updated_notes,
        "path_sealed": True,
    }, changed_rels


def note_promote(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_ref: str,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    target_zone: str,
    new_doc_type: Optional[str],
    on_conflict: str,
    proj_reg: Optional[KekProjectRegistry] = None,
    project_ref: Optional[str] = None,
    scope: str = "auto",
) -> Tuple[Dict[str, Any], List[str]]:
    tz = (target_zone or "atomics").strip().lower()
    if tz not in ("atomics", "moc", "library", "artifacts"):
        raise VaultStoreError("E_PARAM_INVALID", "--target_zone 仅支持 atomics / moc / library / artifacts", exit_code=1)
    policy = (on_conflict or "abort").strip().lower()
    if policy not in ("abort", "supersede", "merge"):
        raise VaultStoreError("E_PARAM_INVALID", "--on_conflict 仅支持 abort / supersede / merge", exit_code=1)

    rec = note_reg.resolve_handle(vault_ref, note_ref)
    old_rel = str(rec["note_rel_path"])
    src = vault_dir / old_rel
    if not src.is_file():
        raise VaultStoreError("E_NOT_FOUND", f"句柄 {note_ref} 对应源笔记不存在", exit_code=1)

    txt = src.read_text(encoding="utf-8")
    meta, body, _ = extract_frontmatter(txt)
    if old_rel.startswith("05-Journal/") or str(meta.get("doc_type") or "").strip().lower() == "journal":
        raise VaultStoreError("E_JOURNAL_PROTECTED", "日记（05-Journal）只归档不升阶删源、不删除；请显式提炼要点另建卡片，原日记保留归档", exit_code=1)
    if str(meta.get("status") or "").strip().lower() == "pending":
        raise VaultStoreError("E_PARAM_CONFLICT", "pending 状态待审笔记禁止直接升阶，请先转正 status=active", exit_code=1)
    src_dt_cur = str(meta.get("doc_type") or "").strip().lower()
    src_zone_cur = _zone_for_rel(old_rel)
    has_external_origin = bool(meta.get("source_url") or meta.get("original_file") or meta.get("source"))
    if tz == "atomics" and (
        (src_zone_cur == "workbench" and ("/source/" in f"/{old_rel}" or has_external_origin))
        or (src_dt_cur == "library" and has_external_origin)
    ):
        raise VaultStoreError(
            "E_PARAM_CONFLICT",
            "外部采集原文（workbench/source 或含外部出处的 21-Library 文献）禁止整篇直接 note-promote 进 22-Atomics；"
            "若在工作台 source/ 请先用 artifact-adopt --target_zone library 采收入库，再经 note-slice + atomic-extract --title <原子命题> --content <提炼正文> 萃取原子卡",
            exit_code=1,
        )

    dt = (new_doc_type or DEFAULT_DOC_TYPE_BY_ZONE[tz]).strip().lower()
    title = str(meta.get("title") or src.stem)
    stem = _slugify_filename(title)

    dom: Optional[Dict[str, Any]] = None
    if proj_reg is not None:
        _, src_prel = infer_zone_and_project_rel(old_rel)
        src_zone = _zone_for_rel(old_rel)
        effective_scope = (scope or "auto").strip().lower()
        if project_ref:
            dom = resolve_domain_scope(
                vault_dir=vault_dir,
                vault_ref=vault_ref,
                proj_reg=proj_reg,
                explicit_project_ref=project_ref,
                scope="project" if effective_scope == "auto" else effective_scope,
                source_rel_path=old_rel,
                source_meta=meta,
            )
        elif effective_scope == "auto" and src_prel and src_zone == tz:
            # Already in project's formal compartment (e.g. 22-Atomics -> atomics): promote upward to AI-Zone
            dom = resolve_domain_scope(
                vault_dir=vault_dir,
                vault_ref=vault_ref,
                proj_reg=proj_reg,
                scope="ai-zone",
            )
        else:
            dom = resolve_domain_scope(
                vault_dir=vault_dir,
                vault_ref=vault_ref,
                proj_reg=proj_reg,
                scope=effective_scope,
                source_rel_path=old_rel,
                source_meta=meta,
            )
        target_dir = resolve_zone_dir(vault_dir, dom, tz)
        new_rel = validate_note_rel_path(f"{target_dir.relative_to(vault_dir).as_posix()}/{stem}.md")
    else:
        new_rel = validate_note_rel_path(f"{ZONES[tz]}/{stem}.md")
    dst = vault_dir / new_rel

    if src.resolve() == dst.resolve():
        raise VaultStoreError("E_PARAM_CONFLICT", "源笔记已位于目标分区", exit_code=1)

    updated_md = replace_frontmatter_field(txt, "doc_type", dt)
    updated_md = replace_frontmatter_field(updated_md, "status", "active")
    updated_md = replace_frontmatter_field(updated_md, "updated", _today())
    if dt == "artifact" and not str(meta.get("stage") or "").strip():
        updated_md = replace_frontmatter_field(updated_md, "stage", "draft")
    if dom is not None:
        updated_md = replace_frontmatter_field(updated_md, "project_id", f'"{fm_scalar(dom["project_id"])}"')
    updated_md = prune_frontmatter_for_doc_type(updated_md, dt)
    cand_meta, _, _ = extract_frontmatter(updated_md)
    cand_errs = validate_note_meta(cand_meta)
    if cand_errs:
        raise VaultStoreError(
            "E_PARAM_INVALID",
            f"升阶前元数据校验失败，请先用 note-update 补齐: {'; '.join(cand_errs)}",
            exit_code=1,
        )

    _preflight_note_moves(vault_dir, {old_rel: new_rel})
    superseded_rel = None
    if dst.exists():
        if policy == "abort":
            raise VaultStoreError("E_PARAM_CONFLICT", "目标分区已存在同名笔记（可通过 --on_conflict supersede|merge 处理）", exit_code=1)
        if policy == "merge":
            dst_txt = dst.read_text(encoding="utf-8").rstrip()
            merged = dst_txt + f"\n\n## 合流补充 ({_today()})\n\n" + body.strip() + "\n"
            merged = replace_frontmatter_field(merged, "updated", _today())
            _atomic_write_md(dst, merged)
            src.unlink()
        else:
            ts = time.strftime("%Y%m%d%H%M%S")
            sup_name = f"{dst.stem}.superseded-{ts}.md"
            sup_path = dst.parent / sup_name
            superseded_rel = sup_path.relative_to(vault_dir).as_posix()
            old_dst_txt = dst.read_text(encoding="utf-8")
            old_dst_txt = replace_frontmatter_field(old_dst_txt, "status", "deprecated")
            old_dst_txt = replace_frontmatter_field(old_dst_txt, "superseded_by", f'"[[{stem}]]"')
            banner = f"> [!WARNING] 已废弃 · 本卡由 [[{stem}]] 于 {_today()} 替代\n\n"
            lines = old_dst_txt.splitlines()
            end_i = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
            if end_i is not None:
                lines.insert(end_i + 1, banner.rstrip())
                old_dst_txt = "\n".join(lines) + "\n"
            _atomic_write_md(sup_path, old_dst_txt)
            dst.unlink()
            updated_md = replace_frontmatter_field(updated_md, "supersedes", f'"[[{sup_path.stem}]]"')
            _atomic_write_md(dst, updated_md)
            src.unlink()
    else:
        _atomic_write_md(dst, updated_md)
        src.unlink()

    cascaded = _cascade_note_moves(vault_dir, {src.relative_to(vault_dir).as_posix(): new_rel})
    moved_rec = note_reg.move_handle(vault_ref, note_ref, new_rel)
    note_reg.sync_paths(vault_ref, _scan_vault_markdowns(vault_dir))
    _append_journal_event(vault_dir, note_reg, vault_ref, dst.stem, f"升阶笔记至 {tz} ({policy})")

    fmeta, _, _ = extract_frontmatter(dst.read_text(encoding="utf-8"))
    pub = _public_note(kek=kek, app_id=app_id, vault_ref=vault_ref, note_ref=note_ref, rel=new_rel, meta=fmeta)
    pub["key_version"] = int(moved_rec.get("key_version", 1))
    pub["wikilink_cascaded_files"] = cascaded
    sealed = [old_rel, new_rel] + ([superseded_rel] if superseded_rel else [])
    return pub, sealed


def note_deprecate(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_ref: str,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    superseded_by_ref: Optional[str],
    move_to_attic: bool = True,
    proj_reg: Optional[KekProjectRegistry] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    rec = note_reg.resolve_handle(vault_ref, note_ref)
    old_rel = str(rec["note_rel_path"])
    src = vault_dir / old_rel
    if not src.is_file():
        raise VaultStoreError("E_NOT_FOUND", f"句柄 {note_ref} 对应笔记不存在", exit_code=1)

    sup_stem = None
    if superseded_by_ref:
        srec = note_reg.resolve_handle(vault_ref, superseded_by_ref)
        sup_stem = Path(str(srec["note_rel_path"])).stem

    txt = src.read_text(encoding="utf-8")
    try:
        jmeta, _, _ = extract_frontmatter(txt)
    except FrontmatterError:
        jmeta = {}
    if old_rel.startswith("05-Journal/") or str(jmeta.get("doc_type") or "").strip().lower() == "journal":
        raise VaultStoreError("E_JOURNAL_PROTECTED", "日记（05-Journal）只归档不废弃、不删除，请改用 journal-archive 按月归档", exit_code=1)
    txt = replace_frontmatter_field(txt, "status", "deprecated")
    txt = replace_frontmatter_field(txt, "updated", _today())
    if sup_stem:
        txt = replace_frontmatter_field(txt, "superseded_by", f'"[[{sup_stem}]]"')
        banner = f"> [!WARNING] 已废弃 · 由 [[{sup_stem}]] 替代 ({_today()})"
    else:
        banner = f"> [!WARNING] 已废弃 ({_today()})"
    lines = txt.splitlines()
    end_i = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end_i is not None and banner not in txt:
        lines.insert(end_i + 1, banner)
        txt = "\n".join(lines) + "\n"

    new_rel = old_rel
    if move_to_attic and _zone_for_rel(old_rel) != "attic":
        _, src_prel = infer_zone_and_project_rel(old_rel)
        attic_prefix = f"{src_prel}/20-Memory/97-Attic" if src_prel else ZONES["attic"]
        new_rel = validate_note_rel_path(f"{attic_prefix}/{src.name}")
        dst = vault_dir / new_rel
        _atomic_write_md(dst, txt)
        src.unlink()
        note_reg.move_handle(vault_ref, note_ref, new_rel)
        target_path = dst
    else:
        _atomic_write_md(src, txt)
        target_path = src

    fmeta, _, _ = extract_frontmatter(target_path.read_text(encoding="utf-8"))
    pub = _public_note(kek=kek, app_id=app_id, vault_ref=vault_ref, note_ref=note_ref, rel=new_rel, meta=fmeta)
    return pub, [old_rel, new_rel]


def note_search(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    query: str,
    zone_filter: Optional[str],
    top_k: int,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    q = query.strip()
    if not q:
        raise VaultStoreError("E_PARAM_MISSING", "note-search 需要 --query", exit_code=1)
    terms = [t.lower() for t in re.split(r"\s+", q) if t.strip()]
    rels = _scan_vault_markdowns(vault_dir)
    mapping = note_reg.sync_paths(vault_ref, rels)

    docs: Dict[str, Dict[str, Any]] = {}
    for rel in rels:
        if zone_filter and _zone_for_rel(rel) != zone_filter.strip().lower():
            continue
        txt = (vault_dir / rel).read_text(encoding="utf-8")
        try:
            meta, body, _ = extract_frontmatter(txt)
        except FrontmatterError:
            meta, body = {"doc_type": "invalid", "title": Path(rel).stem}, txt
        docs[rel] = {
            "meta": meta,
            "fields": {
                "title": str(meta.get("title") or Path(rel).stem),
                "aliases": " ".join(str(a) for a in (meta.get("aliases") or []) if isinstance(a, str)),
                "summary": str(meta.get("summary") or ""),
                "tags": " ".join(str(t) for t in (meta.get("tags") or []) if isinstance(t, str)),
                "body": body,
            },
        }
    if not docs:
        return [], rels

    lengths: Dict[str, int] = {rel: sum(len(v) for v in d["fields"].values()) for rel, d in docs.items()}
    n_docs = len(docs)
    avgdl = (sum(lengths.values()) / n_docs) or 1.0

    df = {t: 0 for t in terms}
    tf_map: Dict[str, Dict[str, float]] = {}
    for rel, d in docs.items():
        lowered = {k: v.lower() for k, v in d["fields"].items()}
        tf: Dict[str, float] = {}
        for t in terms:
            freq = sum(BM25_WEIGHTS[k] * txt.count(t) for k, txt in lowered.items() if txt)
            if freq > 0:
                tf[t] = freq
                df[t] += 1
        if tf:
            tf_map[rel] = tf

    scored: List[Tuple[float, str]] = []
    for rel, tf in tf_map.items():
        dl = lengths[rel]
        s = 0.0
        for t, f in tf.items():
            idf = math.log(1.0 + (n_docs - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * (f * (BM25_K1 + 1.0)) / (f + BM25_K1 * (1.0 - BM25_B + BM25_B * dl / avgdl))
        if s > 0:
            scored.append((s, rel))

    scored.sort(key=lambda item: (-item[0], item[1]))
    hits: List[Dict[str, Any]] = []
    for score, rel in scored[: max(1, top_k)]:
        meta = docs[rel]["meta"]
        pub = _public_note(kek=kek, app_id=app_id, vault_ref=vault_ref, note_ref=mapping[rel], rel=rel, meta=meta)
        pub["bm25_score"] = round(score, 4)
        body_clean = " ".join(docs[rel]["fields"]["body"].split())
        pub["snippet"] = body_clean[:140]
        hits.append(pub)
    return hits, rels


def note_search_all_vaults(
    *,
    vault_entries: List[Tuple[str, str, Path]],  # (vault_ref, vault_title, vault_dir)
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    query: str,
    zone_filter: Optional[str],
    top_k: int,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Federated BM25 search across all active non-deleted Vaults without leaking paths."""
    merged_hits: List[Dict[str, Any]] = []
    sealed: List[str] = []
    per_vault_k = max(5, top_k)
    for vref, vtitle, vdir in vault_entries:
        if not vdir.is_dir():
            continue
        hits, rels = note_search(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            query=query,
            zone_filter=zone_filter,
            top_k=per_vault_k,
        )
        sealed.extend(rels)
        for h in hits:
            h["vault_title"] = vtitle
            merged_hits.append(h)

    merged_hits.sort(key=lambda item: (-float(item.get("bm25_score", 0.0)), str(item.get("vault_ref", "")), str(item.get("note_ref", ""))))
    return merged_hits[: max(1, top_k)], sealed


def _audit_note_records(
    *,
    vault_dir: Path,
    rels: List[str],
    mapping: Dict[str, str],
    known_stems: Optional[set[str]] = None,
) -> Dict[str, Any]:
    schema_issues: List[Dict[str, Any]] = []
    unknown_field_issues: List[Dict[str, Any]] = []
    zone_mismatch_issues: List[Dict[str, Any]] = []
    broken_links: List[Dict[str, Any]] = []
    exempt_skipped_count = 0

    for rel in rels:
        nref = mapping[rel]
        txt = (vault_dir / rel).read_text(encoding="utf-8")
        exempt = is_exempt_rel_path(rel)
        try:
            meta, body, has_fm = extract_frontmatter(txt)
            if not has_fm:
                if exempt:
                    exempt_skipped_count += 1
                else:
                    schema_issues.append({"note_ref": nref, "zone": _zone_for_rel(rel), "errors": ["缺少 YAML Frontmatter"]})
                continue

            insp = inspect_note_against_dict(meta, rel_path=rel)
            dt = str(meta.get("doc_type") or "").strip()
            zone = _zone_for_rel(rel)
            if insp["errors"]:
                schema_issues.append({"note_ref": nref, "zone": zone, "doc_type": dt, "errors": insp["errors"]})
            if insp["unknown_fields"]:
                unknown_field_issues.append(
                    {"note_ref": nref, "zone": zone, "doc_type": dt, "unknown_fields": insp["unknown_fields"]}
                )
            if insp["zone_errors"]:
                zone_mismatch_issues.append(
                    {"note_ref": nref, "zone": zone, "doc_type": dt, "errors": insp["zone_errors"]}
                )
            if known_stems is not None:
                for target_stem in extract_wikilinks(body):
                    norm_target = target_stem[:-3] if target_stem.endswith(".md") else target_stem
                    if not resolve_wikilink(target_stem, rel, mapping):
                        broken_links.append({"note_ref": nref, "zone": zone, "missing_target_stem": target_stem})
        except FrontmatterError as exc:
            schema_issues.append({"note_ref": nref, "zone": _zone_for_rel(rel), "errors": [str(exc)]})

    return {
        "schema_issues": schema_issues,
        "unknown_field_issues": unknown_field_issues,
        "zone_mismatch_issues": zone_mismatch_issues,
        "broken_links": broken_links,
        "exempt_skipped_count": exempt_skipped_count,
    }


def health_check(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    strict: bool = False,
) -> Tuple[Dict[str, Any], List[str]]:
    rels = _scan_vault_markdowns(vault_dir)
    mapping = note_reg.sync_paths(vault_ref, rels)
    known_stems = (
        {Path(r).stem for r in rels}
        | {r[:-3] if r.endswith(".md") else r for r in rels}
        | set(rels)
        | {"Manifest-AI-Zone"}
    )
    dict_report = validate_dicts_integrity()
    audit = _audit_note_records(vault_dir=vault_dir, rels=rels, mapping=mapping, known_stems=known_stems)
    schema_issues = audit["schema_issues"]
    unknown_field_issues = audit["unknown_field_issues"]
    zone_mismatch_issues = audit["zone_mismatch_issues"]
    broken_links = audit["broken_links"]

    idx_dir = vault_dir / "10-Index"
    base_count = len(list(idx_dir.glob("*.base"))) if idx_dir.is_dir() else 0
    strict_clean = (
        dict_report["ok"]
        and not schema_issues
        and not broken_links
        and not unknown_field_issues
        and not zone_mismatch_issues
    )
    base_clean = dict_report["ok"] and not schema_issues and not broken_links
    report = {
        "vault_ref": vault_ref,
        "total_notes": len(rels),
        "obsidian_base_count": base_count,
        "dicts_healthy": dict_report["ok"],
        "dict_versions": dict_report["versions"],
        "dict_errors": dict_report["errors"],
        "schema_issue_count": len(schema_issues),
        "unknown_field_issue_count": len(unknown_field_issues),
        "zone_mismatch_issue_count": len(zone_mismatch_issues),
        "broken_wikilink_count": len(broken_links),
        "schema_issues": schema_issues,
        "unknown_field_issues": unknown_field_issues,
        "zone_mismatch_issues": zone_mismatch_issues,
        "broken_wikilinks": broken_links,
        "exempt_skipped_count": audit["exempt_skipped_count"],
        "strict": strict,
        "strict_clean": strict_clean,
        "healthy": strict_clean if strict else base_clean,
        "path_sealed": True,
    }
    return report, rels


def schema_check(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    note_ref: Optional[str] = None,
    strict: bool = True,
) -> Tuple[Dict[str, Any], List[str]]:
    rels = _scan_vault_markdowns(vault_dir)
    mapping = note_reg.sync_paths(vault_ref, rels)
    dict_report = validate_dicts_integrity()
    if note_ref:
        rec = note_reg.resolve_handle(vault_ref, note_ref)
        target_rels = [str(rec["note_rel_path"])]
    else:
        target_rels = rels

    audit = _audit_note_records(vault_dir=vault_dir, rels=target_rels, mapping=mapping, known_stems=None)
    schema_issues = audit["schema_issues"]
    unknown_field_issues = audit["unknown_field_issues"]
    zone_mismatch_issues = audit["zone_mismatch_issues"]
    strict_clean = (
        dict_report["ok"]
        and not schema_issues
        and not unknown_field_issues
        and not zone_mismatch_issues
    )
    base_clean = dict_report["ok"] and not schema_issues
    report = {
        "vault_ref": vault_ref,
        "note_ref": note_ref,
        "checked_notes": len(target_rels),
        "strict": strict,
        "dicts_healthy": dict_report["ok"],
        "dict_versions": dict_report["versions"],
        "dict_errors": dict_report["errors"],
        "exempt_skipped_count": audit["exempt_skipped_count"],
        "schema_issue_count": len(schema_issues),
        "unknown_field_issue_count": len(unknown_field_issues),
        "zone_mismatch_issue_count": len(zone_mismatch_issues),
        "schema_issues": schema_issues,
        "unknown_field_issues": unknown_field_issues,
        "zone_mismatch_issues": zone_mismatch_issues,
        "strict_clean": strict_clean,
        "clean": strict_clean if strict else base_clean,
        "path_sealed": True,
    }
    return report, rels


def schema_list(*, doc_type: Optional[str] = None) -> Dict[str, Any]:
    dict_report = validate_dicts_integrity()
    meta_dict = load_metadata_dict()
    layout_dict = load_layout_dict()
    meth_dict = load_methodology_dict()
    schemas = meta_dict.get("schemas") or {}
    vocabs = meta_dict.get("vocabularies") or {}

    dt_filter = (doc_type or "").strip().lower()
    if dt_filter:
        if dt_filter not in schemas:
            raise VaultStoreError("E_PARAM_INVALID", f"未知 doc_type schema: {dt_filter}", exit_code=1)
        selected_schemas = {dt_filter: schemas[dt_filter]}
    else:
        selected_schemas = schemas

    return {
        "dicts_healthy": dict_report["ok"],
        "dict_versions": {
            "layout": str(layout_dict.get("version") or ""),
            "metadata": str(meta_dict.get("version") or ""),
            "methodology": str(meth_dict.get("version") or ""),
        },
        "vocabularies": {
            k: list((v.get("allowed_values") or {}).keys()) if isinstance(v, dict) else []
            for k, v in vocabs.items()
        },
        "layout": {
            "top_level": {
                k: v
                for k, v in (layout_dict.get("top_level") or {}).items()
                if k != "trash" and not str(v).startswith(".")
            },
            "domain_skeleton": layout_dict.get("domain_skeleton") or {},
            "exempt_zones": layout_dict.get("exempt_zones") or [],
            "formal_cabins": layout_dict.get("formal_cabins") or [],
            "lazy_dirs": layout_dict.get("lazy_dirs") or [],
        },
        "doc_type_filter": dt_filter or None,
        "schemas": selected_schemas,
        "path_sealed": True,
    }


def _first_meaningful_sentence(text: str, fallback: str) -> str:
    norm_fb = re.sub(r"\s+", " ", (fallback or "").strip())
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln or ln.startswith(("#", "```", "~~~", "---", ">", "|")):
            continue
        clean = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", lambda m: m.group(2) or m.group(1), ln)
        clean = re.sub(r"[*_`]+", "", clean).strip("-• ").strip()
        if len(clean) >= 4 and re.sub(r"\s+", " ", clean) != norm_fb:
            return clean[:120]
    return fallback


def _split_markdown_sections(body: str, fallback_title: str, level: int = 2) -> List[Tuple[str, str]]:
    """Fence-aware Markdown section splitter producing [(section_title, section_body), ...]."""
    from notes.atlas_markdown_blocks import code_line_mask

    atx_re = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*$")
    lines = body.splitlines()
    headings: List[Tuple[int, int, str]] = []
    for idx, (line, is_code) in enumerate(zip(lines, code_line_mask(body))):
        if is_code:
            continue
        hm = atx_re.match(line)
        if hm:
            headings.append((idx, len(hm.group(1)), hm.group(2).strip()))

    split_points = [h for h in headings if h[1] <= max(1, min(5, level))]
    if not split_points and headings:
        deepest = max(h[1] for h in headings)
        split_points = [h for h in headings if h[1] <= deepest]
    if not split_points:
        clean_body = body.strip("\r\n")
        return [(fallback_title, clean_body if clean_body else f"# {fallback_title}")]

    sections: List[Tuple[str, str]] = []
    first_idx = split_points[0][0]
    preamble = "\n".join(lines[:first_idx]).strip("\r\n")
    if preamble:
        sections.append((f"{fallback_title}-导言", preamble))

    for pos, (start_idx, _lvl, h_title) in enumerate(split_points):
        end_idx = split_points[pos + 1][0] if pos + 1 < len(split_points) else len(lines)
        chunk = "\n".join(lines[start_idx:end_idx]).strip("\r\n")
        if chunk:
            sections.append((h_title, chunk))
    return sections or [(fallback_title, body.strip("\r\n") or f"# {fallback_title}")]


def note_slice(
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
    level: int = 2,
    adaptive: bool = False,
    min_chars: Optional[int] = None,
    max_chars: Optional[int] = None,
    target_chars: Optional[int] = None,
    prefix: Optional[str] = None,
    idempotent: bool = False,
) -> Tuple[Dict[str, Any], List[str]]:
    if adaptive or min_chars is not None or max_chars is not None or target_chars is not None or (prefix and prefix.strip()):
        from pipeline.atlas_slicer_engine import note_slice_adaptive

        return note_slice_adaptive(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            note_reg=note_reg,
            proj_reg=proj_reg,
            app_id=app_id,
            note_ref=note_ref,
            project_ref=project_ref,
            scope=scope,
            min_chars=int(min_chars) if min_chars is not None else 300,
            max_chars=int(max_chars) if max_chars is not None else 4000,
            custom_prefix=prefix,
            target_chars=int(target_chars) if target_chars is not None else None,
            idempotent=idempotent,
        )
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
    if _zone_for_rel(src_rel) == "attic" or str(meta.get("status") or "").strip().lower() in ("deprecated", "archived"):
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
        raise VaultStoreError("E_PARAM_CONFLICT", f"目标切片目录已存在切片（避免覆盖已有切片）", exit_code=1)

    sections = _split_markdown_sections(body, doc_title, level=level)
    slice_status = "active" if dom["default_mode"] == "auto" else "pending"
    target_pid = str(dom["project_id"])
    created_rels: List[str] = []
    slice_meta_rows: List[Tuple[str, str, str, str]] = []

    for idx, (sec_title, sec_body) in enumerate(sections, start=1):
        slice_id = f"slice_{idx:02d}"
        sec_slug = _slugify_filename(sec_title)
        fname = f"{idx:02d}-{sec_slug}.md"
        s_rel = validate_note_rel_path(f"{cluster_dir.relative_to(vault_dir).as_posix()}/{fname}")
        summary = _first_meaningful_sentence(sec_body, sec_title)
        slice_md = (
            "---\n"
            "doc_type: slice\n"
            f'title: "{fm_scalar(sec_title)}"\n'
            f'summary: "{fm_scalar(summary)}"\n'
            f"status: {slice_status}\n"
            f'slice_id: "{slice_id}"\n'
            f'source_doc: "[[{fm_scalar(doc_stem)}]]"\n'
            f'project_id: "{fm_scalar(target_pid)}"\n'
            'tags: ["切片"]\n'
            f"updated: {_today()}\n"
            "---\n\n"
            f"{sec_body}\n"
        )
        _atomic_write_md(vault_dir / s_rel, slice_md)
        created_rels.append(s_rel)
        slice_meta_rows.append((slice_id, Path(fname).stem, sec_title, summary))

    toc_title = f"TOC-{doc_stem}"
    toc_rel = validate_note_rel_path(f"{cluster_dir.relative_to(vault_dir).as_posix()}/{toc_title}.md")
    toc_lines = [
        "---",
        "doc_type: toc",
        f'title: "{fm_scalar(toc_title)}"',
        f'summary: "{fm_scalar(doc_title)} 章节切片骨架（共 {len(slice_meta_rows)} 片）"',
        f"status: {slice_status}",
        f'source_doc: "[[{fm_scalar(doc_stem)}]]"',
        f'project_id: "{fm_scalar(target_pid)}"',
        f"total_slices: {len(slice_meta_rows)}",
        'tags: ["切片", "TOC"]',
        f"updated: {_today()}",
        "---",
        "",
        f"# {doc_title} · 切片目录骨架",
        "",
        f"> 原文档：[[{doc_stem}]]",
        "",
        "## 切片索引",
        "",
        *[f"- `{sid}` [[{stem}|{stitle}]] — {ssum}" for sid, stem, stitle, ssum in slice_meta_rows],
        "",
    ]
    _atomic_write_md(vault_dir / toc_rel, "\n".join(toc_lines))
    created_rels.append(toc_rel)

    source_processed = False
    src_dt = str(meta.get("doc_type") or "").strip().lower()
    if has_fm and src_dt in ("library", "artifact"):
        if meta.get("processed") is not True:
            updated_src = replace_frontmatter_field(txt, "processed", "true")
            _atomic_write_md(src_path, updated_src)
        source_processed = True

    all_md = _scan_vault_markdowns(vault_dir)
    mapping = note_reg.sync_paths(vault_ref, all_md)
    slice_refs = [
        {"note_ref": mapping[srel], "slice_id": sid, "title": stitle}
        for srel, (sid, _stem, stitle, _ssum) in zip(created_rels[:-1], slice_meta_rows)
    ]
    res: Dict[str, Any] = {
        "vault_ref": vault_ref,
        "source_note_ref": note_ref,
        "source_processed": source_processed,
        "domain_scope": dom["scope"],
        "project_ref": dom.get("project_ref"),
        "project_id": target_pid,
        "toc_note_ref": mapping[toc_rel],
        "slice_count": len(slice_refs),
        "slices": slice_refs,
        "path_sealed": True,
    }
    sealed = [src_rel, *created_rels, *all_md]
    if dom.get("project_rel_path"):
        sealed.append(str(dom["project_rel_path"]))
    return res, sealed


_FILE_READ_ALLOWED_ZONES = (
    "workbench",
    "dropzone",
    "library",
    "atomics",
    "moc",
    "slices",
    "artifacts",
    "slip",
    "journal",
)


def _safe_rel_segments(raw: str, label: str) -> List[str]:
    clean = str(raw or "").strip()
    if not clean:
        return []
    if "\x00" in clean or "\\" in clean or clean.startswith("/") or clean.endswith("/"):
        raise VaultStoreError("E_PARAM_INVALID", f"{label} 包含非法路径字符", exit_code=1)
    parts = [p for p in clean.split("/") if p]
    for seg in parts:
        if seg in (".", "..") or seg.startswith("."):
            raise VaultStoreError("E_SECURITY_VIOLATION", f"{label} 禁止穿越或访问隐藏目录/文件", exit_code=2)
    return parts


def _assert_no_symlink_in_chain(root: Path, target: Path) -> Path:
    cur = root
    for part in target.relative_to(root).parts:
        cur = cur / part
        if cur.is_symlink():
            raise VaultStoreError("E_SECURITY_VIOLATION", "拒绝访问符号链接路径", exit_code=2)
    resolved = target.resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and not resolved.is_relative_to(root_resolved):
        raise VaultStoreError("E_SECURITY_VIOLATION", "拒绝越界路径访问", exit_code=2)
    return resolved


def file_read(
    *,
    vault_dir: Path,
    vault_ref: str,
    proj_reg: KekProjectRegistry,
    project_ref: Optional[str] = None,
    scope: str = "auto",
    zone: str = "workbench",
    subdir: Optional[str] = None,
    file_name: Optional[str] = None,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    head: Optional[int] = None,
    tail: Optional[int] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """List files or read a complete UTF-8 file inside an allowed logical zone without exposing host paths."""
    norm_zone = (zone or "workbench").strip().lower()
    if norm_zone not in _FILE_READ_ALLOWED_ZONES:
        raise VaultStoreError(
            "E_PARAM_INVALID",
            f"file-read --zone 仅支持: {', '.join(_FILE_READ_ALLOWED_ZONES)}",
            exit_code=1,
        )
    eff_scope = "project" if (project_ref and (scope or "auto").strip().lower() == "auto") else scope
    dom = resolve_domain_scope(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        proj_reg=proj_reg,
        explicit_project_ref=project_ref,
        scope=eff_scope,
    )
    base_dir = resolve_zone_dir(vault_dir, dom, norm_zone)
    if base_dir.is_symlink():
        raise VaultStoreError("E_SECURITY_VIOLATION", "目标分区为非法符号链接", exit_code=2)
    _assert_no_symlink_in_chain(vault_dir, base_dir)

    sub_parts = _safe_rel_segments(subdir or "", "--subdir")
    clean_subdir = "/".join(sub_parts) if sub_parts else None
    scan_root = base_dir
    for seg in sub_parts:
        scan_root = scan_root / seg
    if sub_parts:
        _assert_no_symlink_in_chain(base_dir, scan_root)

    fname_raw = str(file_name or "").strip()
    if not fname_raw:
        if start_line is not None or end_line is not None or head is not None or tail is not None:
            raise VaultStoreError("E_PARAM_INVALID", "--start_line / --end_line / --head / --tail 需要同时指定 --file_name", exit_code=1)
        items: List[Dict[str, Any]] = []
        if scan_root.exists():
            if not scan_root.is_dir():
                raise VaultStoreError("E_NOT_FOUND", "目标子目录不存在或不是目录", exit_code=1)
            for p in sorted(scan_root.rglob("*")):
                rel_parts = p.relative_to(base_dir).parts
                if any(part.startswith(".") for part in rel_parts):
                    continue
                if p.is_symlink():
                    continue
                if p.is_file():
                    items.append(
                        {
                            "file_name": p.relative_to(base_dir).as_posix(),
                            "ext": p.suffix.lower(),
                            "size_bytes": p.stat().st_size,
                            "is_markdown": p.suffix.lower() == ".md",
                        }
                    )
        return {
            "vault_ref": vault_ref,
            "mode": "list",
            "zone": norm_zone,
            "domain_scope": dom["scope"],
            "project_ref": dom.get("project_ref"),
            "project_id": dom.get("project_id"),
            "subdir": clean_subdir,
            "count": len(items),
            "files": items,
            "path_sealed": True,
        }, []

    file_parts = _safe_rel_segments(fname_raw, "--file_name")
    target = scan_root
    for seg in file_parts:
        target = target / seg
    _assert_no_symlink_in_chain(base_dir, target)
    rel_to_zone = target.relative_to(base_dir).as_posix()
    if not target.exists() or not target.is_file():
        raise VaultStoreError("E_NOT_FOUND", f"受控文件不存在: {rel_to_zone}", exit_code=1)

    raw_bytes = target.read_bytes()
    if b"\x00" in raw_bytes:
        raise VaultStoreError(
            "E_BINARY_NOT_SUPPORTED",
            f"目标文件「{rel_to_zone}」为二进制文件，不可作为文本直读（请用 fmt 转译或 inbox-list 查看摘要）",
            exit_code=1,
        )
    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VaultStoreError(
            "E_BINARY_NOT_SUPPORTED",
            f"目标文件「{rel_to_zone}」不是有效 UTF-8 文本，不可直读",
            exit_code=1,
        ) from exc

    sliced = _slice_text_lines(raw_text, start_line=start_line, end_line=end_line, head=head, tail=tail)
    sealed_rel = target.relative_to(vault_dir).as_posix()
    res: Dict[str, Any] = {
        "vault_ref": vault_ref,
        "mode": "read",
        "zone": norm_zone,
        "domain_scope": dom["scope"],
        "project_ref": dom.get("project_ref"),
        "project_id": dom.get("project_id"),
        "subdir": clean_subdir,
        "file_name": rel_to_zone,
        "ext": target.suffix.lower(),
        "size_bytes": len(raw_bytes),
        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        **sliced,
        "path_sealed": True,
    }
    return res, [sealed_rel]


_MANAGED_BLOCK_SPECS: Tuple[Tuple[str, str, str], ...] = (
    ("moc-index", "<!-- atlas:moc-index:start -->", "<!-- atlas:moc-index:end -->"),
    ("workbench-preset", "<!-- atlas:workbench-preset:begin -->", "<!-- atlas:workbench-preset:end -->"),
    ("manifest-ledger", "<!-- atlas:manifest-ledger:begin -->", "<!-- atlas:manifest-ledger:end -->"),
    ("manifest-history", "<!-- atlas:manifest-history:begin -->", "<!-- atlas:manifest-history:end -->"),
)


def _extract_managed_blocks(body: str) -> List[Tuple[str, str]]:
    found: List[Tuple[str, str]] = []
    for name, start_tok, end_tok in _MANAGED_BLOCK_SPECS:
        idx = body.find(start_tok)
        if idx < 0:
            continue
        end_idx = body.find(end_tok, idx + len(start_tok))
        if end_idx < 0:
            continue
        found.append((name, body[idx : end_idx + len(end_tok)]))
    return found


def _list_body_headings(body: str, limit: int = 12) -> List[str]:
    from notes.atlas_markdown_blocks import code_line_mask

    lines = body.splitlines()
    mask = code_line_mask(body)
    out: List[str] = []
    for line, is_code in zip(lines, mask):
        if is_code:
            continue
        m = _ATX_HEADING_RE.match(line)
        if m:
            out.append(f"{m.group(1)} {m.group(2).strip()}")
            if len(out) >= limit:
                break
    return out


def _replace_section_body(body: str, section_query: str, new_section_content: str) -> str:
    """Replace the targeted Markdown heading section (preserving surrounding sections)."""
    from notes.atlas_markdown_blocks import code_line_mask

    raw_q = section_query.strip()
    if not raw_q:
        raise VaultStoreError("E_PARAM_MISSING", "--section 不能为空", exit_code=1)
    q_clean = re.sub(r"^#{1,6}\s+", "", raw_q).strip().casefold()
    lines = body.splitlines()
    mask = code_line_mask(body)
    headings: List[Tuple[int, int, str, str]] = []
    for idx, (line, is_code) in enumerate(zip(lines, mask)):
        if is_code:
            continue
        m = _ATX_HEADING_RE.match(line)
        if m:
            headings.append((idx, len(m.group(1)), m.group(2).strip(), line))
    match_pos: Optional[int] = None
    for pos, (_li, _lvl, htxt, _raw_line) in enumerate(headings):
        if htxt.casefold() == q_clean:
            match_pos = pos
            break
    if match_pos is None:
        for pos, (_li, _lvl, htxt, _raw_line) in enumerate(headings):
            if q_clean in htxt.casefold():
                match_pos = pos
                break
    if match_pos is None:
        avail = [f"{'#' * lvl} {htxt}" for _li, lvl, htxt, _raw in headings[:12]]
        hint = f"；当前可用标题: {', '.join(avail)}" if avail else "；当前正文无 Markdown 标题"
        raise VaultStoreError("E_PATCH_NOT_FOUND", f"未找到匹配章节标题: {section_query}{hint}", exit_code=1)

    start_idx, start_lvl, _htxt, orig_heading_line = headings[match_pos]
    end_idx = len(lines)
    for next_idx, next_lvl, _ntxt, _nraw in headings[match_pos + 1 :]:
        if next_lvl <= start_lvl:
            end_idx = next_idx
            break

    rep = new_section_content.strip("\r\n")
    rep_lines = rep.splitlines() if rep else []
    if rep_lines and _ATX_HEADING_RE.match(rep_lines[0]):
        replacement_block = rep_lines
    else:
        replacement_block = [orig_heading_line, "", *rep_lines] if rep_lines else [orig_heading_line]

    # Keep clean blank-line separation before the following heading if any
    tail_lines = lines[end_idx:]
    if tail_lines and replacement_block and replacement_block[-1].strip() != "":
        replacement_block = [*replacement_block, ""]

    merged = [*lines[:start_idx], *replacement_block, *tail_lines]
    return "\n".join(merged)


def _apply_patch_op(body: str, spec: Dict[str, Any]) -> Tuple[str, str]:
    op = str(spec.get("op") or "").strip().lower()
    if op == "replace":
        old_text = spec.get("old_text")
        new_text = spec.get("new_text")
        if old_text is None or str(old_text) == "":
            raise VaultStoreError("E_PARAM_MISSING", "replace 补丁缺少非空 old_text", exit_code=1)
        old_s = str(old_text)
        new_s = "" if new_text is None else str(new_text)
        replace_all = bool(spec.get("replace_all", False))
        cnt = body.count(old_s)
        if cnt == 0:
            avail = _list_body_headings(body, limit=10)
            hint = f"（当前标题大纲: {', '.join(avail)}）" if avail else ""
            raise VaultStoreError("E_PATCH_NOT_FOUND", f"未在笔记正文中找到匹配的 old_text，请核对最新正文后重试{hint}", exit_code=1)
        if cnt > 1 and not replace_all:
            raise VaultStoreError(
                "E_PATCH_AMBIGUOUS",
                f"old_text 在正文中命中 {cnt} 处，请提供更长上下文片段或显式传 --replace_all true",
                exit_code=1,
            )
        if replace_all:
            return body.replace(old_s, new_s), f"replace_all({cnt})"
        return body.replace(old_s, new_s, 1), "replace(1)"
    if op == "section":
        sec = str(spec.get("section") or "").strip()
        if not sec:
            raise VaultStoreError("E_PARAM_MISSING", "section 补丁缺少 --section 标题", exit_code=1)
        new_content = "" if spec.get("content") is None else str(spec.get("content"))
        return _replace_section_body(body, sec, new_content), f"section({sec})"
    if op == "append":
        add = "" if spec.get("content") is None else str(spec.get("content"))
        if not add.strip():
            raise VaultStoreError("E_PARAM_MISSING", "append 补丁缺少非空 --content", exit_code=1)
        base = body.rstrip()
        return (f"{base}\n\n{add.lstrip()}" if base else add.lstrip()), "append"
    if op == "prepend":
        add = "" if spec.get("content") is None else str(spec.get("content"))
        if not add.strip():
            raise VaultStoreError("E_PARAM_MISSING", "prepend 补丁缺少非空 --content", exit_code=1)
        base = body.lstrip()
        return (f"{add.rstrip()}\n\n{base}" if base else add.rstrip()), "prepend"
    raise VaultStoreError("E_PARAM_INVALID", f"不支持的补丁操作 op={op!r}（仅支持 replace/section/append/prepend）", exit_code=1)


def note_patch(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_ref: str,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    old_text: Optional[str] = None,
    new_text: Optional[str] = None,
    replace_all: bool = False,
    section: Optional[str] = None,
    mode: Optional[str] = None,
    content: Optional[str] = None,
    ops_json: Any = None,
    allow_managed_block: bool = False,
    title: Optional[str] = None,
    summary: Optional[str] = None,
    tags: Any = None,
    status: Optional[str] = None,
    set_meta: Any = None,
    expected_sha256: Optional[str] = None,
    dry_run: bool = False,
    cache_dir: Optional[Path] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Apply targeted incremental edits (replace/section/append/prepend/ops_json) to a note atomically."""
    import json as _json
    from storage.note_index_cache import resolve_note_jit, update_single_note_index

    rel, fpath = resolve_note_jit(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        note_ref=note_ref,
        note_reg=note_reg,
        kek=kek,
        app_id=app_id,
        cache_dir=cache_dir,
    )
    zone = _zone_for_rel(rel)
    if zone == "library":
        raise VaultStoreError(
            "E_READONLY_LIBRARY",
            "21-Library 原始文献正文只读保真，禁止通过 note-patch 改写正文；请在切片/原子卡或工作台草稿区派生",
            exit_code=1,
        )

    raw_bytes = fpath.read_bytes()
    pre_sha = hashlib.sha256(raw_bytes).hexdigest()
    if expected_sha256 and pre_sha != expected_sha256:
        raise VaultStoreError("E_VERSION_CONFLICT", f"笔记 {note_ref} 内容哈希已变化，请重新读取后再补丁更新", exit_code=1)

    old_txt = raw_bytes.decode("utf-8")
    _meta, body, _has_fm = extract_frontmatter(old_txt)
    is_journal = rel.startswith("05-Journal/")
    human_journal_before = _extract_section_body(body, "人类记录") if is_journal else None

    ops: List[Dict[str, Any]] = []
    if ops_json is not None and str(ops_json).strip():
        if isinstance(ops_json, list):
            raw_ops = ops_json
        else:
            try:
                raw_ops = _json.loads(str(ops_json))
            except Exception as exc:
                raise VaultStoreError("E_PARAM_INVALID", f"--ops_json JSON 解析失败: {exc}", exit_code=1) from exc
        if not isinstance(raw_ops, list) or not raw_ops:
            raise VaultStoreError("E_PARAM_INVALID", "--ops_json 必须为非空 JSON 数组", exit_code=1)
        for item in raw_ops:
            if not isinstance(item, dict):
                raise VaultStoreError("E_PARAM_INVALID", "--ops_json 每项必须为 JSON 对象", exit_code=1)
            ops.append(item)
    elif old_text is not None:
        ops.append({"op": "replace", "old_text": old_text, "new_text": new_text, "replace_all": bool(replace_all)})
    elif section is not None and str(section).strip():
        ops.append({"op": "section", "section": str(section).strip(), "content": content})
    elif mode is not None and str(mode).strip().lower() in ("append", "prepend"):
        ops.append({"op": str(mode).strip().lower(), "content": content})
    else:
        raise VaultStoreError(
            "E_PARAM_MISSING",
            "note-patch 需要提供 --old_text/--new_text、--section/--content、--mode append|prepend 或 --ops_json",
            exit_code=1,
        )

    managed_before = _extract_managed_blocks(body)
    cur_body = body
    applied_labels: List[str] = []
    for spec in ops:
        cur_body, label = _apply_patch_op(cur_body, spec)
        applied_labels.append(label)

    if is_journal and human_journal_before is not None:
        human_journal_after = _extract_section_body(cur_body, "人类记录")
        if human_journal_after != human_journal_before:
            raise VaultStoreError(
                "E_JOURNAL_PROTECTED",
                "日记（05-Journal）# 人类记录 区受永久保护，严禁通过 note-patch 修改",
                exit_code=1,
            )

    if not allow_managed_block and managed_before:
        for bname, block_exact in managed_before:
            if block_exact not in cur_body:
                raise VaultStoreError(
                    "E_MANAGED_BLOCK_PROTECTED",
                    f"补丁触碰或破坏了系统受控块（{bname}），已自动拦截；如需显式覆盖请传 --allow_managed_block true",
                    exit_code=1,
                )

    fpath, rel, rendered, nmeta, rewrites, _, _, latest_sha = _prepare_note_update(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        note_ref=note_ref,
        note_reg=note_reg,
        title=title,
        summary=summary,
        tags=tags,
        status=status,
        content=cur_body,
        set_meta=set_meta,
        expected_sha256=expected_sha256 or pre_sha,
        allow_managed_block=allow_managed_block,
        op_name="note-patch",
    )
    if dry_run:
        pub = _public_note(kek=kek, app_id=app_id, vault_ref=vault_ref, note_ref=note_ref, rel=rel, meta=nmeta)
        pub["dry_run"] = True
        pub["changed"] = (rendered.encode("utf-8") != raw_bytes)
        pub["patch_ops_applied"] = applied_labels
        pub["chars_before"] = len(body)
        pub["chars_after"] = len(cur_body)
        if rewrites:
            pub["links_resolved_count"] = len(rewrites)
        return pub, [rel]

    _atomic_write_md(fpath, rendered, expected_sha256=expected_sha256 or latest_sha)
    if cache_dir is not None:
        update_single_note_index(vault_dir=vault_dir, vault_ref=vault_ref, rel_paths=[rel], kek=kek, app_id=app_id, cache_dir=cache_dir)

    new_bytes = fpath.read_bytes()
    pub = _public_note(kek=kek, app_id=app_id, vault_ref=vault_ref, note_ref=note_ref, rel=rel, meta=nmeta)
    pub["content_sha256"] = hashlib.sha256(new_bytes).hexdigest()
    pub["patch_ops_applied"] = applied_labels
    pub["chars_before"] = len(body)
    pub["chars_after"] = len(cur_body)
    if rewrites:
        pub["links_resolved_count"] = len(rewrites)
        pub["resolved_links"] = _sanitize_resolved_links(rewrites, limit=20)
    return pub, [rel]


def note_patch_batch(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_refs: Any,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    old_text: Optional[str] = None,
    new_text: Optional[str] = None,
    replace_all: bool = False,
    section: Optional[str] = None,
    mode: Optional[str] = None,
    content: Optional[str] = None,
    ops_json: Any = None,
    allow_managed_block: bool = False,
    title: Optional[str] = None,
    summary: Optional[str] = None,
    tags: Any = None,
    status: Optional[str] = None,
    set_meta: Any = None,
    dry_run: bool = False,
    cache_dir: Optional[Path] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Apply the same incremental patch across multiple notes safely (skipping non-matching notes while enforcing all zone/block guards)."""
    refs = list(dict.fromkeys(_parse_list_arg(note_refs)))
    if not refs:
        raise VaultStoreError("E_PARAM_MISSING", "批量 note-patch 需要非空 --note_refs", exit_code=1)
    if len(refs) > 50:
        raise VaultStoreError("E_PARAM_INVALID", "单次跨卡 note-patch 最多支持 50 篇笔记", exit_code=1)

    results: List[Dict[str, Any]] = []
    sealed_all: List[str] = []
    patched = 0
    skipped = 0
    failed = 0
    for nref in refs:
        try:
            pub, npaths = note_patch(
                vault_dir=vault_dir,
                vault_ref=vault_ref,
                note_ref=nref,
                note_reg=note_reg,
                kek=kek,
                app_id=app_id,
                old_text=old_text,
                new_text=new_text,
                replace_all=replace_all,
                section=section,
                mode=mode,
                content=content,
                ops_json=ops_json,
                allow_managed_block=allow_managed_block,
                title=title,
                summary=summary,
                tags=tags,
                status=status,
                set_meta=set_meta,
                dry_run=dry_run,
                cache_dir=cache_dir,
            )
            sealed_all.extend(npaths)
            patched += 1
            results.append(
                {
                    "note_ref": nref,
                    "status": "would_patch" if dry_run else "patched",
                    "title": pub.get("title"),
                    "zone": pub.get("zone"),
                    "patch_ops_applied": pub.get("patch_ops_applied"),
                    "chars_before": pub.get("chars_before"),
                    "chars_after": pub.get("chars_after"),
                }
            )
        except VaultStoreError as exc:
            if exc.code == "E_PATCH_NOT_FOUND":
                skipped += 1
                results.append({"note_ref": nref, "status": "skipped", "code": exc.code, "reason": exc.message})
            else:
                failed += 1
                results.append({"note_ref": nref, "status": "failed", "code": exc.code, "reason": exc.message})
    return {
        "vault_ref": vault_ref,
        "dry_run": bool(dry_run),
        "total": len(refs),
        "patched": patched,
        "skipped": skipped,
        "failed": failed,
        "results": results,
        "path_sealed": True,
    }, sealed_all


def note_grep(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    pattern: str,
    regex: bool = False,
    case_sensitive: bool = False,
    context_lines: int = 1,
    max_matches: int = 50,
    zone: Optional[str] = None,
    project_ref: Optional[str] = None,
    proj_reg: Any = None,
    doc_type: Optional[str] = None,
    private: bool = False,
    include_archived: bool = False,
    include_workbench: bool = False,
    with_section: bool = False,
    cache_dir: Optional[Path] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Controlled cross-note literal/regex search returning opaque handles + line context (zero path leak)."""
    from storage.note_index_cache import reconcile_note_index

    pat_raw = str(pattern or "")
    if not pat_raw:
        raise VaultStoreError("E_PARAM_MISSING", "note-grep 需要提供非空 --pattern", exit_code=1)
    if regex:
        if len(pat_raw) > 256:
            raise VaultStoreError("E_PARAM_INVALID", "正则表达式长度不能超过 256 字符", exit_code=1)
        if re.search(r"\([^()]*[+*][^()]*\)\s*(?:[+*?]|\{\d)", pat_raw):
            raise VaultStoreError("E_PARAM_INVALID", "拒绝存在灾难性回溯（ReDoS）风险的嵌套量词正则表达式", exit_code=1)
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        rx = re.compile(pat_raw if regex else re.escape(pat_raw), flags)
    except re.error as exc:
        raise VaultStoreError("E_PARAM_INVALID", f"无效正则表达式: {exc}", exit_code=1) from exc

    ctx_n = max(0, min(5, int(context_lines)))
    max_m = max(1, min(500, int(max_matches)))
    norm_zone = str(zone or "").strip().lower() or None
    norm_dt = str(doc_type or "").strip().lower() or None
    proj_prefix: Optional[str] = None
    if project_ref and proj_reg is not None:
        prec = proj_reg.resolve_handle(vault_ref, str(project_ref).strip())
        prel = str(prec.get("project_rel_path") or "").strip("/")
        if prel:
            proj_prefix = prel + "/"

    private_projects: set[str] = set()
    pdir = vault_dir / "30-Projects"
    if pdir.is_dir():
        for pd in pdir.iterdir():
            if pd.is_dir() and not pd.name.startswith("."):
                m = read_domain_manifest(pd, pd.name)
                if m.get("private") is True:
                    private_projects.add(pd.name)

    entries, mapping = reconcile_note_index(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        note_reg=note_reg,
        kek=kek,
        app_id=app_id,
        cache_dir=cache_dir,
    )

    results: List[Dict[str, Any]] = []
    sealed_rels: List[str] = []
    total_matches = 0
    truncated = False
    allow_wb = bool(include_workbench) or (norm_zone == "workbench") or (proj_prefix is not None and norm_zone is None)

    for rel in sorted(entries.keys()):
        if proj_prefix and not rel.startswith(proj_prefix):
            continue
        ent = entries[rel]
        z = _zone_for_rel(rel)
        if norm_zone and z != norm_zone:
            continue
        if z == "workbench" and not allow_wb:
            continue
        if z in ("attic", "archives") and not include_archived and norm_zone != z:
            continue
        dt = str(ent.get("doc_type") or "").strip().lower()
        if norm_dt and dt != norm_dt:
            continue
        st = str(ent.get("status") or "").strip().lower()
        if st in ("archived", "deprecated") and not include_archived:
            continue
        if not private:
            if bool(ent.get("private", False)) or z == "journal" or rel.startswith("05-Journal/"):
                continue
            parts = Path(rel).parts
            if len(parts) >= 2 and parts[0] == "30-Projects" and parts[1] in private_projects:
                continue

        fp = vault_dir / rel
        if not fp.is_file() or fp.is_symlink():
            continue
        try:
            txt = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not rx.search(txt):
            continue

        nref = mapping.get(rel)
        if not nref:
            continue
        lines = txt.splitlines()
        note_headings: List[Tuple[int, int, str]] = []
        if with_section:
            from notes.atlas_markdown_blocks import code_line_mask

            lmask = code_line_mask(txt)
            for l_idx, (l_txt, l_code) in enumerate(zip(lines, lmask), start=1):
                if l_code:
                    continue
                hm = _ATX_HEADING_RE.match(l_txt)
                if hm:
                    note_headings.append((l_idx, len(hm.group(1)), f"{hm.group(1)} {hm.group(2).strip()}"))
        note_matches: List[Dict[str, Any]] = []
        seen_sections: set[int] = set()
        for idx, line in enumerate(lines, start=1):
            if rx.search(line):
                before = lines[max(0, idx - 1 - ctx_n) : idx - 1] if ctx_n > 0 else []
                after = lines[idx : min(len(lines), idx + ctx_n)] if ctx_n > 0 else []
                m_item: Dict[str, Any] = {
                    "line_no": idx,
                    "line": line[:300],
                    "before": [b[:240] for b in before],
                    "after": [a[:240] for a in after],
                }
                if with_section and note_headings:
                    enclosing_pos = -1
                    for hpos, (h_ln, _h_lvl, _h_lbl) in enumerate(note_headings):
                        if h_ln <= idx:
                            enclosing_pos = hpos
                        else:
                            break
                    if enclosing_pos >= 0:
                        s_ln, s_lvl, s_lbl = note_headings[enclosing_pos]
                        e_ln = len(lines)
                        for nh_ln, nh_lvl, _ in note_headings[enclosing_pos + 1 :]:
                            if nh_lvl <= s_lvl:
                                e_ln = nh_ln - 1
                                break
                        m_item["section_heading"] = s_lbl
                        m_item["section_start_line"] = s_ln
                        m_item["section_end_line"] = e_ln
                        if s_ln not in seen_sections:
                            seen_sections.add(s_ln)
                            sec_txt = "\n".join(lines[s_ln - 1 : e_ln])
                            m_item["section_content"] = sec_txt[:1500]
                            if len(sec_txt) > 1500:
                                m_item["section_truncated"] = True
                note_matches.append(m_item)
                total_matches += 1
                if total_matches >= max_m:
                    truncated = True
                    break
        if note_matches:
            sealed_rels.append(rel)
            results.append(
                {
                    "note_ref": nref,
                    "title": str(ent.get("title") or Path(rel).stem),
                    "zone": z,
                    "doc_type": dt,
                    "status": st,
                    "match_count": len(note_matches),
                    "matches": note_matches,
                }
            )
        if truncated:
            break

    return {
        "vault_ref": vault_ref,
        "pattern": pat_raw,
        "regex": bool(regex),
        "project_ref": project_ref,
        "zone": norm_zone,
        "matched_notes": len(results),
        "total_matches": total_matches,
        "truncated": truncated,
        "results": results,
        "path_sealed": True,
    }, sealed_rels


def project_brief(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    proj_reg: Any,
    kek: bytes,
    app_id: str,
    project_ref: Optional[str] = None,
    include_slices: bool = False,
    max_notes: int = 200,
    cache_dir: Optional[Path] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Single-call panoramic overview (counts, handles, metadata, link topology) for a project or vault."""
    from storage.note_index_cache import build_backlink_map, reconcile_note_index

    entries, mapping = reconcile_note_index(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        note_reg=note_reg,
        kek=kek,
        app_id=app_id,
        cache_dir=cache_dir,
    )
    bl_map = build_backlink_map(entries=entries, handle_map=mapping)

    proj_prefix: Optional[str] = None
    proj_meta: Optional[Dict[str, Any]] = None
    sealed_rels: List[str] = []
    if project_ref and proj_reg is not None:
        prec = proj_reg.resolve_handle(vault_ref, str(project_ref).strip())
        prel = str(prec.get("project_rel_path") or "").strip("/")
        proj_id = Path(prel).name if prel else prec.get("project_id")
        m_meta = read_domain_manifest(vault_dir / prel, str(proj_id or "")) if prel and (vault_dir / prel).is_dir() else {}
        if prel:
            proj_prefix = prel + "/"
            sealed_rels.append(prel)
        proj_meta = {
            "project_ref": str(project_ref).strip(),
            "project_id": m_meta.get("project_id") or proj_id,
            "title": m_meta.get("title") or prec.get("title") or proj_id,
            "status": m_meta.get("status") or prec.get("status") or "active",
            "review_mode": m_meta.get("default_mode") or m_meta.get("review_mode") or prec.get("review_mode") or "supervised",
            "visibility": "private" if m_meta.get("private") is True else (m_meta.get("visibility") or prec.get("visibility") or "internal"),
        }

    # Build fast wikilink target -> note_ref lookup using index entries
    by_rel_no_md: Dict[str, str] = {(r[:-3] if r.endswith(".md") else r): r for r in mapping}
    by_stem: Dict[str, List[str]] = {}
    for r in mapping:
        by_stem.setdefault(Path(r).stem, []).append(r)

    zone_counts: Dict[str, int] = {}
    status_counts: Dict[str, int] = {}
    notes_rows: List[Dict[str, Any]] = []
    edges: List[List[str]] = []
    edge_seen: set[Tuple[str, str]] = set()
    omitted_slices = 0
    scope_rels = sorted(r for r in entries if (not proj_prefix or r.startswith(proj_prefix)))
    scope_ref_set = {mapping[r] for r in scope_rels if r in mapping}

    for rel in scope_rels:
        if Path(rel).name.startswith("Manifest-"):
            continue
        ent = entries[rel]
        nref = mapping.get(rel)
        if not nref:
            continue
        z = _zone_for_rel(rel)
        st = str(ent.get("status") or "active").strip().lower()
        zone_counts[z] = zone_counts.get(z, 0) + 1
        status_counts[st] = status_counts.get(st, 0) + 1
        sealed_rels.append(rel)

        out_refs: List[str] = []
        for raw_t in ent.get("out_wikilinks") or []:
            t = str(raw_t or "").strip()
            if not t:
                continue
            t_no_md = t[:-3] if t.endswith(".md") else t
            t_rel: Optional[str] = None
            if "/" in t_no_md:
                t_rel = by_rel_no_md.get(t_no_md)
            else:
                hits = by_stem.get(t_no_md)
                if hits and len(hits) == 1:
                    t_rel = hits[0]
            if t_rel and t_rel != rel and t_rel in mapping:
                dst_ref = mapping[t_rel]
                if dst_ref not in out_refs:
                    out_refs.append(dst_ref)
                if dst_ref in scope_ref_set and (nref, dst_ref) not in edge_seen and len(edges) < 500:
                    edge_seen.add((nref, dst_ref))
                    edges.append([nref, dst_ref])

        if z == "slices" and not include_slices:
            omitted_slices += 1
            continue
        in_refs = bl_map.get(rel) or []
        notes_rows.append(
            {
                "note_ref": nref,
                "title": str(ent.get("title") or Path(rel).stem),
                "zone": z,
                "doc_type": str(ent.get("doc_type") or ""),
                "status": st,
                "size_bytes": int(ent.get("size", 0)),
                "in_degree": len(in_refs),
                "out_degree": len(out_refs),
                "out_refs": out_refs[:12],
            }
        )

    limit = max(1, min(1000, int(max_notes)))
    notes_truncated = len(notes_rows) > limit
    if notes_truncated:
        notes_rows = notes_rows[:limit]

    projects_overview: List[Dict[str, Any]] = []
    if not project_ref and proj_reg is not None:
        try:
            proj_root = vault_dir / "30-Projects"
            if proj_root.is_dir():
                disk_proj_rels = sorted(
                    f"30-Projects/{d.name}"
                    for d in proj_root.iterdir()
                    if d.is_dir() and not d.is_symlink() and not d.name.startswith(".")
                )
                proj_reg.sync_projects(vault_ref, disk_proj_rels)
            for p in proj_reg.list_projects(vault_ref):
                prel = str(p.get("project_rel_path") or "").strip("/")
                if prel:
                    sealed_rels.append(prel)
                pid = Path(prel).name if prel else p.get("project_id")
                m_meta = read_domain_manifest(vault_dir / prel, str(pid or "")) if prel and (vault_dir / prel).is_dir() else {}
                projects_overview.append(
                    {
                        "project_ref": p.get("project_ref"),
                        "project_id": m_meta.get("project_id") or pid,
                        "title": m_meta.get("title") or p.get("title") or pid,
                        "status": m_meta.get("status") or p.get("status") or "active",
                        "review_mode": m_meta.get("default_mode") or m_meta.get("review_mode") or "supervised",
                    }
                )
        except Exception:
            pass

    return {
        "vault_ref": vault_ref,
        "scope": "project" if project_ref else "vault",
        "project": proj_meta,
        "projects": projects_overview if not project_ref else None,
        "total_notes": sum(zone_counts.values()),
        "zone_counts": zone_counts,
        "status_counts": status_counts,
        "omitted_slices_count": omitted_slices,
        "returned_notes": len(notes_rows),
        "notes_truncated": notes_truncated,
        "notes": notes_rows,
        "edge_count": len(edges),
        "edges": edges,
        "path_sealed": True,
    }, sealed_rels
