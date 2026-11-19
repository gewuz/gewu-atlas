"""Domain distribution bundler injected into every Forge-generated App.

Packages:
1. Verified Domain source tree (declared in contracts/object-catalog.json + root metadata);
2. Integrated GewuTrack kernel alongside the Domain (verified via MANIFEST.json SHA-256);
3. Declared data directory skeleton (DIRTYPE only, NEVER live business data or secrets).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

_SELF_DIR = Path(__file__).resolve().parent
APP_DIR = Path(__file__).resolve().parents[2]
for _cand in (_SELF_DIR, _SELF_DIR.parents[1] / "scripts" / "distribution"):
    if (_cand / "archive_io.py").is_file() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))
        break
from archive_io import (
    ArchiveMember,
    FileSource,
    is_shell_bundle,
    parse_shell_bundle_metadata,
    read_archive,
    write_archive,
    write_shell_bundle,
)

ROOT_DIR = APP_DIR.parent
LIMIT = 64 * 1024 * 1024

_FORBIDDEN_ARCHIVE_PATTERNS = (
    re.compile(r"(^|/)(?:\.local|\.runtime|\.git|__pycache__|build|dist|backups)(?:/|$)"),
    re.compile(r"(^|/)(?:.*\.enc\.db(?:-.*)?|vault\.enc|\.vault_kek|kek\.key|.*\.key|.*\.pem|\.env(?:\.[^/]+)?)$"),
)

_BASE_FIXED_APP_FILES = (
    "AGENTS.md",
    "app.json",
    "bootstrap.py",
    "gewu",
    "contracts/bootstrap-profile.json",
    "contracts/object-catalog.json",
    ".gitignore",
    ".cursorignore",
    ".claudeignore",
)
_FIXED_APP_FILES = _BASE_FIXED_APP_FILES + ("references/app-description.md",)


def _is_reference_rel(rel: str) -> bool:
    return rel == "references" or rel.startswith("references/")


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
        log_file = log_dir / "app-dist-diagnostics.log"
        fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now(timezone.utc).isoformat()}] {tag}: {message}\n")
        os.chmod(log_file, 0o600)
    except OSError:
        pass


def _load_json(path: Path) -> Dict[str, Any]:
    _safe(path)
    return json.loads(path.read_text("utf-8"))


def _source(path: Path) -> FileSource:
    _safe(path)
    st = path.stat()
    _need(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_size <= LIMIT, "invalid regular source file")
    mode = 0o755 if (st.st_mode & 0o111) else 0o644
    return FileSource.from_path(path, mode)


def _load_profiles() -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    _safe(APP_DIR)
    app_meta = _load_json(APP_DIR / "app.json")
    objects = _load_json(APP_DIR / "contracts/object-catalog.json")
    boot_prof = _load_json(APP_DIR / "contracts/bootstrap-profile.json")
    dl_file = APP_DIR / "contracts/data-layout-profile.json"
    if dl_file.is_file():
        data_layout = _load_json(dl_file)
    else:
        data_layout = {
            "schema": "forge.data-layout-profile/1",
            "app_id": app_meta["app_id"],
            "directory": app_meta["directory"],
            "separation": "separated",
            "track_integrated": True,
            "data_dirs": [],
            "dist_include_data_structure": False,
            "backup_include_data_dirs": True,
        }
    return app_meta, objects, boot_prof, data_layout


def _collect_plan(
    *,
    include_track: Optional[bool] = None,
    include_data_structure: Optional[bool] = None,
    edition: str = "release",
) -> Tuple[Dict[str, FileSource], List[str], Dict[str, Any]]:
    _need(edition in ("release", "dev"), f"invalid dist edition: {edition}")
    include_references = edition == "dev"

    app_meta, objects, boot_prof, data_layout = _load_profiles()
    app_id = app_meta["app_id"]
    app_dir_name = app_meta["directory"]
    _need(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", app_dir_name), "invalid app directory name")

    # 1. Domain source files declared by object catalog + fixed files
    rel_files = set(_FIXED_APP_FILES if include_references else _BASE_FIXED_APP_FILES)
    for obj in objects.get("objects", []):
        for f in obj.get("files", []):
            rel_files.add(f)
    for extra_dir_name in ("tests", "references", "dicts"):
        extra_dir = APP_DIR / extra_dir_name
        if extra_dir.is_dir() and not extra_dir.is_symlink():
            for cur_root, dirs, files in os.walk(extra_dir, followlinks=False):
                cur = Path(cur_root)
                for child in dirs + files:
                    _need(not (cur / child).is_symlink(), f"linked Domain {extra_dir_name} file refused: {cur / child}")
                dirs[:] = sorted(d for d in dirs if d != "__pycache__")
                for name in files:
                    if name in (".DS_Store",) or name.endswith(".pyc"):
                        continue
                    rel_files.add((cur / name).relative_to(APP_DIR).as_posix())

    raw_data_dirs = [str(_rel_posix(d)) for d in data_layout.get("data_dirs", [])]
    skipped_roots = {".git", ".local", ".runtime", "instances", "build", "dist", "backups", "logs", "validation", "__pycache__"}

    def source_excluded(rel: str) -> bool:
        first = rel.split("/", 1)[0]
        return first in skipped_roots or any(rel == data_dir or rel.startswith(data_dir + "/") for data_dir in raw_data_dirs)

    observed_files = set()
    for cur_root, dirs, files in os.walk(APP_DIR, followlinks=False):
        cur = Path(cur_root)
        for child in dirs + files:
            _need(not (cur / child).is_symlink(), f"linked Domain source refused: {cur / child}")
        dirs[:] = sorted(d for d in dirs if not source_excluded((cur / d).relative_to(APP_DIR).as_posix()))
        for name in files:
            if name in (".DS_Store",) or name.endswith(".pyc"):
                continue
            rel = (cur / name).relative_to(APP_DIR).as_posix()
            if not source_excluded(rel):
                observed_files.add(rel)
    unregistered = sorted(observed_files - rel_files)
    _need(not unregistered, f"unregistered Domain source files: {unregistered}")

    files_map: Dict[str, FileSource] = {}
    excluded_references_files: List[str] = []
    for rel in sorted(rel_files):
        p = _rel_posix(rel)
        arc_name = f"{app_dir_name}/{p}"
        for pat in _FORBIDDEN_ARCHIVE_PATTERNS:
            _need(not pat.search(arc_name), f"forbidden path in source catalog: {rel}")
        src = APP_DIR / str(p)
        _need(src.is_file(), f"declared domain source missing: {rel}")
        if _is_reference_rel(rel) and not include_references:
            excluded_references_files.append(str(p))
            continue
        files_map[arc_name] = _source(src)

    app_files_count = len(files_map)

    # 2. Integrated GewuTrack bundle (only when domain is not standalone)
    standalone = (
        (boot_prof.get("track_integrated") is False)
        or (boot_prof.get("schema") == "forge.bootstrap-profile/2")
        or (data_layout.get("track_integrated") is False)
    )
    track_files_count = 0
    if standalone:
        _need(include_track is not True, "standalone Domain does not integrate GewuTrack")
        use_track = False
        track_path_str = str(_rel_posix(boot_prof.get("track_path") or "GewuTrack"))
        track_version = None
        track_manifest_sha = None
        track_protocol = None
        required_track_adapters: List[str] = []
        workspace_files: List[str] = ["gewu"]
        files_map["gewu"] = _source(_safe(ROOT_DIR / "gewu"))
    else:
        use_track = bool(data_layout.get("track_integrated", True)) if include_track is None else bool(include_track)
        track_path_str = str(_rel_posix(boot_prof["track_path"]))
        track_version = boot_prof.get("track_version")
        track_manifest_sha = boot_prof.get("track_manifest_sha256")
        track_protocol = boot_prof.get("track_protocol")
        required_track_adapters = list(
            boot_prof.get("required_track_adapters")
            or ["track.py", "gewu_proto/cli.py", "gewu_proto/runtime.py", "gewu_proto/venv_runner.py"]
        )
        track_root = _safe(ROOT_DIR / track_path_str)
        if not track_protocol and (track_root / "MANIFEST.json").is_file():
            tm_raw = _source(track_root / "MANIFEST.json").read_bytes()
            if _sha256(tm_raw) == track_manifest_sha:
                track_protocol = json.loads(tm_raw.decode("utf-8")).get("profile")
        if use_track:
            _need(track_root.is_dir(), f"integrated track directory missing: {track_path_str}")
            manifest_path = _safe(track_root / "MANIFEST.json")
            manifest_source = _source(manifest_path)
            manifest_bytes = manifest_source.read_bytes()
            _need(_sha256(manifest_bytes) == track_manifest_sha, "integrated GewuTrack MANIFEST digest mismatch")
            t_manifest = json.loads(manifest_bytes.decode("utf-8"))
            track_version = t_manifest.get("version") or track_version
            track_protocol = t_manifest.get("profile") or track_protocol
            _need(not boot_prof.get("track_version") or track_version == boot_prof["track_version"], "GewuTrack version binding drift")
            track_rows = t_manifest.get("files", [])
            declared_track_files = {"MANIFEST.json"}
            for row in track_rows:
                rel = str(_rel_posix(row["path"]))
                _need(rel not in declared_track_files, f"duplicate GewuTrack manifest path: {rel}")
                declared_track_files.add(rel)
            for adapter_rel in required_track_adapters:
                _need(adapter_rel in declared_track_files, f"GewuTrack missing required Domain adapter: {adapter_rel}")
            excluded_track_files = {str(_rel_posix(name)) for name in t_manifest.get("excluded_source_files", [])}
            excluded_track_dirs = {str(_rel_posix(name)) for name in t_manifest.get("excluded_source_directories", [])}

            def track_source_excluded(rel: str) -> bool:
                return rel in excluded_track_files or any(rel == root or rel.startswith(root + "/") for root in excluded_track_dirs)

            observed_track_files = set()
            for cur_root, dirs, files in os.walk(track_root, followlinks=False):
                cur = Path(cur_root)
                for child in dirs + files:
                    _need(not (cur / child).is_symlink(), f"linked GewuTrack source refused: {cur / child}")
                dirs[:] = [d for d in dirs if d not in ("__pycache__", "validation") and not track_source_excluded((cur / d).relative_to(track_root).as_posix())]
                for name in files:
                    rel = (cur / name).relative_to(track_root).as_posix()
                    if name == ".DS_Store" or name.endswith(".pyc") or track_source_excluded(rel):
                        continue
                    observed_track_files.add(rel)
            _need(observed_track_files == declared_track_files, "GewuTrack source file set differs from MANIFEST")
            files_map[f"{track_path_str}/MANIFEST.json"] = manifest_source
            track_files_count += 1
            for row in track_rows:
                tp = _rel_posix(row["path"])
                arc_tp = f"{track_path_str}/{tp}"
                raw_source = _source(track_root / str(tp))
                _need(raw_source.sha256.removeprefix("sha256:") == row["sha256"].removeprefix("sha256:"), f"GewuTrack file digest drift: {tp}")
                files_map[arc_tp] = raw_source
                track_files_count += 1

        workspace_files = []
        if use_track:
            # Full bundles bootstrap a new workspace; legacy non-standalone domain-only bundles omit root files.
            root_gateway = _safe(ROOT_DIR / "gewu")
            files_map["gewu"] = _source(root_gateway)
            workspace_files.append("gewu")

    # 3. Data directory structure skeleton (DIRTYPE only, NEVER live data files)
    separation = data_layout.get("separation", "separated")
    use_data_skeleton = (
        bool(data_layout.get("dist_include_data_structure", separation == "embedded"))
        if include_data_structure is None
        else bool(include_data_structure)
    )
    skeleton_dirs_set = set()
    excluded_live_files_count = 0
    for rel_dir in raw_data_dirs:
        if use_data_skeleton:
            cur = PurePosixPath(app_dir_name) / rel_dir
            for anc in [cur, *cur.parents]:
                if str(anc) not in (".", app_dir_name):
                    skeleton_dirs_set.add(str(anc))
        disk_dir = APP_DIR / rel_dir
        if disk_dir.exists():
            _safe(disk_dir)
            _need(disk_dir.is_dir(), f"declared data_dir is not directory: {rel_dir}")
            for cur_root, dirs, files in os.walk(disk_dir, followlinks=False):
                cur_p = Path(cur_root)
                _safe(cur_p)
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                excluded_live_files_count += len(files)
                if use_data_skeleton:
                    for dname in dirs:
                        sub = (cur_p / dname).relative_to(APP_DIR).as_posix()
                        skeleton_dirs_set.add(f"{app_dir_name}/{sub}")

    skeleton_dirs = sorted(skeleton_dirs_set) if use_data_skeleton else []
    total_bytes = sum(value.size for value in files_map.values())
    _need(total_bytes <= LIMIT, "domain dist source exceeds 64 MiB limit")

    rows = [
        {"path": name, "sha256": value.sha256.removeprefix("sha256:"), "size": value.size, "mode": value.mode}
        for name, value in sorted(files_map.items())
    ]
    content_digest = _sha256(_encoded(rows))
    manifest = {
        "schema": "forge.app-dist-manifest/1",
        "bundle_kind": "domain-with-track" if use_track else "domain-only",
        "edition": edition,
        "include_references": include_references,
        "excluded_references_files": excluded_references_files,
        "excluded_references_count": len(excluded_references_files),
        "app_id": app_id,
        "directory": app_dir_name,
        "separation": separation,
        "track_bundled": use_track,
        "track_required": not standalone,
        "track_path": track_path_str,
        "track_version": track_version,
        "track_protocol": track_protocol,
        "required_track_adapters": required_track_adapters,
        "track_manifest_sha256": track_manifest_sha,
        "track_files_count": track_files_count,
        "app_files_count": app_files_count,
        "total_files_count": len(files_map),
        "workspace_files": workspace_files,
        "data_structure_included": bool(use_data_skeleton and skeleton_dirs),
        "declared_data_dirs": [f"{app_dir_name}/{d}" for d in raw_data_dirs],
        "data_dirs_skeleton": skeleton_dirs,
        "excluded_live_data_files_count": excluded_live_files_count,
        "excluded_patterns": [
            ".local/",
            ".runtime/",
            "build/",
            "dist/",
            "backups/",
            "*.enc.db",
            "vault.enc",
            ".vault_kek",
            "kek.key",
            "*.key",
            "*.env",
            "live business files inside data_dirs",
            *([] if include_references else ["references/"]),
        ],
        "content_digest": content_digest,
        "files": rows,
    }
    return files_map, skeleton_dirs, manifest


def _write_dist_archive(
    archive_path: Path,
    files_map: Dict[str, FileSource],
    skeleton_dirs: List[str],
    manifest: Dict[str, Any],
) -> None:
    write_archive(
        archive_path,
        files_map,
        skeleton_dirs,
        "DIST_MANIFEST.json",
        manifest,
        directory_mode=0o750,
        manifest_mode=0o644,
        archive_mode=0o644,
    )


def _member_digest(member: ArchiveMember) -> str:
    return member.sha256.removeprefix("sha256:")


def _verify_archive(archive_path: Path) -> Tuple[Dict[str, Any], str]:
    path = _canonical_cli_path(archive_path)
    with read_archive(
        path,
        max_archive_bytes=LIMIT * 2,
        max_uncompressed_bytes=LIMIT,
        max_members=10000,
        file_modes=(0o644, 0o755),
        directory_modes=(0o700, 0o750, 0o755),
    ) as view:
        file_entries = dict(view.files)
        for norm in [*file_entries, *view.directories]:
            _rel_posix(norm)
            for pat in _FORBIDDEN_ARCHIVE_PATTERNS:
                _need(not pat.search(norm), f"forbidden runtime/secret path inside dist: {norm}")

        _need("DIST_MANIFEST.json" in file_entries, "missing DIST_MANIFEST.json")
        manifest = json.loads(file_entries.pop("DIST_MANIFEST.json").read_bytes().decode("utf-8"))
        _need(manifest.get("schema") == "forge.app-dist-manifest/1", "invalid dist manifest schema")
        rows = manifest["files"]
        _need(_sha256(_encoded(rows)) == manifest["content_digest"], "manifest content_digest mismatch")
        _need(len(rows) == len(file_entries) and {r["path"] for r in rows} == set(file_entries), "member file set mismatch")
        for row in rows:
            member = file_entries[row["path"]]
            _need(
                member.size == row["size"]
                and _member_digest(member) == row["sha256"]
                and member.mode == row["mode"],
                f"digest mismatch: {row['path']}",
            )

        expected_dirs = sorted(manifest.get("data_dirs_skeleton", []))
        _need(view.directories == expected_dirs, "data directory skeleton mismatch")
        for dprefix in manifest.get("declared_data_dirs", []):
            for fpath in file_entries:
                _need(not (fpath == dprefix or fpath.startswith(dprefix + "/")), f"live data file leaked into dist: {dprefix}")

        expected_workspace_files = manifest.get("workspace_files", [])
        _need(expected_workspace_files == (["gewu"] if manifest.get("track_bundled") else []), "workspace root file declaration mismatch")
        _need(all(name in file_entries for name in expected_workspace_files), "declared workspace root file missing")
        _need(manifest.get("bundle_kind") == ("domain-with-track" if manifest.get("track_bundled") else "domain-only"), "bundle kind mismatch")
        if manifest.get("track_required") is False:
            _need(not manifest.get("track_bundled"), "standalone bundle must not bundle GewuTrack")
            _need(manifest.get("track_version") is None and manifest.get("track_manifest_sha256") is None, "standalone bundle carries Track binding")
        else:
            _need(isinstance(manifest.get("track_version"), str) and manifest["track_version"], "required GewuTrack version missing")
            _need(isinstance(manifest.get("track_manifest_sha256"), str) and re.fullmatch(r"[a-f0-9]{64}", manifest["track_manifest_sha256"]), "required GewuTrack manifest digest missing")
            if manifest.get("track_protocol") is not None:
                _need(isinstance(manifest["track_protocol"], str) and manifest["track_protocol"], "invalid GewuTrack protocol declaration")
        req_adapters = manifest.get("required_track_adapters", [])
        _need(isinstance(req_adapters, list) and all(isinstance(a, str) and str(_rel_posix(a)) == a for a in req_adapters), "invalid required_track_adapters")

        domain_dir = manifest["directory"]
        domain_files = {name for name in file_entries if name == domain_dir or name.startswith(domain_dir + "/")}
        _need(manifest["app_files_count"] == len(domain_files), "Domain file count mismatch")
        _need(manifest["total_files_count"] == len(file_entries), "total file count mismatch")
        _need(manifest["track_files_count"] == sum(1 for name in file_entries if name.startswith(manifest["track_path"] + "/")), "Track file count mismatch")
        app_meta = json.loads(file_entries[f"{domain_dir}/app.json"].read_bytes().decode("utf-8"))
        _need(app_meta.get("app_id") == manifest["app_id"] and app_meta.get("directory") == domain_dir, "Domain app.json identity mismatch")

        tp = manifest["track_path"]
        if manifest.get("track_bundled"):
            manifest_name = f"{tp}/MANIFEST.json"
            _need(manifest_name in file_entries, "bundled GewuTrack MANIFEST missing")
            track_manifest_raw = file_entries[manifest_name].read_bytes()
            _need(_sha256(track_manifest_raw) == manifest["track_manifest_sha256"], "bundled GewuTrack digest mismatch")
            track_manifest = json.loads(track_manifest_raw.decode("utf-8"))
            _need(track_manifest.get("version") == manifest["track_version"], "bundled GewuTrack version mismatch")
            if manifest.get("track_protocol") is not None:
                _need(track_manifest.get("profile") == manifest["track_protocol"], "bundled GewuTrack protocol mismatch")
            declared = set()
            for row in track_manifest.get("files", []):
                rel = str(_rel_posix(row["path"]))
                _need(rel not in declared, "duplicate GewuTrack manifest path")
                declared.add(rel)
                _need(_member_digest(file_entries[f"{tp}/{rel}"]) == row["sha256"].removeprefix("sha256:"), f"bundled GewuTrack file digest mismatch: {rel}")
            for adapter_rel in req_adapters:
                _need(adapter_rel in declared, f"bundled GewuTrack missing required adapter: {adapter_rel}")
            actual = {name[len(tp) + 1:] for name in file_entries if name.startswith(tp + "/") and name != manifest_name}
            _need(actual == declared, "bundled GewuTrack file set differs from MANIFEST")
        else:
            _need(not any(name == tp or name.startswith(tp + "/") for name in file_entries), "Domain-only bundle contains GewuTrack files")

        edition = manifest.get("edition", "dev")
        include_refs = bool(manifest.get("include_references", True))
        _need(edition in ("release", "dev"), f"invalid edition in manifest: {edition}")
        _need(include_refs == (edition == "dev"), "dist manifest edition/include_references mismatch")
        if not include_refs:
            ref_prefix = f"{domain_dir}/references/"
            _need(not any(name.startswith(ref_prefix) for name in file_entries), "release edition bundle must not contain references/")

        payload_sha256 = view.archive_sha256.removeprefix("sha256:")
        shell_bundle = is_shell_bundle(path)
        if shell_bundle:
            sh_meta = parse_shell_bundle_metadata(path)
            _need(sh_meta.get("GEWU_APP_ID") == manifest["app_id"], "shell header app_id mismatch")
            _need(sh_meta.get("GEWU_BUNDLE_KIND") == manifest["bundle_kind"], "shell header bundle_kind mismatch")
            _need(sh_meta.get("GEWU_EDITION") == edition, "shell header edition mismatch")
            _need(sh_meta.get("GEWU_INCLUDE_REFERENCES") == ("true" if include_refs else "false"), "shell header include_references mismatch")
            _need(sh_meta.get("GEWU_PAYLOAD_SHA256") == payload_sha256, "shell header payload_sha256 mismatch")

        report = {
            "schema": "forge.app-dist-verify/1",
            "ok": True,
            "bundle_format": "sh" if shell_bundle else "tar.gz",
            "edition": edition,
            "include_references": include_refs,
            "excluded_references_count": manifest.get("excluded_references_count", 0),
            "app_id": manifest["app_id"],
            "bundle_kind": manifest["bundle_kind"],
            "separation": manifest["separation"],
            "track_bundled": manifest["track_bundled"],
            "track_files_count": manifest["track_files_count"],
            "app_files_count": manifest["app_files_count"],
            "data_structure_included": manifest["data_structure_included"],
            "data_dirs_skeleton": manifest["data_dirs_skeleton"],
            "excluded_live_data_files_count": manifest["excluded_live_data_files_count"],
            "content_digest": manifest["content_digest"],
            "payload_tar_gz_sha256": payload_sha256,
        }
        return report, payload_sha256


def verify_archive(archive_path: Path) -> Dict[str, Any]:
    return _verify_archive(archive_path)[0]


def _run_build_or_dist(
    *,
    mode: str,
    out_dir: Optional[Path],
    include_track: Optional[bool],
    include_data_structure: Optional[bool],
    edition: str = "release",
    dist_format: str = "sh",
) -> Dict[str, Any]:
    _need(dist_format in ("sh", "tar.gz", "both"), f"invalid dist format: {dist_format}")
    files_map, skeleton_dirs, manifest = _collect_plan(
        include_track=include_track,
        include_data_structure=include_data_structure,
        edition=edition,
    )
    build_id = uuid.uuid4().hex
    if mode == "build":
        stage_dir = _safe(APP_DIR / "build" / build_id)
        stage_dir.mkdir(parents=True, exist_ok=False)
        stage_archive = stage_dir / f"{manifest['app_id']}-stage.tar.gz"
        (stage_dir / "manifest.json").write_bytes(_encoded(manifest))
        _write_dist_archive(stage_archive, files_map, skeleton_dirs, manifest)
        _, stage_archive_sha256 = _verify_archive(stage_archive)
        return {
            "schema": "forge.app-build-report/1",
            "ok": True,
            "build_id": build_id,
            "app_id": manifest["app_id"],
            "bundle_kind": manifest["bundle_kind"],
            "edition": manifest["edition"],
            "include_references": manifest["include_references"],
            "excluded_references_count": manifest["excluded_references_count"],
            "stage_archive": str(stage_archive),
            "stage_archive_sha256": stage_archive_sha256,
            "track_bundled": manifest["track_bundled"],
            "track_files_count": manifest["track_files_count"],
            "app_files_count": manifest["app_files_count"],
            "data_structure_included": manifest["data_structure_included"],
            "data_dirs_skeleton": skeleton_dirs,
            "excluded_live_data_files_count": manifest["excluded_live_data_files_count"],
            "content_digest": manifest["content_digest"],
        }

    if out_dir is None:
        stage_dir = _safe(APP_DIR / "build" / build_id)
        stage_dir.mkdir(parents=True, exist_ok=False)
        stage_archive = stage_dir / f"{manifest['app_id']}-stage.tar.gz"
        (stage_dir / "manifest.json").write_bytes(_encoded(manifest))
        _write_dist_archive(stage_archive, files_map, skeleton_dirs, manifest)
        dest_dir = _safe(APP_DIR / "dist" / build_id)
        dest_dir.mkdir(parents=True, exist_ok=False)
    else:
        dest_dir = _canonical_cli_path(out_dir)
        if dest_dir.exists():
            _need(dest_dir.is_dir(), "out-dir is not a directory")
            _need(not (dest_dir / "dist-report.json").exists(), "out-dir already contains dist-report.json; refuse overwrite")
        else:
            _need(dest_dir.parent.is_dir(), "out-dir parent missing")
            dest_dir.mkdir(parents=False, exist_ok=False)

    scope_slug = "full" if manifest["track_bundled"] else "domain-only"
    stem = f"{manifest['app_id']}-{scope_slug}-{edition}-dist"
    tgz_path = dest_dir / f"{stem}.tar.gz"
    sh_path = dest_dir / f"{stem}.sh"
    if dist_format in ("tar.gz", "both"):
        _need(not tgz_path.exists(), "dist tar.gz bundle already exists")
    if dist_format in ("sh", "both"):
        _need(not sh_path.exists(), "dist shell bundle already exists")

    if out_dir is None:
        archive_bytes = stage_archive.read_bytes()
    else:
        import tempfile as _tmp_dist

        with _tmp_dist.TemporaryDirectory(prefix=".app-dist-tmp-") as td_tmp:
            tmp_tgz = Path(td_tmp).resolve() / f"{stem}.tar.gz"
            _write_dist_archive(tmp_tgz, files_map, skeleton_dirs, manifest)
            archive_bytes = tmp_tgz.read_bytes()

    payload_tar_gz_sha256 = _sha256(archive_bytes)
    archive_tar_gz_out: Optional[str] = None
    installer_script_out: Optional[str] = None
    primary_bundle_path: Optional[Path] = None
    primary_bundle_sha256: Optional[str] = None

    if dist_format in ("tar.gz", "both"):
        with tgz_path.open("xb") as fh:
            fh.write(archive_bytes)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tgz_path, 0o644)
        _, tgz_verified_sha = _verify_archive(tgz_path)
        _need(tgz_verified_sha == payload_tar_gz_sha256, "tar.gz payload sha256 mismatch")
        archive_tar_gz_out = str(tgz_path)
        primary_bundle_path = tgz_path
        primary_bundle_sha256 = tgz_verified_sha

    if dist_format in ("sh", "both"):
        dir_name = manifest["directory"]
        written_sh, sh_sha256 = write_shell_bundle(
            sh_path,
            archive_bytes,
            app_id=manifest["app_id"],
            bundle_kind=manifest["bundle_kind"],
            edition=edition,
            include_references=manifest["include_references"],
            installer_archive_path=f"{dir_name}/scripts/distribution/install_dist.py",
            archive_io_archive_path=f"{dir_name}/scripts/distribution/archive_io.py",
            mode=0o755,
        )
        _, verified_payload_sha = _verify_archive(written_sh)
        _need(verified_payload_sha == payload_tar_gz_sha256, "shell bundle payload sha256 mismatch")
        installer_script_out = str(written_sh)
        primary_bundle_path = written_sh
        primary_bundle_sha256 = sh_sha256

    report = {
        "schema": "forge.app-dist-report/1",
        "ok": True,
        "build_id": build_id,
        "app_id": manifest["app_id"],
        "bundle_kind": manifest["bundle_kind"],
        "bundle_format": dist_format,
        "edition": manifest["edition"],
        "include_references": manifest["include_references"],
        "excluded_references_files": manifest["excluded_references_files"],
        "excluded_references_count": manifest["excluded_references_count"],
        "directory": manifest["directory"],
        "separation": manifest["separation"],
        "track_bundled": manifest["track_bundled"],
        "track_path": manifest["track_path"],
        "track_version": manifest["track_version"],
        "track_manifest_sha256": manifest["track_manifest_sha256"],
        "track_files_count": manifest["track_files_count"],
        "app_files_count": manifest["app_files_count"],
        "data_structure_included": manifest["data_structure_included"],
        "declared_data_dirs": manifest["declared_data_dirs"],
        "data_dirs_skeleton": skeleton_dirs,
        "excluded_live_data_files_count": manifest["excluded_live_data_files_count"],
        "content_digest": manifest["content_digest"],
        "bundle": str(primary_bundle_path),
        "bundle_sha256": primary_bundle_sha256,
        "payload_tar_gz_sha256": payload_tar_gz_sha256,
        "installer_script": installer_script_out,
        "archive_tar_gz": archive_tar_gz_out,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with (dest_dir / "dist-report.json").open("xb") as fh:
        fh.write(_encoded(report))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="cmd", required=True)
    subs.add_parser("inspect")

    for name in ("plan", "build", "dist"):
        q = subs.add_parser(name)
        tgrp = q.add_mutually_exclusive_group()
        tgrp.add_argument("--include-track", dest="include_track", action="store_true", default=None)
        tgrp.add_argument("--no-track", dest="include_track", action="store_false")
        dgrp = q.add_mutually_exclusive_group()
        dgrp.add_argument("--include-data-structure", dest="include_data", action="store_true", default=None)
        dgrp.add_argument("--no-data-structure", dest="include_data", action="store_false")
        q.add_argument("--edition", choices=("release", "dev"), default="release")
        if name == "dist":
            q.add_argument("--out-dir", type=Path, default=None)
            q.add_argument("--format", dest="dist_format", choices=("sh", "tar.gz", "both"), default="sh")

    vq = subs.add_parser("verify")
    vq.add_argument("--archive", type=Path, required=True)

    args = parser.parse_args()
    try:
        if args.cmd == "inspect":
            app_meta, _, boot_prof, data_layout = _load_profiles()
            is_standalone = (
                (boot_prof.get("track_integrated") is False)
                or (boot_prof.get("schema") == "forge.bootstrap-profile/2")
                or (data_layout.get("track_integrated") is False)
            )
            res = {
                "schema": "forge.app-dist-inspection/1",
                "ok": True,
                "app_id": app_meta["app_id"],
                "directory": app_meta["directory"],
                "separation": data_layout.get("separation", "separated"),
                "track_integrated": not is_standalone,
                "track_path": boot_prof.get("track_path"),
                "track_version": boot_prof.get("track_version"),
                "data_dirs": data_layout.get("data_dirs", []),
                "dist_include_data_structure": bool(data_layout.get("dist_include_data_structure", False)),
            }
        elif args.cmd == "plan":
            _, skeleton_dirs, manifest = _collect_plan(
                include_track=args.include_track,
                include_data_structure=args.include_data,
                edition=args.edition,
            )
            res = {
                "schema": "forge.app-dist-plan/1",
                "ok": True,
                "app_id": manifest["app_id"],
                "bundle_kind": manifest["bundle_kind"],
                "edition": manifest["edition"],
                "include_references": manifest["include_references"],
                "excluded_references_files": manifest["excluded_references_files"],
                "excluded_references_count": manifest["excluded_references_count"],
                "separation": manifest["separation"],
                "track_bundled": manifest["track_bundled"],
                "track_files_count": manifest["track_files_count"],
                "app_files_count": manifest["app_files_count"],
                "data_structure_included": manifest["data_structure_included"],
                "data_dirs_skeleton": skeleton_dirs,
                "excluded_live_data_files_count": manifest["excluded_live_data_files_count"],
                "content_digest": manifest["content_digest"],
            }
        elif args.cmd in ("build", "dist"):
            res = _run_build_or_dist(
                mode=args.cmd,
                out_dir=getattr(args, "out_dir", None),
                include_track=args.include_track,
                include_data_structure=args.include_data,
                edition=args.edition,
                dist_format=getattr(args, "dist_format", "sh"),
            )
        else:
            res = verify_archive(args.archive)
        print(json.dumps(res, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, json.JSONDecodeError) as exc:
        _log_private(args.cmd, str(exc))
        print(json.dumps({"ok": False, "effect": "failed", "code": "E_DIST_FAILED", "reason": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
