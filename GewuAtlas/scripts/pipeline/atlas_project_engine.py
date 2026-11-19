#!/usr/bin/env python3
"""Zero-path KEK-handle Project (课题/主题) engine for GewuAtlas (CRUD, merge with conflict-pause & workbench auto-rename, topic split, Ticket log & lossless undo)."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
from datetime import datetime
from storage.vault_volume import init_empty_workbench, initial_workbench_readme
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from facade.atlas_domain_scope import get_project_fractal_subdirs
from notes.atlas_note_engine import (
    _atomic_write_md,
    _cascade_note_moves,
    _preflight_note_moves,
    _parse_list_arg,
    _scan_vault_markdowns,
    _slugify_filename,
    _today,
    _update_frontmatter_preserving_extras,
)
from notes.atlas_yamlmini import FrontmatterError, extract_frontmatter, fm_scalar, validate_note_meta
from storage.note_kek_store import KekNoteRegistry
from storage.project_kek_store import KekProjectRegistry
from collab.ticket_bridge import record_project_operation_ticket
from storage.vault_kek_store import VaultStoreError

PROJECT_CONTAINERS = ("10-Artifacts", "20-Memory", "25-Slip", "30-Workbench")


class MergeConflictPause(Exception):
    """Raised before any disk mutation when merge finds collisions requiring user confirmation."""

    def __init__(self, conflicts: List[Dict[str, Any]], workbench_planned_renames: List[Dict[str, Any]]) -> None:
        super().__init__("检测到成果或记忆重名/重复冲突，需用户确认合并策略")
        self.conflicts = conflicts
        self.workbench_planned_renames = workbench_planned_renames


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_noexec_bytes(dst: Path, data: bytes) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.parent / f".{dst.name}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink()


def _project_seal(kek: bytes, app_id: str, vault_ref: str, project_ref: str, project_rel_path: str) -> str:
    msg = f"{app_id}|{vault_ref}|{project_ref}|{project_rel_path}".encode("utf-8")
    return "hmac-sha256:" + hmac.new(kek, msg, hashlib.sha256).hexdigest()[:16]


def _normalize_project_id(raw: str, fallback_title: str = "") -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", (raw or "").strip().lower()).strip("-_")
    if not base and fallback_title:
        base = re.sub(r"[^a-zA-Z0-9_-]+", "-", fallback_title.strip().lower()).strip("-_")
    if not base:
        base = f"prj-{secrets.token_hex(4)}"
    return base[:64]


def _scan_project_dirs(vault_dir: Path) -> List[str]:
    p_root = vault_dir / "30-Projects"
    if not p_root.is_dir():
        return []
    out: List[str] = []
    for child in sorted(p_root.iterdir()):
        if child.is_symlink() or not child.is_dir():
            continue
        if child.name.startswith("."):
            continue
        out.append(f"30-Projects/{child.name}")
    return out


def _find_manifest(proj_dir: Path) -> Optional[Path]:
    cands = sorted(
        p
        for p in proj_dir.glob("*.md")
        if p.is_file() and not p.is_symlink() and not p.name.startswith(".")
    )
    for c in cands:
        if c.name.lower().startswith("manifest"):
            return c
    for c in cands:
        try:
            meta, _, has_fm = extract_frontmatter(c.read_text(encoding="utf-8"))
            if has_fm and str(meta.get("doc_type") or "").strip().lower() == "manifest":
                return c
        except Exception:
            continue
    return None


def _read_manifest_meta(proj_dir: Path) -> Tuple[Optional[Path], Dict[str, Any]]:
    mf = _find_manifest(proj_dir)
    if mf is None:
        return None, {
            "doc_type": "manifest",
            "title": proj_dir.name,
            "summary": "",
            "status": "active",
            "tags": [],
            "default_mode": "auto",
            "private": False,
            "project_id": proj_dir.name,
            "updated": "",
        }
    try:
        meta, _, _ = extract_frontmatter(mf.read_text(encoding="utf-8"))
    except FrontmatterError:
        meta = {}
    return mf, {
        "doc_type": str(meta.get("doc_type") or "manifest"),
        "title": str(meta.get("title") or proj_dir.name),
        "summary": str(meta.get("summary") or ""),
        "status": str(meta.get("status") or "active"),
        "tags": list(meta.get("tags") or []) if isinstance(meta.get("tags"), list) else [],
        "default_mode": str(meta.get("default_mode") or "auto"),
        "private": bool(meta.get("private", False)),
        "project_id": str(meta.get("project_id") or proj_dir.name),
        "combined_from": list(meta.get("combined_from") or []) if isinstance(meta.get("combined_from"), list) else [],
        "split_from": str(meta.get("split_from") or ""),
        "merged_into": str(meta.get("merged_into") or ""),
        "updated": str(meta.get("updated") or ""),
    }


def _count_compartment_files(comp_dir: Path) -> int:
    if not comp_dir.is_dir():
        return 0
    cnt = 0
    for p in comp_dir.rglob("*"):
        if p.is_symlink() or not p.is_file():
            continue
        rel_parts = p.relative_to(comp_dir).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        cnt += 1
    return cnt


def _public_project(
    *,
    kek: bytes,
    app_id: str,
    vault_ref: str,
    project_ref: str,
    project_rel_path: str,
    proj_dir: Path,
) -> Dict[str, Any]:
    _, meta = _read_manifest_meta(proj_dir)
    out: Dict[str, Any] = {
        "vault_ref": vault_ref,
        "project_ref": project_ref,
        "project_id": meta["project_id"],
        "title": meta["title"],
        "summary": meta["summary"],
        "status": meta["status"],
        "tags": meta["tags"],
        "default_mode": meta["default_mode"],
        "private": meta["private"],
        "counts": {
            "artifacts": _count_compartment_files(proj_dir / "10-Artifacts"),
            "memory": _count_compartment_files(proj_dir / "20-Memory"),
            "slip": _count_compartment_files(proj_dir / "25-Slip"),
            "workbench": _count_compartment_files(proj_dir / "30-Workbench"),
        },
        "updated": meta["updated"],
        "handle_seal": _project_seal(kek, app_id, vault_ref, project_ref, project_rel_path),
        "path_sealed": True,
    }
    if meta.get("combined_from"):
        out["combined_from"] = meta["combined_from"]
    if meta.get("split_from"):
        out["split_from"] = meta["split_from"]
    if meta.get("merged_into"):
        out["merged_into"] = meta["merged_into"]
    return out


def _render_manifest_md(
    *,
    title: str,
    summary: str,
    project_id: str,
    tags: List[str],
    status: str = "active",
    default_mode: str = "auto",
    private: bool = False,
    combined_from: Optional[List[str]] = None,
    split_from: str = "",
) -> str:
    tags_yaml = ", ".join(f'"{fm_scalar(t)}"' for t in tags)
    lines = [
        "---",
        "doc_type: manifest",
        f'title: "{fm_scalar(title)}"',
        f'summary: "{fm_scalar(summary or title)}"',
        f"status: {status}",
        f"tags: [{tags_yaml}]",
        f"updated: {_today()}",
        f"default_mode: {default_mode}",
        f'project_id: "{fm_scalar(project_id)}"',
        f"private: {'true' if private else 'false'}",
    ]
    if combined_from:
        cf_yaml = ", ".join(f'"{fm_scalar(c)}"' for c in combined_from)
        lines.append(f"combined_from: [{cf_yaml}]")
    if split_from:
        lines.append(f'split_from: "{fm_scalar(split_from)}"')
    lines.extend(
        [
            "---",
            "",
            f"# {title}",
            "",
            f"{summary or title}",
            "",
            "## 0. 课题定义与研究边界 (Human / Agent Maintained)",
            "",
            "- **研究目标**：（待补充）",
            "- **核心问题 (RQ)**：（待补充）",
            "- **待解缺口与下一步**：（待补充）",
            "",
            "<!-- atlas:manifest-ledger:begin -->",
            "## 1. 课题核心产出导航 (Project Home Dashboard)",
            "",
            "- **10-Artifacts（报告成果，0 篇）**：（暂无）",
            "- **23-MOC（知识地图，0 篇）**：（暂无）",
            "- **20-Memory（底层资产）**：Atomics=0, Library=0, Slices=0",
            "- **30-Workbench（工作台）**：0 项文件（已初始化默认工作台）",
            "<!-- atlas:manifest-ledger:end -->",
            "",
            "<!-- atlas:manifest-history:begin -->",
            "## 2. 课题增量交付批次台账 (Incremental Delivery Batches)",
            "",
            "- （待交付正式成果：同一批次内的 `报告(10-Artifacts)`、`MOC(23-MOC)`、`Atomic(22-Atomics)` 自动归并至当前开放批次；运行 `guard --mode delivery` 验收通过后封板，下一轮任务自动按 `---` 开启新批次并仅标记本次增量）",
            "<!-- atlas:manifest-history:end -->",
            "",
        ]
    )
    return "\n".join(lines)


def _init_project_skeleton(
    proj_dir: Path,
    *,
    title: str,
    summary: str,
    project_id: str,
    tags: List[str],
    default_mode: str = "auto",
    private: bool = False,
    combined_from: Optional[List[str]] = None,
    split_from: str = "",
) -> Path:
    proj_dir.mkdir(parents=True, exist_ok=True)
    for sub in (*PROJECT_CONTAINERS, *get_project_fractal_subdirs()):
        sdir = proj_dir / sub
        sdir.mkdir(parents=True, exist_ok=True)
        keep = sdir / ".gitkeep"
        if sub != "30-Workbench" and not keep.exists():
            _write_noexec_bytes(keep, b"")
    init_empty_workbench(proj_dir / "30-Workbench", project_id)
    stem = _slugify_filename(title)
    manifest_path = proj_dir / f"Manifest-{stem}.md"
    _atomic_write_md(
        manifest_path,
        _render_manifest_md(
            title=title,
            summary=summary,
            project_id=project_id,
            tags=tags,
            default_mode=default_mode,
            private=private,
            combined_from=combined_from,
            split_from=split_from,
        ),
    )
    return manifest_path


def _undo_vault_dir(state_dir: Path, vault_ref: str) -> Path:
    d = state_dir / "atlas-project-undo" / vault_ref
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d.parent, 0o700)
    os.chmod(d, 0o700)
    return d


def _next_op_id(u_dir: Path) -> str:
    max_num = 0
    for p in u_dir.glob("op_*.json"):
        m = re.match(r"^op_(\d+)\.json$", p.name)
        if m:
            max_num = max(max_num, int(m.group(1)))
    return f"op_{max_num + 1:02d}"


def _snapshot_tree_0600(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    os.chmod(dst, 0o700)
    for root, dirs, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        target_root = dst / rel_root
        target_root.mkdir(parents=True, exist_ok=True)
        os.chmod(target_root, 0o700)
        for d in dirs:
            p_d = target_root / d
            p_d.mkdir(parents=True, exist_ok=True)
            os.chmod(p_d, 0o700)
        for f in files:
            sf = Path(root) / f
            if sf.is_symlink() or not sf.is_file():
                continue
            df = target_root / f
            df.write_bytes(sf.read_bytes())
            os.chmod(df, 0o600)


def _write_json_0600(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_snapshot_path(root: Path, rel: str) -> Path:
    if root.is_symlink():
        raise VaultStoreError("E_HALT_UNDO_SNAPSHOT", "快照根目录含受限符号链接", exit_code=2)
    parts = Path(rel).parts
    if not parts or Path(rel).is_absolute() or ".." in parts:
        raise VaultStoreError("E_HALT_UNDO_SNAPSHOT", "课题快照路径损坏", exit_code=2)
    target = root.joinpath(*parts)
    for part in (target, *target.parents):
        if part == root:
            break
        if part.is_symlink():
            raise VaultStoreError("E_HALT_UNDO_SNAPSHOT", "课题快照或目标含受限符号链接", exit_code=2)
    return target


def _tree_fingerprint(root: Path) -> Optional[Dict[str, str]]:
    if root.is_symlink():
        raise VaultStoreError("E_HALT_UNDO_SNAPSHOT", "课题快照或目标含受限符号链接", exit_code=2)
    if not root.exists():
        return None
    if not root.is_dir():
        raise VaultStoreError("E_UNDO_CONFLICT", "课题目标类型已发生变化，保留当前内容", exit_code=1)
    out: Dict[str, str] = {".": "directory"}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise VaultStoreError("E_HALT_UNDO_SNAPSHOT", "课题快照或目标含受限符号链接", exit_code=2)
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            out[rel] = "directory"
        elif path.is_file():
            out[rel] = _file_digest(path)
        else:
            raise VaultStoreError("E_HALT_UNDO_SNAPSHOT", "课题包含不支持的特殊文件", exit_code=2)
    return out


def _seal_project_snapshot(meta_file: Path, vault_dir: Path, doc: Dict[str, Any]) -> None:
    """Only a completed operation supplies a trustworthy post-image for conflict detection."""
    doc["post_projects"] = {
        p["project_rel_path"]: _tree_fingerprint(_safe_snapshot_path(vault_dir, p["project_rel_path"]))
        for p in doc["affected_projects"]
    }
    changed_external = []
    for entry in doc.get("external_backups", []):
        target = _safe_snapshot_path(vault_dir, entry["rel_path"])
        post = _file_digest(target) if target.is_file() else None
        # Never roll back unrelated external documents captured before the operation.
        if post != entry["pre_sha256"]:
            changed_external.append({**entry, "post_sha256": post})
    doc["external_backups"] = changed_external
    doc["status"] = "applied"
    _write_json_0600(meta_file, doc)


def _legacy_recovery_plan(vault_dir: Path, snap_dir: Path, doc: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Observe surviving bytes for explicit recovery; never manufacture historical post-images."""
    if doc.get("snapshot_version", 1) != 1 or doc.get("status") != "applied":
        raise VaultStoreError("E_UNDO_UNVERIFIED", "仅支持处于 applied 状态的旧版快照人工恢复", exit_code=1)
    if (snap_dir / "undo-current").exists():
        raise VaultStoreError("E_HALT_UNDO_INCOMPLETE", "存在未收口恢复备份，禁止覆盖或重试", exit_code=2)
    observed = {"affected_projects": [], "external_backups": [], "post_projects": {}}
    items = []
    protected_count = 0
    seen = set()

    def item(name, current, backup):
        # Content previews are bounded; hashes bind the entire file, including binary data.
        def sample(path):
            if path is None or not path.is_file(): return None
            with path.open("rb") as stream: raw = stream.read(4097)
            try: value = raw[:4096].decode("utf-8")
            except UnicodeDecodeError: return {"binary": True, "bytes": path.stat().st_size}
            return {"text": value, "truncated": len(raw) > 4096}
        items.append({"item_id": f"item_{len(items)+1:04d}", "name": name,
                      "current": sample(current), "restore": sample(backup),
                      "effect": "remove" if backup is None else "restore" if current is None else "replace"})

    for info in doc.get("affected_projects", []):
        rel = info["project_rel_path"]
        if len(Path(rel).parts) != 2 or Path(rel).parts[0] != "30-Projects" or rel in seen or info["slot_name"] != Path(rel).name:
            raise VaultStoreError("E_HALT_UNDO_SNAPSHOT", "课题快照范围损坏", exit_code=2)
        seen.add(rel)
        live = _safe_snapshot_path(vault_dir, rel)
        backup = _safe_snapshot_path(snap_dir / "projects", info["slot_name"])
        current = _tree_fingerprint(live)
        pre = _tree_fingerprint(backup) if info["existed_before"] else None
        if info["existed_before"] and pre is None:
            raise VaultStoreError("E_HALT_UNDO_SNAPSHOT", "旧快照缺失，无法生成恢复方案", exit_code=2)
        observed["affected_projects"].append({**info, "pre_tree": pre})
        observed["post_projects"][rel] = current
        for name in sorted(set(current or {}) | set(pre or {})):
            if (current or {}).get(name) == (pre or {}).get(name): continue
            item(Path(name).name if name != "." else Path(rel).name,
                 live / name if name in (current or {}) else None,
                 backup / name if name in (pre or {}) else None)
    if not seen:
        raise VaultStoreError("E_HALT_UNDO_SNAPSHOT", "旧快照未声明课题范围", exit_code=2)
    external_seen = set()
    for info in doc.get("external_backups", []):
        rel = info["rel_path"]
        if rel.startswith(("05-Journal/", "90-System/")):
            protected_count += 1
            continue
        if (rel in external_seen or any(rel == p or rel.startswith(p + "/") for p in seen)
                or not rel.endswith(".md") or any(p.startswith(".") for p in Path(rel).parts)):
            raise VaultStoreError("E_HALT_UNDO_SNAPSHOT", "旧快照外部引用范围损坏或受保护", exit_code=2)
        external_seen.add(rel)
        live = _safe_snapshot_path(vault_dir, rel)
        backup = _safe_snapshot_path(snap_dir / "external", info["blob_name"])
        if not backup.is_file() or (live.exists() and not live.is_file()):
            raise VaultStoreError("E_HALT_UNDO_SNAPSHOT", "旧快照外部文件缺失或类型异常", exit_code=2)
        pre = _file_digest(backup)
        current = _file_digest(live) if live.is_file() else None
        observed["external_backups"].append({**info, "pre_sha256": pre, "post_sha256": current})
        if pre != current: item(Path(rel).name, live if live.exists() else None, backup)
    digest = hashlib.sha256(json.dumps({"snapshot": doc, "observed": observed,
        "scope": str(snap_dir.resolve())}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    preview = {"op_id": doc.get("op_id"), "dry_run": True, "mode": "legacy-recovery",
               "expected_sha256": digest, "historical_integrity_verified": False,
               "requires_confirmation": True, "protected_entries_skipped": protected_count, "changed_items": items, "changed_count": len(items),
               "warning": "旧快照无历史完整性或操作后证明。恢复将覆盖所列差异；外部快照可能包含无关历史修改。请核对内容，截断或二进制项需额外人工核对。日记和系统日志不恢复。执行前备份完整当前内容。",
               "path_sealed": True}
    return observed, preview


def _apply_project_undo(vault_dir: Path, snap_dir: Path, meta_file: Path, doc: Dict[str, Any], *, expected_sha256: str = "") -> List[str]:
    """Validate every backup and post-image before mutating any live file."""
    validation = doc
    if expected_sha256:
        validation, preview = _legacy_recovery_plan(vault_dir, snap_dir, doc)
        if expected_sha256 != preview["expected_sha256"]:
            raise VaultStoreError("E_UNDO_CONFLICT", "恢复预览已过期，当前内容或快照已变化；请重新核对", exit_code=1)
    elif doc.get("snapshot_version") != 2 or not isinstance(doc.get("post_projects"), dict):
        raise VaultStoreError("E_UNDO_UNVERIFIED", "旧快照缺少操作后版本证据，禁止自动覆盖；请人工核对恢复方案", exit_code=1)
    projects = []
    for info in validation.get("affected_projects", []):
        rel = info["project_rel_path"]
        if len(Path(rel).parts) != 2 or Path(rel).parts[0] != "30-Projects":
            raise VaultStoreError("E_HALT_UNDO_SNAPSHOT", "课题快照范围损坏", exit_code=2)
        live = _safe_snapshot_path(vault_dir, rel)
        backup = _safe_snapshot_path(snap_dir / "projects", info["slot_name"])
        expected = info.get("pre_tree")
        if info["existed_before"]:
            if expected is None or _tree_fingerprint(backup) != expected:
                raise VaultStoreError("E_HALT_UNDO_SNAPSHOT", "课题快照缺失或校验失败，未修改当前课题", exit_code=2)
        elif expected is not None:
            raise VaultStoreError("E_HALT_UNDO_SNAPSHOT", "课题快照前置状态损坏", exit_code=2)
        if rel not in validation["post_projects"] or _tree_fingerprint(live) != validation["post_projects"][rel]:
            raise VaultStoreError("E_UNDO_CONFLICT", "课题在原操作后新增、修改或删除了内容，已保留现场，禁止直接撤回", exit_code=1)
        projects.append((rel, live, backup, info))
    external = []
    for info in validation.get("external_backups", []):
        live = _safe_snapshot_path(vault_dir, info["rel_path"])
        backup = _safe_snapshot_path(snap_dir / "external", info["blob_name"])
        if not backup.is_file() or _file_digest(backup) != info.get("pre_sha256"):
            raise VaultStoreError("E_HALT_UNDO_SNAPSHOT", "外部引用快照缺失或校验失败，未修改当前内容", exit_code=2)
        current = _file_digest(live) if live.is_file() else None
        if (live.exists() and not live.is_file()) or current != info.get("post_sha256"):
            raise VaultStoreError("E_UNDO_CONFLICT", "外部引用在原操作后发生变化，已保留当前内容", exit_code=1)
        external.append((info["rel_path"], live, backup))

    # Retain the complete current state, including large binaries, for I/O failure or process interruption.
    recovery = snap_dir / "undo-current"
    if recovery.exists():
        raise VaultStoreError("E_HALT_UNDO_INCOMPLETE", "发现未收口撤回备份，禁止重复撤回，请人工核对", exit_code=2)
    if expected_sha256:
        doc["recovery_review"] = {"expected_sha256": expected_sha256, "basis": "explicit-current-state-review",
                                  "historical_integrity_verified": False, "reviewed_at": _now_iso(),
                                  "observed": validation}
    recovery.mkdir(mode=0o700)
    for _, live, _, info in projects:
        if live.exists():
            _snapshot_tree_0600(live, recovery / "projects" / info["slot_name"])
    external_before = {rel: live.read_bytes() if live.is_file() else None for rel, live, _ in external}
    external_root = recovery / "external"
    external_root.mkdir(mode=0o700)
    for i, (rel, raw) in enumerate(external_before.items()):
        if raw is not None:
            path = external_root / f"ext_{i:03d}.bin"
            path.write_bytes(raw)
            os.chmod(path, 0o600)
    doc["undo_recovery_external"] = [{"rel_path": rel, "blob_name": f"ext_{i:03d}.bin", "existed": raw is not None}
                                     for i, (rel, raw) in enumerate(external_before.items())]
    # Catch concurrent editor changes during backup before publishing any replacement.
    for rel, live, _, info in projects:
        if (_tree_fingerprint(live) != validation["post_projects"][rel]
                or _tree_fingerprint(recovery / "projects" / info["slot_name"]) != validation["post_projects"][rel]):
            raise VaultStoreError("E_UNDO_CONFLICT", "备份期间课题内容发生变化，保留现场与备份", exit_code=1)
    for rel, live, _ in external:
        if (live.read_bytes() if live.is_file() else None) != external_before[rel]:
            raise VaultStoreError("E_UNDO_CONFLICT", "备份期间外部引用发生变化，保留现场与备份", exit_code=1)
    # Freeze and verify replacement copies before removing any live directory.
    prepared_projects = []
    for rel, live, backup, info in projects:
        prepared = recovery / "prepared" / "projects" / info["slot_name"]
        if info["existed_before"]:
            _snapshot_tree_0600(backup, prepared)
            if _tree_fingerprint(prepared) != info["pre_tree"]:
                raise VaultStoreError("E_HALT_UNDO_SNAPSHOT", "恢复准备期间快照发生变化，未修改当前内容", exit_code=2)
        prepared_projects.append((rel, live, prepared, info))
    prepared_external = []
    for index, (rel, live, backup) in enumerate(external):
        prepared = recovery / "prepared" / "external" / f"ext_{index:03d}.bin"
        raw = backup.read_bytes()
        if hashlib.sha256(raw).hexdigest() != validation["external_backups"][index]["pre_sha256"]:
            raise VaultStoreError("E_HALT_UNDO_SNAPSHOT", "恢复准备期间外部快照发生变化，未修改当前内容", exit_code=2)
        prepared.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        prepared.write_bytes(raw)
        os.chmod(prepared, 0o600)
        prepared_external.append((rel, live, prepared))
    projects, external = prepared_projects, prepared_external
    doc["status"] = "undo_started"
    _write_json_0600(meta_file, doc)
    try:
        for _, live, backup, info in projects:
            if live.exists():
                shutil.rmtree(live)
            if info["existed_before"]:
                shutil.copytree(backup, live)
        for _, live, backup in external:
            _write_noexec_bytes(live, backup.read_bytes())
    except Exception:
        # Keep durable backups even if rollback itself fails; never retry automatically.
        for _, live, _, info in projects:
            current_backup = recovery / "projects" / info["slot_name"]
            if live.exists():
                shutil.rmtree(live)
            if current_backup.exists():
                shutil.copytree(current_backup, live)
        for rel, live, _ in external:
            raw = external_before[rel]
            if raw is None:
                if live.exists():
                    live.unlink()
            else:
                _write_noexec_bytes(live, raw)
        doc["status"] = "undo_failed_restored"
        _write_json_0600(meta_file, doc)
        raise
    return [rel for rel, *_ in projects] + [rel for rel, *_ in external]


def _save_pre_op_snapshot(
    *,
    state_dir: Path,
    vault_ref: str,
    op_id: str,
    op_kind: str,
    title: str,
    vault_dir: Path,
    affected_project_rels: List[str],
    external_backups: Dict[str, bytes],
) -> Path:
    u_dir = _undo_vault_dir(state_dir, vault_ref)
    snap_dir = u_dir / op_id
    backup_root = snap_dir / "projects"
    backup_root.mkdir(parents=True, exist_ok=True)
    os.chmod(snap_dir, 0o700)
    os.chmod(backup_root, 0o700)

    proj_records: List[Dict[str, Any]] = []
    for prel in affected_project_rels:
        pdir = vault_dir / prel
        slot_name = Path(prel).name
        pre_tree = _tree_fingerprint(pdir)
        existed = pre_tree is not None
        if existed:
            _snapshot_tree_0600(pdir, backup_root / slot_name)
            if _tree_fingerprint(backup_root / slot_name) != pre_tree:
                raise VaultStoreError("E_HALT_UNDO_SNAPSHOT", "课题快照不完整，禁止开始变更", exit_code=2)
        proj_records.append({"project_rel_path": prel, "slot_name": slot_name, "existed_before": existed, "pre_tree": pre_tree})

    ext_root = snap_dir / "external"
    ext_records: List[Dict[str, str]] = []
    if external_backups:
        ext_root.mkdir(parents=True, exist_ok=True)
        os.chmod(ext_root, 0o700)
        for idx, (erel, raw_bytes) in enumerate(sorted(external_backups.items())):
            bfile = ext_root / f"ext_{idx:03d}.bin"
            bfile.write_bytes(raw_bytes)
            os.chmod(bfile, 0o600)
            ext_records.append({"rel_path": erel, "blob_name": bfile.name, "pre_sha256": hashlib.sha256(raw_bytes).hexdigest()})

    meta_file = u_dir / f"{op_id}.json"
    _write_json_0600(
        meta_file,
        {
            "op_id": op_id,
            "op_kind": op_kind,
            "title": title,
            "status": "prepared",
            "snapshot_version": 2,
            "created_at": _now_iso(),
            "undone_at": "",
            "ticket_id": "",
            "ticket_note_ref": "",
            "affected_projects": proj_records,
            "external_backups": ext_records,
        },
    )
    return meta_file


def _collect_compartment_files(comp_dir: Path) -> List[Path]:
    if not comp_dir.is_dir():
        return []
    out: List[Path] = []
    for p in sorted(comp_dir.rglob("*")):
        if p.is_symlink() or not p.is_file():
            continue
        rel_parts = p.relative_to(comp_dir).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        out.append(p)
    return out


def _add_provenance_to_md(
    raw_txt: str,
    *,
    fallback_title: str,
    new_project_id: str,
    combined_from_id: Optional[str] = None,
    split_from_id: Optional[str] = None,
) -> str:
    meta, _, has_fm = extract_frontmatter(raw_txt)
    if not has_fm:
        body = raw_txt.strip() or f"# {fallback_title}"
        cf_line = f'combined_from: ["{fm_scalar(combined_from_id)}"]\n' if combined_from_id else ""
        sf_line = f'split_from: "{fm_scalar(split_from_id)}"\n' if split_from_id else ""
        return (
            "---\n"
            "doc_type: atomic\n"
            f'title: "{fm_scalar(fallback_title)}"\n'
            f'summary: "{fm_scalar(fallback_title)}"\n'
            "status: active\n"
            "tags: []\n"
            f'project_id: "{fm_scalar(new_project_id)}"\n'
            f"{cf_line}"
            f"{sf_line}"
            f"updated: {_today()}\n"
            "---\n\n"
            f"{body}\n"
        )
    updates: List[Tuple[str, str]] = [
        ("project_id", f'"{fm_scalar(new_project_id)}"'),
        ("updated", _today()),
    ]
    if combined_from_id:
        existing_cf = [str(x) for x in (meta.get("combined_from") or []) if str(x)]
        if combined_from_id not in existing_cf:
            existing_cf.append(combined_from_id)
        cf_yaml = ", ".join(f'"{fm_scalar(x)}"' for x in existing_cf)
        updates.append(("combined_from", f"[{cf_yaml}]"))
    if split_from_id:
        updates.append(("split_from", f'"{fm_scalar(split_from_id)}"'))
    return _update_frontmatter_preserving_extras(raw_txt, updates, None, str(meta.get("title") or fallback_title))


def _unique_renamed_path(target_dir: Path, rel_inside_comp: Path, suffix_tag: str) -> Path:
    parent = target_dir / rel_inside_comp.parent
    stem = rel_inside_comp.stem
    ext = rel_inside_comp.suffix
    cand = parent / f"{stem}-from-{suffix_tag}{ext}"
    idx = 2
    while cand.exists():
        cand = parent / f"{stem}-from-{suffix_tag}-{idx}{ext}"
        idx += 1
    return cand


def _snapshot_external_markdowns(vault_dir: Path, skip_prefixes: List[str]) -> Dict[str, bytes]:
    out: Dict[str, bytes] = {}
    for rel in _scan_vault_markdowns(vault_dir):
        if any(rel == pref or rel.startswith(pref + "/") for pref in skip_prefixes):
            continue
        out[rel] = (vault_dir / rel).read_bytes()
    return out


def _ensure_yymm_prefix(title: str) -> str:
    clean = (title or "").strip()
    if clean and not re.match(r"^\d{2}(?:0[1-9]|1[0-2])-", clean):
        return datetime.now().astimezone().strftime("%y%m-") + clean
    return clean


def _normalize_title_for_dedup(title: str) -> str:
    stripped = re.sub(r"^\d{2}(?:0[1-9]|1[0-2])-", "", (title or "").strip())
    return re.sub(r"[\s_\-]+", "", stripped).lower()


def project_create(
    *,
    vault_dir: Path,
    vault_ref: str,
    proj_reg: KekProjectRegistry,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    title: str,
    project_id: Optional[str] = None,
    summary: Optional[str] = None,
    tags: Any = None,
    default_mode: str = "auto",
    private: bool = False,
    preset: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    clean_title = (title or "").strip()
    if not clean_title:
        raise VaultStoreError("E_PARAM_MISSING", "project-create 需要 --title", exit_code=1)
    if default_mode not in ("auto", "supervised"):
        raise VaultStoreError("E_PARAM_INVALID", "--default_mode 仅支持 auto 或 supervised", exit_code=1)

    # Human display name uses the creation month; explicit IDs and existing projects stay stable.
    raw_title = clean_title
    clean_title = _ensure_yymm_prefix(clean_title)

    existing_rels = _scan_project_dirs(vault_dir)
    rel_to_ref = proj_reg.sync_projects(vault_ref, existing_rels)

    pid = _normalize_project_id(project_id or "", raw_title)
    dir_slug = _slugify_filename(clean_title) if not project_id else _slugify_filename(pid)
    cand_rel = f"30-Projects/{dir_slug}"
    norm_new_title = _normalize_title_for_dedup(clean_title)

    # Fail-closed duplicate guard: never silently spawn duplicate projects or -<hex> clones
    for prel in existing_rels:
        pdir = vault_dir / prel
        _, emeta = _read_manifest_meta(pdir)
        if str(emeta.get("status") or "active").strip().lower() == "archived":
            continue
        existing_pid = str(emeta.get("project_id") or Path(prel).name).strip()
        existing_title = str(emeta.get("title") or Path(prel).name).strip()
        existing_ref = rel_to_ref.get(prel, Path(prel).name)
        existing_norm_title = _normalize_title_for_dedup(existing_title)
        if (
            existing_pid.lower() == pid.lower()
            or (norm_new_title and existing_norm_title == norm_new_title)
            or prel.lower() == cand_rel.lower()
        ):
            raise VaultStoreError(
                "E_PROJECT_ALREADY_EXISTS",
                f"课题已存在（句柄 {existing_ref}，标题「{existing_title}」，project_id={existing_pid}）；"
                f"禁止重复创建！请直接复用 --project_ref {existing_ref}，或先执行 project-rename 改名 / project-trash 软删。",
                exit_code=1,
            )

    if (vault_dir / cand_rel).exists():
        cand_rel = f"30-Projects/{dir_slug}-{secrets.token_hex(2)}"

    tag_list = _parse_list_arg(tags) or ["Project"]
    clean_summary = (summary or "").strip() or f"{clean_title} 独立课题域"
    proj_dir = vault_dir / cand_rel
    _init_project_skeleton(
        proj_dir,
        title=clean_title,
        summary=clean_summary,
        project_id=pid,
        tags=tag_list,
        default_mode=default_mode,
        private=private,
    )
    rec = proj_reg.bind_new_project(vault_ref, cand_rel)
    note_reg.sync_paths(vault_ref, _scan_vault_markdowns(vault_dir))
    preset_info: Optional[Dict[str, Any]] = None
    sealed_extra: List[str] = []
    if preset and preset.strip():
        preset_res, p_sealed = apply_workbench_preset(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            proj_reg=proj_reg,
            note_reg=note_reg,
            preset=preset,
            project_ref=rec["project_ref"],
            scope="project",
        )
        preset_info = preset_res
        sealed_extra.extend(p_sealed)
    pub = _public_project(
        kek=kek,
        app_id=app_id,
        vault_ref=vault_ref,
        project_ref=rec["project_ref"],
        project_rel_path=cand_rel,
        proj_dir=proj_dir,
    )
    if preset_info is not None:
        pub["workbench_preset"] = preset_info.get("preset")
        pub["workbench_preset_name"] = preset_info.get("preset_name")
    return pub, [cand_rel, *_scan_project_dirs(vault_dir), *sealed_extra]


def project_list(
    *,
    vault_dir: Path,
    vault_ref: str,
    proj_reg: KekProjectRegistry,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    include_archived: bool = False,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    existing_rels = _scan_project_dirs(vault_dir)
    mapping = proj_reg.sync_projects(vault_ref, existing_rels)
    note_reg.sync_paths(vault_ref, _scan_vault_markdowns(vault_dir))
    items: List[Dict[str, Any]] = []
    for prel in existing_rels:
        pref = mapping[prel]
        pub = _public_project(
            kek=kek,
            app_id=app_id,
            vault_ref=vault_ref,
            project_ref=pref,
            project_rel_path=prel,
            proj_dir=vault_dir / prel,
        )
        if not include_archived and pub.get("status") == "archived":
            continue
        items.append(pub)
    return items, existing_rels


def project_get(
    *,
    vault_dir: Path,
    vault_ref: str,
    project_ref: str,
    proj_reg: KekProjectRegistry,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
) -> Tuple[Dict[str, Any], List[str]]:
    existing_rels = _scan_project_dirs(vault_dir)
    proj_reg.sync_projects(vault_ref, existing_rels)
    rec = proj_reg.resolve_handle(vault_ref, project_ref)
    prel = str(rec["project_rel_path"])
    proj_dir = vault_dir / prel
    if not proj_dir.is_dir():
        raise VaultStoreError("E_NOT_FOUND", f"句柄 {project_ref} 对应课题目录不存在", exit_code=1)

    all_md = _scan_vault_markdowns(vault_dir)
    note_map = note_reg.sync_paths(vault_ref, all_md)
    pub = _public_project(
        kek=kek,
        app_id=app_id,
        vault_ref=vault_ref,
        project_ref=project_ref,
        project_rel_path=prel,
        proj_dir=proj_dir,
    )

    compartments: Dict[str, List[Dict[str, Any]]] = {"artifacts": [], "memory": [], "slip": [], "workbench": []}
    comp_map = (("10-Artifacts", "artifacts"), ("20-Memory", "memory"), ("25-Slip", "slip"), ("30-Workbench", "workbench"))
    for cname, key in comp_map:
        cdir = proj_dir / cname
        for fpath in _collect_compartment_files(cdir):
            rel_in_vault = fpath.relative_to(vault_dir).as_posix()
            sub_rel = fpath.relative_to(cdir).as_posix()
            if fpath.suffix.lower() == ".md":
                txt = fpath.read_text(encoding="utf-8")
                try:
                    meta, _, _ = extract_frontmatter(txt)
                except FrontmatterError:
                    meta = {}
                compartments[key].append(
                    {
                        "note_ref": note_map.get(rel_in_vault),
                        "title": str(meta.get("title") or fpath.stem),
                        "doc_type": str(meta.get("doc_type") or ""),
                        "status": str(meta.get("status") or ""),
                        "subpath_level": len(Path(sub_rel).parts),
                    }
                )
            else:
                compartments[key].append(
                    {
                        "kind": fpath.suffix.lstrip(".").lower() or "file",
                        "item": sub_rel,
                    }
                )
    pub["compartments"] = compartments
    return pub, existing_rels + all_md


def _project_short_tag(pid: str) -> str:
    stripped = re.sub(r"^\d{2}(?:0[1-9]|1[0-2])-", "", (pid or "").strip())
    clean = re.sub(r"[^A-Za-z0-9_-]+", "-", stripped).strip("-_")
    if not clean:
        clean = re.sub(r"[^A-Za-z0-9_-]+", "-", (pid or "").strip()).strip("-_") or "src"
    return clean[:20]


_DOMAIN_NAMESPACED_STEM_RE = re.compile(r"^(?:Atom|MOC|Art|Slip)-[A-Za-z0-9_-]+-\d{3}-")


def _is_domain_namespaced_stem(stem: str) -> bool:
    return bool(_DOMAIN_NAMESPACED_STEM_RE.match(stem or ""))


def _is_auto_suffix_conflict(cname: str, rel_sub_posix: str) -> bool:
    """Human-named Artifacts, Library, and Slices auto-suffix with -from-<short_tag>; other conflicts pause for user."""
    if cname == "10-Artifacts":
        return True
    if cname == "20-Memory" and (rel_sub_posix.startswith("21-Library/") or rel_sub_posix.startswith("24-Slices/")):
        return True
    return False


def _domain_root_of(rel_path: str) -> str:
    parts = Path(rel_path).parts
    if len(parts) >= 2 and parts[0] == "30-Projects":
        return f"30-Projects/{parts[1]}/"
    if parts and parts[0] == "20-AI-Zone":
        return "20-AI-Zone/"
    return ""


def project_merge(
    *,
    ws_root: Path,
    state_dir: Path,
    vault_dir: Path,
    vault_ref: str,
    proj_reg: KekProjectRegistry,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    source_refs: List[str],
    target_ref: Optional[str] = None,
    title: Optional[str] = None,
    project_id: Optional[str] = None,
    summary: Optional[str] = None,
    on_conflict: str = "ask",
    workbench_conflict: str = "rename",
    add_combined_from: bool = True,
    archive_sources: bool = True,
) -> Tuple[Dict[str, Any], List[str]]:
    if on_conflict not in ("ask", "rename", "skip", "overwrite", "merge", "supersede"):
        raise VaultStoreError("E_PARAM_INVALID", "--on_conflict 仅支持 ask / rename / skip / overwrite / merge / supersede", exit_code=1)
    if workbench_conflict not in ("rename", "ask", "skip", "overwrite"):
        raise VaultStoreError("E_PARAM_INVALID", "--workbench_conflict 仅支持 rename / ask / skip / overwrite", exit_code=1)

    existing_rels = _scan_project_dirs(vault_dir)
    proj_reg.sync_projects(vault_ref, existing_rels)
    all_md = _scan_vault_markdowns(vault_dir)
    note_map = note_reg.sync_paths(vault_ref, all_md)

    dedup_sources: List[str] = []
    for sr in source_refs:
        s = sr.strip()
        if s and s not in dedup_sources:
            dedup_sources.append(s)
    if target_ref and target_ref in dedup_sources:
        dedup_sources = [s for s in dedup_sources if s != target_ref]
    min_sources = 1 if target_ref else 2
    if len(dedup_sources) < min_sources:
        raise VaultStoreError("E_PARAM_INVALID", "project-merge 至少需要 2 个不同课题参与合并", exit_code=1)

    source_infos: List[Dict[str, Any]] = []
    for pref in dedup_sources:
        rec = proj_reg.resolve_handle(vault_ref, pref)
        prel = str(rec["project_rel_path"])
        pdir = vault_dir / prel
        if not pdir.is_dir():
            raise VaultStoreError("E_NOT_FOUND", f"源课题 {pref} 不存在", exit_code=1)
        _, pmeta = _read_manifest_meta(pdir)
        source_infos.append({"ref": pref, "rel": prel, "dir": pdir, "meta": pmeta, "tag": _project_short_tag(pmeta["project_id"])})

    target_existed = bool(target_ref)
    if target_ref:
        trec = proj_reg.resolve_handle(vault_ref, target_ref)
        t_rel = str(trec["project_rel_path"])
        t_dir = vault_dir / t_rel
        if not t_dir.is_dir():
            raise VaultStoreError("E_NOT_FOUND", f"目标课题 {target_ref} 不存在", exit_code=1)
        _, t_meta = _read_manifest_meta(t_dir)
        target_pid = t_meta["project_id"]
        target_title = (title or "").strip() or t_meta["title"]
        target_summary = (summary or "").strip() or t_meta["summary"]
    else:
        raw_title = (title or "").strip() or (" + ".join(s["meta"]["title"] for s in source_infos[:3]))
        clean_title = _ensure_yymm_prefix(raw_title)
        target_pid = _normalize_project_id(project_id or "", raw_title)
        dir_slug = _slugify_filename(clean_title) if not project_id else _slugify_filename(target_pid)
        t_rel = f"30-Projects/{dir_slug}"
        if (vault_dir / t_rel).exists():
            t_rel = f"30-Projects/{dir_slug}-{secrets.token_hex(2)}"
        t_dir = vault_dir / t_rel
        target_title = clean_title
        combined_names = "、".join(s["meta"]["title"] for s in source_infos)
        target_summary = (summary or "").strip() or f"合并自：{combined_names}"

    # 1. Zero-mutation pre-flight collision scan (O(N) in-memory index)
    seen_am: Dict[str, Dict[str, Dict[str, Any]]] = {"10-Artifacts": {}, "20-Memory": {}, "25-Slip": {}}
    seen_am_titles: Dict[str, Dict[str, Dict[str, Any]]] = {"10-Artifacts": {}, "20-Memory": {}, "25-Slip": {}}
    seen_am_hashes: Dict[str, Dict[str, Dict[str, Any]]] = {"10-Artifacts": {}, "20-Memory": {}, "25-Slip": {}}
    seen_wb_dirs: Dict[str, Dict[str, Any]] = {}
    seen_wb_root_files: Dict[str, Dict[str, Any]] = {}

    if target_existed:
        for cname in ("10-Artifacts", "20-Memory", "25-Slip"):
            cdir = t_dir / cname
            for fp in _collect_compartment_files(cdir):
                rel_sub = fp.relative_to(cdir).as_posix()
                raw_b = fp.read_bytes()
                fhash = hashlib.sha256(raw_b).hexdigest()
                title_val = fp.stem
                if fp.suffix.lower() == ".md":
                    try:
                        m, _, _ = extract_frontmatter(raw_b.decode("utf-8"))
                        title_val = str(m.get("title") or fp.stem)
                    except Exception:
                        pass
                info = {"project_ref": target_ref, "project_id": target_pid, "rel_sub": rel_sub, "title": title_val, "sha256": fhash}
                seen_am[cname][rel_sub] = info
                if not _is_domain_namespaced_stem(fp.stem):
                    seen_am_titles[cname][title_val.strip().lower()] = info
                seen_am_hashes[cname][fhash] = info
        wb_dir = t_dir / "30-Workbench"
        for fp in _collect_compartment_files(wb_dir):
            rel_parts = fp.relative_to(wb_dir).parts
            if len(rel_parts) >= 2:
                seen_wb_dirs.setdefault(rel_parts[0], {"project_ref": target_ref, "project_id": target_pid})
            else:
                seen_wb_root_files[rel_parts[0]] = {"project_ref": target_ref, "project_id": target_pid}

    conflicts: List[Dict[str, Any]] = []
    planned_wb_renames: List[Dict[str, Any]] = []

    for sinfo in source_infos:
        spid = sinfo["meta"]["project_id"]
        stag = sinfo["tag"]
        sref = sinfo["ref"]
        sdir = sinfo["dir"]
        for cname in ("10-Artifacts", "20-Memory", "25-Slip"):
            cdir = sdir / cname
            for fp in _collect_compartment_files(cdir):
                rel_sub = fp.relative_to(cdir).as_posix()
                raw_b = fp.read_bytes()
                fhash = hashlib.sha256(raw_b).hexdigest()
                title_val = fp.stem
                if fp.suffix.lower() == ".md":
                    try:
                        m, _, _ = extract_frontmatter(raw_b.decode("utf-8"))
                        title_val = str(m.get("title") or fp.stem)
                    except Exception:
                        pass
                is_ns = _is_domain_namespaced_stem(fp.stem)
                reason = ""
                if rel_sub in seen_am[cname]:
                    reason = "same_filename"
                elif not is_ns and title_val.strip().lower() in seen_am_titles[cname]:
                    reason = "duplicate_title"
                elif fhash in seen_am_hashes[cname]:
                    reason = "identical_content"
                if reason:
                    if not _is_auto_suffix_conflict(cname, rel_sub):
                        v_rel = fp.relative_to(vault_dir).as_posix()
                        comp_name = "slip" if cname == "25-Slip" else "memory"
                        conflicts.append(
                            {
                                "compartment": comp_name,
                                "reason": reason,
                                "source_project_ref": sref,
                                "source_project_id": spid,
                                "note_ref": note_map.get(v_rel),
                                "title": title_val,
                                "conflict_with_project_ref": (
                                    seen_am[cname].get(rel_sub)
                                    or (None if is_ns else seen_am_titles[cname].get(title_val.strip().lower()))
                                    or seen_am_hashes[cname].get(fhash)
                                    or {}
                                ).get("project_ref"),
                            }
                        )
                else:
                    info = {"project_ref": sref, "project_id": spid, "rel_sub": rel_sub, "title": title_val, "sha256": fhash}
                    seen_am[cname][rel_sub] = info
                    if not is_ns:
                        seen_am_titles[cname][title_val.strip().lower()] = info
                    seen_am_hashes[cname][fhash] = info

        wb_dir = sdir / "30-Workbench"
        wb_files = _collect_compartment_files(wb_dir)
        src_top_dirs = sorted({fp.relative_to(wb_dir).parts[0] for fp in wb_files if len(fp.relative_to(wb_dir).parts) >= 2})
        src_root_files = sorted({fp.relative_to(wb_dir).parts[0] for fp in wb_files if len(fp.relative_to(wb_dir).parts) == 1})
        for dname in src_top_dirs:
            if dname in seen_wb_dirs:
                if workbench_conflict == "ask":
                    conflicts.append(
                        {
                            "compartment": "workbench",
                            "reason": "same_workbench_dir",
                            "source_project_ref": sref,
                            "source_project_id": spid,
                            "item": dname,
                            "conflict_with_project_ref": seen_wb_dirs[dname]["project_ref"],
                        }
                    )
                elif workbench_conflict == "rename":
                    planned_wb_renames.append(
                        {
                            "compartment": "workbench",
                            "kind": "dir",
                            "source_project_ref": sref,
                            "source_project_id": spid,
                            "original_item": dname,
                            "renamed_item": f"{dname}-from-{stag}",
                        }
                    )
            else:
                seen_wb_dirs[dname] = {"project_ref": sref, "project_id": spid}
        for fname in src_root_files:
            if fname in seen_wb_root_files:
                if workbench_conflict == "ask":
                    conflicts.append(
                        {
                            "compartment": "workbench",
                            "reason": "same_workbench_file",
                            "source_project_ref": sref,
                            "source_project_id": spid,
                            "item": fname,
                            "conflict_with_project_ref": seen_wb_root_files[fname]["project_ref"],
                        }
                    )
                elif workbench_conflict == "rename":
                    p_f = Path(fname)
                    planned_wb_renames.append(
                        {
                            "compartment": "workbench",
                            "kind": "file",
                            "source_project_ref": sref,
                            "source_project_id": spid,
                            "original_item": fname,
                            "renamed_item": f"{p_f.stem}-from-{stag}{p_f.suffix}",
                        }
                    )
            else:
                seen_wb_root_files[fname] = {"project_ref": sref, "project_id": spid}

    if conflicts and ((on_conflict == "ask") or (workbench_conflict == "ask" and any(c["compartment"] == "workbench" for c in conflicts))):
        raise MergeConflictPause(conflicts, planned_wb_renames)

    from notes.atlas_yamlmini import extract_wikilinks, resolve_wikilink
    original_paths = _scan_vault_markdowns(vault_dir)
    original_note_mapping = note_reg.list_vault_handles(vault_ref)
    affected_prefixes = tuple(info["rel"] + "/" for info in source_infos)
    for source_rel in original_paths:
        if source_rel.startswith("05-Journal/") or Path(source_rel).name.startswith("Manifest-"):
            continue
        source_dom = _domain_root_of(source_rel)
        source_text = (vault_dir / source_rel).read_text(encoding="utf-8")
        for target in extract_wikilinks(source_text):
            candidates = resolve_wikilink(target, source_rel, original_paths)
            if len(candidates) > 1 and any(p.startswith(affected_prefixes) for p in candidates):
                same_dom = [c for c in candidates if source_dom and _domain_root_of(c) == source_dom]
                if len(same_dom) != 1:
                    raise VaultStoreError("E_AMBIGUOUS_LINK", "合并涉及跨域同名短链，请先用明确目标修正引用后再合并", exit_code=1)
    link_moves: Dict[str, str] = {}

    protected_moves = {}
    for source in source_infos:
        for compartment in PROJECT_CONTAINERS:
            for path in _collect_compartment_files(source["dir"] / compartment):
                old = path.relative_to(vault_dir).as_posix()
                protected_moves[old] = (t_dir / compartment / path.relative_to(source["dir"] / compartment)).relative_to(vault_dir).as_posix()
        protected_moves[source["rel"] + "/_"] = t_rel + "/_"
    _preflight_note_moves(vault_dir, protected_moves)

    # 2. Save full pre-op undo snapshot BEFORE mutating disk
    u_dir = _undo_vault_dir(state_dir, vault_ref)
    op_id = _next_op_id(u_dir)
    affected_rels = [s["rel"] for s in source_infos] + [t_rel]
    ext_backups = _snapshot_external_markdowns(vault_dir, affected_rels)
    snap_meta_path = _save_pre_op_snapshot(
        state_dir=state_dir,
        vault_ref=vault_ref,
        op_id=op_id,
        op_kind="merge",
        title=f"合并课题 → {target_title}",
        vault_dir=vault_dir,
        affected_project_rels=affected_rels,
        external_backups=ext_backups,
    )

    # 3. Create or prepare target project
    src_pids = [s["meta"]["project_id"] for s in source_infos]
    all_tags: List[str] = []
    for s in source_infos:
        for t in s["meta"].get("tags") or []:
            if t not in all_tags:
                all_tags.append(t)
    if target_existed:
        existing_tmeta_file, existing_tmeta = _read_manifest_meta(t_dir)
        for t in existing_tmeta.get("tags") or []:
            if t not in all_tags:
                all_tags.append(t)
        if existing_tmeta_file and existing_tmeta_file.is_file():
            old_m = existing_tmeta_file.read_text(encoding="utf-8")
            cf_list = list(existing_tmeta.get("combined_from") or [])
            if add_combined_from:
                for spid in src_pids:
                    if spid not in cf_list:
                        cf_list.append(spid)
            updates = [("updated", _today()), ("title", f'"{fm_scalar(target_title)}"'), ("summary", f'"{fm_scalar(target_summary)}"')]
            if add_combined_from and cf_list:
                updates.append(("combined_from", "[" + ", ".join(f'"{fm_scalar(c)}"' for c in cf_list) + "]"))
            _atomic_write_md(existing_tmeta_file, _update_frontmatter_preserving_extras(old_m, updates, None, target_title))
    else:
        _init_project_skeleton(
            t_dir,
            title=target_title,
            summary=target_summary,
            project_id=target_pid,
            tags=all_tags or ["Project"],
            combined_from=src_pids if add_combined_from else None,
        )

    # 4. Execute batch-indexed merges with O(1) duplicate tracking and zero-dangling-link redirects
    moved_counts = {"artifacts": 0, "memory": 0, "slip": 0, "workbench": 0}
    skipped_items: List[str] = []
    wb_renames: List[Dict[str, str]] = []
    auto_renames: List[Dict[str, str]] = []

    dst_by_rel: Dict[str, Dict[str, Path]] = {"10-Artifacts": {}, "20-Memory": {}, "25-Slip": {}}
    dst_by_title: Dict[str, Dict[str, Path]] = {"10-Artifacts": {}, "20-Memory": {}, "25-Slip": {}}
    dst_by_hash: Dict[str, Dict[str, Path]] = {"10-Artifacts": {}, "20-Memory": {}, "25-Slip": {}}
    dst_slice_clusters: set[str] = set()

    def _index_dst_file(cname_k: str, path_obj: Path, raw_bytes: bytes) -> None:
        rel_k = path_obj.relative_to(t_dir / cname_k).as_posix()
        dst_by_rel[cname_k][rel_k] = path_obj
        dst_by_hash[cname_k][hashlib.sha256(raw_bytes).hexdigest()] = path_obj
        if path_obj.suffix.lower() == ".md" and not _is_domain_namespaced_stem(path_obj.stem):
            try:
                em, _, _ = extract_frontmatter(raw_bytes.decode("utf-8"))
                etitle = str(em.get("title") or path_obj.stem).strip().lower()
            except Exception:
                etitle = path_obj.stem.lower()
            dst_by_title[cname_k][etitle] = path_obj
        if cname_k == "20-Memory" and rel_k.startswith("24-Slices/"):
            sparts = Path(rel_k).parts
            if len(sparts) >= 3:
                dst_slice_clusters.add(sparts[1])

    for cname_init in ("10-Artifacts", "20-Memory", "25-Slip"):
        for existing_fp in _collect_compartment_files(t_dir / cname_init):
            _index_dst_file(cname_init, existing_fp, existing_fp.read_bytes())

    for sinfo in source_infos:
        spid = sinfo["meta"]["project_id"]
        stag = sinfo["tag"]
        sref = sinfo["ref"]
        sdir = sinfo["dir"]
        slice_cluster_map: Dict[str, str] = {}

        for cname, ckey in (("10-Artifacts", "artifacts"), ("20-Memory", "memory"), ("25-Slip", "slip")):
            src_cdir = sdir / cname
            dst_cdir = t_dir / cname
            for fp in _collect_compartment_files(src_cdir):
                rel_sub = fp.relative_to(src_cdir)
                rel_sub_posix = rel_sub.as_posix()
                old_vrel = fp.relative_to(vault_dir).as_posix()

                # Cluster-level collision handling for 24-Slices/<cluster>/...
                if cname == "20-Memory" and rel_sub_posix.startswith("24-Slices/") and len(rel_sub.parts) >= 3:
                    orig_cluster = rel_sub.parts[1]
                    if orig_cluster not in slice_cluster_map:
                        if orig_cluster in dst_slice_clusters or (dst_cdir / "24-Slices" / orig_cluster).exists():
                            cand_c = f"{orig_cluster}-from-{stag}"
                            idx_c = 2
                            while cand_c in dst_slice_clusters or (dst_cdir / "24-Slices" / cand_c).exists():
                                cand_c = f"{orig_cluster}-from-{stag}-{idx_c}"
                                idx_c += 1
                            slice_cluster_map[orig_cluster] = cand_c
                            auto_renames.append({"compartment": "slices", "source_project_ref": sref, "original_item": orig_cluster, "renamed_item": cand_c})
                        else:
                            slice_cluster_map[orig_cluster] = orig_cluster
                        dst_slice_clusters.add(slice_cluster_map[orig_cluster])
                    mapped_cluster = slice_cluster_map[orig_cluster]
                    if mapped_cluster != orig_cluster:
                        new_parts = (rel_sub.parts[0], mapped_cluster, *rel_sub.parts[2:])
                        rel_sub = Path(*new_parts)
                        dst_path = _unique_renamed_path(dst_cdir, rel_sub, stag)
                        if fp.suffix.lower() == ".md":
                            updated_txt = _add_provenance_to_md(
                                fp.read_text(encoding="utf-8"),
                                fallback_title=fp.stem,
                                new_project_id=target_pid,
                                combined_from_id=spid if add_combined_from else None,
                            )
                            _atomic_write_md(dst_path, updated_txt)
                            _index_dst_file(cname, dst_path, updated_txt.encode("utf-8"))
                            link_moves[old_vrel] = dst_path.relative_to(vault_dir).as_posix()
                        else:
                            raw_bytes = fp.read_bytes()
                            _write_noexec_bytes(dst_path, raw_bytes)
                            _index_dst_file(cname, dst_path, raw_bytes)
                        fp.unlink()
                        moved_counts[ckey] += 1
                        continue

                raw_b = fp.read_bytes()
                src_h = hashlib.sha256(raw_b).hexdigest()
                dst_path = dst_cdir / rel_sub
                matched_existing: Optional[Path] = dst_by_rel[cname].get(rel_sub_posix)
                if matched_existing is None and dst_path.exists():
                    matched_existing = dst_path
                if matched_existing is None and fp.suffix.lower() == ".md":
                    if src_h in dst_by_hash[cname]:
                        matched_existing = dst_by_hash[cname][src_h]
                    elif not _is_domain_namespaced_stem(fp.stem):
                        try:
                            sm, _, _ = extract_frontmatter(raw_b.decode("utf-8"))
                            stitle = str(sm.get("title") or fp.stem).strip().lower()
                        except Exception:
                            stitle = fp.stem.lower()
                        matched_existing = dst_by_title[cname].get(stitle)

                if matched_existing is not None:
                    eff_policy = "rename" if _is_auto_suffix_conflict(cname, rel_sub_posix) else on_conflict
                    if eff_policy == "skip":
                        skipped_items.append(fp.stem)
                        if fp.suffix.lower() == ".md" and matched_existing.is_file():
                            link_moves[old_vrel] = matched_existing.relative_to(vault_dir).as_posix()
                        fp.unlink()
                        continue
                    if eff_policy == "merge" and fp.suffix.lower() == ".md" and matched_existing.is_file():
                        _, src_body, _ = extract_frontmatter(raw_b.decode("utf-8"))
                        ex_txt = matched_existing.read_text(encoding="utf-8").rstrip()
                        merged_txt = ex_txt + f"\n\n## 合流补充 ({_today()} · 来自 {stag})\n\n" + src_body.strip() + "\n"
                        merged_txt = _add_provenance_to_md(
                            merged_txt,
                            fallback_title=matched_existing.stem,
                            new_project_id=target_pid,
                            combined_from_id=spid if add_combined_from else None,
                        )
                        _atomic_write_md(matched_existing, merged_txt)
                        _index_dst_file(cname, matched_existing, merged_txt.encode("utf-8"))
                        link_moves[old_vrel] = matched_existing.relative_to(vault_dir).as_posix()
                        fp.unlink()
                        moved_counts[ckey] += 1
                        continue
                    if eff_policy == "supersede" and fp.suffix.lower() == ".md" and matched_existing.is_file():
                        attic_dir = t_dir / "20-Memory" / "97-Attic"
                        attic_dir.mkdir(parents=True, exist_ok=True)
                        sup_path = _unique_renamed_path(attic_dir, Path(matched_existing.name), "superseded")
                        old_dst_txt = matched_existing.read_text(encoding="utf-8")
                        old_dst_txt = _update_frontmatter_preserving_extras(
                            old_dst_txt,
                            [("status", "deprecated"), ("superseded_by", f'"[[{dst_path.stem}]]"'), ("updated", _today())],
                            None,
                            matched_existing.stem,
                        )
                        _atomic_write_md(sup_path, old_dst_txt)
                        if matched_existing.resolve() != dst_path.resolve():
                            matched_existing.unlink()
                        updated_txt = _add_provenance_to_md(
                            raw_b.decode("utf-8"),
                            fallback_title=fp.stem,
                            new_project_id=target_pid,
                            combined_from_id=spid if add_combined_from else None,
                        )
                        updated_txt = _update_frontmatter_preserving_extras(
                            updated_txt,
                            [("supersedes", f'"[[{sup_path.stem}]]"')],
                            None,
                            fp.stem,
                        )
                        _atomic_write_md(dst_path, updated_txt)
                        _index_dst_file(cname, dst_path, updated_txt.encode("utf-8"))
                        link_moves[old_vrel] = dst_path.relative_to(vault_dir).as_posix()
                        fp.unlink()
                        moved_counts[ckey] += 1
                        continue
                    if eff_policy == "rename":
                        dst_path = _unique_renamed_path(dst_cdir, rel_sub, stag)
                        auto_renames.append(
                            {
                                "compartment": ckey,
                                "source_project_ref": sref,
                                "original_item": rel_sub_posix,
                                "renamed_item": dst_path.relative_to(dst_cdir).as_posix(),
                            }
                        )

                if fp.suffix.lower() == ".md":
                    old_stem = fp.stem
                    updated_txt = _add_provenance_to_md(
                        raw_b.decode("utf-8"),
                        fallback_title=old_stem,
                        new_project_id=target_pid,
                        combined_from_id=spid if add_combined_from else None,
                    )
                    _atomic_write_md(dst_path, updated_txt)
                    _index_dst_file(cname, dst_path, updated_txt.encode("utf-8"))
                    fp.unlink()
                    link_moves[old_vrel] = dst_path.relative_to(vault_dir).as_posix()
                else:
                    _write_noexec_bytes(dst_path, raw_b)
                    _index_dst_file(cname, dst_path, raw_b)
                    fp.unlink()
                moved_counts[ckey] += 1

        # Workbench merge: directory-level collision -> rename whole colliding folder to <dir>-from-<short_tag>/
        src_wb = sdir / "30-Workbench"
        dst_wb = t_dir / "30-Workbench"
        wb_dir_map: Dict[str, Optional[str]] = {}
        for fp in _collect_compartment_files(src_wb):
            rel_sub = fp.relative_to(src_wb)
            old_vrel = fp.relative_to(vault_dir).as_posix()
            if len(rel_sub.parts) >= 2:
                top_dir = rel_sub.parts[0]
                if top_dir not in wb_dir_map:
                    if (dst_wb / top_dir).exists():
                        if workbench_conflict == "skip":
                            wb_dir_map[top_dir] = None
                        elif workbench_conflict == "rename":
                            cand_d = f"{top_dir}-from-{stag}"
                            idx_d = 2
                            while (dst_wb / cand_d).exists():
                                cand_d = f"{top_dir}-from-{stag}-{idx_d}"
                                idx_d += 1
                            wb_dir_map[top_dir] = cand_d
                            wb_renames.append(
                                {
                                    "kind": "dir",
                                    "source_project_ref": sref,
                                    "source_project_id": spid,
                                    "original_item": top_dir,
                                    "renamed_item": cand_d,
                                }
                            )
                        else:
                            wb_dir_map[top_dir] = top_dir
                    else:
                        wb_dir_map[top_dir] = top_dir
                mapped_top = wb_dir_map[top_dir]
                if mapped_top is None:
                    skipped_items.append(rel_sub.as_posix())
                    fp.unlink()
                    continue
                dst_path = dst_wb / Path(mapped_top, *rel_sub.parts[1:])
            else:
                dst_path = dst_wb / rel_sub
                if dst_path.exists():
                    if workbench_conflict == "skip":
                        skipped_items.append(rel_sub.as_posix())
                        fp.unlink()
                        continue
                    if workbench_conflict == "rename":
                        dst_path = _unique_renamed_path(dst_wb, rel_sub, stag)
                        wb_renames.append(
                            {
                                "kind": "file",
                                "source_project_ref": sref,
                                "source_project_id": spid,
                                "original_item": rel_sub.as_posix(),
                                "renamed_item": dst_path.relative_to(dst_wb).as_posix(),
                            }
                        )
            if fp.suffix.lower() == ".md":
                raw_wb = fp.read_text(encoding="utf-8")
                try:
                    _, _, has_fm = extract_frontmatter(raw_wb)
                except FrontmatterError:
                    has_fm = False
                if has_fm and add_combined_from:
                    raw_wb = _add_provenance_to_md(
                        raw_wb,
                        fallback_title=fp.stem,
                        new_project_id=target_pid,
                        combined_from_id=spid,
                    )
                _atomic_write_md(dst_path, raw_wb)
                link_moves[old_vrel] = dst_path.relative_to(vault_dir).as_posix()
            else:
                _write_noexec_bytes(dst_path, fp.read_bytes())
            fp.unlink()
            moved_counts["workbench"] += 1

        # Clean up empty subdirs in source compartments (keep top dirs with .gitkeep)
        for cname in PROJECT_CONTAINERS:
            cdir = sdir / cname
            if cdir.is_dir():
                for d in sorted(cdir.rglob("*"), reverse=True):
                    if d.is_dir() and not any(d.iterdir()):
                        d.rmdir()
                keep = cdir / ".gitkeep"
                if not keep.exists():
                    _write_noexec_bytes(keep, b"")

    cascaded_total = _cascade_note_moves(vault_dir, link_moves, original_paths)
    handle_moves = {
        original_note_mapping[old_rel]: new_rel
        for old_rel, new_rel in link_moves.items()
        if old_rel in original_note_mapping and (vault_dir / new_rel).is_file()
    }
    note_reg.move_handles_batch(vault_ref, handle_moves)

    # Mark source manifests archived after cascade link rewriting so manifest wikilinks remain valid
    if archive_sources:
        for sinfo in source_infos:
            smf, _ = _read_manifest_meta(sinfo["dir"])
            if smf and smf.is_file():
                sm_txt = smf.read_text(encoding="utf-8")
                arch_txt = _update_frontmatter_preserving_extras(
                    sm_txt,
                    [("status", "archived"), ("merged_into", f'"{fm_scalar(target_pid)}"'), ("updated", _today())],
                    None,
                    sinfo["meta"]["title"],
                )
                _atomic_write_md(smf, arch_txt)

    # 5. Bind handles, refresh target Manifest dashboard & record DOC Ticket
    proj_reg.sync_projects(vault_ref, _scan_project_dirs(vault_dir))
    t_handle_rec = proj_reg.bind_new_project(vault_ref, t_rel)
    target_prj_ref = t_handle_rec["project_ref"]

    sync_and_check_project_manifest(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        proj_dir=t_dir,
        project_ref=target_prj_ref,
        note_reg=note_reg,
        action_name="project-merge",
        append_history=False,
    )

    src_names = " + ".join(s["meta"]["title"] for s in source_infos)
    tkt_res = record_project_operation_ticket(
        ws_root=ws_root,
        vault_dir=vault_dir,
        op_id=op_id,
        op_kind="merge",
        title=f"[课题合并] {src_names} → {target_title}",
        summary=f"合并 {len(source_infos)} 个课题至「{target_title}」：Artifacts={moved_counts['artifacts']}，Memory={moved_counts['memory']}，Slip={moved_counts['slip']}，Workbench={moved_counts['workbench']}（重命名 {len(wb_renames)}）",
        details_lines=[
            f"源课题列表：{src_names}（ID: {', '.join(src_pids)}）",
            f"目标课题：{target_title}（句柄：`{target_prj_ref}`，ID：`{target_pid}`）",
            f"`combined_from` 写入：{'是' if add_combined_from else '否'}；源课题归档：{'是' if archive_sources else '否'}",
            f"迁移明细：Artifacts={moved_counts['artifacts']}，Memory={moved_counts['memory']}，Slip={moved_counts['slip']}，Workbench={moved_counts['workbench']}，Workbench 重命名={len(wb_renames)}，自动后缀={len(auto_renames)}，跳过={len(skipped_items)}",
        ],
        undo_hint=f"./gewu atlas --action project-undo --ref {vault_ref} --op_id {op_id}",
        project_ids=[*src_pids, target_pid],
    )

    all_md_after = _scan_vault_markdowns(vault_dir)
    n_map_after = note_reg.sync_paths(vault_ref, all_md_after)
    ticket_note_ref = n_map_after.get(tkt_res["ticket_rel_path"], "")

    snap_doc = json.loads(snap_meta_path.read_text(encoding="utf-8"))
    snap_doc["note_moves"] = [{"note_ref": original_note_mapping[old], "before": old, "after": new} for old, new in link_moves.items() if old in original_note_mapping]
    snap_doc["ticket_id"] = tkt_res["ticket_id"]
    snap_doc["ticket_note_ref"] = ticket_note_ref
    _seal_project_snapshot(snap_meta_path, vault_dir, snap_doc)

    target_pub = _public_project(
        kek=kek,
        app_id=app_id,
        vault_ref=vault_ref,
        project_ref=target_prj_ref,
        project_rel_path=t_rel,
        proj_dir=t_dir,
    )
    res = {
        "vault_ref": vault_ref,
        "op_id": op_id,
        "target_project": target_pub,
        "merged_source_refs": dedup_sources,
        "combined_from": src_pids if add_combined_from else [],
        "moved_counts": moved_counts,
        "workbench_renames": wb_renames,
        "auto_suffix_renames": auto_renames,
        "skipped_count": len(skipped_items),
        "wikilink_cascaded_files": cascaded_total,
        "ticket_id": tkt_res["ticket_id"],
        "ticket_note_ref": ticket_note_ref,
        "undo_available": True,
        "path_sealed": True,
    }
    return res, affected_rels + all_md_after + _scan_project_dirs(vault_dir)


def _match_topic(fpath: Path, branch: Dict[str, Any], note_ref: Optional[str]) -> bool:
    nrefs = [str(x).strip() for x in (branch.get("note_refs") or []) if str(x).strip()]
    if note_ref and note_ref in nrefs:
        return True
    b_tags = {str(t).strip().lower() for t in (branch.get("tags") or []) if str(t).strip()}
    b_kws = [str(k).strip().lower() for k in (branch.get("keywords") or []) if str(k).strip()]

    if fpath.suffix.lower() == ".md":
        txt = fpath.read_text(encoding="utf-8")
        try:
            meta, body, _ = extract_frontmatter(txt)
        except FrontmatterError:
            meta, body = {}, txt
        ftags = {str(t).strip().lower() for t in (meta.get("tags") or []) if str(t).strip()}
        if b_tags and (b_tags & ftags):
            return True
        hay = f"{meta.get('title', '')}\n{meta.get('summary', '')}\n{fpath.stem}\n{body}".lower()
        return any(kw in hay for kw in b_kws)

    # Workbench non-md item: match filename/subpath or small UTF-8 text body
    hay = fpath.name.lower()
    if any(kw in hay for kw in b_kws) or any(t in hay for t in b_tags):
        return True
    try:
        if fpath.stat().st_size <= 262144:
            text_body = fpath.read_text(encoding="utf-8", errors="ignore").lower()
            if any(kw in text_body for kw in b_kws):
                return True
    except Exception:
        pass
    return False


def project_split(
    *,
    ws_root: Path,
    state_dir: Path,
    vault_dir: Path,
    vault_ref: str,
    proj_reg: KekProjectRegistry,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    project_ref: str,
    branches: List[Dict[str, Any]],
) -> Tuple[Dict[str, Any], List[str]]:
    if not branches:
        raise VaultStoreError("E_PARAM_MISSING", "project-split 需要拆分主题方案（--split_plan 或 --title）", exit_code=1)

    existing_rels = _scan_project_dirs(vault_dir)
    proj_reg.sync_projects(vault_ref, existing_rels)
    rec = proj_reg.resolve_handle(vault_ref, project_ref)
    s_rel = str(rec["project_rel_path"])
    s_dir = vault_dir / s_rel
    if not s_dir.is_dir():
        raise VaultStoreError("E_NOT_FOUND", f"源课题 {project_ref} 不存在", exit_code=1)
    _, s_meta = _read_manifest_meta(s_dir)
    s_pid = s_meta["project_id"]

    all_md = _scan_vault_markdowns(vault_dir)
    note_map = note_reg.sync_paths(vault_ref, all_md)

    # Plan assignments before mutating disk
    assigned_paths: set[Path] = set()
    branch_plans: List[Dict[str, Any]] = []
    used_target_rels: set[str] = set(existing_rels)

    for b in branches:
        raw_b_title = str(b.get("title") or "").strip()
        if not raw_b_title:
            raise VaultStoreError("E_PARAM_INVALID", "拆分分支缺少 title", exit_code=1)
        b_title = _ensure_yymm_prefix(raw_b_title)
        b_pid = _normalize_project_id(str(b.get("project_id") or ""), raw_b_title)
        dir_slug = _slugify_filename(b_title) if not b.get("project_id") else _slugify_filename(b_pid)
        t_rel = f"30-Projects/{dir_slug}"
        if t_rel in used_target_rels or (vault_dir / t_rel).exists():
            t_rel = f"30-Projects/{dir_slug}-{secrets.token_hex(2)}"
        used_target_rels.add(t_rel)

        matched: List[Tuple[str, Path, Path]] = []
        for cname in PROJECT_CONTAINERS:
            cdir = s_dir / cname
            for fp in _collect_compartment_files(cdir):
                if fp in assigned_paths:
                    continue
                v_rel = fp.relative_to(vault_dir).as_posix()
                nref = note_map.get(v_rel)
                if _match_topic(fp, b, nref):
                    matched.append((cname, fp, fp.relative_to(cdir)))
                    assigned_paths.add(fp)
        branch_plans.append(
            {
                "title": b_title,
                "project_id": b_pid,
                "summary": str(b.get("summary") or f"从「{s_meta['title']}」按主题拆出").strip(),
                "tags": [str(t) for t in (b.get("tags") or []) if str(t).strip()] or [b_title],
                "target_rel": t_rel,
                "matched": matched,
            }
        )

    total_matched = sum(len(bp["matched"]) for bp in branch_plans)
    if total_matched == 0:
        raise VaultStoreError(
            "E_PARAM_INVALID",
            "未在源课题中匹配到符合拆分条件的文件，请检查 --tags / --query / --note_refs",
            exit_code=1,
        )

    _preflight_note_moves(vault_dir, {
        source.relative_to(vault_dir).as_posix(): f"{branch['target_rel']}/{compartment}/{relative.as_posix()}"
        for branch in branch_plans for compartment, source, relative in branch["matched"]
    })

    # Save pre-op undo snapshot BEFORE disk mutation
    u_dir = _undo_vault_dir(state_dir, vault_ref)
    op_id = _next_op_id(u_dir)
    affected_rels = [s_rel] + [bp["target_rel"] for bp in branch_plans]
    snap_meta_path = _save_pre_op_snapshot(
        state_dir=state_dir,
        vault_ref=vault_ref,
        op_id=op_id,
        op_kind="split",
        title=f"按主题拆分课题：{s_meta['title']}",
        vault_dir=vault_dir,
        affected_project_rels=affected_rels,
        external_backups=_snapshot_external_markdowns(vault_dir, affected_rels),
    )

    link_moves: Dict[str, str] = {}
    split_handle_moves: Dict[str, str] = {}
    created_projects: List[Dict[str, Any]] = []
    created_target_dirs: List[Tuple[Path, str]] = []
    detail_lines: List[str] = [f"源课题：{s_meta['title']}（句柄：`{project_ref}`，ID：`{s_pid}`）"]
    involved_pids = [s_pid]

    for bp in branch_plans:
        t_dir = vault_dir / bp["target_rel"]
        _init_project_skeleton(
            t_dir,
            title=bp["title"],
            summary=bp["summary"],
            project_id=bp["project_id"],
            tags=bp["tags"],
            split_from=s_pid,
        )
        counts = {"artifacts": 0, "memory": 0, "slip": 0, "workbench": 0}
        ckey_map = {"10-Artifacts": "artifacts", "20-Memory": "memory", "25-Slip": "slip", "30-Workbench": "workbench"}
        for cname, src_fp, rel_sub in bp["matched"]:
            dst_fp = t_dir / cname / rel_sub
            if src_fp.suffix.lower() == ".md":
                raw = src_fp.read_text(encoding="utf-8")
                try:
                    _, _, has_fm = extract_frontmatter(raw)
                except FrontmatterError:
                    has_fm = False
                if cname != "30-Workbench" or has_fm:
                    raw = _add_provenance_to_md(
                        raw,
                        fallback_title=src_fp.stem,
                        new_project_id=bp["project_id"],
                        split_from_id=s_pid,
                    )
                _atomic_write_md(dst_fp, raw)
            else:
                _write_noexec_bytes(dst_fp, src_fp.read_bytes())
            old_rel = src_fp.relative_to(vault_dir).as_posix()
            new_rel = dst_fp.relative_to(vault_dir).as_posix()
            if src_fp.suffix.lower() == ".md":
                link_moves[old_rel] = new_rel
            old_ref = note_map.get(old_rel)
            if old_ref:
                split_handle_moves[old_ref] = new_rel
            src_fp.unlink()
            counts[ckey_map[cname]] += 1

        proj_reg.sync_projects(vault_ref, _scan_project_dirs(vault_dir))
        trec = proj_reg.bind_new_project(vault_ref, bp["target_rel"])
        created_target_dirs.append((t_dir, trec["project_ref"]))
        pub = _public_project(
            kek=kek,
            app_id=app_id,
            vault_ref=vault_ref,
            project_ref=trec["project_ref"],
            project_rel_path=bp["target_rel"],
            proj_dir=t_dir,
        )
        created_projects.append({"project": pub, "moved_counts": counts})
        involved_pids.append(bp["project_id"])
        detail_lines.append(
            f"拆出子课题「{bp['title']}」（句柄：`{trec['project_ref']}`，ID：`{bp['project_id']}`）："
            f"Artifacts={counts['artifacts']}，Memory={counts['memory']}，Workbench={counts['workbench']}"
        )

    # Clean up empty subdirectories left behind in source compartments
    for cname in PROJECT_CONTAINERS:
        cdir = s_dir / cname
        if cdir.is_dir():
            for d in sorted(cdir.rglob("*"), reverse=True):
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            keep = cdir / ".gitkeep"
            if cname != "30-Workbench" and not keep.exists():
                _write_noexec_bytes(keep, b"")

    note_reg.move_handles_batch(vault_ref, split_handle_moves)
    _cascade_note_moves(vault_dir, link_moves, all_md)

    # Refresh Manifest dashboards on both source and newly split child projects
    smf, _ = _read_manifest_meta(s_dir)
    if smf and smf.is_file():
        _atomic_write_md(
            smf,
            _update_frontmatter_preserving_extras(smf.read_text(encoding="utf-8"), [("updated", _today())], None, s_meta["title"]),
        )
    sync_and_check_project_manifest(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        proj_dir=s_dir,
        project_ref=project_ref,
        note_reg=note_reg,
        action_name="project-split",
        append_history=False,
    )
    for td_path, td_ref in created_target_dirs:
        sync_and_check_project_manifest(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            proj_dir=td_path,
            project_ref=td_ref,
            note_reg=note_reg,
            action_name="project-split",
            append_history=False,
        )

    tkt_res = record_project_operation_ticket(
        ws_root=ws_root,
        vault_dir=vault_dir,
        op_id=op_id,
        op_kind="split",
        title=f"[课题拆分] {s_meta['title']} → {len(created_projects)} 个子主题",
        summary=f"从「{s_meta['title']}」按主题拆出 {len(created_projects)} 个子课题，共迁移 {total_matched} 项文件",
        details_lines=detail_lines,
        undo_hint=f"./gewu atlas --action project-undo --ref {vault_ref} --op_id {op_id}",
        project_ids=involved_pids,
    )

    all_md_after = _scan_vault_markdowns(vault_dir)
    n_map_after = note_reg.sync_paths(vault_ref, all_md_after)
    ticket_note_ref = n_map_after.get(tkt_res["ticket_rel_path"], "")

    snap_doc = json.loads(snap_meta_path.read_text(encoding="utf-8"))
    snap_doc["note_moves"] = [{"note_ref": note_map[old], "before": old, "after": new} for old, new in link_moves.items() if old in note_map]
    snap_doc["ticket_id"] = tkt_res["ticket_id"]
    snap_doc["ticket_note_ref"] = ticket_note_ref
    _seal_project_snapshot(snap_meta_path, vault_dir, snap_doc)

    src_pub = _public_project(
        kek=kek,
        app_id=app_id,
        vault_ref=vault_ref,
        project_ref=project_ref,
        project_rel_path=s_rel,
        proj_dir=s_dir,
    )
    res = {
        "vault_ref": vault_ref,
        "op_id": op_id,
        "source_project": src_pub,
        "split_targets": created_projects,
        "total_moved": total_matched,
        "ticket_id": tkt_res["ticket_id"],
        "ticket_note_ref": ticket_note_ref,
        "undo_available": True,
        "path_sealed": True,
    }
    return res, affected_rels + all_md_after + _scan_project_dirs(vault_dir)


def project_history(
    *,
    state_dir: Path,
    vault_ref: str,
) -> List[Dict[str, Any]]:
    u_dir = _undo_vault_dir(state_dir, vault_ref)
    ops: List[Dict[str, Any]] = []
    for meta_file in sorted(u_dir.glob("op_*.json")):
        try:
            doc = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        ops.append(
            {
                "op_id": doc.get("op_id"),
                "op_kind": doc.get("op_kind"),
                "title": doc.get("title"),
                "status": doc.get("status"),
                "created_at": doc.get("created_at"),
                "undone_at": doc.get("undone_at"),
                "ticket_id": doc.get("ticket_id"),
                "ticket_note_ref": doc.get("ticket_note_ref"),
                "can_preview_legacy_recovery": doc.get("status") == "applied" and doc.get("snapshot_version", 1) == 1,
                "can_undo": doc.get("status") == "applied" and doc.get("snapshot_version") == 2 and "post_projects" in doc,
            }
        )
    return ops


def project_undo(
    *,
    ws_root: Path,
    state_dir: Path,
    vault_dir: Path,
    vault_ref: str,
    proj_reg: KekProjectRegistry,
    note_reg: KekNoteRegistry,
    op_id: Optional[str] = None,
    mode: str = "standard",
    dry_run: bool = False,
    expected_sha256: str = "",
) -> Tuple[Dict[str, Any], List[str]]:
    if mode not in ("standard", "legacy-recovery") or (mode == "standard" and (dry_run or expected_sha256)):
        raise VaultStoreError("E_PARAM_INVALID", "旧快照预览与确认须使用 --mode legacy-recovery", exit_code=1)
    if mode == "legacy-recovery" and not op_id:
        raise VaultStoreError("E_PARAM_MISSING", "旧快照恢复必须指定 --op_id", exit_code=1)
    u_dir = _undo_vault_dir(state_dir, vault_ref)
    target_op = (op_id or "").strip()
    if not target_op:
        applied = [o for o in project_history(state_dir=state_dir, vault_ref=vault_ref) if o.get("can_undo")]
        if not applied:
            raise VaultStoreError("E_NOT_FOUND", "当前知识库没有可回滚的课题合并/拆分操作", exit_code=1)
        target_op = str(applied[-1]["op_id"])

    if not re.match(r"^op_\d+$", target_op):
        raise VaultStoreError("E_PARAM_INVALID", "--op_id 格式非法，应如 op_01", exit_code=1)

    meta_file = u_dir / f"{target_op}.json"
    snap_dir = u_dir / target_op
    if not meta_file.is_file():
        raise VaultStoreError("E_NOT_FOUND", f"未找到操作快照 {target_op}", exit_code=1)
    snap_doc = json.loads(meta_file.read_text(encoding="utf-8"))
    if snap_doc.get("status") != "applied":
        raise VaultStoreError("E_HALT_UNDO_INCOMPLETE", f"操作 {target_op} 不处于可撤回终态，请核对历史及保留的恢复证据，禁止重试", exit_code=2)

    if mode == "legacy-recovery":
        _, preview = _legacy_recovery_plan(vault_dir, snap_dir, snap_doc)
        if dry_run:
            return {"vault_ref": vault_ref, **preview}, []
        if not expected_sha256:
            raise VaultStoreError("E_UNDO_UNVERIFIED", "先执行 --dry_run true 并核对恢复预览，再传 --expected_sha256 确认", exit_code=1)
    affected_rels = _apply_project_undo(vault_dir, snap_dir, meta_file, snap_doc, expected_sha256=expected_sha256)

    for move in snap_doc.get("note_moves", []):
        note_reg.move_handle(vault_ref, move["note_ref"], move["before"])

    proj_reg.sync_projects(vault_ref, _scan_project_dirs(vault_dir))
    undo_op_id = f"{target_op}-undo"
    tkt_res = record_project_operation_ticket(
        ws_root=ws_root,
        vault_dir=vault_dir,
        op_id=undo_op_id,
        op_kind="undo",
        title=f"[课题后悔药回滚] 撤销 {target_op}（{snap_doc.get('title', '')}）",
        summary=(f"已按人工核对的存量快照恢复 {target_op}；历史完整性未验证，日记和系统日志保持现状"
                 if mode == "legacy-recovery" else f"已将课题操作 {target_op} 完整回滚至操作前状态"),
        details_lines=[
            f"原操作编号：`{target_op}`（类型：`{snap_doc.get('op_kind')}`）",
            f"原协同工单：`{snap_doc.get('ticket_id')}`",
            f"已还原受影响课题目录数量：{len(snap_doc.get('affected_projects') or [])}",
        ],
        undo_hint="本次为回滚操作，原快照已还原完毕。",
    )
    all_md_after = _scan_vault_markdowns(vault_dir)
    n_map_after = note_reg.sync_paths(vault_ref, all_md_after)
    undo_ticket_note_ref = n_map_after.get(tkt_res["ticket_rel_path"], "")
    snap_doc["status"] = "undone"
    snap_doc["undone_at"] = _now_iso()
    _write_json_0600(meta_file, snap_doc)

    res = {
        "vault_ref": vault_ref,
        "undone_op_id": target_op,
        "original_op_kind": snap_doc.get("op_kind"),
        "restored_projects_count": len(snap_doc.get("affected_projects") or []),
        "undo_ticket_id": tkt_res["ticket_id"],
        "undo_ticket_note_ref": undo_ticket_note_ref,
        "protected_entries_skipped": preview["protected_entries_skipped"] if mode == "legacy-recovery" else 0,
        "recovery_mode": mode,
        "historical_integrity_verified": mode != "legacy-recovery",
        "path_sealed": True,
    }
    return res, affected_rels + all_md_after + _scan_project_dirs(vault_dir)


def project_rename(
    *,
    vault_dir: Path,
    vault_ref: str,
    project_ref: str,
    proj_reg: KekProjectRegistry,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    new_title: Optional[str] = None,
    new_project_id: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Conservatively rename a Project title/id without mutating immutable Ticket receipts or dropping handles."""
    existing_rels = _scan_project_dirs(vault_dir)
    proj_reg.sync_projects(vault_ref, existing_rels)
    rec = proj_reg.resolve_handle(vault_ref, project_ref)
    old_prel = str(rec["project_rel_path"])
    old_dir = vault_dir / old_prel
    if not old_dir.is_dir():
        raise VaultStoreError("E_NOT_FOUND", f"句柄 {project_ref} 对应课题目录不存在", exit_code=1)

    old_manifest = _find_manifest(old_dir)
    if old_manifest is None or not old_manifest.is_file():
        raise VaultStoreError("E_NOT_FOUND", f"课题 {project_ref} 缺失 Manifest 清单", exit_code=1)
    m_raw = old_manifest.read_text(encoding="utf-8")
    m_meta, _, _ = extract_frontmatter(m_raw)
    if str(m_meta.get("status") or "").strip().lower() == "archived":
        raise VaultStoreError("E_PARAM_CONFLICT", "已归档课题不可重命名", exit_code=1)

    old_pid = str(m_meta.get("project_id") or old_dir.name).strip()
    old_title = str(m_meta.get("title") or old_dir.name).strip()
    target_title = (new_title or "").strip() or old_title
    target_pid = _normalize_project_id(new_project_id) if (new_project_id or "").strip() else old_pid

    if target_title == old_title and target_pid == old_pid:
        raise VaultStoreError("E_PARAM_INVALID", "project-rename 需要提供新 --title 或 --new_project_id", exit_code=1)

    old_files = [p.relative_to(vault_dir).as_posix() for p in old_dir.rglob("*") if p.is_file() and not p.is_symlink()]
    new_prel = old_prel
    new_dir = old_dir
    planned_root = f"30-Projects/{target_pid}" if target_pid != old_pid else old_prel
    protected_moves = {rel: planned_root + rel[len(old_prel):] for rel in old_files}
    protected_moves[old_manifest.relative_to(vault_dir).as_posix()] = f"{planned_root}/Manifest-{_slugify_filename(target_title)}.md"
    _preflight_note_moves(vault_dir, protected_moves)
    if target_pid != old_pid:
        if target_pid.lower() == "ai-zone":
            raise VaultStoreError("E_PARAM_INVALID", "课题标识不可为保留字 ai-zone", exit_code=1)
        new_prel = f"30-Projects/{target_pid}"
        new_dir = vault_dir / new_prel
        if new_dir.exists():
            raise VaultStoreError("E_PARAM_CONFLICT", "目标课题目录标识已存在", exit_code=1)
        old_dir.rename(new_dir)
        proj_reg.move_project(vault_ref, project_ref, new_prel)
        note_reg.rebase_prefix(vault_ref, old_prel, new_prel)

    cur_manifest = new_dir / old_manifest.name
    new_manifest_name = f"Manifest-{_slugify_filename(target_title)}.md"
    new_manifest = new_dir / new_manifest_name
    fm_updates: List[Tuple[str, str]] = [
        ("title", f'"{fm_scalar(target_title)}"'),
        ("project_id", f'"{fm_scalar(target_pid)}"'),
        ("updated", _today()),
    ]
    if target_title != old_title:
        aliases = list(m_meta.get("aliases") or []) if isinstance(m_meta.get("aliases"), list) else []
        if old_title not in aliases:
            aliases.append(old_title)
        alias_yaml = ", ".join(f'"{fm_scalar(str(a))}"' for a in aliases)
        fm_updates.append(("aliases", f"[{alias_yaml}]"))
    updated_manifest = _update_frontmatter_preserving_extras(
        cur_manifest.read_text(encoding="utf-8"),
        fm_updates,
        None,
        target_title,
    )
    _atomic_write_md(new_manifest, updated_manifest)
    if cur_manifest.resolve() != new_manifest.resolve() and cur_manifest.exists():
        cur_manifest.unlink()
        note_reg.rebase_prefix(vault_ref, f"{new_prel}/{old_manifest.name}", f"{new_prel}/{new_manifest.name}")

    # Update project_id frontmatter on in-project notes when directory ID changed
    updated_notes = 0
    if target_pid != old_pid:
        for p in sorted(new_dir.rglob("*.md")):
            if p == new_manifest or p.is_symlink() or not p.is_file():
                continue
            try:
                ntxt = p.read_text(encoding="utf-8")
                nmeta, _, has_fm = extract_frontmatter(ntxt)
            except (OSError, FrontmatterError):
                continue
            if has_fm and nmeta.get("project_id") == old_pid:
                ntxt2 = _update_frontmatter_preserving_extras(
                    ntxt,
                    [("project_id", f'"{fm_scalar(target_pid)}"'), ("updated", _today())],
                    None,
                    str(nmeta.get("title") or p.stem),
                )
                _atomic_write_md(p, ntxt2)
                updated_notes += 1

    from pipeline.atlas_vfs_engine import _cascade_moves_with_rollback
    moves = {rel: new_prel + rel[len(old_prel):] for rel in old_files}
    moves[old_manifest.relative_to(vault_dir).as_posix()] = new_manifest.relative_to(vault_dir).as_posix()
    cascaded = _cascade_moves_with_rollback(vault_dir, moves)
    all_md = _scan_vault_markdowns(vault_dir)
    note_reg.sync_paths(vault_ref, all_md)
    proj_reg.sync_projects(vault_ref, _scan_project_dirs(vault_dir))

    pub = _public_project(
        kek=kek,
        app_id=app_id,
        vault_ref=vault_ref,
        project_ref=project_ref,
        project_rel_path=new_prel,
        proj_dir=new_dir,
    )
    pub["previous_title"] = old_title
    pub["previous_project_id"] = old_pid
    pub["updated_project_notes"] = updated_notes
    pub["wikilink_cascaded_files"] = cascaded
    return pub, [old_prel, new_prel, *all_md]


def _set_project_privacy(proj_dir: Path, private: bool) -> Tuple[int, List[str]]:
    mf = _find_manifest(proj_dir)
    if mf is None:
        raise VaultStoreError("E_NOT_FOUND", "课题缺少 Manifest 台账文件", exit_code=1)
    today = _today()
    updates: List[Tuple[str, str]] = [
        ("private", "true" if private else "false"),
        ("updated", today),
    ]
    if private:
        updates.append(("private_since", today))
    mf_txt = mf.read_text(encoding="utf-8")
    _, mmeta = _read_manifest_meta(proj_dir)
    mf_new = _update_frontmatter_preserving_extras(mf_txt, updates, None, mmeta["title"])
    _atomic_write_md(mf, mf_new)

    updated_notes = 0
    touched_rels: List[str] = []
    for cname in ("10-Artifacts", "20-Memory", "25-Slip"):
        cdir = proj_dir / cname
        if not cdir.is_dir():
            continue
        for fp in sorted(cdir.rglob("*.md")):
            if fp.is_symlink() or not fp.is_file():
                continue
            try:
                ntxt = fp.read_text(encoding="utf-8")
                nmeta, _, has_fm = extract_frontmatter(ntxt)
            except (OSError, FrontmatterError):
                continue
            if not has_fm:
                continue
            cur_priv = bool(nmeta.get("private", False))
            if cur_priv == private:
                continue
            ntxt2 = _update_frontmatter_preserving_extras(
                ntxt,
                updates,
                None,
                str(nmeta.get("title") or fp.stem),
            )
            _atomic_write_md(fp, ntxt2)
            updated_notes += 1
            touched_rels.append(str(fp))
    return updated_notes, touched_rels


def project_publish(
    *,
    vault_dir: Path,
    vault_ref: str,
    project_ref: str,
    proj_reg: KekProjectRegistry,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
) -> Tuple[Dict[str, Any], List[str]]:
    """Mark project manifest and internal Artifacts/Memory/Slip notes public (private: false)."""
    proj_reg.sync_projects(vault_ref, _scan_project_dirs(vault_dir))
    rec = proj_reg.resolve_handle(vault_ref, project_ref)
    prel = str(rec["project_rel_path"])
    pdir = vault_dir / prel
    if not pdir.is_dir():
        raise VaultStoreError("E_NOT_FOUND", f"课题不存在: {project_ref}", exit_code=1)
    _, m_meta = _read_manifest_meta(pdir)
    if str(m_meta.get("status") or "active").strip().lower() in ("archived", "deprecated"):
        raise VaultStoreError("E_DOMAIN_FROZEN", "已冻结（archived/deprecated）课题不允许公开，请先 project-reopen", exit_code=1)

    updated_notes, _ = _set_project_privacy(pdir, private=False)
    all_md = _scan_vault_markdowns(vault_dir)
    note_reg.sync_paths(vault_ref, all_md)
    pub = _public_project(
        kek=kek,
        app_id=app_id,
        vault_ref=vault_ref,
        project_ref=project_ref,
        project_rel_path=prel,
        proj_dir=pdir,
    )
    pub["updated_notes"] = updated_notes
    pub["published"] = True
    return pub, [prel, *all_md]


def project_unpublish(
    *,
    state_dir: Path,
    vault_dir: Path,
    vault_ref: str,
    project_ref: str,
    proj_reg: KekProjectRegistry,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    on_links: str = "",
    apply: bool = False,
) -> Tuple[Dict[str, Any], List[str]]:
    """Mark project private (private: true); gate/downgrade external incoming wikilinks with audit log."""
    from pipeline.atlas_vfs_engine import _WIKILINK_RE, _basename_of, _match_candidate_to_targets, _rewrite_prose, _split_inner

    proj_reg.sync_projects(vault_ref, _scan_project_dirs(vault_dir))
    rec = proj_reg.resolve_handle(vault_ref, project_ref)
    prel = str(rec["project_rel_path"])
    pdir = vault_dir / prel
    if not pdir.is_dir():
        raise VaultStoreError("E_NOT_FOUND", f"课题不存在: {project_ref}", exit_code=1)
    _, pmeta = _read_manifest_meta(pdir)
    pid = str(pmeta["project_id"])

    all_md = _scan_vault_markdowns(vault_dir)
    n_map = note_reg.sync_paths(vault_ref, all_md)
    target_rels = [rel for rel in all_md if rel.startswith(prel + "/")]
    target_set = set(target_rels)
    bare_names = {Path(r).stem for r in target_rels}
    external_rels = [rel for rel in all_md if not rel.startswith(prel + "/") and not rel.startswith("05-Journal/")]

    ext_matches: List[Dict[str, Any]] = []
    total_ext_links = 0
    for erel in external_rels:
        epath = vault_dir / erel
        if not epath.is_file() or epath.is_symlink():
            continue
        txt = epath.read_text(encoding="utf-8")
        links_in_file: List[Dict[str, str]] = []

        def _scan_seg(seg: str) -> str:
            for m in _WIKILINK_RE.finditer(seg):
                raw_link = m.group(0)
                if raw_link.startswith("!"):
                    continue
                t, _anchor, alias = _split_inner(m.group(2))
                if t and _match_candidate_to_targets(t, epath, vault_dir, target_set, bare_names):
                    plain = alias or _basename_of(t)
                    links_in_file.append({"raw_link": raw_link, "plain_text": plain})
            return seg

        _rewrite_prose(txt, _scan_seg)
        if links_in_file:
            total_ext_links += len(links_in_file)
            ext_matches.append(
                {
                    "note_ref": n_map.get(erel) or "",
                    "rel": erel,
                    "path": epath,
                    "links": links_in_file,
                }
            )

    link_mode = (on_links or "").strip().lower()
    if total_ext_links > 0 and (not apply or link_mode != "downgrade"):
        return {
            "paused": True,
            "vault_ref": vault_ref,
            "project_ref": project_ref,
            "incoming_external_count": total_ext_links,
            "affected_notes": [
                {"note_ref": item["note_ref"], "link_count": len(item["links"])}
                for item in ext_matches
            ],
            "path_sealed": True,
        }, [prel, *all_md]

    snapshot_id: Optional[str] = None
    downgraded_count = 0
    if total_ext_links > 0:
        snapshot_id = f"unp_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        snap_entries: List[Dict[str, Any]] = []
        for item in ext_matches:
            epath = item["path"]
            old_txt = epath.read_text(encoding="utf-8")
            replacements: List[Dict[str, str]] = []

            def _downgrade_seg(seg: str) -> str:
                def _repl(m: re.Match) -> str:
                    raw_link = m.group(0)
                    if raw_link.startswith("!"):
                        return raw_link
                    t, _anchor, alias = _split_inner(m.group(2))
                    if not t or not _match_candidate_to_targets(t, epath, vault_dir, target_set, bare_names):
                        return raw_link
                    plain = alias or _basename_of(t)
                    replacements.append({"raw_link": raw_link, "plain_text": plain})
                    return plain

                return _WIKILINK_RE.sub(_repl, seg)

            new_txt = _rewrite_prose(old_txt, _downgrade_seg)
            if replacements and new_txt != old_txt:
                if new_txt.startswith("---"):
                    new_txt = _update_frontmatter_preserving_extras(new_txt, [("updated", _today())], None, epath.stem)
                _atomic_write_md(epath, new_txt)
                downgraded_count += len(replacements)
                snap_entries.append({"rel": item["rel"], "replacements": replacements})

        log_dir = state_dir / "unpublish_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(log_dir, 0o700)
        snap_doc = {
            "snapshot_id": snapshot_id,
            "vault_ref": vault_ref,
            "project_id": pid,
            "created_at": _now_iso(),
            "restored": False,
            "entries": snap_entries,
        }
        _write_noexec_bytes(
            log_dir / f"{vault_ref}__{pid}__{snapshot_id}.json",
            json.dumps(snap_doc, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    updated_notes, _ = _set_project_privacy(pdir, private=True)
    all_md_after = _scan_vault_markdowns(vault_dir)
    note_reg.sync_paths(vault_ref, all_md_after)
    pub = _public_project(
        kek=kek,
        app_id=app_id,
        vault_ref=vault_ref,
        project_ref=project_ref,
        project_rel_path=prel,
        proj_dir=pdir,
    )
    pub["paused"] = False
    pub["unpublished"] = True
    pub["updated_notes"] = updated_notes
    pub["downgraded_links_count"] = downgraded_count
    if snapshot_id:
        pub["snapshot_id"] = snapshot_id
    return pub, [prel, *all_md_after]


def project_restore_links(
    *,
    state_dir: Path,
    vault_dir: Path,
    vault_ref: str,
    project_ref: str,
    proj_reg: KekProjectRegistry,
    note_reg: KekNoteRegistry,
    op_id: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Restore downgraded external wikilinks from unpublish audit snapshot."""
    proj_reg.sync_projects(vault_ref, _scan_project_dirs(vault_dir))
    rec = proj_reg.resolve_handle(vault_ref, project_ref)
    prel = str(rec["project_rel_path"])
    pdir = vault_dir / prel
    _, pmeta = _read_manifest_meta(pdir)
    pid = str(pmeta["project_id"])

    log_dir = state_dir / "unpublish_logs"
    cands = sorted(log_dir.glob(f"{vault_ref}__{pid}__*.json")) if log_dir.is_dir() else []
    target_snap: Optional[Path] = None
    if op_id and op_id.strip():
        clean_id = op_id.strip()
        for c in cands:
            if clean_id in c.name:
                target_snap = c
                break
    elif cands:
        target_snap = cands[-1]

    if target_snap is None or not target_snap.is_file():
        raise VaultStoreError("E_NOT_FOUND", "未找到可还原的下架链接审计快照", exit_code=1)

    snap_doc = json.loads(target_snap.read_text(encoding="utf-8"))
    restored_files = 0
    restored_links = 0
    skipped_links = 0
    sealed: List[str] = [prel]

    for entry in snap_doc.get("entries") or []:
        rel = str(entry.get("rel") or "")
        sealed.append(rel)
        epath = vault_dir / rel
        if not rel or not epath.is_file() or epath.is_symlink():
            skipped_links += len(entry.get("replacements") or [])
            continue
        txt = epath.read_text(encoding="utf-8")
        new_txt = txt
        file_restored = 0
        for rep in entry.get("replacements") or []:
            raw_link = str(rep.get("raw_link") or "")
            plain = str(rep.get("plain_text") or "")
            if not raw_link or not plain:
                skipped_links += 1
                continue
            if new_txt.count(plain) >= 1 and raw_link not in new_txt:
                new_txt = new_txt.replace(plain, raw_link, 1)
                file_restored += 1
                restored_links += 1
            else:
                skipped_links += 1
        if file_restored > 0 and new_txt != txt:
            if new_txt.startswith("---"):
                new_txt = _update_frontmatter_preserving_extras(new_txt, [("updated", _today())], None, epath.stem)
            _atomic_write_md(epath, new_txt)
            restored_files += 1

    snap_doc["restored"] = True
    snap_doc["restored_at"] = _now_iso()
    _write_noexec_bytes(target_snap, json.dumps(snap_doc, ensure_ascii=False, indent=2).encode("utf-8"))
    all_md = _scan_vault_markdowns(vault_dir)
    note_reg.sync_paths(vault_ref, all_md)
    return {
        "vault_ref": vault_ref,
        "project_ref": project_ref,
        "snapshot_id": snap_doc.get("snapshot_id"),
        "restored_files": restored_files,
        "restored_links": restored_links,
        "skipped_links": skipped_links,
        "path_sealed": True,
    }, sealed + all_md


def _set_project_status(
    *,
    vault_dir: Path,
    vault_ref: str,
    project_ref: str,
    proj_reg: KekProjectRegistry,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    target_status: str,
) -> Tuple[Dict[str, Any], List[str]]:
    proj_reg.sync_projects(vault_ref, _scan_project_dirs(vault_dir))
    rec = proj_reg.resolve_handle(vault_ref, project_ref)
    prel = str(rec["project_rel_path"])
    pdir = vault_dir / prel
    mf = _find_manifest(pdir)
    if mf is None:
        raise VaultStoreError("E_NOT_FOUND", f"课题缺少 Manifest 台账: {project_ref}", exit_code=1)
    _, mmeta = _read_manifest_meta(pdir)
    new_mf = _update_frontmatter_preserving_extras(
        mf.read_text(encoding="utf-8"),
        [("status", target_status), ("updated", _today())],
        None,
        mmeta["title"],
    )
    _atomic_write_md(mf, new_mf)
    all_md = _scan_vault_markdowns(vault_dir)
    note_reg.sync_paths(vault_ref, all_md)
    pub = _public_project(
        kek=kek,
        app_id=app_id,
        vault_ref=vault_ref,
        project_ref=project_ref,
        project_rel_path=prel,
        proj_dir=pdir,
    )
    return pub, [prel, *all_md]


def project_archive(
    *,
    vault_dir: Path,
    vault_ref: str,
    project_ref: str,
    proj_reg: KekProjectRegistry,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
) -> Tuple[Dict[str, Any], List[str]]:
    pub, sealed = _set_project_status(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        project_ref=project_ref,
        proj_reg=proj_reg,
        note_reg=note_reg,
        kek=kek,
        app_id=app_id,
        target_status="archived",
    )
    pub["archived"] = True
    return pub, sealed


def project_reopen(
    *,
    vault_dir: Path,
    vault_ref: str,
    project_ref: str,
    proj_reg: KekProjectRegistry,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
) -> Tuple[Dict[str, Any], List[str]]:
    pub, sealed = _set_project_status(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        project_ref=project_ref,
        proj_reg=proj_reg,
        note_reg=note_reg,
        kek=kek,
        app_id=app_id,
        target_status="active",
    )
    pub["reopened"] = True
    return pub, sealed


def project_trash(
    *,
    vault_dir: Path,
    vault_ref: str,
    project_ref: str,
    proj_reg: KekProjectRegistry,
    note_reg: KekNoteRegistry,
    trash_reg: Any,
    on_links: str = "",
    apply: bool = False,
) -> Tuple[Dict[str, Any], List[str]]:
    """Soft-delete a 30-Projects/<dir> project directory into vault .trash with external link gate."""
    from pipeline.atlas_vfs_engine import _downgrade_refs_in_vault, _scan_incoming_references_for_targets

    proj_reg.sync_projects(vault_ref, _scan_project_dirs(vault_dir))
    rec = proj_reg.resolve_handle(vault_ref, project_ref)
    prel = str(rec["project_rel_path"])
    pdir = vault_dir / prel
    if not pdir.is_dir() or pdir.is_symlink():
        raise VaultStoreError("E_NOT_FOUND", f"课题不存在: {project_ref}", exit_code=1)
    _, pmeta = _read_manifest_meta(pdir)

    all_md = _scan_vault_markdowns(vault_dir)
    note_reg.sync_paths(vault_ref, all_md)
    proj_md_rels = [r for r in all_md if r.startswith(prel + "/")]
    ext_incoming = _scan_incoming_references_for_targets(vault_dir, note_reg, vault_ref, proj_md_rels)
    link_mode = (on_links or "").strip().lower()
    if not apply or (ext_incoming and link_mode != "downgrade"):
        return {
            "paused": True,
            "vault_ref": vault_ref,
            "project_ref": project_ref,
            "title": pmeta["title"],
            "referenced_count": len(ext_incoming),
            "referenced_notes": [
                {"note_ref": item.get("note_ref") or "", "kind": item.get("kind", "note")}
                for item in ext_incoming[:30]
            ],
            "path_sealed": True,
        }, [prel, *all_md]

    downgrade_stats = {"files_updated": 0, "links_downgraded": 0, "canvas_nodes_downgraded": 0}
    if ext_incoming and link_mode == "downgrade":
        downgrade_stats = _downgrade_refs_in_vault(vault_dir, proj_md_rels)

    trash_root = vault_dir / ".trash"
    trash_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    dst_name = pdir.name
    dst = trash_root / dst_name
    if dst.exists() or dst.is_symlink():
        dst_name = f"{pdir.name}.{stamp}"
        dst = trash_root / dst_name

    total_bytes = sum(f.stat().st_size for f in pdir.rglob("*") if f.is_file() and not f.is_symlink())
    trel = dst.relative_to(vault_dir).as_posix()
    shutil.move(str(pdir), str(dst))
    row = trash_reg.record_trashed(
        vault_ref=vault_ref,
        trash_rel=trel,
        orig_rel=prel,
        orig_zone="projects",
        display_name=pdir.name,
        kind="dir",
        size_bytes=total_bytes,
    )
    proj_reg.sync_projects(vault_ref, _scan_project_dirs(vault_dir))
    all_md_after = _scan_vault_markdowns(vault_dir)
    note_reg.sync_paths(vault_ref, all_md_after)
    return {
        "paused": False,
        "vault_ref": vault_ref,
        "project_ref": project_ref,
        "trash_ref": row["trash_ref"],
        "title": pmeta["title"],
        "referenced_count": len(ext_incoming),
        "downgraded_links_count": downgrade_stats["links_downgraded"],
        "trashed": True,
        "path_sealed": True,
    }, [prel, trel, *all_md, *all_md_after]


def project_untrash(
    *,
    vault_dir: Path,
    vault_ref: str,
    proj_reg: KekProjectRegistry,
    note_reg: KekNoteRegistry,
    trash_reg: Any,
    kek: bytes,
    app_id: str,
    trash_ref: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """List or restore soft-deleted 30-Projects/<dir> directories from .trash."""
    rows = trash_reg.sync_vault(vault_dir, vault_ref)
    proj_rows = [r for r in rows if r.get("kind") == "dir" and r.get("orig_zone") == "projects"]
    sealed = [str(p) for r in rows for p in (r.get("trash_rel"), r.get("orig_rel")) if p]

    if not trash_ref:
        items = [
            {
                "trash_ref": r["trash_ref"],
                "name": r["name"],
                "size_bytes": int(r.get("size_bytes", 0)),
                "deleted_at": r.get("deleted_at"),
                "path_sealed": True,
            }
            for r in sorted(proj_rows, key=lambda x: str(x.get("deleted_at") or ""), reverse=True)
        ]
        return {
            "vault_ref": vault_ref,
            "mode": "list",
            "count": len(items),
            "items": items,
            "path_sealed": True,
        }, sealed

    rec = trash_reg.resolve_handle(vault_dir, vault_ref, trash_ref)
    if rec.get("kind") != "dir" or rec.get("orig_zone") != "projects":
        raise VaultStoreError("E_PARAM_INVALID", f"{trash_ref} 不是已软删的课题目录句柄", exit_code=1)
    trel = str(rec["trash_rel"])
    src = vault_dir / trel
    if not src.is_dir() or src.is_symlink():
        raise VaultStoreError("E_NOT_FOUND", f"回收站课题目录不存在: {trash_ref}", exit_code=1)

    orig_rel = str(rec.get("orig_rel") or "").strip()
    dst_rel = orig_rel if orig_rel.startswith("30-Projects/") else f"30-Projects/{rec['name']}"
    dst = vault_dir / dst_rel
    if dst.exists() or dst.is_symlink():
        raise VaultStoreError("E_COLLISION", "目标课题槽位已存在同名目录，拒绝恢复覆盖", exit_code=1)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))

    trash_reg.sync_vault(vault_dir, vault_ref)
    p_map = proj_reg.sync_projects(vault_ref, _scan_project_dirs(vault_dir))
    all_md = _scan_vault_markdowns(vault_dir)
    note_reg.sync_paths(vault_ref, all_md)
    new_pref = p_map.get(dst_rel)
    if not new_pref:
        raise VaultStoreError("E_INTERNAL", "课题恢复后未能重绑逻辑句柄", exit_code=1)
    pub = _public_project(
        kek=kek,
        app_id=app_id,
        vault_ref=vault_ref,
        project_ref=new_pref,
        project_rel_path=dst_rel,
        proj_dir=dst,
    )
    pub["restored_from_trash_ref"] = trash_ref
    pub["mode"] = "restored"
    return pub, sealed + [dst_rel, *all_md]


WORKBENCH_PRESETS: Dict[str, Dict[str, Any]] = {
    "multi-agent": {
        "aliases": ["collab", "multiagent", "ath"],
        "name": "多智能体协同型 (ATH模式)",
        "tree": {
            "10-common": ["00-inbox", "10-draft", "20-reference", "30-consensus"],
            "20-ag-task": ["10-draft", "20-discussion", "30-tests"],
            "30-codex-task": ["10-draft", "20-reading", "30-discussion", "40-reviews"],
        },
        "items": [
            "- `10-common/`（跨主体公共协同空间）",
            "  - `00-inbox/`：跨主体未分流的原始笔记（note）与临时任务",
            "  - `10-draft/`：多方共同演进的公共草案",
            "  - `20-reference/`：已整理的文档（document）与一手参考文献",
            "  - `30-consensus/`：多方确认的架构决议与共识基准",
            "- `20-ag-task/`（Antigravity 任务工作空间）",
            "  - `10-draft/`：Antigravity 探索草稿",
            "  - `20-discussion/`：协同讨论与历史版本快照",
            "  - `30-tests/`：核心特性与回归测试用例",
            "- `30-codex-task/`（Codex 任务工作空间）",
            "  - `10-draft/`：Codex 独立推演草案",
            "  - `20-reading/`：细读摘记与资料评估记录",
            "  - `30-discussion/`：讨论快照与演进记录",
            "  - `40-reviews/`：双方代码与方案交叉评审记录",
        ],
    },
    "engineering": {
        "aliases": ["dev", "eng"],
        "name": "工程研发型",
        "tree": {
            "10-core": ["00-inbox", "10-draft", "20-reference", "30-tests"],
            "20-adapter": ["00-inbox", "10-draft", "20-reference", "30-tests"],
            "30-infra": ["10-draft", "20-reference", "30-scripts"],
        },
        "items": [
            "- `10-core/`（核心引擎模块）",
            "  - `00-inbox/`：核心引擎原始笔记与需求输入",
            "  - `10-draft/`：核心模块技术方案与工程设计草案",
            "  - `20-reference/`：核心技术标准与参考文档",
            "  - `30-tests/`：单元与集成测试夹具",
            "- `20-adapter/`（外围适配器模块）",
            "  - `00-inbox/`：上层应用与适配器输入契约",
            "  - `10-draft/`：适配器设计与接入指南草案",
            "  - `20-reference/`：第三方 SDK 与 API 规范",
            "  - `30-tests/`：适配器集成测试用例",
            "- `30-infra/`（基础设施与运维夹具）",
            "  - `10-draft/`：部署运维与运行环境设计",
            "  - `20-reference/`：基础设施规范与配置基线",
            "  - `30-scripts/`：辅助脚本与调测夹具",
        ],
    },
    "academic": {
        "aliases": ["research", "acad"],
        "name": "学术科研型",
        "tree": {
            "10-literature": ["00-inbox", "10-notes", "20-sources"],
            "20-methodology": ["10-draft", "30-experiments"],
            "30-manuscript": ["10-draft", "20-peer-review"],
        },
        "items": [
            "- `10-literature/`（文献综述与基准线）",
            "  - `00-inbox/`：待阅论文线索与原始笔记（note）",
            "  - `10-notes/`：文献精读摘记与批判性研判",
            "  - `20-sources/`：一手原始文档与实验数据源",
            "- `20-methodology/`（理论与实证推导）",
            "  - `10-draft/`：算法推演与方法论草案",
            "  - `30-experiments/`：实证数据、图表与实验记录",
            "- `30-manuscript/`（论文成文阶段稿）",
            "  - `10-draft/`：论文正文分章节草稿",
            "  - `20-peer-review/`：评审意见反馈与返修备忘",
        ],
    },
    "solo": {
        "aliases": ["creator", "simple"],
        "name": "独立创作者型",
        "tree": {
            "10-main": ["00-inbox", "10-draft", "20-reference"],
            "20-sandbox": ["10-draft", "90-scratch"],
        },
        "items": [
            "- `10-main/`（主线创作）",
            "  - `00-inbox/`：灵感捕捉与原始笔记（note）输入",
            "  - `10-draft/`：主线内容草稿迭代",
            "  - `20-reference/`：参考引用文档",
            "- `20-sandbox/`（发散探索与灵感实验）",
            "  - `10-draft/`：支线脑暴与发散实验",
            "  - `90-scratch/`：涂鸦与临时速记",
        ],
    },
}

_PRESET_MAP_BEGIN = "<!-- atlas:workbench-preset:begin -->"
_PRESET_MAP_END = "<!-- atlas:workbench-preset:end -->"


def _match_workbench_preset(preset_name: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    low = (preset_name or "").strip().lower()
    if not low:
        return None, None
    for key, cfg in WORKBENCH_PRESETS.items():
        if low == key or low in cfg.get("aliases", []):
            return key, cfg
    return None, None


def _preset_map_block(cfg: Dict[str, Any]) -> str:
    return _PRESET_MAP_BEGIN + "\n" + "\n".join(cfg["items"]) + "\n" + _PRESET_MAP_END


def _initial_workbench_readme(domain_label: str) -> str:
    return initial_workbench_readme(domain_label)


def _preset_workbench_readme(domain_label: str, cfg: Dict[str, Any]) -> str:
    return (
        f"# 工作台流转与发布公约 · {domain_label}\n\n"
        "> 本文件是当前域工作台（`30-Workbench`）的协作约定。工作台允许放置未加 YAML 的原始笔记（`note`）与自由探索草案；定稿成果须经 `artifact-adopt` / `artifact-finalize` 采收至 `10-Artifacts`。\n\n"
        "**预设目录地图**：以下受控标记区间为自动生成路标；切换预设时仅刷新该区间，完整保留其余旧目录与人工自定义规则。\n\n"
        "## 1. 空间与工序双层地图（" + cfg["name"] + "）\n\n"
        + _preset_map_block(cfg)
        + "\n\n## 2. 文档晋升与成果发布约定\n\n"
        "- 原始笔记（`note`）可在各空间 `00-inbox/` / `10-draft/` 自由记录，整理成结构化 `document` 后可纳入 `21-Library`。\n"
        "- 多方定稿后通过 `artifact-adopt` 沉淀至 `10-Artifacts`，确认定稿再执行 `artifact-finalize`。\n\n"
        "## 3. 用户个性化习惯\n\n"
        "- [自定义]：二级目录下允许按需自由下钻组织子目录，不受层级限制。\n"
    )


def list_workbench_presets() -> List[Dict[str, Any]]:
    return [
        {
            "preset": key,
            "name": cfg["name"],
            "aliases": list(cfg["aliases"]),
            "tree": {k: list(v) for k, v in cfg["tree"].items()},
        }
        for key, cfg in WORKBENCH_PRESETS.items()
    ]


def apply_workbench_preset(
    *,
    vault_dir: Path,
    vault_ref: str,
    proj_reg: KekProjectRegistry,
    note_reg: KekNoteRegistry,
    preset: Optional[str] = None,
    project_ref: Optional[str] = None,
    scope: str = "auto",
    dry_run: bool = False,
) -> Tuple[Dict[str, Any], List[str]]:
    """List presets or scaffold a two-level 30-Workbench preset for a Project or AI-Zone."""
    from facade.atlas_domain_scope import resolve_domain_scope

    if not (preset or "").strip():
        return {
            "vault_ref": vault_ref,
            "mode": "list",
            "count": len(WORKBENCH_PRESETS),
            "presets": list_workbench_presets(),
            "path_sealed": True,
        }, []

    matched_key, cfg = _match_workbench_preset(preset or "")
    if not matched_key or not cfg:
        valid = ", ".join(WORKBENCH_PRESETS.keys())
        raise VaultStoreError("E_PARAM_INVALID", f"未知工作台预设 '{preset}'，可选: {valid}", exit_code=1)

    domain = resolve_domain_scope(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        proj_reg=proj_reg,
        explicit_project_ref=project_ref,
        scope=scope,
    )
    wb_dir = Path(domain["domain_root"]) / "30-Workbench"
    if wb_dir.is_symlink():
        raise VaultStoreError("E_SECURITY_VIOLATION", "工作台目录为非法符号链接", exit_code=2)

    target_dirs = [wb_dir / l1 / l2 for l1, children in cfg["tree"].items() for l2 in children]
    sealed: List[str] = [p.relative_to(vault_dir).as_posix() for p in target_dirs]
    for path in target_dirs:
        if path.is_symlink():
            raise VaultStoreError("E_SECURITY_VIOLATION", "预设目标路径包含符号链接", exit_code=2)
        if path.exists() and not path.is_dir():
            raise VaultStoreError("E_COLLISION", "预设目录与工作台已有文件冲突，未写入", exit_code=1)

    readme_path = wb_dir / "README.md"
    sealed.append(readme_path.relative_to(vault_dir).as_posix())
    if readme_path.is_symlink():
        raise VaultStoreError("E_SECURITY_VIOLATION", "工作台 README 为非法符号链接", exit_code=2)

    domain_label = str(domain["project_id"])
    new_readme = _preset_workbench_readme(domain_label, cfg)
    readme_action = "create"
    if readme_path.exists():
        old_txt = readme_path.read_text(encoding="utf-8")
        if old_txt == _initial_workbench_readme(domain_label):
            readme_action = "overwrite_initial"
        else:
            if old_txt.count(_PRESET_MAP_BEGIN) != 1 or old_txt.count(_PRESET_MAP_END) != 1:
                raise VaultStoreError(
                    "E_PARAM_CONFLICT",
                    "工作台 README 含人工定制内容且缺少预设地图标记，已保留原文以免覆盖",
                    exit_code=1,
                )
            start = old_txt.index(_PRESET_MAP_BEGIN)
            end = old_txt.index(_PRESET_MAP_END) + len(_PRESET_MAP_END)
            cur_block = old_txt[start:end]
            if cur_block not in [_preset_map_block(c) for c in WORKBENCH_PRESETS.values()]:
                raise VaultStoreError(
                    "E_PARAM_CONFLICT",
                    "工作台 README 预设地图已被手工修改，已保留原文以免覆盖",
                    exit_code=1,
                )
            new_readme = old_txt[:start] + _preset_map_block(cfg) + old_txt[end:]
            readme_action = "update_map"

    created_dirs = 0
    existing_dirs = 0
    for path in target_dirs:
        if path.is_dir():
            existing_dirs += 1
        else:
            created_dirs += 1
            if not dry_run:
                path.mkdir(parents=True, exist_ok=True)
                keep = path / ".gitkeep"
                if not keep.exists():
                    _write_noexec_bytes(keep, b"")

    if not dry_run:
        _atomic_write_md(readme_path, new_readme)
        all_md = _scan_vault_markdowns(vault_dir)
        note_reg.sync_paths(vault_ref, all_md)
        sealed.extend(all_md)

    return {
        "vault_ref": vault_ref,
        "mode": "applied",
        "dry_run": dry_run,
        "preset": matched_key,
        "preset_name": cfg["name"],
        "domain_scope": domain["scope"],
        "project_id": domain["project_id"],
        "created_dir_count": created_dirs,
        "existing_dir_count": existing_dirs,
        "readme_action": readme_action,
        "tree": {k: list(v) for k, v in cfg["tree"].items()},
        "path_sealed": True,
    }, sealed


def _slugify_ws_token(raw: str, max_len: int = 24) -> str:
    txt = re.sub(r"[^A-Za-z0-9_-]+", "-", (raw or "").strip().lower()).strip("-_")
    txt = re.sub(r"-{2,}", "-", txt)
    if not txt or not txt[0].isalnum():
        return ""
    return txt[:max_len].rstrip("-_")


def workbench_workspace(
    *,
    vault_dir: Path,
    vault_ref: str,
    proj_reg: KekProjectRegistry,
    note_reg: KekNoteRegistry,
    kek: bytes,
    app_id: str,
    project_ref: Optional[str] = None,
    scope: str = "auto",
    agent: Optional[str] = None,
    topic: Optional[str] = None,
    mode: str = "allocate",
    dry_run: bool = False,
) -> Tuple[Dict[str, Any], List[str]]:
    """Allocate or list Agent personal workspaces under `<domain>/30-Workbench/ws-<agent>[--<topic>]/`.

    Naming convention: `ws-<agent_slug>` (or `ws-<agent_slug>--<task_slug>`), containing:
    - `drafts/`  (Agent drafts)
    - `91-Temp/` (Temporary files / scratch)
    - `source/`  (Downloaded source materials; automatically git-ignored by DOC track)
    """
    from facade.atlas_domain_scope import resolve_domain_scope

    op_mode = (mode or "allocate").strip().lower()
    if op_mode not in ("allocate", "list"):
        raise VaultStoreError("E_PARAM_INVALID", "--mode 仅支持 allocate 或 list", exit_code=1)

    domain = resolve_domain_scope(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        proj_reg=proj_reg,
        explicit_project_ref=project_ref,
        scope=scope,
    )
    wb_dir = Path(domain["domain_root"]) / "30-Workbench"
    if wb_dir.is_symlink():
        raise VaultStoreError("E_SECURITY_VIOLATION", "工作台目录为非法符号链接", exit_code=2)

    if op_mode == "list":
        workspaces: List[Dict[str, Any]] = []
        if wb_dir.is_dir():
            for child in sorted(wb_dir.iterdir()):
                if child.is_symlink() or not child.is_dir() or not child.name.startswith("ws-"):
                    continue
                ws_id = child.name
                parts = ws_id[3:].split("--", 1)
                owner = parts[0]
                task_part = parts[1] if len(parts) > 1 else None
                workspaces.append(
                    {
                        "workspace_id": ws_id,
                        "agent": owner,
                        "task_slug": task_part,
                        "subdirs": {
                            "root": ws_id,
                            "drafts": f"{ws_id}/drafts",
                            "temp": f"{ws_id}/91-Temp",
                            "source": f"{ws_id}/source",
                        },
                        "git_ignored_source": True,
                    }
                )
        return {
            "vault_ref": vault_ref,
            "mode": "list",
            "domain_scope": domain["scope"],
            "project_id": domain["project_id"],
            "naming_convention": "ws-<agent_slug>[--<task_slug>]",
            "count": len(workspaces),
            "workspaces": workspaces,
            "path_sealed": True,
        }, []

    agent_slug = _slugify_ws_token(agent or "")
    if not agent_slug:
        raise VaultStoreError("E_PARAM_MISSING", "workbench-workspace 分配模式需要 --by <agent> 或 --agent <name>（字母数字开头）", exit_code=1)
    topic_slug = _slugify_ws_token(topic or "", max_len=20)
    ws_id = f"ws-{agent_slug}--{topic_slug}" if topic_slug else f"ws-{agent_slug}"

    ws_dir = (wb_dir / ws_id).resolve()
    if not ws_dir.is_relative_to(wb_dir.resolve()) or ws_dir == wb_dir.resolve():
        raise VaultStoreError("E_VFS_STRUCTURAL", "非法工作空间名称", exit_code=1)

    sub_names = ("drafts", "91-Temp", "source")
    target_dirs = [ws_dir / s for s in sub_names]
    sealed: List[str] = []
    if ws_dir.is_symlink():
        raise VaultStoreError("E_SECURITY_VIOLATION", "个人工作空间目录为非法符号链接", exit_code=2)
    if ws_dir.exists() and not ws_dir.is_dir():
        raise VaultStoreError("E_COLLISION", "工作台内存在同名文件冲突，无法分配个人空间", exit_code=1)

    created_dirs = 0
    existing_dirs = 0
    for path in target_dirs:
        if path.is_symlink():
            raise VaultStoreError("E_SECURITY_VIOLATION", "个人工作空间子目录为非法符号链接", exit_code=2)
        if path.is_dir():
            existing_dirs += 1
        else:
            created_dirs += 1
            if not dry_run:
                path.mkdir(parents=True, exist_ok=True)
                keep = path / ".gitkeep"
                if not keep.exists():
                    _write_noexec_bytes(keep, b"")

    ws_card = ws_dir / "WORKSPACE.md"
    ws_card_rel = ws_card.relative_to(vault_dir).as_posix()
    sealed.append(ws_card_rel)
    card_created = not ws_card.exists()
    if not dry_run:
        if card_created:
            md_txt = "\n".join(
                [
                    "---",
                    "doc_type: workbench",
                    f'title: "Agent 工作空间 {ws_id}"',
                    f'summary: "{agent_slug} 在 {domain["project_id"]} 工作台内的专属工作目录（drafts / 91-Temp / source）"',
                    "status: active",
                    f'project_id: "{domain["project_id"]}"',
                    'tags: ["Workbench", "Workspace"]',
                    f"updated: {_today()}",
                    "---",
                    "",
                    f"# Agent 工作空间：`{ws_id}`",
                    "",
                    f"- **所属主体**：`{agent_slug}`",
                    f"- **命名规范**：`ws-<agent_slug>[--<task_slug>]`",
                    f"- **草稿区**：`{ws_id}/drafts/`（`--zone workbench --subdir {ws_id}/drafts`）",
                    f"- **临时区**：`{ws_id}/91-Temp/`（`--zone workbench --subdir {ws_id}/91-Temp`）",
                    f"- **抓源原文区**：`{ws_id}/source/`（已纳入 DOC Git 忽略列表；或共用 `30-Workbench/source/`）",
                    "",
                ]
            )
            _atomic_write_md(ws_card, md_txt)
        all_md = _scan_vault_markdowns(vault_dir)
        mapping = note_reg.sync_paths(vault_ref, all_md)
        sealed.extend(all_md)
        card_ref = mapping.get(ws_card_rel)
    else:
        card_ref = None

    return {
        "vault_ref": vault_ref,
        "mode": "allocated",
        "dry_run": dry_run,
        "domain_scope": domain["scope"],
        "project_id": domain["project_id"],
        "workspace_id": ws_id,
        "agent": agent_slug,
        "naming_convention": "ws-<agent_slug>[--<task_slug>]",
        "subdirs": {
            "root": ws_id,
            "drafts": f"{ws_id}/drafts",
            "temp": f"{ws_id}/91-Temp",
            "source": f"{ws_id}/source",
        },
        "git_ignored_source": True,
        "created_dir_count": created_dirs,
        "existing_dir_count": existing_dirs,
        "workspace_card_ref": card_ref,
        "card_created": card_created,
        "path_sealed": True,
    }, sealed


_MANIFEST_LEDGER_BEGIN = "<!-- atlas:manifest-ledger:begin -->"
_MANIFEST_LEDGER_END = "<!-- atlas:manifest-ledger:end -->"
_MANIFEST_HISTORY_BEGIN = "<!-- atlas:manifest-history:begin -->"
_MANIFEST_HISTORY_END = "<!-- atlas:manifest-history:end -->"
_BATCH_META_RE = re.compile(r"<!--\s*atlas:delivery-batch\s+([^>]+?)-->")
_BATCH_ATTR_RE = re.compile(r'([a-zA-Z0-9_]+)="([^"]*)"')
_WIKILINK_EXTRACT_RE = re.compile(r"\[\[([^\]\|]+)(?:\|[^\]]*)?\]\]")


def _parse_batch_attrs(block: str) -> Dict[str, str]:
    m = _BATCH_META_RE.search(block)
    if not m:
        return {}
    return {k: v for k, v in _BATCH_ATTR_RE.findall(m.group(1))}


def _extract_block_wikilinks(block: str) -> Dict[str, List[str]]:
    """Extract categorized [[link_key|title]] items from an existing batch block."""
    out: Dict[str, List[str]] = {"artifacts": [], "moc": [], "atomics": [], "other": []}
    for line in block.splitlines():
        if "交付" not in line or "[[" not in line:
            continue
        for seg in line.split("|"):
            seg_s = seg.strip()
            links = re.findall(r"\[\[[^\]]+\]\]", seg_s)
            if not links:
                continue
            if "报告：" in seg_s:
                bucket = "artifacts"
            elif "MOC：" in seg_s:
                bucket = "moc"
            elif "Atomic：" in seg_s:
                bucket = "atomics"
            else:
                bucket = "other"
            for lk in links:
                if lk not in out[bucket]:
                    out[bucket].append(lk)
    return out


def _link_key_of(wlink: str) -> str:
    m = _WIKILINK_EXTRACT_RE.search(wlink)
    return m.group(1).strip() if m else wlink.strip()


def _format_deliverable_wikilinks(
    vault_dir: Path,
    proj_dir: Path,
    rels: List[str],
    note_reg: Optional[KekNoteRegistry],
    vault_ref: str,
    *,
    meta_by_rel: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Format deliverable markdown relative paths into collision-safe Obsidian [[link_key|title]] wikilinks."""
    if not rels:
        return []
    if meta_by_rel is None:
        all_md = _scan_vault_markdowns(vault_dir)
        if note_reg is not None:
            n_map = note_reg.sync_paths(vault_ref, all_md)
        else:
            n_map = {r: r for r in all_md}
        from notes.atlas_wikilink_tools import build_vault_link_index

        idx = build_vault_link_index(vault_dir, n_map)
        meta_by_rel = idx.get("meta_by_rel") or {}
    links: List[str] = []
    seen: set[str] = set()
    for rel in rels:
        if rel in seen:
            continue
        seen.add(rel)
        info = meta_by_rel.get(rel)
        if info:
            lk = str(info.get("link_key") or Path(rel).stem)
            ttl = str(info.get("title") or Path(rel).stem)
            links.append(f"[[{lk}|{ttl}]]")
        else:
            p = vault_dir / rel
            if p.is_file():
                links.append(f"[[{p.stem}]]")
    return links


def _format_artifact_nav_items(
    vault_dir: Path,
    rels: List[str],
    meta_by_rel: Dict[str, Any],
) -> List[str]:
    items: List[str] = []
    for rel in rels:
        info = meta_by_rel.get(rel) or {}
        lk = str(info.get("link_key") or Path(rel).stem)
        ttl = str(info.get("title") or Path(rel).stem)
        stage = ""
        try:
            fmeta, _, _ = extract_frontmatter((vault_dir / rel).read_text(encoding="utf-8"))
            stage = str(fmeta.get("stage") or "").strip()
        except Exception:
            stage = ""
        suffix = f"（`{stage}`）" if stage else ""
        items.append(f"[[{lk}|{ttl}]]{suffix}")
    return items


def _merge_unique_links(existing: List[str], incoming: List[str]) -> List[str]:
    out = list(existing)
    seen_keys = {_link_key_of(x) for x in out}
    for item in incoming:
        k = _link_key_of(item)
        if k not in seen_keys:
            out.append(item)
            seen_keys.add(k)
    return out


def sync_and_check_project_manifest(
    *,
    vault_dir: Path,
    vault_ref: str,
    proj_dir: Path,
    project_ref: str = "",
    note_reg: Optional[KekNoteRegistry] = None,
    agent_name: str = "ai",
    action_name: str = "delivery-check",
    gate_score: int = 60,
    gate_passed: bool = True,
    gate_issues: Optional[List[str]] = None,
    verified_refs: Optional[List[str]] = None,
    deliverable_rels: Optional[List[str]] = None,
    append_history: bool = False,
    batch_key: str = "",
    fast_check: bool = False,
) -> Dict[str, Any]:
    """Verify project Manifest-*.md integrity, refresh Project Home Dashboard with clickable links, and upsert/seal batch-scoped incremental delivery records."""
    from datetime import datetime
    from notes.atlas_wikilink_tools import build_vault_link_index

    mf = _find_manifest(proj_dir)
    if mf is None or not mf.is_file():
        return {
            "project_ref": project_ref or proj_dir.name,
            "ok": False,
            "issue": f"课题 {project_ref or proj_dir.name} 缺少 Manifest 台账文件",
        }
    try:
        raw_txt = mf.read_text(encoding="utf-8")
        meta, body, has_fm = extract_frontmatter(raw_txt)
    except Exception:
        return {
            "project_ref": project_ref or proj_dir.name,
            "ok": False,
            "issue": f"课题 {project_ref or proj_dir.name} 的 Manifest Frontmatter 解析失败",
        }
    if not has_fm or str(meta.get("doc_type") or "").strip().lower() != "manifest":
        return {
            "project_ref": project_ref or proj_dir.name,
            "ok": False,
            "issue": f"课题 {project_ref or proj_dir.name} 的 Manifest doc_type 非法",
        }
    req_keys = ("title", "status", "project_id", "default_mode")
    missing_k = [k for k in req_keys if not str(meta.get(k) or "").strip()]
    if missing_k:
        return {
            "project_ref": project_ref or proj_dir.name,
            "ok": False,
            "issue": f"课题 {project_ref or proj_dir.name} 的 Manifest 缺少必填属性: {', '.join(missing_k)}",
        }
    if fast_check and not append_history:
        status_tag_fast = f"PASS ({gate_score}/60)" if gate_passed else f"BLOCKED ({gate_score}/60)"
        return {
            "project_ref": project_ref or str(meta.get("project_id") or proj_dir.name),
            "project_id": str(meta.get("project_id") or proj_dir.name),
            "ok": True,
            "manifest_updated": False,
            "history_appended": False,
            "fast_checked": True,
            "batch_id": "",
            "batch_state": "",
            "agent": (agent_name or "ai").strip() or "ai",
            "action": (action_name or "delivery-check").strip() or "delivery-check",
            "deliverable_links": [],
            "incremental_deliverables": [],
            "incremental_count": 0,
            "gate_status": status_tag_fast,
        }

    art_cnt = _count_compartment_files(proj_dir / "10-Artifacts")
    mem_cnt = _count_compartment_files(proj_dir / "20-Memory")
    lib_cnt = _count_compartment_files(proj_dir / "20-Memory" / "21-Library")
    atm_cnt = _count_compartment_files(proj_dir / "20-Memory" / "22-Atomics")
    moc_cnt = _count_compartment_files(proj_dir / "20-Memory" / "23-MOC")
    slc_cnt = _count_compartment_files(proj_dir / "20-Memory" / "24-Slices")
    wb_cnt = _count_compartment_files(proj_dir / "30-Workbench")

    wb_root = proj_dir / "30-Workbench"
    ws_dirs = sorted(
        p.name for p in wb_root.iterdir() if p.is_dir() and p.name.startswith("ws-")
    ) if wb_root.is_dir() else []
    ws_desc = ", ".join(f"`{w}`" for w in ws_dirs) if ws_dirs else "未分配专属 `ws-<agent>`"

    def _list_md_rels(sub_path: Path) -> List[str]:
        if not sub_path.is_dir():
            return []
        return sorted(
            p.relative_to(vault_dir).as_posix()
            for p in sub_path.rglob("*.md")
            if p.is_file() and not p.is_symlink() and not p.name.startswith(".")
        )

    all_md = _scan_vault_markdowns(vault_dir)
    n_map = note_reg.sync_paths(vault_ref, all_md) if note_reg is not None else {r: r for r in all_md}
    idx = build_vault_link_index(vault_dir, n_map)
    meta_by_rel = idx.get("meta_by_rel") or {}

    artifact_rels = _list_md_rels(proj_dir / "10-Artifacts")
    moc_rels = _list_md_rels(proj_dir / "20-Memory" / "23-MOC")
    atomic_rels = _list_md_rels(proj_dir / "20-Memory" / "22-Atomics")

    nav_artifact_items = _format_artifact_nav_items(vault_dir, artifact_rels, meta_by_rel)
    nav_moc_links = _format_deliverable_wikilinks(vault_dir, proj_dir, moc_rels, note_reg, vault_ref, meta_by_rel=meta_by_rel)

    actor = (agent_name or "ai").strip() or "ai"
    act_lbl = (action_name or "delivery-check").strip() or "delivery-check"
    now_ts = datetime.now().astimezone().isoformat(timespec="seconds")
    status_tag = f"PASS ({gate_score}/60)" if gate_passed else f"BLOCKED ({gate_score}/60)"
    issues_desc = "无阻塞问题（空间围墙/Schema/双链真值/Manifest台账全绿）" if not gate_issues else "; ".join(gate_issues)

    art_nav_str = "、".join(nav_artifact_items) if nav_artifact_items else "（暂无）"
    moc_nav_str = "、".join(nav_moc_links) if nav_moc_links else "（暂无）"

    summary_lines = [
        _MANIFEST_LEDGER_BEGIN,
        "## 1. 课题核心产出导航 (Project Home Dashboard)",
        "",
        f"- **10-Artifacts（报告成果，{art_cnt} 篇）**：{art_nav_str}",
        f"- **23-MOC（知识地图，{moc_cnt} 篇）**：{moc_nav_str}",
        f"- **20-Memory（底层资产）**：Atomics={atm_cnt}, Library={lib_cnt}, Slices={slc_cnt}",
        f"- **30-Workbench（工作台）**：{wb_cnt} 项文件（工作空间：{ws_desc}）",
        f"- **最新门禁状态**：`{status_tag}`",
        _MANIFEST_LEDGER_END,
    ]
    summary_block = "\n".join(summary_lines)

    updated_raw = raw_txt
    if _MANIFEST_LEDGER_BEGIN in updated_raw and _MANIFEST_LEDGER_END in updated_raw:
        pre_s = updated_raw.split(_MANIFEST_LEDGER_BEGIN, 1)[0]
        post_s = updated_raw.split(_MANIFEST_LEDGER_END, 1)[1]
        updated_raw = pre_s + summary_block + post_s
    else:
        updated_raw = updated_raw.rstrip() + "\n\n" + summary_block + "\n"

    # Parse existing delivery batch blocks to compute true incremental deltas & support open-batch upsert
    hist_header = "## 2. 课题增量交付批次台账 (Incremental Delivery Batches)"
    existing_blocks: List[str] = []
    if _MANIFEST_HISTORY_BEGIN in updated_raw and _MANIFEST_HISTORY_END in updated_raw:
        mid_existing = updated_raw.split(_MANIFEST_HISTORY_BEGIN, 1)[1].split(_MANIFEST_HISTORY_END, 1)[0].strip()
        body_lines = mid_existing.splitlines()
        if body_lines and body_lines[0].startswith("## "):
            mid_body = "\n".join(body_lines[1:]).strip()
        else:
            mid_body = mid_existing
        if "### [" in mid_body:
            existing_blocks = [b.strip() for b in re.split(r"\n\s*---\s*\n", mid_body) if b.strip() and "### [" in b]

    # Collect deliverable keys already sealed in previous completed batches
    sealed_keys: set[str] = set()
    last_is_open = False
    last_attrs: Dict[str, str] = {}
    if existing_blocks:
        last_attrs = _parse_batch_attrs(existing_blocks[-1])
        last_is_open = last_attrs.get("state") == "open"
        prior_blocks = existing_blocks[:-1] if last_is_open else existing_blocks
        for pb in prior_blocks:
            pb_attrs = _parse_batch_attrs(pb)
            if pb_attrs.get("state", "sealed") == "sealed":
                for m_lk in _WIKILINK_EXTRACT_RE.finditer(pb):
                    sealed_keys.add(m_lk.group(1).strip())

    explicit_batch_id = (batch_key or "").strip()
    current_batch_id = ""
    current_batch_state = ""
    incremental_rels: List[str] = []

    if append_history:
        is_delivery_check = act_lbl == "delivery-check"
        if deliverable_rels:
            candidate_rels = list(deliverable_rels)
        else:
            # True incremental delta: only deliverables not yet recorded in prior sealed batches
            all_formal_rels = [*artifact_rels, *moc_rels, *atomic_rels]
            candidate_rels = []
            for r in all_formal_rels:
                lk = str((meta_by_rel.get(r) or {}).get("link_key") or Path(r).stem)
                if lk not in sealed_keys:
                    candidate_rels.append(r)

        t_art = [r for r in candidate_rels if "/10-Artifacts/" in r]
        t_moc = [r for r in candidate_rels if "/23-MOC/" in r]
        t_atm = [r for r in candidate_rels if "/22-Atomics/" in r]
        t_other = [r for r in candidate_rels if r not in t_art and r not in t_moc and r not in t_atm]

        l_art = _format_deliverable_wikilinks(vault_dir, proj_dir, t_art, note_reg, vault_ref, meta_by_rel=meta_by_rel)
        l_moc = _format_deliverable_wikilinks(vault_dir, proj_dir, t_moc, note_reg, vault_ref, meta_by_rel=meta_by_rel)
        l_atm = _format_deliverable_wikilinks(vault_dir, proj_dir, t_atm, note_reg, vault_ref, meta_by_rel=meta_by_rel)
        l_oth = _format_deliverable_wikilinks(vault_dir, proj_dir, t_other, note_reg, vault_ref, meta_by_rel=meta_by_rel)

        can_upsert_open = (
            last_is_open
            and (not explicit_batch_id or last_attrs.get("id") == explicit_batch_id)
            and not (is_delivery_check and not gate_passed)
        )
        if is_delivery_check and last_is_open and last_attrs.get("status") == "BLOCKED":
            can_upsert_open = False

        merged_links = {"artifacts": l_art, "moc": l_moc, "atomics": l_atm, "other": l_oth}
        agents_list: List[str] = []
        if can_upsert_open:
            prev_blk = existing_blocks[-1]
            prev_links = _extract_block_wikilinks(prev_blk)
            for k in ("artifacts", "moc", "atomics", "other"):
                merged_links[k] = _merge_unique_links(prev_links[k], merged_links[k])
            for a in [x.strip() for x in last_attrs.get("agents", "").split(",") if x.strip()]:
                if a not in agents_list:
                    agents_list.append(a)
            current_batch_id = last_attrs.get("id") or explicit_batch_id or f"batch_{len(existing_blocks):02d}"
        else:
            next_num = len(existing_blocks) + 1
            current_batch_id = explicit_batch_id or f"batch_{next_num:02d}"
        if actor not in agents_list:
            agents_list.append(actor)

        if is_delivery_check:
            current_batch_state = "sealed" if gate_passed else "blocked"
        else:
            current_batch_state = "open"

        batch_link_keys = {
            _link_key_of(lk)
            for bucket in ("artifacts", "moc", "atomics", "other")
            for lk in merged_links[bucket]
        }
        for r in [*artifact_rels, *moc_rels, *atomic_rels]:
            lk = str((meta_by_rel.get(r) or {}).get("link_key") or Path(r).stem)
            if lk in batch_link_keys:
                incremental_rels.append(r)
        for r in candidate_rels:
            if r not in incremental_rels:
                incremental_rels.append(r)

        grouped_parts: List[str] = []
        if merged_links["artifacts"]:
            grouped_parts.append(f"报告：{'、'.join(merged_links['artifacts'])}")
        if merged_links["moc"]:
            grouped_parts.append(f"MOC：{'、'.join(merged_links['moc'])}")
        if merged_links["atomics"]:
            grouped_parts.append(f"Atomic：{'、'.join(merged_links['atomics'])}")
        if merged_links["other"]:
            grouped_parts.append(f"其他：{'、'.join(merged_links['other'])}")
        links_str = " | ".join(grouped_parts) if grouped_parts else "（本批次暂无新增报告/MOC/Atomic 正式交付物，仅核验课题基线）"
        vref_desc = f" | 核验句柄: {', '.join(f'`{r}`' for r in verified_refs)}" if verified_refs else ""
        agents_tag_str = " · ".join(f"Agent: `{a}`" for a in agents_list)
        added_cnt = sum(len(merged_links[k]) for k in ("artifacts", "moc", "atomics", "other"))
        status_word = "PASS" if gate_passed else "BLOCKED"
        batch_meta_comment = (
            f'<!-- atlas:delivery-batch id="{current_batch_id}" state="{current_batch_state}" '
            f'status="{status_word}" score="{gate_score}/60" agents="{",".join(agents_list)}" '
            f'added_count="{added_cnt}" -->'
        )
        new_entry = "\n".join(
            [
                batch_meta_comment,
                f"### [{now_ts}] · 批次 `{current_batch_id}` ({current_batch_state}) · {agents_tag_str} · 交付阶段: `{act_lbl}`",
                f"- **本次增量交付**：{links_str}",
                f"- **验收状态**：`{status_tag}`{vref_desc}",
                f"- **验收明细**：{issues_desc}",
            ]
        )
        if can_upsert_open:
            new_blocks = [*existing_blocks[:-1], new_entry]
        else:
            new_blocks = [*existing_blocks, new_entry]
        history_body = "\n\n---\n\n".join(new_blocks)
        history_block = f"{_MANIFEST_HISTORY_BEGIN}\n{hist_header}\n\n{history_body}\n{_MANIFEST_HISTORY_END}"
        if _MANIFEST_HISTORY_BEGIN in updated_raw and _MANIFEST_HISTORY_END in updated_raw:
            pre_h = updated_raw.split(_MANIFEST_HISTORY_BEGIN, 1)[0]
            post_h = updated_raw.split(_MANIFEST_HISTORY_END, 1)[1]
            updated_raw = pre_h + history_block + post_h
        else:
            updated_raw = updated_raw.rstrip() + "\n\n" + history_block + "\n"
    else:
        incremental_rels = list(deliverable_rels or [])

    if updated_raw != raw_txt:
        _atomic_write_md(mf, updated_raw)

    hist_section = updated_raw.split(_MANIFEST_HISTORY_BEGIN, 1)[1].split(_MANIFEST_HISTORY_END, 1)[0] if _MANIFEST_HISTORY_BEGIN in updated_raw else ""
    entry_count = hist_section.count("### [")
    separator_count = hist_section.count("\n---\n")

    safe_entry_links: List[str] = []
    seen_safe: set[str] = set()
    for rel in incremental_rels:
        if rel in seen_safe:
            continue
        seen_safe.add(rel)
        nref = n_map.get(rel) if note_reg is not None else None
        stem = Path(rel).stem
        safe_entry_links.append(f"[[{nref}|{stem}]]" if nref else f"[[{stem}]]")

    return {
        "project_ref": project_ref or str(meta.get("project_id") or proj_dir.name),
        "project_id": str(meta.get("project_id") or proj_dir.name),
        "ok": True,
        "manifest_updated": updated_raw != raw_txt,
        "history_appended": append_history,
        "batch_id": current_batch_id,
        "batch_state": current_batch_state,
        "agent": actor,
        "action": act_lbl,
        "deliverable_links": safe_entry_links,
        "incremental_deliverables": safe_entry_links,
        "incremental_count": len(safe_entry_links),
        "history_entry_count": entry_count,
        "separator_count": separator_count,
        "counts": {
            "artifacts": art_cnt,
            "memory": mem_cnt,
            "library": lib_cnt,
            "atomics": atm_cnt,
            "moc": moc_cnt,
            "slices": slc_cnt,
            "workbench": wb_cnt,
        },
        "workspaces": ws_dirs,
        "gate_status": status_tag,
    }
