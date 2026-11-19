"""Self-contained scenario test runner injected into every GewuForge App.

Context-Boundary Discipline:
- Never embeds hidden core module/class/key identifiers or raw DB strings.
- Passes test business payloads via stdin or :memory: isolation inside worker, never CLI argv.
- Swallows raw subprocess tracebacks into .local/logs/ and returns sanitized status/codes to stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

APP_DIR = Path(__file__).resolve().parents[2]
ROOT_DIR = APP_DIR.parent

_LEAK_TOKENS = (
    "SQLite " + "format 3",
    "enc:" + "v1:",
    "PRAGMA " + "key",
    "vault." + "enc",
)
_GIT_LEAK_RE = re.compile(
    r"(^|/)(?:\.local/.*|.*\.enc\.db(?:-.*)?|vault\.enc|\.vault_kek|kek\.key|.*\.key|.*\.pem|\.env(?:\.[^/]+)?)$"
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text("utf-8"))


def _log_private(tag: str, raw_text: str) -> None:
    if not raw_text.strip():
        return
    try:
        log_dir = APP_DIR / ".local/logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(log_dir, 0o700)
        log_file = log_dir / "app-test-diagnostics.log"
        fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(f"=== {tag} ===\n{raw_text}\n")
        os.chmod(log_file, 0o600)
    except OSError:
        pass


def _run_py(cmd: List[str], *, cwd: Path, env: Dict[str, str] | None = None, stdin_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-B", *cmd],
        cwd=str(cwd),
        env=env,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _check_rail_and_leaks(stage: str, errors: List[str], warnings_disclose: List[str], ignored_staged: List[str]) -> Dict[str, Any]:
    checks: Dict[str, Any] = {}
    agents = APP_DIR / "AGENTS.md"
    if not agents.is_file():
        errors.append("E_RAIL_MISSING_AGENTS")
    else:
        size = agents.stat().st_size
        checks["agents_bytes"] = size
        if size > 8192:
            errors.append(f"E_RAIL_AGENTS_TOO_LARGE:{size}")
        atxt = agents.read_text("utf-8", errors="replace")
        for tok in _LEAK_TOKENS:
            if tok in atxt:
                errors.append("E_SEC_LEAK_IN_AGENTS")
        if "./gewu" not in atxt:
            errors.append("E_RAIL_AGENTS_MISSING_GATEWAY")
        checks["agents_gateway_entry"] = "passed" if "./gewu" in atxt else "missing"

    for req in (
        "gewu",
        "rules/rule-app-contract-rail.md",
        "rules/rule-testing-strategy.md",
        "contracts/cli-manifest.json",
        "contracts/execution-routing-v1.md",
        "contracts/commit-message-v1.md",
        "contracts/testing-profile.json",
        "contracts/data-layout-profile.json",
        "scripts/distribution/app_backup.py",
        "scripts/manage/app_manage.py",
    ):
        if not (APP_DIR / req).is_file():
            errors.append(f"E_RAIL_MISSING_CONTRACT:{req}")

    gw_path = APP_DIR / "gewu"
    if gw_path.exists() and (gw_path.is_symlink() or (sys.platform != "win32" and (gw_path.stat().st_mode & 0o111) == 0)):
        errors.append("E_RAIL_GATEWAY_NOT_EXECUTABLE")
    cm_path = APP_DIR / "contracts/cli-manifest.json"
    if cm_path.is_file():
        try:
            cm_data = _load_json(cm_path)
            if cm_data.get("schema") != "gewu.cli-manifest/1" or not isinstance(cm_data.get("routes"), dict):
                errors.append("E_RAIL_CLI_MANIFEST_INVALID")
            else:
                app_res = APP_DIR.resolve()
                for rname, rinfo in cm_data.get("routes", {}).items():
                    rel = str(rinfo.get("script", "")) if isinstance(rinfo, dict) else ""
                    rp = APP_DIR / rel
                    if not rel or rp.is_symlink() or not rp.is_file() or app_res not in rp.resolve().parents:
                        errors.append(f"E_RAIL_CLI_MANIFEST_ROUTE_MISSING:{rname}")
                delegate = cm_data.get("facade_delegate")
                if delegate is not None:
                    dp = APP_DIR / str(delegate)
                    if not isinstance(delegate, str) or not delegate or dp.is_symlink() or not dp.is_file() or app_res not in dp.resolve().parents:
                        errors.append("E_RAIL_CLI_MANIFEST_DELEGATE_MISSING")
                checks["cli_manifest_routes"] = "passed" if not any(e.startswith("E_RAIL_CLI_MANIFEST") for e in errors) else "failed"
        except Exception:  # noqa: BLE001
            errors.append("E_RAIL_CLI_MANIFEST_INVALID")

    for hidden_rel in ("app_core", "scripts/app_core", ".local"):
        hdir = APP_DIR / hidden_rel
        if hdir.is_dir():
            for item in hdir.rglob("*"):
                if item.is_file() and item.name.lower().startswith("readme"):
                    errors.append(f"E_ELEPHANT_PARADOX:{hidden_rel}/{item.name}")

    skills = sorted((APP_DIR / "skills").glob("*/SKILL.md")) if (APP_DIR / "skills").is_dir() else []
    if not skills:
        if stage == "drafting":
            ignored_staged.append("no_skills_yet: no skills/*/SKILL.md found (allowed in staging)")
        else:
            errors.append("E_RAIL_MISSING_SKILL")
    skills_ok = 0
    for sf in skills:
        stxt = sf.read_text("utf-8", errors="replace")
        sname = sf.parent.name
        m = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", stxt, re.S)
        if not m or f"name: {sname}" not in m.group(1) or "description:" not in m.group(1) or not stxt[m.end():].strip():
            errors.append(f"E_SKILL_INVALID_FRONTMATTER:{sname}")
            continue
        for tok in _LEAK_TOKENS:
            if tok in stxt:
                errors.append(f"E_SEC_LEAK_IN_SKILL:{sname}")
        if "--" not in stxt and "bootstrap.py" not in stxt and "app_facade.py" not in stxt and "./gewu" not in stxt:
            errors.append(f"E_SKILL_NO_CLI_TEMPLATE:{sname}")
        else:
            skills_ok += 1
    checks["skills_validated"] = f"{skills_ok}/{len(skills)}"

    sec_file = APP_DIR / "contracts/app-security-profile.json"
    if sec_file.is_file():
        sec = _load_json(sec_file)
        tier = sec.get("security_tier", "plaintext-py")
        checks["security_tier"] = tier
        if tier in ("binary-hybrid", "binary-direct"):
            wb = APP_DIR / "scripts/app_core/layer2/whitebox_exec.py"
            if not wb.is_file():
                errors.append("E_LAYER2_MISSING_WHITEBOX")
            elif _sha256(wb.read_bytes()) != sec.get("whitebox_sha256"):
                errors.append("E_LAYER2_WHITEBOX_DIGEST_MISMATCH")
            l2_bin = APP_DIR / ".local/bin/gewu_vault_exec"
            if l2_bin.exists():
                p = subprocess.run([str(l2_bin), "--self-test"], capture_output=True, text=True, timeout=10)
                if p.returncode != 0 or '"ok":true' not in p.stdout:
                    _log_private("layer2_selftest", p.stdout + "\n" + p.stderr)
                    errors.append("E_LAYER2_BINARY_SELFTEST_FAILED")
                else:
                    checks["layer2_binary"] = "self_test_passed"
                    warnings_disclose.append(
                        "layer2_cross_uid_not_verified_on_single_user: binary self-test verified locally; cross-UID isolation unverified"
                    )
            else:
                checks["layer2_binary"] = "not_compiled_yet"
                msg = "layer2_binary_not_compiled_yet: .local/bin/gewu_vault_exec not compiled yet"
                if stage == "drafting":
                    ignored_staged.append(msg)
                else:
                    warnings_disclose.append(msg)

    in_git = subprocess.run(
        ["git", "-C", str(APP_DIR), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if in_git.returncode != 0 or in_git.stdout.strip() != "true":
        checks["git_history_zero_trace"] = "not_a_git_repo"
        warnings_disclose.append("git_not_initialized: App is not inside a git work tree; Git history zero-trace unverified")
    else:
        hist = subprocess.run(
            ["git", "-C", str(APP_DIR), "log", "--all", "--name-only", "--pretty=format:"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        leaked = [l.strip() for l in hist.stdout.splitlines() if l.strip() and _GIT_LEAK_RE.search(l.strip())]
        if leaked:
            errors.append(f"E_GIT_HISTORY_SECRET_TRACE:count={len(leaked)}")
        checks["git_history_zero_trace"] = "passed" if not leaked else "failed"
        p_cstat = _run_py([str(APP_DIR / "scripts/manage/app_manage.py"), "commit-status"], cwd=ROOT_DIR)
        if p_cstat.returncode == 0:
            try:
                cdata = json.loads(p_cstat.stdout)
                if cdata.get("pending_system_commit"):
                    warnings_disclose.append("uncommitted_domain_system_changes: run scripts/manage/app_manage.py commit once task batch finishes")
            except json.JSONDecodeError:
                pass
    return checks


def _check_smoke(errors: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    boot_proc = _run_py([str(APP_DIR / "bootstrap.py"), "inspect"], cwd=ROOT_DIR)
    if boot_proc.returncode != 0:
        _log_private("bootstrap_inspect", boot_proc.stdout + "\n" + boot_proc.stderr)
        errors.append("E_SMOKE_BOOTSTRAP_INSPECT_FAILED")
    else:
        bdata = json.loads(boot_proc.stdout)
        out["bootstrap_ready"] = bool(bdata.get("ready"))
        out["track_integrated"] = bool(bdata.get("track_integrated", False))
        if bdata.get("track_version") is not None:
            out["track_version"] = bdata.get("track_version")

    facade = APP_DIR / "scripts/facade/app_facade.py"
    with tempfile.TemporaryDirectory() as td:
        env = dict(os.environ)
        env["GEWU_APP_RUNTIME_DIR"] = td
        p_tpl = _run_py([str(facade), "--template"], cwd=ROOT_DIR, env=env)
        p_unk = _run_py([str(facade), "--action", "status", "--force"], cwd=ROOT_DIR, env=env)
        # Sensitive business test payload MUST travel via stdin ('--content -'), never CLI argv.
        probe_payload = "in_mem_smoke_payload"
        p_put = _run_py([str(facade), "--action", "put", "--kind", "note", "--content", "-"], cwd=ROOT_DIR, env=env, stdin_text=probe_payload)
        if p_tpl.returncode != 0 or "fill_template" not in p_tpl.stdout:
            _log_private("facade_template", p_tpl.stdout + "\n" + p_tpl.stderr)
            errors.append("E_SMOKE_FACADE_TEMPLATE_FAILED")
        if p_unk.returncode == 0 or "E_PARAM_UNKNOWN_FIELD" not in p_unk.stdout:
            _log_private("facade_unknown_field", p_unk.stdout + "\n" + p_unk.stderr)
            errors.append("E_SMOKE_FACADE_UNKNOWN_FIELD_NOT_REJECTED")
        if p_put.returncode != 0 or probe_payload in p_put.stdout:
            _log_private("facade_put_stdin", p_put.stdout + "\n" + p_put.stderr)
            errors.append("E_SMOKE_FACADE_PUT_FAILED_OR_LEAKED")
        if "Traceback" in p_tpl.stdout + p_tpl.stderr + p_unk.stdout + p_unk.stderr + p_put.stdout + p_put.stderr:
            _log_private("facade_traceback", p_tpl.stderr + "\n" + p_unk.stderr + "\n" + p_put.stderr)
            errors.append("E_SMOKE_FACADE_TRACEBACK_LEAK")
        gw = APP_DIR / "gewu"
        p_gw_cap = _run_py([str(gw), "capabilities"], cwd=ROOT_DIR, env=env)
        p_gw_tpl = _run_py([str(gw), "--template"], cwd=ROOT_DIR, env=env)
        if p_gw_cap.returncode != 0 or '"ok": true' not in p_gw_cap.stdout.lower():
            _log_private("gateway_capabilities", p_gw_cap.stdout + "\n" + p_gw_cap.stderr)
            errors.append("E_SMOKE_GATEWAY_CAPABILITIES_FAILED")
        if p_gw_tpl.returncode != 0 or ("fill_template" not in p_gw_tpl.stdout and "gewu.fill-template/1" not in p_gw_tpl.stdout):
            _log_private("gateway_template", p_gw_tpl.stdout + "\n" + p_gw_tpl.stderr)
            errors.append("E_SMOKE_GATEWAY_TEMPLATE_FAILED")
        out["facade_smoke"] = "passed" if not errors else "failed"

    gw = APP_DIR / "gewu"
    p_bak_list = _run_py([str(gw), "backup", "list"], cwd=ROOT_DIR)
    if p_bak_list.returncode != 0 or '"ok": true' not in p_bak_list.stdout.lower():
        _log_private("backup_list_smoke", p_bak_list.stdout + "\n" + p_bak_list.stderr)
        errors.append("E_SMOKE_APP_BACKUP_LIST_FAILED")
    out["dist_backup_smoke"] = "passed" if not errors else "failed"

    p_inv = _run_py([str(gw), "manage", "inventory"], cwd=ROOT_DIR)
    p_ag = _run_py([str(gw), "manage", "inspect-agents"], cwd=ROOT_DIR)
    if p_inv.returncode != 0 or '"ok": true' not in p_inv.stdout.lower():
        _log_private("manage_inventory_smoke", p_inv.stdout + "\n" + p_inv.stderr)
        errors.append("E_SMOKE_APP_MANAGE_INVENTORY_FAILED")
    if p_ag.returncode != 0 or '"ok": true' not in p_ag.stdout.lower():
        _log_private("manage_agents_smoke", p_ag.stdout + "\n" + p_ag.stderr)
        errors.append("E_SMOKE_APP_MANAGE_AGENTS_FAILED")
    out["domain_manage_smoke"] = "passed" if not errors else "failed"
    return out


def _check_security(errors: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    guard = APP_DIR / "scripts/guard/pre_tool_use_guard.py"
    for cmd in ("git show HEAD:scripts/app_core/probe.py", "git log -p", "cat scripts/app_core/probe.py"):
        res = _run_py([str(guard), "--self-test-tool", "Bash", "--self-test-input", json.dumps({"command": cmd})], cwd=ROOT_DIR)
        if res.returncode != 2:
            errors.append(f"E_GUARD_MISSED_PROBE:{cmd.split()[0]}")
    allow = _run_py([str(guard), "--self-test-tool", "Bash", "--self-test-input", json.dumps({"command": "git status"})], cwd=ROOT_DIR)
    if allow.returncode != 0:
        errors.append("E_GUARD_BLOCKED_SAFE_GIT_STATUS")

    validator = APP_DIR / "scripts/git/validate_commit.py"
    with tempfile.TemporaryDirectory() as td:
        ok_f = Path(td) / "ok.msg"
        bad_f = Path(td) / "bad.msg"
        ok_f.write_text("feat(core): [AppTest] Smoke verify commit contract\n\nBody.\n", encoding="utf-8")
        bad_f.write_text("feat(core): [AppTest] Leak " + ("enc:" + "v1:") + "payload\n", encoding="utf-8")
        p_ok = _run_py([str(validator), "--message-file", str(ok_f), "--agent", "AppTest"], cwd=ROOT_DIR)
        p_bad = _run_py([str(validator), "--message-file", str(bad_f), "--agent", "AppTest"], cwd=ROOT_DIR)
        if p_ok.returncode != 0 or p_bad.returncode != 2:
            errors.append("E_COMMIT_VALIDATOR_FAILED")

    # Delegate zero-disk & AAD anti-transplant check into hidden core via controlled facade (zero internal symbol leakage).
    facade = APP_DIR / "scripts/facade/app_facade.py"
    sp = _run_py([str(facade), "--self-test-security"], cwd=ROOT_DIR)
    if sp.returncode != 0 or '"OK_SECURITY_SELFTEST"' not in sp.stdout or "Traceback" in sp.stdout + sp.stderr:
        _log_private("hidden_security_selftest", sp.stdout + "\n" + sp.stderr)
        errors.append("E_SEC_ZERO_DISK_AAD_SELFTEST_FAILED")

    # Isolated app_backup backup+verify probe inside temporary dir (dist probe removed in ws-5)
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)

        rt_tmp = td_path / "rt"
        bak_out = td_path / "bak_probe"
        bak_out.mkdir()
        env = dict(os.environ)
        env["GEWU_APP_RUNTIME_DIR"] = str(rt_tmp)
        p_seed = _run_py([str(facade), "--action", "put", "--kind", "note", "--content", "-"], cwd=ROOT_DIR, env=env, stdin_text="backup-probe-item")
        p_bak = _run_py([str(APP_DIR / "scripts/distribution/app_backup.py"), "backup", "--out-dir", str(bak_out), "--kek-mode", "exclude"], cwd=ROOT_DIR, env=env)
        if p_seed.returncode != 0 or p_bak.returncode != 0:
            _log_private("backup_probe", p_bak.stdout + "\n" + p_bak.stderr)
            errors.append("E_SEC_APP_BACKUP_PROBE_FAILED")
        else:
            b_info = json.loads(p_bak.stdout)
            if b_info.get("kek_included") is not False:
                errors.append("E_SEC_BACKUP_UNEXPECTED_KEK_INCLUDE")
            p_bver = _run_py([str(APP_DIR / "scripts/distribution/app_backup.py"), "verify", "--backup-id", b_info["backup_id"], "--backup-dir", str(bak_out)], cwd=ROOT_DIR, env=env)
            if p_bver.returncode != 0:
                _log_private("backup_verify_probe", p_bver.stdout + "\n" + p_bver.stderr)
                errors.append("E_SEC_APP_BACKUP_VERIFY_FAILED")
        if "Traceback" in p_bak.stdout + p_bak.stderr:
            errors.append("E_SEC_DIST_BACKUP_TRACEBACK_LEAK")

    out["security_probes"] = "passed" if not errors else "failed"
    return out


def _check_track_capabilities(profile: Dict[str, Any], errors: List[str], warnings_disclose: List[str]) -> Dict[str, Any]:
    boot_prof = _load_json(APP_DIR / "contracts/bootstrap-profile.json")
    data_layout_file = APP_DIR / "contracts/data-layout-profile.json"
    data_layout = _load_json(data_layout_file) if data_layout_file.is_file() else {}
    if (
        boot_prof.get("track_integrated") is False
        or boot_prof.get("schema") == "forge.bootstrap-profile/2"
        or data_layout.get("track_integrated") is False
        or not boot_prof.get("track_path")
    ):
        return {"required": False, "ran": False, "reason": "Domain standalone (track_integrated=false)"}
    track_dir = ROOT_DIR / boot_prof["track_path"]
    manifest = _load_json(track_dir / "MANIFEST.json")
    for row in manifest["files"]:
        fp = track_dir / row["path"]
        if not fp.is_file() or _sha256(fp.read_bytes()) != row["sha256"].removeprefix("sha256:"):
            errors.append(f"E_TRACK_MANIFEST_DRIFT:{row['path']}")

    caps = [c for c in profile.get("track_capabilities", []) if c not in ("bootstrap", "facade")]
    cap_map = {
        "entry": ["test_entry_mapping", "test_entry_integration"],
        "build": ["test_build_mapping", "test_build_integration"],
        "object": ["test_object_mapping", "test_object_integration"],
        "app": ["test_app_mapping", "test_app_integration"],
        "release": ["test_release_mapping", "test_release_integration"],
    }
    slices: List[str] = []
    for c in caps:
        for mod in cap_map.get(c, []):
            if mod not in slices:
                slices.append(mod)

    out: Dict[str, Any] = {"track_manifest_checked": True, "declared_capabilities": caps, "slices": slices}
    if slices:
        tests_dir = track_dir / "tests"
        if not tests_dir.is_dir():
            warnings_disclose.append("track_slices_unavailable: GewuTrack tests directory absent in packaged distribution")
        else:
            env = dict(os.environ)
            extra = f"{track_dir}{os.pathsep}{tests_dir}"
            env["PYTHONPATH"] = f"{extra}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else extra
            p = subprocess.run(
                [sys.executable, "-B", "-m", "unittest", "-v", *slices],
                cwd=str(tests_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )
            out["slices_passed"] = p.returncode == 0
            if p.returncode != 0:
                _log_private("track_slices", p.stdout + "\n" + p.stderr)
                errors.append(f"E_TRACK_SLICES_FAILED:{','.join(slices)}")
    return out


def _check_domain_tests(errors: List[str]) -> Dict[str, Any]:
    tests_dir = APP_DIR / "tests"
    test_files = sorted(tests_dir.glob("test_*.py")) if tests_dir.is_dir() else []
    if not test_files:
        return {"discovered": 0, "ran": False, "status": "none"}
    proc = subprocess.run(
        [sys.executable, "-I", "-B", "-m", "unittest", "discover", "-s", str(tests_dir), "-p", "test_*.py", "-v"],
        cwd=str(APP_DIR),
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        _log_private("domain_tests", proc.stdout + "\n" + proc.stderr)
        errors.append("E_DOMAIN_TESTS_FAILED")
    return {
        "discovered": len(test_files),
        "files": [p.name for p in test_files],
        "ran": True,
        "status": "passed" if proc.returncode == 0 else "failed",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("smoke", "security", "staging", "release"), default="smoke")
    args = parser.parse_args()

    profile = _load_json(APP_DIR / "contracts/testing-profile.json")
    errors: List[str] = []
    warnings_disclose: List[str] = []
    ignored_staged: List[str] = []
    checks: Dict[str, Any] = {}

    if args.scenario == "smoke":
        checks["smoke"] = _check_smoke(errors)
        checks["domain_tests"] = _check_domain_tests(errors)
    elif args.scenario == "security":
        checks["smoke"] = _check_smoke(errors)
        checks["security"] = _check_security(errors)
    elif args.scenario == "staging":
        checks["rail"] = _check_rail_and_leaks("drafting", errors, warnings_disclose, ignored_staged)
        checks["smoke"] = _check_smoke(errors)
        checks["security"] = _check_security(errors)
        checks["domain_tests"] = _check_domain_tests(errors)
    elif args.scenario == "release":
        checks["rail"] = _check_rail_and_leaks("release", errors, warnings_disclose, ignored_staged)
        checks["smoke"] = _check_smoke(errors)
        checks["security"] = _check_security(errors)
        checks["domain_tests"] = _check_domain_tests(errors)
        checks["track"] = _check_track_capabilities(profile, errors, warnings_disclose)

    status = "pass" if not errors else "fail"
    print(
        json.dumps(
            {
                "schema": "forge.app-test-run/1",
                "app_id": profile.get("app_id"),
                "scenario": args.scenario,
                "status": status,
                "errors": errors,
                "warnings_disclose": sorted(set(warnings_disclose)),
                "ignored_staged_warnings": sorted(set(ignored_staged)),
                "checks": checks,
            },
            ensure_ascii=False,
        )
    )
    return 0 if status == "pass" else 2


if __name__ == "__main__":
    sys.exit(main())
