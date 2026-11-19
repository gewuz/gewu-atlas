"""Standalone Multi-Domain Dist Installer (Python 3.9+ stdlib).

Workflow:
1. Pre-install host & environment check (Python >= 3.9, venv, C compiler if Layer-2, offline dependencies).
2. Preflight bundle compatibility without modifying the target; prepare the isolated environment
   only for accepted bundles. Preserve permissions of an existing target directory.
3. Install `domain-with-track` bundles into empty/matching workspaces, or `domain-only` bundles
   after strictly verifying installed GewuTrack protocol, adapters, version/MANIFEST SHA-256,
   and root ./gewu compatibility. Never replaces GewuTrack or rebinds Domain profiles.
4. Register idempotent Domain-owned entry blocks in root AGENTS.md without overwriting other content.
5. Run verification tests on installed Domains and synchronously roll back added AGENTS.md entry
   blocks and partial Domain artifacts if verification fails or an installation error aborts the run.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Set, Tuple

SCRIPTS_SELF_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
for _p in (SCRIPTS_SELF_DIR, SCRIPTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from archive_io import ArchiveMember, ArchiveView, is_shell_bundle, read_archive

LIMIT = 128 * 1024 * 1024
_AGENTS_MAX_BYTES = 12000
_FORBIDDEN_ARCHIVE_PATTERNS = (
    re.compile(r"(^|/)(?:\.local|\.runtime|\.git|__pycache__|build|dist|backups)(?:/|$)"),
    re.compile(r"(^|/)(?:.*\.enc\.db(?:-.*)?|vault\.enc|\.vault_kek|kek\.key|.*\.key|.*\.pem|\.env(?:\.[^/]+)?)$"),
)


def _need(cond: Any, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _encoded(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _safe(path: Path) -> Path:
    for p in [path, *path.parents]:
        _need(not p.is_symlink(), f"linked path refused: {p}")
    return path


def _canonical_path(raw: Path) -> Path:
    base = raw if raw.is_absolute() else (Path.cwd() / raw)
    _need(".." not in base.parts, "canonical path without '..' required")
    _need(not base.is_symlink(), f"linked path refused: {base}")
    parent_res = base.parent.resolve()
    if base.parent != parent_res:
        _need(str(parent_res) == "/private" + str(base.parent), f"linked parent refused: {base.parent}")
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
        f"invalid archive path: {name!r}",
    )
    return p


def _write_atomic_bytes(path: Path, body: bytes, mode: int = 0o644) -> None:
    _safe(path.parent)
    fd, tmp_raw = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}-")
    tmp = Path(tmp_raw)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def check_environment(
    *,
    target_dir: Optional[Path] = None,
    wheels_dir: Optional[Path] = None,
    extra_requires: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Validate host runtime prerequisites, venv capability, and target directory cleanliness."""
    py_ver = sys.version_info[:3]
    checks: Dict[str, Any] = {
        "platform": sys.platform,
        "python_version": ".".join(map(str, py_ver)),
        "python_ok": py_ver >= (3, 9, 0),
        "venv_available": False,
        "c_compiler": shutil.which("clang") or shutil.which("cc") or None,
    }
    _need(sys.platform in ("darwin", "linux", "win32"), f"unsupported OS platform: {sys.platform}")
    _need(py_ver >= (3, 9, 0), f"Python 3.9+ required, found {checks['python_version']}")
    probe = subprocess.run([sys.executable, "-m", "venv", "--help"], capture_output=True, text=True, timeout=15)
    checks["venv_available"] = probe.returncode == 0
    _need(checks["venv_available"], "Python stdlib venv module is unavailable")

    if target_dir is not None:
        t = _canonical_path(target_dir)
        if t.exists():
            _need(t.is_dir(), "target exists and is not a directory")
            entries = sorted(e.name for e in t.iterdir() if e.name != ".DS_Store")
            checks["target_exists"] = True
            checks["target_empty"] = len(entries) == 0
            checks["existing_entries"] = entries
        else:
            _need(t.parent.is_dir(), f"target parent directory missing: {t.parent}")
            checks["target_exists"] = False
            checks["target_empty"] = True
            checks["existing_entries"] = []
        checks["target"] = str(t)

    if wheels_dir is not None:
        w = _canonical_path(wheels_dir)
        _need(w.is_dir(), "wheels_dir must be an existing directory")
        checks["wheels_dir"] = str(w)
        checks["wheel_count"] = len(list(w.glob("*.whl")))

    if extra_requires:
        checks["extra_requires"] = list(extra_requires)
    return {"schema": "forge.install-env-check/1", "ok": True, "checks": checks}


_PKG_SPEC_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9._,-]+\])?(?:\s*(?:==|!=|<=|>=|<|>|~=)\s*[A-Za-z0-9.*+_!-]+(?:\s*,\s*(?:==|!=|<=|>=|<|>|~=)\s*[A-Za-z0-9.*+_!-]+)*)?"
)

_PKG_PROBE_CODE = """
import importlib, importlib.metadata, re, sys
spec = sys.argv[1]
dist_name = re.split(r"[<>=!~;\\[\\s]", spec, 1)[0].strip()
try:
    dist = importlib.metadata.distribution(dist_name)
except importlib.metadata.PackageNotFoundError:
    mod = dist_name.replace("-", "_")
    if not mod.isidentifier():
        raise SystemExit(1)
    importlib.import_module(mod)
    raise SystemExit(0)
mods = []
top = dist.read_text("top_level.txt")
if top:
    mods = [line.strip() for line in top.splitlines() if line.strip() and not line.strip().startswith("#")]
if not mods and dist.files:
    seen = []
    for f in dist.files:
        parts = f.parts
        if len(parts) > 1 and parts[0].isidentifier() and not parts[0].endswith(".dist-info"):
            if parts[0] not in seen:
                seen.append(parts[0])
        elif len(parts) == 1 and parts[0].endswith(".py") and parts[0][:-3].isidentifier():
            mod = parts[0][:-3]
            if mod not in seen:
                seen.append(mod)
    mods = seen
for mod in mods or [dist_name.replace("-", "_")]:
    importlib.import_module(mod)
"""


def _probe_package_installed(py_bin: Path, pkg_spec: str) -> bool:
    chk = subprocess.run([str(py_bin), "-B", "-c", _PKG_PROBE_CODE, pkg_spec], capture_output=True, text=True, timeout=15)
    return chk.returncode == 0


def _ensure_isolated_venv(target: Path, wheels_dir: Optional[Path], packages: List[str]) -> Tuple[Path, Dict[str, Any]]:
    rt_root = _safe(target / ".runtime" / "install-env")
    rt_root.mkdir(parents=True, exist_ok=True)
    os.chmod(rt_root, 0o700)
    venv_dir = rt_root / "venv"
    py_bin = venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not py_bin.exists():
        proc = subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], capture_output=True, text=True, timeout=90)
        _need(proc.returncode == 0 and py_bin.exists(), f"failed to create isolated venv: {proc.stderr.strip()}")
    vp = subprocess.run([str(py_bin), "-I", "-B", "-c", "import sys,json;print(json.dumps({'prefix':sys.prefix}))"], capture_output=True, text=True, timeout=15)
    _need(vp.returncode == 0, "isolated venv python failed self-check")

    installed_pkgs: List[str] = []
    if packages:
        missing: List[str] = []
        for raw_pkg in packages:
            pkg = raw_pkg.strip()
            _need(bool(_PKG_SPEC_RE.fullmatch(pkg)), f"invalid package spec: {raw_pkg}")
            if not _probe_package_installed(py_bin, pkg):
                missing.append(pkg)
        if missing:
            _need(wheels_dir is not None, f"missing required packages {missing} and no --wheels-dir provided for offline install")
            wdir = _canonical_path(wheels_dir)
            cmd = [str(py_bin), "-m", "pip", "install", "--no-index", "--find-links", str(wdir), *missing]
            iproc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            _need(iproc.returncode == 0, f"offline package install failed: {iproc.stderr.strip()}")
            for pkg in missing:
                _need(_probe_package_installed(py_bin, pkg), f"package installed but import verification failed: {pkg}")
            installed_pkgs = missing
    return py_bin, {
        "venv_dir": str(venv_dir.relative_to(target)),
        "python_bin": str(py_bin),
        "required_packages": packages,
        "installed_packages": installed_pkgs,
        "dependencies_status": "installed" if installed_pkgs else ("stdlib-only-satisfied" if not packages else "preinstalled"),
    }


def _track_manifest_declarations(raw: bytes) -> Tuple[Dict[str, Any], Dict[str, str]]:
    manifest = json.loads(raw.decode("utf-8"))
    _need(isinstance(manifest.get("version"), str) and manifest["version"], "GewuTrack manifest version missing")
    rows = manifest.get("files")
    _need(isinstance(rows, list), "GewuTrack manifest files must be a list")
    declarations: Dict[str, str] = {}
    for row in rows:
        _need(isinstance(row, dict), "invalid GewuTrack manifest row")
        rel = str(_rel_posix(row["path"]))
        sha = str(row["sha256"]).removeprefix("sha256:")
        _need(re.fullmatch(r"[a-f0-9]{64}", sha) and rel not in declarations, "invalid/duplicate GewuTrack manifest declaration")
        declarations[rel] = sha
    return manifest, declarations


def _verify_bundled_track(bundle: Dict[str, Any]) -> None:
    track_path = bundle["track_path"]
    manifest_name = f"{track_path}/MANIFEST.json"
    _need(manifest_name in bundle["track_files"], "bundled GewuTrack missing MANIFEST.json")
    manifest_raw = bundle["track_files"][manifest_name].read_bytes()
    _need(_sha256(manifest_raw) == bundle["track_manifest_sha256"], "bundled GewuTrack MANIFEST digest mismatch")
    manifest, declarations = _track_manifest_declarations(manifest_raw)
    _need(manifest["version"] == bundle["track_version"], "bundled GewuTrack version declaration mismatch")
    if bundle.get("track_protocol") is not None:
        _need(manifest.get("profile") == bundle["track_protocol"], "bundled GewuTrack protocol mismatch")
    for adapter_rel in bundle.get("required_track_adapters", []):
        _need(adapter_rel in declarations, f"bundled GewuTrack missing required adapter: {adapter_rel}")
    payload_names = set(bundle["track_files"]) - {manifest_name}
    expected_names = {f"{track_path}/{rel}" for rel in declarations}
    _need(payload_names == expected_names, "bundled GewuTrack file set differs from MANIFEST")
    for rel, expected_sha in declarations.items():
        _need(
            bundle["track_files"][f"{track_path}/{rel}"].sha256.removeprefix("sha256:") == expected_sha,
            f"bundled GewuTrack file digest drift: {rel}",
        )


def _build_dist_bundle(path: Path, view: ArchiveView) -> Dict[str, Any]:
    files = view.files
    dirs = view.directories
    for norm in [*files, *dirs]:
        for pat in _FORBIDDEN_ARCHIVE_PATTERNS:
            _need(not pat.search(norm), f"forbidden runtime/secret path in bundle: {norm}")

    def member_sha(member: ArchiveMember) -> str:
        return member.sha256.removeprefix("sha256:")

    _need("DIST_MANIFEST.json" in files, f"missing DIST_MANIFEST.json in {path.name}")
    dist_manifest = json.loads(files["DIST_MANIFEST.json"].read_bytes().decode("utf-8"))
    schema = dist_manifest.get("schema")
    _need(schema in ("forge.dist-manifest/1", "forge.app-dist-manifest/1"), f"unsupported dist manifest schema: {schema}")

    if schema == "forge.dist-manifest/1":
        _need("PACKAGE.json" in files, "missing PACKAGE.json in forge dist")
        pkg = json.loads(files["PACKAGE.json"].read_bytes().decode("utf-8"))
        rows = pkg["files"]
        _need(_sha256(_encoded(rows)) == pkg["content_digest"] == dist_manifest["content_digest"], "forge manifest digest mismatch")
        payload_files = {k: v for k, v in files.items() if k not in ("PACKAGE.json", "DIST_MANIFEST.json")}
        _need({r["path"] for r in rows} == set(payload_files), "forge file set mismatch")
        for r in rows:
            member = payload_files[r["path"]]
            _need(member.size == r["size"] and member_sha(member) == r["sha256"] and member.mode == r["mode"], f"file integrity mismatch: {r['path']}")
        domain_dir = "GewuForge"
        app_id = "gewuforge"
        track_bundled = bool(dist_manifest.get("track_integrated"))
        needs_cc = False
    else:
        rows = dist_manifest["files"]
        _need(_sha256(_encoded(rows)) == dist_manifest["content_digest"], "domain manifest digest mismatch")
        payload_files = {k: v for k, v in files.items() if k != "DIST_MANIFEST.json"}
        _need(len(rows) == len(payload_files) and {r["path"] for r in rows} == set(payload_files), "domain file set mismatch")
        for r in rows:
            member = payload_files[r["path"]]
            _need(member.size == r["size"] and member_sha(member) == r["sha256"] and member.mode == r["mode"], f"file integrity mismatch: {r['path']}")
        domain_dir = dist_manifest["directory"]
        app_id = dist_manifest["app_id"]
        track_bundled = bool(dist_manifest.get("track_bundled"))
        sec_member = payload_files.get(f"{domain_dir}/contracts/app-security-profile.json") or payload_files.get(f"{domain_dir}/connect/app-security-profile.json")
        needs_cc = False
        if sec_member is not None:
            sec_prof = json.loads(sec_member.read_bytes().decode("utf-8"))
            needs_cc = sec_prof.get("security_tier") in ("binary-hybrid", "binary-direct")

    standalone = dist_manifest.get("track_required") is False
    _need(not standalone or not track_bundled, "standalone bundle cannot bundle GewuTrack")
    track_path = dist_manifest.get("track_path") or "GewuTrack"
    track_version = dist_manifest.get("track_version")
    track_protocol = dist_manifest.get("track_protocol")
    track_sha = dist_manifest.get("track_manifest_sha256")
    req_adapters = dist_manifest.get("required_track_adapters") or []
    _need(isinstance(req_adapters, list) and all(isinstance(a, str) and str(_rel_posix(a)) == a for a in req_adapters), "invalid required_track_adapters")
    deps: List[str] = list(dist_manifest.get("dependencies", []))

    _need(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", domain_dir) and domain_dir not in ("GewuTrack", ".runtime", ".git", ".agents"), "invalid 2nd-level domain directory")
    _need(track_path == "GewuTrack", "GewuTrack binding must use the canonical GewuTrack path")
    if not standalone:
        _need(isinstance(track_version, str) and track_version, "Domain bundle must declare its required GewuTrack version")
        _need(isinstance(track_sha, str) and re.fullmatch(r"[a-f0-9]{64}", track_sha), "Domain bundle must declare its exact required GewuTrack manifest SHA-256")
    else:
        _need(track_version is None and track_sha is None and not req_adapters, "standalone bundle carries Track requirements")
    if track_protocol is not None:
        _need(isinstance(track_protocol, str) and track_protocol, "invalid track_protocol declaration")

    track_files: Dict[str, ArchiveMember] = {}
    domain_files: Dict[str, ArchiveMember] = {}
    root_files: Dict[str, ArchiveMember] = {}
    forge_root_allowed = {"AGENTS.md", ".gitignore", ".cursorignore", ".claudeignore", "gewu"}
    for k, v in payload_files.items():
        if track_bundled and (k == track_path or k.startswith(track_path + "/")):
            track_files[k] = v
        elif k == domain_dir or k.startswith(domain_dir + "/"):
            domain_files[k] = v
        elif (track_bundled or standalone) and ((schema == "forge.dist-manifest/1" and k in forge_root_allowed) or (schema == "forge.app-dist-manifest/1" and k == "gewu")):
            root_files[k] = v
        else:
            raise ValueError(f"unexpected top-level member outside domain/track in bundle: {k}")

    _need(bool(domain_files), f"bundle has no files under domain directory '{domain_dir}'")
    _need(re.fullmatch(r"[a-z][a-z0-9-]{0,63}", app_id), "invalid Domain app_id")
    if schema == "forge.app-dist-manifest/1":
        _need(f"{domain_dir}/AGENTS.md" in domain_files, "Domain bundle missing Domain-owned AGENTS.md")
        app_meta_name = f"{domain_dir}/app.json"
        _need(app_meta_name in domain_files, "Domain bundle missing app.json")
        app_meta = json.loads(domain_files[app_meta_name].read_bytes().decode("utf-8"))
        _need(app_meta.get("app_id") == app_id and app_meta.get("directory") == domain_dir, "Domain app.json identity differs from dist manifest")
    elif track_bundled:
        _need("AGENTS.md" in root_files or f"{domain_dir}/AGENTS.md" in domain_files, "Forge bundle missing root AGENTS.md")

    domain_skeleton_dirs = sorted(d for d in dirs if d.startswith(domain_dir + "/"))
    cli_alias = app_id
    manifest_member = domain_files.get(f"{domain_dir}/contracts/cli-manifest.json") or domain_files.get(f"{domain_dir}/connect/cli-manifest.json")
    if manifest_member is not None:
        cm = json.loads(manifest_member.read_bytes().decode("utf-8"))
        if "cli_alias" in cm and cm["cli_alias"] is not None:
            _need(isinstance(cm["cli_alias"], str), "invalid cli_alias in cli-manifest.json")
            cand_alias = cm["cli_alias"].strip()
            _need(bool(re.fullmatch(r"[a-z][a-z0-9-]{0,31}", cand_alias)), f"invalid cli_alias in cli-manifest.json: {cand_alias!r}")
            cli_alias = cand_alias
    bundle = {
        "archive": str(path),
        "schema": schema,
        "app_id": app_id,
        "cli_alias": cli_alias,
        "domain_dir": domain_dir,
        "track_bundled": track_bundled,
        "track_required": not standalone,
        "track_path": track_path,
        "track_version": track_version,
        "track_protocol": track_protocol,
        "required_track_adapters": list(req_adapters),
        "track_manifest_sha256": track_sha,
        "dependencies": deps,
        "needs_c_compiler": needs_cc,
        "domain_files": domain_files,
        "domain_skeleton_dirs": domain_skeleton_dirs,
        "track_files": track_files,
        "root_files": root_files,
        "dist_manifest": dist_manifest,
    }
    if track_bundled:
        _verify_bundled_track(bundle)
    expected_kind = "domain-with-track" if track_bundled else "domain-only"
    _need(dist_manifest.get("bundle_kind") == expected_kind, "dist bundle_kind mismatch")
    _need(dist_manifest.get("track_files_count") == len(track_files), "dist Track file count mismatch")
    _need(dist_manifest.get("workspace_files") == sorted(root_files), "dist workspace root file set mismatch")
    if not track_bundled and not standalone:
        _need(not root_files, "Domain-only bundle must not carry root workspace files")
    if schema == "forge.dist-manifest/1":
        _need(dist_manifest.get("forge_files_count") == len(domain_files), "dist Forge file count mismatch")
        _need(dist_manifest.get("total_files_count") == len(payload_files), "dist total file count mismatch")
    else:
        _need(dist_manifest.get("app_files_count") == len(domain_files), "dist Domain file count mismatch")
        _need(dist_manifest.get("total_files_count") == len(payload_files), "dist total file count mismatch")
        _need(dist_manifest.get("data_dirs_skeleton", []) == domain_skeleton_dirs, "dist data directory skeleton mismatch")
    return bundle


def _read_dist_bundle(archive: Path) -> Dict[str, Any]:
    """Materialized bundle reader for focused unit tests."""
    path = _canonical_path(archive)
    max_file_bytes = (LIMIT * 4 + 65536) if is_shell_bundle(path) else (LIMIT * 2)
    _need(path.is_file() and path.stat().st_size <= max_file_bytes, f"dist archive not found or too large: {archive}")
    with read_archive(
        path,
        max_archive_bytes=LIMIT * 2,
        max_uncompressed_bytes=LIMIT,
        max_members=10000,
        file_modes=(0o644, 0o755),
        directory_modes=(0o700, 0o750, 0o755),
    ) as view:
        bundle = _build_dist_bundle(path, view)
        for key in ("domain_files", "track_files", "root_files"):
            bundle[key] = {name: (member.read_bytes(), member.mode) for name, member in bundle[key].items()}
        return bundle


def _open_dist_bundles(archives: List[Path]) -> Tuple[List[Dict[str, Any]], List[ArchiveView]]:
    bundles: List[Dict[str, Any]] = []
    views: List[ArchiveView] = []
    try:
        for archive in archives:
            path = _canonical_path(archive)
            max_file_bytes = (LIMIT * 4 + 65536) if is_shell_bundle(path) else (LIMIT * 2)
            _need(path.is_file() and path.stat().st_size <= max_file_bytes, f"dist archive not found or too large: {archive}")
            view = read_archive(
                path,
                max_archive_bytes=LIMIT * 2,
                max_uncompressed_bytes=LIMIT,
                max_members=10000,
                file_modes=(0o644, 0o755),
                directory_modes=(0o700, 0o750, 0o755),
            )
            try:
                bundles.append(_build_dist_bundle(path, view))
            except Exception:
                view.close()
                raise
            views.append(view)
    except Exception:
        for view in views:
            view.close()
        raise
    return bundles, views


def _read_installed_track_info(target: Path) -> Optional[Dict[str, Any]]:
    track_dir = target / "GewuTrack"
    if not track_dir.exists():
        return None
    _safe(track_dir)
    manifest_file = _safe(track_dir / "MANIFEST.json")
    _need(manifest_file.is_file(), "existing GewuTrack directory lacks MANIFEST.json")
    raw_bytes = manifest_file.read_bytes()
    manifest, declarations = _track_manifest_declarations(raw_bytes)
    excluded_files = {str(_rel_posix(name)) for name in manifest.get("excluded_source_files", [])}
    excluded_dirs = {str(_rel_posix(name)) for name in manifest.get("excluded_source_directories", [])}

    def track_source_excluded(rel: str) -> bool:
        return rel in excluded_files or any(rel == d or rel.startswith(d + "/") for d in excluded_dirs)

    actual_files = set()
    for cur_root, dirs, files in os.walk(track_dir, followlinks=False):
        cur = Path(cur_root)
        for child in dirs + files:
            _need(not (cur / child).is_symlink(), f"linked installed GewuTrack path refused: {cur / child}")
        dirs[:] = [
            d for d in dirs
            if d not in ("__pycache__", "validation")
            and not track_source_excluded((cur / d).relative_to(track_dir).as_posix())
        ]
        for name in files:
            if name == ".DS_Store" or name.endswith(".pyc"):
                continue
            fp = _safe(cur / name)
            rel = fp.relative_to(track_dir).as_posix()
            if track_source_excluded(rel):
                continue
            st = fp.stat()
            _need(stat.S_ISREG(st.st_mode) and st.st_nlink == 1, f"invalid installed GewuTrack file: {fp}")
            actual_files.add(rel)
    expected_files = {"MANIFEST.json", *declarations}
    _need(actual_files == expected_files, f"installed GewuTrack file set drift: missing={sorted(expected_files - actual_files)}, extra={sorted(actual_files - expected_files)}")
    file_hashes: Dict[str, str] = {}
    for rel, expected_sha in declarations.items():
        body = _safe(track_dir / rel).read_bytes()
        actual_sha = _sha256(body)
        _need(actual_sha == expected_sha, f"installed GewuTrack file digest drift: {rel}")
        file_hashes[rel] = actual_sha
    return {
        "version": manifest["version"],
        "protocol": manifest.get("profile"),
        "manifest_sha256": _sha256(raw_bytes),
        "files_count": len(declarations) + 1,
        "declared_files": set(declarations),
        "file_hashes": file_hashes,
    }


def _track_info_from_bundle(bundle: Dict[str, Any]) -> Dict[str, Any]:
    track_path = bundle["track_path"]
    manifest_raw = bundle["track_files"][f"{track_path}/MANIFEST.json"].read_bytes()
    manifest, declarations = _track_manifest_declarations(manifest_raw)
    tprefix = track_path + "/"
    file_hashes = {
        k[len(tprefix):]: member.sha256.removeprefix("sha256:")
        for k, member in bundle["track_files"].items()
        if k != f"{track_path}/MANIFEST.json"
    }
    return {
        "version": manifest["version"],
        "protocol": manifest.get("profile"),
        "manifest_sha256": bundle["track_manifest_sha256"],
        "files_count": len(declarations) + 1,
        "declared_files": set(declarations),
        "file_hashes": file_hashes,
    }


def _track_bundle_matches_installed(bundle: Dict[str, Any], installed: Dict[str, Any]) -> bool:
    if bundle["track_manifest_sha256"] != installed["manifest_sha256"]:
        return False
    if bundle["track_version"] != installed["version"]:
        return False
    tprefix = bundle["track_path"] + "/"
    for k, member in bundle["track_files"].items():
        if k == f"{bundle['track_path']}/MANIFEST.json":
            continue
        rel = k[len(tprefix):]
        if installed["file_hashes"].get(rel) != member.sha256.removeprefix("sha256:"):
            return False
    return True


def _agents_entry_markers(app_id: str) -> Tuple[str, str]:
    return f"<!-- forge:{app_id}:entry -->", f"<!-- /forge:{app_id}:entry -->"


def _agents_entry_block(domain_dir: str, app_id: str, cli_alias: Optional[str] = None) -> str:
    start, end = _agents_entry_markers(app_id)
    alias = (cli_alias or app_id).strip() or app_id
    _need(bool(re.fullmatch(r"[a-z][a-z0-9-]{0,31}", alias)), f"unsafe cli_alias for AGENTS entry: {alias!r}")
    return (
        f"{start}\n"
        f"## {domain_dir} 域入口\n\n"
        f"- 域规则见 [{domain_dir}/AGENTS.md]({domain_dir}/AGENTS.md)。\n"
        f"- 统一命令入口：`./gewu {alias}`。\n"
        f"{end}"
    )


def _plan_agents_entry(
    current_text: Optional[str],
    domain_dir: str,
    app_id: str,
    cli_alias: Optional[str] = None,
) -> Tuple[str, str, Optional[str]]:
    """Return (status, projected_text, conflict_reason) without mutating disk."""
    if app_id == "gewuforge":
        return "not_applicable", current_text or "", None
    base = current_text if current_text is not None else "# 格物工作区\n"
    start, end = _agents_entry_markers(app_id)
    block = _agents_entry_block(domain_dir, app_id, cli_alias=cli_alias)
    if start in base or end in base:
        if block in base:
            return "unchanged", base, None
        return "conflict", base, "root AGENTS.md already contains a different entry block for this Domain; refused overwrite"
    updated = base.rstrip() + "\n\n" + block + "\n"
    if len(updated.encode("utf-8")) > _AGENTS_MAX_BYTES:
        return "conflict", base, f"root AGENTS.md would exceed {_AGENTS_MAX_BYTES} bytes; Domain entry was not added"
    return ("updated" if current_text is not None else "created"), updated, None


def _strip_agents_entry_block(text: str, app_id: str) -> Tuple[str, bool]:
    start, end = _agents_entry_markers(app_id)
    pattern = re.compile(rf"\n*{re.escape(start)}\n.*?\n{re.escape(end)}\n*", flags=re.DOTALL)
    new_text, count = pattern.subn("\n\n", text)
    if count == 0:
        return text, False
    cleaned = new_text.strip()
    return (cleaned + "\n" if cleaned else ""), True


def _precheck_bundle(
    bundle: Dict[str, Any],
    *,
    target: Path,
    planned_dirs: Set[str],
    planned_app_ids: Set[str],
    effective_track: Optional[Dict[str, Any]],
    effective_root_sha: Dict[str, str],
    projected_agents: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Validate every compatibility & collision constraint BEFORE mutating disk."""
    app_id = bundle["app_id"]
    domain_dir = bundle["domain_dir"]

    if (target / domain_dir).exists() or domain_dir in planned_dirs or app_id in planned_app_ids:
        return {
            "conflict_id": f"dir:{domain_dir}",
            "kind": "domain_directory_conflict",
            "app_id": app_id,
            "domain_dir": domain_dir,
            "archive": bundle["archive"],
            "reason": f"directory '{domain_dir}' or app_id '{app_id}' already exists under target; refused overwrite",
        }

    if bundle.get("track_required") is False:
        for rk, rmember in sorted(bundle.get("root_files", {}).items()):
            cand_sha = _sha256(rmember.read_bytes())
            inst_sha = effective_root_sha.get(rk)
            if inst_sha is not None and inst_sha != cand_sha:
                if rk == "gewu":
                    return {
                        "conflict_id": f"gateway:{domain_dir}",
                        "kind": "gewu_gateway_conflict",
                        "app_id": app_id,
                        "domain_dir": domain_dir,
                        "archive": bundle["archive"],
                        "installed_gateway_sha256": inst_sha,
                        "candidate_gateway_sha256": cand_sha,
                        "reason": "root ./gewu differs from candidate domain gateway SHA-256; refused overwrite",
                    }
                return {
                    "conflict_id": f"root-metadata:{rk}",
                    "kind": "root_metadata_conflict",
                    "app_id": app_id,
                    "domain_dir": domain_dir,
                    "archive": bundle["archive"],
                    "path": rk,
                    "installed_sha256": inst_sha,
                    "candidate_sha256": cand_sha,
                    "reason": "existing workspace root metadata differs from standalone bundle; refused overwrite",
                }
    elif bundle["track_bundled"]:
        if effective_track is not None and not _track_bundle_matches_installed(bundle, effective_track):
            return {
                "conflict_id": f"track:{domain_dir}",
                "kind": "gewutrack_version_conflict",
                "app_id": app_id,
                "domain_dir": domain_dir,
                "archive": bundle["archive"],
                "installed_track": {
                    "version": effective_track["version"],
                    "manifest_sha256": effective_track["manifest_sha256"],
                    "source_archive": effective_track.get("source_archive"),
                },
                "candidate_track": {
                    "version": bundle["track_version"],
                    "manifest_sha256": bundle["track_manifest_sha256"],
                    "source_archive": bundle["archive"],
                },
                "resolution_options": [
                    {
                        "action": "use-domain-only-bundle",
                        "description": "Install a Domain-only bundle built against the exact installed GewuTrack manifest; this installer never rebinds a Domain to another Track.",
                    },
                    {
                        "action": "upgrade-track-separately",
                        "description": "Use the independent GewuTrack upgrade process before retrying the full bundle; Domain installation never replaces GewuTrack.",
                    },
                ],
            }
        for rk, rmember in sorted(bundle.get("root_files", {}).items()):
            cand_sha = _sha256(rmember.read_bytes())
            inst_sha = effective_root_sha.get(rk)
            if inst_sha is not None and inst_sha != cand_sha:
                if rk == "gewu":
                    return {
                        "conflict_id": f"gateway:{domain_dir}",
                        "kind": "gewu_gateway_conflict",
                        "app_id": app_id,
                        "domain_dir": domain_dir,
                        "archive": bundle["archive"],
                        "installed_gateway_sha256": inst_sha,
                        "candidate_gateway_sha256": cand_sha,
                        "reason": "root ./gewu differs from candidate domain gateway SHA-256; refused overwrite",
                    }
                return {
                    "conflict_id": f"root-metadata:{rk}",
                    "kind": "root_metadata_conflict",
                    "app_id": app_id,
                    "domain_dir": domain_dir,
                    "archive": bundle["archive"],
                    "path": rk,
                    "installed_sha256": inst_sha,
                    "candidate_sha256": cand_sha,
                    "reason": "existing workspace root metadata differs from the full bundle; refused overwrite",
                }
    else:
        if effective_track is None:
            return {
                "conflict_id": f"track-required:{domain_dir}",
                "kind": "gewutrack_required",
                "app_id": app_id,
                "domain_dir": domain_dir,
                "archive": bundle["archive"],
                "required_track": {
                    "version": bundle["track_version"],
                    "manifest_sha256": bundle["track_manifest_sha256"],
                },
                "reason": "Domain-only bundle requires a pre-installed compatible GewuTrack",
            }
        if bundle.get("track_protocol") and effective_track.get("protocol") and bundle["track_protocol"] != effective_track["protocol"]:
            return {
                "conflict_id": f"track-protocol:{domain_dir}",
                "kind": "gewutrack_protocol_incompatible",
                "app_id": app_id,
                "domain_dir": domain_dir,
                "archive": bundle["archive"],
                "installed_protocol": effective_track.get("protocol"),
                "required_protocol": bundle.get("track_protocol"),
                "reason": "installed GewuTrack protocol profile differs from Domain-only bundle requirement",
            }
        req_adapters = bundle.get("required_track_adapters", [])
        missing_adapters = [a for a in req_adapters if a not in effective_track.get("declared_files", set())]
        if missing_adapters:
            return {
                "conflict_id": f"track-adapters:{domain_dir}",
                "kind": "gewutrack_adapter_missing",
                "app_id": app_id,
                "domain_dir": domain_dir,
                "archive": bundle["archive"],
                "missing_adapters": missing_adapters,
                "reason": "installed GewuTrack lacks action adapter modules required by this Domain",
            }
        if bundle["track_version"] != effective_track["version"] or bundle["track_manifest_sha256"] != effective_track["manifest_sha256"]:
            return {
                "conflict_id": f"track-required:{domain_dir}",
                "kind": "gewutrack_incompatible",
                "app_id": app_id,
                "domain_dir": domain_dir,
                "archive": bundle["archive"],
                "installed_track": {
                    "version": effective_track["version"],
                    "manifest_sha256": effective_track["manifest_sha256"],
                },
                "required_track": {
                    "version": bundle["track_version"],
                    "manifest_sha256": bundle["track_manifest_sha256"],
                },
                "reason": "Domain-only bundle was built against a different GewuTrack; automatic rebind is refused",
            }
        if "gewu" not in effective_root_sha:
            return {
                "conflict_id": f"gateway-required:{domain_dir}",
                "kind": "gewu_gateway_required",
                "app_id": app_id,
                "domain_dir": domain_dir,
                "archive": bundle["archive"],
                "reason": "Domain-only bundle requires the existing workspace root ./gewu; root files are never supplied by this bundle",
            }

    if app_id != "gewuforge":
        status, _, reason = _plan_agents_entry(projected_agents, domain_dir, app_id, cli_alias=bundle.get("cli_alias"))
        if status == "conflict":
            return {
                "conflict_id": f"agents:{domain_dir}",
                "kind": "agents_entry_conflict",
                "app_id": app_id,
                "domain_dir": domain_dir,
                "reason": reason,
            }
    return None


def _write_tree(target: Path, files_map: Dict[str, ArchiveMember], dirs_list: List[str]) -> None:
    for drel in sorted(dirs_list):
        dp = _safe(target / drel)
        dp.mkdir(parents=True, exist_ok=True)
        os.chmod(dp, 0o750)
    for frel, member in sorted(files_map.items()):
        fp = _safe(target / frel)
        member.copy_to(fp)


def _sync_root_files_for_bundle(
    target: Path, bundle: Dict[str, Any], created: List[Path],
) -> None:
    for rk, rmember in sorted(bundle.get("root_files", {}).items()):
        rp = _safe(target / rk)
        if not rp.exists():
            created.append(rp)
            rmember.copy_to(rp)


def _sync_domain_agents_entry(target: Path, domain_dir: str, app_id: str, cli_alias: Optional[str] = None) -> str:
    agents_path = _safe(target / "AGENTS.md")
    existed = agents_path.exists()
    current = agents_path.read_text("utf-8") if existed else None
    status, updated, reason = _plan_agents_entry(current, domain_dir, app_id, cli_alias=cli_alias)
    _need(status != "conflict", reason or "AGENTS.md conflict")
    if status in ("not_applicable", "unchanged"):
        return status
    mode = (agents_path.stat().st_mode & 0o777) if existed else 0o644
    _write_atomic_bytes(agents_path, updated.encode("utf-8"), mode=mode)
    return status


def _ensure_root_gitignore(target: Path) -> Optional[Path]:
    gi = _safe(target / ".gitignore")
    if gi.exists():
        return None
    gi.write_text(
        "# Isolated deployment env, domain encrypted states & backups\n"
        ".runtime/\n"
        "**/.local/\n"
        "*.enc.db\n"
        "vault.enc\n"
        ".vault_kek\n"
        "kek.key\n"
        "*.key\n"
        "*.pem\n"
        ".env\n",
        encoding="utf-8",
    )
    return gi


def _run_domain_tests(root: Path, domain_dir: str, app_id: str, py_bin: Path) -> Dict[str, Any]:
    dom = root / domain_dir
    if app_id == "gewuforge":
        boot_p = subprocess.run([str(py_bin), "-I", "-B", str(dom / "bootstrap.py"), "inspect"], cwd=str(root), capture_output=True, text=True, timeout=30)
        test_p = subprocess.run(
            [str(py_bin), "-I", "-B", str(dom / "scripts/checks/run_tests.py"), "--profile", "unit-operator", "--target", "tests/test_structure_and_rail.py"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=240,
        )
        out_json = {}
        if test_p.stdout.strip():
            try:
                out_json = json.loads(test_p.stdout)
            except json.JSONDecodeError:
                out_json = {"raw_stdout": test_p.stdout[:300]}
        return {
            "domain": domain_dir,
            "app_id": app_id,
            "bootstrap_ok": boot_p.returncode == 0,
            "tests_passed": boot_p.returncode == 0 and test_p.returncode == 0 and out_json.get("status") == "pass",
            "returncode": test_p.returncode,
            "summary": out_json,
        }

    boot_init = subprocess.run([str(py_bin), "-I", "-B", str(dom / "bootstrap.py"), "init-runtime"], cwd=str(root), capture_output=True, text=True, timeout=90)
    runner = dom / "scripts/checks/run_app_tests.py"
    test_p = subprocess.run([str(py_bin), "-I", "-B", str(runner), "--scenario", "release"], cwd=str(root), capture_output=True, text=True, timeout=180)
    out_json = {}
    if test_p.stdout.strip():
        try:
            out_json = json.loads(test_p.stdout)
        except json.JSONDecodeError:
            out_json = {"raw_stdout": test_p.stdout[:300]}
    return {
        "domain": domain_dir,
        "app_id": app_id,
        "init_runtime_ok": boot_init.returncode == 0,
        "tests_passed": boot_init.returncode == 0 and test_p.returncode == 0 and out_json.get("status") == "pass",
        "returncode": test_p.returncode,
        "summary": out_json,
    }


def _run_track_tests(root: Path, py_bin: Path, suite: str = "quick") -> Dict[str, Any]:
    track_dir = root / "GewuTrack"
    manifest_file = track_dir / "MANIFEST.json"
    if manifest_file.is_file():
        manifest = json.loads(manifest_file.read_text("utf-8"))
        for row in manifest.get("files", []):
            fp = _safe(track_dir / str(_rel_posix(row["path"])))
            if not fp.is_file() or _sha256(fp.read_bytes()) != row["sha256"].removeprefix("sha256:"):
                return {"ran": True, "suite": suite, "passed": False, "returncode": 2, "tail": [f"manifest drift: {row['path']}"]}
    if suite == "manifest":
        return {"ran": True, "suite": "manifest", "passed": True, "returncode": 0, "tail": ["GewuTrack MANIFEST SHA-256 verified"]}
    val_script = track_dir / "tests/run_validation.py"
    if not val_script.is_file():
        return {"ran": False, "passed": True, "reason": "GewuTrack/tests/run_validation.py not present"}
    if suite == "quick":
        tests_dir = track_dir / "tests"
        env = dict(os.environ)
        extra = f"{track_dir}{os.pathsep}{tests_dir}"
        env["PYTHONPATH"] = f"{extra}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else extra
        p = subprocess.run(
            [str(py_bin), "-B", "-m", "unittest", "-v", "test_ath", "test_bounded_process", "test_round2_acceptance"],
            cwd=str(tests_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        tail = (p.stdout + "\n" + p.stderr).strip().splitlines()[-5:]
        return {"ran": True, "suite": "quick", "passed": p.returncode == 0, "returncode": p.returncode, "tail": tail}
    p = subprocess.run([str(py_bin), "-B", str(val_script)], cwd=str(track_dir), capture_output=True, text=True, timeout=240)
    return {"ran": True, "suite": "full", "passed": p.returncode == 0, "returncode": p.returncode, "tail": p.stdout.splitlines()[-5:] if p.stdout else []}


def _run_all_target_tests(root: Path, domain_targets: List[Tuple[str, str]], py_bin: Path, include_track: bool, track_suite: str) -> Dict[str, Any]:
    results: Dict[str, Any] = {"track": None, "domains": []}
    jobs = len(domain_targets) + (1 if include_track else 0)
    if jobs == 0:
        return results
    with ThreadPoolExecutor(max_workers=min(jobs, 2)) as pool:
        f_track = pool.submit(_run_track_tests, root, py_bin, track_suite) if include_track else None
        f_doms = [pool.submit(_run_domain_tests, root, ddir, aid, py_bin) for ddir, aid in domain_targets]
        if f_track is not None:
            results["track"] = f_track.result()
        results["domains"] = [f.result() for f in f_doms]
    return results


def _rollback_install_artifacts(
    target: Path,
    *,
    domains_to_remove: List[Dict[str, Any]],
    remove_all_new_root_and_track: bool,
    created_track_dir: Optional[Path],
    created_root_paths: List[Path],
    orig_agents_existed: bool,
    orig_agents_bytes: Optional[bytes],
    orig_agents_mode: int,
) -> None:
    """Synchronously revoke root AGENTS.md entries and remove newly created artifacts from a failed install."""
    for item in domains_to_remove:
        ddir = target / item["domain_dir"]
        if ddir.exists():
            shutil.rmtree(ddir)

    agents_path = target / "AGENTS.md"
    if remove_all_new_root_and_track:
        if orig_agents_existed and orig_agents_bytes is not None:
            _write_atomic_bytes(agents_path, orig_agents_bytes, mode=orig_agents_mode)
        elif agents_path.exists():
            agents_path.unlink()
        for rp in created_root_paths:
            if rp != agents_path and rp.exists():
                rp.unlink()
        if created_track_dir is not None and created_track_dir.exists():
            shutil.rmtree(created_track_dir)
    elif agents_path.is_file():
        text = agents_path.read_text("utf-8")
        changed = False
        for item in domains_to_remove:
            if item["app_id"] == "gewuforge":
                continue
            text, removed = _strip_agents_entry_block(text, item["app_id"])
            changed = changed or removed
        if changed:
            if not orig_agents_existed and not text.strip():
                agents_path.unlink()
            else:
                mode = (agents_path.stat().st_mode & 0o777) or orig_agents_mode
                _write_atomic_bytes(agents_path, text.encode("utf-8"), mode=mode)


def install_bundles(
    *,
    archives: List[Path],
    target_dir: Path,
    wheels_dir: Optional[Path] = None,
    extra_requires: Optional[List[str]] = None,
    init_git: bool = False,
    run_tests: bool = True,
    track_suite: str = "quick",
) -> Dict[str, Any]:
    _need(archives, "at least one --archive or --dist-dir containing *-dist.tar.gz is required")
    bundles, views = _open_dist_bundles(archives)
    try:
        return _install_bundles(
            bundles,
            target_dir=target_dir,
            wheels_dir=wheels_dir,
            extra_requires=extra_requires,
            init_git=init_git,
            run_tests=run_tests,
            track_suite=track_suite,
        )
    finally:
        for view in views:
            view.close()


def _install_bundles(
    bundles: List[Dict[str, Any]],
    *,
    target_dir: Path,
    wheels_dir: Optional[Path],
    extra_requires: Optional[List[str]],
    init_git: bool,
    run_tests: bool,
    track_suite: str,
) -> Dict[str, Any]:
    # All compatibility checks precede target creation, chmod, venv and git writes.
    env_report = check_environment(target_dir=target_dir, wheels_dir=wheels_dir)
    target = _canonical_path(target_dir)

    agents_path = _safe(target / "AGENTS.md")
    orig_agents_existed = agents_path.exists()
    orig_agents_bytes = agents_path.read_bytes() if orig_agents_existed else None
    orig_agents_mode = (agents_path.stat().st_mode & 0o777) if orig_agents_existed else 0o644

    installed_track = _read_installed_track_info(target) if any(b.get("track_required", True) for b in bundles) else None
    if installed_track is not None:
        installed_track["source_archive"] = None
    effective_track = installed_track
    track_source_archive: Optional[str] = None

    effective_root_sha: Dict[str, str] = {}
    for root_name in ("gewu", "AGENTS.md", ".gitignore", ".cursorignore", ".claudeignore"):
        rp = _safe(target / root_name)
        if rp.is_file():
            effective_root_sha[root_name] = _sha256(rp.read_bytes())

    projected_agents: Optional[str] = orig_agents_bytes.decode("utf-8") if orig_agents_bytes is not None else None
    planned_dirs: Set[str] = set()
    planned_app_ids: Set[str] = set()
    accepted_bundles: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []

    for b in bundles:
        conflict = _precheck_bundle(
            b,
            target=target,
            planned_dirs=planned_dirs,
            planned_app_ids=planned_app_ids,
            effective_track=effective_track,
            effective_root_sha=effective_root_sha,
            projected_agents=projected_agents,
        )
        if conflict is not None:
            conflicts.append(conflict)
            continue

        planned_dirs.add(b["domain_dir"])
        planned_app_ids.add(b["app_id"])
        accepted_bundles.append(b)
        if b["track_bundled"] and effective_track is None:
            effective_track = _track_info_from_bundle(b)
            effective_track["source_archive"] = b["archive"]
            track_source_archive = b["archive"]
        for rk, rmember in b.get("root_files", {}).items():
            raw = rmember.read_bytes()
            effective_root_sha.setdefault(rk, _sha256(raw))
            if rk == "AGENTS.md" and projected_agents is None:
                projected_agents = raw.decode("utf-8")
        if b["app_id"] != "gewuforge":
            _, projected_agents, _ = _plan_agents_entry(projected_agents, b["domain_dir"], b["app_id"], cli_alias=b.get("cli_alias"))

    py_bin = Path(sys.executable)
    venv_info: Dict[str, Any] = {"status": "not_prepared_no_accepted_bundles"}
    if accepted_bundles:
        all_deps = list(extra_requires or [])
        for bundle in accepted_bundles:
            for dependency in bundle["dependencies"]:
                if dependency not in all_deps:
                    all_deps.append(dependency)
        if any(bundle["needs_c_compiler"] for bundle in accepted_bundles):
            _need(bool(env_report["checks"]["c_compiler"]), "C compiler (clang/cc) required for binary-tier Domain")
        if not target.exists():
            target.mkdir(mode=0o755, parents=False)
        # Never relax an existing workspace's permissions, including on failure.
        py_bin, venv_info = _ensure_isolated_venv(target, wheels_dir, all_deps)
        if init_git and not (target / ".git").exists():
            subprocess.run(["git", "-C", str(target), "init"], check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(target), "config", "user.email", "deploy@gewu.local"], check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(target), "config", "user.name", "GewuDeploy"], check=True, capture_output=True, text=True)

    installed_domains: List[Dict[str, Any]] = []
    started_domains: List[Dict[str, Any]] = []
    rolled_back_domains: List[str] = []
    created_track_dir: Optional[Path] = None
    created_root_paths: List[Path] = []

    try:
        if accepted_bundles:
            if not (target / ".gitignore").exists():
                created_root_paths.append(target / ".gitignore")
            _ensure_root_gitignore(target)

        for b in accepted_bundles:
            if b["track_bundled"] and installed_track is None:
                track_dir = _safe(target / "GewuTrack")
                track_dir.mkdir()  # Reserve ownership before writing the first member.
                created_track_dir = track_dir
                _write_tree(target, b["track_files"], [])
                installed_track = _read_installed_track_info(target) if any(b.get("track_required", True) for b in bundles) else None
                track_source_archive = b["archive"]
            _safe(target / b["domain_dir"]).mkdir()
            started_domains.append(b)
            _write_tree(target, b["domain_files"], b["domain_skeleton_dirs"])
            _sync_root_files_for_bundle(target, b, created_root_paths)
            agents_entry = _sync_domain_agents_entry(target, b["domain_dir"], b["app_id"], cli_alias=b.get("cli_alias"))
            installed_domains.append(
                {
                    "app_id": b["app_id"],
                    "domain_dir": b["domain_dir"],
                    "archive": b["archive"],
                    "bundle_kind": "domain-with-track" if b["track_bundled"] else "domain-only",
                    "bundle_format": "sh" if is_shell_bundle(Path(b["archive"])) else "tar.gz",
                    "edition": b["dist_manifest"].get("edition"),
                    "include_references": b["dist_manifest"].get("include_references"),
                    "track_bundled": b["track_bundled"],
                    "separation": b["dist_manifest"].get("separation"),
                    "agents_entry": agents_entry,
                }
            )

        if run_tests and (installed_domains or created_track_dir is not None):
            test_results: Dict[str, Any] = _run_all_target_tests(
                target,
                [(item["domain_dir"], item["app_id"]) for item in installed_domains],
                py_bin,
                include_track=installed_track is not None,
                track_suite=track_suite,
            )
        else:
            test_results = {"track": None, "domains": []}

        track_ok = test_results["track"] is None or test_results["track"].get("passed") is True
        passed_domain_dirs = {d["domain"] for d in test_results["domains"] if d.get("tests_passed") is True}
        failed_domain_items = [
            item
            for item in installed_domains
            if (not track_ok) or (item["domain_dir"] not in passed_domain_dirs)
        ]

        if run_tests and (not track_ok or failed_domain_items):
            all_failed = len(failed_domain_items) == len(installed_domains)
            _rollback_install_artifacts(
                target,
                domains_to_remove=failed_domain_items,
                remove_all_new_root_and_track=(not track_ok) or all_failed,
                created_track_dir=created_track_dir,
                created_root_paths=created_root_paths,
                orig_agents_existed=orig_agents_existed,
                orig_agents_bytes=orig_agents_bytes,
                orig_agents_mode=orig_agents_mode,
            )
            rolled_back_domains = [item["domain_dir"] for item in failed_domain_items]
            rolled_set = set(rolled_back_domains)
            installed_domains = [item for item in installed_domains if item["domain_dir"] not in rolled_set]
            if (not track_ok or all_failed) and created_track_dir is not None:
                installed_track = None
                track_source_archive = None
    except Exception:
        _rollback_install_artifacts(
            target,
            domains_to_remove=started_domains,
            remove_all_new_root_and_track=True,
            created_track_dir=created_track_dir,
            created_root_paths=created_root_paths,
            orig_agents_existed=orig_agents_existed,
            orig_agents_bytes=orig_agents_bytes,
            orig_agents_mode=orig_agents_mode,
        )
        raise

    all_installed_passed = (
        (test_results["track"] is None or test_results["track"].get("passed") is True)
        and all(d.get("tests_passed") is True for d in test_results["domains"])
    )
    tests_ok = all_installed_passed if (run_tests and (accepted_bundles or created_track_dir is not None)) else True
    if not tests_ok:
        status = "installed_tests_failed"
    elif conflicts:
        status = "installed_with_conflicts"
    elif run_tests:
        status = "installed_and_verified"
    else:
        status = "installed_no_tests"

    return {
        "schema": "forge.dist-install-report/1",
        "ok": tests_ok,
        "status": status,
        "decision_required": bool(conflicts),
        "rolled_back": bool(rolled_back_domains),
        "rolled_back_domains": rolled_back_domains,
        "target": str(target),
        "environment": env_report["checks"],
        "isolated_venv": venv_info,
        "installed_track": (
            {
                "version": installed_track["version"],
                "protocol": installed_track.get("protocol"),
                "manifest_sha256": installed_track["manifest_sha256"],
                "source_archive": track_source_archive,
            }
            if installed_track
            else None
        ),
        "installed_domains": installed_domains,
        "conflicts": conflicts,
        "root_gateway": {
            "path": str(target / "gewu"),
            "present": (target / "gewu").is_file(),
            "sha256": _sha256((target / "gewu").read_bytes()) if (target / "gewu").is_file() else None,
        },
        "test_results": test_results,
    }


def _collect_archives(archive_args: List[Path], dist_dir: Optional[Path]) -> List[Path]:
    out = list(archive_args)
    if dist_dir is not None:
        dd = _canonical_path(dist_dir)
        _need(dd.is_dir(), "dist-dir is not a directory")
        discovered: Dict[Tuple[Path, str], Path] = {}
        for p in sorted(dd.rglob("*-dist.sh")):
            if p.is_file() and not p.is_symlink():
                discovered[(p.parent, p.name[:-3])] = p
        for p in sorted(dd.rglob("*-dist.tar.gz")):
            if p.is_file() and not p.is_symlink():
                discovered.setdefault((p.parent, p.name[:-7]), p)
        out.extend(sorted(discovered.values(), key=lambda p: str(p)))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="cmd", required=True)

    ce = subs.add_parser("check-env")
    ce.add_argument("--target", type=Path, default=None)
    ce.add_argument("--wheels-dir", type=Path, default=None)
    ce.add_argument("--require", action="append", default=[])

    ins = subs.add_parser("install")
    ins.add_argument("--archive", type=Path, action="append", default=[])
    ins.add_argument("--dist-dir", type=Path, default=None)
    ins.add_argument("--target", type=Path, required=True)
    ins.add_argument("--wheels-dir", type=Path, default=None)
    ins.add_argument("--require", action="append", default=[])
    ins.add_argument("--init-git", action="store_true")
    ins.add_argument("--skip-tests", action="store_true")
    ins.add_argument("--track-suite", choices=("manifest", "quick", "full"), default="quick")

    args = parser.parse_args()
    try:
        if args.cmd == "check-env":
            out = check_environment(target_dir=args.target, wheels_dir=args.wheels_dir, extra_requires=args.require)
        else:
            archives = _collect_archives(args.archive, args.dist_dir)
            out = install_bundles(
                archives=archives,
                target_dir=args.target,
                wheels_dir=args.wheels_dir,
                extra_requires=args.require,
                init_git=args.init_git,
                run_tests=not args.skip_tests,
                track_suite=args.track_suite,
            )
        print(json.dumps(out, ensure_ascii=False))
        return 0 if out.get("ok", True) else 2
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "effect": "failed", "code": "E_INSTALL_DIST_FAILED", "reason": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
