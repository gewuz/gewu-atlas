"""Isolated unittest scheduling, exact coverage accounting and sanitized timing reports."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import redirect_stdout, redirect_stderr
import io
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import unittest

APP_DIR = Path(__file__).resolve().parents[2]
SELF = Path(__file__).resolve()


def _diagnostic(tag, text):
    # Reuse the domain's protected diagnostics sink; never expose assertion payloads.
    sys.path.insert(0, str(SELF.parent))
    from run_app_tests import _log_private
    _log_private(tag, text)


def _flatten(suite):
    for case in suite:
        if isinstance(case, unittest.TestSuite):
            yield from _flatten(case)
        else:
            yield case


class TimedResult(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.rows = {}
        self.started = {}

    def startTest(self, test):
        super().startTest(test)
        self.started[test.id()] = time.perf_counter()
        self.rows[test.id()] = {"test_id": test.id(), "status": "passed"}

    def _mark(self, test, status):
        self.rows.setdefault(test.id(), {"test_id": test.id()})["status"] = status

    def _failure_location(self, test, err):
        tb = err[2]
        location = None
        while tb:
            path = Path(tb.tb_frame.f_code.co_filename)
            try: rel = path.resolve().relative_to(APP_DIR).as_posix()
            except ValueError: rel = ""
            if rel.startswith(("tests/", "scripts/checks/")):
                location = {"file": rel, "line": tb.tb_lineno}
            tb = tb.tb_next
        if location:
            self.rows[test.id()]["failure_location"] = location
        self.rows[test.id()]["exception_type"] = err[0].__name__

    def addFailure(self, test, err):
        super().addFailure(test, err); self._mark(test, "failed")
        self._failure_location(test, err)

    def addError(self, test, err):
        super().addError(test, err); self._mark(test, "error")
        self._failure_location(test, err)

    def addSkip(self, test, reason):
        super().addSkip(test, reason); self._mark(test, "skipped")

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err); self._mark(test, "expected_failure")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test); self._mark(test, "unexpected_success")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None: self._mark(test, "failed")

    def stopTest(self, test):
        self.rows[test.id()]["elapsed_seconds"] = round(time.perf_counter() - self.started[test.id()], 4)
        super().stopTest(test)


def _worker(tests_dir, test_ids=None):
    sys.path.insert(0, str(APP_DIR / "scripts"))
    sys.path.insert(0, str(tests_dir))
    captured = io.StringIO()
    try:
        with redirect_stdout(captured), redirect_stderr(captured):
            loader = unittest.TestLoader()
            suite = (loader.discover(str(tests_dir), pattern="test_*.py") if test_ids is None
                     else loader.loadTestsFromNames(test_ids))
            if loader.errors:
                _diagnostic("domain_discovery", "\n".join(loader.errors))
                return {"status": "failed", "code": "E_TEST_DISCOVERY"}
            found = [case.id() for case in _flatten(suite)]
            if len(found) != len(set(found)):
                return {"status": "failed", "code": "E_TEST_DUPLICATE_ID"}
            if test_ids is None:
                return {"status": "passed", "test_ids": found}
            if sorted(found) != sorted(test_ids):
                return {"status": "failed", "code": "E_TEST_SELECTION_MISMATCH"}
            result = TimedResult()
            suite.run(result)
            if result.errors or result.failures:
                _diagnostic("domain_failures", "\n".join(raw for _, raw in result.errors + result.failures))
            rows = list(result.rows.values())
            complete = sorted(row["test_id"] for row in rows) == sorted(test_ids) and result.testsRun == len(test_ids)
            return {"status": "passed" if result.wasSuccessful() and complete else "failed",
                    "code": "E_TEST_INCOMPLETE" if not complete else "OK" if result.wasSuccessful() else "E_TEST_CASE_FAILED",
                    "tests_run": result.testsRun, "results": rows}
    except Exception:
        _diagnostic("domain_worker", traceback.format_exc())
        return {"status": "failed", "code": "E_TEST_WORKER"}
    finally:
        _diagnostic("domain_output", captured.getvalue())


def _invoke(tests_dir, test_ids, timeout):
    # Every process receives an independent default runtime and temporary namespace.
    with tempfile.TemporaryDirectory(prefix="atlas-test-worker-") as td:
        root = Path(td).resolve()
        env = dict(os.environ)
        for key in ("GEWU_APP_RUNTIME_DIR", "GEWU_ATLAS_WORKSPACE_ROOT", "GEWU_STATE_HOME"):
            target = root / key.lower(); target.mkdir()
            env[key] = str(target)
        env["TMPDIR"] = str(root)
        cmd = [sys.executable, "-I", "-B", str(SELF), "--worker", "--tests-dir", str(tests_dir)]
        if test_ids is None: cmd.append("--discover")
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env, start_new_session=True)
        try:
            out, err = proc.communicate(json.dumps(test_ids), timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name == "posix": os.killpg(proc.pid, signal.SIGKILL)
            else: proc.kill()
            out, err = proc.communicate()
            _diagnostic("domain_timeout", out + "\n" + err)
            return {"status": "failed", "code": "E_TEST_TIMEOUT", "results": [], "tests_run": 0}
        _diagnostic("domain_worker_stderr", err)
        try: report = json.loads(out)
        except (ValueError, TypeError):
            _diagnostic("domain_worker_output", out)
            return {"status": "failed", "code": "E_TEST_WORKER_OUTPUT", "results": [], "tests_run": 0}
        if not isinstance(report, dict):
            return {"status": "failed", "code": "E_TEST_WORKER_OUTPUT", "results": [], "tests_run": 0}
        if proc.returncode != 0 and report.get("status") == "passed":
            return {"status": "failed", "code": "E_TEST_WORKER_EXIT", "results": [], "tests_run": 0}
        return report


def select_tests(inventory, suite, requested, prefixes):
    if suite != "all" and suite not in prefixes:
        raise ValueError("E_TEST_SUITE_UNKNOWN")
    selected = list(inventory) if suite == "all" else [t for t in inventory if any(t.startswith(p) for p in prefixes[suite])]
    if requested:
        if len(set(requested)) != len(requested) or not set(requested).issubset(selected):
            raise ValueError("E_TEST_SELECTION_UNKNOWN_OR_DUPLICATE")
        selected = [t for t in selected if t in requested]
    if not selected:
        raise ValueError("E_TEST_SELECTION_EMPTY")
    return selected


def run_domain_tests(*, tests_dir, jobs=1, suite="all", requested=(), profile=None, list_only=False, progress=None, timeout=600):
    profile = profile or {}
    started = time.perf_counter()
    discovered = _invoke(tests_dir, None, min(timeout, 60))
    if discovered.get("status") != "passed": return discovered
    inventory = discovered["test_ids"]
    try: selected = select_tests(inventory, suite, requested, profile.get("suite_prefixes", {}))
    except ValueError as exc: return {"status": "failed", "code": str(exc), "tests_run": 0}
    selection_sha = hashlib.sha256(json.dumps(sorted(selected)).encode()).hexdigest()
    base = {"discovered_tests": len(inventory), "selected_tests": len(selected), "test_ids": selected,
            "selection_sha256": selection_sha, "full_coverage": set(selected) == set(inventory), "suite": suite}
    if list_only: return {**base, "status": "listed", "ran": False, "tests_run": 0}
    if jobs < 1 or jobs > 4: return {**base, "status": "failed", "code": "E_TEST_JOBS_RANGE", "tests_run": 0}
    approved = set(profile.get("parallel_safe_test_ids", []))
    safe = [[t] for t in selected if t in approved] if jobs > 1 else []
    serial = [t for t in selected if t not in approved] if jobs > 1 else selected
    hints = profile.get("duration_hints_seconds", {})
    safe.sort(key=lambda ids: (-float(hints.get(ids[0], 0)), ids[0]))
    reports = []
    deadline = started + timeout
    def invoke_budgeted(ids):
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return {"status": "failed", "code": "E_TEST_TIMEOUT", "results": [], "tests_run": 0}
        return _invoke(tests_dir, ids, remaining)
    def completed(ids, report):
        reports.append(report)
        if progress: progress(f"cases {sum(r.get('tests_run', 0) for r in reports)}/{len(selected)}: {report.get('status', 'failed')}")
    if safe:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            pending = {pool.submit(invoke_budgeted, ids): ids for ids in safe}
            for future in as_completed(pending):
                ids = pending[future]
                try: report = future.result()
                except Exception:
                    _diagnostic("domain_scheduler", traceback.format_exc())
                    report = {"status": "failed", "code": "E_TEST_SCHEDULER", "results": [], "tests_run": 0}
                completed(ids, report)
    # Unreviewed tests retain suite/class fixtures and cannot overlap approved parallel cases.
    if serial: completed(serial, invoke_budgeted(serial))
    rows = [r for report in reports for r in report.get("results", [])]
    covered = [r["test_id"] for r in rows]
    complete = sorted(covered) == sorted(selected)
    failures = [r["test_id"] for r in rows if r["status"] in ("failed", "error", "unexpected_success")]
    issues = [r.get("code", "E_TEST_WORKER") for r in reports if r.get("status") != "passed"]
    if not complete: issues.append("E_TEST_COVERAGE_INCOMPLETE")
    return {**base, "ran": True, "jobs": jobs, "parallel_cases": len(safe), "serial_cases": len(serial),
            "tests_run": sum(r.get("tests_run", 0) for r in reports), "failed_tests": failures,
            "skipped": sum(r["status"] == "skipped" for r in rows),
            "skipped_tests": [r["test_id"] for r in rows if r["status"] == "skipped"],
            "worker_errors": issues, "status": "passed" if not issues and not failures and complete else "failed",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "case_timings": sorted(rows, key=lambda r: (-r.get("elapsed_seconds", 0), r["test_id"]))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", required=True)
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--tests-dir", type=Path, required=True)
    args = parser.parse_args()
    ids = None if args.discover else json.load(sys.stdin)
    report = _worker(args.tests_dir.resolve(), ids)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get("status") == "passed" else 2


if __name__ == "__main__":
    sys.exit(main())
