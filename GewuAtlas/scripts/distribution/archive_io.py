"""Deterministic, bounded, single-pass tar.gz & self-extracting .sh archive I/O for Forge and Apps."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import re
import stat
import tarfile
import tempfile
import unicodedata
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple, Union

CHUNK_SIZE = 1024 * 1024
SH_FORMAT_ID = "gewu.dist-shell/1"
SH_MARKER = b"\n__GEWU_DIST_TAR_GZ_BASE64__\n"
SH_HEADER_SCAN_BYTES = 16384


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def encoded_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _safe_existing_path(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.exists() or candidate.is_symlink():
            if candidate.is_symlink():
                raise ValueError(f"linked path refused: {candidate}")


def _signature(info: os.stat_result) -> Tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _copy_and_digest(source: Any, destination: Any) -> Tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = source.read(CHUNK_SIZE)
        if not chunk:
            break
        destination.write(chunk)
        digest.update(chunk)
        size += len(chunk)
    return size, "sha256:" + digest.hexdigest()


def _normalized_name(name: str) -> Tuple[str, str]:
    raw = name[:-1] if name.endswith("/") and not name.endswith("//") else name
    if not raw or "\\" in raw or ":" in raw or "\x00" in raw:
        raise ValueError(f"unsafe archive member name: {name!r}")
    pure = PurePosixPath(raw)
    if raw != pure.as_posix() or pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise ValueError(f"unsafe archive member name: {name!r}")
    normalized = unicodedata.normalize("NFC", pure.as_posix())
    return normalized, normalized.casefold()


@dataclass(frozen=True)
class FileSource:
    path: Path
    mode: int
    size: int
    sha256: str
    _signature: Tuple[int, int, int, int]

    @classmethod
    def from_path(cls, path: Union[str, Path], mode: Optional[int] = None) -> "FileSource":
        source = Path(path)
        _safe_existing_path(source)
        info = source.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"archive source must be a regular single-link file: {source}")
        signature = _signature(info)
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            while True:
                chunk = handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
        if _signature(source.stat()) != signature:
            raise ValueError(f"archive source changed while hashing: {source}")
        return cls(source, (stat.S_IMODE(info.st_mode) & 0o7777) if mode is None else mode, info.st_size, "sha256:" + digest.hexdigest(), signature)

    def open(self) -> Any:
        self.verify_unchanged()
        handle = self.path.open("rb")
        if _signature(os.fstat(handle.fileno())) != self._signature:
            handle.close()
            raise ValueError(f"archive source changed before reading: {self.path}")
        return handle

    def verify_unchanged(self) -> None:
        if _signature(self.path.stat()) != self._signature:
            raise ValueError(f"archive source changed before reading: {self.path}")

    def read_bytes(self) -> bytes:
        with self.open() as handle:
            body = handle.read()
        if len(body) != self.size or sha256_bytes(body) != self.sha256 or _signature(self.path.stat()) != self._signature:
            raise ValueError(f"archive source changed while reading: {self.path}")
        return body


class _DigestReader:
    def __init__(self, source: Any):
        self.source = source
        self.digest = hashlib.sha256()
        self.size = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self.source.read(size)
        if chunk:
            self.digest.update(chunk)
            self.size += len(chunk)
        return chunk


SourceValue = Union[FileSource, Tuple[bytes, int]]


def _source_details(value: SourceValue) -> Tuple[int, int, str]:
    if isinstance(value, FileSource):
        return value.size, value.mode, value.sha256
    body, mode = value
    if not isinstance(body, bytes) or not isinstance(mode, int):
        raise ValueError("archive bytes sources must be (bytes, mode) tuples")
    return len(body), mode, sha256_bytes(body)


def source_row(name: str, value: SourceValue) -> Dict[str, Any]:
    size, mode, digest = _source_details(value)
    return {"path": name, "sha256": digest, "size": size, "mode": mode}


def write_archive(
    destination: Union[str, Path],
    sources: Mapping[str, SourceValue],
    directories: Iterable[str],
    manifest_name: str,
    manifest: Mapping[str, Any],
    *,
    directory_mode: Union[int, Callable[[str], int]] = 0o700,
    manifest_mode: int = 0o600,
    archive_mode: int = 0o600,
    compressed: bool = True,
) -> Path:
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    _safe_existing_path(target.parent)
    entries: Dict[str, SourceValue] = dict(sources)
    if manifest_name in entries:
        raise ValueError("manifest collides with an archive payload member")
    entries[manifest_name] = (encoded_json(manifest), manifest_mode)
    seen = set()
    normalized_dirs = []
    for raw_name in directories:
        name, key = _normalized_name(raw_name)
        if key in seen:
            raise ValueError(f"duplicate archive member: {name}")
        seen.add(key)
        normalized_dirs.append(name)
    for raw_name in entries:
        _, key = _normalized_name(raw_name)
        if key in seen:
            raise ValueError(f"duplicate archive member: {raw_name}")
        seen.add(key)

    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".archive-", suffix=".tar.gz" if compressed else ".tar")
    try:
        with os.fdopen(fd, "wb") as raw:
            with ExitStack() as stack:
                stream = stack.enter_context(gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0)) if compressed else raw
                with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as tar:
                    for name in sorted(normalized_dirs):
                        info = tarfile.TarInfo(name)
                        info.type = tarfile.DIRTYPE
                        info.mode = directory_mode(name) if callable(directory_mode) else directory_mode
                        info.mtime, info.uid, info.gid = 0, 0, 0
                        info.uname = info.gname = ""
                        tar.addfile(info)
                    for raw_name in sorted(entries):
                        name, _ = _normalized_name(raw_name)
                        value = entries[raw_name]
                        size, mode, expected_digest = _source_details(value)
                        info = tarfile.TarInfo(name)
                        info.size, info.mode, info.mtime, info.uid, info.gid = size, mode, 0, 0, 0
                        info.uname = info.gname = ""
                        if isinstance(value, FileSource):
                            with value.open() as source:
                                reader = _DigestReader(source)
                                tar.addfile(info, reader)
                                actual_digest = "sha256:" + reader.digest.hexdigest()
                                if reader.size != size or actual_digest != expected_digest or _signature(value.path.stat()) != value._signature:
                                    raise ValueError(f"archive source changed while writing: {value.path}")
                        else:
                            tar.addfile(info, io.BytesIO(value[0]))
            raw.flush()
            os.fsync(raw.fileno())
        os.chmod(temporary, archive_mode)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


@dataclass(frozen=True)
class ArchiveMember:
    path: Path
    mode: int
    size: int
    sha256: str

    def open(self) -> Any:
        return self.path.open("rb")

    def read_bytes(self) -> bytes:
        with self.open() as handle:
            body = handle.read()
        if len(body) != self.size or sha256_bytes(body) != self.sha256:
            raise ValueError(f"spooled archive member changed: {self.path}")
        return body

    def copy_to(self, destination: Union[str, Path], *, overwrite: bool = False) -> None:
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".archive-member-")
        try:
            with os.fdopen(fd, "wb") as output, self.open() as source:
                size, digest = _copy_and_digest(source, output)
                if size != self.size or digest != self.sha256:
                    raise ValueError(f"spooled archive member changed: {self.path}")
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, self.mode & 0o777)
            if overwrite:
                os.replace(temporary, target)
            else:
                os.link(temporary, target)
                os.unlink(temporary)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class ArchiveView:
    def __init__(self, files: Dict[str, ArchiveMember], directories: Iterable[str], archive_sha256: str, temporary: Any):
        self.files = files
        self.directories = sorted(directories)
        self.archive_sha256 = archive_sha256
        self._temporary = temporary

    def close(self) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    def __enter__(self) -> "ArchiveView":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def _parse_shell_metadata(header_text: str) -> Dict[str, str]:
    meta: Dict[str, str] = {}
    for line in header_text.splitlines():
        m = re.fullmatch(r"#\s*(GEWU_[A-Z0-9_]+)=([^\r\n]*)", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if key in meta:
                raise ValueError(f"duplicate shell bundle header: {key}")
            meta[key] = val
    return meta


def is_shell_bundle(source: Union[str, Path]) -> bool:
    """Return True if *source* starts with a valid Gewu self-extracting .sh header."""
    archive = Path(source)
    if not archive.is_file() or archive.is_symlink():
        return False
    with archive.open("rb") as fh:
        head = fh.read(SH_HEADER_SCAN_BYTES)
    return head.startswith(b"#!/usr/bin/env bash\n") and f"# GEWU_DIST_FORMAT={SH_FORMAT_ID}\n".encode("utf-8") in head


def parse_shell_bundle_metadata(source: Union[str, Path]) -> Dict[str, str]:
    """Read and validate declared `# GEWU_*` headers from a Gewu .sh bundle."""
    archive = Path(source)
    _safe_existing_path(archive)
    with archive.open("rb") as fh:
        head = fh.read(SH_HEADER_SCAN_BYTES)
    if not head.startswith(b"#!/usr/bin/env bash\n"):
        raise ValueError("invalid shell bundle shebang")
    idx = head.find(SH_MARKER)
    header_bytes = head[:idx] if idx != -1 else head
    meta = _parse_shell_metadata(header_bytes.decode("utf-8"))
    if meta.get("GEWU_DIST_FORMAT") != SH_FORMAT_ID:
        raise ValueError("unsupported shell bundle format")
    return meta


def extract_shell_archive_bytes(
    source: Union[str, Path],
    *,
    max_archive_bytes: int = 128 * 1024 * 1024 * 2,
) -> Tuple[bytes, Dict[str, str]]:
    """Decode and SHA-256 verify the embedded tar.gz payload from a Gewu .sh bundle."""
    archive = Path(source)
    _safe_existing_path(archive)
    info = archive.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("shell bundle must be a regular single-link file")
    max_shell_bytes = max_archive_bytes * 2 + 65536
    if info.st_size > max_shell_bytes:
        raise ValueError("shell bundle exceeds size limit")
    raw = archive.read_bytes()
    if _signature(archive.stat()) != _signature(info):
        raise ValueError("shell bundle changed while reading")
    parts = raw.split(SH_MARKER)
    if len(parts) != 2:
        raise ValueError("shell bundle marker missing or duplicated")
    header_bytes, payload_b64 = parts
    meta = _parse_shell_metadata(header_bytes.decode("utf-8"))
    if meta.get("GEWU_DIST_FORMAT") != SH_FORMAT_ID:
        raise ValueError("unsupported shell bundle format")
    expected_sha = meta.get("GEWU_PAYLOAD_SHA256", "")
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha):
        raise ValueError("invalid shell bundle payload sha256 header")
    compact = payload_b64.replace(b"\n", b"").replace(b"\r", b"")
    try:
        archive_bytes = base64.b64decode(compact, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"corrupt base64 payload in shell bundle: {exc}") from exc
    if len(archive_bytes) > max_archive_bytes:
        raise ValueError("decoded shell archive exceeds compressed size limit")
    actual_sha = hashlib.sha256(archive_bytes).hexdigest()
    if actual_sha != expected_sha:
        raise ValueError(f"shell bundle payload sha256 mismatch: expected {expected_sha}, got {actual_sha}")
    return archive_bytes, meta


_SHELL_RUNNER_BODY = """\
set -eu
PYTHON_BIN="${GEWU_PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo '{"ok":false,"code":"E_PYTHON_REQUIRED","reason":"python3 >= 3.9 is required to inspect or install this Gewu bundle"}' >&2
  exit 2
fi
SELF_PATH="$0"
exec "$PYTHON_BIN" -I -B - "$SELF_PATH" "$@" <<'__GEWU_SH_RUNNER__'
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

MARKER = ("\\n__GEWU_DIST_" + "TAR_GZ_BASE64__\\n").encode("utf-8")


def _fail(code: str, reason: str, rc: int = 2) -> int:
    print(json.dumps({"ok": False, "code": code, "reason": reason}, ensure_ascii=False))
    return rc


def _load_self_payload(self_path: Path):
    raw = self_path.read_bytes()
    parts = raw.split(MARKER)
    if len(parts) != 2:
        raise ValueError("shell bundle marker missing or duplicated")
    header_text = parts[0].decode("utf-8")
    meta = {}
    for line in header_text.splitlines():
        m = re.fullmatch(r"#\\s*(GEWU_[A-Z0-9_]+)=([^\\r\\n]*)", line)
        if m:
            meta[m.group(1)] = m.group(2).strip()
    if meta.get("GEWU_DIST_FORMAT") != "gewu.dist-shell/1":
        raise ValueError("unsupported shell bundle format")
    expected_sha = meta.get("GEWU_PAYLOAD_SHA256", "")
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha):
        raise ValueError("missing GEWU_PAYLOAD_SHA256 header")
    compact = parts[1].replace(b"\\n", b"").replace(b"\\r", b"")
    archive_bytes = base64.b64decode(compact, validate=True)
    actual_sha = hashlib.sha256(archive_bytes).hexdigest()
    if actual_sha != expected_sha:
        raise ValueError(f"payload sha256 mismatch: expected {expected_sha}, got {actual_sha}")
    return archive_bytes, meta


def _safe_tar_member_bytes(tar: tarfile.TarFile, target_name: str) -> bytes:
    member = tar.getmember(target_name)
    if not member.isfile():
        raise ValueError(f"installer member is not a regular file: {target_name}")
    extracted = tar.extractfile(member)
    if extracted is None:
        raise ValueError(f"unable to extract member: {target_name}")
    return extracted.read()


def main() -> int:
    if len(sys.argv) < 2:
        return _fail("E_SELF_ARG_MISSING", "missing bundle script path")
    self_path = Path(sys.argv[1]).resolve()
    parser = argparse.ArgumentParser(prog=Path(sys.argv[1]).name, description="Gewu self-extracting distribution installer")
    parser.add_argument("--verify", action="store_true", help="Verify embedded payload SHA-256 and print DIST_MANIFEST summary")
    parser.add_argument("--inspect", action="store_true", help="Alias for --verify")
    parser.add_argument("--extract-archive", type=Path, default=None, help="Extract verified inner .tar.gz archive to path")
    parser.add_argument("--target", type=Path, default=None, help="Install bundle cleanly into target workspace directory")
    parser.add_argument("--wheels-dir", type=Path, default=None, help="Offline Python wheelhouse directory passed to install_dist")
    parser.add_argument("--init-git", action="store_true", help="Initialize Git workspace inside target before domain installation")
    parser.add_argument("--skip-tests", action="store_true", help="Skip post-install verification tests")
    parser.add_argument("--track-suite", choices=("manifest", "quick", "full"), default="quick")
    parser.add_argument("--require", action="append", default=[], help="Extra Python dependency passed to install_dist")
    args = parser.parse_args(sys.argv[2:])

    try:
        archive_bytes, meta = _load_self_payload(self_path)
    except Exception as exc:  # noqa: BLE001
        return _fail("E_BUNDLE_CORRUPT", str(exc))

    if args.extract_archive is not None:
        out_path = args.extract_archive.expanduser()
        if not out_path.is_absolute():
            out_path = (Path.cwd() / out_path).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=out_path.parent, prefix=".extract-", suffix=".tar.gz")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(archive_bytes)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp_name, 0o644)
            os.replace(tmp_name, out_path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        print(json.dumps({
            "ok": True,
            "extracted_archive": str(out_path),
            "sha256": meta["GEWU_PAYLOAD_SHA256"],
            "app_id": meta.get("GEWU_APP_ID"),
            "bundle_kind": meta.get("GEWU_BUNDLE_KIND"),
            "edition": meta.get("GEWU_EDITION"),
        }, ensure_ascii=False))
        return 0

    if args.target is None or args.verify or args.inspect:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
            dist_manifest = json.loads(_safe_tar_member_bytes(tar, "DIST_MANIFEST.json").decode("utf-8"))
        summary = dict(dist_manifest)
        summary.pop("files", None)
        print(json.dumps({
            "ok": True,
            "verified": True,
            "bundle_format": "sh",
            "bundle_path": str(self_path),
            "payload_tar_gz_sha256": meta["GEWU_PAYLOAD_SHA256"],
            "header": meta,
            "manifest": summary,
        }, ensure_ascii=False))
        return 0

    installer_rel = meta.get("GEWU_INSTALLER_ARCHIVE_PATH", "")
    archive_io_rel = meta.get("GEWU_ARCHIVE_IO_ARCHIVE_PATH", "")
    if not installer_rel or not archive_io_rel:
        return _fail("E_INSTALLER_MISSING", "missing embedded installer coordinates in bundle header")

    with tempfile.TemporaryDirectory(prefix=".gewu-sh-inst-") as td:
        os.chmod(td, 0o700)
        td_path = Path(td)
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
            inst_bytes = _safe_tar_member_bytes(tar, installer_rel)
            aio_bytes = _safe_tar_member_bytes(tar, archive_io_rel)
        (td_path / "archive_io.py").write_bytes(aio_bytes)
        inst_py = td_path / "install_dist.py"
        inst_py.write_bytes(inst_bytes)
        cmd = [
            sys.executable,
            "-I",
            "-B",
            str(inst_py),
            "install",
            "--archive",
            str(self_path),
            "--target",
            str(args.target),
            "--track-suite",
            args.track_suite,
        ]
        if args.wheels_dir is not None:
            cmd.extend(["--wheels-dir", str(args.wheels_dir)])
        if args.init_git:
            cmd.append("--init-git")
        if args.skip_tests:
            cmd.append("--skip-tests")
        for req in args.require:
            cmd.extend(["--require", req])
        proc = subprocess.run(cmd, cwd=str(Path.cwd()), check=False)
        return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
__GEWU_SH_RUNNER__
"""


def write_shell_bundle(
    destination: Union[str, Path],
    archive_bytes: bytes,
    *,
    app_id: str,
    bundle_kind: str,
    edition: str,
    include_references: bool,
    installer_archive_path: str,
    archive_io_archive_path: str,
    mode: int = 0o755,
) -> Tuple[Path, str]:
    """Write a deterministic executable self-extracting .sh bundle around *archive_bytes*."""
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    _safe_existing_path(target.parent)
    for k, v in (
        ("app_id", app_id),
        ("bundle_kind", bundle_kind),
        ("edition", edition),
        ("installer_archive_path", installer_archive_path),
        ("archive_io_archive_path", archive_io_archive_path),
    ):
        if not v or "\n" in v or "\r" in v:
            raise ValueError(f"invalid shell bundle header field {k}")
    payload_sha256 = hashlib.sha256(archive_bytes).hexdigest()
    header = (
        "#!/usr/bin/env bash\n"
        f"# GEWU_DIST_FORMAT={SH_FORMAT_ID}\n"
        f"# GEWU_APP_ID={app_id}\n"
        f"# GEWU_BUNDLE_KIND={bundle_kind}\n"
        f"# GEWU_EDITION={edition}\n"
        f"# GEWU_INCLUDE_REFERENCES={'true' if include_references else 'false'}\n"
        f"# GEWU_PAYLOAD_SHA256={payload_sha256}\n"
        f"# GEWU_INSTALLER_ARCHIVE_PATH={installer_archive_path}\n"
        f"# GEWU_ARCHIVE_IO_ARCHIVE_PATH={archive_io_archive_path}\n"
    ).encode("utf-8")
    runner = _SHELL_RUNNER_BODY.encode("utf-8")
    encoded_b64 = base64.encodebytes(archive_bytes)
    full_bytes = header + runner + SH_MARKER + encoded_b64
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".sh-bundle-", suffix=".sh")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(full_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target, hashlib.sha256(full_bytes).hexdigest()


def read_archive(
    source: Union[str, Path],
    *,
    max_archive_bytes: int,
    max_uncompressed_bytes: int,
    max_members: int,
    file_modes: Iterable[int],
    directory_modes: Iterable[int] = (0o700,),
    compressed: bool = True,
) -> ArchiveView:
    archive = Path(source)
    _safe_existing_path(archive)
    info = archive.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("archive must be a regular single-link file")
    shell_mode = False
    with archive.open("rb") as probe:
        head = probe.read(SH_HEADER_SCAN_BYTES)
    if head.startswith(b"#!/usr/bin/env bash\n") and f"# GEWU_DIST_FORMAT={SH_FORMAT_ID}\n".encode("utf-8") in head:
        if not compressed:
            raise ValueError("shell bundle requires compressed archive mode")
        shell_mode = True
    elif info.st_size > max_archive_bytes:
        raise ValueError("archive exceeds compressed size limit")
    allowed_file_modes = set(file_modes)
    allowed_directory_modes = set(directory_modes)
    temporary = tempfile.TemporaryDirectory(prefix=".forge-archive-")
    spool = Path(temporary.name)
    files: Dict[str, ArchiveMember] = {}
    directories = []
    seen: Dict[str, str] = {}
    file_keys = set()
    total_size = 0
    try:
        if shell_mode:
            payload_bytes, _ = extract_shell_archive_bytes(archive, max_archive_bytes=max_archive_bytes)
            raw_ctx = io.BytesIO(payload_bytes)
        else:
            raw_ctx = archive.open("rb")
        with raw_ctx as raw:
            digest_reader = _DigestReader(raw)
            with tarfile.open(fileobj=digest_reader, mode="r|gz" if compressed else "r|") as tar:
                for index, member in enumerate(tar):
                    if index >= max_members:
                        raise ValueError("archive member limit exceeded")
                    if member.pax_headers:
                        raise ValueError(f"archive PAX headers refused: {member.name}")
                    name, key = _normalized_name(member.name)
                    if key in seen:
                        raise ValueError(f"archive path collision: {name} vs {seen[key]}")
                    for parent in PurePosixPath(key).parents:
                        if parent.as_posix() in file_keys:
                            raise ValueError(f"archive file/directory conflict: {name}")
                    seen[key] = name
                    if member.isdir():
                        if member.mode not in allowed_directory_modes:
                            raise ValueError(f"archive dir mode refused: {member.name}")
                        if key in file_keys:
                            raise ValueError(f"archive file/directory conflict: {name}")
                        directories.append(name)
                        continue
                    if not member.isfile():
                        raise ValueError(f"archive special member refused: {member.name}")
                    if member.mode not in allowed_file_modes or member.size < 0:
                        raise ValueError(f"archive file mode/size refused: {member.name}")
                    if any(existing.startswith(key + "/") for existing in seen if existing != key):
                        raise ValueError(f"archive file/directory conflict: {name}")
                    total_size += member.size
                    if total_size > max_uncompressed_bytes:
                        raise ValueError("archive exceeds uncompressed size limit")
                    extracted = tar.extractfile(member)
                    if extracted is None:
                        raise ValueError(f"archive member unreadable: {member.name}")
                    destination = spool / f"payload-{index}"
                    with extracted, destination.open("xb") as output:
                        actual_size, digest = _copy_and_digest(extracted, output)
                    if actual_size != member.size:
                        raise ValueError(f"archive member size mismatch: {member.name}")
                    files[name] = ArchiveMember(destination, member.mode, actual_size, digest)
                    file_keys.add(key)
            while digest_reader.read(CHUNK_SIZE):
                pass
        if _signature(archive.stat()) != _signature(info):
            raise ValueError("archive changed while reading")
        return ArchiveView(files, directories, "sha256:" + digest_reader.digest.hexdigest(), temporary)
    except Exception:
        temporary.cleanup()
        raise
