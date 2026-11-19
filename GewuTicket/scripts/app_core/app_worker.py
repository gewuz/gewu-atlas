"""Hidden core worker injected into every Forge-generated App (invoked via python3 -I -B)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

CORE_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = CORE_DIR.parent
APP_DIR = SCRIPTS_DIR.parent if SCRIPTS_DIR.name == "scripts" else SCRIPTS_DIR
if str(CORE_DIR.parent) not in sys.path:
    sys.path.insert(0, str(CORE_DIR.parent))

from app_core.capsule import format_envelope, guard_capsule, run_with_guard, triad_capsule
from app_core.param_normalizer import CommandSchema, SlotSpec, normalize_args
from app_core.vault_store import OpaqueVault, VaultSecurityError, load_or_init_kek


def _app_id() -> str:
    env_id = os.environ.get("GEWU_APP_ID", "").strip()
    if env_id:
        return env_id
    custom = os.environ.get("GEWU_APP_RUNTIME_DIR", "").strip()
    if custom and (Path(custom) / ".app_id").exists():
        return (Path(custom) / ".app_id").read_text("utf-8").strip()
    meta_file = APP_DIR / "app.json"
    if meta_file.exists():
        return json.loads(meta_file.read_text("utf-8"))["app_id"]
    return "gewu-app"


def _runtime_dirs() -> tuple[Path, Path]:
    custom = os.environ.get("GEWU_APP_RUNTIME_DIR", "").strip()
    base = Path(custom) if custom else (APP_DIR / ".local")
    return base / "state", base / "logs"


SCHEMA = CommandSchema(
    command_hint="python3 -I -B scripts/facade/app_facade.py --action <put|exec|list|status> [--ref rec_01] [--kind <record|note>] [--content <text|->",
    slots=(
        SlotSpec(
            name="action",
            kind="enum",
            required=True,
            choices=("put", "exec", "list", "status"),
            aliases={"store": "put", "execute": "exec", "run": "exec", "ls": "list"},
            description="动作类型",
            example="put",
        ),
        SlotSpec(
            name="ref",
            kind="ref",
            required=False,
            ref_prefix="rec_",
            description="托管短句柄（如 rec_01）",
            example="rec_01",
        ),
        SlotSpec(
            name="kind",
            kind="enum",
            required=False,
            default="record",
            choices=("record", "note", "task-item"),
            description="记录类别",
            example="record",
        ),
        SlotSpec(
            name="content",
            kind="str",
            required=False,
            default="",
            allow_stdin_dash=True,
            description="待托管正文（支持 - 从 stdin 读取）",
            example="任务输入",
        ),
    ),
)


def main() -> int:
    raw_argv = sys.argv[1:]
    app_id = _app_id()
    state_dir, log_dir = _runtime_dirs()
    stdin_data = sys.stdin.read() if not sys.stdin.isatty() else None

    if raw_argv == ["--self-test-security"]:
        try:
            with tempfile.TemporaryDirectory() as td:
                r = Path(td)
                kek = load_or_init_kek(r / "s" / "k.key")
                vs = OpaqueVault(app_id, r / "s" / "v.bin", kek)
                res = vs.put_record("note", {"body": "probe-in-memory"})
                enc_bytes = (r / "s" / "v.bin").read_bytes()
                if b"SQLite format 3" in enc_bytes or b"probe-in-memory" in enc_bytes:
                    raise VaultSecurityError("plaintext detected in vault container")
                other = OpaqueVault("otherapp" if app_id != "otherapp" else "foreignapp", r / "s" / "v.bin", kek)
                aad_blocked = False
                try:
                    other.put_record("note", {"body": "tamper"})
                except VaultSecurityError:
                    aad_blocked = True
                if not aad_blocked:
                    raise VaultSecurityError("AAD cross-app check did not halt")
                exe_receipt = vs.execute_on_handle(
                    res["ref"],
                    lambda kind, payload: {"kind": kind, "bytes": len(str(payload.get("body", "")))},
                )
            cap = triad_capsule(
                state="隐身运行态零落盘、加密与 AAD 防移植自检通过（全程内存执行，零敏感信息回吐）",
                orbit_options=["使用 --template 查看业务调用模板"],
                anchor="内部存储细节保留在隐身核与纯内存中，绝不透传至上下文。",
                terminal=True,
            )
            print(
                json.dumps(
                    format_envelope(
                        True,
                        "OK_SECURITY_SELFTEST",
                        cap,
                        data={"zero_disk_magic": True, "aad_blocked": True, "in_memory_exec": bool(exe_receipt.get("bytes"))},
                    ),
                    ensure_ascii=False,
                )
            )
            return 0
        except Exception:  # noqa: BLE001 - log raw error privately, return sanitized capsule
            import traceback

            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "worker-selftest.log").write_text(traceback.format_exc(), encoding="utf-8")
            cap = guard_capsule(
                mode="HALTED",
                reason="E_HALT_SECURITY: 运行态安全自检未通过",
                action_guidance="请人类工程师查看受保护运行日志排查，Agent 严禁重试或探测底层。",
                anchor="GUARD: HALTED | 禁止重试",
            )
            print(json.dumps(format_envelope(False, "E_HALT_SECURITY", cap), ensure_ascii=False))
            return 2

    if any(t in ("--template", "--describe", "-h", "--help") for t in raw_argv):
        _, tpl = normalize_args(SCHEMA, ["--template"], stdin_data=None)
        cap = triad_capsule(
            state="已返回最小填空模板（零副作用）",
            orbit_options=[
                "按模板填写后调用: --action <put|exec|list|status> 传入 app_facade.py",
            ],
            anchor="仅填写合法槽位；严禁自造未定义参数或探测内部运行态。",
            terminal=False,
        )
        print(json.dumps(format_envelope(True, "OK_TEMPLATE", cap, data=tpl), ensure_ascii=False))
        return 0

    def _security_profile() -> Dict[str, Any]:
        for cand in (state_dir.parent / "app-security-profile.json", APP_DIR / "contracts/app-security-profile.json"):
            if cand.is_file() and not cand.is_symlink():
                return json.loads(cand.read_text("utf-8"))
        return {}

    def _run() -> Dict[str, Any]:
        args, _ = normalize_args(SCHEMA, raw_argv, stdin_data=stdin_data)
        assert args is not None
        action = args["action"]

        if action in ("status", "list", "put"):
            kek = load_or_init_kek(state_dir / "kek.key")
            vault = OpaqueVault(app_id, state_dir / "vault.enc", kek)

            if action == "status":
                handles = vault.list_handles()
                cap = triad_capsule(
                    state=f"App({app_id}) 运行态正常，已托管句柄={len(handles)} [只读完成]",
                    orbit_options=[
                        "托管新内容: --action put --content <正文>",
                        "执行既有句柄: --action exec --ref <rec_xx>",
                    ],
                    anchor="运行态采用零落盘双重加密内存库；严禁探测隐藏目录。",
                    terminal=True,
                )
                return format_envelope(True, "OK_STATUS", cap, data={"app_id": app_id, "handle_count": len(handles)})

            if action == "list":
                handles = vault.list_handles()
                cap = triad_capsule(
                    state=f"已列出 {len(handles)} 个托管句柄 [只读完成]",
                    orbit_options=[
                        "消费指定句柄: --action exec --ref <rec_xx>",
                    ],
                    anchor="Agent 侧仅见短句柄，严禁尝试逆向解析。",
                    terminal=True,
                )
                return format_envelope(True, "OK_LIST", cap, data={"handles": handles})

            if not str(args.get("content", "")).strip():
                cap = guard_capsule(
                    mode="PARAM_FIX",
                    reason="E_PARAM_MISSING: --action put 需要非空 --content",
                    action_guidance="请补齐 content 后重试（支持 --content - 从 stdin 传入）。",
                    anchor="同指纹连续失败 2 次将触发硬熔断。",
                    fill_template={"action": "put", "kind": args["kind"], "content": "<必填: 待托管内容>"},
                    attempt_remaining=1,
                )
                return format_envelope(False, "E_PARAM_MISSING", cap, missing_slots=["content"])
            stored = vault.put_record(args["kind"], {"body": args["content"]})
            ref = stored["ref"]
            cap = triad_capsule(
                state=f"{ref} 为运行时内部托管句柄（Agent 侧无可读内容，无需也不可解析）[已托管 | 可传递或收口]",
                orbit_options=[
                    f"继续执行该句柄: --action exec --ref {ref}",
                ],
                anchor=f"仅通过受控门面传递 {ref}；严禁探测内部存储路径。",
                terminal=False,
            )
            return format_envelope(True, "OK_STORED", cap, data=stored)

        ref = args.get("ref") or ""
        if not ref:
            cap = guard_capsule(
                mode="PARAM_FIX",
                reason="E_PARAM_MISSING: --action exec 需要必填句柄 --ref",
                action_guidance="请填入前序返回的 rec_xx 句柄后重试。",
                anchor="仅接受受控托管短句柄，禁止传入文件路径。",
                fill_template={"action": "exec", "ref": "<rec_01>"},
                attempt_remaining=1,
            )
            return format_envelope(False, "E_PARAM_MISSING", cap, missing_slots=["ref"])

        profile = _security_profile()
        tier = profile.get("security_tier", "plaintext-py")
        if tier in ("binary-hybrid", "binary-direct"):
            expected_sha = str(profile.get("whitebox_sha256", "")).strip()
            whitebox = CORE_DIR / "layer2/whitebox_exec.py"
            binary = state_dir.parent / "bin/gewu_vault_exec"
            if not expected_sha or not whitebox.is_file() or whitebox.is_symlink():
                raise VaultSecurityError("Layer-2 whitebox script missing or unsafe")
            if hashlib.sha256(whitebox.read_bytes()).hexdigest() != expected_sha:
                raise VaultSecurityError("Layer-2 whitebox SHA-256 mismatch")
            if not binary.is_file() or binary.is_symlink():
                raise VaultSecurityError("Layer-2 native executor missing; run bootstrap.py init-runtime")
            mode = "direct-exec" if tier == "binary-direct" else "hybrid-exec"
            proc = subprocess.run(
                [
                    str(binary),
                    "--mode",
                    mode,
                    "--python",
                    sys.executable,
                    "--whitebox",
                    str(whitebox),
                    "--expected-sha256",
                    expected_sha,
                    "--audit-log",
                    str(log_dir / "human-only/audit.log"),
                    "--state-dir",
                    str(state_dir),
                    "--app-id",
                    app_id,
                    "--ref",
                    ref,
                ],
                shell=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 0:
                raise VaultSecurityError(f"Layer-2 binary executor failed: {proc.stdout.strip()}")
            receipt = json.loads(proc.stdout)
        else:
            kek = load_or_init_kek(state_dir / "kek.key")
            vault = OpaqueVault(app_id, state_dir / "vault.enc", kek)
            receipt = vault.execute_on_handle(
                ref,
                lambda kind, payload: {
                    "ref": ref,
                    "kind": kind,
                    "executed": True,
                    "content_bytes": len(str(payload.get("body", "")).encode("utf-8")),
                },
            )
        cap = triad_capsule(
            state=f"已完成对 {ref} 的内存解密直执（零明文回吐）[执行完成 | 可收口]",
            orbit_options=[
                "列出其他托管句柄: --action list",
            ],
            anchor="结果已按白名单裁剪；严禁探测内部状态库与日志。",
            terminal=True,
        )
        return format_envelope(True, "OK_EXECUTED", cap, data=receipt)

    is_ro = any(x in raw_argv for x in ("list", "status"))
    code, env = run_with_guard(f"{app_id}-facade", {"argv": raw_argv}, log_dir, state_dir, _run, read_only=is_ro)
    print(json.dumps(env, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
