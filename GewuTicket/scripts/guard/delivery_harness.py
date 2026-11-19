#!/usr/bin/env python3
"""Reusable ws-5 Spatial Boundary, Piggyback Gate, Tiered Delivery Evaluation,
and Dual-Source GewuLog Telemetry module for Forge-generated Domains.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_SPRAWL_DIR_RE = re.compile(r"^(?:reports|sources|99-assets|drafts|output|[a-z0-9_-]+-research)$", re.I)
_LEVEL_RANK = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}


def scan_workspace_sprawl(ws_root: Path) -> List[str]:
    """Detect unauthorized research/report/source directories directly under workspace root."""
    sprawl: List[str] = []
    try:
        if not ws_root.is_dir():
            return []
        for child in sorted(ws_root.iterdir(), key=lambda p: p.name):
            name = child.name
            if name.startswith("."):
                continue
            if child.is_dir() and _SPRAWL_DIR_RE.match(name):
                sprawl.append(name)
    except OSError:
        return []
    return sprawl


def attach_qa_gate(
    env: Dict[str, Any],
    *,
    stage: str,
    hit_count: int = 0,
    read_count: int = 0,
    next_cmd_hint: str = "",
) -> None:
    """Attach [QA_GATE: SNIPPET_ONLY | ZERO_HITS | GROUNDED (60/60)] onto read operator capsules."""
    sc = env.get("structuredContent") if isinstance(env.get("structuredContent"), dict) else {}
    cap = sc.get("capsule") if isinstance(sc.get("capsule"), dict) else None
    if not isinstance(cap, dict):
        return
    if stage == "search":
        tag = "[QA_GATE: SNIPPET_ONLY]" if hit_count > 0 else "[QA_GATE: ZERO_HITS]"
        advice = (
            f"严禁仅凭摘要列表拼贴作答！{next_cmd_hint or '请批量实读全文后再融合成文。'}"
            if hit_count > 0
            else "检索 0 命中：若确认无资料须如实声明缺口，严禁凭记忆编造库内事实。"
        )
    else:
        tag = "[QA_GATE: GROUNDED (60/60)]"
        advice = f"已实读 {read_count} 项原文，请熔炼为连贯文章并标注逻辑句柄，零物理路径泄漏。"
    cur_state = str(cap.get("state") or "")
    if tag not in cur_state:
        cap["state"] = f"{cur_state} {tag}".strip()
    cur_anchor = str(cap.get("anchor") or "")
    if tag not in cur_anchor:
        cap["anchor"] = f"{cur_anchor} {tag} {advice}".strip()
    env["reminder"] = (
        "🧭 长牵引绳胶囊 (Triad Capsule)\n"
        f"📍 当前坐标 (STATE): {cap.get('state', '')}\n"
        f"🚀 可选推进 (ORBIT): {' | '.join(cap.get('orbit_options') or [])}\n"
        f"⚓ 物理锚点 (ANCHOR): {cap['anchor']}"
    )
    cap["rendered_text"] = env["reminder"]


def piggyback_delivery_gate(
    *,
    ws_root: Path,
    env: Dict[str, Any],
    extra_issues: Optional[List[str]] = None,
    fix_cmd_hint: str = "",
) -> Dict[str, Any]:
    """Piggyback spatial-boundary & integrity gate onto write operator capsules without requiring Agent self-invocation."""
    sprawl = scan_workspace_sprawl(ws_root)
    issues: List[str] = []
    score = 60
    if sprawl:
        score -= 15
        issues.append(f"工作区根目录存在未授权自建目录: {', '.join(sprawl)}（须移入受控工作台或清理）")
    for iss in (extra_issues or []):
        if iss and str(iss).strip():
            score = max(0, score - 15)
            issues.append(str(iss).strip())
    passed = score == 60 and len(issues) == 0
    gate = {
        "operator_score": score,
        "operator_max": 60,
        "operator_passed": passed,
        "boundary_clean": len(sprawl) == 0,
        "sprawl_items": sprawl,
        "issues": issues,
        "eval_routing": "subagent_if_available_else_self_eval",
        "rubric_40": {
            "completeness": 15,
            "groundedness": 15,
            "readability_and_zero_path": 10,
        },
    }
    sc = env.get("structuredContent") if isinstance(env.get("structuredContent"), dict) else {}
    cap = sc.get("capsule") if isinstance(sc.get("capsule"), dict) else None
    if isinstance(cap, dict):
        old_anchor = str(cap.get("anchor") or "")
        orbit_list = list(cap.get("orbit_options") or [])
        if not passed:
            cap["terminal"] = False
            if fix_cmd_hint and fix_cmd_hint not in orbit_list:
                orbit_list.insert(0, fix_cmd_hint)
            cap["orbit_options"] = orbit_list
            cap["anchor"] = f"🚫 [DELIVERY_GATE: BLOCKED ({score}/60)] {'; '.join(issues)} | {old_anchor}"
        else:
            cap["anchor"] = (
                f"{old_anchor} [DELIVERY_GATE: PASS (60/60) | "
                "L1单步直交；L2/L3收尾若有Subagent能力优先调Subagent盲审，否则按完整性/真实性/可读性自评交付]"
            )
        env["reminder"] = (
            "🧭 长牵引绳胶囊 (Triad Capsule)\n"
            f"📍 当前坐标 (STATE): {cap.get('state', '')}\n"
            f"🚀 可选推进 (ORBIT): {' | '.join(cap.get('orbit_options') or [])}\n"
            f"⚓ 物理锚点 (ANCHOR): {cap['anchor']}"
        )
        cap["rendered_text"] = env["reminder"]
    return gate


def build_tiered_semantic_protocol() -> Dict[str, Any]:
    """Return universal 40-point semantic evaluation rubric (Subagent preferred, self-eval fallback)."""
    return {
        "max_score": 40,
        "pass_total_line": 60,
        "routing_policy": "subagent_if_available_else_self_eval",
        "tiers": {
            "L1": "单步原子读写，算子校验通过即直交（0额外步）",
            "L2": "局部检索/单卡创作，依托胶囊门禁与脑内三维自检交付",
            "L3": "复合研究/多卡变更，算子60分硬评估 + Subagent优先(无则自评)40分语义评估",
        },
        "rubric": [
            {"id": "completeness", "name": "完整性 (Completeness)", "points": 15, "criteria": "对照用户原始请求，核心诉求是否全部落实（无漏项、非半成品列表拼贴）"},
            {"id": "groundedness", "name": "真实性 (Groundedness)", "points": 15, "criteria": "结论是否句句绑定已实读/核验的逻辑句柄；未查到资料是否如实声明缺口"},
            {"id": "readability", "name": "可读性与脱敏 (Readability & Zero-Path)", "points": 10, "criteria": "散文流畅、结构清晰，且全文零本机物理路径泄漏"},
        ],
    }


def active_log_level(ws_root: Path) -> str:
    env_lvl = os.environ.get("GEWU_LOG_LEVEL", "").strip().upper()
    if env_lvl in _LEVEL_RANK:
        return env_lvl
    vol_meta = ws_root / "Volumes" / "GewuLog-data" / "volume.json"
    if vol_meta.is_file():
        try:
            return str(json.loads(vol_meta.read_text("utf-8")).get("default_level") or "INFO").upper()
        except Exception:
            return "INFO"
    return "INFO"


def attach_debug_trace_prompt(env: Dict[str, Any], ws_root: Path) -> None:
    """Only when GewuLog level=DEBUG, prompt Agent to attach forward-looking `--trace 'Signal => Decision'`."""
    if active_log_level(ws_root) != "DEBUG":
        return
    sc = env.get("structuredContent") if isinstance(env.get("structuredContent"), dict) else {}
    cap = sc.get("capsule") if isinstance(sc.get("capsule"), dict) else None
    if isinstance(cap, dict):
        cur_anc = str(cap.get("anchor") or "")
        if "[DEBUG_TRACE" not in cur_anc:
            cap["anchor"] = f"{cur_anc} [DEBUG_TRACE: 下步调用请附带 --trace '触发信号=>当前正向决策'（勿复述错误，仅记前向行动）]"
            if "rendered_text" in cap:
                cap["rendered_text"] = str(cap["rendered_text"]) + " [DEBUG_TRACE: 下步调用请附带 --trace '触发信号=>当前正向决策']"
                env["reminder"] = cap["rendered_text"]


def emit_dual_source_log(
    *,
    ws_root: Path,
    state_dir: Path,
    domain: str,
    action: str,
    rc: int,
    env: Dict[str, Any],
    args: Optional[Dict[str, Any]] = None,
    is_read_only: bool = False,
) -> None:
    """Emit dual-source leveled telemetry to GewuLog:
    - Operator records objective past (`mistake` / `reminder`) silently.
    - Agent (only in DEBUG) records forward-looking `--trace 'Signal => Decision'`.
    """
    try:
        log_vol = ws_root / "Volumes" / "GewuLog-data"
        env_lvl = os.environ.get("GEWU_LOG_LEVEL", "").strip().upper()
        if not (log_vol / "volume.json").is_file() and not env_lvl:
            return
        threshold = _LEVEL_RANK.get(active_log_level(ws_root), 20)
        args = args or {}
        sc = env.get("structuredContent") if isinstance(env.get("structuredContent"), dict) else {}
        cap = sc.get("capsule") if isinstance(sc.get("capsule"), dict) else {}
        code = str(env.get("code") or "")
        anchor = str(cap.get("anchor") or "")
        reason = str(cap.get("state") or "")
        agent_trace = str(args.get("trace") or args.get("thought") or "").strip()

        rem_file = state_dir / f"{domain}-last-reminder.json"
        prior_rem: Dict[str, Any] = {}
        if rem_file.is_file():
            with contextlib.suppress(Exception):
                prior_rem = json.loads(rem_file.read_text("utf-8"))

        state_dir.mkdir(parents=True, exist_ok=True)
        if rc != 0 or "SNIPPET_ONLY" in anchor or "BLOCKED" in anchor or "INPUT_REQUIRED" in str(cap.get("mode", "")):
            rem_file.write_text(
                json.dumps({"prev_action": action, "prev_code": code, "prev_reminder": (reason + " | " + anchor)[:200]}, ensure_ascii=False),
                encoding="utf-8",
            )
        elif rem_file.is_file():
            with contextlib.suppress(OSError):
                rem_file.unlink()

        is_wakeup = bool(prior_rem and rc == 0)
        if rc != 0:
            lvl, ev = ("ERROR" if rc >= 2 else "WARN"), "MISTAKE_FIX"
        elif "BLOCKED" in anchor:
            lvl, ev = "WARN", "GATE_EVAL"
        elif is_wakeup:
            lvl, ev = ("INFO" if agent_trace else "DEBUG"), "REMINDER_WAKEUP"
        elif agent_trace:
            lvl, ev = "DEBUG", "THOUGHT"
        elif not is_read_only:
            lvl, ev = "INFO", "OP_CALL"
        else:
            lvl, ev = "DEBUG", "OP_CALL"

        if _LEVEL_RANK.get(lvl, 10) < threshold:
            return

        log_scripts = ws_root / "GewuLog" / "scripts"
        if not (log_scripts / "log" / "log_engine.py").is_file():
            return
        if str(log_scripts) not in sys.path:
            sys.path.insert(0, str(log_scripts))
        from log.log_engine import record_log_entry

        mistake_str = f"{action} -> {code}: {reason[:120]}" if rc != 0 else (
            f"前态停留在 {prior_rem.get('prev_action')} ({prior_rem.get('prev_code')})" if is_wakeup else ""
        )
        reminder_str = str(cap.get("action_guidance") or anchor)[:160] if rc != 0 else (
            str(prior_rem.get("prev_reminder") or "")[:160] if is_wakeup else ""
        )
        record_log_entry(
            log_vol,
            level=lvl,
            event=ev,
            source="dual" if (agent_trace and (mistake_str or reminder_str)) else ("agent" if agent_trace else "operator"),
            by=str(args.get("by") or args.get("agent") or "ai"),
            domain=domain,
            action_name=action,
            ref=str(args.get("ref") or ""),
            msg=f"[{action}] {code}" + (f" | {agent_trace}" if agent_trace else ""),
            thought=agent_trace,
            mistake=mistake_str,
            reminder=reminder_str,
            action_taken=f"{action} ({code})" if rc == 0 else "",
            force=True,
        )
    except Exception:
        pass
