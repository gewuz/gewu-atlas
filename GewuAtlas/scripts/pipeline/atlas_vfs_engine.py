#!/usr/bin/env python3
"""KEK-blinded Virtual File System (VFS) transaction engine for GewuAtlas.

Ports 116-gwf-v2-R6 `vfs.sh`, `vfs_guard.py`, and `link_cascade.py`:
- Safe O_EXCL creation (`vfs-create`) with placeholder & compartment guards;
- Pre-move ambiguity check, code-fence-aware two-pass + frontmatter link cascade & rollback (`vfs-move`);
- Safe collision-guarded copy (`vfs-copy`);
- Reference-checking soft delete into `.trash/` with KEK-sealed `trs_XX` handles (`vfs-trash`);
- Opaque-handle trash listing & controlled recovery (`vfs-untrash`);
- Vault topology & symlink validation (`vfs-validate`) and full wikilink/canvas/frontmatter audit (`vfs-links`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from facade.atlas_domain_scope import (
    DOMAIN_COMPARTMENTS,
    infer_zone_and_project_rel,
    read_domain_manifest,
    resolve_archive_dir,
    resolve_domain_scope,
    resolve_zone_dir,
    scan_project_rels,
)
from notes.atlas_note_engine import (
    DEFAULT_DOC_TYPE_BY_ZONE,
    _atomic_write_md,
    _public_note,
    _render_note_md,
    _scan_vault_markdowns,
    _slugify_filename,
    _today,
)
from notes.atlas_yamlmini import FrontmatterError, extract_frontmatter, get_vault_layout_spec, replace_frontmatter_field
from storage.note_kek_store import KekNoteRegistry, validate_note_rel_path
from storage.project_kek_store import KekProjectRegistry
from storage.vault_kek_store import (
    VaultStoreError,
    _atomic_write_0600,
    _check_no_symlink,
    aead_xchacha20_poly1305_decrypt,
    aead_xchacha20_poly1305_encrypt,
    hkdf_sha256,
    registry_file_lock,
)

TRS_HANDLE_RE = re.compile(r"^trs_[0-9]{2,6}$")
_TRASH_MAGIC = b"GWTR1"
_NONCE_LEN = 24
_TAG_LEN = 16

_WIKILINK_RE = re.compile(r"(!?)\[\[([^\]]+)\]\]")
_NOT_REMOTE = r"(?!(?i:https?|ftp)://|//)"
_REF_FIELDS = ("source", "sources", "toc", "slices", "supersedes", "superseded_by", "source_file", "extracted_to")
_REF_LIST_FIELDS = ("sources", "toc", "slices", "source_file", "extracted_to")
_AI_ZONE_ATOMIC_PLACEHOLDERS = frozenset({"concept", "note", "idea", "atom", "test", "temp"})
_TOPS = frozenset({"05-Journal", "00-DropZone", "10-Index", "20-AI-Zone", "30-Projects", "90-System", "99-Assets"})
_FLAT_DIRS = ("22-Atomics", "23-MOC", "25-Slip", "10-Attachments")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class KekTrashRegistry:
    """AEAD-encrypted registry mapping opaque `trs_XX` handles <-> `.trash/` items."""

    def __init__(self, app_id: str, enc_path: Path, master_kek: bytes) -> None:
        self.app_id = app_id
        self.enc_path = enc_path
        self._thread_lock = threading.Lock()
        self._kek = hkdf_sha256(
            master_kek,
            salt=b"gewu-atlas-trash-salt-v1",
            info=f"{app_id}||trash-v1".encode("utf-8"),
            length=32,
        )

    @contextmanager
    def _container_lock(self, *, shared: bool = False):
        with registry_file_lock(self.enc_path, shared=shared, thread_lock=self._thread_lock):
            yield

    def _file_aad(self) -> bytes:
        return f"{self.app_id}||trash-container||v1".encode("utf-8")

    def _load_doc(self) -> Dict[str, Any]:
        _check_no_symlink(self.enc_path)
        if not self.enc_path.exists():
            return {"app_id": self.app_id, "next_seq": 1, "rows": []}
        raw = self.enc_path.read_bytes()
        if len(raw) < len(_TRASH_MAGIC) + _NONCE_LEN + _TAG_LEN or not raw.startswith(_TRASH_MAGIC):
            raise VaultStoreError("E_HALT_SECURITY", "回收站密文容器格式损坏", exit_code=2)
        pos = len(_TRASH_MAGIC)
        nonce = raw[pos : pos + _NONCE_LEN]
        pos += _NONCE_LEN
        tag = raw[pos : pos + _TAG_LEN]
        pos += _TAG_LEN
        ct = raw[pos:]
        pt = aead_xchacha20_poly1305_decrypt(self._kek, nonce, ct, self._file_aad(), tag)
        doc = json.loads(pt.decode("utf-8"))
        if not isinstance(doc, dict) or doc.get("app_id") != self.app_id:
            raise VaultStoreError("E_HALT_SECURITY", "回收站容器 app_id 不匹配", exit_code=2)
        return doc

    def _save_doc(self, doc: Dict[str, Any]) -> None:
        nonce = os.urandom(_NONCE_LEN)
        pt = json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ct, tag = aead_xchacha20_poly1305_encrypt(self._kek, nonce, pt, self._file_aad())
        blob = _TRASH_MAGIC + nonce + tag + ct
        _atomic_write_0600(self.enc_path, blob)

    def sync_vault(self, vault_dir: Path, vault_ref: str) -> List[Dict[str, Any]]:
        with self._container_lock():
            return self._sync_vault_unlocked(vault_dir, vault_ref)

    def _sync_vault_unlocked(self, vault_dir: Path, vault_ref: str) -> List[Dict[str, Any]]:
        trash_root = vault_dir / ".trash"
        if trash_root.is_symlink():
            raise VaultStoreError("E_HALT_SECURITY", "回收站目录禁止软链接", exit_code=2)

        doc = self._load_doc()
        rows: List[Dict[str, Any]] = [r for r in doc.get("rows", []) if isinstance(r, dict)]
        next_seq = int(doc.get("next_seq", 1))
        tracked_dir_rels = {
            str(r.get("trash_rel") or "")
            for r in rows
            if r.get("vault_ref") == vault_ref and r.get("kind") == "dir" and str(r.get("trash_rel") or "")
        }

        on_disk: Dict[str, Path] = {}
        if trash_root.is_dir():
            for cur_root, dirs, files in os.walk(trash_root, followlinks=False):
                cur = Path(cur_root)
                cur_rel = cur.relative_to(vault_dir).as_posix()
                if cur_rel in tracked_dir_rels:
                    # Preserve trashed directory as a single opaque handle; do not split child files
                    dirs[:] = []
                    continue
                dirs[:] = [
                    d
                    for d in sorted(dirs)
                    if not (cur / d).is_symlink() and (cur / d).relative_to(vault_dir).as_posix() not in tracked_dir_rels
                ]
                for fname in sorted(files):
                    if fname in (".gitkeep", ".trash.lock", ".DS_Store"):
                        continue
                    fp = cur / fname
                    if fp.is_symlink() or not fp.is_file():
                        continue
                    rel = fp.relative_to(vault_dir).as_posix()
                    on_disk[rel] = fp

        by_trel: Dict[str, Dict[str, Any]] = {}
        kept_rows: List[Dict[str, Any]] = []
        changed = False
        for row in rows:
            if row.get("vault_ref") == vault_ref:
                trel = str(row.get("trash_rel") or "")
                is_live_dir = row.get("kind") == "dir" and bool(trel) and (vault_dir / trel).is_dir() and not (vault_dir / trel).is_symlink()
                if trel in on_disk or is_live_dir:
                    by_trel[trel] = row
                    kept_rows.append(row)
                else:
                    changed = True
            else:
                kept_rows.append(row)

        for trel in sorted(on_disk):
            if trel not in by_trel:
                fp = on_disk[trel]
                tref = f"trs_{next_seq:02d}"
                next_seq += 1
                st = fp.stat()
                row = {
                    "vault_ref": vault_ref,
                    "trash_ref": tref,
                    "trash_rel": trel,
                    "orig_rel": "",
                    "orig_zone": "",
                    "name": fp.name,
                    "kind": "note" if (fp.suffix.lower() == ".md" or ".md." in fp.name) else "file",
                    "size_bytes": int(st.st_size),
                    "deleted_at": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds"),
                }
                kept_rows.append(row)
                by_trel[trel] = row
                changed = True

        if changed:
            doc["rows"] = kept_rows
            doc["next_seq"] = next_seq
            self._save_doc(doc)
        return [dict(r) for r in kept_rows if r.get("vault_ref") == vault_ref]

    def record_trashed(
        self,
        *,
        vault_ref: str,
        trash_rel: str,
        orig_rel: str,
        orig_zone: str,
        display_name: str,
        kind: str,
        size_bytes: int,
    ) -> Dict[str, Any]:
        with self._container_lock():
            doc = self._load_doc()
            rows: List[Dict[str, Any]] = [r for r in doc.get("rows", []) if isinstance(r, dict)]
            next_seq = int(doc.get("next_seq", 1))
            tref = f"trs_{next_seq:02d}"
            next_seq += 1
            row = {
                "vault_ref": vault_ref,
                "trash_ref": tref,
                "trash_rel": trash_rel,
                "orig_rel": orig_rel,
                "orig_zone": orig_zone,
                "name": display_name,
                "kind": kind,
                "size_bytes": int(size_bytes),
                "deleted_at": _now_iso(),
            }
            rows.append(row)
            doc["rows"] = rows
            doc["next_seq"] = next_seq
            self._save_doc(doc)
            return dict(row)

    def resolve_handle(self, vault_dir: Path, vault_ref: str, trash_ref: str) -> Dict[str, Any]:
        if not TRS_HANDLE_RE.fullmatch(trash_ref):
            raise VaultStoreError("E_PARAM_INVALID", f"非法回收站句柄: {trash_ref}", exit_code=1)
        rows = self.sync_vault(vault_dir, vault_ref)
        for r in rows:
            if r.get("trash_ref") == trash_ref:
                return r
        raise VaultStoreError("E_NOT_FOUND", f"未找到回收站条目: {trash_ref}", exit_code=1)


def _format_size(bytes_val: int) -> str:
    if bytes_val < 1024:
        return f"{bytes_val} B"
    if bytes_val < 1024 * 1024:
        return f"{bytes_val / 1024:.1f} KB"
    return f"{bytes_val / (1024 * 1024):.1f} MB"


def _is_rel_frozen(vault_dir: Path, rel_path: str) -> Optional[str]:
    parts = Path(rel_path).parts
    if len(parts) >= 2 and parts[0] == "30-Projects":
        pdir = vault_dir / "30-Projects" / parts[1]
        m = read_domain_manifest(pdir, parts[1])
        if m.get("status") in ("deprecated", "archived"):
            return str(m.get("project_id") or parts[1])
    elif parts and parts[0] == "20-AI-Zone":
        m = read_domain_manifest(vault_dir / "20-AI-Zone", "ai-zone")
        if m.get("status") in ("deprecated", "archived"):
            return "ai-zone"
    return None


def _require_unfrozen(vault_dir: Path, rel_path: str) -> None:
    frozen = _is_rel_frozen(vault_dir, rel_path)
    if frozen:
        raise VaultStoreError("E_DOMAIN_FROZEN", f"目标所属域已冻结/归档 ({frozen})，禁止 VFS 写入或变更", exit_code=1)


def _is_artifact_rel(rel_path: str) -> bool:
    parts = Path(rel_path).parts
    return rel_path.endswith(".md") and (
        parts[:2] == ("20-AI-Zone", "10-Artifacts")
        or (len(parts) >= 3 and parts[0] == "30-Projects" and parts[2] == "10-Artifacts")
    )


def _validate_destination_topology(vault_dir: Path, dst_rel: str, *, src_rel: Optional[str] = None, is_untrash: bool = False) -> None:
    parts = Path(dst_rel).parts
    tops = get_vault_layout_spec()["top_level_names"]
    if not parts or parts[0] not in tops:
        raise VaultStoreError("E_VFS_TOPOLOGY", "目标目录不在 Vault 允许分区内", exit_code=1)
    if parts[0] in ("20-AI-Zone", "30-Projects"):
        offset = 1 if parts[0] == "20-AI-Zone" else 2
        tail = parts[offset:]
        allowed_cabins = (
            ("10-Artifacts", "20-Memory", "25-Slip", "30-Workbench", "99-Assets")
            if parts[0] == "20-AI-Zone"
            else ("10-Artifacts", "20-Memory", "25-Slip", "30-Workbench")
        )
        legal = (tail and tail[0] in allowed_cabins) or (
            len(tail) == 1 and tail[0].lower().startswith("manifest")
        )
        if not legal:
            raise VaultStoreError("E_VFS_TOPOLOGY", "非法域内舱位", exit_code=1)
        if tail and tail[0] == "20-Memory":
            if len(tail) < 3 or tail[1] not in ("21-Library", "22-Atomics", "23-MOC", "24-Slices", "97-Attic"):
                raise VaultStoreError("E_VFS_TOPOLOGY", "非法记忆舱位", exit_code=1)
    for flat in _FLAT_DIRS:
        if flat in parts and len(parts) != parts.index(flat) + 2:
            raise VaultStoreError("E_VFS_FLAT_DIR", f"扁平目录禁止多级子目录: {flat}", exit_code=1)

    if _is_artifact_rel(dst_rel):
        if is_untrash:
            raise VaultStoreError("E_VFS_ARTIFACT_GUARD", "回收站单文件缺少入舱证明，请先恢复至 workbench 后再执行 artifact-adopt", exit_code=1)
        if src_rel is None or not (_is_artifact_rel(src_rel) and Path(src_rel).parent == Path(dst_rel).parent):
            raise VaultStoreError("E_VFS_ARTIFACT_GUARD", "正式成果须在工作台评审后通过 artifact-adopt 入舱", exit_code=1)

    if parts[:3] == ("20-AI-Zone", "20-Memory", "22-Atomics") and dst_rel.endswith(".md"):
        if Path(dst_rel).stem.lower() in _AI_ZONE_ATOMIC_PLACEHOLDERS:
            raise VaultStoreError("E_VFS_NAME_GUARD", "AI-Zone 原子卡禁止使用 concept/note/idea/atom/test/temp 占位命名", exit_code=1)

    _require_unfrozen(vault_dir, dst_rel)


def _atomic_create_excl(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(target, flags, 0o644)
    except FileExistsError as exc:
        raise VaultStoreError("E_COLLISION", "目标文件已存在（O_EXCL 并发防护）", exit_code=1) from exc
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())


def _split_inner(inner: str) -> Tuple[str, str, str]:
    from notes.atlas_wikilink_tools import split_wikilink_inner

    return split_wikilink_inner(inner)


def _basename_of(target: str) -> str:
    base = target.rsplit("/", 1)[-1]
    return base[:-3] if base.endswith(".md") else base


def _iter_vault_ref_files(vault_dir: Path) -> List[Path]:
    out: List[Path] = []
    for cur_root, dirs, files in os.walk(vault_dir, followlinks=False):
        cur = Path(cur_root)
        dirs[:] = [
            d
            for d in sorted(dirs)
            if not d.startswith(".") and d not in ("__pycache__", "node_modules") and not (cur / d).is_symlink()
        ]
        for fname in sorted(files):
            fp = cur / fname
            if fp.is_symlink() or fp.suffix.lower() not in (".md", ".canvas", ".base"):
                continue
            out.append(fp)
    return out


def _extract_file_candidates(path: Path, vault_dir: Path) -> List[str]:
    from urllib.parse import unquote

    text = path.read_text(encoding="utf-8", errors="replace")
    candidates: List[str] = []
    if path.suffix.lower() == ".canvas":
        try:
            data = json.loads(text)
            for node in data.get("nodes", []):
                if isinstance(node, dict) and node.get("type") == "file" and isinstance(node.get("file"), str):
                    candidates.append(node["file"])
        except Exception:
            pass
    else:
        def _collect_prose(seg: str) -> str:
            for _, inner in _WIKILINK_RE.findall(seg):
                t, _, _ = _split_inner(inner)
                if t:
                    candidates.append(t)
            for _, raw in re.findall(r"(!?\[[^\]]*\]\(\s*)(<[^>]+>|[^)\s]+)", seg):
                val = raw[1:-1] if raw.startswith("<") else raw
                val = val.split("#", 1)[0].strip()
                if val and not val.startswith(("./", "../")):
                    candidates.append(val)
            for src in re.findall(r"""<img[^>]+src=["']([^"']+)["']""", seg, re.I):
                if src and not src.startswith(("./", "../")):
                    candidates.append(src)
            return seg

        _rewrite_prose(text, _collect_prose)
        meta, _, has_fm = extract_frontmatter(text)
        if has_fm:
            def _push_fm_ref(raw_s: str) -> None:
                s = raw_s.strip()
                if s.startswith("[[") and s.endswith("]]"):
                    t, _, _ = _split_inner(s[2:-2])
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
        if not u or re.match(r"^(?:[A-Za-z][A-Za-z0-9+.-]*://|mailto:|tel:|urn:|data:|javascript:|obsidian:)", u, re.I) or u.startswith("//"):
            continue
        cleaned.append(u)
    return cleaned


_ALLOWED_DIR_ZONES = ("dropzone", "library", "slices", "workbench", "archives")
_PROTECTED_SUBDIR_NAMES = frozenset({"91-temp", "source", "10-attachments", "98-archives"})


def _resolve_logical_subdir(
    *,
    vault_dir: Path,
    vault_ref: str,
    proj_reg: KekProjectRegistry,
    zone: str,
    subdir: str,
    project_ref: Optional[str] = None,
    scope: str = "auto",
    must_exist: bool = True,
) -> Tuple[Path, str, Dict[str, Any]]:
    """Resolve a user subfolder inside an allowed non-flat zone (blocks skeleton/manifest roots)."""
    z = (zone or "").strip().lower()
    if z not in _ALLOWED_DIR_ZONES:
        raise VaultStoreError(
            "E_PARAM_INVALID",
            f"目录操作 --zone 仅支持: {', '.join(_ALLOWED_DIR_ZONES)}",
            exit_code=1,
        )
    clean_sub = (subdir or "").strip().strip("/")
    parts = [p for p in clean_sub.split("/") if p]
    if not parts or "\\" in clean_sub or any(p in (".", "..") or p.startswith(".") for p in parts):
        raise VaultStoreError("E_PARAM_INVALID", f"非法逻辑子目录: {subdir}", exit_code=1)
    if parts[0].lower() in _PROTECTED_SUBDIR_NAMES and len(parts) == 1:
        raise VaultStoreError("E_VFS_STRUCTURAL", f"系统骨架内置子目录 '{parts[0]}' 禁止删改", exit_code=1)

    domain = resolve_domain_scope(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        proj_reg=proj_reg,
        explicit_project_ref=project_ref,
        scope=scope,
    )
    if z == "dropzone":
        base_dir = vault_dir / "00-DropZone"
    elif z == "archives":
        base_dir = resolve_archive_dir(vault_dir, domain)
    else:
        base_dir = resolve_zone_dir(vault_dir, domain, z)

    target_dir = (base_dir / "/".join(parts)).resolve()
    if not target_dir.is_relative_to(base_dir.resolve()) or target_dir == base_dir.resolve():
        raise VaultStoreError("E_VFS_STRUCTURAL", "禁止越界或直接删改分区根目录", exit_code=1)
    rel = target_dir.relative_to(vault_dir.resolve()).as_posix()
    _require_unfrozen(vault_dir, rel + "/_")
    if must_exist:
        if not target_dir.is_dir() or target_dir.is_symlink():
            raise VaultStoreError("E_NOT_FOUND", "指定逻辑子目录不存在或为受限符号链接", exit_code=1)
        for p in target_dir.rglob("Manifest-*.md"):
            if p.is_file():
                raise VaultStoreError("E_VFS_STRUCTURAL", "包含课题清单 Manifest 的目录禁止 VFS 删改", exit_code=1)
    return target_dir, rel, domain


def _collect_target_specs(
    vault_dir: Path,
    target_rels: Sequence[str],
) -> Tuple[set[str], set[str], set[str]]:
    """Build (exact_rel_and_nomd_set, unique_bare_names_set, target_rels_set) for matching."""
    target_rel_set = set(target_rels)
    surviving_stems = {
        Path(r).stem
        for r in _scan_vault_markdowns(vault_dir)
        if r not in target_rel_set
    }
    target_set: set[str] = set()
    bare_names: set[str] = set()
    for rel in target_rels:
        p = Path(rel)
        no_md = rel[:-3] if rel.endswith(".md") else rel
        target_set.add(rel)
        target_set.add(no_md)
        if p.stem not in surviving_stems:
            bare_names.add(p.name)
            bare_names.add(p.stem)
            if p.suffix.lower() == ".md":
                bare_names.add(f"{p.stem}.md")
    return target_set, bare_names, target_rel_set


def _match_candidate_to_targets(
    raw_cand: str,
    ref_file: Path,
    vault_dir: Path,
    target_set: set[str],
    bare_names: set[str],
) -> bool:
    from urllib.parse import unquote

    c = unquote(str(raw_cand or "").strip().strip("<>"))
    if not c or re.match(r"^(?:[A-Za-z][A-Za-z0-9+.-]*://|mailto:|tel:|urn:|data:|javascript:|obsidian:)", c, re.I) or c.startswith("//"):
        return False
    c_no_md = c[:-3] if c.endswith(".md") else c
    cand_paths = {c, c_no_md, f"{c}.md"}
    local = (ref_file.parent / c).resolve()
    v_res = vault_dir.resolve()
    if local.is_relative_to(v_res):
        lrel = local.relative_to(v_res).as_posix()
        cand_paths.add(lrel)
        if not local.suffix:
            cand_paths.add(lrel + ".md")
    if cand_paths & target_set:
        return True
    return "/" not in c and c in bare_names


def _scan_incoming_references_for_targets(
    vault_dir: Path,
    note_reg: KekNoteRegistry,
    vault_ref: str,
    target_rels: Sequence[str],
) -> List[Dict[str, Any]]:
    if not target_rels:
        return []
    target_set, bare_names, target_rel_set = _collect_target_specs(vault_dir, target_rels)
    all_md = _scan_vault_markdowns(vault_dir)
    n_map = note_reg.sync_paths(vault_ref, all_md)
    hits: List[Dict[str, Any]] = []
    for ref_file in _iter_vault_ref_files(vault_dir):
        ref_rel = ref_file.relative_to(vault_dir).as_posix()
        if ref_rel in target_rel_set or ref_file.name.startswith("Manifest-"):
            continue
        cands = _extract_file_candidates(ref_file, vault_dir)
        matched = any(_match_candidate_to_targets(c, ref_file, vault_dir, target_set, bare_names) for c in cands)
        if matched:
            if ref_file.suffix.lower() == ".md":
                hits.append({"kind": "note", "note_ref": n_map.get(ref_rel), "title": ref_file.stem})
            elif ref_file.suffix.lower() == ".canvas":
                hits.append({"kind": "canvas", "title": ref_file.stem})
            else:
                hits.append({"kind": "base", "title": ref_file.stem})
    return hits


def _scan_incoming_references(
    vault_dir: Path,
    note_reg: KekNoteRegistry,
    vault_ref: str,
    target_rel: str,
) -> List[Dict[str, Any]]:
    return _scan_incoming_references_for_targets(vault_dir, note_reg, vault_ref, [target_rel])


def _downgrade_refs_in_vault(
    vault_dir: Path,
    target_rels: Sequence[str],
) -> Dict[str, int]:
    """Downgrade all incoming wikilinks / Markdown links / canvas file nodes to plaintext."""
    if not target_rels:
        return {"files_updated": 0, "links_downgraded": 0, "canvas_nodes_downgraded": 0}
    target_set, bare_names, target_rel_set = _collect_target_specs(vault_dir, target_rels)
    files_updated = 0
    links_downgraded = 0
    canvas_nodes_downgraded = 0

    for ref_file in _iter_vault_ref_files(vault_dir):
        ref_rel = ref_file.relative_to(vault_dir).as_posix()
        if ref_rel in target_rel_set or _is_rel_frozen(vault_dir, ref_rel):
            continue
        suf = ref_file.suffix.lower()
        if suf == ".canvas":
            try:
                data = json.loads(ref_file.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            nodes = data.get("nodes")
            if not isinstance(nodes, list):
                continue
            changed_canvas = 0
            for node in nodes:
                if not isinstance(node, dict) or node.get("type") != "file":
                    continue
                fval = str(node.get("file") or "").strip()
                if fval and _match_candidate_to_targets(fval, ref_file, vault_dir, target_set, bare_names):
                    node["type"] = "text"
                    node["text"] = Path(fval).stem
                    node.pop("file", None)
                    node.pop("subpath", None)
                    changed_canvas += 1
            if changed_canvas > 0:
                _atomic_write_md(ref_file, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
                files_updated += 1
                canvas_nodes_downgraded += changed_canvas
                links_downgraded += changed_canvas
            continue

        if suf != ".md":
            continue
        old_txt = ref_file.read_text(encoding="utf-8", errors="replace")
        if old_txt.lstrip("\ufeff \t\r\n").startswith("%PDF-"):
            continue
        file_hits = 0

        def _downgrade_seg(seg: str) -> str:
            nonlocal file_hits

            def _wiki_repl(m: re.Match) -> str:
                nonlocal file_hits
                t, _anchor, alias = _split_inner(m.group(2))
                if not t or not _match_candidate_to_targets(t, ref_file, vault_dir, target_set, bare_names):
                    return m.group(0)
                file_hits += 1
                return alias or _basename_of(t)

            seg2 = _WIKILINK_RE.sub(_wiki_repl, seg)

            def _md_repl(m: re.Match) -> str:
                nonlocal file_hits
                label = m.group(2)
                raw = m.group(3)
                inner = raw[1:-1] if raw.startswith("<") else raw
                target_head = inner.split("#", 1)[0].strip()
                target_url = (target_head if raw.startswith("<") else target_head.split()[0]) if target_head else ""
                if not target_url or not _match_candidate_to_targets(target_url, ref_file, vault_dir, target_set, bare_names):
                    return m.group(0)
                file_hits += 1
                return label.strip() or _basename_of(target_url)

            return re.sub(r"(!?)\[([^\]]*)\]\(\s*(<[^>]+>|[^)\s]+(?:\s+[\"'][^\"']*[\"'])?)\s*\)", _md_repl, seg2)

        new_txt = _rewrite_prose(old_txt, _downgrade_seg)
        if file_hits > 0 and new_txt != old_txt:
            if new_txt.startswith("---"):
                new_txt = replace_frontmatter_field(new_txt, "updated", _today())
            _atomic_write_md(ref_file, new_txt)
            files_updated += 1
            links_downgraded += file_hits

    return {
        "files_updated": files_updated,
        "links_downgraded": links_downgraded,
        "canvas_nodes_downgraded": canvas_nodes_downgraded,
    }


def _alias_suffix(alias: str, stems: Tuple[str, ...]) -> str:
    if not alias or alias in stems:
        return ""
    return "|" + alias


def _rewrite_prose(text: str, rewriter: Callable[[str], str]) -> str:
    header = ""
    match = re.match(r"\A---[^\S\n]*\r?\n.*?\r?\n---[^\S\n]*(?:\r?\n|$)", text, re.S)
    if match:
        header = rewriter(match.group(0))
        text = text[match.end() :]
    out: List[str] = []
    prose: List[str] = []
    inline_re = re.compile(r"(?<!`)(?P<tick>`+)(?!`)(?P<code>.*?)(?<!`)(?P=tick)(?!`)", re.S)

    def flush() -> None:
        if not prose:
            return
        content = "".join(prose)
        start = 0
        for item in inline_re.finditer(content):
            out.append(rewriter(content[start : item.start()]))
            out.append(item.group(0))
            start = item.end()
        out.append(rewriter(content[start:]))
        prose.clear()

    from notes.atlas_markdown_blocks import code_line_mask
    for line, is_code in zip(text.splitlines(keepends=True), code_line_mask(text)):
        if is_code:
            flush()
            out.append(line)
        else:
            prose.append(line)
    flush()
    return header + "".join(out)


def _rewrite_moved_links(text: str, source_before: str, source_after: str, moves: Dict[str, str]) -> str:
    """Resolve in the old location, map once, then render in the new location."""
    from urllib.parse import quote, unquote, urlsplit

    def rewrite(target: str, wiki: bool) -> Optional[str]:
        decoded = unquote(target)
        if not decoded or decoded.startswith(("#", "/")) or urlsplit(decoded).scheme:
            return None
        explicit_relative = decoded.startswith(("./", "../"))
        vault_relative = decoded.split("/", 1)[0] in _TOPS
        # Bare wikilinks use Obsidian's vault-wide resolution, not the source directory.
        if wiki and not explicit_relative:
            resolved = decoded
        elif vault_relative:
            resolved = decoded
        else:
            resolved = os.path.normpath((Path(source_before).parent / decoded).as_posix())
        extensionless = wiki and not resolved.endswith(".md")
        key = resolved + ".md" if extensionless and resolved + ".md" in moves else resolved
        destination = moves.get(key, resolved)
        if key in moves and extensionless and destination.endswith(".md"):
            destination = destination[:-3]
        relative = explicit_relative or (not wiki and not vault_relative)
        if key not in moves and (not relative or source_before == source_after):
            return None
        if relative:
            destination = os.path.relpath(destination, Path(source_after).parent.as_posix())
            if explicit_relative and not destination.startswith("."):
                destination = "./" + destination
        return quote(destination, safe="/.-_") if "%" in target else destination

    def wiki(m: re.Match) -> str:
        target, anchor, alias = _split_inner(m.group(2))
        updated = rewrite(target, True)
        if updated is None:
            return m.group(0)
        return f"{m.group(1)}[[{updated}{anchor}{'|' + alias if alias else ''}]]"

    text = _WIKILINK_RE.sub(wiki, text)

    def md(m: re.Match) -> str:
        raw = m.group(2)
        inner = raw[1:-1] if raw.startswith("<") else raw
        target, sep, anchor = inner.partition("#")
        updated = rewrite(target, False)
        if updated is None:
            return m.group(0)
        updated += sep + anchor
        if raw.startswith("<"):
            updated = "<" + updated + ">"
        elif " " in updated:
            updated = quote(updated, safe="/.-_#")
        return m.group(1) + updated

    return re.sub(r"(!?\[[^\]]*\]\(\s*)(<[^>]+>|[^)\s]+)", md, text)


def _rewrite_relative_links(text: str, source_rel: str, old_rel: str, new_rel: str) -> str:
    before = old_rel if source_rel == new_rel else source_rel
    return _rewrite_moved_links(text, before, source_rel, {old_rel: new_rel})


def _has_bare_reference(text: str, stem: str) -> bool:
    for m in _WIKILINK_RE.finditer(text):
        t, _, _ = _split_inner(m.group(2))
        if "/" not in t and _basename_of(t) == stem:
            return True
    mdlink_re = re.compile(rf"(\[[^\]]*\]\({_NOT_REMOTE}){re.escape(stem)}(\.md(?:#[^)\s]+)?(?:\s+[\"'][^\"']*[\"'])?\))")
    if mdlink_re.search(text):
        return True
    lines = text.split("\n")
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is not None:
            key_re = re.compile(rf'^(\s*(?:{"|".join(_REF_FIELDS)})\s*:\s*["\']?){re.escape(stem)}(\.md)?(["\']?\s*(?:#.*)?)$')
            for i in range(1, end):
                if key_re.match(lines[i]):
                    return True
    return False


def _rewrite_content_move(text: str, old_stem: str, new_stem: str, old_dir: str, new_dir: str, allow_short: bool) -> str:
    stem_changed = old_stem != new_stem
    dir_changed = old_dir != new_dir
    if not allow_short and _has_bare_reference(text, old_stem):
        raise VaultStoreError("E_AMBIGUOUS_LINK", f"存在同名笔记短链引用 [[{old_stem}]]，无法安全歧义消解，请先改写为显式路径", exit_code=1)
    need_stem = stem_changed and old_stem in text
    need_path = bool(old_dir) and (dir_changed or stem_changed) and old_dir in text
    if not need_stem and not need_path:
        return text

    if need_path:
        def _path_repl(m: re.Match) -> str:
            t, anchor, alias = _split_inner(m.group(2))
            if "/" not in t or t.rpartition("/")[0] != old_dir or _basename_of(t) != old_stem:
                return m.group(0)
            suffix = ".md" if t.endswith(".md") else ""
            return f"{m.group(1)}[[{new_dir}/{new_stem}{suffix}{anchor}{_alias_suffix(alias, (old_stem, new_stem))}]]"

        text = _WIKILINK_RE.sub(_path_repl, text)
        md_path_re = re.compile(
            rf"(\[[^\]]*\]\({_NOT_REMOTE}){re.escape(old_dir)}/{re.escape(old_stem)}(\.md(?:#[^)\s]+)?(?:\s+[\"'][^\"']*[\"'])?\))"
        )
        text = md_path_re.sub(lambda m: f"{m.group(1)}{new_dir}/{new_stem}{m.group(2)}", text)

    if need_stem:
        def _base_repl(m: re.Match) -> str:
            t, anchor, alias = _split_inner(m.group(2))
            if "/" in t or _basename_of(t) != old_stem:
                return m.group(0)
            suffix = ".md" if t.endswith(".md") else ""
            return f"{m.group(1)}[[{new_stem}{suffix}{anchor}{_alias_suffix(alias, (old_stem, new_stem))}]]"

        text = _WIKILINK_RE.sub(_base_repl, text)
        md_base_re = re.compile(rf"(\[[^\]]*\]\({_NOT_REMOTE}){re.escape(old_stem)}(\.md(?:#[^)\s]+)?(?:\s+[\"'][^\"']*[\"'])?\))")
        text = md_base_re.sub(lambda m: f"{m.group(1)}{new_stem}{m.group(2)}", text)

    if not stem_changed:
        return text

    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return text
    fm_end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if fm_end is None:
        return text

    key_re = re.compile(rf'^(\s*(?:{"|".join(_REF_FIELDS)})\s*:\s*["\']?){re.escape(old_stem)}(\.md)?(["\']?\s*(?:#.*)?)$')
    item_re = re.compile(rf'^(\s*-\s*["\']?){re.escape(old_stem)}(\.md)?(["\']?\s*(?:#.*)?)$')
    field_head = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*:")
    in_ref_list = False
    changed = False
    for i in range(1, fm_end):
        line = lines[i]
        head = field_head.match(line)
        if head:
            in_ref_list = head.group(1) in _REF_LIST_FIELDS
            new_line = key_re.sub(lambda m: f"{m.group(1)}{new_stem}{m.group(2) or ''}{m.group(3)}", line)
        elif in_ref_list and line.lstrip().startswith("-"):
            new_line = item_re.sub(lambda m: f"{m.group(1)}{new_stem}{m.group(2) or ''}{m.group(3)}", line)
        else:
            new_line = line
        if new_line != line:
            lines[i] = new_line
            changed = True
    return "\n".join(lines) if changed else text


def _same_stem_paths(vault_dir: Path, stem: str) -> List[str]:
    return [rel for rel in _scan_vault_markdowns(vault_dir) if Path(rel).stem == stem]


def _cascade_moves_with_rollback(vault_dir: Path, moves: Dict[str, str]) -> int:
    moves = {old: new for old, new in moves.items() if old != new}
    if not moves:
        return 0
    inverse = {new: old for old, new in moves.items()}
    all_md = _scan_vault_markdowns(vault_dir)
    plan: List[Tuple[Path, bytes, str]] = []
    for rel in all_md:
        parts = Path(rel).parts
        if parts and parts[0] == "05-Journal":
            continue
        if _is_rel_frozen(vault_dir, rel):
            continue
        path = vault_dir / rel
        before = path.read_bytes()

        def rewrite(segment: str) -> str:
            updated = _rewrite_moved_links(segment, inverse.get(rel, rel), rel, moves)
            for old, new in moves.items():
                old_p, new_p = Path(old), Path(new)
                if old_p.suffix != ".md" or old_p.stem == new_p.stem:
                    continue
                remaining = [p for p in all_md if Path(p).stem == old_p.stem and p not in inverse]
                destinations = [p for p in all_md if Path(p).stem == new_p.stem and p != new]
                if destinations and (_has_bare_reference(segment, old_p.stem) or _has_bare_reference(segment, new_p.stem)):
                    raise VaultStoreError("E_AMBIGUOUS_LINK", "目标名存在重名短链冲突，请先改为显式路径", exit_code=1)
                updated = _rewrite_content_move(updated, old_p.stem, new_p.stem, "", "", allow_short=not remaining)
            return updated

        updated = _rewrite_prose(before.decode("utf-8"), rewrite)
        if updated.encode("utf-8") != before:
            plan.append((path, before, updated))

    # Canvas file references are vault-relative. Update only exact mapped file nodes.
    for path in _iter_vault_ref_files(vault_dir):
        if path.suffix != ".canvas":
            continue
        before = path.read_bytes()
        doc = json.loads(before.decode("utf-8"))
        changed = False
        for node in doc.get("nodes", []):
            if node.get("type") == "file" and node.get("file") in moves:
                node["file"] = moves[node["file"]]
                changed = True
        if changed:
            plan.append((path, before, json.dumps(doc, ensure_ascii=False, indent=2) + "\n"))

    written: List[Tuple[Path, bytes]] = []
    try:
        for path, before, updated in plan:
            written.append((path, before))
            _atomic_write_md(path, updated)
    except Exception:
        for path, before in reversed(written):
            _atomic_write_md(path, before.decode("utf-8"))
        raise
    return len(written)


def _cascade_move_with_rollback(vault_dir: Path, old_rel: str, new_rel: str) -> int:
    return _cascade_moves_with_rollback(vault_dir, {old_rel: new_rel})


def vfs_create(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    proj_reg: KekProjectRegistry,
    kek: bytes,
    app_id: str,
    zone: str,
    title: str,
    content: str = "",
    doc_type: str = "",
    summary: str = "",
    tags: Any = None,
    project_ref: Optional[str] = None,
    subdir: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    clean_title = (title or "").strip()
    if not clean_title:
        raise VaultStoreError("E_PARAM_MISSING", "vfs-create 需要非空 --title", exit_code=1)
    slug = _slugify_filename(clean_title)
    if zone in ("journal", "dropzone", "other"):
        domain = resolve_domain_scope(vault_dir=vault_dir, vault_ref=vault_ref, proj_reg=proj_reg, explicit_project_ref=project_ref)
    else:
        domain = resolve_domain_scope(vault_dir=vault_dir, vault_ref=vault_ref, proj_reg=proj_reg, explicit_project_ref=project_ref)

    if zone == "journal":
        target_dir = vault_dir / "05-Journal"
    elif zone == "dropzone":
        target_dir = vault_dir / "00-DropZone"
    elif zone in DOMAIN_COMPARTMENTS:
        target_dir = resolve_zone_dir(vault_dir, domain, zone)
    else:
        raise VaultStoreError("E_PARAM_INVALID", f"不支持的 VFS 分区: {zone}", exit_code=1)

    if subdir is not None and str(subdir).strip():
        raw_sub = str(subdir).strip().strip("/")
        sub_parts = [p for p in raw_sub.split("/") if p]
        if not sub_parts or "\\" in raw_sub or any(p in (".", "..") or p.startswith(".") for p in sub_parts):
            raise VaultStoreError("E_PARAM_INVALID", f"非法子目录参数 --subdir: {subdir}", exit_code=1)
        if any(
            p.lower()
            in {
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
            }
            for p in sub_parts
        ):
            raise VaultStoreError(
                "E_PARAM_INVALID",
                f"--subdir 禁止使用系统保留分区名 {subdir}（工作台目录请使用 workbench-workspace --agent <name>）",
                exit_code=1,
            )
        target_dir = (target_dir / "/".join(sub_parts)).resolve()
        if not target_dir.is_relative_to(vault_dir.resolve()):
            raise VaultStoreError("E_VFS_STRUCTURAL", "禁止越界创建笔记", exit_code=1)

    dst = target_dir / f"{slug}.md"
    dst_rel = validate_note_rel_path(dst.relative_to(vault_dir).as_posix())
    _validate_destination_topology(vault_dir, dst_rel)

    eff_doc_type = doc_type.strip() or DEFAULT_DOC_TYPE_BY_ZONE.get(zone, "fleeting")
    raw_txt = content or ""
    if raw_txt.lstrip().startswith("---\n"):
        final_md = raw_txt
    else:
        final_md = _render_note_md(
            doc_type=eff_doc_type,
            title=clean_title,
            summary=summary or clean_title,
            status="active" if domain["default_mode"] == "auto" else "pending",
            tags=[str(x).strip() for x in (tags.split(",") if isinstance(tags, str) else (tags or [])) if str(x).strip()],
            aliases=[],
            updated=_today(),
            body=raw_txt or f"# {clean_title}\n",
        )
        if domain["scope"] == "project":
            final_md = replace_frontmatter_field(final_md, "project_id", json.dumps(domain["project_id"], ensure_ascii=False))
    _atomic_create_excl(dst, final_md)

    all_md = _scan_vault_markdowns(vault_dir)
    n_map = note_reg.sync_paths(vault_ref, all_md)
    nref = n_map[dst_rel]
    meta, _, _ = extract_frontmatter(final_md)
    return _public_note(kek=kek, app_id=app_id, vault_ref=vault_ref, note_ref=nref, rel=dst_rel, meta=meta), [dst_rel]


def vfs_move(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    proj_reg: KekProjectRegistry,
    kek: bytes,
    app_id: str,
    note_ref: Optional[str] = None,
    target_zone: str = "",
    project_ref: Optional[str] = None,
    scope: str = "auto",
    new_title: Optional[str] = None,
    source_zone: Optional[str] = None,
    subdir: Optional[str] = None,
    target_subdir: Optional[str] = None,
    on_links: str = "cascade",
) -> Tuple[Dict[str, Any], List[str]]:
    link_mode = (on_links or "cascade").strip().lower()
    if link_mode not in ("cascade", "downgrade"):
        raise VaultStoreError("E_PARAM_INVALID", "--on_links 仅支持 cascade 或 downgrade", exit_code=1)

    if source_zone and subdir:
        if source_zone.strip().lower() == "journal":
            raise VaultStoreError("E_JOURNAL_PROTECTED", "日记（05-Journal）只归档不迁出、不删除，请改用 journal-archive 按 YYYY/MM/ 月度归档", exit_code=1)
        src_dir, src_rel, _src_dom = _resolve_logical_subdir(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            proj_reg=proj_reg,
            zone=source_zone,
            subdir=subdir,
            project_ref=project_ref,
            scope=scope,
            must_exist=True,
        )
        tz = (target_zone or source_zone).strip().lower()
        if tz not in _ALLOWED_DIR_ZONES:
            raise VaultStoreError(
                "E_PARAM_INVALID",
                f"目录迁徙 --target_zone 仅支持: {', '.join(_ALLOWED_DIR_ZONES)}",
                exit_code=1,
            )
        domain = resolve_domain_scope(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            proj_reg=proj_reg,
            explicit_project_ref=project_ref,
            scope=scope,
            source_rel_path=src_rel,
        )
        if target_subdir and target_subdir.strip():
            clean_tsub = target_subdir.strip().strip("/")
        elif new_title and new_title.strip():
            parent_sub = "/".join(subdir.strip().strip("/").split("/")[:-1]) if tz == source_zone.strip().lower() else ""
            slug_dir = _slugify_filename(new_title.strip())
            clean_tsub = f"{parent_sub}/{slug_dir}" if parent_sub else slug_dir
        elif tz != source_zone.strip().lower() or project_ref or (scope or "auto").strip().lower() != "auto":
            clean_tsub = src_dir.name
        else:
            raise VaultStoreError("E_PARAM_MISSING", "目录搬迁/改名需要 --target_zone、--target_subdir 或 --title", exit_code=1)

        t_parts = [p for p in clean_tsub.split("/") if p]
        if not t_parts or "\\" in clean_tsub or any(p in (".", "..") or p.startswith(".") for p in t_parts):
            raise VaultStoreError("E_PARAM_INVALID", f"非法目标子目录: {clean_tsub}", exit_code=1)
        if tz == "dropzone":
            dst_base = vault_dir / "00-DropZone"
        elif tz == "archives":
            dst_base = resolve_archive_dir(vault_dir, domain)
        else:
            dst_base = resolve_zone_dir(vault_dir, domain, tz)
        dst_dir = (dst_base / "/".join(t_parts)).resolve()
        if not dst_dir.is_relative_to(dst_base.resolve()) or dst_dir == dst_base.resolve():
            raise VaultStoreError("E_VFS_TOPOLOGY", "目标子目录越界", exit_code=1)
        if dst_dir == src_dir.resolve():
            raise VaultStoreError("E_PARAM_INVALID", "源目录与目标目录完全一致，无需移动", exit_code=1)
        if dst_dir.is_relative_to(src_dir.resolve()):
            raise VaultStoreError("E_PARAM_INVALID", "禁止将文件夹移动至自身子目录内", exit_code=1)
        if dst_dir.exists():
            raise VaultStoreError("E_COLLISION", "目标位置已存在同名文件夹", exit_code=1)
        new_dir_rel = dst_dir.relative_to(vault_dir.resolve()).as_posix()
        _validate_destination_topology(vault_dir, f"{new_dir_rel}/_", src_rel=f"{src_rel}/_")

        src_files = sorted(
            p.relative_to(vault_dir.resolve()).as_posix()
            for p in src_dir.rglob("*")
            if p.is_file() and not p.is_symlink()
        )
        from notes.atlas_note_engine import _preflight_note_moves
        _preflight_note_moves(vault_dir, {
            rel: f"{new_dir_rel}/{Path(rel).relative_to(src_rel).as_posix()}" for rel in src_files
        })
        incoming = _scan_incoming_references_for_targets(vault_dir, note_reg, vault_ref, src_files)
        downgrade_stats = {"files_updated": 0, "links_downgraded": 0, "canvas_nodes_downgraded": 0}
        if link_mode == "downgrade" and incoming:
            downgrade_stats = _downgrade_refs_in_vault(vault_dir, src_files)
        elif any(h["kind"] == "canvas" for h in incoming):
            raise VaultStoreError("E_VFS_REFERENCED", "存在 .canvas 画布引用该目录内容，请改用 --on_links downgrade 降级后移动", exit_code=1)

        dst_dir.parent.mkdir(parents=True, exist_ok=True)
        # Keep originals so failures in metadata/link rewriting restore source bytes too.
        source_bytes = {rel: (vault_dir / rel).read_bytes() for rel in src_files}
        shutil.move(str(src_dir), str(dst_dir))
        cascaded = 0
        try:
            moved_pairs: List[Tuple[str, str]] = []
            for old_frel in src_files:
                sub_f = Path(old_frel).relative_to(src_rel).as_posix()
                new_frel = f"{new_dir_rel}/{sub_f}"
                if old_frel.endswith(".md"):
                    moved_pairs.append((old_frel, new_frel))
                    nf_path = vault_dir / new_frel
                    cur_txt = nf_path.read_text(encoding="utf-8")
                    _, _, has_fm = extract_frontmatter(cur_txt)
                    if has_fm and domain["scope"] == "project":
                        cur_txt = replace_frontmatter_field(cur_txt, "project_id", json.dumps(domain["project_id"], ensure_ascii=False))
                        cur_txt = replace_frontmatter_field(cur_txt, "updated", _today())
                        _atomic_write_md(nf_path, cur_txt)
            if link_mode == "cascade":
                cascaded = _cascade_moves_with_rollback(vault_dir, {
                    rel: f"{new_dir_rel}/{Path(rel).relative_to(src_rel).as_posix()}" for rel in src_files
                })
        except Exception:
            shutil.move(str(dst_dir), str(src_dir))
            for rel, raw in source_bytes.items():
                (vault_dir / rel).write_bytes(raw)
            raise

        note_reg.rebase_prefix(vault_ref, src_rel, new_dir_rel)
        all_md = _scan_vault_markdowns(vault_dir)
        note_reg.sync_paths(vault_ref, all_md)
        return {
            "vault_ref": vault_ref,
            "mode": "folder-moved",
            "folder_name": dst_dir.name,
            "target_zone": tz,
            "domain_scope": domain["scope"],
            "moved_files_count": len(src_files),
            "cascaded_count": cascaded,
            "downgraded_files_count": downgrade_stats["files_updated"],
            "downgraded_links_count": downgrade_stats["links_downgraded"],
            "path_sealed": True,
        }, [src_rel, new_dir_rel, *src_files]

    if not note_ref:
        raise VaultStoreError("E_PARAM_MISSING", "vfs-move 需要 --note_ref，或传入 --zone 与 --subdir 操作文件夹", exit_code=1)

    rec = note_reg.resolve_handle(vault_ref, note_ref)
    old_rel = str(rec["note_rel_path"])
    if old_rel.startswith("05-Journal/"):
        raise VaultStoreError("E_JOURNAL_PROTECTED", "日记（05-Journal）只归档不迁出、不删除，请改用 journal-archive 按 YYYY/MM/ 月度归档", exit_code=1)
    src = vault_dir / old_rel
    if not src.is_file():
        raise VaultStoreError("E_NOT_FOUND", f"源笔记不存在: {note_ref}", exit_code=1)
    _require_unfrozen(vault_dir, old_rel)

    incoming = _scan_incoming_references(vault_dir, note_reg, vault_ref, old_rel)
    if link_mode == "cascade" and any(h["kind"] == "canvas" for h in incoming):
        raise VaultStoreError("E_VFS_REFERENCED", "存在 .canvas 画布直接引用，VFS 暂不支持自动改写画布路径，请改用 --on_links downgrade 或先解除引用", exit_code=1)

    domain = resolve_domain_scope(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        proj_reg=proj_reg,
        explicit_project_ref=project_ref,
        scope=scope,
        source_rel_path=old_rel,
    )
    tz = (target_zone or "").strip().lower()
    if not tz and (new_title or project_ref):
        inferred_zone, _ = infer_zone_and_project_rel(old_rel)
        if inferred_zone in ("dropzone", *DOMAIN_COMPARTMENTS.keys()):
            tz = inferred_zone
    if tz not in ("dropzone", *DOMAIN_COMPARTMENTS.keys()):
        raise VaultStoreError("E_PARAM_INVALID", f"--target_zone 仅支持: dropzone, {', '.join(DOMAIN_COMPARTMENTS)}", exit_code=1)
    base_dst_dir = vault_dir / "00-DropZone" if tz == "dropzone" else resolve_zone_dir(vault_dir, domain, tz)
    if target_subdir and target_subdir.strip():
        raw_tsub = target_subdir.strip().strip("/")
        t_parts = [p for p in raw_tsub.split("/") if p]
        if not t_parts or "\\" in raw_tsub or any(p in (".", "..") or p.startswith(".") for p in t_parts):
            raise VaultStoreError("E_PARAM_INVALID", f"非法目标子目录: {raw_tsub}", exit_code=1)
        from notes.atlas_note_engine import _RESERVED_SUBDIR_NAMES
        if any(p.lower() in _RESERVED_SUBDIR_NAMES for p in t_parts):
            raise VaultStoreError(
                "E_PARAM_INVALID",
                f"--target_subdir 禁止使用系统保留分区名 {target_subdir}（工作台目录请使用 workbench-workspace --agent <name>）",
                exit_code=1,
            )
        dst_dir = (base_dst_dir / "/".join(t_parts)).resolve()
        if not dst_dir.is_relative_to(base_dst_dir.resolve()):
            raise VaultStoreError("E_VFS_TOPOLOGY", "目标子目录越界", exit_code=1)
    else:
        dst_dir = base_dst_dir
    stem = _slugify_filename(new_title.strip()) if (new_title and new_title.strip()) else src.stem
    dst = dst_dir / f"{stem}.md"
    new_rel = validate_note_rel_path(dst.relative_to(vault_dir).as_posix())
    if new_rel == old_rel:
        raise VaultStoreError("E_PARAM_INVALID", "源与目标完全一致，无需移动", exit_code=1)
    if dst.exists():
        raise VaultStoreError("E_COLLISION", "目标位置已存在同名笔记", exit_code=1)
    _validate_destination_topology(vault_dir, new_rel, src_rel=old_rel)

    from notes.atlas_note_engine import _preflight_note_moves
    _preflight_note_moves(vault_dir, {old_rel: new_rel})
    downgrade_stats = {"files_updated": 0, "links_downgraded": 0, "canvas_nodes_downgraded": 0}
    if link_mode == "downgrade" and incoming:
        downgrade_stats = _downgrade_refs_in_vault(vault_dir, [old_rel])

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    cascaded = 0
    if link_mode == "cascade":
        try:
            cascaded = _cascade_move_with_rollback(vault_dir, old_rel, new_rel)
        except Exception:
            shutil.move(str(dst), str(src))
            raise

    if new_title and new_title.strip():
        cur_txt = dst.read_text(encoding="utf-8")
        cur_txt = replace_frontmatter_field(cur_txt, "title", json.dumps(new_title.strip(), ensure_ascii=False))
        cur_txt = replace_frontmatter_field(cur_txt, "updated", _today())
        _atomic_write_md(dst, cur_txt)
    if domain["scope"] == "project":
        cur_txt = dst.read_text(encoding="utf-8")
        _, _, has_fm = extract_frontmatter(cur_txt)
        if has_fm:
            cur_txt = replace_frontmatter_field(cur_txt, "project_id", json.dumps(domain["project_id"], ensure_ascii=False))
            _atomic_write_md(dst, cur_txt)

    note_reg.move_handle(vault_ref, note_ref, new_rel)
    meta, _, _ = extract_frontmatter(dst.read_text(encoding="utf-8"))
    pub = _public_note(kek=kek, app_id=app_id, vault_ref=vault_ref, note_ref=note_ref, rel=new_rel, meta=meta)
    pub["cascaded_count"] = cascaded
    pub["downgraded_files_count"] = downgrade_stats["files_updated"]
    pub["downgraded_links_count"] = downgrade_stats["links_downgraded"]
    return pub, [old_rel, new_rel]


def vfs_copy(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    proj_reg: KekProjectRegistry,
    kek: bytes,
    app_id: str,
    note_ref: str,
    target_zone: str,
    project_ref: Optional[str] = None,
    scope: str = "auto",
    new_title: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    rec = note_reg.resolve_handle(vault_ref, note_ref)
    old_rel = str(rec["note_rel_path"])
    if old_rel.startswith("05-Journal/"):
        raise VaultStoreError("E_JOURNAL_PROTECTED", "日记（05-Journal）仅归档且不整篇外拷入库，请显式挑选内容另存为素材", exit_code=1)
    src = vault_dir / old_rel
    if not src.is_file():
        raise VaultStoreError("E_NOT_FOUND", f"源笔记不存在: {note_ref}", exit_code=1)

    domain = resolve_domain_scope(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        proj_reg=proj_reg,
        explicit_project_ref=project_ref,
        scope=scope,
        source_rel_path=old_rel,
    )
    tz = (target_zone or "").strip().lower()
    if not tz and (new_title or project_ref):
        inferred_zone, _ = infer_zone_and_project_rel(old_rel)
        if inferred_zone in ("dropzone", *DOMAIN_COMPARTMENTS.keys()):
            tz = inferred_zone
    if tz not in ("dropzone", *DOMAIN_COMPARTMENTS.keys()):
        raise VaultStoreError("E_PARAM_INVALID", f"--target_zone 仅支持: dropzone, {', '.join(DOMAIN_COMPARTMENTS)}", exit_code=1)
    dst_dir = vault_dir / "00-DropZone" if tz == "dropzone" else resolve_zone_dir(vault_dir, domain, tz)
    stem = _slugify_filename(new_title.strip()) if (new_title and new_title.strip()) else f"{src.stem}_copy"
    dst = dst_dir / f"{stem}.md"
    new_rel = validate_note_rel_path(dst.relative_to(vault_dir).as_posix())
    _validate_destination_topology(vault_dir, new_rel, src_rel=None)

    txt = src.read_text(encoding="utf-8")
    if new_title and new_title.strip():
        txt = replace_frontmatter_field(txt, "title", json.dumps(new_title.strip(), ensure_ascii=False))
    txt = replace_frontmatter_field(txt, "updated", _today())
    _atomic_create_excl(dst, txt)

    all_md = _scan_vault_markdowns(vault_dir)
    n_map = note_reg.sync_paths(vault_ref, all_md)
    new_ref = n_map[new_rel]
    meta, _, _ = extract_frontmatter(txt)
    pub = _public_note(kek=kek, app_id=app_id, vault_ref=vault_ref, note_ref=new_ref, rel=new_rel, meta=meta)
    pub["copied_from_ref"] = note_ref
    return pub, [old_rel, new_rel]


def vfs_trash(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    trash_reg: KekTrashRegistry,
    proj_reg: Optional[KekProjectRegistry] = None,
    note_ref: Optional[str] = None,
    file_name: Optional[str] = None,
    zone: Optional[str] = None,
    subdir: Optional[str] = None,
    project_ref: Optional[str] = None,
    scope: str = "auto",
    on_links: str = "",
    force: bool = False,
    apply: bool = False,
) -> Tuple[Dict[str, Any], List[str]]:
    sealed: List[str] = []
    target_rels: List[str] = []
    if subdir:
        if not zone:
            raise VaultStoreError("E_PARAM_MISSING", "删除目录时需同时指定 --zone 与 --subdir", exit_code=1)
        if str(zone).strip().lower() == "journal":
            raise VaultStoreError("E_JOURNAL_PROTECTED", "日记（05-Journal）只归档不删除，严禁软删或移入废纸篓；请改用 journal-archive 按 YYYY/MM/ 月度归档", exit_code=1)
        if proj_reg is None:
            raise VaultStoreError("E_INTERNAL", "目录软删缺少 KekProjectRegistry 实例", exit_code=1)
        src, rel, _ = _resolve_logical_subdir(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            proj_reg=proj_reg,
            zone=zone,
            subdir=subdir,
            project_ref=project_ref,
            scope=scope,
            must_exist=True,
        )
        zone = (zone or "").strip().lower()
        kind = "dir"
        target_rels = [
            p.relative_to(vault_dir).as_posix()
            for p in sorted(src.rglob("*"))
            if p.is_file() and not p.is_symlink()
        ]
        sealed.extend(target_rels)
    elif note_ref:
        rec = note_reg.resolve_handle(vault_ref, note_ref)
        rel = str(rec["note_rel_path"])
        src = vault_dir / rel
        if not src.is_file():
            raise VaultStoreError("E_NOT_FOUND", f"笔记不存在: {note_ref}", exit_code=1)
        zone, _ = infer_zone_and_project_rel(rel)
        kind = "note"
        target_rels = [rel]
    elif file_name:
        if "/" in file_name or "\\" in file_name or ".." in file_name:
            raise VaultStoreError("E_PARAM_INVALID", "--file_name 仅允许纯文件名", exit_code=1)
        matches = []
        for sub in ("99-Assets/10-Attachments", "00-DropZone"):
            base = vault_dir / sub
            if base.is_dir():
                for p in sorted(base.rglob("*")):
                    if not p.is_symlink() and p.is_file() and p.name == file_name:
                        matches.append(p)
        if not matches:
            raise VaultStoreError("E_NOT_FOUND", f"未找到待软删文件: {file_name}", exit_code=1)
        if len(matches) > 1:
            raise VaultStoreError("E_AMBIGUOUS", f"存在多个同名文件，请改用逻辑句柄或先归并: {file_name}", exit_code=1)
        src = matches[0]
        rel = src.relative_to(vault_dir).as_posix()
        zone = "attachment" if rel.startswith("99-Assets/") else "dropzone"
        kind = "file"
        target_rels = [rel]
    else:
        raise VaultStoreError("E_PARAM_MISSING", "vfs-trash 需要 --note_ref <nte_01>、--file_name <文件名> 或 --zone <区> --subdir <目录>", exit_code=1)

    sealed.append(rel)
    if (zone or "").strip().lower() == "journal" or rel == "05-Journal" or rel.startswith("05-Journal/"):
        raise VaultStoreError("E_JOURNAL_PROTECTED", "日记（05-Journal）只归档不删除，严禁软删或移入废纸篓；请改用 journal-archive 按 YYYY/MM/ 月度归档", exit_code=1)
    if kind == "note":
        try:
            jmeta, _, _ = extract_frontmatter(src.read_text(encoding="utf-8", errors="replace"))
        except FrontmatterError:
            jmeta = {}
        if str(jmeta.get("doc_type") or "").strip().lower() == "journal":
            raise VaultStoreError("E_JOURNAL_PROTECTED", "journal 类型日记只归档不删除，严禁软删或移入废纸篓；请改用 journal-archive 按月归档", exit_code=1)
    _require_unfrozen(vault_dir, rel + ("/_" if kind == "dir" else ""))
    parts = Path(rel).parts
    if kind != "dir" and parts and parts[0] == "30-Projects" and Path(rel).name.lower().startswith("manifest"):
        raise VaultStoreError("E_VFS_STRUCTURAL", "课题清单一律使用 project 治理算子，禁止 VFS 直接软删除", exit_code=1)

    mode = (on_links or "").strip().lower()
    if not mode:
        mode = "keep" if force else "block"
    elif mode not in ("block", "downgrade", "keep"):
        raise VaultStoreError("E_PARAM_INVALID", f"--on_links 仅支持 block / downgrade / keep，收到: {on_links}", exit_code=1)

    refs = _scan_incoming_references_for_targets(vault_dir, note_reg, vault_ref, target_rels)
    if refs:
        if mode == "block":
            raise VaultStoreError(
                "E_VFS_REFERENCED",
                f"检测到 {len(refs)} 处双向链接/画布引用，已阻断软删以防死链（引用方: {', '.join((r.get('note_ref') or r['title']) for r in refs[:8])}）",
                exit_code=1,
            )
        if mode == "keep" and not apply:
            raise VaultStoreError("E_PARAM_MISSING", "强制忽略引用软删须同时传入 --force --apply true 明确确认", exit_code=1)
        if mode == "downgrade" and not apply:
            return {
                "requires_approval": True,
                "vault_ref": vault_ref,
                "kind": kind,
                "name": src.stem if kind == "note" else src.name,
                "orig_zone": zone,
                "on_links": mode,
                "target_file_count": len(target_rels),
                "referenced_count": len(refs),
                "references": refs[:50],
                "path_sealed": True,
            }, sealed

    downgrade_stats = {"files_updated": 0, "links_downgraded": 0, "canvas_nodes_downgraded": 0}
    if refs and mode == "downgrade":
        downgrade_stats = _downgrade_refs_in_vault(vault_dir, target_rels)

    if kind == "dir":
        size_bytes = sum(int(p.stat().st_size) for p in src.rglob("*") if p.is_file() and not p.is_symlink())
    else:
        size_bytes = int(src.stat().st_size)

    trash_dir = vault_dir / ".trash"
    trash_dir.mkdir(parents=True, exist_ok=True)
    dst = trash_dir / src.name
    n = 1
    while dst.exists():
        if kind == "dir" or not src.suffix:
            dst = trash_dir / f"{src.name}.{n}"
        else:
            dst = trash_dir / f"{src.stem}.{n}{src.suffix}"
        n += 1
    shutil.move(str(src), str(dst))
    trel = dst.relative_to(vault_dir).as_posix()
    sealed.append(trel)

    note_reg.sync_paths(vault_ref, _scan_vault_markdowns(vault_dir))
    entry = trash_reg.record_trashed(
        vault_ref=vault_ref,
        trash_rel=trel,
        orig_rel=rel,
        orig_zone=zone,
        display_name=src.stem if kind == "note" else src.name,
        kind=kind,
        size_bytes=size_bytes,
    )
    return {
        "requires_approval": False,
        "vault_ref": vault_ref,
        "trash_ref": entry["trash_ref"],
        "name": entry["name"],
        "kind": kind,
        "orig_zone": zone,
        "on_links": mode,
        "size_human": _format_size(size_bytes),
        "referenced_count": len(refs),
        "downgraded_files_count": downgrade_stats["files_updated"],
        "downgraded_links_count": downgrade_stats["links_downgraded"],
        "downgraded_canvas_nodes_count": downgrade_stats["canvas_nodes_downgraded"],
        "forced": bool(refs and mode == "keep" and apply),
        "path_sealed": True,
    }, sealed


def vfs_trash_list(
    *,
    vault_dir: Path,
    vault_ref: str,
    trash_reg: KekTrashRegistry,
    query: str = "",
    kind_filter: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
) -> Tuple[Dict[str, Any], List[str]]:
    """Read-only list of opaque handles in vault .trash (syncing KekTrashRegistry)."""
    rows = trash_reg.sync_vault(vault_dir, vault_ref)
    terms = [t for t in (query or "").casefold().split() if t]
    k_filter = (kind_filter or "").strip().lower() or None
    filtered: List[Dict[str, Any]] = []
    for r in rows:
        if k_filter and str(r.get("kind") or "").lower() != k_filter:
            continue
        hay = f"{r['name']} {r['orig_zone']} {r['kind']}".casefold()
        if all(t in hay for t in terms):
            filtered.append(r)
    filtered.sort(key=lambda r: (str(r.get("deleted_at") or ""), str(r["trash_ref"])), reverse=True)
    page = filtered[max(0, offset) : max(0, offset) + max(1, min(limit, 200))]
    items = [
        {
            "trash_ref": r["trash_ref"],
            "name": r["name"],
            "kind": r["kind"],
            "orig_zone": r.get("orig_zone") or "unknown",
            "size_bytes": int(r.get("size_bytes", 0)),
            "size_human": _format_size(int(r.get("size_bytes", 0))),
            "deleted_at": r.get("deleted_at"),
            "path_sealed": True,
        }
        for r in page
    ]
    return {
        "vault_ref": vault_ref,
        "mode": "list",
        "total": len(filtered),
        "count": len(items),
        "items": items,
        "path_sealed": True,
    }, [str(p) for r in rows for p in (r.get("trash_rel"), r.get("orig_rel")) if p]


def vfs_untrash(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    proj_reg: KekProjectRegistry,
    trash_reg: KekTrashRegistry,
    kek: bytes,
    app_id: str,
    trash_ref: Optional[str] = None,
    target_zone: Optional[str] = None,
    project_ref: Optional[str] = None,
    scope: str = "auto",
    query: str = "",
    offset: int = 0,
    limit: int = 20,
) -> Tuple[Dict[str, Any], List[str]]:
    if not trash_ref:
        rows = trash_reg.sync_vault(vault_dir, vault_ref)
        terms = [t for t in (query or "").casefold().split() if t]
        filtered = [r for r in rows if all(t in f"{r['name']} {r['orig_zone']} {r['kind']}".casefold() for t in terms)]
        filtered.sort(key=lambda r: (str(r.get("deleted_at") or ""), str(r["trash_ref"])), reverse=True)
        page = filtered[max(0, offset) : max(0, offset) + max(1, min(limit, 200))]
        items = [
            {
                "trash_ref": r["trash_ref"],
                "name": r["name"],
                "kind": r["kind"],
                "orig_zone": r.get("orig_zone") or "unknown",
                "size_human": _format_size(int(r.get("size_bytes", 0))),
                "deleted_at": r.get("deleted_at"),
                "path_sealed": True,
            }
            for r in page
        ]
        return {
            "vault_ref": vault_ref,
            "mode": "list",
            "total": len(filtered),
            "count": len(items),
            "items": items,
            "path_sealed": True,
        }, [str(r.get("trash_rel") or "") for r in rows]

    rec = trash_reg.resolve_handle(vault_dir, vault_ref, trash_ref)
    trel = str(rec["trash_rel"])
    src = vault_dir / trel
    kind = str(rec.get("kind") or "file")
    if not (src.is_dir() if kind == "dir" else src.is_file()):
        raise VaultStoreError("E_NOT_FOUND", f"回收站对象不存在: {trash_ref}", exit_code=1)

    tz = (target_zone or rec.get("orig_zone") or "dropzone").strip().lower()
    if kind == "note":
        domain = resolve_domain_scope(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            proj_reg=proj_reg,
            explicit_project_ref=project_ref,
            scope=scope,
            source_rel_path=str(rec.get("orig_rel") or "") or None,
        )
        if tz == "dropzone":
            dst_dir = vault_dir / "00-DropZone"
        elif tz in DOMAIN_COMPARTMENTS:
            dst_dir = resolve_zone_dir(vault_dir, domain, tz)
        else:
            raise VaultStoreError("E_PARAM_INVALID", f"不支持的恢复分区: {tz}", exit_code=1)
        clean_name = re.sub(r"\.\d+(?=\.md$|$)", "", src.name)
        if not clean_name.endswith(".md"):
            clean_name = f"{rec['name']}.md"
        dst = dst_dir / clean_name
    elif kind == "dir":
        if tz not in _ALLOWED_DIR_ZONES:
            raise VaultStoreError("E_PARAM_INVALID", f"目录恢复 --zone 仅支持: {', '.join(_ALLOWED_DIR_ZONES)}", exit_code=1)
        domain = resolve_domain_scope(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            proj_reg=proj_reg,
            explicit_project_ref=project_ref,
            scope=scope,
            source_rel_path=str(rec.get("orig_rel") or "") or None,
        )
        if tz == "dropzone":
            dst_dir = vault_dir / "00-DropZone"
        elif tz == "archives":
            dst_dir = resolve_archive_dir(vault_dir, domain)
        else:
            dst_dir = resolve_zone_dir(vault_dir, domain, tz)
        clean_name = str(rec["name"]).strip()
        dst = dst_dir / clean_name
    else:
        clean_name = rec["name"]
        dst_dir = vault_dir / ("99-Assets/10-Attachments" if tz in ("attachment", "attachments") else "00-DropZone")
        dst = dst_dir / clean_name

    dst_rel = dst.relative_to(vault_dir).as_posix()
    _validate_destination_topology(vault_dir, dst_rel + ("/_" if kind == "dir" else ""), is_untrash=True)
    if dst.exists():
        raise VaultStoreError("E_COLLISION", "目标位置已存在同名对象，拒绝恢复覆盖", exit_code=1)

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    trash_reg.sync_vault(vault_dir, vault_ref)

    out: Dict[str, Any] = {
        "vault_ref": vault_ref,
        "mode": "restored",
        "kind": kind,
        "trash_ref": trash_ref,
        "name": dst.stem if kind == "note" else dst.name,
        "restored_zone": tz,
        "path_sealed": True,
    }
    if kind == "dir":
        all_md = _scan_vault_markdowns(vault_dir)
        note_reg.sync_paths(vault_ref, all_md)
        out["link_review_hint"] = "已恢复目录至目标舱位；建议运行 vfs-links 核验双向链接连通性"
    elif dst.suffix.lower() == ".md":
        all_md = _scan_vault_markdowns(vault_dir)
        n_map = note_reg.sync_paths(vault_ref, all_md)
        out["note_ref"] = n_map.get(dst_rel)
        out["link_review_hint"] = "已恢复至目标舱位；建议运行 vfs-links 核验双向链接连通性"
    return out, [trel, dst_rel]


def vfs_validate(*, vault_dir: Path, vault_ref: str) -> Dict[str, Any]:
    required_dirs = tuple(get_vault_layout_spec()["required_dirs"])
    missing_skeleton = [d for d in required_dirs if not (vault_dir / d).is_dir()]
    symlink_count = 0
    flat_violations = 0
    for cur_root, dirs, files in os.walk(vault_dir, followlinks=False):
        cur = Path(cur_root)
        for name in dirs + files:
            p = cur / name
            if p.is_symlink():
                symlink_count += 1
        rel_cur = cur.relative_to(vault_dir).parts
        for flat in _FLAT_DIRS:
            if flat in rel_cur and len(rel_cur) > rel_cur.index(flat) + 1:
                flat_violations += 1
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__" and not (cur / d).is_symlink()]

    project_rels = scan_project_rels(vault_dir)
    frozen_projects = 0
    for prel in project_rels:
        m = read_domain_manifest(vault_dir / prel, Path(prel).name)
        if m.get("status") in ("deprecated", "archived"):
            frozen_projects += 1

    violations: List[Dict[str, Any]] = []
    if missing_skeleton:
        violations.append({"rule": "missing_skeleton", "count": len(missing_skeleton)})
    if symlink_count:
        violations.append({"rule": "symlink_forbidden", "count": symlink_count})
    if flat_violations:
        violations.append({"rule": "flat_dir_violation", "count": flat_violations})

    ok = not violations
    return {
        "vault_ref": vault_ref,
        "ok": ok,
        "valid": ok,
        "violations": violations,
        "missing_skeleton_count": len(missing_skeleton),
        "symlink_count": symlink_count,
        "flat_dir_violations": flat_violations,
        "project_count": len(project_rels),
        "frozen_project_count": frozen_projects,
        "path_sealed": True,
    }


def vfs_links(
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    note_ref: Optional[str] = None,
    kek: Optional[bytes] = None,
    app_id: Optional[str] = None,
    cache_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    cached_entries: Optional[Dict[str, Dict[str, Any]]] = None
    if cache_dir is not None and kek is not None and app_id is not None:
        from storage.note_index_cache import reconcile_note_index

        cached_entries, n_map = reconcile_note_index(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            cache_dir=cache_dir,
        )
        all_md = sorted(cached_entries.keys())
    else:
        all_md = _scan_vault_markdowns(vault_dir)
        n_map = note_reg.sync_paths(vault_ref, all_md)

    by_rel_no_md = {rel[:-3] if rel.endswith(".md") else rel: rel for rel in all_md}
    by_stem: Dict[str, List[str]] = {}
    for rel in all_md:
        by_stem.setdefault(Path(rel).stem, []).append(rel)
    att_names = (
        {p.name for p in (vault_dir / "99-Assets/10-Attachments").iterdir() if p.is_file()}
        if (vault_dir / "99-Assets/10-Attachments").is_dir()
        else set()
    )
    vault_res = vault_dir.resolve()

    if note_ref:
        rec = note_reg.resolve_handle(vault_ref, note_ref)
        target_rels = [str(rec["note_rel_path"])]
    else:
        target_rels = all_md

    broken: List[Dict[str, Any]] = []
    ambiguous: List[Dict[str, Any]] = []
    unique_targets: set[str] = set()
    total_links = 0

    for rel in target_rels:
        if note_ref is None and rel.startswith("05-Journal/"):
            continue
        fp = vault_dir / rel
        if cached_entries is not None and rel in cached_entries:
            cands = list(cached_entries[rel].get("vfs_cands") or [])
        else:
            if not fp.is_file():
                continue
            cands = _extract_file_candidates(fp, vault_dir)
        for c in cands:
            total_links += 1
            unique_targets.add(c)
            c_no_md = c[:-3] if c.endswith(".md") else c
            if c in by_rel_no_md or c_no_md in by_rel_no_md or Path(c).name in att_names:
                continue
            stem_hits = by_stem.get(_basename_of(c), [])
            if len(stem_hits) == 1:
                continue
            local = (fp.parent / c).resolve()
            resolved_local = None
            if local.is_relative_to(vault_res) and local.exists():
                resolved_local = local.relative_to(vault_res).as_posix()
            elif not local.suffix and (fp.parent / (c + ".md")).resolve().is_relative_to(vault_res) and (fp.parent / (c + ".md")).is_file():
                resolved_local = (fp.parent / (c + ".md")).resolve().relative_to(vault_res).as_posix()
            if resolved_local:
                continue
            if len(stem_hits) > 1 and "/" not in c:
                ambiguous.append({"source_note_ref": n_map.get(rel), "target_stem": _basename_of(c), "candidate_count": len(stem_hits)})
            else:
                broken.append({"source_note_ref": n_map.get(rel), "target_name": Path(c).name, "_src_rel": rel, "_raw": c})

    if broken:
        from notes.atlas_wikilink_tools import build_vault_link_index, suggest_targets_for_broken

        link_idx = build_vault_link_index(vault_dir, n_map)
        for item in broken[:50]:
            sugs = suggest_targets_for_broken(str(item.pop("_raw", "")), source_rel=str(item.pop("_src_rel", "")), index=link_idx)
            if sugs:
                item["suggestions"] = sugs
        for item in broken[50:]:
            item.pop("_raw", None)
            item.pop("_src_rel", None)

    return {
        "vault_ref": vault_ref,
        "scoped_note_ref": note_ref,
        "scanned_files": len(target_rels),
        "total_links": total_links,
        "unique_target_count": len(unique_targets),
        "dangling_count": len(broken),
        "broken_count": len(broken),
        "ambiguous_count": len(ambiguous),
        "broken": broken[:50],
        "ambiguous": ambiguous[:50],
        "path_sealed": True,
    }
