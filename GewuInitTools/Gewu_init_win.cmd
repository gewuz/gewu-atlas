@echo off
setlocal DisableDelayedExpansion

rem Gewu_init_win.cmd - Windows direct-copied / git-cloned workspace initializer & doctor.
rem Detects/downloads Python 3.9+ (x64/ARM64), repairs/creates .runtime\install-env\venv,
rem discovers Gewu domains, installs per-domain dependencies, syncs skills (symlink/junction),
rem normalizes gewu.cmd, hardens .runtime ACLs, and validates gateway capabilities.

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

set "DEFAULT_PY_VERSION=3.12.16"
set "DEFAULT_PY_TAG=20261110"
set "DEFAULT_PY_FILE_X64=cpython-%DEFAULT_PY_VERSION%%%2B%DEFAULT_PY_TAG%-x86_64-pc-windows-msvc-install_only.tar.gz"
set "DEFAULT_PY_SHA_X64=e23fe5b16926de1d81661b1a97352e89fb167549c55a6825796ccd0f32ec7f1b"
set "DEFAULT_PY_FILE_ARM64=cpython-%DEFAULT_PY_VERSION%%%2B%DEFAULT_PY_TAG%-aarch64-pc-windows-msvc-install_only.tar.gz"
set "DEFAULT_PY_SHA_ARM64=7122bf62a0cf601527ba62e19ca715d64db86b269765da23937e9ac7f968f39c"
set "DEFAULT_PY_MIRROR_1=https://registry.npmmirror.com/-/binary/python-build-standalone/%DEFAULT_PY_TAG%"
set "DEFAULT_PY_MIRROR_2=https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone/%DEFAULT_PY_TAG%"
set "DEFAULT_PY_MIRROR_3=https://github.com/astral-sh/python-build-standalone/releases/download/%DEFAULT_PY_TAG%"

set "WS_ARG="
set "EXPLICIT_PY="
set "PY_URL=%GEWU_PYTHON_URL%"
set "PY_TAR=%GEWU_PYTHON_TAR%"
set "PY_SHA=%GEWU_PYTHON_SHA256%"
set "FORCE_DL_PY=0"
set "CHECK_ONLY=0"
set "OFFLINE=0"
set "JSON_OUT=0"

:parse_args
if "%~1"=="" goto after_parse
if /I "%~1"=="-h" goto show_help
if /I "%~1"=="--help" goto show_help
if /I "%~1"=="--force-download-python" goto flag_force_py
if /I "%~1"=="--check-only" goto flag_check_only
if /I "%~1"=="--offline" goto flag_offline
if /I "%~1"=="--json" goto flag_json
if /I "%~1"=="--workspace" goto arg_ws
if /I "%~1"=="--python" goto arg_py
if /I "%~1"=="--python-url" goto arg_py_url
if /I "%~1"=="--python-tar" goto arg_py_tar
if /I "%~1"=="--python-sha256" goto arg_py_sha
shift
goto parse_args

:flag_force_py
set "FORCE_DL_PY=1"
shift
goto parse_args

:flag_check_only
set "CHECK_ONLY=1"
shift
goto parse_args

:flag_offline
set "OFFLINE=1"
shift
goto parse_args

:flag_json
set "JSON_OUT=1"
shift
goto parse_args

:arg_ws
if "%~2"=="" goto missing_arg
set "WS_ARG=%~2"
shift
shift
goto parse_args

:arg_py
if "%~2"=="" goto missing_arg
set "EXPLICIT_PY=%~2"
shift
shift
goto parse_args

:arg_py_url
if "%~2"=="" goto missing_arg
set "PY_URL=%~2"
shift
shift
goto parse_args

:arg_py_tar
if "%~2"=="" goto missing_arg
set "PY_TAR=%~2"
shift
shift
goto parse_args

:arg_py_sha
if "%~2"=="" goto missing_arg
set "PY_SHA=%~2"
shift
shift
goto parse_args

:missing_arg
echo Option %~1 requires an argument. 1>&2
exit /b 2

:show_help
echo Usage: GewuInitTools\Gewu_init_win.cmd [options]
echo.
echo Options:
echo   --workspace ^<dir^>         Target Gewu workspace root (default: auto-detect)
echo   --python ^<path^>           Explicit Python 3.9+ interpreter (with venv/ensurepip)
echo   --force-download-python   Download standalone CPython into .runtime\install-env\python
echo   --python-url ^<url^>        Override standalone CPython tarball URL
echo   --python-tar ^<path^>       Use a local standalone CPython .tar.gz archive
echo   --python-sha256 ^<hex^>     Expected SHA-256 for custom --python-url / --python-tar
echo   --offline                 Install Python packages offline (--no-index)
echo   --wheels-dir ^<dir^>        Local wheelhouse directory for pip --find-links
echo   --reinstall               Rebuild venv and reinstall domain dependencies
echo   --check-only              Inspect only; make zero modifications to workspace
echo   --skip-skills             Skip .agents\skills link synchronization
echo   --skip-git-check          Warn instead of failing if git is unavailable
echo   --skip-git-init           Do not run git init when .git is absent
echo   --json                    Emit machine-readable JSON receipt on stdout
echo   -h, --help                Show this help message
exit /b 0

:after_parse
set "WIN_ARCH=%PROCESSOR_ARCHITECTURE%"
if defined PROCESSOR_ARCHITEW6432 set "WIN_ARCH=%PROCESSOR_ARCHITEW6432%"
if /I "%WIN_ARCH%"=="ARM64" (
  set "HOST_ARCH=arm64"
) else (
  set "HOST_ARCH=x86_64"
)

rem Resolve workspace root in CMD so we know where .runtime\install-env\python lives
set "WS_ROOT="
if defined WS_ARG (
  pushd "%WS_ARG%" 2>nul
  if errorlevel 1 (
    echo Target workspace directory not found: "%WS_ARG%" 1>&2
    exit /b 2
  )
  set "WS_ROOT=%CD%"
  popd
  goto ws_resolved
)

if exist "%SCRIPT_DIR%\gewu" (
  set "WS_ROOT=%SCRIPT_DIR%"
  goto ws_resolved
)
for %%D in ("%SCRIPT_DIR%\..") do set "PARENT_DIR=%%~fD"
if exist "%PARENT_DIR%\gewu" (
  set "WS_ROOT=%PARENT_DIR%"
  goto ws_resolved
)
for /D %%G in ("%SCRIPT_DIR%\*") do (
  if exist "%%~fG\app.json" if exist "%%~fG\contracts\cli-manifest.json" set "WS_ROOT=%SCRIPT_DIR%"
)
if defined WS_ROOT goto ws_resolved
for /D %%G in ("%PARENT_DIR%\*") do (
  if exist "%%~fG\app.json" if exist "%%~fG\contracts\cli-manifest.json" set "WS_ROOT=%PARENT_DIR%"
)
if not defined WS_ROOT (
  echo Unable to detect Gewu workspace root. Pass --workspace ^<dir^>. 1>&2
  exit /b 2
)

:ws_resolved
echo Gewu Windows workspace check and init (%HOST_ARCH%) 1>&2
echo Stage 1/6: Checking workspace and local Python 3.9+ (venv/ensurepip) 1>&2

set "BASE_PY="
set "PY_SOURCE="
set "STANDALONE_PY=%WS_ROOT%\.runtime\install-env\python\python.exe"

if "%FORCE_DL_PY%"=="1" goto need_download_py

if defined EXPLICIT_PY (
  "%EXPLICIT_PY%" -I -X utf8 -B -c "import sys,venv,ensurepip,ssl;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>nul
  if errorlevel 1 (
    echo Specified Python does not satisfy 3.9+ with venv/ensurepip/ssl. 1>&2
    exit /b 2
  )
  set "BASE_PY=%EXPLICIT_PY%"
  set "PY_SOURCE=explicit-python"
  goto py_ready
)

if exist "%STANDALONE_PY%" (
  "%STANDALONE_PY%" -I -X utf8 -B -c "import sys,venv,ensurepip,ssl;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>nul
  if not errorlevel 1 (
    set "BASE_PY=%STANDALONE_PY%"
    set "PY_SOURCE=workspace-standalone-existing"
    goto py_ready
  )
)

py -3 -I -X utf8 -B -c "import sys,venv,ensurepip,ssl;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>nul
if not errorlevel 1 (
  for /f "usebackq delims=" %%P in (`py -3 -I -X utf8 -B -c "import sys;print(sys.executable)"`) do set "BASE_PY=%%P"
  set "PY_SOURCE=local-system"
  goto py_ready
)

python -I -X utf8 -B -c "import sys,venv,ensurepip,ssl;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>nul
if not errorlevel 1 (
  for /f "usebackq delims=" %%P in (`python -I -X utf8 -B -c "import sys;print(sys.executable)"`) do set "BASE_PY=%%P"
  set "PY_SOURCE=local-system"
  goto py_ready
)

python3 -I -X utf8 -B -c "import sys,venv,ensurepip,ssl;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>nul
if not errorlevel 1 (
  for /f "usebackq delims=" %%P in (`python3 -I -X utf8 -B -c "import sys;print(sys.executable)"`) do set "BASE_PY=%%P"
  set "PY_SOURCE=local-system"
  goto py_ready
)

:need_download_py
if "%CHECK_ONLY%"=="1" (
  echo Python 3.9+ with venv/ensurepip/ssl not found under --check-only mode. 1>&2
  exit /b 2
)

set "ARCHIVE_CLEANUP=0"
if defined PY_TAR (
  if not exist "%PY_TAR%" (
    echo Specified --python-tar does not exist: "%PY_TAR%" 1>&2
    exit /b 2
  )
  set "SRC_ARCHIVE=%PY_TAR%"
  set "EXPECTED_SHA=%PY_SHA%"
  goto verify_py_archive
)

if "%OFFLINE%"=="1" (
  echo Python 3.9+ not found and --offline is set; pass --python-tar or --python. 1>&2
  exit /b 2
)

set "DL_TMP=%TEMP%\gewu-py-dl-%RANDOM%%RANDOM%"
mkdir "%DL_TMP%" 2>nul
set "SRC_ARCHIVE=%DL_TMP%\cpython-standalone.tar.gz"
set "ARCHIVE_CLEANUP=1"
echo Local Python 3.9+ unavailable; downloading Windows (%HOST_ARCH%) standalone CPython %DEFAULT_PY_VERSION%... 1>&2

if defined PY_URL (
  set "EXPECTED_SHA=%PY_SHA%"
  set "DL_CAND_1=%PY_URL%"
  set "DL_CAND_2="
  set "DL_CAND_3="
) else (
  if "%HOST_ARCH%"=="arm64" (
    set "PY_FILE=%DEFAULT_PY_FILE_ARM64%"
    set "EXPECTED_SHA=%DEFAULT_PY_SHA_ARM64%"
  ) else (
    set "PY_FILE=%DEFAULT_PY_FILE_X64%"
    set "EXPECTED_SHA=%DEFAULT_PY_SHA_X64%"
  )
  set "DL_CAND_1=%DEFAULT_PY_MIRROR_1%/%PY_FILE%"
  set "DL_CAND_2=%DEFAULT_PY_MIRROR_2%/%PY_FILE%"
  set "DL_CAND_3=%DEFAULT_PY_MIRROR_3%/%PY_FILE%"
)

set "DL_OK=0"
for %%U in ("%DL_CAND_1%" "%DL_CAND_2%" "%DL_CAND_3%") do (
  if not "%%~U"=="" if "%DL_OK%"=="0" (
    set "DL_URL=%%~U"
    call :download_and_check_py
  )
)
if not "%DL_OK%"=="1" (
  rmdir /s /q "%DL_TMP%" 2>nul
  echo Failed to download standalone CPython archive from China mirrors and GitHub. 1>&2
  exit /b 2
)
goto after_verify_py_archive

:download_and_check_py
if exist "%SRC_ARCHIVE%" del /f /q "%SRC_ARCHIVE%" 2>nul
echo   * Trying mirror: %DL_URL% 1>&2
set "DL_STEP_OK=0"
where curl.exe >nul 2>nul
if not errorlevel 1 (
  curl.exe -fL --retry 2 --connect-timeout 15 -o "%SRC_ARCHIVE%" "%DL_URL%"
  if not errorlevel 1 set "DL_STEP_OK=1"
)
if "%DL_STEP_OK%"=="0" (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -UseBasicParsing -Uri $env:DL_URL -OutFile $env:SRC_ARCHIVE"
  if not errorlevel 1 set "DL_STEP_OK=1"
)
if "%DL_STEP_OK%"=="0" (
  echo   ! Mirror download failed, trying next mirror... 1>&2
  exit /b 0
)
if defined EXPECTED_SHA (
  set "ACTUAL_SHA="
  for /f "usebackq delims=" %%H in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "(Get-FileHash -LiteralPath $env:SRC_ARCHIVE -Algorithm SHA256).Hash.ToLowerInvariant()" 2^>nul`) do set "ACTUAL_SHA=%%H"
  if not defined ACTUAL_SHA (
    for /f "tokens=*" %%H in ('certutil -hashfile "%SRC_ARCHIVE%" SHA256 ^| findstr /R /I "^[0-9a-f][0-9a-f ]*$"') do set "ACTUAL_SHA=%%H"
  )
  if defined ACTUAL_SHA set "ACTUAL_SHA=%ACTUAL_SHA: =%"
  if /I not "%ACTUAL_SHA%"=="%EXPECTED_SHA%" (
    echo   ! Mirror archive SHA-256 mismatch (actual=%ACTUAL_SHA%), trying next mirror... 1>&2
    exit /b 0
  )
)
set "DL_OK=1"
exit /b 0

:verify_py_archive
if defined EXPECTED_SHA (
  set "ACTUAL_SHA="
  for /f "usebackq delims=" %%H in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "(Get-FileHash -LiteralPath $env:SRC_ARCHIVE -Algorithm SHA256).Hash.ToLowerInvariant()" 2^>nul`) do set "ACTUAL_SHA=%%H"
  if not defined ACTUAL_SHA (
    for /f "tokens=*" %%H in ('certutil -hashfile "%SRC_ARCHIVE%" SHA256 ^| findstr /R /I "^[0-9a-f][0-9a-f ]*$"') do set "ACTUAL_SHA=%%H"
  )
  if defined ACTUAL_SHA set "ACTUAL_SHA=%ACTUAL_SHA: =%"
  if /I not "%ACTUAL_SHA%"=="%EXPECTED_SHA%" (
    if "%ARCHIVE_CLEANUP%"=="1" rmdir /s /q "%DL_TMP%" 2>nul
    echo Standalone CPython SHA-256 mismatch: expected=%EXPECTED_SHA%, actual=%ACTUAL_SHA% 1>&2
    exit /b 2
  )
)

:after_verify_py_archive

where tar.exe >nul 2>nul
if errorlevel 1 (
  if "%ARCHIVE_CLEANUP%"=="1" rmdir /s /q "%DL_TMP%" 2>nul
  echo Windows built-in tar.exe is required to extract standalone CPython (.tar.gz). 1>&2
  exit /b 2
)

if not exist "%WS_ROOT%\.runtime\install-env" mkdir "%WS_ROOT%\.runtime\install-env"
set "PY_STAGE=%WS_ROOT%\.runtime\install-env\.python-stage-%RANDOM%%RANDOM%"
mkdir "%PY_STAGE%"
tar.exe -xzf "%SRC_ARCHIVE%" -C "%PY_STAGE%"
if errorlevel 1 (
  rmdir /s /q "%PY_STAGE%" 2>nul
  if "%ARCHIVE_CLEANUP%"=="1" rmdir /s /q "%DL_TMP%" 2>nul
  echo Failed to extract standalone CPython archive. 1>&2
  exit /b 2
)
if "%ARCHIVE_CLEANUP%"=="1" rmdir /s /q "%DL_TMP%" 2>nul

if exist "%PY_STAGE%\python\python.exe" (
  set "EXTRACTED_ROOT=%PY_STAGE%\python"
) else (
  set "EXTRACTED_ROOT=%PY_STAGE%"
)
if not exist "%EXTRACTED_ROOT%\python.exe" (
  rmdir /s /q "%PY_STAGE%" 2>nul
  echo Extracted CPython archive missing python.exe. 1>&2
  exit /b 2
)
"%EXTRACTED_ROOT%\python.exe" -I -X utf8 -B -c "import sys,venv,ensurepip,ssl;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>nul
if errorlevel 1 (
  rmdir /s /q "%PY_STAGE%" 2>nul
  echo Downloaded standalone CPython self-check failed. 1>&2
  exit /b 2
)

if exist "%WS_ROOT%\.runtime\install-env\python" rmdir /s /q "%WS_ROOT%\.runtime\install-env\python"
move "%EXTRACTED_ROOT%" "%WS_ROOT%\.runtime\install-env\python" >nul
rmdir /s /q "%PY_STAGE%" 2>nul
set "BASE_PY=%STANDALONE_PY%"
set "PY_SOURCE=workspace-standalone-downloaded"

:py_ready
for /f "usebackq delims=" %%V in (`"%BASE_PY%" -I -X utf8 -B -c "import sys;print('.'.join(map(str,sys.version_info[:3])))"`) do set "PY_VER=%%V"
echo [OK] Base Python ready: %PY_VER% (%PY_SOURCE%) 1>&2

set "GEWU_INIT_WS_ROOT=%WS_ROOT%"
set "GEWU_INIT_BASE_PY=%BASE_PY%"
set "GEWU_INIT_PY_SOURCE=%PY_SOURCE%"
set "GEWU_INIT_HOST_ARCH=%HOST_ARCH%"

"%BASE_PY%" -I -X utf8 -B -c "import pathlib,sys;exec(pathlib.Path(sys.argv[1]).read_text('utf-8').split('\n__GEWU_INIT_WIN_PY__\n',1)[1])" "%~f0" %*
exit /b %ERRORLEVEL%

__GEWU_INIT_WIN_PY__
import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

SKIP_SCAN_DIRS = frozenset(
    {"GewuTrack", "GewuInitTools", "Volumes", "demo", "build", "dist", "logs", "backups", "instances", "outputs"}
)


def notice(msg, kind="stage"):
    clean = "".join(c if c.isprintable() else " " for c in str(msg))
    print(clean, file=sys.stderr, flush=True)


def need(ok, msg):
    if not ok:
        raise ValueError(msg)


def pip_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("PIP_")}
    env["PIP_CONFIG_FILE"] = os.devnull
    return env


def is_link(path):
    path = Path(path)
    try:
        if path.is_symlink():
            return True
        return bool(
            getattr(path.lstat(), "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
    except (FileNotFoundError, NotADirectoryError):
        return False


def _current_user_sid():
    class _TOKEN_USER(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wt.HANDLE
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    advapi32.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
    advapi32.OpenProcessToken.restype = wt.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wt.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.POINTER(wt.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wt.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wt.BOOL

    token = wt.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken")
    try:
        needed = wt.DWORD(0)
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buf = (ctypes.c_ubyte * needed.value)()
        if not advapi32.GetTokenInformation(token, 1, buf, needed, ctypes.byref(needed)):
            raise OSError(ctypes.get_last_error(), "GetTokenInformation")
        user = _TOKEN_USER.from_buffer(buf)
        sid_str = wt.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(user.Sid, ctypes.byref(sid_str)):
            raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW")
        try:
            return sid_str.value
        finally:
            kernel32.LocalFree(sid_str)
    finally:
        kernel32.CloseHandle(token)


def harden_private_dir(path):
    if os.name != "nt":
        return "inherited-windows-acl"
    try:
        sid = _current_user_sid()
        proc = subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"*{sid}:(OI)(CI)F",
                "*S-1-5-18:(OI)(CI)F",
                "*S-1-5-32-544:(OI)(CI)F",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        return "hardened-windows-acl" if proc.returncode == 0 else "inherited-windows-acl"
    except (OSError, subprocess.SubprocessError):
        return "inherited-windows-acl"


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
        if not child.is_dir() or is_link(child) or child.name.startswith(".") or child.name in SKIP_SCAN_DIRS:
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
                need(not any(is_link(p) for p in [req_path, *req_path.parents]), f"禁止符号链接/重解析依赖文件：{req_path}")
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
                if fb.is_file() and not is_link(fb):
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
            [sys.executable, "-I", "-X", "utf8", "-B", str(gw), "list-domains"],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        need(probe.returncode == 0, f"根网关 gewu list-domains 自检失败：{(probe.stderr or probe.stdout).strip()}")

    chosen_tokens = {d[k].casefold() for d in domains for k in ("app_id", "cli_alias", "directory")}
    for d in domains:
        d["missing_peers"] = [p for p in d["peer_domains"] if p.casefold() not in chosen_tokens]
    return domains


def inspect_venv(venv_dir):
    py = venv_dir / "Scripts/python.exe"
    cfg = venv_dir / "pyvenv.cfg"
    if not (cfg.is_file() and py.is_file()):
        return False, "missing"
    probe = subprocess.run(
        [
            str(py),
            "-I",
            "-X",
            "utf8",
            "-B",
            "-c",
            "import json,sys; print(json.dumps({'venv': sys.prefix != sys.base_prefix, 'ver': list(sys.version_info[:3]), 'plat': sys.platform}))",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
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
    if info.get("plat") != "win32":
        return False, "wrong-platform"
    if tuple(info.get("ver", [0, 0])) < (3, 9):
        return False, "python-too-old"
    pip_chk = subprocess.run(
        [str(py), "-I", "-X", "utf8", "-B", "-m", "pip", "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=pip_env(),
        timeout=20,
    )
    if pip_chk.returncode != 0:
        return False, "pip-unavailable"
    return True, "ok"


def ensure_venv(root, base_py, check_only, reinstall):
    venv_dir = root / ".runtime/install-env/venv"
    py = venv_dir / "Scripts/python.exe"
    healthy, reason = inspect_venv(venv_dir)
    if healthy and not reinstall:
        perm_model = harden_private_dir(venv_dir.parent) if not check_only else "checked"
        return py, "reused", reason, perm_model

    need(not check_only, f"隔离环境未就绪或已损坏（{reason}）；请去掉 --check-only 重新运行")
    if venv_dir.exists() or is_link(venv_dir):
        notice(f"▶ 检测到旧/损坏隔离环境（{reason}），正在重建 .runtime\\install-env\\venv...")
        if is_link(venv_dir):
            venv_dir.unlink()
        else:
            shutil.rmtree(venv_dir)
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    harden_private_dir(root / ".runtime")
    perm_model = harden_private_dir(venv_dir.parent)
    proc = subprocess.run(
        [base_py, "-I", "-X", "utf8", "-B", "-m", "venv", str(venv_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    need(proc.returncode == 0 and py.is_file(), f"创建隔离 venv 失败：{(proc.stderr or proc.stdout).strip()}")
    ok_after, reason_after = inspect_venv(venv_dir)
    need(ok_after, f"新建隔离 venv 自检失败：{reason_after}")
    return py, ("rebuilt" if reason != "missing" else "created"), reason, perm_model


def check_domain_deps_and_imports(py, domains):
    chk = subprocess.run(
        [str(py), "-I", "-X", "utf8", "-B", "-m", "pip", "check"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
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
            encoding="utf-8",
            errors="replace",
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
                encoding="utf-8",
                errors="replace",
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


def install_domain_deps(py, root, packages, offline, wheels_dir, index_url):
    if not packages:
        return "no-packages-required"
    req_stage = root / ".runtime/install-env/domain-requirements.txt"
    req_stage.write_text("\n".join(packages) + "\n", encoding="utf-8")
    cmd = [str(py), "-I", "-X", "utf8", "-B", "-m", "pip", "install", "--disable-pip-version-check"]
    if offline:
        cmd += ["--no-index"]
        if wheels_dir:
            cmd += ["--find-links", str(wheels_dir)]
        cmd += ["-r", str(req_stage)]
        notice("▶ 正在安装各域依赖库（离线模式）...")
        proc = subprocess.run(
            cmd,
            env=pip_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
        need(proc.returncode == 0, "pip install 失败：" + (proc.stderr or proc.stdout)[-2000:])
        return "installed"

    mirrors = [index_url]
    fallback_idx = "https://pypi.tuna.tsinghua.edu.cn/simple"
    if not os.environ.get("GEWU_PIP_INDEX_URL") and fallback_idx not in mirrors:
        mirrors.append(fallback_idx)
    last_err = ""
    for idx_url in mirrors:
        run_cmd = list(cmd) + ["--index-url", idx_url]
        if wheels_dir:
            run_cmd += ["--find-links", str(wheels_dir)]
        run_cmd += ["-r", str(req_stage)]
        notice(f"▶ 正在安装各域依赖库（源：{idx_url}）...")
        proc = subprocess.run(
            run_cmd,
            env=pip_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
        if proc.returncode == 0:
            return "installed"
        last_err = (proc.stderr or proc.stdout)[-2000:]
        if idx_url != mirrors[-1]:
            notice(f"  ! 依赖源 {idx_url} 安装失败，自动切换备源重试...", "warn")
    need(False, "pip install 失败：" + last_err)
    return "installed"


def create_windows_skill_link(link, rel_target):
    try:
        link.symlink_to(rel_target, target_is_directory=True)
        return "symlink"
    except OSError as exc:
        abs_target = (link.parent / rel_target).resolve()
        if abs_target.is_dir() and not is_link(abs_target):
            proc = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(abs_target)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if proc.returncode == 0 and is_link(link):
                return "junction"
        raise ValueError(
            "Windows 技能目录链接创建失败；请开启开发者模式或授予符号链接/目录联接 (mklink /J) 权限。"
        ) from exc


def sync_skills(root, domains, check_only):
    discovery = root / ".agents/skills"
    if not check_only:
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
            abs_target = (root / d["directory"] / "skills" / sname).resolve()
            need(sname not in seen, f"技能名称冲突：{sname} 同时存在于 {seen.get(sname)} 与 {d['directory']}")
            seen[sname] = d["directory"]
            link = discovery / sname
            if is_link(link):
                if link.resolve() != abs_target:
                    need(not check_only, f"技能发现链接指向异常：{sname} -> {link.resolve()}（期望 {abs_target}）")
                    if link.is_symlink() or link.is_file():
                        link.unlink()
                    else:
                        link.rmdir()
                    create_windows_skill_link(link, rel_target)
                synced += 1
            elif link.exists():
                # Git on Windows without core.symlinks checks out POSIX symlinks as plain text files
                is_git_stub = False
                if link.is_file() and link.stat().st_size <= 256:
                    try:
                        if link.read_text("utf-8").strip() == rel_target.as_posix():
                            is_git_stub = True
                    except (OSError, UnicodeDecodeError):
                        pass
                if is_git_stub and not check_only:
                    link.unlink()
                    create_windows_skill_link(link, rel_target)
                    synced += 1
                else:
                    raise ValueError(f".agents\\skills\\{sname} 被非链接文件占用（如为 Git symlink 占位文件，请检查内容）")
            elif check_only:
                missing.append(sname)
            else:
                create_windows_skill_link(link, rel_target)
                synced += 1
    need(not missing, "缺少 .agents\\skills 链接：" + ", ".join(missing))
    return synced


def ensure_launchers(root, check_only):
    gw = root / "gewu"
    need(gw.is_file() and not is_link(gw), "缺少根目录网关脚本 gewu")
    cmd_launcher = root / "gewu.cmd"
    expected_bytes = (
        "@echo off\r\n"
        "setlocal DisableDelayedExpansion\r\n"
        'if exist "%~dp0.runtime\\install-env\\git\\cmd\\git.exe" set "PATH=%~dp0.runtime\\install-env\\git\\cmd;%PATH%"\r\n'
        '"%~dp0.runtime\\install-env\\venv\\Scripts\\python.exe" -I -X utf8 -B "%~dp0gewu" %*\r\n'
        "exit /b %ERRORLEVEL%\r\n"
    ).encode("ascii")
    if check_only:
        need(cmd_launcher.is_file(), "根目录 gewu.cmd 缺失")
        return "checked"
    if not cmd_launcher.is_file() or cmd_launcher.read_bytes() != expected_bytes:
        cmd_launcher.write_bytes(expected_bytes)
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
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        need(proc.returncode == 0, f"域 {d['cli_alias']} 能力校验失败：{(proc.stderr or proc.stdout).strip()}")
        cap = json.loads(proc.stdout)
        need(cap.get("ok") is True, f"域 {d['cli_alias']} capabilities 返回异常")
        results[d["cli_alias"]] = {"app_id": d["app_id"], "commands": len(cap.get("commands") or [])}
    return results


def maybe_write_receipt(root, host_arch, py_source, domains, dep_checks, deps_action, venv_action, perm_model, index_url, offline, check_only):
    receipt_file = root / "INSTALL_RECEIPT.json"
    should_write = not receipt_file.is_file()
    if not should_write:
        try:
            existing = json.loads(receipt_file.read_text("utf-8"))
            if existing.get("platform") != "win32":
                should_write = True
        except (OSError, json.JSONDecodeError):
            should_write = True
    if should_write and not check_only:
        receipt = {
            "schema": "forge.workspace-install/1",
            "status": "installed",
            "platform": "win32",
            "arch": host_arch,
            "permission_model": perm_model,
            "python_source": py_source,
            "venv_action": venv_action,
            "dependency_source": "offline-wheels" if offline else index_url,
            "dependencies_status": deps_action,
            "dependency_checks": dep_checks,
            "domains": [d["app_id"] for d in domains],
            "volumes_included": False,
            "keys_migrated": False,
            "volume_initialized": False,
            "capabilities": "passed",
            "business_acceptance": "not_run",
            "initialized_via": "GewuInitTools/Gewu_init_win.cmd",
        }
        receipt_file.write_text(
            json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return True
    return False


MINGIT_VERSION = "2.56.1"
MINGIT_TAG = "v2.56.1.windows.1"
MINGIT_SPECS = {
    "x86_64": ("MinGit-2.56.1-64-bit.zip", "4a92850b6e917da4fd010669b614b55dccf5b0ec333a534cc4be4700dfefdafd"),
    "arm64": ("MinGit-2.56.1-arm64.zip", "96571e442e90523a90e5fd8661b4c4e43bc192910f3c6db938816e6955b4cc8c"),
}
MINGIT_MIRRORS = (
    f"https://repo.huaweicloud.com/git-for-windows/{MINGIT_TAG}",
    f"https://registry.npmmirror.com/-/binary/git-for-windows/{MINGIT_TAG}",
    f"https://github.com/git-for-windows/git/releases/download/{MINGIT_TAG}",
)


def install_portable_mingit(root, host_arch):
    fname, expected_sha = MINGIT_SPECS.get(host_arch, MINGIT_SPECS["x86_64"])
    git_dir = root / ".runtime/install-env/git"
    git_exe = git_dir / "cmd/git.exe"
    notice(f"▶ 未检测到本机 Git，正在从国内镜像下载便携版 MinGit {MINGIT_VERSION} ({host_arch}) 到工作区...", "warn")
    with tempfile.TemporaryDirectory(prefix="gewu-git-dl-") as td:
        archive = Path(td) / fname
        downloaded = False
        for base in MINGIT_MIRRORS:
            url = f"{base}/{fname}"
            notice(f"  • 尝试镜像：{url}")
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "GewuInitTools/1.0"})
                with urllib.request.urlopen(req, timeout=30) as resp, archive.open("wb") as fh:
                    h = hashlib.sha256()
                    while True:
                        chunk = resp.read(262144)
                        if not chunk:
                            break
                        fh.write(chunk)
                        h.update(chunk)
                actual_sha = h.hexdigest().lower()
                if actual_sha != expected_sha:
                    notice(f"  ! 镜像文件 SHA-256 不匹配（actual={actual_sha}），切换下一个源...", "warn")
                    continue
                downloaded = True
                break
            except Exception as exc:
                notice(f"  ! 镜像下载失败（{exc}），切换下一个源...", "warn")
        need(downloaded, f"下载便携版 MinGit 失败（已尝试华为云、npmmirror 与 GitHub）；可手动安装 Git for Windows 或使用 --skip-git-check")

        install_env = root / ".runtime/install-env"
        install_env.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".git-stage-", dir=str(install_env)))
        try:
            stage_resolved = stage.resolve()
            with zipfile.ZipFile(archive, "r") as zf:
                for member in zf.infolist():
                    target = (stage / member.filename).resolve()
                    need(
                        target == stage_resolved or stage_resolved in target.parents,
                        f"MinGit 压缩包路径越界：{member.filename}",
                    )
                zf.extractall(stage)
            need((stage / "cmd/git.exe").is_file(), "MinGit 解压后缺少 cmd/git.exe")
            if git_dir.exists():
                shutil.rmtree(git_dir)
            stage.rename(git_dir)
        except Exception:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            raise
    return str(git_exe)


def check_git(root, host_arch, offline, skip_git_check, skip_git_init, check_only):
    candidates = []
    which_git = shutil.which("git")
    if which_git:
        candidates.append(which_git)
    portable_git = root / ".runtime/install-env/git/cmd/git.exe"
    if portable_git.is_file() and str(portable_git) not in candidates:
        candidates.append(str(portable_git))

    git_bin = None
    git_ver = None
    git_source = "local-system"
    for cand in candidates:
        proc = subprocess.run(
            [cand, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            git_bin = cand
            git_ver = proc.stdout.strip()
            git_source = "workspace-portable-mingit" if Path(cand).resolve() == portable_git.resolve() else "local-system"
            break

    if not git_bin and not offline and not check_only and not skip_git_check:
        try:
            git_bin = install_portable_mingit(root, host_arch)
            proc = subprocess.run(
                [git_bin, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                git_ver = proc.stdout.strip()
                git_source = "workspace-portable-mingit"
            else:
                git_bin = None
        except Exception as exc:
            notice(f"  ! 自动下载便携版 MinGit 失败：{exc}", "warn")
            git_bin = None

    if not git_bin:
        msg = "未检测到 Git（域变更引擎依赖 git）；请从国内镜像（https://repo.huaweicloud.com/git-for-windows/ 或 https://registry.npmmirror.com/-/binary/git-for-windows/）安装 Git for Windows 并确保 git 在 PATH 中"
        if skip_git_check:
            notice(f"  ! {msg}（已通过 --skip-git-check 忽略）", "warn")
            return {
                "available": False,
                "version": None,
                "source": None,
                "is_git_repo": (root / ".git").exists(),
                "repo_action": "skipped-no-git",
            }
        raise ValueError(msg)

    git_cmd_dir = str(Path(git_bin).resolve().parent)
    if git_source == "workspace-portable-mingit" and git_cmd_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = git_cmd_dir + os.pathsep + os.environ.get("PATH", "")

    has_git_dir = (root / ".git").exists()
    repo_action = "already-initialized"
    if not has_git_dir:
        if check_only:
            repo_action = "missing-check-only"
        elif skip_git_init:
            repo_action = "skipped"
        else:
            init_proc = subprocess.run(
                [git_bin, "-C", str(root), "init", "-b", "main"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
            )
            if init_proc.returncode != 0:
                init_proc = subprocess.run(
                    [git_bin, "-C", str(root), "init"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                )
            need(init_proc.returncode == 0 and (root / ".git").exists(), f"git init 失败：{(init_proc.stderr or init_proc.stdout).strip()}")
            has_git_dir = True
            repo_action = "initialized"
            notice("✓ 已执行 git init 初始化工作区仓库")

    info = {
        "available": True,
        "version": git_ver,
        "is_git_repo": has_git_dir,
        "repo_action": repo_action,
    }
    if info["is_git_repo"]:
        ac = subprocess.run(
            [git_bin, "-C", str(root), "config", "--get", "core.autocrlf"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if ac.returncode == 0 and ac.stdout.strip().lower() == "true":
            notice("  ! 检测到仓库 core.autocrlf=true，请确保 .gitattributes 固定 *.sh 为 LF", "warn")
    return info


def main():
    p = argparse.ArgumentParser(prog="Gewu_init_win.cmd")
    p.add_argument("--workspace", default=None)
    p.add_argument("--python", default=None)
    p.add_argument("--force-download-python", action="store_true")
    p.add_argument("--python-url", default=None)
    p.add_argument("--python-tar", default=None)
    p.add_argument("--python-sha256", default=None)
    p.add_argument("--offline", action="store_true")
    p.add_argument("--wheels-dir", type=Path, default=None)
    p.add_argument("--reinstall", action="store_true")
    p.add_argument("--check-only", action="store_true")
    p.add_argument("--skip-skills", action="store_true")
    p.add_argument("--skip-git-check", action="store_true")
    p.add_argument("--skip-git-init", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(sys.argv[2:])

    ws_root = Path(os.environ.get("GEWU_INIT_WS_ROOT") or args.workspace or ".").resolve()
    base_py = os.environ.get("GEWU_INIT_BASE_PY") or sys.executable
    py_source = os.environ.get("GEWU_INIT_PY_SOURCE") or "local-system"
    host_arch = os.environ.get("GEWU_INIT_HOST_ARCH") or "x86_64"
    wheels_dir = args.wheels_dir.resolve() if args.wheels_dir else None
    index_url = os.environ.get("GEWU_PIP_INDEX_URL", "https://mirrors.aliyun.com/pypi/simple/")
    machine = args.json or not sys.stdout.isatty()

    try:
        need(sys.platform == "win32", f"Gewu_init_win.cmd 仅支持 Windows（当前：{sys.platform}）")
        if wheels_dir is not None:
            need(wheels_dir.is_dir(), f"离线 wheels 目录不存在：{wheels_dir}")

        git_info = check_git(ws_root, host_arch, args.offline, args.skip_git_check, args.skip_git_init, args.check_only)
        if git_info["available"]:
            notice(f"✓ Git 就绪：{git_info['version']}")

        notice("▶ 阶段 2/6：扫描工作区格物域与依赖要求")
        domains = discover_domains(ws_root)
        py_ver_tuple = sys.version_info[:3]
        for d in domains:
            req_min = parse_version_tuple(d["python_minimum"])
            need(
                py_ver_tuple >= req_min,
                f"域 {d['directory']} 要求 Python >= {d['python_minimum']}，当前为 {'.'.join(map(str, py_ver_tuple))}",
            )
            if d["supported_platforms"] and "windows" not in d["supported_platforms"]:
                if "windows" in d["experimental_platforms"]:
                    notice(f"  ! 域 {d['directory']} 仅声明 windows 为实验性平台", "warn")
                else:
                    notice(f"  ! 域 {d['directory']} 未在 supported_platforms 声明 windows", "warn")
            if d["missing_peers"]:
                notice(f"  ! 域 {d['directory']} 声明了未在当前工作区出现的协作域：{', '.join(d['missing_peers'])}", "warn")
            notice(
                f"  • 发现域：{d['directory']} (alias={d['cli_alias']}, version={d['version']}, deps={len(d['packages'])}, imports={len(d['imports'])})"
            )

        agg_packages = sorted({pkg for d in domains for pkg in d["packages"]})
        notice(f"  • 汇总唯一依赖包：{len(agg_packages)} 项")

        notice("▶ 阶段 3/6：检测/准备隔离环境 (.runtime\\install-env\\venv)")
        venv_py, venv_action, venv_prev, perm_model = ensure_venv(ws_root, base_py, args.check_only, args.reinstall)
        notice(f"✓ 隔离环境状态：{venv_action} (previous={venv_prev}, acl={perm_model})")

        notice("▶ 阶段 4/6：核验并按域要求安装依赖库")
        deps_ok, deps_reason, dep_checks = check_domain_deps_and_imports(venv_py, domains)
        deps_action = "already-satisfied"
        if args.check_only:
            need(deps_ok, deps_reason)
        elif args.reinstall or not deps_ok or venv_action != "reused":
            if not deps_ok:
                notice(f"  • 需要安装/补齐依赖：{deps_reason}")
            install_action = install_domain_deps(venv_py, ws_root, agg_packages, args.offline, wheels_dir, index_url)
            deps_ok2, deps_reason2, dep_checks = check_domain_deps_and_imports(venv_py, domains)
            need(deps_ok2, deps_reason2)
            deps_action = install_action
        notice(f"✓ 依赖核验通过（{deps_action}）")

        notice("▶ 阶段 5/6：校准 gewu.cmd 与 .agents\\skills 发现链接")
        launcher_status = ensure_launchers(ws_root, args.check_only)
        skills_count = 0 if args.skip_skills else sync_skills(ws_root, domains, args.check_only)

        notice("▶ 阶段 6/6：校验工作区网关与各域能力")
        cap_results = validate_capabilities(venv_py, ws_root, domains)
        receipt_written = maybe_write_receipt(
            ws_root, host_arch, py_source, domains, dep_checks, deps_action, venv_action, perm_model, index_url, args.offline, args.check_only
        )

        summary = {
            "schema": "gewu.init-win/1",
            "status": "checked" if args.check_only else "ready",
            "platform": "windows",
            "arch": host_arch,
            "permission_model": perm_model,
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
        notice("✓ 工作区初始化完成（" + ", ".join(d["cli_alias"] for d in domains) + "）")
        if machine:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        notice(f"✗ 检查/初始化失败：{exc}")
        if machine:
            print(json.dumps({"schema": "gewu.init-win/1", "status": "failed", "reason": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
