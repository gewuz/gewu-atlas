"""Thin Agent-facing facade injected into every Forge-generated App.

Supports both in-workspace hidden core (scripts/app_core/) and out-of-workspace
migrated runtime (via .runtime/<app_id>/runtime-location.json). Zero SQL or keys.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[2]
ROOT_DIR = APP_DIR.parent


def _resolve_timeout_seconds() -> int:
    for meta_path in (APP_DIR / "contracts/cli-manifest.json", APP_DIR / "app.json"):
        if meta_path.is_file():
            try:
                raw = json.loads(meta_path.read_text("utf-8")).get("facade_timeout_seconds")
                if raw is not None:
                    return max(5, min(int(raw), 600))
            except Exception:  # noqa: BLE001
                pass
    return 25


def _resolve_worker_and_env() -> tuple[Path, dict]:
    app_meta = json.loads((APP_DIR / "app.json").read_text("utf-8"))
    app_id = app_meta["app_id"]
    loc_file = ROOT_DIR / ".runtime" / app_id / "runtime-location.json"
    env = dict(os.environ)
    env["GEWU_APP_ID"] = app_id
    if loc_file.exists():
        loc = json.loads(loc_file.read_text("utf-8"))
        worker = Path(loc["core_dir"]) / "app_worker.py"
        env["GEWU_APP_RUNTIME_DIR"] = str(loc["runtime_dir"])
        return worker, env
    return APP_DIR / "scripts" / "app_core" / "app_worker.py", env


def _needs_stdin(argv: list[str]) -> bool:
    return any(
        tok == "-" or (tok.startswith("--") and tok.endswith("=-")) or tok in ("--json-stdin", "--json_stdin")
        for tok in argv
    )


def main() -> int:
    try:
        worker, env = _resolve_worker_and_env()
        argv = sys.argv[1:]
        sub_io = {"input": sys.stdin.read()} if _needs_stdin(argv) and not sys.stdin.isatty() else {"stdin": subprocess.DEVNULL}
        proc = subprocess.run(
            [sys.executable, "-I", "-B", str(worker), *argv],
            capture_output=True,
            text=True,
            shell=False,
            env=env,
            timeout=_resolve_timeout_seconds(),
            **sub_io,
        )
        if proc.stdout.strip():
            sys.stdout.write(proc.stdout)
            return proc.returncode
        sys.stdout.write(
            json.dumps(
                {
                    "ok": False,
                    "code": "E_HALT_INTERNAL",
                    "isError": True,
                    "reminder": "🛡️ 门禁熔断胶囊 (Guard Capsule)\n❌ 中断拦截 (GUARD: HALTED | 禁止重试 | Exit Code: 2): 受控执行器未返回结构化回执。\n🎯 行动指引 (ACTION): 立即停止重试并向人类汇报。",
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        return 2
    except Exception:  # noqa: BLE001 - swallow all tracebacks at facade boundary
        sys.stdout.write(
            json.dumps(
                {
                    "ok": False,
                    "code": "E_HALT_INTERNAL",
                    "isError": True,
                    "reminder": "🛡️ 门禁熔断胶囊 (Guard Capsule)\n❌ 中断拦截 (GUARD: HALTED | 禁止重试 | Exit Code: 2): 门面调用异常。\n🎯 行动指引 (ACTION): 立即停止重试并向人类汇报。",
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
