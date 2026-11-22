#!/usr/bin/env python3
"""KEK-blinded multimodal format conversion (`fmt`) & URL ingest engine for GewuAtlas."""

from __future__ import annotations

from http.client import HTTPException
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
from contextvars import ContextVar
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from facade.atlas_domain_scope import (
    read_domain_manifest,
    resolve_archive_dir,
    resolve_domain_scope,
    scan_project_rels,
)
from notes.atlas_note_engine import (
    _append_journal_event,
    _public_note,
    _scan_vault_markdowns,
    _slugify_filename,
)
from notes.atlas_yamlmini import (
    allowed_fields_for_doc_type,
    extract_frontmatter,
    fm_scalar,
    prune_frontmatter_for_doc_type,
    replace_frontmatter_field,
)
from storage.note_kek_store import KekNoteRegistry
from storage.project_kek_store import KekProjectRegistry
from collab.ticket_bridge import ensure_doc_ticket, update_doc_ticket_progress
from storage.vault_kek_store import VaultStoreError

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
APP_DIR = SCRIPTS_DIR.parent
CONVERTERS_DIR = SCRIPTS_DIR / "converters"
REQUIREMENTS_PATH = APP_DIR / "contracts" / "fmt-dependencies.json"

if str(CONVERTERS_DIR) not in sys.path:
    sys.path.insert(0, str(CONVERTERS_DIR))

from archive_month import month_tag, monthly_archive_dir  # noqa: E402
from url_ingest_helper import check_url as _check_url, download_url, parse_url_request  # noqa: E402


def check_url(vault_dir: Path, url: str, *, resolve_dns: bool = True) -> None:
    try:
        _check_url(vault_dir, url, resolve_dns=resolve_dns)
    except (OSError, HTTPException) as exc:
        raise VaultStoreError("E_URL_NETWORK", "URL 域名解析或网络核验不可用；保留来源并检查网络环境", exit_code=1) from exc
    except ValueError as exc:
        raise VaultStoreError("E_URL_POLICY", "URL 格式或安全核验未通过；保留来源，不下载", exit_code=1) from exc


def _download_url_safe(
    vault_dir: Path,
    url: str,
    stage: Path,
    *,
    library: Path,
    ws_root: Path,
) -> Path:
    try:
        return download_url(vault_dir, url, stage, library=library)
    except urllib.error.HTTPError as exc:
        raise VaultStoreError(
            "E_URL_NETWORK",
            f"URL 请求失败（HTTP {exc.code}）；保留来源并跳过该链接，可继续处理其他有效 URL",
            exit_code=1,
        ) from exc
    except (OSError, HTTPException, urllib.error.URLError) as exc:
        raise VaultStoreError(
            "E_URL_NETWORK",
            "URL 域名解析或网络抓取不可用（连接超时/TLS/网络中断）；保留来源并跳过该链接，可继续处理其他有效 URL",
            exit_code=1,
        ) from exc
    except ValueError as exc:
        detail = _scrub_paths(str(exc), ws_root, vault_dir) or "URL 格式、策略或内容类型暂不支持"
        raise VaultStoreError(
            "E_URL_POLICY",
            f"{detail}；保留来源，不下载",
            exit_code=1,
        ) from exc


IMAGE_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".bmp",
    ".tiff",
    ".tif",
    ".drawio",
    ".excalidraw",
}
NOTE_EXTS = {".md", ".markdown", ".txt", ".rtf"}
LEGACY_OLE_EXTS = {".doc", ".ppt"}
MULTIMODAL_SWEEP_EXTS = (
    IMAGE_EXTS
    | LEGACY_OLE_EXTS
    | {".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".csv", ".html", ".htm", ".url", ".urls", ".txt", ".rtf"}
)

REQUIRED_MODULES = (
    "fitz",
    "docx",
    "pptx",
    "openpyxl",
    "xlrd",
    "xlsxwriter",
    "bs4",
    "readability",
    "html2text",
    "lxml",
    "PIL",
    "chardet",
    "requests",
    "yaml",
    "striprtf",
)


def _scrub_paths(text: str, ws_root: Path, vault_dir: Path) -> str:
    s = str(text or "")
    for secret in (str(vault_dir.resolve()), str(vault_dir), str(ws_root.resolve()), str(ws_root)):
        if secret:
            s = s.replace(secret, "<vault>")
    s = re.sub(r"[^\s'\"`]*GewuAtlas-data[^\s'\"`]*", "<vault>", s)
    s = re.sub(r"Volumes[/\\][^\s'\"`]*", "<vault>", s)
    s = s.replace(".trash", "<archive-trash>")
    return s.strip()


def _probe_python_deps(py_bin: Path) -> bool:
    code = "import " + ", ".join(REQUIRED_MODULES)
    res = subprocess.run(
        [str(py_bin), "-I", "-B", "-c", code],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return res.returncode == 0


def _resolve_fmt_python() -> Path:
    candidates: List[Path] = []
    env_override = os.environ.get("GEWU_ATLAS_FMT_PYTHON", "").strip()
    if env_override:
        candidates.append(Path(env_override).expanduser())
    candidates.extend(
        [
            APP_DIR.parent / ".runtime" / "install-env" / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"),
            APP_DIR / ".runtime" / "fmt-venv" / "Scripts" / "python.exe",
            APP_DIR / ".runtime" / "fmt-venv" / "bin" / "python3",
            APP_DIR / ".runtime" / "fmt-venv" / "bin" / "python",
            APP_DIR / ".local" / "fmt-venv" / "bin" / "python3",
            Path(sys.executable),
        ]
    )
    seen = set()
    for cand in candidates:
        key = str(cand)
        if key in seen:
            continue
        seen.add(key)
        try:
            if cand.is_file() and _probe_python_deps(cand):
                return cand
        except OSError:
            continue
    raise VaultStoreError(
        "E_RUNTIME_DEPENDENCY",
        "未检测到完整的 fmt 第三方依赖环境；请先执行 `./gewu atlas --action fmt --setup` 安装 `.runtime/fmt-venv`",
        exit_code=2,
    )


def setup_fmt_environment(*, force: bool = False) -> Dict[str, Any]:
    venv_dir = APP_DIR / ".runtime" / "fmt-venv"
    py_bin = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python3")
    if not force:
        try:
            _resolve_fmt_python()
            return {"setup": "already-ready", "packages_verified": len(REQUIRED_MODULES)}
        except VaultStoreError:
            pass
    if py_bin.is_file() and not force and _probe_python_deps(py_bin):
        return {"setup": "already-ready", "packages_verified": len(REQUIRED_MODULES)}
    if not REQUIREMENTS_PATH.is_file():
        raise VaultStoreError("E_NOT_FOUND", "缺少 contracts/fmt-dependencies.json 运行依赖清单", exit_code=1)
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    if force and venv_dir.exists():
        shutil.rmtree(venv_dir)
    if not py_bin.is_file():
        rc = subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], capture_output=True, text=True, check=False)
        if rc.returncode != 0:
            raise VaultStoreError("E_EXEC_FAILED", "创建 fmt 虚拟环境失败；请检查本机 Python venv 支持", exit_code=1)
    pip_rc = subprocess.run(
        [str(py_bin), "-I", "-m", "pip", "install", "--index-url", "https://pypi.tuna.tsinghua.edu.cn/simple", *json.loads(REQUIREMENTS_PATH.read_text(encoding="utf-8"))["packages"]],
        capture_output=True,
        text=True,
        check=False,
    )
    if pip_rc.returncode != 0 or not _probe_python_deps(py_bin):
        raise VaultStoreError("E_EXEC_FAILED", "安装 fmt 第三方依赖失败；请检查网络、清华源和本机 Python 兼容性", exit_code=1)
    return {"setup": "installed", "packages_verified": len(REQUIRED_MODULES)}


def _select_converter(target: str) -> str:
    if target.lower().endswith((".drawio.svg", ".drawio.png", ".excalidraw.svg", ".excalidraw.png", ".excalidraw.md")):
        return "image_converter.py"
    if target.startswith(("http://", "https://")):
        return "web_converter.py"
    ext = os.path.splitext(target)[1].lower()
    if ext == ".pdf":
        return "pdf_converter.py"
    if ext in (".docx", ".doc"):
        return "docx_converter.py"
    if ext in (".pptx", ".ppt"):
        return "pptx_converter.py"
    if ext in (".xlsx", ".xls", ".csv"):
        return "excel_converter.py"
    if ext in (".html", ".htm"):
        return "html_converter.py"
    if ext in IMAGE_EXTS:
        return "image_converter.py"
    if ext in (".url", ".urls"):
        return "web_converter.py"
    if ext in NOTE_EXTS:
        return "note_refiner.py"
    return ""


def _source_type_for_target(target: str) -> str:
    if target.startswith(("http://", "https://")):
        return "web"
    ext = os.path.splitext(target)[1].lower()
    if ext == ".pdf":
        return "pdf"
    if ext in (".docx", ".doc"):
        return "docx"
    if ext in (".pptx", ".ppt"):
        return "pptx"
    if ext in (".xlsx", ".xls", ".csv"):
        return "excel"
    if ext in (".html", ".htm"):
        return "html"
    if ext in (".url", ".urls"):
        return "web"
    if ext in IMAGE_EXTS:
        return "image"
    return "document"


def _infer_dropzone_project_ref(
    vault_dir: Path,
    vault_ref: str,
    proj_reg: KekProjectRegistry,
    rel_path: str,
    explicit_project_ref: Optional[str],
    scope: str,
) -> Dict[str, Any]:
    if explicit_project_ref or (scope or "auto").strip().lower() != "auto":
        return resolve_domain_scope(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            proj_reg=proj_reg,
            explicit_project_ref=explicit_project_ref,
            scope=scope,
            source_rel_path=rel_path,
        )
    parts = Path(rel_path).parts
    if len(parts) >= 3 and parts[0] == "00-DropZone":
        hint = parts[1].strip().lower()
        existing_rels = scan_project_rels(vault_dir)
        rel_to_ref = proj_reg.sync_projects(vault_ref, existing_rels)
        for prel in existing_rels:
            pdir = vault_dir / prel
            meta = read_domain_manifest(pdir, Path(prel).name)
            candidates = {
                Path(prel).name.lower(),
                str(meta.get("project_id") or "").strip().lower(),
                str(meta.get("title") or "").strip().lower(),
            }
            if hint in candidates:
                return resolve_domain_scope(
                    vault_dir=vault_dir,
                    vault_ref=vault_ref,
                    proj_reg=proj_reg,
                    explicit_project_ref=rel_to_ref[prel],
                    scope="project",
                )
    return resolve_domain_scope(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        proj_reg=proj_reg,
        explicit_project_ref=None,
        scope=scope,
        source_rel_path=rel_path,
    )


def _converter_env(vault_dir: Path, domain: Dict[str, Any]) -> Dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "en_US.UTF-8"),
        "GEWU_ATLAS_VAULT_ROOT": str(vault_dir.resolve()),
        "GEWU_ATLAS_MANAGED_FMT": "1",
        "GEWU_ATLAS_DOMAIN_MODE": str(domain.get("default_mode") or "auto"),
        "PYTHONPATH": str(CONVERTERS_DIR),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return env


def _run_py_tool(
    fmt_py: Path,
    script_name: str,
    cmd_args: List[str],
    *,
    env: Dict[str, str],
    ws_root: Path,
    vault_dir: Path,
) -> subprocess.CompletedProcess:
    script_path = CONVERTERS_DIR / script_name
    if not script_path.is_file():
        raise VaultStoreError("E_NOT_FOUND", f"缺少转换算子: {script_name}", exit_code=1)
    proc = subprocess.run(
        [str(fmt_py), "-B", str(script_path), *cmd_args],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if proc.returncode != 0:
        detail = _scrub_paths(proc.stderr or proc.stdout or f"returncode={proc.returncode}", ws_root, vault_dir)
        raise VaultStoreError("E_FMT_FAILED", f"{script_name} 执行失败: {detail[:400]}", exit_code=1)
    return proc


_ALLOWED_FMT_ZONES: Dict[str, Tuple[str, str]] = {
    "library": ("20-Memory/21-Library", "library"),
    "workbench": ("30-Workbench", "workbench"),
    "dropzone": ("00-DropZone", "memo"),
    "atomics": ("20-Memory/22-Atomics", "atomic"),
    "moc": ("20-Memory/23-MOC", "moc"),
    "slices": ("20-Memory/24-Slices", "slice"),
}


def _resolve_fmt_target_dir(
    vault_dir: Path,
    domain: Dict[str, Any],
    target_zone: Optional[str] = None,
    subdir: Optional[str] = None,
) -> Tuple[Path, str, str]:
    """Resolve logical fmt/url-ingest destination without exposing disk paths to the caller."""
    z = (target_zone or "library").strip().lower()
    if z in ("workbench_source", "source"):
        z = "workbench"
        subdir = subdir or "source"
    if z not in _ALLOWED_FMT_ZONES:
        raise VaultStoreError(
            "E_PARAM_INVALID",
            "--target_zone 仅支持 library / workbench / dropzone / atomics / moc / slices",
            exit_code=1,
        )
    rel_comp, doc_type = _ALLOWED_FMT_ZONES[z]
    base_dir = (vault_dir / "00-DropZone") if z == "dropzone" else (Path(domain["domain_root"]) / rel_comp)
    clean_sub = (subdir or "").strip().strip("/")
    if clean_sub:
        if z in ("atomics", "moc"):
            raise VaultStoreError("E_VFS_FLAT_DIR", f"扁平分区 {z} 不允许写入子目录", exit_code=1)
        if "\\" in clean_sub or ".." in clean_sub.split("/") or any(p.startswith(".") or not p for p in clean_sub.split("/")):
            raise VaultStoreError("E_PARAM_INVALID", f"非法逻辑子目录: {subdir}", exit_code=1)
        base_dir = base_dir / clean_sub
    return base_dir, doc_type, z


def _is_pdf_file(path: Path) -> bool:
    with path.open("rb") as stream:
        return stream.read(1024).lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"%PDF-")


def _archive_to_monthly_dir(src: Path, archive_dir: Path) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".pdf" if _is_pdf_file(src) else src.suffix
    dst = archive_dir / (src.stem + suffix)
    idx = 1
    while dst.exists():
        try:
            if dst.read_bytes() == src.read_bytes():
                return dst
        except OSError:
            pass
        dst = archive_dir / f"{src.stem}_{idx}{suffix}"
        idx += 1
    shutil.copy2(src, dst)
    return dst


def _trash_dropzone_source(src: Path, vault_dir: Path) -> None:
    try:
        rel = src.resolve().relative_to(vault_dir.resolve())
    except ValueError:
        return
    if rel.parts and rel.parts[0] == "00-DropZone" and src.is_file():
        trash_dir = vault_dir / ".trash"
        trash_dir.mkdir(parents=True, exist_ok=True)
        dst = trash_dir / src.name
        n = 1
        while dst.exists():
            dst = trash_dir / f"{src.name}.{n}"
            n += 1
        shutil.move(str(src), str(dst))


def _hydrate_output_card(
    out_file: Path,
    *,
    by: Optional[str],
    domain: Dict[str, Any],
    source_url: Optional[str] = None,
    orig_name: Optional[str] = None,
    target_doc_type: Optional[str] = None,
) -> Tuple[str, str]:
    text = out_file.read_text(encoding="utf-8")
    meta, body, has_fm = extract_frontmatter(text)
    desired_dt = (target_doc_type or "library").strip().lower()
    allowed_fields = allowed_fields_for_doc_type(desired_dt)
    if not has_fm:
        title = out_file.stem
        status = "active" if domain["default_mode"] == "auto" else "pending"
        pid_line = f'project_id: "{fm_scalar(domain["project_id"])}"\n' if domain["scope"] == "project" else ""
        src_line = f'source: "{fm_scalar(source_url)}"\n' if source_url else ""
        extra_lines = ""
        if desired_dt == "library":
            extra_lines = (
                'source_type: document\n'
                'channel: "fmt"\n'
                f'original_file: "{fm_scalar(orig_name or "")}"\n'
                "processed: false\n"
            )
        fm = (
            "---\n"
            f"doc_type: {desired_dt}\n"
            f'title: "{fm_scalar(title)}"\n'
            'summary: "格式转译伴生卡"\n'
            f"status: {status}\n"
            "tags: [Library, 转译入库]\n"
            f"updated: {date.today().isoformat()}\n"
            f"{src_line}"
            f"{pid_line}"
            f"{extra_lines}"
            "---\n\n"
        )
        text = fm + body.lstrip("\n")
        out_file.write_text(text, encoding="utf-8")
        meta, _, _ = extract_frontmatter(text)
    final_dt = desired_dt if target_doc_type else str(meta.get("doc_type") or "library").strip().lower()
    final_allowed = allowed_fields_for_doc_type(final_dt) or allowed_fields
    preserved_pending = str(meta.get("status") or "") == "pending" and str(meta.get("doc_type") or "") in ("atomic", "moc")
    text = replace_frontmatter_field(text, "status", "pending" if domain["default_mode"] != "auto" or preserved_pending else "active")
    if target_doc_type and str(meta.get("doc_type") or "").strip().lower() != desired_dt:
        text = replace_frontmatter_field(text, "doc_type", desired_dt)
    orig_url = source_url or str(meta.get("original_url") or meta.get("source_url") or "").strip()
    if orig_url:
        if "source_url" in final_allowed:
            text = replace_frontmatter_field(text, "source_url", f'"{fm_scalar(orig_url)}"')
        if "source" in final_allowed:
            text = replace_frontmatter_field(text, "source", f'"{fm_scalar(orig_url)}"')
    if by:
        if not meta.get("created_by"):
            text = replace_frontmatter_field(text, "created_by", f'"{fm_scalar(by)}"')
        text = replace_frontmatter_field(text, "updated_by", f'"{fm_scalar(by)}"')
    if domain["scope"] == "project":
        text = replace_frontmatter_field(text, "project_id", f'"{fm_scalar(domain["project_id"])}"')
    text = prune_frontmatter_for_doc_type(text, final_dt)
    if orig_url and re.match(r"^https?://", orig_url, re.I):
        from urllib.parse import urljoin
        from notes.atlas_wikilink_tools import rewrite_markdown_prose

        def _absolutize_web_links(seg: str) -> str:
            def _sub(m: re.Match[str]) -> str:
                prefix, raw_val = m.group(1), m.group(2)
                if prefix.startswith("!"):
                    return m.group(0)
                wrapped = raw_val.startswith("<") and raw_val.endswith(">")
                val = raw_val[1:-1] if wrapped else raw_val
                v_str = val.strip()
                if not v_str or v_str.startswith("#") or v_str.startswith("//") or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", v_str):
                    return m.group(0)
                if v_str.startswith("99-Assets/"):
                    return m.group(0)
                abs_u = urljoin(orig_url, v_str)
                return f"{prefix}<{abs_u}>" if wrapped else f"{prefix}{abs_u}"

            return re.sub(r"(!?\[[^\]]*\]\(\s*)(<[^>]+>|[^)\s]+)", _sub, seg)

        text = rewrite_markdown_prose(text, _absolutize_web_links, include_frontmatter=False)
    out_file.write_text(text, encoding="utf-8")
    meta2, _, _ = extract_frontmatter(text)
    return str(meta2.get("title") or out_file.stem), str(meta2.get("status") or "active")


def _normalize_md_inprocess(out_file: Path, *, check_only: bool = False, no_symbols: bool = False) -> int:
    import contextlib
    import io

    conv_str = str(CONVERTERS_DIR)
    if conv_str not in sys.path:
        sys.path.insert(0, conv_str)
    import md_normalizer  # type: ignore

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return int(md_normalizer.normalize(out_file, check_only=check_only, symbols=not no_symbols))


def _extract_deep_chunks(fmt_py: Path, out_file: Path, *, env: Dict[str, str], ws_root: Path, vault_dir: Path) -> Optional[Dict[str, Any]]:
    proc = _run_py_tool(
        fmt_py,
        "chunk_window.py",
        ["split", str(out_file), "--json"],
        env=env,
        ws_root=ws_root,
        vault_dir=vault_dir,
    )
    try:
        chunks = json.loads(proc.stdout)
        if isinstance(chunks, list):
            return {
                "chunk_count": len(chunks),
                "sections": [c.get("section", "") for c in chunks[:20]],
                "table_chunks": sum(1 for c in chunks if c.get("has_table")),
            }
    except Exception:
        pass
    return None


def _unique_library_target(lib_dir: Path, stem: str) -> Path:
    cand = lib_dir / f"{stem}.md"
    idx = 1
    while cand.exists():
        cand = lib_dir / f"{stem}_{idx}.md"
        idx += 1
    return cand


def _snapshot_attachments(att_dir: Path) -> set[str]:
    if not att_dir.is_dir():
        return set()
    return {p.name for p in att_dir.iterdir() if p.is_file() and not p.is_symlink()}


def _format_arguments(converter: str, options: Dict[str, Any]) -> List[str]:
    args: List[str] = []
    if converter == "pdf_converter.py":
        mode = str(options.get("pdf_mode") or "auto")
        if mode not in ("auto", "paper", "ppt"):
            raise VaultStoreError("E_PARAM_INVALID", "pdf_mode 仅支持 auto/paper/ppt", exit_code=1)
        args += ["--mode", mode]
        if options.get("dpi") is not None:
            args += ["--dpi", str(options["dpi"])]
        if options.get("skip_decorative"):
            args += ["--skip-decorative"]
        for slot, flag in [("decorative_size", "--decorative-size"), ("decorative_dim", "--decorative-dim")]:
            if options.get(slot) is not None: args += [flag, str(options[slot])]
    if converter == "excel_converter.py" and options.get("threshold") is not None:
        args += ["--threshold", str(options["threshold"])]
    if converter == "excel_converter.py" and options.get("dump_json"):
        args += ["--dump-json"]
    if converter == "image_converter.py" and options.get("dump_payload"):
        args += ["--dump-payload"]
    if converter == "note_refiner.py" and options.get("encoding"):
        args += ["--encoding", str(options["encoding"])]
    return args


def _convert_single_local_file(
    *,
    fmt_py: Path,
    src_file: Path,
    vault_dir: Path,
    ws_root: Path,
    domain: Dict[str, Any],
    by: Optional[str],
    dry_run: bool,
    deep: bool,
    annotate_images: bool,
    source_url: Optional[str] = None,
    target_zone: Optional[str] = None,
    subdir: Optional[str] = None,
    isolated_worker: bool = False,
    fmt_options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    fmt_options = fmt_options or {}
    ext = src_file.suffix.lower()
    if ext in LEGACY_OLE_EXTS:
        raise VaultStoreError(
            "E_UNSUPPORTED_FORMAT",
            f"不支持旧版二进制 OLE 格式 ({ext}): {src_file.name}；请先另存为 {ext}x 或 PDF",
            exit_code=1,
        )
    converter = "pdf_converter.py" if _is_pdf_file(src_file) else _select_converter(src_file.name)
    if not converter:
        raise VaultStoreError("E_UNSUPPORTED_FORMAT", f"不支持的格式: {src_file.name}", exit_code=1)

    lib_dir, target_doc_type, zone_key = _resolve_fmt_target_dir(vault_dir, domain, target_zone, subdir)
    att_dir = vault_dir / "99-Assets" / "10-Attachments"
    archive_dir = monthly_archive_dir(resolve_archive_dir(vault_dir, domain))
    env = _converter_env(vault_dir, domain)
    if fmt_options.get("encoding"):
        env["GEWU_ATLAS_TEXT_ENCODING"] = str(fmt_options["encoding"])
    extra_args = _format_arguments(converter, fmt_options)

    def normalize_output(path):
        if isolated_worker:
            # The in-process normalizer redirects process-global stdout and stderr.
            _run_py_tool(fmt_py, "md_normalizer.py", [str(path)], env=env, ws_root=ws_root, vault_dir=vault_dir)
        else:
            _normalize_md_inprocess(path)

    if converter == "note_refiner.py":
        pid_arg = Path(domain["domain_root"]).name if domain["scope"] == "project" else "AI-Zone"
        if dry_run:
            _run_py_tool(
                fmt_py,
                "note_refiner.py",
                ["-i", str(src_file), "--project-id", pid_arg, "--dry-run", *extra_args],
                env=env,
                ws_root=ws_root,
                vault_dir=vault_dir,
            )
            return {
                "source_name": src_file.name,
                "converter": converter,
                "source_type": "pdf" if converter == "pdf_converter.py" else _source_type_for_target(src_file.name),
                "domain_scope": domain["scope"],
                "project_ref": domain["project_ref"],
                "project_id": domain["project_id"],
                "dry_run": True,
            }
        before_mds = set(_scan_vault_markdowns(vault_dir))
        _run_py_tool(
            fmt_py,
            "note_refiner.py",
            ["-i", str(src_file), "--project-id", pid_arg, *extra_args],
            env=env,
            ws_root=ws_root,
            vault_dir=vault_dir,
        )
        after_mds = set(_scan_vault_markdowns(vault_dir))
        created_or_updated = sorted(after_mds - before_mds)
        if not created_or_updated:
            cand = lib_dir / f"{src_file.stem}.md"
            if cand.is_file():
                created_or_updated = [cand.relative_to(vault_dir).as_posix()]
        target_rel = created_or_updated[0] if created_or_updated else None
        title = src_file.stem
        status = "active" if domain["default_mode"] == "auto" else "pending"
        chunks_meta = None
        if target_rel:
            out_file = vault_dir / target_rel
            if target_zone and out_file.parent.resolve() != lib_dir.resolve():
                lib_dir.mkdir(parents=True, exist_ok=True)
                dest_file = _unique_library_target(lib_dir, out_file.stem)
                shutil.move(str(out_file), str(dest_file))
                out_file = dest_file
                target_rel = out_file.relative_to(vault_dir).as_posix()
            if not deep:
                normalize_output(out_file)
            else:
                chunks_meta = _extract_deep_chunks(fmt_py, out_file, env=env, ws_root=ws_root, vault_dir=vault_dir)
            title, status = _hydrate_output_card(
                out_file,
                by=by,
                domain=domain,
                source_url=source_url,
                orig_name=src_file.name,
                target_doc_type=target_doc_type if target_zone else None,
            )
        res = {
            "source_name": src_file.name,
            "converter": converter,
            "source_type": "pdf" if converter == "pdf_converter.py" else _source_type_for_target(src_file.name),
            "title": title,
            "note_name": Path(target_rel).stem if target_rel else title,
            "status": status,
            "target_zone": zone_key,
            "domain_scope": domain["scope"],
            "project_ref": domain["project_ref"],
            "project_id": domain["project_id"],
            "month_archive": month_tag(),
            "archived": True,
            "output_rel_path": target_rel,
            "dry_run": False,
        }
        if chunks_meta:
            res["deep_chunks"] = chunks_meta
        res["agent_work_required"] = bool(deep or annotate_images)
        res["processing_stage"] = "agent_pending" if deep or annotate_images else "script_complete"
        return res

    # Multimodal converters (-o <out_file> --attachments <att_dir>)
    out_file = _unique_library_target(lib_dir, src_file.stem)
    if dry_run:
        cmd_args = [str(src_file), "-o", str(out_file), "--attachments", str(att_dir), "--dry-run", *extra_args]
        if converter == "pdf_converter.py" and annotate_images:
            cmd_args.append("--annotate-images")
        _run_py_tool(fmt_py, converter, cmd_args, env=env, ws_root=ws_root, vault_dir=vault_dir)
        return {
            "source_name": src_file.name,
            "converter": converter,
            "source_type": "pdf" if converter == "pdf_converter.py" else _source_type_for_target(src_file.name),
            "domain_scope": domain["scope"],
            "project_ref": domain["project_ref"],
            "project_id": domain["project_id"],
            "dry_run": True,
        }

    lib_dir.mkdir(parents=True, exist_ok=True)
    att_dir.mkdir(parents=True, exist_ok=True)
    att_before = _snapshot_attachments(att_dir)

    cmd_args = [str(src_file), "-o", str(out_file), "--attachments", str(att_dir), *extra_args]
    if converter == "pdf_converter.py" and annotate_images:
        cmd_args.append("--annotate-images")
    _run_py_tool(fmt_py, converter, cmd_args, env=env, ws_root=ws_root, vault_dir=vault_dir)
    if not out_file.is_file():
        raise VaultStoreError("E_FMT_FAILED", f"{converter} 未产生伴生 Markdown 卡片: {src_file.name}", exit_code=1)

    if annotate_images:
        raw = out_file.read_text(encoding="utf-8")
        slot = "\n> [!NOTE] 图示解析与要素拆解\n> （待 Agent 读取图片后回填；脚本未进行视觉识别）\n"
        raw = re.sub(r"(!\[\[[^\]]+\]\])(?!\s*> \[!NOTE\])", lambda m: m.group(1)+slot, raw)
        out_file.write_text(raw, encoding="utf-8")
    chunks_meta = None
    if not deep:
        normalize_output(out_file)
    else:
        chunks_meta = _extract_deep_chunks(fmt_py, out_file, env=env, ws_root=ws_root, vault_dir=vault_dir)

    title, status = _hydrate_output_card(
        out_file,
        by=by,
        domain=domain,
        source_url=source_url,
        orig_name=src_file.name,
        target_doc_type=target_doc_type,
    )
    _archive_to_monthly_dir(src_file, archive_dir)
    _trash_dropzone_source(src_file, vault_dir)

    att_after = _snapshot_attachments(att_dir)
    res = {
        "source_name": src_file.name,
        "converter": converter,
        "source_type": "pdf" if converter == "pdf_converter.py" else _source_type_for_target(src_file.name),
        "title": title,
        "note_name": out_file.stem,
        "status": status,
        "target_zone": zone_key,
        "domain_scope": domain["scope"],
        "project_ref": domain["project_ref"],
        "project_id": domain["project_id"],
        "attachments_added": len(att_after - att_before),
        "month_archive": month_tag(),
        "archived": True,
        "output_rel_path": out_file.relative_to(vault_dir).as_posix(),
        "dry_run": False,
    }
    if fmt_options.get("dump_json") or fmt_options.get("dump_payload"):
        sidecar = out_file.with_suffix(".json" if converter == "excel_converter.py" else ".payload.json")
        if sidecar.is_file():
            detail = json.loads(sidecar.read_text(encoding="utf-8"))
            # Paths are implementation details; the public payload contains only semantic metadata.
            for key in ("abs_path", "markdown_path"): detail.pop(key, None)
            res["format_payload"] = json.loads(_scrub_paths(json.dumps(detail, ensure_ascii=False), ws_root, vault_dir))
    if chunks_meta:
        res["deep_chunks"] = chunks_meta
    res["agent_work_required"] = bool(deep or annotate_images)
    res["processing_stage"] = "agent_pending" if deep or annotate_images else "script_complete"
    return res


_MD_LINK_URL_RE = re.compile(r"(?<!!)\[[^\]]*\]\((https?://[^\s\)]+)\)")
_MD_LINK_FULL_RE = re.compile(r"(?<!!)\[[^\]]*\]\(https?://[^\s\)]+\)")
_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\([^\)]*\)")
_BARE_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+")
_CONVERTER_CALLOUT_RE = re.compile(r"(?m)^>\s*\[!NOTE\]\s*离线网页转译提示.*\n(?:>\s*.*\n)*")
_WAYBACK_RE = re.compile(r"^https?://web\.archive\.org/web/(\d{14})/(https?://.+)$", re.IGNORECASE)
MAX_URL_CRAWL_DEPTH = 3


def _validate_crawl_depth(depth: Optional[int]) -> int:
    d = 1 if depth is None else int(depth)
    if d < 1 or d > MAX_URL_CRAWL_DEPTH:
        raise VaultStoreError(
            "E_PARAM_INVALID",
            f"多层网页爬取深度 --depth 必须在 1..{MAX_URL_CRAWL_DEPTH} 之间（契约上限 {MAX_URL_CRAWL_DEPTH} 层）",
            exit_code=1,
        )
    return d


def _normalize_fetch_url(url: str) -> str:
    """Rewrite Wayback Machine wrapper URLs to raw identity (`id_`) so PDFs/pages are fetched directly."""
    u = (url or "").strip()
    m = _WAYBACK_RE.match(u)
    if m:
        return f"https://web.archive.org/web/{m.group(1)}id_/{m.group(2)}"
    return u


def _url_key(url):
    return url.strip().rstrip("/").lower()


def _url_index(lib_dir, vault_dir):
    index = {}
    for fp in sorted(lib_dir.rglob("*.md")):
        if fp.is_symlink() or not fp.is_file() or fp.name.startswith("."):
            continue
        try:
            meta, _, _ = extract_frontmatter(fp.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            # A damaged unrelated card must not block ingestion of the rest of the library.
            continue
        key = _url_key(str(meta.get("source_url") or ""))
        if key:
            index.setdefault(key, (fp.relative_to(vault_dir).as_posix(), meta.get("title") or fp.stem))
    return index


def _lookup_url(index, url):
    matches = [index[k] for k in (_url_key(url), _url_key(_normalize_fetch_url(url))) if k in index]
    return min(matches) if matches else None


active_preparation = ContextVar("atlas_url_preparation", default=None)


class UrlPreparation:
    """Snapshot the destination identity, then prepare isolated files without a registry or write lock."""

    def __init__(self, vault, domain, library, urls, index, options):
        self.vault = vault
        self.domain = domain
        self.library = library
        self.missing = [url for url in urls if not _lookup_url(index, url)]
        self.options = options
        self.items = {}
        self.index = index if not self.missing else None
        self.fmt_py = None

    def prepare(self, temp):
        from concurrent.futures import ThreadPoolExecutor
        self.fmt_py = _resolve_fmt_python()

        def one(pair):
            i, url = pair
            work = temp / str(i)
            download = work / "download"
            try:
                download.mkdir(parents=True)
                source = _download_url_safe(self.vault, _normalize_fetch_url(url), download,
                                            library=self.library, ws_root=temp)
                shadow = work / "vault"
                domain = dict(self.domain, domain_root=shadow / self.domain["domain_root"].relative_to(self.vault))
                domain["domain_root"].mkdir(parents=True)
                # Converters can archive/move their input. Keep the download for collision recovery.
                copy = shadow / ".input" / source.name
                copy.parent.mkdir()
                shutil.copy2(source, copy)
                item = _convert_single_local_file(fmt_py=self.fmt_py, isolated_worker=True, src_file=copy, vault_dir=shadow,
                    ws_root=temp, domain=domain, dry_run=False, source_url=url, **self.options)
                return url, (source, shadow, item)
            except VaultStoreError as exc:
                if exc.code in ("E_URL_NETWORK", "E_URL_POLICY"):
                    return url, exc
                return url, VaultStoreError(exc.code, "URL 下载或转换失败，请检查来源与文档格式", exit_code=exc.exit_code)
            except Exception:
                return url, VaultStoreError("E_URL_FAILED", "下载或转换失败", exit_code=1)

        with ThreadPoolExecutor(max_workers=min(4, len(self.missing))) as pool:
            self.items = dict(pool.map(one, enumerate(self.missing)))


def plan_url_preparation(vault, vault_ref, proj_reg, args):
    urls = _parse_url_batch(args.get("url"), args.get("urls"))
    if len(urls) < 2:
        return None
    _validate_crawl_depth(args.get("depth"))
    domain = resolve_domain_scope(vault_dir=vault, vault_ref=vault_ref, proj_reg=proj_reg,
                                  explicit_project_ref=args.get("project_ref"), scope=args.get("scope") or "auto")
    options = dict(by=args.get("by"), deep=bool(args.get("deep")), annotate_images=False,
                   target_zone=args.get("target_zone"), subdir=args.get("subdir"))
    library, _, _ = _resolve_fmt_target_dir(vault, domain, options["target_zone"], options["subdir"])
    return UrlPreparation(vault, domain, library, urls, _url_index(library, vault), options)


def _install_prepared(prepared, url, vault_dir, domain, library, fmt_py, ws_root):
    if prepared.vault != vault_dir or prepared.domain != domain or prepared.library != library:
        raise VaultStoreError("E_PARAM_CONFLICT", "准备期间入库目标已变更，请重新提交", exit_code=1)
    result = prepared.items.get(url)
    if isinstance(result, Exception):
        raise result
    if result is None:
        raise VaultStoreError("E_PARAM_CONFLICT", "准备期间已有来源被移除，请重新提交", exit_code=1)
    source, shadow, item = result
    if not item.get("output_rel_path"):
        raise VaultStoreError("E_FMT_FAILED", "转换未产生文档", exit_code=1)
    files = [p for p in shadow.rglob("*") if p.is_file()
             and not any(part.startswith(".") for part in p.relative_to(shadow).parts)]
    targets = [(p, vault_dir / p.relative_to(shadow)) for p in files]
    output = vault_dir / item["output_rel_path"]
    allowed_dirs = (vault_dir / "99-Assets" / "10-Attachments",
                    vault_dir / "99-Assets" / "98-Archives", resolve_archive_dir(vault_dir, domain),
                    domain["domain_root"] / "99-Assets" / "98-Archives")
    for src, dst in targets:
        if dst != output and not any(directory in dst.parents for directory in allowed_dirs):
            raise VaultStoreError("E_FMT_FAILED", "转换产物超出预期的附件或归档目录", exit_code=1)
        if src.is_symlink() or dst.resolve() != dst or vault_dir not in dst.resolve().parents:
            raise VaultStoreError("E_SECURITY_VIOLATION", "拒绝通过符号链接安装转换产物", exit_code=2)
    if not any(dst == output for _, dst in targets):
        raise VaultStoreError("E_FMT_FAILED", "转换产物缺少正文", exit_code=1)
    if any(dst.exists() for _, dst in targets):
        # Reuse the download and let the original converter rebase colliding names and attachment links.
        import tempfile
        with tempfile.TemporaryDirectory(prefix=".url-ingest-", dir=vault_dir) as temp:
            local = Path(temp) / source.name
            shutil.copy2(source, local)
            return _convert_single_local_file(fmt_py=fmt_py, src_file=local, vault_dir=vault_dir,
                ws_root=ws_root, domain=domain, dry_run=False, source_url=url, **prepared.options)
    installed = []
    try:
        for src, dst in targets:
            dst.parent.mkdir(parents=True, exist_ok=True)
            with dst.open("xb") as out, src.open("rb") as inp:
                installed.append(dst)
                shutil.copyfileobj(inp, out)
    except Exception:
        for dst in reversed(installed):
            dst.unlink(missing_ok=True)
        raise
    return item


def _extract_url_body_metrics(body: str, source_url: str, *, max_links: int = 30) -> Tuple[int, int, List[str]]:
    """Compute net char_count, link-stripped prose_chars, and deduplicated outbound HTTP(S) links."""
    stripped_body = _CONVERTER_CALLOUT_RE.sub("", body or "")
    raw_chars = len(re.sub(r"[\s#*`>|\-_]+", "", stripped_body))
    prose_only = _MD_IMG_RE.sub("", stripped_body)
    prose_only = _MD_LINK_FULL_RE.sub("", prose_only)
    prose_only = _BARE_URL_RE.sub("", prose_only)
    prose_chars = len(re.sub(r"[\s#*`>|\-_]+", "", prose_only))

    src_norm = (source_url or "").strip().rstrip("/").lower()
    links: List[str] = []
    seen: set[str] = set()
    for cand in (*_MD_LINK_URL_RE.findall(stripped_body), *_BARE_URL_RE.findall(stripped_body)):
        u = cand.rstrip(".,;:!?)\"]")
        u_norm = u.rstrip("/").lower()
        if not u or u_norm == src_norm or u_norm in seen:
            continue
        seen.add(u_norm)
        links.append(u)
        if len(links) >= max_links:
            break
    return raw_chars, prose_chars, links


def _evaluate_url_output(
    md_path: Path,
    source_url: str,
    *,
    min_chars: int = 300,
    depth: int = 1,
    parent_url: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """Evaluate downloaded URL markdown under dual-threshold gate (char_count & link-stripped prose_chars).

    Returns (override_result_if_deleted, metrics_dict).
    """
    if not md_path.is_file():
        return None, {}
    try:
        raw = md_path.read_text(encoding="utf-8", errors="replace")
        _, body, _ = extract_frontmatter(raw)
    except Exception:
        return None, {}

    raw_chars, prose_chars, links = _extract_url_body_metrics(body, source_url, max_links=30)
    is_index_cand = bool(len(links) >= 5 and prose_chars < max(600, len(links) * 35))
    metrics: Dict[str, Any] = {
        "char_count": raw_chars,
        "prose_chars": prose_chars,
        "link_count": len(links),
        "depth": depth,
    }
    if parent_url:
        metrics["parent_url"] = parent_url
    if is_index_cand:
        metrics["index_candidate"] = True
        metrics["extracted_links"] = links

    if min_chars <= 0:
        return None, metrics
    if raw_chars >= min_chars and prose_chars >= min_chars:
        return None, metrics

    # Empty stub or pure directory page (prose_chars < min_chars): unlink so zero junk enters 21-Library/.
    try:
        md_path.unlink(missing_ok=True)
    except OSError:
        pass

    base_meta: Dict[str, Any] = {
        "source_name": source_url,
        "char_count": raw_chars,
        "prose_chars": prose_chars,
        "min_chars": min_chars,
        "depth": depth,
        "auto_deleted": True,
    }
    if parent_url:
        base_meta["parent_url"] = parent_url

    if links:
        return {
            **base_meta,
            "status": "thin_with_links",
            "is_directory_only": raw_chars >= min_chars,
            "preview": body.strip()[:1200],
            "extracted_links": links,
            "next_depth": depth + 1 if depth < MAX_URL_CRAWL_DEPTH else None,
            "depth_limit_reached": depth >= MAX_URL_CRAWL_DEPTH,
        }, metrics
    return {
        **base_meta,
        "status": "discarded_too_short",
    }, metrics


def _inspect_thin_url_output(
    md_path: Path,
    source_url: str,
    *,
    min_chars: int = 300,
    depth: int = 1,
    parent_url: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Quality gate for downloaded URL content (<min_chars net body or link-stripped prose):
    - Has outbound HTTP(S) links -> remove stub & return thin_with_links for Agent follow-up decision.
    - No outbound links -> remove stub & return discarded_too_short.
    """
    override, _ = _evaluate_url_output(md_path, source_url, min_chars=min_chars, depth=depth, parent_url=parent_url)
    return override


def _append_url_ingest_log(
    *,
    vault_dir: Path,
    vault_ref: str,
    domain: Dict[str, Any],
    note_reg: KekNoteRegistry,
    by: Optional[str],
    subdir: Optional[str],
    converted_items: List[Dict[str, Any]],
    thin_items: List[Dict[str, Any]],
    discarded_items: List[Dict[str, Any]],
    blocked_items: List[Dict[str, Any]],
) -> Optional[str]:
    """Append structured URL ingest receipt to <domain>/30-Workbench/[ws-<agent>/]91-Temp/url-ingest-log.md."""
    from datetime import datetime
    from notes.atlas_note_engine import _atomic_write_md

    wb_root = Path(domain["domain_root"]) / "30-Workbench"
    sub_parts = Path(str(subdir or "").strip().strip("/")).parts if subdir else ()
    if sub_parts and sub_parts[0].startswith("ws-"):
        log_dir = wb_root / sub_parts[0] / "91-Temp"
    else:
        log_dir = wb_root / "91-Temp"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "url-ingest-log.md"

    ts = datetime.now().astimezone().isoformat(timespec="seconds")
    actor = (by or "ai").strip() or "ai"
    pid = str(domain.get("project_id") or "AI-Zone")
    if not log_path.is_file():
        header = "\n".join(
            [
                "---",
                "doc_type: workbench",
                'title: "URL 采集日志"',
                f'summary: "{pid} 域 URL 采集批次台账（含成功句柄、短页待追查外链、抛弃与失败记录）"',
                "status: active",
                f'project_id: "{pid}"',
                'tags: ["Workbench", "UrlIngestLog"]',
                f"updated: {date.today().isoformat()}",
                "---",
                "",
                "# URL 采集日志 (url-ingest-log)",
                "",
            ]
        )
        _atomic_write_md(log_path, header)

    rows: List[str] = []
    def _esc(s: Any) -> str:
        return str(s or "").replace("|", "/").replace("\n", " ").strip()

    for it in converted_items:
        st_lbl = "already_ingested" if it.get("already_ingested") else ("index_candidate" if it.get("index_candidate") else "ok")
        cnt_lbl = f"{it.get('prose_chars', '-')}/{it.get('char_count', '-')}" if "prose_chars" in it else str(it.get("char_count", "-"))
        lks = ", ".join(it.get("extracted_links") or [])[:180] if it.get("index_candidate") else "-"
        rows.append(f"| L{it.get('depth', 1)} | {_esc(it.get('source_name'))} | {st_lbl} | `{_esc(it.get('note_ref') or it.get('note_name'))}` | {cnt_lbl} | {_esc(lks)} |")
    for it in thin_items:
        lks = ", ".join(it.get("extracted_links") or [])[:180]
        cnt_lbl = f"{it.get('prose_chars', 0)}/{it.get('char_count', 0)}"
        rows.append(f"| L{it.get('depth', 1)} | {_esc(it.get('source_name'))} | thin_with_links | 未落盘（待追查外链） | {cnt_lbl} | {_esc(lks)} |")
    for it in discarded_items:
        cnt_lbl = f"{it.get('prose_chars', 0)}/{it.get('char_count', 0)}"
        rows.append(f"| L{it.get('depth', 1)} | {_esc(it.get('source_name'))} | discarded_too_short | <={it.get('min_chars', 300)}字且无外链已顺风车删除 | {cnt_lbl} | - |")
    for it in blocked_items:
        rows.append(f"| L{it.get('depth', 1)} | {_esc(it.get('source_name'))} | blocked | {_esc(it.get('code'))}: {_esc(it.get('reason'))[:120]} | - | - |")

    entry = "\n".join(
        [
            f"## [{ts}] · Agent: `{actor}` · 成功={len(converted_items)} | 短页/目录待追查={len(thin_items)} | 自动删除={len(discarded_items)} | 失败={len(blocked_items)}",
            "",
            "| 层级 | URL | 状态 | 句柄 / 原因 | 纯正文/总字数 | 待追查外链 |",
            "| :---: | :--- | :--- | :--- | :---: | :--- |",
            *rows,
            "",
        ]
    )
    existing = log_path.read_text(encoding="utf-8")
    _atomic_write_md(log_path, existing.rstrip() + "\n\n" + entry)
    all_md = _scan_vault_markdowns(vault_dir)
    n_map = note_reg.sync_paths(vault_ref, all_md)
    return n_map.get(log_path.relative_to(vault_dir).as_posix())


def _convert_single_url(
    *,
    fmt_py: Path,
    url: str,
    vault_dir: Path,
    ws_root: Path,
    domain: Dict[str, Any],
    by: Optional[str],
    dry_run: bool,
    deep: bool,
    note_reg: KekNoteRegistry,
    vault_ref: str,
    annotate_images: bool = False,
    forced_stem: Optional[str] = None,
    target_zone: Optional[str] = None,
    subdir: Optional[str] = None,
    defer_sync: bool = False,
    url_index=None,
    prepared=None,
    min_chars: int = 300,
    depth: int = 1,
    parent_url: Optional[str] = None,
) -> Dict[str, Any]:
    lib_dir, target_doc_type, zone_key = _resolve_fmt_target_dir(vault_dir, domain, target_zone, subdir)
    att_dir = vault_dir / "99-Assets" / "10-Attachments"
    env = _converter_env(vault_dir, domain)
    env["GEWU_ATLAS_ANNOTATE_IMAGES"] = "1" if annotate_images else "0"

    slug_raw = re.sub(r"^https?://", "", url)
    slug = _slugify_filename(re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "-", slug_raw).strip("-")[:48] or "page")
    stem = forced_stem or f"Web-{date.today():%Y%m%d}-{slug}"
    out_file = _unique_library_target(lib_dir, stem)

    if dry_run:
        check_url(vault_dir, url, resolve_dns=False)
        _run_py_tool(
            fmt_py,
            "web_converter.py",
            [url, "-o", str(out_file), "--attachments", str(att_dir), "--dry-run"],
            env=env,
            ws_root=ws_root,
            vault_dir=vault_dir,
        )
        return {
            "source_name": url,
            "converter": "web_converter.py",
            "source_type": "web",
            "target_zone": zone_key,
            "domain_scope": domain["scope"],
            "project_ref": domain["project_ref"],
            "project_id": domain["project_id"],
            "depth": depth,
            "dry_run": True,
            "network_validated": False,
        }

    fetch_url = _normalize_fetch_url(url)
    index = url_index if url_index is not None else _url_index(lib_dir, vault_dir)
    hit = _lookup_url(index, url)
    if hit:
        rel, title = hit
        existing_dup = {"status": "already_ingested", "already_ingested": True, "source_name": url,
                        "output_rel_path": rel, "note_name": Path(rel).stem, "title": title}
        if not defer_sync:
            existing_dup.pop("output_rel_path")
            existing_dup["note_ref"] = note_reg.sync_paths(vault_ref, _scan_vault_markdowns(vault_dir)).get(rel)
        existing_dup["target_zone"] = zone_key
        existing_dup["domain_scope"] = domain["scope"]
        existing_dup["project_ref"] = domain["project_ref"]
        existing_dup["project_id"] = domain["project_id"]
        existing_dup["depth"] = depth
        return existing_dup

    # One download/type-dispatch path for direct URLs and URL lists. A PDF URL is
    # not a webpage, regardless of its suffix or the server's Content-Type.
    if prepared is not None:
        item = _install_prepared(prepared, url, vault_dir, domain, lib_dir, fmt_py, ws_root)
    else:
        import tempfile
        with tempfile.TemporaryDirectory(prefix=".url-ingest-", dir=vault_dir) as temp:
            source = _download_url_safe(vault_dir, fetch_url, Path(temp), library=lib_dir, ws_root=ws_root)
            item = _convert_single_local_file(
                fmt_py=fmt_py, src_file=source, vault_dir=vault_dir, ws_root=ws_root,
                domain=domain, by=by, dry_run=False, deep=deep,
                annotate_images=annotate_images, source_url=url,
                target_zone=target_zone, subdir=subdir,
            )
    item["source_name"] = url
    rel_out = item.get("output_rel_path")
    out_md_p = (vault_dir / str(rel_out)) if rel_out else (Path(item["_out_path"]) if item.get("_out_path") else None)
    if out_md_p is not None:
        override, metrics = _evaluate_url_output(
            out_md_p, url, min_chars=min_chars, depth=depth, parent_url=parent_url,
        )
        if override is not None:
            return override
        item.update(metrics)
        if rel_out and out_md_p.is_file() and "source_url" in allowed_fields_for_doc_type(target_doc_type):
            index.setdefault(_url_key(url), (rel_out, item.get("title") or out_md_p.stem))
    if defer_sync:
        return item
    all_md = _scan_vault_markdowns(vault_dir)
    n_map = note_reg.sync_paths(vault_ref, all_md)
    return _to_public_fmt_item(item, n_map, vault_dir)


def _ingest_url_list_file(
    *,
    fmt_py: Path,
    src_file: Path,
    vault_dir: Path,
    ws_root: Path,
    vault_ref: str,
    domain: Dict[str, Any],
    by: Optional[str],
    dry_run: bool,
    deep: bool,
    annotate_images: bool,
    note_reg: KekNoteRegistry,
    ticket_id: Optional[str] = None,
    pre_ticket: Optional[Dict[str, Any]] = None,
    suppress_ticket: bool = False,
    target_zone: Optional[str] = None,
    subdir: Optional[str] = None,
    fmt_options: Optional[Dict[str, Any]] = None,
    min_chars: int = 300,
    depth: int = 1,
    parent_url: Optional[str] = None,
) -> Dict[str, Any]:
    raw_text = src_file.read_text(encoding="utf-8-sig", errors="replace")
    parsed = parse_url_request(raw_text)
    if not parsed:
        raise VaultStoreError("E_PARAM_INVALID", "未检测到独占行 HTTP(S) URL 清单", exit_code=1)
    urls, batch_req, url_reqs = parsed
    lib_dir, _, _ = _resolve_fmt_target_dir(vault_dir, domain, target_zone, subdir)
    stage_dir = vault_dir / ".runtime-url-stage" / hashlib.sha256(src_file.read_bytes()).hexdigest()[:12]

    if dry_run:
        return {
            "source_name": src_file.name,
            "mode": "url-list",
            "url_count": len(urls),
            "urls": urls,
            "has_requirements": bool(batch_req or any(url_reqs.values())),
            "domain_scope": domain["scope"],
            "project_ref": domain["project_ref"],
            "project_id": domain["project_id"],
            "dry_run": True,
        }

    has_reqs = bool(batch_req or any(url_reqs.values()))
    url_hash = hashlib.sha256("\n".join(urls).encode("utf-8")).hexdigest()[:10]
    url_task_key = f"url-list:{vault_ref}:{domain.get('project_id', '')}:{url_hash}"
    if pre_ticket is None and not suppress_ticket and ticket_id:
        pre_ticket = ensure_doc_ticket(
            ws_root,
            vault_dir,
            task_key=url_task_key,
            title=f"URL 任务清单：{src_file.stem}（{len(urls)} 项）",
            summary=batch_req or f"采集 {len(urls)} 条 URL 并核验产物入库",
            project_id=str(domain.get("project_id") or ""),
            ticket_id=ticket_id,
        )

    results: List[Dict[str, Any]] = []
    thin_items: List[Dict[str, Any]] = []
    discarded_items: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    lib_dir.mkdir(parents=True, exist_ok=True)
    try:
        for u in urls:
            try:
                dl_path = download_url(vault_dir, u, stage_dir, library=lib_dir)
                item = _convert_single_local_file(
                    fmt_py=fmt_py,
                    src_file=dl_path,
                    vault_dir=vault_dir,
                    ws_root=ws_root,
                    domain=domain,
                    by=by or "gewu-atlas-agent",
                    dry_run=False,
                    deep=deep,
                    annotate_images=annotate_images,
                    fmt_options=fmt_options,
                    source_url=u,
                    target_zone=target_zone,
                    subdir=subdir,
                )
                rel_out = item.get("output_rel_path")
                if rel_out:
                    override, metrics = _evaluate_url_output(
                        vault_dir / str(rel_out), u, min_chars=min_chars, depth=depth, parent_url=parent_url,
                    )
                    if override is not None:
                        if override.get("status") == "thin_with_links":
                            thin_items.append(override)
                        else:
                            discarded_items.append(override)
                        continue
                    item.update(metrics)
                results.append(item)
            except Exception as exc:
                errors.append({"url": u, "reason": _scrub_paths(str(exc), ws_root, vault_dir)})
    finally:
        if stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)
        if stage_dir.parent.exists() and not any(stage_dir.parent.iterdir()):
            stage_dir.parent.rmdir()

    all_md = _scan_vault_markdowns(vault_dir)
    n_map = note_reg.sync_paths(vault_ref, all_md)
    for item in results:
        rel = item.pop("output_rel_path", None)
        if rel:
            item["note_ref"] = n_map.get(rel)

    log_ref_list = _append_url_ingest_log(
        vault_dir=vault_dir,
        vault_ref=vault_ref,
        domain=domain,
        note_reg=note_reg,
        by=by,
        subdir=subdir,
        converted_items=results,
        thin_items=thin_items,
        discarded_items=discarded_items,
        blocked_items=[{"source_name": e["url"], "code": "E_URL_FAILED", "reason": e["reason"]} for e in errors],
    )

    trashed_list = False
    if not errors:
        _trash_dropzone_source(src_file, vault_dir)
        trashed_list = True

    idx_cand_refs = [str(it["note_ref"]) for it in results if it.get("index_candidate") and it.get("note_ref")]
    out_list: Dict[str, Any] = {
        "source_name": src_file.name,
        "mode": "url-list",
        "url_count": len(urls),
        "converted_count": len(results),
        "thin_with_links_count": len(thin_items),
        "discarded_count": len(discarded_items),
        "failed_count": len(errors),
        "blocked_count": len(errors),
        "depth": depth,
        "max_depth": MAX_URL_CRAWL_DEPTH,
        "next_depth": depth + 1 if depth < MAX_URL_CRAWL_DEPTH else None,
        "depth_limit_reached": depth >= MAX_URL_CRAWL_DEPTH,
        "index_candidate_refs": idx_cand_refs,
        "list_archived_to_trash": trashed_list,
        "has_requirements": has_reqs,
        "results": results,
        "thin_with_links": thin_items,
        "discarded": discarded_items,
        "errors": errors,
    }
    if log_ref_list:
        out_list["ingest_log_ref"] = log_ref_list
    if not suppress_ticket:
        tinfo = pre_ticket or ensure_doc_ticket(
            ws_root,
            vault_dir,
            task_key=url_task_key,
            title=f"URL 任务清单：{src_file.stem}（{len(urls)} 项）",
            summary=batch_req or f"采集 {len(urls)} 条 URL 并核验产物入库",
            project_id=str(domain.get("project_id") or ""),
            ticket_id=ticket_id,
        )
        doc_ticket = update_doc_ticket_progress(
            ws_root,
            vault_dir,
            ticket_info=tinfo,
            note=f"url-list 完成：total={len(urls)}, ok={len(results)}, failed={len(errors)}, trashed={trashed_list}, has_requirements={has_reqs}",
            mark_done=(len(errors) == 0 and not has_reqs),
        )
        out_list["doc_ticket"] = {k: v for k, v in doc_ticket.items() if k != "ticket_rel_path"}
    return out_list


def _scan_dropzone_candidates(vault_dir: Path, file_name: Optional[str] = None) -> List[Path]:
    dz_root = vault_dir / "00-DropZone"
    if not dz_root.is_dir():
        return []
    candidates: List[Path] = []
    for p in sorted(dz_root.rglob("*")):
        if p.is_symlink() or not p.is_file():
            continue
        rel_parts = p.relative_to(dz_root).parts
        if any(part.startswith((".", "_")) or part == "91-Temp" for part in rel_parts):
            continue
        ext = p.suffix.lower()
        if file_name:
            if p.name == file_name:
                candidates.append(p)
            continue
        if ext in MULTIMODAL_SWEEP_EXTS:
            candidates.append(p)
        elif ext in (".md", ".markdown"):
            try:
                if _is_pdf_file(p) or parse_url_request(p.read_text(encoding="utf-8-sig", errors="replace")):
                    candidates.append(p)
            except OSError:
                pass
    if file_name and len(candidates) > 1:
        raise VaultStoreError("E_AMBIGUOUS", f"DropZone 存在多个同名文件: {file_name}", exit_code=1)
    if file_name and not candidates:
        raise VaultStoreError("E_NOT_FOUND", f"DropZone 未找到指定文件: {file_name}", exit_code=1)
    return candidates


def _to_public_fmt_item(item: Dict[str, Any], note_map: Dict[str, str], vault_dir: Path) -> Dict[str, Any]:
    rel = item.pop("output_rel_path", None)
    out = dict(item)
    if rel:
        out["note_ref"] = note_map.get(rel)
    if not out.get("already_ingested") and out.get("note_name") and out.get("title"):
        out["wikilink"] = f"[[{out['note_name']}|{out['title']}]]"
    return out


def _parse_url_batch(url: Optional[str], urls: Optional[Any]) -> List[str]:
    out: List[str] = []
    def _push(raw: Any) -> None:
        if raw is None:
            return
        if isinstance(raw, list):
            for x in raw:
                _push(x)
            return
        for part in re.split(r"[\r\n,]+", str(raw)):
            s = part.strip()
            if s and s not in out:
                out.append(s)
    _push(url)
    _push(urls)
    return out


def run_fmt(
    *,
    vault_dir: Path,
    vault_ref: str,
    ws_root: Path,
    proj_reg: KekProjectRegistry,
    note_reg: KekNoteRegistry,
    note_ref: Optional[str] = None,
    project_ref: Optional[str] = None,
    scope: str = "auto",
    url: Optional[str] = None,
    urls: Optional[Any] = None,
    file_name: Optional[str] = None,
    by: Optional[str] = None,
    dry_run: bool = False,
    deep: bool = False,
    annotate_images: bool = False,
    normalize: bool = False,
    check: bool = False,
    ticket_id: Optional[str] = None,
    target_zone: Optional[str] = None,
    subdir: Optional[str] = None,
    fmt_options: Optional[Dict[str, Any]] = None,
    min_chars: Optional[int] = None,
    depth: Optional[int] = None,
    prepared=None,
    parent_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute KEK-blinded multimodal `fmt` pipeline across all 116 converters."""
    if file_name and ("/" in file_name or "\\" in file_name or ".." in file_name):
        raise VaultStoreError("E_PARAM_INVALID", "--file_name 仅接受纯文件名（禁止目录前缀与上级跳转）", exit_code=1)

    url_list = _parse_url_batch(url, urls)
    if check and not normalize:
        raise VaultStoreError("E_PARAM_INVALID", "check 仅适用于 normalize", exit_code=1)
    if normalize and (deep or url_list or file_name or not note_ref):
        raise VaultStoreError("E_PARAM_INVALID", "normalize 要求 note_ref，且不能与 deep/url/file_name 混用", exit_code=1)
    fmt_options = fmt_options or {}
    for key, low, high in (("dpi", 36, 600), ("threshold", 1, 10000), ("decorative_size", 1, 10000), ("decorative_dim", 1, 10000)):
        if key in fmt_options and not low <= int(fmt_options[key]) <= high:
            raise VaultStoreError("E_PARAM_INVALID", f"{key} 必须在 {low}..{high}", exit_code=1)
    if fmt_options.get("encoding"):
        import codecs
        try: codecs.lookup(str(fmt_options["encoding"]))
        except LookupError as exc: raise VaultStoreError("E_PARAM_INVALID", "未知文本编码", exit_code=1) from exc
    # A locked all-hit replay never launches a converter.
    fmt_py = prepared.fmt_py if prepared is not None else _resolve_fmt_python()
    pre_ticket: Optional[Dict[str, Any]] = None
    if ticket_id and not dry_run and not (normalize and check):
        pre_ticket = ensure_doc_ticket(
            ws_root,
            vault_dir,
            task_key=f"fmt-sweep:{vault_ref}:{project_ref or 'inbox'}",
            title="DropZone 多模态转译与入库",
            ticket_id=ticket_id,
        )

    # 1. URL / batch-URLs conversion mode
    if url_list:
        eff_depth = _validate_crawl_depth(depth)
        domain = resolve_domain_scope(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            proj_reg=proj_reg,
            explicit_project_ref=project_ref,
            scope=scope,
        )
        eff_min_chars = 300 if min_chars is None else max(0, int(min_chars))
        if len(url_list) == 1:
            item = _convert_single_url(
                fmt_py=fmt_py,
                url=url_list[0],
                vault_dir=vault_dir,
                ws_root=ws_root,
                domain=domain,
                by=by,
                dry_run=dry_run,
                deep=deep,
                annotate_images=annotate_images,
                note_reg=note_reg,
                vault_ref=vault_ref,
                target_zone=target_zone,
                subdir=subdir,
                min_chars=eff_min_chars,
                depth=eff_depth,
                parent_url=parent_url,
            )
            st_item = item.get("status")
            thin_list = [item] if st_item == "thin_with_links" else []
            disc_list = [item] if st_item == "discarded_too_short" else []
            ok_list = [] if (thin_list or disc_list) else [item]
            log_ref: Optional[str] = None
            if not dry_run:
                if ok_list:
                    _append_journal_event(vault_dir, note_reg, vault_ref, str(item.get("note_name") or ""), f"URL 转译入库（{item.get('note_ref')}）")
                log_ref = _append_url_ingest_log(
                    vault_dir=vault_dir, vault_ref=vault_ref, domain=domain, note_reg=note_reg,
                    by=by, subdir=subdir, converted_items=ok_list, thin_items=thin_list, discarded_items=disc_list, blocked_items=[],
                )
            idx_cand_refs = [str(it["note_ref"]) for it in ok_list if it.get("index_candidate") and it.get("note_ref")]
            out_url: Dict[str, Any] = {
                "mode": "url",
                "vault_ref": vault_ref,
                "dry_run": dry_run,
                "converted_count": 0 if dry_run else len(ok_list),
                "thin_with_links_count": len(thin_list),
                "discarded_count": len(disc_list),
                "blocked_count": 0,
                "depth": eff_depth,
                "max_depth": MAX_URL_CRAWL_DEPTH,
                "next_depth": eff_depth + 1 if eff_depth < MAX_URL_CRAWL_DEPTH else None,
                "depth_limit_reached": eff_depth >= MAX_URL_CRAWL_DEPTH,
                "index_candidate_refs": idx_cand_refs,
                "target_zone": item.get("target_zone"),
                "results": ok_list,
                "thin_with_links": thin_list,
                "discarded": disc_list,
                "blocked": [],
            }
            if log_ref:
                out_url["ingest_log_ref"] = log_ref
            for k in ("note_ref", "note_name", "wikilink"):
                if item.get(k):
                    out_url[k] = item[k]
            if pre_ticket is not None and not dry_run:
                dt = update_doc_ticket_progress(
                    ws_root,
                    vault_dir,
                    ticket_info=pre_ticket,
                    note=f"单条 URL 转译入库（depth={eff_depth}）：converted={len(ok_list)}, thin={len(thin_list)}, discarded={len(disc_list)}",
                    mark_done=False,
                )
                out_url["doc_ticket"] = {k: v for k, v in dt.items() if k != "ticket_rel_path"}
            return out_url

        raw_batch: List[Dict[str, Any]] = []
        thin_batch: List[Dict[str, Any]] = []
        disc_batch: List[Dict[str, Any]] = []
        blocked_batch: List[Dict[str, Any]] = []
        lib_dir, _, _ = _resolve_fmt_target_dir(vault_dir, domain, target_zone, subdir)
        index = (prepared.index if prepared and prepared.index is not None else
                 {} if dry_run else _url_index(lib_dir, vault_dir))
        for single_u in url_list:
            try:
                res_one = _convert_single_url(
                    fmt_py=fmt_py,
                    url=single_u,
                    vault_dir=vault_dir,
                    ws_root=ws_root,
                    domain=domain,
                    by=by,
                    dry_run=dry_run,
                    deep=deep,
                    annotate_images=annotate_images,
                    note_reg=note_reg,
                    vault_ref=vault_ref,
                    target_zone=target_zone,
                    subdir=subdir,
                    defer_sync=not dry_run, url_index=index, prepared=prepared,
                    min_chars=eff_min_chars,
                    depth=eff_depth,
                    parent_url=parent_url,
                )
                if res_one.get("status") == "thin_with_links":
                    thin_batch.append(res_one)
                elif res_one.get("status") == "discarded_too_short":
                    disc_batch.append(res_one)
                else:
                    raw_batch.append(res_one)
            except VaultStoreError as exc:
                blocked_batch.append({"source_name": single_u, "code": exc.code, "reason": exc.message})
            except Exception as exc:
                blocked_batch.append({"source_name": single_u, "code": "E_URL_FAILED", "reason": _scrub_paths(str(exc), ws_root, vault_dir)})

        log_ref_batch: Optional[str] = None
        if not dry_run:
            all_md = _scan_vault_markdowns(vault_dir)
            n_map = note_reg.sync_paths(vault_ref, all_md)
            pub_batch = [_to_public_fmt_item(it, n_map, vault_dir) for it in raw_batch]
            if pub_batch:
                _append_journal_event(vault_dir, note_reg, vault_ref, "", f"批量 URL 入库（depth={eff_depth}，成功 {len(pub_batch)} 篇，待追查 {len(thin_batch)} 篇，自动删除 {len(disc_batch)} 篇，失败 {len(blocked_batch)} 篇）")
            log_ref_batch = _append_url_ingest_log(
                vault_dir=vault_dir, vault_ref=vault_ref, domain=domain, note_reg=note_reg,
                by=by, subdir=subdir, converted_items=pub_batch, thin_items=thin_batch, discarded_items=disc_batch, blocked_items=blocked_batch,
            )
        else:
            pub_batch = raw_batch

        idx_cand_batch = [str(it["note_ref"]) for it in pub_batch if it.get("index_candidate") and it.get("note_ref")]
        out_batch: Dict[str, Any] = {
            "mode": "url-batch",
            "vault_ref": vault_ref,
            "dry_run": dry_run,
            "url_count": len(url_list),
            "converted_count": 0 if dry_run else len(pub_batch),
            "thin_with_links_count": len(thin_batch),
            "discarded_count": len(disc_batch),
            "blocked_count": len(blocked_batch),
            "depth": eff_depth,
            "max_depth": MAX_URL_CRAWL_DEPTH,
            "next_depth": eff_depth + 1 if eff_depth < MAX_URL_CRAWL_DEPTH else None,
            "depth_limit_reached": eff_depth >= MAX_URL_CRAWL_DEPTH,
            "index_candidate_refs": idx_cand_batch,
            "results": pub_batch,
            "thin_with_links": thin_batch,
            "discarded": disc_batch,
            "blocked": blocked_batch,
        }
        if log_ref_batch:
            out_batch["ingest_log_ref"] = log_ref_batch
        if pre_ticket is not None and not dry_run:
            dt = update_doc_ticket_progress(
                ws_root,
                vault_dir,
                ticket_info=pre_ticket,
                note=f"批量 URL 转译入库：total={len(url_list)}, converted={len(pub_batch)}, thin={len(thin_batch)}, discarded={len(disc_batch)}, blocked={len(blocked_batch)}",
                mark_done=False,
            )
            out_batch["doc_ticket"] = {k: v for k, v in dt.items() if k != "ticket_rel_path"}
        return out_batch

    # 2. Specific note_ref mode (--normalize or DropZone URL-list or 6-path note_refiner)
    if note_ref:
        rec = note_reg.resolve_handle(vault_ref, note_ref)
        rel = str(rec["note_rel_path"])
        src_file = vault_dir / rel
        if not src_file.is_file():
            raise VaultStoreError("E_NOT_FOUND", f"笔记不存在: {note_ref}", exit_code=1)
        if rel.startswith("05-Journal/"):
            raise VaultStoreError("E_JOURNAL_PROTECTED", "[BLOCKED] 日记原文不进入普通fmt，请显式挑选内容另存为素材", exit_code=1)
        domain = resolve_domain_scope(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            proj_reg=proj_reg,
            explicit_project_ref=project_ref,
            scope=scope,
            source_rel_path=rel,
        )
        env = _converter_env(vault_dir, domain)
        if normalize:
            issues = _normalize_md_inprocess(
                src_file,
                check_only=bool(check or dry_run),
                no_symbols=bool(fmt_options.get("no_symbols")),
            )
            return {
                "mode": "normalize",
                "vault_ref": vault_ref,
                "note_ref": note_ref,
                "check": check,
                "ok": (issues == 0) if (check or dry_run) else True,
                "notes": "passed" if (check and issues == 0) else ("issues_found" if check else "normalized"),
            }
        if not _is_pdf_file(src_file) and rel.startswith("00-DropZone/") and src_file.suffix.lower() in (".md", ".markdown"):
            if parse_url_request(src_file.read_text(encoding="utf-8-sig", errors="replace")):
                eff_depth = _validate_crawl_depth(depth)
                eff_min_chars = 300 if min_chars is None else max(0, int(min_chars))
                res = _ingest_url_list_file(
                    fmt_py=fmt_py,
                    src_file=src_file,
                    vault_dir=vault_dir,
                    ws_root=ws_root,
                    vault_ref=vault_ref,
                    domain=domain,
                    by=by,
                    dry_run=dry_run,
                    deep=deep,
                    annotate_images=annotate_images,
                    fmt_options=fmt_options,
                    note_reg=note_reg,
                    ticket_id=ticket_id,
                    pre_ticket=pre_ticket,
                    target_zone=target_zone,
                    subdir=subdir,
                    min_chars=eff_min_chars,
                    depth=eff_depth,
                    parent_url=parent_url,
                )
                if not dry_run:
                    _append_journal_event(vault_dir, note_reg, vault_ref, src_file.stem, f"URL 清单采集完成（{res['converted_count']} 篇）")
                return {"mode": "url-list", "vault_ref": vault_ref, **res}

        item = _convert_single_local_file(
            fmt_py=fmt_py,
            src_file=src_file,
            vault_dir=vault_dir,
            ws_root=ws_root,
            domain=domain,
            by=by,
            dry_run=dry_run,
            deep=deep,
            annotate_images=annotate_images,
                    fmt_options=fmt_options,
            target_zone=target_zone,
            subdir=subdir,
        )
        all_md = _scan_vault_markdowns(vault_dir)
        n_map = note_reg.sync_paths(vault_ref, all_md)
        pub = _to_public_fmt_item(item, n_map, vault_dir)
        if not dry_run:
            _append_journal_event(vault_dir, note_reg, vault_ref, str(pub.get("title") or src_file.stem), f"多模态转译入库（{pub.get('note_ref')}）")
        out_note: Dict[str, Any] = {
            "mode": "note",
            "vault_ref": vault_ref,
            "dry_run": dry_run,
            "converted_count": 0 if dry_run else 1,
            "blocked_count": 0,
            "results": [pub],
            "blocked": [],
        }
        if pre_ticket is not None and not dry_run:
            dt = update_doc_ticket_progress(
                ws_root,
                vault_dir,
                ticket_info=pre_ticket,
                note=f"单文件转译入库：note_ref={pub.get('note_ref')} title=「{pub.get('title') or ''}」",
                mark_done=False,
            )
            out_note["doc_ticket"] = {k: v for k, v in dt.items() if k != "ticket_rel_path"}
        return out_note

    # 3. DropZone sweep or --file_name mode
    candidates = _scan_dropzone_candidates(vault_dir, file_name=file_name)
    if not candidates:
        empty_fmt: Dict[str, Any] = {
            "mode": "dropzone-sweep",
            "vault_ref": vault_ref,
            "dry_run": dry_run,
            "converted_count": 0,
            "blocked_count": 0,
            "results": [],
            "blocked": [],
            "message": "00-DropZone 暂无待转换的多模态素材或 URL 清单",
        }
        if pre_ticket is not None and not dry_run:
            dt = update_doc_ticket_progress(
                ws_root,
                vault_dir,
                ticket_info=pre_ticket,
                note="fmt sweep：00-DropZone 暂无待转换素材",
                mark_done=False,
            )
            empty_fmt["doc_ticket"] = {k: v for k, v in dt.items() if k != "ticket_rel_path"}
        return empty_fmt

    raw_results: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    url_list_results: List[Dict[str, Any]] = []

    for src in candidates:
        rel = src.relative_to(vault_dir).as_posix()
        try:
            domain = _infer_dropzone_project_ref(vault_dir, vault_ref, proj_reg, rel, project_ref, scope)
            if not _is_pdf_file(src) and src.suffix.lower() in (".md", ".markdown") and parse_url_request(
                src.read_text(encoding="utf-8-sig", errors="replace")
            ):
                ures = _ingest_url_list_file(
                    fmt_py=fmt_py,
                    src_file=src,
                    vault_dir=vault_dir,
                    ws_root=ws_root,
                    vault_ref=vault_ref,
                    domain=domain,
                    by=by,
                    dry_run=dry_run,
                    deep=deep,
                    annotate_images=annotate_images,
                    fmt_options=fmt_options,
                    note_reg=note_reg,
                    suppress_ticket=True,
                    target_zone=target_zone,
                    subdir=subdir,
                )
                url_list_results.append(ures)
                continue
            item = _convert_single_local_file(
                fmt_py=fmt_py,
                src_file=src,
                vault_dir=vault_dir,
                ws_root=ws_root,
                domain=domain,
                by=by,
                dry_run=dry_run,
                deep=deep,
                annotate_images=annotate_images,
                    fmt_options=fmt_options,
                target_zone=target_zone,
                subdir=subdir,
            )
            raw_results.append(item)
        except Exception as exc:
            blocked.append({"source_name": src.name, "reason": _scrub_paths(str(exc), ws_root, vault_dir)})

    all_md = _scan_vault_markdowns(vault_dir)
    n_map = note_reg.sync_paths(vault_ref, all_md)
    pub_results = [_to_public_fmt_item(r, n_map, vault_dir) for r in raw_results]
    if not dry_run and (pub_results or url_list_results):
        _append_journal_event(
            vault_dir,
            note_reg,
            vault_ref,
            str(pub_results[0].get("note_name") or "") if len(pub_results) == 1 else "",
            f"DropZone 多模态转译：已转译 {len(pub_results)} 项、URL清单 {len(url_list_results)} 份、拦截 {len(blocked)} 项",
        )
    doc_ticket: Optional[Dict[str, Any]] = None
    if not dry_run and (len(candidates) >= 2 or blocked or pre_ticket is not None):
        tinfo = pre_ticket or ensure_doc_ticket(
            ws_root,
            vault_dir,
            task_key=f"fmt-sweep:{vault_ref}:{project_ref or 'inbox'}",
            title=f"DropZone 批量转译（{len(candidates)} 项）",
            summary=f"批量 fmt sweep：成功 {len(pub_results)}、URL清单 {len(url_list_results)}、拦截 {len(blocked)}",
            ticket_id=ticket_id,
        )
        doc_ticket = update_doc_ticket_progress(
            ws_root,
            vault_dir,
            ticket_info=tinfo,
            note=f"fmt sweep：converted={len(pub_results)}, url_lists={len(url_list_results)}, blocked={len(blocked)}",
            mark_done=(len(blocked) == 0),
        )
    out_fmt: Dict[str, Any] = {
        "mode": "dropzone-sweep",
        "vault_ref": vault_ref,
        "dry_run": dry_run,
        "converted_count": 0 if dry_run else len(pub_results),
        "url_list_count": len(url_list_results),
        "blocked_count": len(blocked),
        "results": pub_results,
        "url_lists": url_list_results,
        "blocked": blocked,
    }
    if doc_ticket is not None:
        out_fmt["doc_ticket"] = {k: v for k, v in doc_ticket.items() if k != "ticket_rel_path"}
    return out_fmt


def run_url_ingest(
    *,
    vault_dir: Path,
    vault_ref: str,
    ws_root: Path,
    proj_reg: KekProjectRegistry,
    note_reg: KekNoteRegistry,
    note_ref: Optional[str] = None,
    file_name: Optional[str] = None,
    url: Optional[str] = None,
    urls: Optional[Any] = None,
    project_ref: Optional[str] = None,
    scope: str = "auto",
    by: Optional[str] = None,
    dry_run: bool = False,
    deep: bool = False,
    ticket_id: Optional[str] = None,
    target_zone: Optional[str] = None,
    subdir: Optional[str] = None,
    min_chars: Optional[int] = None,
    depth: Optional[int] = None,
    parent_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Dedicated `url-ingest` action for URLs and DropZone URL-list cards."""
    eff_depth = _validate_crawl_depth(depth)
    if url or urls:
        return run_fmt(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            ws_root=ws_root,
            proj_reg=proj_reg,
            note_reg=note_reg,
            project_ref=project_ref,
            scope=scope,
            url=url, prepared=active_preparation.get(),
            urls=urls,
            by=by,
            dry_run=dry_run,
            deep=deep,
            ticket_id=ticket_id,
            target_zone=target_zone,
            subdir=subdir,
            min_chars=min_chars,
            depth=eff_depth,
            parent_url=parent_url,
        )
    if note_ref:
        rec = note_reg.resolve_handle(vault_ref, note_ref)
        rel = str(rec["note_rel_path"])
        src = vault_dir / rel
        if not src.is_file() or not rel.startswith("00-DropZone/"):
            raise VaultStoreError("E_PARAM_INVALID", "URL 清单卡必须位于 00-DropZone", exit_code=1)
        domain = _infer_dropzone_project_ref(vault_dir, vault_ref, proj_reg, rel, project_ref, scope)
        fmt_py = _resolve_fmt_python()
        eff_min_chars = 300 if min_chars is None else max(0, int(min_chars))
        res = _ingest_url_list_file(
            fmt_py=fmt_py,
            src_file=src,
            vault_dir=vault_dir,
            ws_root=ws_root,
            vault_ref=vault_ref,
            domain=domain,
            by=by,
            dry_run=dry_run,
            deep=deep,
            annotate_images=False,
            note_reg=note_reg,
            ticket_id=ticket_id,
            target_zone=target_zone,
            subdir=subdir,
            min_chars=eff_min_chars,
            depth=eff_depth,
            parent_url=parent_url,
        )
        if not dry_run:
            _append_journal_event(vault_dir, note_reg, vault_ref, src.stem, f"URL 清单采集完成（{res['converted_count']} 篇）")
        return {"vault_ref": vault_ref, **res}

    if file_name:
        return run_fmt(
            vault_dir=vault_dir,
            vault_ref=vault_ref,
            ws_root=ws_root,
            proj_reg=proj_reg,
            note_reg=note_reg,
            project_ref=project_ref,
            scope=scope,
            file_name=file_name,
            by=by,
            dry_run=dry_run,
            deep=deep,
            ticket_id=ticket_id,
            target_zone=target_zone,
            subdir=subdir,
        )
    raise VaultStoreError("E_PARAM_MISSING", "url-ingest 需要 --url、--note_ref 或 --file_name", exit_code=1)


def import_attachment(*, ws_root: Path, vault_dir: Path, vault_ref: str, input_file: str) -> Dict[str, Any]:
    """Copy one explicitly selected public inputs/ attachment, never scan a host directory."""
    import stat
    import tempfile
    root = ws_root.resolve()
    rel = Path(input_file)
    if rel.is_absolute() or not rel.parts or rel.parts[0] != "inputs" or ".." in rel.parts:
        raise VaultStoreError("E_PARAM_INVALID", "input_file 必须为工作区 inputs/ 内明确指定的相对文件", exit_code=1)
    src = root / rel
    if any(p.is_symlink() for p in [src, *src.parents] if p != root.parent):
        raise VaultStoreError("E_PARAM_INVALID", "附件入口不接受符号链接", exit_code=1)
    if not src.is_file() or src.suffix.lower() not in MULTIMODAL_SWEEP_EXTS | {".md", ".markdown"}:
        raise VaultStoreError("E_PARAM_INVALID", "附件不存在或格式不受支持", exit_code=1)
    with src.open("rb") as stream:
        data = stream.read(50 * 1024 * 1024 + 1)
    if len(data) > 50 * 1024 * 1024:
        raise VaultStoreError("E_PARAM_INVALID", "单附件不能超过50MiB", exit_code=1)
    dz = vault_dir / "00-DropZone"
    if dz.is_symlink():
        raise VaultStoreError("E_HALT_SECURITY", "受管收件箱不能是符号链接", exit_code=2)
    dz.mkdir(parents=True, exist_ok=True)
    imported_name = src.stem + ".pdf" if data.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"%PDF-") else src.name
    dst = dz / imported_name
    digest = hashlib.sha256(data).hexdigest()
    if dst.exists() or dst.is_symlink():
        if not dst.is_symlink() and dst.is_file() and hashlib.sha256(dst.read_bytes()).hexdigest() == digest:
            return {"vault_ref": vault_ref, "file_name": imported_name, "sha256": digest, "bytes": len(data), "imported": False, "already_present": True, "path_sealed": True}
        raise VaultStoreError("E_PARAM_CONFLICT", "收件箱已有不同内容的同名附件；未覆盖", exit_code=1)
    fd, tmp = tempfile.mkstemp(prefix=".attachment-", dir=str(dz))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        # Exclusive hard link prevents a concurrent importer from overwriting a winner.
        try:
            os.link(tmp, dst)
        except FileExistsError as exc:
            raise VaultStoreError("E_PARAM_CONFLICT", "同名附件已由另一操作创建；未覆盖", exit_code=1) from exc
    finally:
        Path(tmp).unlink(missing_ok=True)
    return {"vault_ref": vault_ref, "file_name": imported_name, "sha256": digest, "bytes": len(data), "imported": True, "path_sealed": True}


def inbox_list(*, vault_dir: Path, vault_ref: str, offset: int = 0, top_k: int = 100) -> Dict[str, Any]:
    if offset < 0 or not 1 <= top_k <= 1000:
        raise VaultStoreError("E_PARAM_INVALID", "offset需非负，top_k范围1..1000", exit_code=1)
    root = vault_dir / "00-DropZone"
    if root.is_symlink():
        raise VaultStoreError("E_HALT_SECURITY", "受管收件箱不能是符号链接", exit_code=2)
    files = []
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith((".", "_")) and d != "91-Temp" and not (Path(base)/d).is_symlink())
        for name in sorted(names):
            path = Path(base)/name
            if name.startswith((".", "_")) or path.is_symlink() or not path.is_file():
                continue
            pdf = _is_pdf_file(path)
            files.append({"file_name": name, "format": "pdf" if pdf else path.suffix.lower().lstrip("."),
                          "extension_mismatch": pdf and path.suffix.lower() != ".pdf", "bytes": path.stat().st_size})
    counts = {}
    for item in files: counts[item["file_name"]] = counts.get(item["file_name"], 0) + 1
    for item in files: item["ambiguous_name"] = counts[item["file_name"]] > 1
    return {"vault_ref": vault_ref, "zone": "dropzone", "total": len(files), "offset": offset,
            "files": files[offset:offset+top_k], "next_offset": offset+top_k if offset+top_k < len(files) else None,
            "path_sealed": True}
