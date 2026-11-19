#!/bin/sh
set -eu

# Gewu_init_linux.sh — Linux direct-copied / git-cloned workspace initializer & doctor.
# Detects/downloads Python 3.9+ (x86_64/aarch64 glibc), repairs/creates .runtime/install-env/venv,
# discovers Gewu domains, installs per-domain dependencies, syncs skills,
# normalizes launchers, and validates gateway capabilities.

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)

DEFAULT_PY_VERSION="3.12.16"
DEFAULT_PY_TAG="20261110"
DEFAULT_PY_URL_ARM64="https://github.com/astral-sh/python-build-standalone/releases/download/${DEFAULT_PY_TAG}/cpython-${DEFAULT_PY_VERSION}%2B${DEFAULT_PY_TAG}-aarch64-unknown-linux-gnu-install_only.tar.gz"
DEFAULT_PY_SHA_ARM64="4792f81750282597fb2a3389fe18b945ed2ba6aad5e45ad406d122c58cb33081"
DEFAULT_PY_URL_X64="https://github.com/astral-sh/python-build-standalone/releases/download/${DEFAULT_PY_TAG}/cpython-${DEFAULT_PY_VERSION}%2B${DEFAULT_PY_TAG}-x86_64-unknown-linux-gnu-install_only.tar.gz"
DEFAULT_PY_SHA_X64="c2f87d4b4e8db3abddb0f116c6ccfc503d0cbdbb9ee8faacddad1b679ec12e99"

WS_ARG=""
EXPLICIT_PY=""
PYTHON_URL="${GEWU_PYTHON_URL:-}"
PYTHON_TAR="${GEWU_PYTHON_TAR:-}"
PYTHON_SHA256="${GEWU_PYTHON_SHA256:-}"
FORCE_DOWNLOAD_PY=0
OFFLINE=0
WHEELS_DIR=""
REINSTALL=0
CHECK_ONLY=0
SKIP_SKILLS=0
SKIP_GIT_CHECK=0
SKIP_GIT_INIT=0
JSON_OUT=0

usage() {
  cat <<'EOF'
Usage: sh GewuInitTools/Gewu_init_linux.sh [options]

Options:
  --workspace <dir>         Target Gewu workspace root (default: auto-detect)
  --python <path>           Explicit Python 3.9+ interpreter (with venv/ensurepip)
  --force-download-python   Download standalone CPython into .runtime/install-env/python
  --python-url <url>        Override standalone CPython tarball URL
  --python-tar <path>       Use a local standalone CPython .tar.gz archive
  --python-sha256 <hex>     Expected SHA-256 for custom --python-url / --python-tar
  --offline                 Install Python packages offline (--no-index)
  --wheels-dir <dir>        Local wheelhouse directory for pip --find-links
  --reinstall               Rebuild venv and reinstall domain dependencies
  --check-only              Inspect only; make zero modifications to workspace
  --skip-skills             Skip .agents/skills symlink synchronization
  --skip-git-check          Warn instead of failing if git is unavailable
  --skip-git-init           Do not run git init when .git is absent
  --json                    Emit machine-readable JSON receipt on stdout
  -h, --help                Show this help message
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --workspace)
      [ "$#" -ge 2 ] || { printf '%s\n' "--workspace requires a directory path" >&2; exit 2; }
      WS_ARG="$2"; shift 2 ;;
    --workspace=*)
      WS_ARG="${1#*=}"; shift ;;
    --python)
      [ "$#" -ge 2 ] || { printf '%s\n' "--python requires an interpreter path" >&2; exit 2; }
      EXPLICIT_PY="$2"; shift 2 ;;
    --python=*)
      EXPLICIT_PY="${1#*=}"; shift ;;
    --force-download-python)
      FORCE_DOWNLOAD_PY=1; shift ;;
    --python-url)
      [ "$#" -ge 2 ] || { printf '%s\n' "--python-url requires a URL" >&2; exit 2; }
      PYTHON_URL="$2"; shift 2 ;;
    --python-url=*)
      PYTHON_URL="${1#*=}"; shift ;;
    --python-tar)
      [ "$#" -ge 2 ] || { printf '%s\n' "--python-tar requires a file path" >&2; exit 2; }
      PYTHON_TAR="$2"; shift 2 ;;
    --python-tar=*)
      PYTHON_TAR="${1#*=}"; shift ;;
    --python-sha256)
      [ "$#" -ge 2 ] || { printf '%s\n' "--python-sha256 requires a hex digest" >&2; exit 2; }
      PYTHON_SHA256="$2"; shift 2 ;;
    --python-sha256=*)
      PYTHON_SHA256="${1#*=}"; shift ;;
    --offline)
      OFFLINE=1; shift ;;
    --wheels-dir)
      [ "$#" -ge 2 ] || { printf '%s\n' "--wheels-dir requires a directory path" >&2; exit 2; }
      WHEELS_DIR="$2"; shift 2 ;;
    --wheels-dir=*)
      WHEELS_DIR="${1#*=}"; shift ;;
    --reinstall)
      REINSTALL=1; shift ;;
    --check-only)
      CHECK_ONLY=1; shift ;;
    --skip-skills)
      SKIP_SKILLS=1; shift ;;
    --skip-git-check)
      SKIP_GIT_CHECK=1; shift ;;
    --skip-git-init)
      SKIP_GIT_INIT=1; shift ;;
    --json)
      JSON_OUT=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2 ;;
  esac
done

use_color=0
if [ -t 2 ] && [ "${TERM:-}" != "dumb" ] && [ -z "${NO_COLOR:-}" ]; then
  use_color=1
fi

notice() {
  msg="$1"
  kind="${2:-stage}"
  if [ "$use_color" -eq 1 ]; then
    case "$kind" in
      title)   code="1;7" ;;
      stage)   code="1;36" ;;
      success) code="1;32" ;;
      warn)    code="33" ;;
      error)   code="1;31" ;;
      *)       code="0" ;;
    esac
    printf '\033[%sm%s\033[0m\n' "$code" "$msg" >&2
  else
    printf '%s\n' "$msg" >&2
  fi
}

fail_early() {
  reason="$1"
  notice "✗ 初始化失败：${reason}" "error"
  if [ "$JSON_OUT" -eq 1 ] || [ ! -t 1 ]; then
    esc=$(printf '%s' "$reason" | sed 's/\\/\\\\/g; s/"/\\"/g')
    printf '{"schema":"gewu.init-linux/1","status":"failed","reason":"%s"}\n' "$esc"
  fi
  exit 2
}

# 1. Host OS & Architecture check
HOST_OS=$(uname -s)
[ "$HOST_OS" = "Linux" ] || fail_early "Gewu_init_linux.sh 仅支持 Linux（当前系统：${HOST_OS}）"

RAW_ARCH=$(uname -m)
case "$RAW_ARCH" in
  x86_64|amd64) HOST_ARCH="x86_64" ;;
  aarch64|arm64) HOST_ARCH="aarch64" ;;
  *) fail_early "不支持的 Linux 架构：${RAW_ARCH}（支持 x86_64 / aarch64）" ;;
esac

# 2. Resolve Workspace Root
looks_like_workspace() {
  cand="$1"
  [ -d "$cand" ] || return 1
  if [ -f "$cand/gewu" ]; then
    return 0
  fi
  for sub in "$cand"/*; do
    [ -d "$sub" ] || continue
    if [ -f "$sub/app.json" ] && [ -f "$sub/contracts/cli-manifest.json" ]; then
      return 0
    fi
  done
  return 1
}

resolve_abs_dir() {
  (CDPATH= cd -- "$1" 2>/dev/null && pwd -P)
}

if [ -n "$WS_ARG" ]; then
  WS_ROOT=$(resolve_abs_dir "$WS_ARG") || fail_early "工作区目录不存在：${WS_ARG}"
else
  PARENT_DIR=$(resolve_abs_dir "$SCRIPT_DIR/..") || fail_early "无法解析父目录"
  if looks_like_workspace "$SCRIPT_DIR"; then
    WS_ROOT="$SCRIPT_DIR"
  elif looks_like_workspace "$PARENT_DIR"; then
    WS_ROOT="$PARENT_DIR"
  else
    fail_early "未在当前目录或上级目录发现格物工作区（缺少 gewu 或域清单），请通过 --workspace 指定"
  fi
fi

looks_like_workspace "$WS_ROOT" || fail_early "目标目录不是有效的格物工作区：${WS_ROOT}"

if [ -n "$WHEELS_DIR" ]; then
  WHEELS_DIR=$(resolve_abs_dir "$WHEELS_DIR") || fail_early "离线 wheels 目录不存在：${WHEELS_DIR}"
fi

notice " Gewu · Linux 工作区检测与初始化 (${HOST_ARCH}) " "title"
notice "▶ 阶段 1/6：检测工作区与本机 Python 3.9+（含 venv / ensurepip）"

# 3. Probe or download Python 3.9+
probe_python() {
  py="$1"
  [ -n "$py" ] || return 1
  [ -x "$py" ] || return 1
  "$py" -I -B -c "import sys, venv, ensurepip, ssl; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >/dev/null 2>&1
}

calc_sha256() {
  file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file" | awk '{print $1}'
  else
    return 1
  fi
}

download_file() {
  url="$1"
  dst="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 --connect-timeout 20 -o "$dst" "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -q --tries=3 --timeout=20 -O "$dst" "$url"
  else
    return 1
  fi
}

BASE_PY=""
PY_SOURCE=""
STANDALONE_PY_DIR="$WS_ROOT/.runtime/install-env/python"
STANDALONE_PY_BIN="$STANDALONE_PY_DIR/bin/python3"

if [ "$FORCE_DOWNLOAD_PY" -eq 0 ]; then
  if [ -n "$EXPLICIT_PY" ]; then
    probe_python "$EXPLICIT_PY" || fail_early "指定的 Python 不满足 3.9+（含 venv/ensurepip/ssl）：${EXPLICIT_PY}"
    BASE_PY="$EXPLICIT_PY"
    PY_SOURCE="explicit-python"
  elif probe_python "$STANDALONE_PY_BIN"; then
    BASE_PY="$STANDALONE_PY_BIN"
    PY_SOURCE="workspace-standalone-existing"
  else
    PATH_PY3=$(command -v python3 2>/dev/null || true)
    for cand in \
      "$PATH_PY3" \
      "/usr/local/bin/python3" \
      "/usr/bin/python3"
    do
      [ -e "$cand" ] || continue
      if probe_python "$cand"; then
        BASE_PY="$cand"
        PY_SOURCE="local-system"
        break
      fi
    done
  fi
fi

download_and_install_python() {
  if [ -n "$PYTHON_TAR" ]; then
    [ -f "$PYTHON_TAR" ] || fail_early "指定的 Python 安装包不存在：${PYTHON_TAR}"
    src_archive="$PYTHON_TAR"
    expected_sha="$PYTHON_SHA256"
    cleanup_archive=0
  else
    [ "$OFFLINE" -eq 0 ] || fail_early "未找到可用本机 Python 3.9+（或缺失 python3-venv），且处于 --offline 模式；请提供 --python-tar 或 --python"
    if [ -n "$PYTHON_URL" ]; then
      dl_url="$PYTHON_URL"
      expected_sha="$PYTHON_SHA256"
    elif [ "$HOST_ARCH" = "aarch64" ]; then
      dl_url="$DEFAULT_PY_URL_ARM64"
      expected_sha="$DEFAULT_PY_SHA_ARM64"
    else
      dl_url="$DEFAULT_PY_URL_X64"
      expected_sha="$DEFAULT_PY_SHA_X64"
    fi
    tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/gewu-py-dl.XXXXXX")
    src_archive="$tmp_dir/cpython-standalone.tar.gz"
    cleanup_archive=1
    notice "▶ 未检测到可用本机 Python 3.9+（含 venv），正在下载 Linux (${HOST_ARCH}) 独立 CPython ${DEFAULT_PY_VERSION} 到工作区..." "warn"
    if ! download_file "$dl_url" "$src_archive"; then
      rm -rf "$tmp_dir"
      fail_early "下载独立 CPython 失败（缺少 curl/wget 或网络受限；可尝试 --python-url / --python-tar，或安装系统 python3 + python3-venv）"
    fi
  fi

  if [ -n "$expected_sha" ]; then
    actual_sha=$(calc_sha256 "$src_archive") || {
      [ "$cleanup_archive" -eq 1 ] && rm -rf "$tmp_dir"
      fail_early "缺少 sha256sum / shasum，无法校验独立 CPython 归档"
    }
    if [ "$actual_sha" != "$expected_sha" ]; then
      [ "$cleanup_archive" -eq 1 ] && rm -rf "$tmp_dir"
      fail_early "独立 CPython 归档 SHA-256 校验不匹配（expected=${expected_sha}, actual=${actual_sha}）"
    fi
  fi

  mkdir -p "$WS_ROOT/.runtime/install-env"
  chmod 700 "$WS_ROOT/.runtime" "$WS_ROOT/.runtime/install-env" 2>/dev/null || true
  stage_dir=$(mktemp -d "$WS_ROOT/.runtime/install-env/.python-stage.XXXXXX")
  if ! tar -xzf "$src_archive" -C "$stage_dir"; then
    rm -rf "$stage_dir"
    [ "$cleanup_archive" -eq 1 ] && rm -rf "$tmp_dir"
    fail_early "解压独立 CPython 失败（需 tar 与 gzip）"
  fi
  [ "$cleanup_archive" -eq 1 ] && rm -rf "$tmp_dir"

  if [ -x "$stage_dir/python/bin/python3" ]; then
    extracted_root="$stage_dir/python"
  elif [ -x "$stage_dir/bin/python3" ]; then
    extracted_root="$stage_dir"
  else
    rm -rf "$stage_dir"
    fail_early "独立 CPython 归档结构异常（未找到 bin/python3）"
  fi

  if ! probe_python "$extracted_root/bin/python3"; then
    rm -rf "$stage_dir"
    fail_early "下载的独立 CPython 自检失败（若为 Alpine/musl 系统，请安装系统 python3 与 py3-pip/venv 或通过 --python 指定）"
  fi

  rm -rf "$STANDALONE_PY_DIR"
  mv "$extracted_root" "$STANDALONE_PY_DIR"
  rm -rf "$stage_dir"
  BASE_PY="$STANDALONE_PY_BIN"
  PY_SOURCE="workspace-standalone-downloaded"
}

if [ -z "$BASE_PY" ]; then
  if [ "$CHECK_ONLY" -eq 1 ]; then
    fail_early "未检测到满足要求的 Python 3.9+（含 venv/ensurepip/ssl），且当前为 --check-only 模式"
  fi
  download_and_install_python
fi

PY_VER=$("$BASE_PY" -I -B -c "import sys; print('.'.join(map(str, sys.version_info[:3])))")
notice "✓ 基础 Python 就绪：${PY_VER} (${PY_SOURCE})" "success"

# 4. Embedded Python phase
exec "$BASE_PY" -I -X utf8 -B - \
  "$WS_ROOT" "$BASE_PY" "$PY_SOURCE" "$HOST_ARCH" \
  "$CHECK_ONLY" "$OFFLINE" "$WHEELS_DIR" "$REINSTALL" "$SKIP_SKILLS" "$SKIP_GIT_CHECK" "$SKIP_GIT_INIT" "$JSON_OUT" \
  <<'GEWU_INIT_LINUX_PY'
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys

(
    ws_root_str,
    base_py,
    py_source,
    host_arch,
    check_only_s,
    offline_s,
    wheels_dir_s,
    reinstall_s,
    skip_skills_s,
    skip_git_s,
    skip_git_init_s,
    json_out_s,
) = sys.argv[1:13]

WS_ROOT = Path(ws_root_str).resolve()
CHECK_ONLY = check_only_s == "1"
OFFLINE = offline_s == "1"
WHEELS_DIR = Path(wheels_dir_s).resolve() if wheels_dir_s else None
REINSTALL = reinstall_s == "1"
SKIP_SKILLS = skip_skills_s == "1"
SKIP_GIT_CHECK = skip_git_s == "1"
SKIP_GIT_INIT = skip_git_init_s == "1"
JSON_OUT = json_out_s == "1"
PIP_INDEX_URL = os.environ.get("GEWU_PIP_INDEX_URL", "https://pypi.tuna.tsinghua.edu.cn/simple")

SKIP_SCAN_DIRS = frozenset(
    {"GewuTrack", "GewuInitTools", "Volumes", "demo", "build", "dist", "logs", "backups", "instances", "outputs"}
)


def notice(msg, kind="stage"):
    clean = "".join(c if c.isprintable() else " " for c in str(msg))
    colors = {"title": "1;7", "stage": "1;36", "success": "1;32", "warn": "33", "error": "1;31"}
    colored = sys.stderr.isatty() and os.environ.get("TERM") != "dumb" and "NO_COLOR" not in os.environ
    text = (f"\033[{colors.get(kind, '0')}m{clean}\033[0m") if colored else clean
    print(text, file=sys.stderr, flush=True)


def need(ok, msg):
    if not ok:
        raise ValueError(msg)


def pip_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("PIP_")}
    env["PIP_CONFIG_FILE"] = os.devnull
    return env


def parse_version_tuple(ver_str):
    parts = re.findall(r"\d+", str(ver_str))
    return tuple(int(p) for p in parts[:3]) if parts else (3, 9)


def validate_rel_path(rel):
    p = PurePosixPath(rel)
    need(
        isinstance(rel, str)
        and rel == str(p)
        and not p.is_absolute()
        and not any(part in ("..", ".", "") for part in p.parts)
        and "\\" not in rel
        and ":" not in rel,
        f"非法相对路径：{rel!r}",
    )
    return p


def discover_domains(root):
    domains = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.is_symlink() or child.name.startswith(".") or child.name in SKIP_SCAN_DIRS:
            continue
        app_f = child / "app.json"
        cm_f = child / "contracts/cli-manifest.json"
        if not (app_f.is_file() or cm_f.is_file()):
            continue

        app_meta = json.loads(app_f.read_text("utf-8")) if app_f.is_file() else {}
        cm_meta = json.loads(cm_f.read_text("utf-8")) if cm_f.is_file() else {}

        directory = cm_meta.get("directory") or app_meta.get("directory") or child.name
        need(directory == child.name, f"域目录声明不一致：{child.name} != {directory}")

        app_id = str(cm_meta.get("app_id") or app_meta.get("app_id") or child.name.lower())
        cli_alias = str(cm_meta.get("cli_alias") or child.name.lower())
        version = str(app_meta.get("version") or "0.0.0")
        dist_cfg = app_meta.get("distribution") if isinstance(app_meta.get("distribution"), dict) else {}

        supported_platforms = list(dist_cfg.get("supported_platforms") or [])
        experimental_platforms = list(dist_cfg.get("experimental_platforms") or [])
        python_minimum = str(dist_cfg.get("python_minimum") or "3.9")
        imports = list(dist_cfg.get("imports") or [])
        peer_domains = [str(p).strip() for p in (cm_meta.get("peer_domains") or []) if str(p).strip()]

        packages = []
        req_sources = []
        if "requirements" in dist_cfg:
            for item in dist_cfg.get("requirements") or []:
                rel = item["path"]
                validate_rel_path(rel)
                req_path = child / rel
                need(not any(p.is_symlink() for p in [req_path, *req_path.parents]), f"禁止符号链接依赖文件：{req_path}")
                need(req_path.is_file(), f"缺少域依赖声明文件：{child.name}/{rel}")
                fmt = item.get("format")
                raw = req_path.read_text("utf-8")
                if fmt == "json-packages":
                    packages.extend(json.loads(raw).get("packages") or [])
                elif fmt == "pip-requirements":
                    packages.extend(
                        line.strip()
                        for line in raw.splitlines()
                        if line.strip() and not line.lstrip().startswith("#")
                    )
                else:
                    raise ValueError(f"不支持的依赖格式 {fmt!r}（{child.name}/{rel}）")
                req_sources.append(f"{child.name}/{rel}")
        else:
            for fallback in ("requirements.txt", "references/requirements.txt"):
                fb = child / fallback
                if fb.is_file() and not fb.is_symlink():
                    packages.extend(
                        line.strip()
                        for line in fb.read_text("utf-8").splitlines()
                        if line.strip() and not line.lstrip().startswith("#")
                    )
                    req_sources.append(f"{child.name}/{fallback} (fallback)")
                    break

        for pkg in packages:
            need(
                isinstance(pkg, str)
                and "\n" not in pkg
                and "\r" not in pkg
                and not pkg.startswith("-")
                and re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*", pkg)
                and not any(x in pkg for x in ("@", "://", "\\")),
                f"域 {child.name} 存在不支持的包声明：{pkg!r}",
            )

        domains.append(
            {
                "directory": child.name,
                "app_id": app_id,
                "cli_alias": cli_alias,
                "version": version,
                "python_minimum": python_minimum,
                "supported_platforms": supported_platforms,
                "experimental_platforms": experimental_platforms,
                "packages": sorted(set(packages)),
                "requirement_sources": req_sources,
                "imports": imports,
                "peer_domains": peer_domains,
            }
        )

    need(bool(domains), f"工作区下未发现任何格物域（{root}）")
    gw = root / "gewu"
    if gw.is_file():
        probe = subprocess.run(
            [sys.executable, "-I", "-B", str(gw), "list-domains"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        need(probe.returncode == 0, f"根网关 gewu list-domains 自检失败：{(probe.stderr or probe.stdout).strip()}")

    chosen_tokens = {d[k].casefold() for d in domains for k in ("app_id", "cli_alias", "directory")}
    for d in domains:
        d["missing_peers"] = [p for p in d["peer_domains"] if p.casefold() not in chosen_tokens]
    return domains


def inspect_venv(venv_dir):
    py = venv_dir / "bin/python"
    cfg = venv_dir / "pyvenv.cfg"
    if not (cfg.is_file() and py.is_file() and os.access(py, os.X_OK)):
        return False, "missing"
    probe = subprocess.run(
        [
            str(py),
            "-I",
            "-B",
            "-c",
            "import json,platform,sys; print(json.dumps({'venv': sys.prefix != sys.base_prefix, 'ver': list(sys.version_info[:3]), 'plat': sys.platform}))",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if probe.returncode != 0:
        return False, "broken-python"
    try:
        info = json.loads(probe.stdout)
    except json.JSONDecodeError:
        return False, "broken-output"
    if not info.get("venv"):
        return False, "not-isolated"
    if not str(info.get("plat", "")).startswith("linux"):
        return False, "wrong-platform"
    if tuple(info.get("ver", [0, 0])) < (3, 9):
        return False, "python-too-old"
    pip_chk = subprocess.run(
        [str(py), "-I", "-B", "-m", "pip", "--version"],
        capture_output=True,
        text=True,
        env=pip_env(),
        timeout=20,
    )
    if pip_chk.returncode != 0:
        return False, "pip-unavailable"
    return True, "ok"


def ensure_venv(root):
    venv_dir = root / ".runtime/install-env/venv"
    py = venv_dir / "bin/python"
    healthy, reason = inspect_venv(venv_dir)
    if healthy and not REINSTALL:
        return py, "reused", reason

    need(not CHECK_ONLY, f"隔离环境未就绪或已损坏（{reason}）；请去掉 --check-only 重新运行")
    if venv_dir.exists() or venv_dir.is_symlink():
        notice(f"▶ 检测到旧/损坏隔离环境（{reason}），正在重建 .runtime/install-env/venv...", "warn")
        if venv_dir.is_symlink():
            venv_dir.unlink()
        else:
            shutil.rmtree(venv_dir)
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(root / ".runtime", 0o700)
    os.chmod(venv_dir.parent, 0o700)
    proc = subprocess.run(
        [base_py, "-I", "-X", "utf8", "-B", "-m", "venv", str(venv_dir)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    need(proc.returncode == 0 and py.is_file(), f"创建隔离 venv 失败：{(proc.stderr or proc.stdout).strip()}")
    ok_after, reason_after = inspect_venv(venv_dir)
    need(ok_after, f"新建隔离 venv 自检失败：{reason_after}")
    return py, ("rebuilt" if reason != "missing" else "created"), reason


def check_domain_deps_and_imports(py, domains):
    chk = subprocess.run(
        [str(py), "-I", "-X", "utf8", "-B", "-m", "pip", "check"],
        capture_output=True,
        text=True,
        env=pip_env(),
        timeout=60,
    )
    if chk.returncode != 0:
        return False, "pip check 失败：" + (chk.stdout + chk.stderr).strip(), {}

    all_pkgs = sorted({pkg for d in domains for pkg in d["packages"]})
    if all_pkgs:
        spec_script = r'''
import importlib.metadata as md, json, sys
pkgs = json.loads(sys.argv[1])
try:
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.version import Version
except Exception:
    print(json.dumps({"ok": False, "reason": "packaging helper unavailable"}))
    raise SystemExit(0)

missing = []
for raw in pkgs:
    req = Requirement(raw)
    if req.marker and not req.marker.evaluate():
        continue
    try:
        ver = Version(md.version(req.name))
    except md.PackageNotFoundError:
        missing.append(raw)
        continue
    if req.specifier and not req.specifier.contains(ver, prereleases=True):
        missing.append(f"{raw} (installed {ver})")
print(json.dumps({"ok": not missing, "missing": missing}))
'''
        sp = subprocess.run(
            [str(py), "-I", "-X", "utf8", "-B", "-c", spec_script, json.dumps(all_pkgs)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if sp.returncode != 0:
            return False, "依赖版本核验失败：" + (sp.stderr or sp.stdout).strip(), {}
        res = json.loads(sp.stdout)
        if not res.get("ok"):
            detail = ", ".join(res.get("missing") or []) or res.get("reason", "unknown")
            return False, "缺少或不满足版本的依赖包：" + detail, {}

    per_domain = {}
    for d in domains:
        mods = d["imports"]
        if mods:
            code = "import importlib,json,sys;[importlib.import_module(n) for n in json.loads(sys.argv[1])]"
            imp = subprocess.run(
                [str(py), "-I", "-X", "utf8", "-B", "-c", code, json.dumps(mods)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if imp.returncode != 0:
                return False, f"域 {d['directory']} 模块导入失败：{(imp.stderr or imp.stdout).strip()}", {}
        per_domain[d["app_id"]] = {
            "directory": d["directory"],
            "version": d["version"],
            "packages_count": len(d["packages"]),
            "imports": mods,
            "import_check": "passed" if mods else "no-third-party-imports-declared",
            "pip_check": "passed",
        }
    return True, "ok", per_domain


def install_domain_deps(py, root, packages):
    if not packages:
        return "no-packages-required"
    req_stage = root / ".runtime/install-env/domain-requirements.txt"
    req_stage.write_text("\n".join(packages) + "\n", encoding="utf-8")
    cmd = [str(py), "-I", "-X", "utf8", "-B", "-m", "pip", "install", "--disable-pip-version-check"]
    if OFFLINE:
        cmd += ["--no-index"]
    else:
        cmd += ["--index-url", PIP_INDEX_URL]
    if WHEELS_DIR:
        cmd += ["--find-links", str(WHEELS_DIR)]
    cmd += ["-r", str(req_stage)]
    notice("▶ 正在安装各域依赖库（离线模式）..." if OFFLINE else f"▶ 正在安装各域依赖库（源：{PIP_INDEX_URL}）...")
    proc = subprocess.run(cmd, env=pip_env(), capture_output=True, text=True, timeout=900)
    need(proc.returncode == 0, "pip install 失败：" + (proc.stderr or proc.stdout)[-2000:])
    return "installed"


def sync_skills(root, domains):
    discovery = root / ".agents/skills"
    if not CHECK_ONLY:
        discovery.mkdir(parents=True, exist_ok=True)
    seen = {}
    missing = []
    synced = 0
    for d in domains:
        skills_dir = root / d["directory"] / "skills"
        if not skills_dir.is_dir():
            continue
        for sk_md in sorted(skills_dir.glob("*/SKILL.md")):
            sname = sk_md.parent.name
            rel_target = Path("../..") / d["directory"] / "skills" / sname
            need(sname not in seen, f"技能名称冲突：{sname} 同时存在于 {seen.get(sname)} 与 {d['directory']}")
            seen[sname] = d["directory"]
            link = discovery / sname
            if link.is_symlink():
                if os.readlink(link) != str(rel_target):
                    need(not CHECK_ONLY, f"技能发现链接指向异常：{sname} -> {os.readlink(link)}（期望 {rel_target}）")
                    link.unlink()
                    link.symlink_to(rel_target, target_is_directory=True)
                synced += 1
            elif link.exists():
                is_git_stub = False
                if link.is_file() and link.stat().st_size <= 256:
                    try:
                        if link.read_text("utf-8").strip() == rel_target.as_posix():
                            is_git_stub = True
                    except (OSError, UnicodeDecodeError):
                        pass
                if is_git_stub and not CHECK_ONLY:
                    link.unlink()
                    link.symlink_to(rel_target, target_is_directory=True)
                    synced += 1
                else:
                    raise ValueError(f".agents/skills/{sname} 被非符号链接文件占用")
            elif CHECK_ONLY:
                missing.append(sname)
            else:
                link.symlink_to(rel_target, target_is_directory=True)
                synced += 1
    need(not missing, "缺少 .agents/skills 链接：" + ", ".join(missing))
    return synced


def ensure_launchers(root):
    gw = root / "gewu"
    need(gw.is_file() and not gw.is_symlink(), "缺少根目录网关脚本 gewu")
    cmd_launcher = root / "gewu.command"
    expected_launcher = (
        "#!/bin/sh\n"
        "set -eu\n"
        'ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        'exec "$ROOT/.runtime/install-env/venv/bin/python" -I -B "$ROOT/gewu" "$@"\n'
    )
    if CHECK_ONLY:
        need(os.access(gw, os.X_OK), "根目录 gewu 缺少可执行权限 (+x)")
        need(cmd_launcher.is_file() and os.access(cmd_launcher, os.X_OK), "根目录 gewu.command 缺失或缺少可执行权限")
        return "checked"

    if not os.access(gw, os.X_OK):
        os.chmod(gw, 0o755)
    write_launcher = not cmd_launcher.is_file()
    if not write_launcher:
        raw = cmd_launcher.read_bytes()
        if b"\r" in raw or raw.decode("utf-8", errors="replace") != expected_launcher:
            write_launcher = True
    if write_launcher:
        cmd_launcher.write_bytes(expected_launcher.encode("utf-8"))
    os.chmod(cmd_launcher, 0o755)
    return "normalized"


def validate_capabilities(py, root, domains):
    gw = root / "gewu"
    results = {}
    for d in domains:
        proc = subprocess.run(
            [str(py), "-I", "-X", "utf8", "-B", str(gw), d["cli_alias"], "capabilities"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        need(proc.returncode == 0, f"域 {d['cli_alias']} 能力校验失败：{(proc.stderr or proc.stdout).strip()}")
        cap = json.loads(proc.stdout)
        need(cap.get("ok") is True, f"域 {d['cli_alias']} capabilities 返回异常")
        results[d["cli_alias"]] = {"app_id": d["app_id"], "commands": len(cap.get("commands") or [])}
    return results


def maybe_write_receipt(root, domains, dep_checks, deps_action, venv_action):
    receipt_file = root / "INSTALL_RECEIPT.json"
    should_write = not receipt_file.is_file()
    if not should_write:
        try:
            existing = json.loads(receipt_file.read_text("utf-8"))
            if existing.get("platform") != "linux":
                should_write = True
        except (OSError, json.JSONDecodeError):
            should_write = True
    if should_write and not CHECK_ONLY:
        receipt = {
            "schema": "forge.workspace-install/1",
            "status": "installed",
            "platform": "linux",
            "arch": host_arch,
            "python_source": py_source,
            "venv_action": venv_action,
            "dependency_source": "offline-wheels" if OFFLINE else PIP_INDEX_URL,
            "dependencies_status": deps_action,
            "dependency_checks": dep_checks,
            "domains": [d["app_id"] for d in domains],
            "volumes_included": False,
            "keys_migrated": False,
            "volume_initialized": False,
            "capabilities": "passed",
            "business_acceptance": "not_run",
            "initialized_via": "GewuInitTools/Gewu_init_linux.sh",
        }
        receipt_file.write_text(
            json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return True
    return False


def check_git(root):
    git_bin = shutil.which("git")
    git_ver = None
    if git_bin:
        proc = subprocess.run([git_bin, "--version"], capture_output=True, text=True, timeout=10)
        if proc.returncode == 0 and proc.stdout.strip():
            git_ver = proc.stdout.strip()
        else:
            git_bin = None
    if not git_bin:
        msg = "未检测到 Git（GewuTrack / 域变更引擎依赖 git）；Ubuntu/Debian 请执行 sudo apt-get install -y git，RHEL/Fedora 请执行 sudo dnf install -y git"
        if SKIP_GIT_CHECK:
            notice(f"  ! {msg}（已通过 --skip-git-check 忽略）", "warn")
            return {
                "available": False,
                "version": None,
                "is_git_repo": (root / ".git").exists(),
                "repo_action": "skipped-no-git",
            }
        raise ValueError(msg)

    has_git_dir = (root / ".git").exists()
    repo_action = "already-initialized"
    if not has_git_dir:
        if CHECK_ONLY:
            repo_action = "missing-check-only"
        elif SKIP_GIT_INIT:
            repo_action = "skipped"
        else:
            init_proc = subprocess.run([git_bin, "-C", str(root), "init", "-b", "main"], capture_output=True, text=True, timeout=20)
            if init_proc.returncode != 0:
                init_proc = subprocess.run([git_bin, "-C", str(root), "init"], capture_output=True, text=True, timeout=20)
            need(init_proc.returncode == 0 and (root / ".git").exists(), f"git init 失败：{(init_proc.stderr or init_proc.stdout).strip()}")
            has_git_dir = True
            repo_action = "initialized"
            notice("✓ 已执行 git init 初始化工作区仓库", "success")

    info = {
        "available": True,
        "version": git_ver,
        "is_git_repo": has_git_dir,
        "repo_action": repo_action,
    }
    if info["is_git_repo"]:
        ac = subprocess.run([git_bin, "-C", str(root), "config", "--get", "core.autocrlf"], capture_output=True, text=True, timeout=10)
        if ac.returncode == 0 and ac.stdout.strip().lower() == "true":
            notice("  ! 检测到仓库 core.autocrlf=true，建议 Linux 保持 input 或 false 以免脚本出现 CRLF", "warn")
    return info


def main():
    try:
        git_info = check_git(WS_ROOT)
        if git_info["available"]:
            notice(f"✓ Git 就绪：{git_info['version']}", "success")
        notice("▶ 阶段 2/6：扫描工作区格物域与依赖要求")
        domains = discover_domains(WS_ROOT)
        py_ver_tuple = sys.version_info[:3]
        for d in domains:
            req_min = parse_version_tuple(d["python_minimum"])
            need(
                py_ver_tuple >= req_min,
                f"域 {d['directory']} 要求 Python >= {d['python_minimum']}，当前为 {'.'.join(map(str, py_ver_tuple))}",
            )
            if d["supported_platforms"] and "linux" not in d["supported_platforms"]:
                if "linux" in d["experimental_platforms"]:
                    notice(f"  ! 域 {d['directory']} 仅声明 linux 为实验性平台", "warn")
                else:
                    notice(f"  ! 域 {d['directory']} 未在 supported_platforms 声明 linux", "warn")
            if d["missing_peers"]:
                notice(f"  ! 域 {d['directory']} 声明了未在当前工作区出现的协作域：{', '.join(d['missing_peers'])}", "warn")
            notice(
                f"  • 发现域：{d['directory']} (alias={d['cli_alias']}, version={d['version']}, deps={len(d['packages'])}, imports={len(d['imports'])})"
            )

        agg_packages = sorted({pkg for d in domains for pkg in d["packages"]})
        notice(f"  • 汇总唯一依赖包：{len(agg_packages)} 项")

        notice("▶ 阶段 3/6：检测/准备隔离环境 (.runtime/install-env/venv)")
        venv_py, venv_action, venv_prev = ensure_venv(WS_ROOT)
        notice(f"✓ 隔离环境状态：{venv_action} (previous={venv_prev})", "success")

        notice("▶ 阶段 4/6：核验并按域要求安装依赖库")
        deps_ok, deps_reason, dep_checks = check_domain_deps_and_imports(venv_py, domains)
        deps_action = "already-satisfied"
        if CHECK_ONLY:
            need(deps_ok, deps_reason)
        elif REINSTALL or not deps_ok or venv_action != "reused":
            if not deps_ok:
                notice(f"  • 需要安装/补齐依赖：{deps_reason}", "warn")
            install_action = install_domain_deps(venv_py, WS_ROOT, agg_packages)
            deps_ok2, deps_reason2, dep_checks = check_domain_deps_and_imports(venv_py, domains)
            need(deps_ok2, deps_reason2)
            deps_action = install_action
        notice(f"✓ 依赖核验通过（{deps_action}）", "success")

        notice("▶ 阶段 5/6：校准入口权限与 .agents/skills 发现链接")
        launcher_status = ensure_launchers(WS_ROOT)
        skills_count = 0 if SKIP_SKILLS else sync_skills(WS_ROOT, domains)

        notice("▶ 阶段 6/6：校验工作区网关与各域能力")
        cap_results = validate_capabilities(venv_py, WS_ROOT, domains)
        receipt_written = maybe_write_receipt(WS_ROOT, domains, dep_checks, deps_action, venv_action)

        summary = {
            "schema": "gewu.init-linux/1",
            "status": "checked" if CHECK_ONLY else "ready",
            "platform": "linux",
            "arch": host_arch,
            "python_source": py_source,
            "python_version": ".".join(map(str, py_ver_tuple)),
            "git": git_info,
            "venv_action": venv_action,
            "dependencies_status": deps_action,
            "aggregated_packages": agg_packages,
            "domains": [
                {
                    "directory": d["directory"],
                    "app_id": d["app_id"],
                    "cli_alias": d["cli_alias"],
                    "version": d["version"],
                    "requirement_sources": d["requirement_sources"],
                    "packages": d["packages"],
                    "missing_peers": d["missing_peers"],
                }
                for d in domains
            ],
            "dependency_checks": dep_checks,
            "launchers": launcher_status,
            "skills_synced": skills_count,
            "capabilities": cap_results,
            "receipt_updated": receipt_written,
        }
        notice(
            "✓ 工作区初始化完成（" + ", ".join(d["cli_alias"] for d in domains) + "）",
            "success",
        )
        if JSON_OUT or not sys.stdout.isatty():
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        notice(f"✗ 检查/初始化失败：{exc}", "error")
        if JSON_OUT or not sys.stdout.isatty():
            print(
                json.dumps(
                    {"schema": "gewu.init-linux/1", "status": "failed", "reason": str(exc)},
                    ensure_ascii=False,
                )
            )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
GEWU_INIT_LINUX_PY
