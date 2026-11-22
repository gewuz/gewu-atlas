#!/usr/bin/env python3
"""KEK-protected Local-First note routing & link-graph index cache (zero plaintext SQLite).

Architecture:
- Disk Markdown files are ALWAYS the Single Source of Truth (SSOT).
- Stores a lightweight zlib-compressed whole-container AEAD-encrypted index (`<vault_ref>.ni1`, 0600)
  containing per-note `(mtime_ns, size, sha256, title, aliases, doc_type, status, out_wikilinks, vfs_cands)`.
- Before search / link-gate / backlink lookup, runs an `os.scandir()` stat-diff (~3-8 ms for
  1000 files, zero file reads for unchanged notes) to detect human external edits, additions,
  renames/moves (preserving `nte_XX` handles via content `sha256` pairing!), or deletions.
- Builds an O(1) inverted backlink map in memory so `note-get` & `note_get_batch` backlink lookup
  takes < 1 ms instead of scanning every note in the vault.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote

from notes.atlas_yamlmini import (
    FrontmatterError,
    extract_frontmatter,
    extract_wikilinks,
    get_vault_layout_spec,
    resolve_wikilink,
)
from storage.note_kek_store import KekNoteRegistry
from storage.vault_kek_store import (
    VaultStoreError,
    _atomic_write_0600,
    _check_no_symlink,
    aead_xchacha20_poly1305_decrypt,
    aead_xchacha20_poly1305_encrypt,
    hkdf_sha256,
)

_INDEX_MAGIC = b"GWNI3"
_INDEX_VERSION = 3
_NONCE_LEN = 24
_TAG_LEN = 16

_WIKILINK_RE = re.compile(r"(!?)\[\[([^\]]+)\]\]")
_REF_FIELDS = ("source", "sources", "toc", "slices", "supersedes", "superseded_by", "source_file", "extracted_to")
_REMOTE_URI_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*://|mailto:|tel:|urn:|data:|javascript:|obsidian:)", re.I)

# Opt-in process RAM hot-cache (enabled ONLY by AtlasHotKernel; disabled in cold CLI/tests).
# Keyed by resolved cache_file path -> (mtime_ns, size, key_digest, entries_dict).
_HOT_CACHE_ENABLED = False
_HOT_CACHE_MAX_ENTRIES = 8
_HOT_INDEX_CACHE: Dict[str, Tuple[int, int, bytes, Dict[str, Dict[str, Any]]]] = {}
_HOT_CACHE_HITS = 0
_HOT_CACHE_MISSES = 0


def set_hot_cache_enabled(enabled: bool, *, max_entries: int = 8) -> None:
    global _HOT_CACHE_ENABLED, _HOT_CACHE_MAX_ENTRIES
    _HOT_CACHE_ENABLED = bool(enabled)
    _HOT_CACHE_MAX_ENTRIES = max(1, int(max_entries))
    if not _HOT_CACHE_ENABLED:
        clear_hot_cache()


def clear_hot_cache() -> None:
    global _HOT_CACHE_HITS, _HOT_CACHE_MISSES
    _HOT_INDEX_CACHE.clear()
    _HOT_CACHE_HITS = 0
    _HOT_CACHE_MISSES = 0


def get_hot_cache_stats() -> Dict[str, Any]:
    return {
        "enabled": _HOT_CACHE_ENABLED,
        "cached_vaults": len(_HOT_INDEX_CACHE),
        "hits": _HOT_CACHE_HITS,
        "misses": _HOT_CACHE_MISSES,
    }


def _hot_key_digest(key: bytes) -> bytes:
    return hashlib.sha256(key).digest()


def _hot_put(cache_key: str, mtime_ns: int, size: int, key_digest: bytes, entries: Dict[str, Dict[str, Any]]) -> None:
    if cache_key in _HOT_INDEX_CACHE:
        _HOT_INDEX_CACHE.pop(cache_key, None)
    elif len(_HOT_INDEX_CACHE) >= _HOT_CACHE_MAX_ENTRIES:
        oldest = next(iter(_HOT_INDEX_CACHE))
        _HOT_INDEX_CACHE.pop(oldest, None)
    _HOT_INDEX_CACHE[cache_key] = (mtime_ns, size, key_digest, copy.deepcopy(entries))


def _index_key(kek: bytes, app_id: str, vault_ref: str) -> bytes:
    return hkdf_sha256(kek, b"gewu-atlas-note-index/v1", f"{app_id}|{vault_ref}".encode("utf-8"), length=32)


def _load_index(cache_file: Path, key: bytes) -> Dict[str, Dict[str, Any]]:
    global _HOT_CACHE_HITS, _HOT_CACHE_MISSES
    if not cache_file.exists():
        if _HOT_CACHE_ENABLED:
            _HOT_INDEX_CACHE.pop(str(cache_file), None)
        return {}
    _check_no_symlink(cache_file)
    c_key = str(cache_file)
    k_dig = _hot_key_digest(key) if _HOT_CACHE_ENABLED else b""
    st_mtime = -1
    st_size = -1
    if _HOT_CACHE_ENABLED:
        try:
            st = cache_file.stat()
            st_mtime = int(st.st_mtime_ns)
            st_size = int(st.st_size)
            hit = _HOT_INDEX_CACHE.get(c_key)
            if hit is not None and hit[0] == st_mtime and hit[1] == st_size and hit[2] == k_dig:
                _HOT_CACHE_HITS += 1
                # Refresh LRU order and return a deep copy so callers cannot mutate cached entry dicts.
                _hot_put(c_key, st_mtime, st_size, k_dig, hit[3])
                return copy.deepcopy(hit[3])
            _HOT_CACHE_MISSES += 1
        except OSError:
            pass
    try:
        blob = cache_file.read_bytes()
        if len(blob) < len(_INDEX_MAGIC) + _NONCE_LEN + _TAG_LEN + 1 or blob[:5] != _INDEX_MAGIC:
            return {}
        nonce = blob[5 : 5 + _NONCE_LEN]
        ct = blob[5 + _NONCE_LEN : -_TAG_LEN]
        tag = blob[-_TAG_LEN:]
        pt = aead_xchacha20_poly1305_decrypt(key, nonce, ct, b"note-index-v1", tag)
        doc = json.loads(zlib.decompress(pt).decode("utf-8"))
        if isinstance(doc, dict) and doc.get("v") == _INDEX_VERSION and isinstance(doc.get("entries"), dict):
            entries = doc["entries"]
            if _HOT_CACHE_ENABLED and st_mtime >= 0:
                _hot_put(c_key, st_mtime, st_size, k_dig, entries)
            return entries
    except Exception:
        return {}
    return {}


def _save_index(cache_file: Path, key: bytes, entries: Dict[str, Dict[str, Any]]) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    nonce = os.urandom(_NONCE_LEN)
    payload = zlib.compress(
        json.dumps({"v": _INDEX_VERSION, "entries": entries}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        level=1,
    )
    ct, tag = aead_xchacha20_poly1305_encrypt(key, nonce, payload, b"note-index-v1")
    _atomic_write_0600(cache_file, _INDEX_MAGIC + nonce + ct + tag)
    if _HOT_CACHE_ENABLED:
        try:
            st = cache_file.stat()
            _hot_put(str(cache_file), int(st.st_mtime_ns), int(st.st_size), _hot_key_digest(key), entries)
        except OSError:
            pass


def scandir_vault_markdowns(vault_dir: Path) -> Dict[str, Tuple[int, int]]:
    """Fast `os.scandir()` stat-only traversal returning `rel_path -> (mtime_ns, size)` without opening files."""
    if not vault_dir.is_dir():
        return {}
    skip_tops = set(get_vault_layout_spec()["skip_note_scan_tops"])
    vault_res = vault_dir.resolve()
    out: Dict[str, Tuple[int, int]] = {}

    def _walk(cur_dir: str, rel_prefix: str, is_root: bool) -> None:
        try:
            with os.scandir(cur_dir) as it:
                for entry in it:
                    name = entry.name
                    if name.startswith(".") or name in ("__pycache__", "node_modules"):
                        continue
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if is_root and name in skip_tops:
                                continue
                            sub_rel = f"{rel_prefix}{name}/" if rel_prefix else f"{name}/"
                            _walk(entry.path, sub_rel, False)
                        elif name.endswith(".md") and entry.is_file(follow_symlinks=False):
                            rel = f"{rel_prefix}{name}"
                            if rel == "20-AI-Zone/Manifest-AI-Zone.md":
                                continue
                            st = entry.stat(follow_symlinks=False)
                            out[rel] = (int(st.st_mtime_ns), int(st.st_size))
                    except OSError:
                        continue
        except OSError:
            return

    _walk(str(vault_res), "", True)
    return out


def _extract_vfs_candidates_from_text(text: str) -> List[str]:
    """Extract internal link candidates from Markdown prose & frontmatter, ignoring external `./` & `../` MD links."""
    from notes.atlas_markdown_blocks import code_line_mask
    from notes.atlas_wikilink_tools import split_wikilink_inner

    candidates: List[str] = []
    header = ""
    match = re.match(r"\A---[^\S\n]*\r?\n.*?\r?\n---[^\S\n]*(?:\r?\n|$)", text, re.S)
    body = text
    if match:
        header = match.group(0)
        body = text[match.end() :]

    inline_re = re.compile(r"(?<!`)(?P<tick>`+)(?!`)(?P<code>.*?)(?<!`)(?P=tick)(?!`)", re.S)

    def _scan_seg(seg: str) -> None:
        for _, inner in _WIKILINK_RE.findall(seg):
            t, _, _ = split_wikilink_inner(inner)
            if t:
                candidates.append(t)
        for _, raw in re.findall(r"(!?\[[^\]]*\]\(\s*)(<[^>]+>|[^)\s]+)", seg):
            val = raw[1:-1] if raw.startswith("<") else raw
            val = val.split("#", 1)[0].strip()
            if not val:
                continue
            # Ignore external relative Markdown links (`./...`, `../...`) scraped from web/GitHub docs.
            if val.startswith(("./", "../")):
                continue
            candidates.append(val)
        for src in re.findall(r"""<img[^>]+src=["']([^"']+)["']""", seg, re.I):
            if src and not src.startswith(("./", "../")):
                candidates.append(src)

    if header:
        _scan_seg(header)
    prose: List[str] = []

    def _flush() -> None:
        if not prose:
            return
        content = "".join(prose)
        start = 0
        for item in inline_re.finditer(content):
            _scan_seg(content[start : item.start()])
            start = item.end()
        _scan_seg(content[start:])
        prose.clear()

    for line, is_code in zip(body.splitlines(keepends=True), code_line_mask(body)):
        if is_code:
            _flush()
        else:
            prose.append(line)
    _flush()

    try:
        meta, _, has_fm = extract_frontmatter(text)
    except FrontmatterError:
        meta, has_fm = {}, False
    if has_fm:
        def _push_fm_ref(raw_s: str) -> None:
            s = raw_s.strip()
            if s.startswith("[[") and s.endswith("]]"):
                t, _, _ = split_wikilink_inner(s[2:-2])
                if t:
                    candidates.append(t)
            elif s:
                candidates.append(s)

        for fk in _REF_FIELDS:
            val = meta.get(fk)
            if isinstance(val, str) and val.strip():
                _push_fm_ref(val)
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, str) and item.strip():
                        _push_fm_ref(item)

    cleaned: List[str] = []
    for c in candidates:
        u = unquote(str(c).strip().strip("<>"))
        if not u or _REMOTE_URI_RE.match(u) or u.startswith("//"):
            continue
        cleaned.append(u)
    return cleaned


def _parse_note_entry(fp: Path, mtime_ns: int, size: int) -> Optional[Dict[str, Any]]:
    try:
        raw_bytes = fp.read_bytes()
    except OSError:
        return None
    if raw_bytes[:1024].lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"%PDF-"):
        return None
    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    txt = raw_bytes.decode("utf-8", errors="replace")
    try:
        meta, _body, has_fm = extract_frontmatter(txt)
    except FrontmatterError:
        meta, has_fm = {"doc_type": "invalid", "title": fp.stem}, False
    aliases = [str(a).strip() for a in (meta.get("aliases") or []) if isinstance(a, str) and str(a).strip()]
    out_wikilinks = extract_wikilinks(txt)
    vfs_cands = _extract_vfs_candidates_from_text(txt)
    return {
        "mtime_ns": mtime_ns,
        "size": size,
        "sha256": sha256,
        "has_fm": bool(has_fm),
        "title": str(meta.get("title") or fp.stem),
        "doc_type": str(meta.get("doc_type") or ""),
        "status": str(meta.get("status") or ""),
        "private": bool(meta.get("private") is True),
        "aliases": aliases,
        "out_wikilinks": out_wikilinks,
        "vfs_cands": vfs_cands,
    }


def reconcile_note_index(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    cache_dir: Optional[Path] = None,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
    """Millisecond-scale `os.scandir()` stat-diff reconciliation with rename/move handle preservation."""
    cfile = (cache_dir / f"{vault_ref}.ni1") if cache_dir else None
    ckey = _index_key(kek, app_id, vault_ref) if cfile else None
    cached = _load_index(cfile, ckey) if (cfile and ckey) else {}

    disk_stats = scandir_vault_markdowns(vault_dir)
    next_entries: Dict[str, Dict[str, Any]] = {}
    dirty = False

    for rel, (mtime_ns, size) in disk_stats.items():
        prev = cached.get(rel)
        if prev is not None and int(prev.get("mtime_ns", -1)) == mtime_ns and int(prev.get("size", -1)) == size:
            if not prev.get("_is_pdf"):
                next_entries[rel] = prev
            continue
        dirty = True
        parsed = _parse_note_entry(vault_dir / rel, mtime_ns, size)
        if parsed is None:
            next_entries[rel] = {"mtime_ns": mtime_ns, "size": size, "_is_pdf": True}
        else:
            next_entries[rel] = parsed

    if len(cached) != len(next_entries) or any(k not in next_entries for k in cached):
        dirty = True

    if dirty and cfile and ckey:
        _save_index(cfile, ckey, next_entries)

    live_entries = {rel: ent for rel, ent in next_entries.items() if not ent.get("_is_pdf")}
    existing_map = note_reg.list_vault_handles(vault_ref)
    live_keys = set(live_entries.keys())
    reg_keys = set(existing_map.keys())
    if live_keys != reg_keys:
        removed = sorted(reg_keys - live_keys)
        added = sorted(live_keys - reg_keys)

        # Pair human external renames/moves by unique (size, sha256) so nte_XX handles survive untouched!
        if removed and added:
            removed_by_sig: Dict[Tuple[int, str], List[str]] = {}
            for r_rel in removed:
                old_ent = cached.get(r_rel)
                if old_ent and old_ent.get("sha256"):
                    sig = (int(old_ent.get("size", -1)), str(old_ent["sha256"]))
                    removed_by_sig.setdefault(sig, []).append(r_rel)

            added_by_sig: Dict[Tuple[int, str], List[str]] = {}
            for a_rel in added:
                new_ent = live_entries.get(a_rel)
                if new_ent and new_ent.get("sha256"):
                    sig = (int(new_ent.get("size", -1)), str(new_ent["sha256"]))
                    added_by_sig.setdefault(sig, []).append(a_rel)

            matched_removed: Set[str] = set()
            matched_added: Set[str] = set()
            for sig, r_list in removed_by_sig.items():
                a_list = added_by_sig.get(sig, [])
                if len(r_list) == 1 and len(a_list) == 1:
                    old_rel, new_rel = r_list[0], a_list[0]
                    old_ref = existing_map.get(old_rel)
                    if old_ref:
                        note_reg.move_handle(vault_ref, old_ref, new_rel)
                        matched_removed.add(old_rel)
                        matched_added.add(new_rel)
            if matched_removed:
                removed = [r for r in removed if r not in matched_removed]
                added = [a for a in added if a not in matched_added]

        if removed:
            note_reg.remove_paths(vault_ref, removed)
        if added:
            note_reg.upsert_paths(vault_ref, added)
        existing_map = note_reg.list_vault_handles(vault_ref)

    return live_entries, existing_map


def update_single_note_index(
    *,
    vault_dir: Path,
    vault_ref: str,
    rel_paths: List[str],
    kek: bytes,
    app_id: str,
    cache_dir: Optional[Path] = None,
) -> None:
    """Incrementally update specific changed note entries in the encrypted index cache."""
    if not cache_dir or not rel_paths:
        return
    cfile = cache_dir / f"{vault_ref}.ni1"
    if not cfile.exists():
        return
    ckey = _index_key(kek, app_id, vault_ref)
    cached = _load_index(cfile, ckey)
    if not cached:
        return
    changed = False
    for rel in rel_paths:
        fp = vault_dir / rel
        if fp.is_file() and not fp.is_symlink():
            st = fp.stat()
            parsed = _parse_note_entry(fp, int(st.st_mtime_ns), int(st.st_size))
            if parsed is not None:
                cached[rel] = parsed
                changed = True
        elif rel in cached:
            cached.pop(rel, None)
            changed = True
    if changed:
        _save_index(cfile, ckey, cached)


def resolve_note_jit(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_ref: str,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    cache_dir: Optional[Path] = None,
) -> Tuple[str, Path]:
    """O(1) route-table lookup with automatic JIT self-healing if human moved/renamed/deleted file."""
    try:
        rec = note_reg.resolve_handle(vault_ref, note_ref)
        rel = str(rec["note_rel_path"])
        fpath = vault_dir / rel
        if fpath.is_file() and not fpath.is_symlink():
            return rel, fpath
    except VaultStoreError as exc:
        if exc.code != "E_NOT_FOUND":
            raise

    reconcile_note_index(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        note_reg=note_reg,
        kek=kek,
        app_id=app_id,
        cache_dir=cache_dir,
    )
    rec = note_reg.resolve_handle(vault_ref, note_ref)
    rel = str(rec["note_rel_path"])
    fpath = vault_dir / rel
    if not fpath.is_file() or fpath.is_symlink():
        raise VaultStoreError("E_NOT_FOUND", f"句柄 {note_ref} 对应笔记不存在于磁盘", exit_code=1)
    return rel, fpath


def build_backlink_map(
    *,
    entries: Dict[str, Dict[str, Any]],
    handle_map: Dict[str, str],
) -> Dict[str, List[str]]:
    """Build O(1) inverted backlink map `target_rel -> sorted(source_note_refs)` in a single pass (~2ms)."""
    known_rel: Set[str] = set(handle_map.keys())
    by_rel_no_md: Dict[str, str] = {
        (r[:-3] if r.endswith(".md") else r): r for r in known_rel
    }
    by_stem: Dict[str, List[str]] = {}
    for r in known_rel:
        by_stem.setdefault(Path(r).stem, []).append(r)

    in_map: Dict[str, Set[str]] = {}
    for src_rel, ent in entries.items():
        if Path(src_rel).name.startswith("Manifest-"):
            continue
        src_ref = handle_map.get(src_rel)
        if not src_ref:
            continue
        for raw_t in ent.get("out_wikilinks") or []:
            t = str(raw_t or "").strip()
            if not t:
                continue
            t_no_md = t[:-3] if t.endswith(".md") else t
            target_rel: Optional[str] = None
            if "/" in t_no_md:
                if t_no_md in by_rel_no_md:
                    target_rel = by_rel_no_md[t_no_md]
                else:
                    # Relative path resolution fallback
                    cands = resolve_wikilink(t, src_rel, handle_map)
                    if len(cands) == 1:
                        target_rel = cands[0]
            else:
                hits = by_stem.get(t_no_md)
                if hits and len(hits) == 1:
                    target_rel = hits[0]
            if target_rel and target_rel != src_rel:
                in_map.setdefault(target_rel, set()).add(src_ref)

    return {k: sorted(v) for k, v in in_map.items()}


def compute_backlinks_from_index(
    *,
    target_rel: str,
    entries: Dict[str, Dict[str, Any]],
    handle_map: Dict[str, str],
    backlink_map: Optional[Dict[str, List[str]]] = None,
) -> List[str]:
    """Return incoming `backlink_refs` in O(1) when `backlink_map` is provided."""
    if backlink_map is not None:
        return list(backlink_map.get(target_rel) or [])
    return list(build_backlink_map(entries=entries, handle_map=handle_map).get(target_rel) or [])
