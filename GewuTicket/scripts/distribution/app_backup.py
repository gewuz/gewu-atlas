"""Owner-only runtime state & data backup/verify/restore injected into every Forge-generated App."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tarfile
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

_SELF_DIR = Path(__file__).resolve().parent
APP_DIR = Path(__file__).resolve().parents[2]
ROOT_DIR = APP_DIR.parent
for _cand in (_SELF_DIR, _SELF_DIR.parents[1] / "scripts" / "distribution"):
    if (_cand / "archive_io.py").is_file() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))
        break
from archive_io import ArchiveMember, FileSource, read_archive, write_archive

LIMIT = 128 * 1024 * 1024
_KEY_SLOT_NAME = "." + "vault" + "_kek"


def _need(cond: Any, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _encoded(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _safe(path: Path) -> Path:
    for p in [path, *path.parents]:
        _need(not p.is_symlink(), "linked path refused")
    return path


def _canonical_cli_path(raw_path: Path) -> Path:
    base = raw_path if raw_path.is_absolute() else (ROOT_DIR / raw_path)
    _need(".." not in base.parts, "canonical path required")
    _need(not base.is_symlink(), "linked path refused")
    parent_res = base.parent.resolve()
    if base.parent != parent_res:
        _need(str(parent_res) == "/private" + str(base.parent), "linked parent directory refused")
    return _safe(parent_res / base.name)


def _rel_posix(name: str) -> PurePosixPath:
    p = PurePosixPath(name)
    _need(
        isinstance(name, str)
        and name == str(p)
        and not p.is_absolute()
        and not any(part in ("..", ".", "") for part in p.parts)
        and "\\" not in name
        and ":" not in name
        and "\x00" not in name,
        "invalid archive relative path",
    )
    return p


def _log_private(tag: str, message: str) -> None:
    try:
        custom = os.environ.get("GEWU_APP_RUNTIME_DIR", "").strip()
        log_dir = (Path(custom) / "logs") if custom else (APP_DIR / ".local/logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(log_dir, 0o700)
        log_file = log_dir / "app-backup-diagnostics.log"
        fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now(timezone.utc).isoformat()}] {tag}: {message}\n")
        os.chmod(log_file, 0o600)
    except OSError:
        pass


def _load_json(path: Path) -> Dict[str, Any]:
    _safe(path)
    return json.loads(path.read_text("utf-8"))


def _resolve_runtime_dir(app_id: str) -> Path:
    custom = os.environ.get("GEWU_APP_RUNTIME_DIR", "").strip()
    if custom:
        return _safe(Path(custom).resolve())
    loc_file = ROOT_DIR / ".runtime" / app_id / "runtime-location.json"
    if loc_file.is_file():
        loc = _load_json(loc_file)
        return _safe(Path(loc["runtime_dir"]).resolve())
    return _safe(APP_DIR / ".local")


def _resolve_backup_dir(out_dir: Optional[Path], create: bool = False) -> Path:
    if out_dir is None:
        custom = os.environ.get("GEWU_APP_RUNTIME_DIR", "").strip()
        bdir = (_safe(Path(custom).resolve()) / "backups") if custom else _safe(APP_DIR / ".local/backups")
    else:
        bdir = _canonical_cli_path(out_dir)
    if create:
        if not bdir.exists():
            _need(bdir.parent.is_dir() or out_dir is None, "backup parent directory missing")
            bdir.mkdir(parents=(out_dir is None), exist_ok=True)
        os.chmod(bdir, 0o700)
    return bdir


def _atomic_write_0600(path: Path, data: bytes) -> None:
    _safe(path)
    fd, tmp_str = tempfile.mkstemp(dir=path.parent, prefix=".tmp-bak-")
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _is_key_slot(filename: str) -> bool:
    return filename == _KEY_SLOT_NAME or filename.endswith((".key", ".pem"))


def _walk_regular_tree(
    src_root: Path,
    arc_prefix: str,
    *,
    file_mode: int,
    dir_mode: int,
    skip_keys: bool = False,
) -> Tuple[Dict[str, FileSource], List[str], int, bool]:
    files_map: Dict[str, FileSource] = {}
    dir_list: List[str] = []
    skipped_keys = False
    total_size = 0
    if not src_root.exists():
        return files_map, dir_list, 0, False
    _safe(src_root)
    _need(src_root.is_dir(), "expected directory for backup source")
    dir_list.append(arc_prefix)
    for cur_root, dirs, files in os.walk(src_root, followlinks=False):
        cur_p = Path(cur_root)
        _safe(cur_p)
        dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", ".git", "backups", "dist", "build"))
        rel_cur = cur_p.relative_to(src_root).as_posix()
        cur_arc = arc_prefix if rel_cur == "." else f"{arc_prefix}/{rel_cur}"
        if cur_arc not in dir_list:
            dir_list.append(cur_arc)
        for fname in sorted(files):
            fpath = cur_p / fname
            _safe(fpath)
            st = fpath.stat()
            _need(stat.S_ISREG(st.st_mode) and st.st_nlink == 1, "unsupported non-regular file in backup scope")
            if skip_keys and _is_key_slot(fname):
                skipped_keys = True
                continue
            source = FileSource.from_path(fpath, file_mode)
            total_size += source.size
            _need(total_size <= LIMIT, "backup payload exceeds size limit")
            files_map[f"{cur_arc}/{fname}"] = source
    return files_map, sorted(set(dir_list)), total_size, skipped_keys


def create_backup(
    *,
    out_dir: Optional[Path] = None,
    kek_mode: str = "exclude",
    note: str = "",
) -> Dict[str, Any]:
    _need(kek_mode in ("exclude", "include"), "kek_mode must be exclude or include")
    app_meta = _load_json(APP_DIR / "app.json")
    app_id = app_meta["app_id"]
    dl_file = APP_DIR / "contracts/data-layout-profile.json"
    data_layout = _load_json(dl_file) if dl_file.is_file() else {"separation": "separated", "data_dirs": [], "backup_include_data_dirs": True}
    runtime_dir = _resolve_runtime_dir(app_id)
    backup_dir = _resolve_backup_dir(out_dir, create=True)

    all_files: Dict[str, FileSource] = {}
    all_dirs: List[str] = []
    state_count = 0
    data_count = 0
    kek_included = False
    total_size = 0

    # 1. Runtime state (encrypted vault container; key slots controlled by kek_mode)
    state_dir = runtime_dir / "state"
    s_files, s_dirs, s_bytes, saw_skipped_key = _walk_regular_tree(
        state_dir,
        "state",
        file_mode=0o600,
        dir_mode=0o700,
        skip_keys=(kek_mode == "exclude"),
    )
    if kek_mode == "include":
        kek_included = any(_is_key_slot(PurePosixPath(k).name) for k in s_files)
        _need(kek_included, "kek include requested but no runtime key slot found")
    all_files.update(s_files)
    all_dirs.extend(s_dirs)
    state_count = len(s_files)
    total_size += s_bytes

    # 2. Configured data directories (in-App embedded dirs and/or runtime/data)
    if data_layout.get("backup_include_data_dirs", True):
        seen_data_roots = set()
        for rel_d in data_layout.get("data_dirs", []):
            p = _rel_posix(rel_d)
            for candidate_root, arc_name in (
                (APP_DIR / str(p), f"data/{p}"),
                (runtime_dir / "data" / str(p), f"data/runtime/{p}"),
            ):
                if candidate_root.exists():
                    rc = str(candidate_root.resolve())
                    if rc in seen_data_roots:
                        continue
                    seen_data_roots.add(rc)
                    d_files, d_dirs, d_bytes, _ = _walk_regular_tree(
                        candidate_root,
                        arc_name,
                        file_mode=0o644,
                        dir_mode=0o750,
                        skip_keys=False,
                    )
                    all_files.update(d_files)
                    all_dirs.extend(d_dirs)
                    data_count += len(d_files)
                    total_size += d_bytes
        rt_data = runtime_dir / "data"
        if rt_data.exists() and str(rt_data.resolve()) not in seen_data_roots:
            # Avoid double-walking declared subdirs inside runtime_dir/data
            if not any(k.startswith("data/runtime/") for k in all_files):
                d_files, d_dirs, d_bytes, _ = _walk_regular_tree(
                    rt_data,
                    "data/runtime",
                    file_mode=0o644,
                    dir_mode=0o750,
                    skip_keys=False,
                )
                all_files.update(d_files)
                all_dirs.extend(d_dirs)
                data_count += len(d_files)
                total_size += d_bytes

    _need(total_size <= LIMIT, "backup total exceeds 128 MiB limit")

    backup_id = f"bak_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    created_at = datetime.now(timezone.utc).isoformat()
    rows = [
        {"path": name, "sha256": source.sha256.removeprefix("sha256:"), "size": source.size, "mode": source.mode}
        for name, source in sorted(all_files.items())
    ]
    all_dirs_sorted = sorted(set(all_dirs))
    content_digest = _sha256(_encoded({"files": rows, "directories": all_dirs_sorted}))

    manifest = {
        "schema": "forge.app-backup-manifest/1",
        "backup_id": backup_id,
        "app_id": app_id,
        "separation": data_layout.get("separation", "separated"),
        "kek_mode": kek_mode,
        "kek_included": kek_included,
        "key_slots_excluded": bool(saw_skipped_key),
        "state_item_count": state_count,
        "data_file_count": data_count,
        "data_dir_count": len(all_dirs_sorted),
        "directories": all_dirs_sorted,
        "files": rows,
        "content_digest": content_digest,
        "note": note.strip()[:200],
        "created_at": created_at,
    }

    archive_path = backup_dir / f"{backup_id}.tar.gz"
    manifest_sidecar = backup_dir / f"{backup_id}.manifest.json"
    _need(not archive_path.exists() and not manifest_sidecar.exists(), "backup id collision")

    write_archive(
        archive_path,
        all_files,
        all_dirs_sorted,
        "BACKUP_MANIFEST.json",
        manifest,
        directory_mode=lambda name: 0o700 if name == "state" or name.startswith("state/") else 0o750,
        manifest_mode=0o600,
        archive_mode=0o600,
    )
    archive_sha = FileSource.from_path(archive_path, 0o600).sha256.removeprefix("sha256:")

    sidecar = {
        "schema": "forge.app-backup-summary/1",
        "backup_id": backup_id,
        "app_id": app_id,
        "kek_mode": kek_mode,
        "kek_included": kek_included,
        "state_item_count": state_count,
        "data_file_count": data_count,
        "data_dir_count": len(all_dirs_sorted),
        "content_digest": content_digest,
        "archive_sha256": archive_sha,
        "created_at": created_at,
    }
    _atomic_write_0600(manifest_sidecar, _encoded(sidecar))
    return _sanitized_receipt("forge.app-backup-receipt/1", sidecar)


def _sanitized_receipt(schema: str, summary: Dict[str, Any]) -> Dict[str, Any]:
    out = {
        "schema": schema,
        "ok": True,
        "backup_id": summary["backup_id"],
        "app_id": summary["app_id"],
        "kek_mode": summary["kek_mode"],
        "kek_included": summary["kek_included"],
        "state_item_count": summary["state_item_count"],
        "data_file_count": summary["data_file_count"],
        "data_dir_count": summary["data_dir_count"],
        "content_digest": summary["content_digest"],
        "archive_sha256": summary.get("archive_sha256"),
        "created_at": summary["created_at"],
    }
    if not summary["kek_included"]:
        out["key_notice"] = "主密钥槽未打包进备份归档，异地恢复时请单独保管或注入外部 KEK。"
    return out


def _resolve_archive(backup_id: Optional[str], archive: Optional[Path], backup_dir: Optional[Path]) -> Path:
    if archive is not None:
        return _canonical_cli_path(archive)
    _need(backup_id and re.fullmatch(r"bak_[0-9TZ_a-f]+", backup_id), "valid --backup-id required")
    bdir = _resolve_backup_dir(backup_dir, create=False)
    return _safe(bdir / f"{backup_id}.tar.gz")


@contextmanager
def _open_and_verify(archive_path: Path):
    with read_archive(
        archive_path,
        max_archive_bytes=LIMIT * 2,
        max_uncompressed_bytes=LIMIT,
        max_members=10000,
        file_modes=(0o600, 0o644, 0o755),
        directory_modes=(0o700, 0o750, 0o755),
    ) as view:
        files = dict(view.files)
        _need("BACKUP_MANIFEST.json" in files, "missing BACKUP_MANIFEST.json")
        manifest = json.loads(files.pop("BACKUP_MANIFEST.json").read_bytes().decode("utf-8"))
        _need(manifest.get("schema") == "forge.app-backup-manifest/1", "invalid backup manifest schema")
        rows = manifest["files"]
        expected_dirs = sorted(manifest.get("directories", []))
        _need(view.directories == expected_dirs, "backup directory list mismatch")
        _need(_sha256(_encoded({"files": rows, "directories": expected_dirs})) == manifest["content_digest"], "backup manifest digest mismatch")
        _need(len(rows) == len(files) and {r["path"] for r in rows} == set(files), "backup member set mismatch")
        for row in rows:
            member = files[row["path"]]
            _need(
                member.size == row["size"]
                and member.sha256.removeprefix("sha256:") == row["sha256"]
                and member.mode == row["mode"],
                "backup file digest mismatch",
            )
        key_slots = {
            name: member
            for name, member in files.items()
            if name.startswith("state/") and _is_key_slot(PurePosixPath(name).name)
        }
        actual_kek_included = bool(key_slots)
        _need(manifest.get("kek_mode") in ("exclude", "include"), "invalid backup kek mode")
        _need(isinstance(manifest.get("kek_included"), bool), "invalid backup kek declaration")
        _need(manifest["kek_included"] == actual_kek_included, "backup kek declaration differs from key slots")
        _need((manifest["kek_mode"] == "include") == actual_kek_included, "backup kek mode differs from key slots")
        for member in key_slots.values():
            _need(member.size > 0 and member.mode == 0o600, "invalid backup key slot")
        manifest["archive_sha256"] = view.archive_sha256.removeprefix("sha256:")
        yield manifest, files, expected_dirs


def _read_and_verify(archive_path: Path) -> Tuple[Dict[str, Any], Dict[str, Tuple[bytes, int]], List[str]]:
    """Compatibility materializer for focused tests; production restore uses _open_and_verify."""
    with _open_and_verify(archive_path) as (manifest, members, directories):
        files = {name: (member.read_bytes(), member.mode) for name, member in members.items()}
        return manifest, files, directories


def list_backups(backup_dir: Optional[Path] = None) -> Dict[str, Any]:
    bdir = _resolve_backup_dir(backup_dir, create=False)
    items: List[Dict[str, Any]] = []
    if bdir.is_dir():
        for f in sorted(bdir.glob("bak_*.manifest.json")):
            _safe(f)
            summary = json.loads(f.read_text("utf-8"))
            _need(summary.get("schema") == "forge.app-backup-summary/1", "invalid backup sidecar")
            items.append(_sanitized_receipt("forge.app-backup-entry/1", summary))
    return {"schema": "forge.app-backup-list/1", "ok": True, "count": len(items), "backups": items}


def restore_backup(
    *,
    backup_id: Optional[str],
    archive: Optional[Path],
    target: Path,
    backup_dir: Optional[Path],
    dry_run: bool,
) -> Dict[str, Any]:
    arc_path = _resolve_archive(backup_id, archive, backup_dir)
    with _open_and_verify(arc_path) as (manifest, files, dirs):
        if dry_run:
            return {
                **_sanitized_receipt("forge.app-backup-restore-plan/1", manifest),
                "dry_run": True,
                "effect": "verified-not-written",
            }
        dest = _canonical_cli_path(target)
        _need(not dest.exists() and dest.parent.is_dir(), "restore requires non-existent target with existing parent")
        dest.mkdir(mode=0o700)
        for dname in dirs:
            dp = _safe(dest / dname)
            dp.mkdir(parents=True, exist_ok=True)
            os.chmod(dp, 0o700 if dname == "state" or dname.startswith("state/") else 0o750)
        for fname, member in sorted(files.items()):
            fp = _safe(dest / fname)
            fp.parent.mkdir(parents=True, exist_ok=True)
            member.copy_to(fp)
        return {
            **_sanitized_receipt("forge.app-backup-restore/1", manifest),
            "dry_run": False,
            "effect": "restored",
            "target": str(dest),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="cmd", required=True)

    bq = subs.add_parser("backup")
    bq.add_argument("--out-dir", type=Path, default=None)
    bq.add_argument("--kek-mode", choices=("exclude", "include"), default="exclude")
    bq.add_argument("--note", default="")

    lq = subs.add_parser("list")
    lq.add_argument("--backup-dir", type=Path, default=None)

    vq = subs.add_parser("verify")
    vgrp = vq.add_mutually_exclusive_group(required=True)
    vgrp.add_argument("--backup-id", default=None)
    vgrp.add_argument("--archive", type=Path, default=None)
    vq.add_argument("--backup-dir", type=Path, default=None)

    rq = subs.add_parser("restore")
    rgrp = rq.add_mutually_exclusive_group(required=True)
    rgrp.add_argument("--backup-id", default=None)
    rgrp.add_argument("--archive", type=Path, default=None)
    rq.add_argument("--backup-dir", type=Path, default=None)
    rq.add_argument("--target", type=Path, required=True)
    rq.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    try:
        if args.cmd == "backup":
            res = create_backup(out_dir=args.out_dir, kek_mode=args.kek_mode, note=args.note)
        elif args.cmd == "list":
            res = list_backups(backup_dir=args.backup_dir)
        elif args.cmd == "verify":
            arc = _resolve_archive(args.backup_id, args.archive, args.backup_dir)
            with _open_and_verify(arc) as (manifest, _, _):
                res = _sanitized_receipt("forge.app-backup-verify/1", manifest)
        else:
            res = restore_backup(
                backup_id=args.backup_id,
                archive=args.archive,
                target=args.target,
                backup_dir=args.backup_dir,
                dry_run=args.dry_run,
            )
        print(json.dumps(res, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, json.JSONDecodeError) as exc:
        _log_private(args.cmd, str(exc))
        print(json.dumps({"ok": False, "effect": "failed", "code": "E_BACKUP_FAILED", "reason": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
