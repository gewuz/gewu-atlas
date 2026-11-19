#!/usr/bin/env python3
"""Generated Domain self-management operator (`rules/`, `contracts/`, `skills/`, `scripts/`, `AGENTS.md`) and commit guard."""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]

KIND_PREFIXES = {
    "rule": ("rules/", "rules/"),
    "contract": ("contracts/", "contracts/"),
    "skill": ("skills/", "skills/"),
    "operator": ("scripts/", "scripts/"),
}

PROTECTED_IDS = frozenset({
    "app-contract-rail-rule",
    "app-testing-strategy-rule",
    "app-security-profile",
    "app-testing-profile",
    "app-data-layout-profile",
    "app-execution-routing-contract",
    "app-commit-message-contract",
    "app-core-runtime",
    "app-dist-backup",
    "app-domain-manage",
    "app-facade-entry",
})

PROTECTED_SYSTEM_PATHS = frozenset({
    "AGENTS.md",
    "app.json",
    "bootstrap.py",
    "gewu",
    "contracts/bootstrap-profile.json",
    "contracts/object-catalog.json",
    "contracts/app-security-profile.json",
    "contracts/cli-manifest.json",
    "contracts/host-guard-template.json",
    "contracts/testing-profile.json",
    "contracts/data-layout-profile.json",
    "contracts/execution-routing-v1.md",
    "contracts/commit-message-v1.md",
    "contracts/sys-change-ticket-v1.md",
    "contracts/markdown-formatting-v1.md",
    "rules/rule-app-contract-rail.md",
    "rules/rule-testing-strategy.md",
    "scripts/facade/app_facade.py",
    "scripts/distribution/app_dist.py",
    "scripts/distribution/app_backup.py",
    "scripts/manage/app_manage.py",
    "scripts/distribution/install_dist.py",
    "scripts/distribution/archive_io.py",
    "scripts/guard/pre_tool_use_guard.py",
    "scripts/git/validate_commit.py",
    "scripts/checks/run_app_tests.py",
})

SAFE_SYSTEM_EXACT = frozenset({
    "AGENTS.md",
    "app.json",
    "bootstrap.py",
    "gewu",
    ".gitignore",
    ".cursorignore",
    ".claudeignore",
})

SAFE_SYSTEM_DIRS = ("rules/", "contracts/", "skills/", "scripts/", "references/", "dicts/", "usecase-for-agent/")

FORBIDDEN_CONTENT_PATTERNS = (
    ("secret_leak_marker", re.compile(r"SECRET[:]|API_KEY[=]|TOKEN[=]|BEGIN[ ]PRIVATE KEY")),
    ("raw_sql_in_markdown", re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE)\b.+\b(FROM|INTO|SET)\b", re.I)),
    ("direct_app_core_import", re.compile(r"from\s+app_core\b|import\s+app_core\b")),
)

LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
AGENTS_MAX_BYTES = 8192
VALID_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")


def _emit(payload, code=0):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    raise SystemExit(code)


def _fail(reason, code=1, **extra):
    payload = {"ok": False, "error": reason}
    payload.update(extra)
    _emit(payload, code=code)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_child(rel_path: str) -> Path:
    if not rel_path or rel_path.startswith("/") or "\\" in rel_path:
        raise ValueError(f"invalid relative path: {rel_path}")
    raw = APP_ROOT / rel_path
    cur = APP_ROOT
    for part in Path(rel_path).parts:
        if part in ("", ".", ".."):
            raise ValueError(f"unsafe path segment: {rel_path}")
        cur = cur / part
        if cur.exists() and cur.is_symlink():
            raise ValueError(f"symlink not allowed: {rel_path}")
    resolved = raw.resolve()
    root_resolved = APP_ROOT.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError(f"path escapes domain root: {rel_path}")
    return raw


def _atomic_write_bytes(path: Path, data: bytes, mode: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".manage-", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def _atomic_write_text(path: Path, text: str, mode: int = 0o644):
    _atomic_write_bytes(path, text.encode("utf-8"), mode)


def _load_json(path: Path):
    return json.loads(path.read_text("utf-8"))


def _load_app_meta():
    return _load_json(APP_ROOT / "app.json")


def _load_catalog():
    return _load_json(APP_ROOT / "contracts" / "object-catalog.json")


def _save_catalog(catalog: dict):
    text = json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
    _atomic_write_text(APP_ROOT / "contracts" / "object-catalog.json", text, 0o644)


def _read_input_text(content: str = None, content_file: str = None) -> str:
    if content is not None and content_file is not None:
        raise ValueError("specify only one of --content or --content-file")
    if content == "-" or content_file == "-":
        return sys.stdin.read()
    if content is not None:
        return content
    if content_file:
        path = Path(content_file)
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        return path.read_text("utf-8")
    raise ValueError("missing content input (--content or --content-file)")


def _scan_forbidden(rel_path: str, text: str, allow_raw_sql: bool = False):
    violations = []
    for label, pattern in FORBIDDEN_CONTENT_PATTERNS:
        if label == "raw_sql_in_markdown" and (allow_raw_sql or not rel_path.endswith(".md")):
            continue
        if label == "direct_app_core_import" and rel_path.startswith("scripts/app_core/"):
            continue
        if pattern.search(text):
            violations.append(label)
    return violations


def _validate_skill_markdown(rel_path: str, text: str):
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        return "SKILL.md missing YAML frontmatter"
    front = text[4:].split("\n---\n", 1)[0]
    if "name:" not in front or "description:" not in front:
        return "SKILL.md frontmatter requires name and description"
    body = text.split("\n---\n", 1)[1]
    if "python3" not in body and "./bootstrap.py" not in body and "./gewu" not in body:
        return "SKILL.md requires CLI command skeleton (no prompt-only orchestration)"
    return ""


def _validate_agents_text(text: str):
    errors = []
    encoded = text.encode("utf-8")
    if len(encoded) > AGENTS_MAX_BYTES:
        errors.append(f"AGENTS.md exceeds {AGENTS_MAX_BYTES} bytes ({len(encoded)})")
    base_mandatory = (
        "execution-routing-v1.md",
        "commit-message-v1.md",
    )
    missing_base = [item for item in base_mandatory if item not in text]
    has_gateway = "./gewu" in text and "cli-manifest.json" in text
    legacy_mandatory = (
        "app_facade.py",
        "bootstrap.py",
        "app_dist.py",
        "app_backup.py",
        "app_manage.py",
        "install_dist.py",
        "validate_commit.py",
        "run_app_tests.py",
    )
    missing_legacy = [item for item in legacy_mandatory if item not in text]
    if missing_base:
        errors.append(f"AGENTS.md missing mandatory contract links: {', '.join(missing_base)}")
    elif not has_gateway and missing_legacy:
        errors.append(
            "AGENTS.md must declare either unified gateway ('./gewu' + 'cli-manifest.json') "
            f"or legacy Domain script entries (missing: {', '.join(missing_legacy)})"
        )
    broken_links = []
    for raw_target in LINK_RE.findall(text):
        target = raw_target.split("#", 1)[0].strip()
        if not target or target.startswith(("http://", "https://", "file://", "mailto:")):
            continue
        if not (APP_ROOT / target).exists():
            broken_links.append(target)
    if broken_links:
        errors.append(f"AGENTS.md has broken relative links: {', '.join(broken_links)}")
    leaks = _scan_forbidden("AGENTS.md", text)
    if leaks:
        errors.append(f"AGENTS.md forbidden markers: {', '.join(leaks)}")
    return errors, len(encoded)


def _git_available():
    rc = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False)
    if rc.returncode != 0:
        return False
    probe = subprocess.run(
        ["git", "-C", str(APP_ROOT), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.returncode == 0


def _is_safe_domain_system_path(rel_path: str) -> bool:
    norm = rel_path.replace("\\", "/").lstrip("./")
    if norm in SAFE_SYSTEM_EXACT:
        return True
    if not any(norm.startswith(prefix) for prefix in SAFE_SYSTEM_DIRS):
        return False
    lower = norm.lower()
    if "/__pycache__/" in lower or lower.endswith((".pyc", ".pyo", ".db", ".enc", ".key", ".pem")):
        return False
    return True


def _git_status_files():
    if not _git_available():
        return {"in_git_repo": False, "pending_system_files": [], "other_dirty_files": []}
    proc = subprocess.run(
        ["git", "-C", str(APP_ROOT), "status", "--porcelain=v1", "-uall", "-z"],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return {"in_git_repo": False, "pending_system_files": [], "other_dirty_files": []}
    # Porcelain paths are repository-root relative, even when git -C targets a domain.
    prefix_probe = subprocess.run(
        ["git", "-C", str(APP_ROOT), "rev-parse", "--show-prefix"],
        capture_output=True, check=False,
    )
    if prefix_probe.returncode != 0:
        _fail("git_domain_prefix_unavailable")
    prefix = prefix_probe.stdout.decode("utf-8", "strict").rstrip("\r\n")

    def domain_path(path):
        if prefix and not path.startswith(prefix):
            return None
        return path[len(prefix):] if prefix else path

    pending_system = []
    other_dirty = []
    entries = [chunk.decode("utf-8", "replace") for chunk in proc.stdout.split(b"\x00") if chunk]
    i = 0
    while i < len(entries):
        rec = entries[i]
        xy = rec[:2]
        path = rec[3:] if len(rec) > 3 else ""
        if xy and ("R" in xy or "C" in xy) and i + 1 < len(entries):
            old_path = entries[i + 1]
            old_local = domain_path(old_path)
            if old_local and _is_safe_domain_system_path(old_local):
                pending_system.append(old_local)
            i += 1
        i += 1
        if not path:
            continue
        local = domain_path(path)
        if local and _is_safe_domain_system_path(local):
            pending_system.append(local)
        else:
            other_dirty.append(path)
    return {
        "in_git_repo": True,
        "pending_system_files": sorted(set(pending_system)),
        "other_dirty_files": sorted(set(other_dirty)),
    }


def _inventory_report():
    catalog = _load_catalog()
    objects = catalog.get("objects", [])
    declared = {
        "contracts/bootstrap-profile.json": "__domain_meta__",
        "contracts/object-catalog.json": "__domain_meta__",
    }
    for obj in objects:
        for rel in obj.get("files", []):
            declared[rel] = obj.get("id")

    actual = []
    for area in ("rules", "contracts", "skills", "scripts"):
        area_dir = APP_ROOT / area
        if not area_dir.exists():
            continue
        for item in sorted(area_dir.rglob("*")):
            if item.is_symlink():
                continue
            if item.is_file():
                rel = item.relative_to(APP_ROOT).as_posix()
                if "__pycache__/" in rel or rel.endswith((".pyc", ".pyo")):
                    continue
                actual.append(rel)

    for rel in declared:
        if (
            (rel.startswith(("dicts/", "tests/", "usecase-for-agent/")) or rel in ("bootstrap.py", "app.json"))
            and (APP_ROOT / rel).is_file()
            and not (APP_ROOT / rel).is_symlink()
        ):
            actual.append(rel)
    actual_set = set(actual)
    declared_set = set(declared)
    return {
        "domain_id": catalog.get("app_id", _load_app_meta().get("app_id", "")),
        "object_count": len(objects),
        "declared_file_count": len(declared_set),
        "actual_file_count": len(actual_set),
        "unregistered": sorted(actual_set - declared_set),
        "missing": sorted(declared_set - actual_set),
        "areas": {
            area: sorted(p for p in actual if p.startswith(area + "/"))
            for area in ("rules", "contracts", "skills", "scripts")
        },
    }


def _default_rel_path(obj_id: str, kind: str) -> str:
    if kind == "rule":
        return f"rules/{obj_id}.md"
    if kind == "contract":
        return f"contracts/{obj_id}.json"
    if kind == "skill":
        return f"skills/{obj_id}/SKILL.md"
    if kind == "operator":
        mod = obj_id.replace("-", "_")
        return f"scripts/ops/{mod}.py"
    raise ValueError(f"unsupported kind: {kind}")


def _require_approved_sys_ticket(target_rel: str = "", ticket_id_hint: str = "") -> None:
    """Block domain system mutations when workspace mounts GewuTicket-data/SYS without an approved in_progress SYS ticket."""
    ws_root = APP_ROOT.parent
    if not (ws_root / "Volumes" / "GewuTicket-data" / "SYS").is_dir():
        return
    guard_path = APP_ROOT / "scripts" / "guard" / "pre_tool_use_guard.py"
    if not guard_path.is_file():
        return
    import importlib.util
    spec = importlib.util.spec_from_file_location("_gewu_guard_sys", guard_path)
    if spec is None or spec.loader is None:
        return
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rel_ws = f"{APP_ROOT.name}/{target_rel.lstrip('./')}" if target_rel else APP_ROOT.name
    old_hint = os.environ.get("GEWU_SYS_TICKET_ID")
    if ticket_id_hint:
        os.environ["GEWU_SYS_TICKET_ID"] = ticket_id_hint
    try:
        ok = mod._has_approved_sys_ticket(ws_root, rel_ws)
    finally:
        if ticket_id_hint:
            if old_hint is None:
                os.environ.pop("GEWU_SYS_TICKET_ID", None)
            else:
                os.environ["GEWU_SYS_TICKET_ID"] = old_hint
    if not ok:
        _fail(
            "sys_ticket_required",
            target=rel_ws,
            volume="./Volumes/GewuTicket-data",
            hint="./gewu.command ticket --action out --volume ./Volumes/GewuTicket-data --category SYS",
        )


def _do_commit(args):
    _require_approved_sys_ticket(ticket_id_hint=getattr(args, "ticket_id", "") or "")
    inv = _inventory_report()
    if inv["unregistered"] or inv["missing"]:
        _fail("domain_inventory_mismatch", unregistered=inv["unregistered"], missing=inv["missing"])
    agents_text = (APP_ROOT / "AGENTS.md").read_text("utf-8")
    agents_errors, agents_bytes = _validate_agents_text(agents_text)
    if agents_errors:
        _fail("agents_validation_failed", errors=agents_errors, agents_bytes=agents_bytes)

    verify_scenario = getattr(args, "verify_scenario", "smoke") or "smoke"
    if verify_scenario != "none":
        test_proc = subprocess.run(
            [sys.executable, "-I", "-B", str(APP_ROOT / "scripts" / "checks" / "run_app_tests.py"), "--scenario", verify_scenario],
            cwd=str(APP_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if test_proc.returncode != 0:
            _fail(
                "domain_tests_failed",
                scenario=verify_scenario,
                output=(test_proc.stdout + "\n" + test_proc.stderr).strip(),
            )

    if getattr(args, "message_file", None):
        message_text = _read_input_text(content_file=args.message_file)
    elif getattr(args, "type", None) and getattr(args, "scope", None) and getattr(args, "subject", None):
        lines = [f"{args.type}({args.scope}): [{args.agent}] {args.subject}"]
        if getattr(args, "body", None):
            lines.extend(["", args.body])
        message_text = "\n".join(lines) + "\n"
    else:
        _fail("commit_requires_message_file_or_structured_fields")

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".msg", delete=False) as tmp:
        tmp.write(message_text)
        msg_path = Path(tmp.name)
    try:
        val_proc = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                str(APP_ROOT / "scripts" / "git" / "validate_commit.py"),
                "--message-file",
                str(msg_path),
                "--agent",
                args.agent,
            ],
            cwd=str(APP_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if val_proc.returncode != 0:
            _fail("commit_message_invalid", detail=(val_proc.stdout + "\n" + val_proc.stderr).strip())

        if not _git_available():
            if getattr(args, "init_git", False):
                subprocess.run(["git", "init", "-b", "main", str(APP_ROOT)], check=True, capture_output=True)
            else:
                _fail("git_repo_not_initialized_pass_init_git_if_desired")

        status_before = _git_status_files()
        safe_paths = status_before["pending_system_files"]
        if not safe_paths:
            return {
                "ok": True,
                "committed": False,
                "reason": "no_domain_system_changes",
                "other_dirty_files": status_before["other_dirty_files"],
            }

        subprocess.run(["git", "-C", str(APP_ROOT), "add", "--"] + safe_paths, check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(APP_ROOT),
                "-c",
                f"user.name={args.agent}",
                "-c",
                f"user.email={args.agent.lower().replace(' ', '-')}@gewu.local",
                "commit",
                "-F",
                str(msg_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        if msg_path.exists():
            msg_path.unlink()

    rev = subprocess.run(
        ["git", "-C", str(APP_ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    log_path = APP_ROOT / ".local" / "logs" / "domain-system-commits.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent": args.agent,
        "commit": rev,
        "files": safe_paths,
        "verify_scenario": verify_scenario,
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    os.chmod(log_path, 0o600)
    return {
        "ok": True,
        "committed": True,
        "commit": rev,
        "staged_files": safe_paths,
        "skipped_non_system_dirty": status_before["other_dirty_files"],
        "verify_scenario": verify_scenario,
    }


def _cmd_template():
    return {
        "ok": True,
        "domain_id": _load_app_meta().get("app_id", ""),
        "templates": {
            "inventory": "python3 -I -B scripts/manage/app_manage.py inventory",
            "upsert_rule_skill_script": (
                "python3 -I -B scripts/manage/app_manage.py upsert-object --id <kebab-id> "
                "--kind rule|contract|skill|operator [--path <rel>] --content-file <file>"
            ),
            "remove_object": "python3 -I -B scripts/manage/app_manage.py remove-object --id <kebab-id>",
            "inspect_agents": "python3 -I -B scripts/manage/app_manage.py inspect-agents",
            "update_agents": (
                "python3 -I -B scripts/manage/app_manage.py update-agents "
                "[--purpose <text>] [--add-invariant <text>] [--add-route SCENARIO=ENTRY]"
            ),
            "commit_status": "python3 -I -B scripts/manage/app_manage.py commit-status",
            "commit": (
                "python3 -I -B scripts/manage/app_manage.py commit --type feat|fix|chore|docs "
                "--scope <scope> --subject <summary> --agent <Agent>"
            ),
        },
    }


def _cmd_inventory():
    inv = _inventory_report()
    status = _git_status_files()
    inv["ok"] = not inv["unregistered"] and not inv["missing"]
    inv["in_git_repo"] = status["in_git_repo"]
    inv["pending_system_commit"] = bool(status["pending_system_files"])
    inv["pending_system_files"] = status["pending_system_files"]
    return inv


def _cmd_upsert_object(args):
    obj_id = args.id.strip()
    if not VALID_ID_RE.match(obj_id):
        _fail("invalid_object_id", id=obj_id)
    if args.kind not in KIND_PREFIXES:
        _fail("unsupported_kind", kind=args.kind)
    rel_path = args.path.strip() if args.path else _default_rel_path(obj_id, args.kind)
    required_prefix = KIND_PREFIXES[args.kind][0]
    app_metadata = args.kind == "contract" and args.allow_system_edit and rel_path == "app.json"
    if not rel_path.startswith(required_prefix) and not app_metadata:
        _fail("path_kind_mismatch", kind=args.kind, expected_prefix=required_prefix, path=rel_path)
    if rel_path.startswith("scripts/app_core/"):
        _fail("forbidden_app_core_mutation", path=rel_path)
    if rel_path in PROTECTED_SYSTEM_PATHS and not args.allow_system_edit:
        _fail("protected_system_path_requires_allow_system_edit", path=rel_path)
    if args.kind == "skill" and not rel_path.endswith("/SKILL.md"):
        _fail("skill_must_end_with_SKILL_md", path=rel_path)
    if args.kind == "operator" and not rel_path.endswith(".py"):
        _fail("operator_must_be_python_script", path=rel_path)

    text = _read_input_text(args.content, args.content_file)
    leaks = _scan_forbidden(rel_path, text, allow_raw_sql=rel_path.endswith(".py"))
    if leaks:
        _fail("forbidden_content_detected", path=rel_path, violations=leaks)
    if args.kind == "skill":
        skill_err = _validate_skill_markdown(rel_path, text)
        if skill_err:
            _fail("invalid_skill_markdown", reason=skill_err, path=rel_path)
    if args.kind == "contract" and rel_path.endswith(".json"):
        json.loads(text)
    _require_approved_sys_ticket(rel_path, getattr(args, "ticket_id", "") or "")

    target = _canonical_child(rel_path)
    mode = 0o755 if (args.executable or rel_path.startswith("scripts/")) else 0o644
    _atomic_write_text(target, text, mode)

    catalog = _load_catalog()
    objects = catalog.get("objects", [])
    refs = list(dict.fromkeys(args.ref or []))
    existing = next((o for o in objects if o.get("id") == obj_id), None)
    if existing is not None:
        if existing.get("kind") != args.kind:
            _fail("object_kind_immutable", id=obj_id, existing=existing.get("kind"), requested=args.kind)
        files = list(existing.get("files", []))
        if rel_path not in files:
            files.append(rel_path)
        existing["files"] = files
        if args.ref is not None:
            existing["references"] = refs
    else:
        objects.append({"id": obj_id, "kind": args.kind, "files": [rel_path], "references": refs})
    catalog["objects"] = objects
    _save_catalog(catalog)

    result = {
        "ok": True,
        "action": "upsert-object",
        "id": obj_id,
        "kind": args.kind,
        "path": rel_path,
        "sha256": _sha256_bytes(text.encode("utf-8")),
        "system_commit_required": True,
    }
    if args.commit:
        result["commit_result"] = _do_commit(args)
        result["system_commit_required"] = False
    return result


def _cmd_remove_object(args):
    obj_id = args.id.strip()
    if obj_id in PROTECTED_IDS:
        _fail("cannot_remove_protected_core_object", id=obj_id)
    app_meta = _load_app_meta()
    if obj_id == app_meta.get("main_skill"):
        _fail("cannot_remove_main_skill", id=obj_id)

    catalog = _load_catalog()
    objects = catalog.get("objects", [])
    target_obj = next((o for o in objects if o.get("id") == obj_id), None)
    if target_obj is None:
        _fail("object_not_found", id=obj_id)
    dependents = [o["id"] for o in objects if o.get("id") != obj_id and obj_id in o.get("references", [])]
    if dependents:
        _fail("object_referenced_by_others", id=obj_id, dependents=dependents)

    removed_files = []
    for rel in target_obj.get("files", []):
        if rel in PROTECTED_SYSTEM_PATHS:
            _fail("cannot_remove_protected_system_path", id=obj_id, path=rel)
        _require_approved_sys_ticket(rel, getattr(args, "ticket_id", "") or "")
        path = _canonical_child(rel)
        if path.exists():
            path.unlink()
            removed_files.append(rel)
            parent = path.parent
            if parent != APP_ROOT and parent.exists() and not any(parent.iterdir()):
                parent.rmdir()

    catalog["objects"] = [o for o in objects if o.get("id") != obj_id]
    _save_catalog(catalog)

    result = {
        "ok": True,
        "action": "remove-object",
        "id": obj_id,
        "removed_files": removed_files,
        "system_commit_required": True,
    }
    if args.commit:
        result["commit_result"] = _do_commit(args)
        result["system_commit_required"] = False
    return result


def _cmd_inspect_agents():
    agents_path = APP_ROOT / "AGENTS.md"
    text = agents_path.read_text("utf-8")
    errors, byte_len = _validate_agents_text(text)
    return {
        "ok": not errors,
        "path": "AGENTS.md",
        "agents_bytes": byte_len,
        "max_bytes": AGENTS_MAX_BYTES,
        "sha256": _sha256_bytes(text.encode("utf-8")),
        "errors": errors,
    }


def _rewrite_markdown_section(text: str, headings, mutator):
    lines = text.splitlines()
    if isinstance(headings, str):
        headings = (headings,)
    markers = tuple(f"## {h}" for h in headings)
    start = None
    end = len(lines)
    for idx, line in enumerate(lines):
        if start is None and line.strip() in markers:
            start = idx + 1
            continue
        if start is not None and line.startswith("## "):
            end = idx
            break
    if start is None:
        raise ValueError(f"missing AGENTS.md section: {' / '.join(markers)}")
    updated_block = mutator(lines[start:end])
    return "\n".join(lines[:start] + updated_block + lines[end:]) + "\n"


def _rewrite_agents_purpose(text: str, purpose: str) -> str:
    lines = text.splitlines()
    title_idx = next((i for i, ln in enumerate(lines) if ln.startswith("# ")), None)
    next_h2 = next((i for i, ln in enumerate(lines) if ln.startswith("## ")), None)
    if title_idx is None or next_h2 is None or next_h2 <= title_idx:
        raise ValueError("AGENTS.md missing title/heading layout for purpose rewrite")
    updated = lines[: title_idx + 1] + ["", purpose.strip(), ""] + lines[next_h2:]
    return "\n".join(updated) + "\n"


def _cmd_update_agents(args):
    agents_path = APP_ROOT / "AGENTS.md"
    if args.replace_file:
        new_text = _read_input_text(content_file=args.replace_file)
    else:
        new_text = agents_path.read_text("utf-8")
        if args.purpose:
            new_text = _rewrite_agents_purpose(new_text, args.purpose)
        inv_headings = ("全局不变量（40%）", "Invariants")
        route_headings = ("场景路由表（60%）", "Scenario Routing")
        for item in args.add_invariant or []:
            bullet = f"- {item.strip()}"
            new_text = _rewrite_markdown_section(
                new_text,
                inv_headings,
                lambda block, b=bullet: (
                    block
                    if b in block
                    else (
                        block[:-1] + [b, ""]
                        if block and block[-1] == ""
                        else block + [b]
                    )
                ),
            )
        for item in args.remove_invariant or []:
            needle = item.strip()
            new_text = _rewrite_markdown_section(
                new_text,
                inv_headings,
                lambda block, n=needle: [ln for ln in block if n not in ln],
            )
        for spec in args.add_route or []:
            if "=" not in spec:
                _fail("invalid_route_spec_expected_SCENARIO=ENTRY", spec=spec)
            scenario, entry = spec.split("=", 1)
            row = f"| {scenario.strip()} | {entry.strip()} |"
            new_text = _rewrite_markdown_section(
                new_text,
                route_headings,
                lambda block, s=scenario.strip(), r=row: (
                    [r if ln.startswith(f"| {s} |") else ln for ln in block]
                    if any(ln.startswith(f"| {s} |") for ln in block)
                    else block + [r]
                ),
            )
        for scenario in args.remove_route or []:
            prefix = f"| {scenario.strip()} |"
            new_text = _rewrite_markdown_section(
                new_text,
                route_headings,
                lambda block, p=prefix: [ln for ln in block if not ln.startswith(p)],
            )

    errors, byte_len = _validate_agents_text(new_text)
    if errors:
        _fail("agents_validation_failed", errors=errors, agents_bytes=byte_len)
    _require_approved_sys_ticket("AGENTS.md", getattr(args, "ticket_id", "") or "")
    _atomic_write_text(agents_path, new_text, 0o644)

    result = {
        "ok": True,
        "action": "update-agents",
        "agents_bytes": byte_len,
        "max_bytes": AGENTS_MAX_BYTES,
        "sha256": _sha256_bytes(new_text.encode("utf-8")),
        "system_commit_required": True,
    }
    if args.commit:
        result["commit_result"] = _do_commit(args)
        result["system_commit_required"] = False
    return result


def _add_commit_args(parser):
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--message-file")
    parser.add_argument("--type")
    parser.add_argument("--scope")
    parser.add_argument("--subject")
    parser.add_argument("--body")
    parser.add_argument("--agent", default="Antigravity")
    parser.add_argument("--verify-scenario", choices=("none", "smoke", "security", "release"), default="smoke")
    parser.add_argument("--init-git", action="store_true")
    parser.add_argument("--ticket-id")


def main():
    parser = argparse.ArgumentParser(description="Domain self-management operator for rules/contracts/skills/scripts/AGENTS.md")
    parser.add_argument("--template", action="store_true", help="Print structured invocation template")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("inspect")
    sub.add_parser("inventory")
    sub.add_parser("inspect-agents")
    sub.add_parser("commit-status")

    p_up = sub.add_parser("upsert-object")
    p_up.add_argument("--id", required=True)
    p_up.add_argument("--kind", required=True, choices=tuple(KIND_PREFIXES))
    p_up.add_argument("--path")
    p_up.add_argument("--content")
    p_up.add_argument("--content-file")
    p_up.add_argument("--executable", action="store_true")
    p_up.add_argument("--ref", action="append")
    p_up.add_argument("--allow-system-edit", action="store_true")
    _add_commit_args(p_up)

    p_rm = sub.add_parser("remove-object")
    p_rm.add_argument("--id", required=True)
    _add_commit_args(p_rm)

    p_ag = sub.add_parser("update-agents")
    p_ag.add_argument("--purpose")
    p_ag.add_argument("--add-invariant", action="append")
    p_ag.add_argument("--remove-invariant", action="append")
    p_ag.add_argument("--add-route", action="append")
    p_ag.add_argument("--remove-route", action="append")
    p_ag.add_argument("--replace-file")
    _add_commit_args(p_ag)

    p_cm = sub.add_parser("commit")
    _add_commit_args(p_cm)

    args = parser.parse_args()
    if args.template:
        _emit(_cmd_template())
    if not args.cmd or args.cmd in ("inspect", "inventory"):
        res = _cmd_inventory()
        _emit(res, code=0 if res["ok"] else 1)
    try:
        if args.cmd == "upsert-object":
            _emit(_cmd_upsert_object(args))
        elif args.cmd == "remove-object":
            _emit(_cmd_remove_object(args))
        elif args.cmd == "inspect-agents":
            res = _cmd_inspect_agents()
            _emit(res, code=0 if res["ok"] else 1)
        elif args.cmd == "update-agents":
            _emit(_cmd_update_agents(args))
        elif args.cmd == "commit-status":
            status = _git_status_files()
            status["ok"] = True
            status["pending_system_commit"] = bool(status["pending_system_files"])
            _emit(status)
        elif args.cmd == "commit":
            _emit(_do_commit(args))
    except (ValueError, KeyError, json.JSONDecodeError, OSError, subprocess.CalledProcessError) as exc:
        _fail("domain_manage_failed", detail=str(exc))


if __name__ == "__main__":
    main()
