"""Improved Dual-State Triad / Guard Capsules, Fingerprint Circuit Breaker,
and Traceback-Swallowing Runtime Wrapper (Layer-1 Contract Rail).
"""

from __future__ import annotations

import hashlib
import json
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

DEFAULT_EXIT_ORBIT = "流程阻断或本轮目标已满足: 直接向人类汇报现状即可"
_CORE_LEAK_TOKENS = (
    "SQLite format 3",
    "enc:v1:",
    "PRAGMA key",
    "vault.enc",
    ".vault_kek",
    "kek.key",
    ".local/state",
    ".local/logs",
)


def needs_stdin(argv: Sequence[str]) -> bool:
    """Return True only when caller explicitly requests stdin via '-' or '--json-stdin'."""
    return any(
        tok == "-" or (tok.startswith("--") and tok.endswith("=-")) or tok in ("--json-stdin", "--json_stdin")
        for tok in argv
    )


def assert_zero_path_leak(payload: Any, *, forbidden_fragments: Sequence[str] = ()) -> None:
    """Raise ValueError if serialized payload leaks protected internal paths or secrets."""
    dumped = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, sort_keys=True)
    lowered = dumped.casefold()
    for token in _CORE_LEAK_TOKENS:
        if token.casefold() in lowered:
            raise ValueError(f"E_HALT_PATH_LEAK: output violated Handle-First leak gate ({token})")
    for frag in forbidden_fragments:
        tok = str(frag).strip()
        if tok and tok.casefold() in lowered:
            raise ValueError("E_HALT_PATH_LEAK: output leaked restricted filesystem path fragment")


def _canonical_digest(skill_name: str, args: Dict[str, Any]) -> str:
    payload = json.dumps(
        {"skill": skill_name, "args": args},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _atomic_write_0600(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".tmp-{os.getpid()}-{path.name}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class CircuitBreaker:
    """Host/facade-side fingerprint circuit breaker preventing retry loops."""

    def __init__(self, state_file: Path, max_fail_streak: int = 2, max_readonly_streak: int = 3) -> None:
        self.state_file = state_file
        self.max_fail_streak = max_fail_streak
        self.max_readonly_streak = max_readonly_streak

    def _load(self) -> Dict[str, Any]:
        if not self.state_file.exists():
            return {"last_fp": None, "fail_streak": 0, "readonly_streak": 0, "failures": {}}
        try:
            data = json.loads(self.state_file.read_text("utf-8"))
            if isinstance(data, dict):
                data.setdefault("failures", {})
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {"last_fp": None, "fail_streak": 0, "readonly_streak": 0, "failures": {}}

    def _save(self, data: Dict[str, Any]) -> None:
        raw = (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        _atomic_write_0600(self.state_file, raw)

    def check_before(self, skill_name: str, normalized_args: Dict[str, Any], read_only: bool = False) -> Optional[str]:
        fp = _canonical_digest(skill_name, normalized_args)
        data = self._load()
        if data["failures"].get(fp, 0) >= self.max_fail_streak:
            return fp
        if read_only and data.get("last_fp") == fp and data.get("readonly_streak", 0) >= self.max_readonly_streak:
            return fp
        return None

    def record_outcome(
        self,
        skill_name: str,
        normalized_args: Dict[str, Any],
        ok: bool,
        read_only: bool = False,
    ) -> int:
        """Record execution result; returns remaining failure attempts for this fingerprint."""
        fp = _canonical_digest(skill_name, normalized_args)
        data = self._load()
        failures = data.setdefault("failures", {})
        if ok:
            failures.pop(fp, None)
            if read_only:
                if data.get("last_fp") == fp:
                    data["readonly_streak"] = data.get("readonly_streak", 0) + 1
                else:
                    data["readonly_streak"] = 1
            else:
                data["readonly_streak"] = 0
            data["last_fp"] = fp
            self._save(data)
            return self.max_fail_streak
        count = int(failures.get(fp, 0)) + 1
        failures[fp] = count
        data["last_fp"] = fp
        data["readonly_streak"] = 0
        self._save(data)
        return max(0, self.max_fail_streak - count)


def triad_capsule(
    state: str,
    orbit_options: List[str],
    anchor: str,
    terminal: bool = False,
) -> Dict[str, Any]:
    """Build dual-track Triad Capsule ensuring a natural exit branch is always present."""
    options = [opt.strip() for opt in orbit_options if opt.strip()]
    if not any("汇报" in opt or "结束" in opt or "收口" in opt for opt in options):
        options.append(DEFAULT_EXIT_ORBIT)
    short_anchor = anchor.strip()[:80]
    rendered = "\n".join(
        [
            "🧭 长牵引绳胶囊 (Triad Capsule)",
            f"📍 当前坐标 (STATE): {state}",
            f"🚀 可选推进 (ORBIT): {' | '.join(options)}",
            f"⚓ 物理锚点 (ANCHOR): {short_anchor}",
        ]
    )
    return {
        "mode": "TRIAD",
        "state": state,
        "terminal": terminal,
        "orbit_options": options,
        "fill_template": None,
        "attempt_remaining": None,
        "anchor": short_anchor,
        "rendered_text": rendered,
    }


def guard_capsule(
    mode: str,
    reason: str,
    action_guidance: str,
    anchor: str,
    fill_template: Optional[Dict[str, Any]] = None,
    attempt_remaining: Optional[int] = None,
    command_hint: Optional[str] = None,
    exit_code: int = 2,
) -> Dict[str, Any]:
    """Build 3-tier Guard Capsule (PARAM_FIX | INPUT_REQUIRED | HALTED)."""
    if mode not in ("PARAM_FIX", "INPUT_REQUIRED", "HALTED"):
        raise ValueError("guard mode must be PARAM_FIX, INPUT_REQUIRED, or HALTED")
    short_anchor = anchor.strip()[:80]
    if mode == "PARAM_FIX":
        rem = 0 if attempt_remaining is None else max(0, attempt_remaining)
        header = f"❌ 中断拦截 (GUARD: PARAM_FIX | 剩余重试: {rem}): {reason}"
        lines = ["🛡️ 门禁熔断胶囊 (Guard Capsule)", header]
        if command_hint:
            lines.append(f"📝 最小填空模板: {command_hint}")
        lines.append(f"🎯 行动指引 (ACTION): {action_guidance}")
        lines.append(f"⚓ 安全底线 (ANCHOR): {short_anchor}")
    elif mode == "INPUT_REQUIRED":
        lines = [
            "🛡️ 门禁熔断胶囊 (Guard Capsule)",
            f"⏸️ 等待人工输入 (GUARD: INPUT_REQUIRED): {reason}",
            f"🎯 行动指引 (ACTION): {action_guidance}",
            f"⚓ 安全底线 (ANCHOR): {short_anchor}",
        ]
    else:
        lines = [
            "🛡️ 门禁熔断胶囊 (Guard Capsule)",
            f"❌ 中断拦截 (GUARD: HALTED | 禁止重试 | Exit Code: {exit_code}): {reason}",
            f"🎯 行动指引 (ACTION): {action_guidance}",
            f"⚓ 安全底线 (ANCHOR): {short_anchor}",
        ]
    return {
        "mode": mode,
        "state": reason,
        "terminal": mode == "HALTED",
        "orbit_options": [],
        "fill_template": fill_template,
        "attempt_remaining": attempt_remaining,
        "anchor": short_anchor,
        "rendered_text": "\n".join(lines),
    }


def append_hidden_error_log(log_dir: Path, skill_name: str, exc: BaseException) -> None:
    """Write full traceback exclusively to hidden .local/logs/ for human diagnostics."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "runtime-errors.log"
    ts = datetime.now(timezone.utc).isoformat()
    entry = f"[{ts}] skill={skill_name} error={type(exc).__name__}: {exc}\n{traceback.format_exc()}\n"
    fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, entry.encode("utf-8"))
    finally:
        os.close(fd)


def format_envelope(
    ok: bool,
    code: str,
    capsule: Dict[str, Any],
    data: Optional[Dict[str, Any]] = None,
    missing_slots: Optional[List[str]] = None,
    fill_template: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create MCP-v2-aligned dual-track JSON response envelope."""
    envelope: Dict[str, Any] = {
        "ok": ok,
        "code": code,
        "isError": not ok,
        "data": data or {},
        "structuredContent": {
            "capsule": capsule,
        },
        "reminder": capsule["rendered_text"],
    }
    if missing_slots:
        envelope["missing_slots"] = missing_slots
    if fill_template is not None:
        envelope["fill_template"] = fill_template
    return envelope


def preview_confirm_gate(
    *,
    code_prefix: str,
    target_label: str,
    preview_items: List[Any],
    affected_count: Optional[int] = None,
    apply: bool = False,
    expected_sha256: str = "",
    actual_sha256: str = "",
    confirm_hint: str = "请向用户展示以上影响预览后，附带 --apply true 重新执行以完成变更。",
    anchor: str = "GUARD: INPUT_REQUIRED | 需人类批准后执行受控写入",
) -> Optional[tuple[int, Dict[str, Any]]]:
    """Standardized Preview -> INPUT_REQUIRED -> --apply true + optimistic-hash gate.

    Returns ``None`` when the caller is clear to commit atomically; otherwise returns
    ``(exit_code, envelope)`` with zero disk mutations performed.
    """
    exp = (expected_sha256 or "").strip()
    act = (actual_sha256 or "").strip()
    if apply and exp and act and exp != act:
        cap = guard_capsule(
            mode="PARAM_FIX",
            reason=f"{code_prefix}_STALE: 目标「{target_label}」自预览后内容已发生变化，已中止写入防误伤",
            action_guidance="请重新执行预览获取最新影响范围与校验摘要后再提交。",
            anchor="乐观锁摘要校验不通过，未改动任何文件。",
            attempt_remaining=1,
        )
        return 2, format_envelope(
            False,
            f"{code_prefix}_STALE",
            cap,
            data={"target": target_label, "expected_sha256": exp, "actual_sha256": act},
        )

    count = len(preview_items) if affected_count is None else int(affected_count)
    if count > 0 and not apply:
        confirm_code = f"{code_prefix}_CONFIRM"
        cap = guard_capsule(
            mode="INPUT_REQUIRED",
            reason=f"{confirm_code}: 「{target_label}」涉及 {count} 处影响项，已冻结预览等待人类确认（未修改任何文件）",
            action_guidance=confirm_hint,
            anchor=anchor,
        )
        data: Dict[str, Any] = {
            "requires_approval": True,
            "target": target_label,
            "affected_count": count,
            "preview_items": preview_items[:50],
        }
        if act:
            data["expected_sha256"] = act
        return 1, format_envelope(False, confirm_code, cap, data=data)

    return None


def run_with_guard(
    skill_name: str,
    raw_args_for_fp: Dict[str, Any],
    log_dir: Path,
    state_dir: Path,
    fn: Callable[[], Dict[str, Any]],
    read_only: bool = False,
) -> tuple[int, Dict[str, Any]]:
    """Execute handler inside traceback-swallowing & circuit-breaker guard."""
    from .param_normalizer import ParamError  # local relative import inside app_core

    breaker = CircuitBreaker(state_dir / "circuit_breaker.json")
    tripped_fp = breaker.check_before(skill_name, raw_args_for_fp, read_only=read_only)
    if tripped_fp is not None:
        cap = guard_capsule(
            mode="HALTED",
            reason="触发连续同参失败或无变更轮询硬熔断（E_HALT_LOOP_DETECTED）",
            action_guidance="立即停止重试与换参绕试；向人类汇报拦截原因与当前坐标。",
            anchor="生产异常或熔断停止后续写入；严禁探测或改写底层源码与状态库。",
            exit_code=2,
        )
        return 2, format_envelope(False, "E_HALT_LOOP_DETECTED", cap)

    try:
        result = fn()
        breaker.record_outcome(skill_name, raw_args_for_fp, ok=True, read_only=read_only)
        return 0, result
    except ParamError as exc:
        remaining = breaker.record_outcome(skill_name, raw_args_for_fp, ok=False, read_only=read_only)
        if remaining <= 0:
            cap = guard_capsule(
                mode="HALTED",
                reason=f"{exc.code}: {exc.message}（重试配额已归零，已升格硬熔断）",
                action_guidance="立即停止重试；向人类用户汇报当前参数缺口或契约冲突。",
                anchor="同参数指纹连续失败 2 次触发硬熔断，禁止继续试错。",
                exit_code=2,
            )
            return 2, format_envelope(False, "E_HALT_LOOP_DETECTED", cap)
        cap = guard_capsule(
            mode="PARAM_FIX",
            reason=f"{exc.code}: {exc.message}，本次未发生状态写入。",
            action_guidance="仅可按 fill_template 补齐或修正对应槽位后重调（或传 --template 领空表）；严禁自造参数或改用原生 Shell。",
            anchor="相同参数指纹连续失败 2 次将触发硬熔断并禁止继续尝试。",
            fill_template=exc.fill_template or None,
            attempt_remaining=remaining,
        )
        return 2, format_envelope(
            False,
            exc.code,
            cap,
            missing_slots=exc.missing_slots,
            fill_template=exc.fill_template or None,
        )
    except Exception as exc:  # noqa: BLE001 - intentional outer boundary swallowing
        append_hidden_error_log(log_dir, skill_name, exc)
        breaker.record_outcome(skill_name, raw_args_for_fp, ok=False, read_only=read_only)
        cap = guard_capsule(
            mode="HALTED",
            reason="运行时内部受控异常（E_HALT_INTERNAL），堆栈已脱敏转存本机诊断日志。",
            action_guidance="立即停止重试；向人类汇报 E_HALT_INTERNAL，严禁探查内部目录或试图修改源码。",
            anchor="异常现场已保留，未授权严禁读取内部日志或状态库。",
            exit_code=2,
        )
        return 2, format_envelope(False, "E_HALT_INTERNAL", cap)
