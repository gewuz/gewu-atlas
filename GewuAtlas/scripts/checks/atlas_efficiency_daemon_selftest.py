#!/usr/bin/env python3
"""End-to-end selftest & security counterexample matrix for GewuAtlas Hot Daemon, P0/P1 operators & MCP."""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
APP_DIR = SCRIPTS_DIR.parent
CLI_PATH = APP_DIR / "scripts/facade/atlas_cli.py"
DAEMON_PATH = APP_DIR / "scripts/facade/atlas_daemon.py"
MCP_PATH = APP_DIR / "scripts/facade/atlas_mcp_server.py"
TICKET_CLI = APP_DIR.parent / "GewuTicket/scripts/ticket/ticket_cli.py"


def _run(
    script: Path,
    args: List[str],
    *,
    env: Dict[str, str],
    stdin_text: Optional[str] = None,
    timeout: float = 30.0,
) -> Tuple[int, Dict[str, Any], str]:
    proc = subprocess.run(
        [sys.executable, "-I", "-B", str(script), *args],
        cwd=str(APP_DIR.parent),
        env=env,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    out = proc.stdout.strip()
    parsed: Dict[str, Any] = {}
    if out:
        last_line = out.splitlines()[-1]
        try:
            parsed = json.loads(last_line)
        except Exception:
            parsed = {"_raw": out}
    return proc.returncode, parsed, proc.stderr


def _assert_no_leaks(raw_obj: Any, forbidden: List[str], label: str) -> None:
    text = json.dumps(raw_obj, ensure_ascii=False)
    for tok in forbidden:
        if tok and tok in text:
            raise AssertionError(f"[{label}] leaked forbidden token: {tok!r}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="atlas-eff-selftest-") as td:
        root = Path(td).resolve()
        runtime_dir = root / "runtime"
        ticket_vol = root / "ticket_vol"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (root / "Volumes" / "GewuAtlas-data").mkdir(parents=True, exist_ok=True)

        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(root),
            "GEWU_ATLAS_WORKSPACE_ROOT": str(root),
            "GEWU_APP_RUNTIME_DIR": str(runtime_dir),
            "GEWU_TICKET_VOLUME": str(ticket_vol),
            "GEWU_SESSION_ID": "eff-selftest-sess",
        }
        if TICKET_CLI.is_file():
            subprocess.run(
                [sys.executable, "-I", "-B", str(TICKET_CLI), "--action", "init-volume", "--volume", str(ticket_vol), "--mount-mode", "rw"],
                cwd=str(APP_DIR.parent),
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )

        forbidden_leaks = [str(root), "GewuAtlas-data", "atlas-daemon.sock", "atlas-daemon.meta.json"]

        # 1. Create test vault & project
        rc, res, err = _run(CLI_PATH, ["--action", "vault-create", "--title", "EffTestVault", "--no_daemon", "true"], env=env)
        assert rc == 0 and res.get("ok"), f"vault-create failed: {res} | {err}"
        vref = str(res["data"]["ref"])
        _assert_no_leaks(res, forbidden_leaks, "vault-create")

        rc, res, err = _run(CLI_PATH, ["--action", "project-create", "--ref", vref, "--title", "PerfStudy", "--agent", "tester", "--no_daemon", "true"], env=env)
        assert rc == 0 and res.get("ok"), f"project-create failed: {res} | {err}"
        pref = str(res["data"]["project_ref"])

        # 2. Create notes: draft in workbench, library note, MOC with managed block, journal
        long_body = (
            "# 性能设计报告\n\n"
            "## 第一章 背景\n\n"
            "背景段落包含 TOKEN_ALPHA_001 与重复词 DUP_WORD DUP_WORD。\n\n"
            "## 第二章 核心方案\n\n"
            "原核心方案描述。\n\n"
            "## 第三章 结论\n\n"
            "原结论内容。\n"
        )
        rc, res, err = _run(
            CLI_PATH,
            [
                "--action", "note-create", "--ref", vref, "--project_ref", pref,
                "--zone", "workbench", "--subdir", "ws-tester/drafts",
                "--title", "性能设计报告", "--summary", "草案", "--content", long_body,
                "--no_daemon", "true",
            ],
            env=env,
        )
        assert rc == 0 and res.get("ok"), f"note-create draft failed: {res} | {err}"
        draft_ref = str(res["data"]["note_ref"])
        # Verify workbench draft uses dirty_only delivery gate!
        assert res["data"]["delivery_gate"]["scope"] == "dirty_only", f"expected dirty_only gate: {res['data']['delivery_gate']}"
        assert res["data"]["delivery_gate"]["operator_passed"] is True

        rc, res, err = _run(
            CLI_PATH,
            [
                "--action", "note-create", "--ref", vref, "--project_ref", pref,
                "--zone", "library", "--doc_type", "library",
                "--title", "一手只读文献", "--summary", "只读文献", "--content", "# 原文\n\n不可被 patch 修改。\n",
                "--no_daemon", "true",
            ],
            env=env,
        )
        assert rc == 0 and res.get("ok"), f"note-create library failed: {res} | {err}"
        lib_ref = str(res["data"]["note_ref"])

        moc_body = (
            "# 主题地图\n\n"
            "顶部综述。\n\n"
            "<!-- atlas:moc-index:start -->\n- 自动索引条目\n<!-- atlas:moc-index:end -->\n\n"
            "## 扩展建议\n\n待补充。\n"
        )
        rc, res, err = _run(
            CLI_PATH,
            [
                "--action", "note-create", "--ref", vref, "--project_ref", pref,
                "--zone", "moc", "--doc_type", "moc",
                "--title", "主题地图", "--summary", "测试 MOC", "--content", moc_body,
                "--no_daemon", "true",
            ],
            env=env,
        )
        assert rc == 0 and res.get("ok"), f"note-create moc failed: {res} | {err}"
        moc_ref = str(res["data"]["note_ref"])

        # 3. Test note-patch full matrix
        # 3a. Unique replace
        rc, res, _ = _run(
            CLI_PATH,
            ["--action", "note-patch", "--ref", vref, "--note_ref", draft_ref, "--old_text", "TOKEN_ALPHA_001", "--new_text", "TOKEN_ALPHA_002", "--no_daemon", "true"],
            env=env,
        )
        assert rc == 0 and res.get("ok"), f"note-patch replace failed: {res}"
        assert res["data"]["delivery_gate"]["scope"] == "dirty_only"

        # 3b. Ambiguous replace rejected, then replace_all succeeds
        rc, res, _ = _run(
            CLI_PATH,
            ["--action", "note-patch", "--ref", vref, "--note_ref", draft_ref, "--old_text", "DUP_WORD", "--new_text", "FIXED_WORD", "--no_daemon", "true"],
            env=env,
        )
        assert rc != 0 and res.get("code") == "E_PATCH_AMBIGUOUS", f"expected E_PATCH_AMBIGUOUS: {res}"

        rc, res, _ = _run(
            CLI_PATH,
            ["--action", "note-patch", "--ref", vref, "--note_ref", draft_ref, "--old_text", "DUP_WORD", "--new_text", "FIXED_WORD", "--replace_all", "true", "--no_daemon", "true"],
            env=env,
        )
        assert rc == 0 and res.get("ok"), f"replace_all failed: {res}"

        # 3c. Section replacement preserving surrounding sections
        rc, res, _ = _run(
            CLI_PATH,
            ["--action", "note-patch", "--ref", vref, "--note_ref", draft_ref, "--section", "第二章 核心方案", "--content", "新核心方案：自托管热内核。", "--no_daemon", "true"],
            env=env,
        )
        assert rc == 0 and res.get("ok"), f"section patch failed: {res}"

        # 3d. Append mode
        rc, res, _ = _run(
            CLI_PATH,
            ["--action", "note-patch", "--ref", vref, "--note_ref", draft_ref, "--mode", "append", "--content", "## 附录\n\n追加内容。", "--no_daemon", "true"],
            env=env,
        )
        assert rc == 0 and res.get("ok"), f"append patch failed: {res}"

        # 3e. Atomic ops_json failure leaves file untouched
        rc, before_get, _ = _run(CLI_PATH, ["--action", "note-get", "--ref", vref, "--note_ref", draft_ref, "--no_daemon", "true"], env=env)
        assert "TOKEN_ALPHA_002" in before_get["data"]["content"]
        assert "新核心方案：自托管热内核。" in before_get["data"]["content"]
        assert "原结论内容。" in before_get["data"]["content"]
        assert "追加内容。" in before_get["data"]["content"]
        sha_before = before_get["data"]["content_sha256"]

        bad_ops = json.dumps([
            {"op": "replace", "old_text": "原结论内容。", "new_text": "不应落盘"},
            {"op": "replace", "old_text": "NON_EXISTENT_NEEDLE", "new_text": "x"},
        ], ensure_ascii=False)
        rc, res, _ = _run(CLI_PATH, ["--action", "note-patch", "--ref", vref, "--note_ref", draft_ref, "--ops_json", bad_ops, "--no_daemon", "true"], env=env)
        assert rc != 0 and res.get("code") == "E_PATCH_NOT_FOUND", f"expected E_PATCH_NOT_FOUND: {res}"
        rc, after_fail_get, _ = _run(CLI_PATH, ["--action", "note-get", "--ref", vref, "--note_ref", draft_ref, "--no_daemon", "true"], env=env)
        assert after_fail_get["data"]["content_sha256"] == sha_before, "failed ops_json mutated disk!"

        # 3f. Managed block protection on MOC
        rc, res, _ = _run(
            CLI_PATH,
            ["--action", "note-patch", "--ref", vref, "--note_ref", moc_ref, "--old_text", "自动索引条目", "--new_text", "篡改", "--no_daemon", "true"],
            env=env,
        )
        assert rc != 0 and res.get("code") == "E_MANAGED_BLOCK_PROTECTED", f"expected E_MANAGED_BLOCK_PROTECTED: {res}"

        # 3g. Read-only library protection
        rc, res, _ = _run(
            CLI_PATH,
            ["--action", "note-patch", "--ref", vref, "--note_ref", lib_ref, "--old_text", "不可被 patch 修改", "--new_text", "篡改", "--no_daemon", "true"],
            env=env,
        )
        assert rc != 0 and res.get("code") == "E_READONLY_LIBRARY", f"expected E_READONLY_LIBRARY: {res}"

        # 3h. Journal human-section protection
        rc, j_res, _ = _run(CLI_PATH, ["--action", "journal-today", "--ref", vref, "--no_daemon", "true"], env=env)
        assert rc == 0 and j_res.get("ok"), f"journal-today failed: {j_res}"
        j_ref = str(j_res["data"]["journal_ref"])
        rc, res, _ = _run(
            CLI_PATH,
            ["--action", "note-patch", "--ref", vref, "--note_ref", j_ref, "--section", "人类记录", "--content", "试图篡改人类区", "--no_daemon", "true"],
            env=env,
        )
        assert rc != 0 and res.get("code") == "E_JOURNAL_PROTECTED", f"expected E_JOURNAL_PROTECTED: {res}"

        # 4. Dirty-only vs Full-vault delivery gate isolation test
        # Create an atomic note with an intentional dangling wikilink
        rc, bad_atom, _ = _run(
            CLI_PATH,
            [
                "--action", "note-create", "--ref", vref, "--project_ref", pref,
                "--zone", "atomics", "--doc_type", "atomic",
                "--title", "含历史断链卡", "--summary", "测试历史断链", "--content", "指向 [[不存在的历史卡片XYZ]]。",
                "--no_daemon", "true",
            ],
            env=env,
        )
        assert rc == 0 and bad_atom["data"]["delivery_gate"]["scope"] == "full_vault"
        assert bad_atom["data"]["delivery_gate"]["operator_passed"] is False
        bad_atom_ref = str(bad_atom["data"]["note_ref"])

        # Now patch clean workbench draft -> dirty_only must PASS (60/60) despite historical broken link!
        rc, clean_patch, _ = _run(
            CLI_PATH,
            ["--action", "note-patch", "--ref", vref, "--note_ref", draft_ref, "--old_text", "原结论内容。", "--new_text", "已更新结论。", "--no_daemon", "true"],
            env=env,
        )
        assert rc == 0 and clean_patch["data"]["delivery_gate"]["scope"] == "dirty_only"
        assert clean_patch["data"]["delivery_gate"]["operator_passed"] is True, f"dirty_only should pass on clean draft: {clean_patch['data']['delivery_gate']}"

        # Fix the historical broken link so subsequent full_vault gates are green
        rc, _, _ = _run(
            CLI_PATH,
            ["--action", "note-patch", "--ref", vref, "--note_ref", bad_atom_ref, "--old_text", "[[不存在的历史卡片XYZ]]", "--new_text", f"[[{moc_ref}]]", "--no_daemon", "true"],
            env=env,
        )
        assert rc == 0

        # 5. Test batch operator (interpolation + unified undo + forbidden action check)
        batch_payload = {
            "action": "batch",
            "ref": vref,
            "project_ref": pref,
            "by": "tester",
            "no_daemon": True,
            "steps": [
                {
                    "id": "a1",
                    "action": "note-create",
                    "args": {"zone": "workbench", "subdir": "ws-tester/drafts", "title": "批处理卡A", "summary": "A", "content": "批处理内容 A"},
                },
                {
                    "id": "a2",
                    "action": "note-create",
                    "args": {"zone": "workbench", "subdir": "ws-tester/drafts", "title": "批处理卡B", "summary": "B", "content": "关联引用 [[$a1.note_ref]]"},
                },
                {
                    "id": "p1",
                    "action": "note-patch",
                    "args": {"note_ref": "$a1.note_ref", "mode": "append", "content": "补充行 BY_BATCH"},
                },
            ],
        }
        rc, b_res, err = _run(CLI_PATH, ["--json-stdin", "-"], env=env, stdin_text=json.dumps(batch_payload, ensure_ascii=False))
        assert rc == 0 and b_res.get("ok"), f"batch failed: {b_res} | {err}"
        assert b_res["data"]["succeeded"] == 3
        a1_ref = b_res["data"]["results"][0]["data"]["note_ref"]
        chg_id = b_res["data"].get("change_id")
        assert chg_id, f"batch should produce unified change_id: {b_res['data']}"

        # Verify forbidden action blocked in batch
        bad_batch = {
            "action": "batch",
            "ref": vref,
            "no_daemon": True,
            "steps": [{"id": "x", "action": "vault-delete", "args": {}}],
        }
        rc, res, _ = _run(CLI_PATH, ["--json-stdin", "-"], env=env, stdin_text=json.dumps(bad_batch, ensure_ascii=False))
        assert rc != 0 and res.get("code") == "E_BATCH_ACTION_FORBIDDEN", f"expected E_BATCH_ACTION_FORBIDDEN: {res}"

        # 5b. Regression: apostrophe in legacy slice ref + sanitized new slice name + native JSON list provenance in batch
        sys.path.insert(0, str(SCRIPTS_DIR))
        from notes.atlas_yamlmini import normalize_provenance_refs

        apos_refs = normalize_provenance_refs("[[URL-demo-05-Removing_files_from_a_repository's_history]]")
        assert apos_refs == ["[[URL-demo-05-Removing_files_from_a_repository's_history]]"]
        rc, lib_apos, _ = _run(
            CLI_PATH,
            [
                "--action", "note-create", "--ref", vref, "--project_ref", pref,
                "--zone", "library", "--doc_type", "library",
                "--title", "GitHubLargeFiles", "--summary", "s",
                "--content", "# GitHubLargeFiles\n\n## Removing files from a repository's history, {% data %}\n\n" + ("Detail text. " * 80) + "\n\n## Second Topic\n\n" + ("More detail. " * 80),
                "--no_daemon", "true",
            ],
            env=env,
        )
        assert rc == 0 and lib_apos.get("ok")
        lib_apos_ref = lib_apos["data"]["note_ref"]
        rc, slc_apos, _ = _run(
            CLI_PATH,
            ["--action", "note-slice", "--ref", vref, "--project_ref", pref, "--note_ref", lib_apos_ref, "--adaptive", "true", "--no_daemon", "true"],
            env=env,
        )
        assert rc == 0 and slc_apos.get("ok")
        toc_apos = slc_apos["data"]["toc_note_ref"]
        slc_apos_ref = slc_apos["data"]["slices"][0]["note_ref"]
        rc, slc_info, _ = _run(CLI_PATH, ["--action", "note-get", "--ref", vref, "--note_ref", slc_apos_ref, "--no_daemon", "true"], env=env)
        assert rc == 0 and not any(ch in slc_info["data"]["note_name"] for ch in ("'", ",", "{", "}", "%")), f"unsanitized slice name: {slc_info['data']['note_name']}"
        # Second library doc to test multi-source provenance + multiple atomic-extract steps in one batch
        rc, lib_apos2, _ = _run(
            CLI_PATH,
            [
                "--action", "note-create", "--ref", vref, "--project_ref", pref,
                "--zone", "library", "--doc_type", "library",
                "--title", "ObsidianSyncSpec", "--summary", "s2",
                "--content", "# ObsidianSyncSpec\n\n## Sync Limits & E2EE\n\n" + ("Sync details. " * 80) + "\n\n## Conflict Model\n\n" + ("Conflict text. " * 80),
                "--no_daemon", "true",
            ],
            env=env,
        )
        assert rc == 0 and lib_apos2.get("ok")
        lib_apos2_ref = lib_apos2["data"]["note_ref"]
        rc, slc_apos2, _ = _run(
            CLI_PATH,
            ["--action", "note-slice", "--ref", vref, "--project_ref", pref, "--note_ref", lib_apos2_ref, "--adaptive", "true", "--no_daemon", "true"],
            env=env,
        )
        assert rc == 0 and slc_apos2.get("ok")
        toc_apos2 = slc_apos2["data"]["toc_note_ref"]
        slc_apos2_ref = slc_apos2["data"]["slices"][0]["note_ref"]

        prov_batch = {
            "action": "batch",
            "ref": vref,
            "project_ref": pref,
            "by": "tester",
            "no_daemon": True,
            "steps": [
                {
                    "id": "ext1",
                    "action": "atomic-extract",
                    "args": {
                        "note_ref": toc_apos,
                        "epistemic_type": "OBJ",
                        "title": "原生Provenance批量萃取验证",
                        "summary": "验证 batch 原生 JSON list provenance 与撇号兼容",
                        "tags": ["Atom", "Test"],
                        "content": "## 1. 核心断言\n\n验证通过。",
                        "provenance": [{"source_ref": lib_apos_ref, "toc_ref": toc_apos, "slice_refs": [slc_apos_ref]}],
                    },
                },
                {
                    "id": "ext2",
                    "action": "atomic-extract",
                    "args": {
                        "note_ref": toc_apos,
                        "epistemic_type": "SYN",
                        "title": "多源综合Provenance批量萃取验证",
                        "summary": "验证单次 batch 内多源 provenance 数组合批萃取",
                        "tags": ["Atom", "MultiSource"],
                        "content": "## 1. 综合结论\n\n跨两篇一手文献的综合权衡命题。",
                        "provenance": [
                            {"source_ref": lib_apos_ref, "toc_ref": toc_apos, "slice_refs": [slc_apos_ref]},
                            {"source_ref": lib_apos2_ref, "toc_ref": toc_apos2, "slice_refs": [slc_apos2_ref]},
                        ],
                    },
                },
                {
                    "id": "ext3",
                    "action": "atomic-extract",
                    "args": {
                        "note_ref": toc_apos2,
                        "epistemic_type": "OBJ",
                        "title": "字符串JSONProvenance批量萃取验证",
                        "summary": "验证 batch 内 JSON 字符串 provenance 与逗号 tags 兼容",
                        "tags": "Atom,JsonStr",
                        "content": "## 1. 断言\n\n字符串 provenance 兼容。",
                        "provenance": json.dumps([{"source_ref": lib_apos2_ref, "toc_ref": toc_apos2, "slice_refs": [slc_apos2_ref]}]),
                    },
                },
            ],
        }
        rc, pb_res, err = _run(CLI_PATH, ["--json-stdin", "-"], env=env, stdin_text=json.dumps(prov_batch, ensure_ascii=False))
        assert rc == 0 and pb_res.get("ok") and pb_res["data"]["succeeded"] == 3, f"batch multi-source provenance failed: {pb_res} | {err}"
        ext2_data = pb_res["data"]["results"][1]["data"]
        ext2_prov = ext2_data.get("provenance") or []
        assert len(ext2_prov) == 2 and ext2_prov[0].get("source") != ext2_prov[1].get("source"), f"expected 2 distinct provenance groups on ext2: {ext2_data}"
        ext2_ref = ext2_data["extracted"][0]["note_ref"]
        rc, ext2_get, _ = _run(CLI_PATH, ["--action", "note-get", "--ref", vref, "--note_ref", ext2_ref, "--no_daemon", "true"], env=env)
        assert rc == 0 and ext2_get.get("ok") and ext2_get["data"].get("doc_type") == "atomic", f"ext2 note-get failed: {ext2_get}"

        # 6. Test P1 read primitives: outline, note-grep, project-brief
        rc, out_res, _ = _run(CLI_PATH, ["--action", "note-get", "--ref", vref, "--note_ref", draft_ref, "--outline", "true", "--no_daemon", "true"], env=env)
        assert rc == 0 and out_res["data"].get("outline_only") is True
        assert "content" not in out_res["data"], "outline_only should omit full content"
        assert len(out_res["data"].get("toc_outline") or []) >= 4
        _assert_no_leaks(out_res, forbidden_leaks, "note-get-outline")

        rc, grep_res, _ = _run(
            CLI_PATH,
            ["--action", "note-grep", "--ref", vref, "--project_ref", pref, "--pattern", r"TOKEN_ALPHA_\d+", "--regex", "true", "--no_daemon", "true"],
            env=env,
        )
        assert rc == 0 and grep_res["data"]["total_matches"] >= 1
        assert grep_res["data"]["results"][0]["note_ref"] == draft_ref
        _assert_no_leaks(grep_res, forbidden_leaks, "note-grep")

        rc, brief_res, _ = _run(
            CLI_PATH,
            ["--action", "project-brief", "--ref", vref, "--project_ref", pref, "--no_daemon", "true"],
            env=env,
        )
        assert rc == 0 and brief_res["data"]["total_notes"] >= 5
        assert brief_res["data"]["edge_count"] >= 1
        _assert_no_leaks(brief_res, forbidden_leaks, "project-brief")

        # 6b. Dual-layer zero-path check: prose `.trash/` & `/Volumes/` readable, while real leaks still HALT!
        rc, gi_create, err = _run(
            CLI_PATH,
            [
                "--action", "note-create", "--ref", vref, "--project_ref", pref,
                "--zone", "workbench", "--subdir", "ws-tester/drafts",
                "--title", "Obsidian忽略规则示例", "--summary", "含.trash与/Volumes讨论",
                "--content", "```gitignore\n.trash/\n.obsidian/workspace.json\n```\n外部挂载卷 `/Volumes/Backup`。",
                "--no_daemon", "true",
            ],
            env=env,
        )
        assert rc == 0 and gi_create.get("ok"), f"gitignore note-create failed: {gi_create} | {err}"
        gi_ref = gi_create["data"]["note_ref"]
        rc, gi_get, _ = _run(CLI_PATH, ["--action", "note-get", "--ref", vref, "--note_ref", gi_ref, "--no_daemon", "true"], env=env)
        assert rc == 0 and gi_get.get("ok") and ".trash/" in gi_get["data"]["content"], f"prose .trash/ wrongly blocked: {gi_get}"
        rc, gi_grep, _ = _run(CLI_PATH, ["--action", "note-grep", "--ref", vref, "--project_ref", pref, "--pattern", ".trash/", "--no_daemon", "true"], env=env)
        assert rc == 0 and gi_grep["data"]["total_matches"] >= 1, f"note-grep on .trash/ wrongly blocked: {gi_grep}"

        from facade.atlas_cli import _assert_zero_path_leak
        from storage.vault_kek_store import VaultStoreError

        # Security counterexample 1: `.trash` in metadata MUST still trigger E_HALT_SECURITY
        try:
            _assert_zero_path_leak({"data": {"rel_path": ".trash/x"}}, root, [])
            raise AssertionError("metadata .trash leak was not blocked!")
        except VaultStoreError as exc:
            assert exc.code == "E_HALT_SECURITY"
        # Security counterexample 2: real ws_root or GewuAtlas-data or bare sealed_path inside body MUST still trigger E_HALT_SECURITY
        for bad_body in (str(root), "GewuAtlas-data/vlt_01", "30-Projects/secret/draft.md", f"[[{root}/x]]", "[[GewuAtlas-data/vlt_01/x]]"):
            try:
                _assert_zero_path_leak({"data": {"content": f"leaked {bad_body}"}}, root, ["30-Projects/secret/draft.md"])
                raise AssertionError(f"body real path leak {bad_body!r} was not blocked!")
            except VaultStoreError as exc:
                assert exc.code == "E_HALT_SECURITY"
        # Regression: legitimate Obsidian path-disambiguated wikilink in note body + `.trash/` in change-get diff MUST pass!
        _assert_zero_path_leak(
            {"data": {"content": "- [[30-Projects/demo-prj/30-Workbench/ws-agent/WORKSPACE|Workspace]]", "files": [{"diff": "+.trash/\n+.obsidian/workspace.json"}]}},
            root,
            ["30-Projects/demo-prj"],
        )
        # Verify change-get on gi_create (which contains `.trash/`) succeeds without false E_HALT_SECURITY
        gi_chg_id = gi_create["data"]["change_id"]
        rc, gi_chg_get, _ = _run(CLI_PATH, ["--action", "change-get", "--ref", vref, "--change_id", gi_chg_id, "--no_daemon", "true"], env=env)
        assert rc == 0 and gi_chg_get.get("ok"), f"change-get on .trash/ note wrongly blocked: {gi_chg_get}"

        # 7. Test Self-Hosted Hot Daemon (start / hot requests / auth / external Obsidian edits / stop / idle TTL / zombie fallback)
        rc, d_start, err = _run(DAEMON_PATH, ["start", "--idle-timeout", "30", "--preload-vault", vref], env=env)
        if d_start.get("code") == "E_DAEMON_SANDBOX_RESTRICTED":
            # Sandbox blocks AF_UNIX bind: verify fast-fail contract & exercise AtlasHotKernel directly in-process
            _assert_no_leaks(d_start, forbidden_leaks, "daemon-start-sandbox-fastfail")
            from facade.atlas_hot_kernel import AtlasHotKernel

            prev_rt_env = os.environ.get("GEWU_APP_RUNTIME_DIR")
            prev_ws_env = os.environ.get("GEWU_ATLAS_WORKSPACE_ROOT")
            os.environ["GEWU_APP_RUNTIME_DIR"] = env["GEWU_APP_RUNTIME_DIR"]
            os.environ["GEWU_ATLAS_WORKSPACE_ROOT"] = env["GEWU_ATLAS_WORKSPACE_ROOT"]
            hot_k = AtlasHotKernel()
            try:
                hot_k.preload_vault(vref, session_env=env)
                for idx in range(3):
                    ec_h, out_h = hot_k.execute_cli(["--action", "note-search", "--ref", vref, "--query", f"热内核{idx}", "--mode", "locate"], session_env=env)
                    assert ec_h == 0 and json.loads(out_h).get("ok")
                for _ in range(2):
                    ec_h, out_h = hot_k.execute_cli(["--action", "project-brief", "--ref", vref, "--project_ref", pref], session_env=env)
                    assert ec_h == 0 and json.loads(out_h).get("ok")
                st_k = hot_k.stats(idle_timeout=30.0)
                assert st_k["requests_served"] >= 5
                assert st_k["search_cache"]["hits"] >= 1
                assert st_k["index_cache"]["hits"] >= 1

                vault_dirs = [p for p in (root / "Volumes" / "GewuAtlas-data").iterdir() if p.is_dir()]
                draft_files = list(vault_dirs[0].rglob("*性能设计报告*.md"))
                ext_file = draft_files[0]
                ext_file.write_text(ext_file.read_text("utf-8") + "\n人类在Obsidian手写新增行_EXT_999\n", encoding="utf-8")
                ec_h, out_h = hot_k.execute_cli(["--action", "note-get", "--ref", vref, "--note_ref", draft_ref], session_env=env)
                assert ec_h == 0 and "人类在Obsidian手写新增行_EXT_999" in json.loads(out_h)["data"]["content"]

                rc, cold_c, _ = _run(
                    CLI_PATH,
                    ["--action", "note-create", "--ref", vref, "--project_ref", pref, "--zone", "workbench", "--subdir", "ws-tester/drafts", "--title", "外部冷创建卡", "--summary", "cold", "--content", "COLD_MARKER_777", "--no_daemon", "true"],
                    env=env,
                )
                assert rc == 0
                cold_ref = cold_c["data"]["note_ref"]
                ec_h, out_h = hot_k.execute_cli(["--action", "note-get", "--ref", vref, "--note_ref", cold_ref], session_env=env)
                assert ec_h == 0 and "COLD_MARKER_777" in json.loads(out_h)["data"]["content"]
            finally:
                hot_k.close()
                if prev_rt_env is None:
                    os.environ.pop("GEWU_APP_RUNTIME_DIR", None)
                else:
                    os.environ["GEWU_APP_RUNTIME_DIR"] = prev_rt_env
                if prev_ws_env is None:
                    os.environ.pop("GEWU_ATLAS_WORKSPACE_ROOT", None)
                else:
                    os.environ["GEWU_ATLAS_WORKSPACE_ROOT"] = prev_ws_env
        else:
            assert rc == 0 and d_start.get("ok"), f"daemon start failed: {d_start} | {err}"
            _assert_no_leaks(d_start, forbidden_leaks, "daemon-start")

            try:
                # Verify 0600 permissions on meta & socket
                meta_file = runtime_dir / "state" / "atlas-daemon.meta.json"
                assert meta_file.is_file()
                assert (stat.S_IMODE(meta_file.stat().st_mode) & 0o077) == 0, "meta file must be 0600"
                meta = json.loads(meta_file.read_text("utf-8"))
                sock_p = Path(meta["sock_path"])
                assert (stat.S_IMODE(sock_p.stat().st_mode) & 0o077) == 0, "socket must be 0600"
                _assert_no_leaks(d_start, [meta["token"]], "daemon-start-no-token")

                # Security counterexample: forged token rejected
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(2.0)
                s.connect(str(sock_p))
                s.sendall((json.dumps({"v": 1, "token": "wrong-token", "op": "ping"}) + "\n").encode("utf-8"))
                bad_resp = json.loads(s.recv(4096).decode("utf-8").splitlines()[0])
                s.close()
                assert bad_resp.get("ok") is False and bad_resp.get("error") == "E_UNAUTHORIZED_TOKEN", f"bad token not rejected: {bad_resp}"

                # Route normal CLI calls through running hot daemon (both search_cache & index_cache)
                for idx in range(3):
                    rc, g_hot, err = _run(CLI_PATH, ["--action", "note-search", "--ref", vref, "--query", f"热内核{idx}", "--mode", "locate"], env=env)
                    assert rc == 0 and g_hot.get("ok"), f"hot note-search failed: {g_hot} | {err}"
                    _assert_no_leaks(g_hot, [*forbidden_leaks, meta["token"]], "hot-search")
                for _ in range(2):
                    rc, p_hot, err = _run(CLI_PATH, ["--action", "project-brief", "--ref", vref, "--project_ref", pref], env=env)
                    assert rc == 0 and p_hot.get("ok"), f"hot project-brief failed: {p_hot} | {err}"
                    _assert_no_leaks(p_hot, [*forbidden_leaks, meta["token"]], "hot-brief")

                rc, d_stat, _ = _run(DAEMON_PATH, ["status"], env=env)
                assert rc == 0 and d_stat["data"]["running"] is True
                served_before_bypass = int(d_stat["data"]["requests_served"])
                assert served_before_bypass >= 5, f"expected daemon served >=5 requests: {d_stat['data']}"
                assert d_stat["data"]["search_cache"]["hits"] >= 1, f"expected RAM search cache hits: {d_stat['data']}"
                assert d_stat["data"]["index_cache"]["hits"] >= 1, f"expected RAM index cache hits: {d_stat['data']}"
                _assert_no_leaks(d_stat, [*forbidden_leaks, meta["token"]], "daemon-status")

                # Network / external-IO actions MUST bypass the hot daemon so caller's sandbox/proxy context applies!
                rc, ing_list, err = _run(CLI_PATH, ["--action", "ingest", "--ref", vref, "--mode", "list"], env=env)
                assert rc == 0 and ing_list.get("ok"), f"bypassed ingest --mode list failed: {ing_list} | {err}"
                rc, fmt_chk, err = _run(CLI_PATH, ["--action", "fmt", "--ref", vref, "--dry_run", "true"], env=env)
                assert rc == 0 and fmt_chk.get("ok"), f"bypassed fmt --dry_run failed: {fmt_chk} | {err}"
                rc, doc_rem, err = _run(CLI_PATH, ["--action", "guard", "--ref", vref, "--mode", "doc-remote"], env=env)
                assert rc == 0 and doc_rem.get("ok"), f"bypassed guard doc-remote failed: {doc_rem} | {err}"
                rc, d_stat_after, _ = _run(DAEMON_PATH, ["status"], env=env)
                assert int(d_stat_after["data"]["requests_served"]) == served_before_bypass, (
                    f"bypassed network/external-IO actions must NOT forward to daemon! before={served_before_bypass} after={d_stat_after['data']['requests_served']}"
                )

                # Human external edit in Obsidian while daemon is hot!
                vault_dirs = [p for p in (root / "Volumes" / "GewuAtlas-data").iterdir() if p.is_dir()]
                assert len(vault_dirs) == 1
                draft_files = list(vault_dirs[0].rglob("*性能设计报告*.md"))
                assert len(draft_files) == 1
                ext_file = draft_files[0]
                ext_file.write_text(ext_file.read_text("utf-8") + "\n人类在Obsidian手写新增行_EXT_999\n", encoding="utf-8")
                # Next hot daemon note-get MUST immediately see human edit!
                rc, ext_get, _ = _run(CLI_PATH, ["--action", "note-get", "--ref", vref, "--note_ref", draft_ref], env=env)
                assert rc == 0 and "人类在Obsidian手写新增行_EXT_999" in ext_get["data"]["content"], "daemon returned stale data after Obsidian edit!"

                # External cold CLI (--no_daemon true) creates a new note -> daemon must immediately see it!
                rc, cold_c, _ = _run(
                    CLI_PATH,
                    ["--action", "note-create", "--ref", vref, "--project_ref", pref, "--zone", "workbench", "--subdir", "ws-tester/drafts", "--title", "外部冷创建卡", "--summary", "cold", "--content", "COLD_MARKER_777", "--no_daemon", "true"],
                    env=env,
                )
                assert rc == 0
                cold_ref = cold_c["data"]["note_ref"]
                rc, hot_see, _ = _run(CLI_PATH, ["--action", "note-get", "--ref", vref, "--note_ref", cold_ref], env=env)
                assert rc == 0 and "COLD_MARKER_777" in hot_see["data"]["content"], "daemon did not invalidate .enc after cold CLI write!"
            finally:
                rc, d_stop, _ = _run(DAEMON_PATH, ["stop"], env=env)
                assert rc == 0 and d_stop.get("ok"), f"daemon stop failed: {d_stop}"

            # 7b. Verify idle TTL auto-shutdown
            rc, d_ttl, _ = _run(DAEMON_PATH, ["start", "--idle-timeout", "2"], env=env)
            assert rc == 0 and d_ttl.get("ok")
            time.sleep(3.2)
            rc, d_after_ttl, _ = _run(DAEMON_PATH, ["status"], env=env)
            assert rc == 0 and d_after_ttl["data"]["running"] is False, f"daemon did not auto-exit after idle TTL: {d_after_ttl['data']}"

        # 7c. Verify zombie meta fallback
        meta_file = runtime_dir / "state" / "atlas-daemon.meta.json"
        meta_file.write_text(json.dumps({"v": 1, "pid": 999999, "sock_path": str(runtime_dir / "state" / "dead.sock"), "token": "abc", "code_fingerprint": "dead"}), encoding="utf-8")
        rc, fb_res, _ = _run(CLI_PATH, ["--action", "vault-list"], env=env)
        assert rc == 0 and fb_res.get("ok"), f"zombie fallback failed: {fb_res}"

        # 7d. Verify sandbox EPERM fast-fail (<0.5s instead of 5s start_timeout)
        import errno as _errno
        import facade.atlas_daemon as _ad

        orig_sock_cls = _ad.socket.socket
        orig_runtime_env = os.environ.get("GEWU_APP_RUNTIME_DIR")

        class _DeniedUnixSocket:
            def __init__(self, family=-1, type=-1, proto=-1, fileno=None):
                self._family = family

            def bind(self, _addr):
                raise PermissionError(_errno.EPERM, "Operation not permitted")

            def close(self):
                pass

        try:
            os.environ["GEWU_APP_RUNTIME_DIR"] = str(runtime_dir)
            _ad.socket.socket = _DeniedUnixSocket  # type: ignore[assignment]
            t_fast0 = time.monotonic()
            ff_rc, ff_info = _ad._daemon_start(30, [])
            ff_dt = time.monotonic() - t_fast0
            assert ff_rc == 1 and ff_info.get("status") == "sandbox_ipc_restricted", f"expected sandbox_ipc_restricted: {ff_rc}, {ff_info}"
            assert ff_dt < 0.5, f"sandbox fast-fail took too long ({ff_dt:.2f}s >= 0.5s)"
        finally:
            _ad.socket.socket = orig_sock_cls  # type: ignore[assignment]
            if orig_runtime_env is None:
                os.environ.pop("GEWU_APP_RUNTIME_DIR", None)
            else:
                os.environ["GEWU_APP_RUNTIME_DIR"] = orig_runtime_env

        # 8. Test Optional Stdio MCP Server (`atlas_mcp_server.py`)
        mcp_reqs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "selftest", "version": "1.0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "atlas_project_brief", "arguments": {"ref": vref, "project_ref": pref}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "atlas_grep", "arguments": {"ref": vref, "project_ref": pref, "pattern": "COLD_MARKER_777"}}},
        ]
        mcp_stdin = "\n".join(json.dumps(r, ensure_ascii=False) for r in mcp_reqs) + "\n"
        mcp_proc = subprocess.run(
            [sys.executable, "-I", "-B", str(MCP_PATH)],
            cwd=str(APP_DIR.parent),
            env=env,
            input=mcp_stdin,
            capture_output=True,
            text=True,
            timeout=25,
        )
        assert mcp_proc.returncode == 0, f"mcp server failed: {mcp_proc.stderr}"
        mcp_lines = [json.loads(line) for line in mcp_proc.stdout.splitlines() if line.strip()]
        assert len(mcp_lines) == 4, f"expected 4 JSON-RPC responses, got {len(mcp_lines)}: {mcp_lines}"
        assert mcp_lines[0]["result"]["serverInfo"]["name"] == "gewu-atlas-mcp"
        tool_names = {t["name"] for t in mcp_lines[1]["result"]["tools"]}
        assert {"atlas_vault", "atlas_search", "atlas_read", "atlas_grep", "atlas_project_brief", "atlas_patch", "atlas_batch"}.issubset(tool_names)
        assert mcp_lines[2]["result"]["isError"] is False
        assert mcp_lines[3]["result"]["isError"] is False
        grep_mcp_data = json.loads(mcp_lines[3]["result"]["content"][0]["text"])
        assert grep_mcp_data["data"]["total_matches"] == 1
        _assert_no_leaks(mcp_lines, forbidden_leaks, "mcp-responses")

        # 8b. Verify curated --template / --help examples for new high-efficiency actions
        rc, root_tpl, _ = _run(CLI_PATH, ["--template"], env=env)
        assert rc == 0 and root_tpl.get("ok")
        rec_acts = set(root_tpl["data"].get("recommended_actions") or [])
        assert {"batch", "note-patch", "note-grep", "project-brief"}.issubset(rec_acts), f"missing new actions in root --template: {rec_acts}"
        for act_name, must_sub in [
            ("batch", "--json-stdin"),
            ("note-patch", '"op": "section"'),
            ("note-grep", "--with_section"),
            ("project-brief", "--include_slices"),
            ("note-get", "--outline"),
        ]:
            rc, act_tpl, _ = _run(CLI_PATH, ["--action", act_name, "--template"], env=env)
            assert rc == 0 and act_tpl.get("ok"), f"{act_name} --template failed: {act_tpl}"
            tpl_dump = json.dumps(act_tpl["data"], ensure_ascii=False)
            assert act_tpl["data"].get("examples"), f"{act_name} --template missing copyable examples"
            assert must_sub in tpl_dump, f"{act_name} --template missing expected guidance {must_sub!r}"

        # 8c. P0/P1 locate & edit speedups: read-only batch fast path + $g.note_refs chaining + note-grep --with_section + multi-note note-patch + outline hint on miss
        rc, sec_grep, _ = _run(
            CLI_PATH,
            ["--action", "note-grep", "--ref", vref, "--project_ref", pref, "--pattern", "TOKEN_ALPHA_002", "--with_section", "true", "--no_daemon", "true"],
            env=env,
        )
        assert rc == 0 and sec_grep.get("ok") and sec_grep["data"]["matched_notes"] >= 1, f"with_section grep failed: {sec_grep}"
        first_m = sec_grep["data"]["results"][0]["matches"][0]
        assert first_m.get("section_heading") and "TOKEN_ALPHA_002" in (first_m.get("section_content") or ""), f"missing section_content in grep: {first_m}"

        ro_batch_payload = {
            "action": "batch",
            "ref": vref,
            "project_ref": pref,
            "no_daemon": True,
            "steps": [
                {"id": "b", "action": "project-brief", "args": {"project_ref": pref}},
                {"id": "g", "action": "note-grep", "args": {"project_ref": pref, "pattern": "TOKEN_ALPHA_002", "with_section": True}},
                {"id": "o", "action": "note-get", "args": {"note_refs": "$g.note_refs", "outline": True}},
            ],
        }
        rc, ro_bres, err = _run(CLI_PATH, ["--json-stdin", "-"], env=env, stdin_text=json.dumps(ro_batch_payload, ensure_ascii=False))
        assert rc == 0 and ro_bres.get("ok") and ro_bres["data"].get("read_only") is True, f"read-only batch failed: {ro_bres} | {err}"
        assert "change_id" not in ro_bres["data"] and "delivery_gate" not in ro_bres["data"], f"read-only batch must skip write hooks: {ro_bres['data']}"
        assert ro_bres["data"]["results"][2]["data"]["count"] >= 1, f"chained $g.note_refs note-get failed: {ro_bres['data']['results'][2]}"

        for second_id in ("step_2", None):
            second = {"action": "project-brief"}
            if second_id:
                second["id"] = second_id
            payload = dict(ro_batch_payload, steps=[{"action": "project-brief", "id": "step_2"}, second])
            rc, report, _ = _run(CLI_PATH, ["--json-stdin", "-"], env=env, stdin_text=json.dumps(payload))
            assert rc != 0 and report["code"] == "E_PARAM_INVALID", report

        payload = dict(ro_batch_payload, on_error="continue", steps=[
            {"id": "missing", "action": "note-get", "args": {"note_ref": "nte_999999"}},
            {"action": "note-get", "args": {"note_ref": "$missing.note_ref"}},
            {"action": "project-brief", "args": {"project_ref": pref}},
        ])
        rc, report, _ = _run(CLI_PATH, ["--json-stdin", "-"], env=env, stdin_text=json.dumps(payload))
        rows = report["data"]["results"]
        assert rc != 0 and [row["ok"] for row in rows] == [False, False, True], report
        assert rows[1]["code"] == "E_BATCH_REF_NOT_FOUND", report

        # Patch miss must return available headings outline without extra note-get
        rc, miss_res, _ = _run(
            CLI_PATH,
            ["--action", "note-patch", "--ref", vref, "--note_ref", draft_ref, "--section", "## 不存在的章节", "--content", "x", "--no_daemon", "true"],
            env=env,
        )
        assert rc != 0 and "当前可用标题" in json.dumps(miss_res, ensure_ascii=False), f"patch miss missing outline hint: {miss_res}"

        # Multi-note note-patch with --dry_run preview + apply + library guard check
        rc, dry_p, _ = _run(
            CLI_PATH,
            [
                "--action", "note-patch", "--ref", vref,
                "--note_refs", f"{draft_ref},{gi_ref}",
                "--old_text", "TOKEN_ALPHA_002", "--new_text", "TOKEN_BETA_2",
                "--replace_all", "true", "--dry_run", "true", "--no_daemon", "true",
            ],
            env=env,
        )
        assert rc == 0 and dry_p["data"]["dry_run"] is True and dry_p["data"]["patched"] == 1 and dry_p["data"]["skipped"] == 1, f"multi-note dry_run failed: {dry_p}"
        assert "change_id" not in dry_p["data"], "dry_run note-patch must not record change_id"

        rc, real_p, _ = _run(
            CLI_PATH,
            [
                "--action", "note-patch", "--ref", vref,
                "--note_refs", f"{draft_ref},{gi_ref}",
                "--old_text", "TOKEN_ALPHA_002", "--new_text", "TOKEN_BETA_2",
                "--replace_all", "true", "--no_daemon", "true",
            ],
            env=env,
        )
        assert rc == 0 and real_p["data"]["patched"] == 1 and real_p["data"]["skipped"] == 1, f"multi-note patch failed: {real_p}"
        assert real_p["data"].get("delivery_gate", {}).get("scope") == "dirty_only", f"note-patch should use dirty_only gate: {real_p['data'].get('delivery_gate')}"
        rc, after_p, _ = _run(CLI_PATH, ["--action", "note-get", "--ref", vref, "--note_ref", draft_ref, "--no_daemon", "true"], env=env)
        assert rc == 0 and "TOKEN_BETA_2" in after_p["data"]["content"]

        # Verify multi-note note-patch still enforces 21-Library read-only protection
        rc, lib_p, _ = _run(
            CLI_PATH,
            [
                "--action", "note-patch", "--ref", vref,
                "--note_refs", f"{lib_apos_ref},{draft_ref}",
                "--old_text", "TOKEN_BETA_2", "--new_text", "TOKEN_GAMMA_3",
                "--no_daemon", "true",
            ],
            env=env,
        )
        assert rc != 0 and lib_p["data"]["failed"] == 1 and lib_p["data"]["results"][0]["code"] == "E_READONLY_LIBRARY", f"library guard bypassed in batch patch: {lib_p}"

        # Cleanup batch via undo just to verify unified undo works end-to-end
        rc, u_res, _ = _run(CLI_PATH, ["--action", "undo", "--ref", vref, "--change_id", chg_id, "--no_daemon", "true"], env=env)
        assert rc == 0 and u_res.get("ok"), f"batch undo failed: {u_res}"
        rc, gone_get, _ = _run(CLI_PATH, ["--action", "note-get", "--ref", vref, "--note_ref", a1_ref, "--no_daemon", "true"], env=env)
        assert rc != 0, "a1_ref should be gone after batch undo!"

        # 9. Static Audit P0/P1 & P2 Regression Suite
        # 9a. P0-1: HotKernel session env forwarding (GEWU_ATLAS_SESSION_ID / CODEX_THREAD_ID) & residual env clearing
        from facade.atlas_hot_kernel import AtlasHotKernel

        rc, v2_create, _ = _run(CLI_PATH, ["--action", "vault-create", "--title", "第二金库", "--no_daemon", "true"], env=env)
        assert rc == 0 and v2_create.get("ok")
        vref2 = v2_create["data"]["ref"]
        prev_rt_9 = os.environ.get("GEWU_APP_RUNTIME_DIR")
        prev_ws_9 = os.environ.get("GEWU_ATLAS_WORKSPACE_ROOT")
        prev_codex_9 = os.environ.get("CODEX_THREAD_ID")
        os.environ["GEWU_APP_RUNTIME_DIR"] = env["GEWU_APP_RUNTIME_DIR"]
        os.environ["GEWU_ATLAS_WORKSPACE_ROOT"] = env["GEWU_ATLAS_WORKSPACE_ROOT"]
        os.environ["CODEX_THREAD_ID"] = "residual_daemon_thread"
        hk9 = AtlasHotKernel()
        try:
            env_s1 = {**env, "GEWU_ATLAS_SESSION_ID": "sess_alpha", "CODEX_THREAD_ID": ""}
            env_s2 = {**env, "GEWU_ATLAS_SESSION_ID": "", "CODEX_THREAD_ID": "sess_beta"}
            ec1, out1 = hk9.execute_cli(["--action", "vault-use", "--ref", vref], session_env=env_s1)
            ec2, out2 = hk9.execute_cli(["--action", "vault-use", "--ref", vref2], session_env=env_s2)
            assert ec1 == 0 and ec2 == 0, f"hot session vault-use failed: {out1} | {out2}"
            ec1c, out1c = hk9.execute_cli(["--action", "vault-context"], session_env=env_s1)
            ec2c, out2c = hk9.execute_cli(["--action", "vault-context"], session_env=env_s2)
            assert json.loads(out1c)["data"]["ref"] == vref, f"sess_alpha bound to wrong vault: {out1c}"
            assert json.loads(out2c)["data"]["ref"] == vref2, f"sess_beta bound to wrong vault: {out2c}"
            # Client without session IDs must NOT inherit residual_daemon_thread
            env_no_sess = {**env, "GEWU_ATLAS_SESSION_ID": "", "CODEX_THREAD_ID": ""}
            ec_ns, out_ns = hk9.execute_cli(["--action", "vault"], session_env=env_no_sess)
            assert ec_ns == 0 and json.loads(out_ns)["data"]["vault_context"]["session_bound"] is False
        finally:
            hk9.close()
            for k_env, old_v in (("GEWU_APP_RUNTIME_DIR", prev_rt_9), ("GEWU_ATLAS_WORKSPACE_ROOT", prev_ws_9), ("CODEX_THREAD_ID", prev_codex_9)):
                if old_v is None:
                    os.environ.pop(k_env, None)
                else:
                    os.environ[k_env] = old_v

        # 9b. P0-2: note-grep private note & 05-Journal isolation + ReDoS regex guard
        rc, j_app, _ = _run(CLI_PATH, ["--action", "journal-append", "--ref", vref, "--content", "SECRET_JOURNAL_TOKEN_901", "--no_daemon", "true"], env=env)
        assert rc == 0 and j_app.get("ok")
        rc, priv_c, _ = _run(
            CLI_PATH,
            ["--action", "note-create", "--ref", vref, "--zone", "atomics", "--title", "私密测试卡", "--summary", "私密", "--content", "SECRET_PRIVATE_NOTE_902", "--no_daemon", "true"],
            env=env,
        )
        assert rc == 0 and priv_c.get("ok")
        priv_nref = priv_c["data"]["note_ref"]
        rc, _, _ = _run(CLI_PATH, ["--action", "note-update", "--ref", vref, "--note_ref", priv_nref, "--set_meta", '{"private": true}', "--no_daemon", "true"], env=env)
        rc, grep_pub1, _ = _run(CLI_PATH, ["--action", "note-grep", "--ref", vref, "--pattern", "SECRET_JOURNAL_TOKEN_901", "--no_daemon", "true"], env=env)
        rc, grep_pub2, _ = _run(CLI_PATH, ["--action", "note-grep", "--ref", vref, "--pattern", "SECRET_PRIVATE_NOTE_902", "--no_daemon", "true"], env=env)
        assert grep_pub1["data"]["matched_notes"] == 0, f"05-Journal leaked in public note-grep: {grep_pub1}"
        assert grep_pub2["data"]["matched_notes"] == 0, f"private:true note leaked in public note-grep: {grep_pub2}"
        rc, grep_priv1, _ = _run(CLI_PATH, ["--action", "note-grep", "--ref", vref, "--pattern", "SECRET_JOURNAL_TOKEN_901", "--private", "true", "--no_daemon", "true"], env=env)
        rc, grep_priv2, _ = _run(CLI_PATH, ["--action", "note-grep", "--ref", vref, "--pattern", "SECRET_PRIVATE_NOTE_902", "--private", "true", "--no_daemon", "true"], env=env)
        assert grep_priv1["data"]["matched_notes"] == 1 and grep_priv2["data"]["matched_notes"] == 1
        rc, redos_res, _ = _run(CLI_PATH, ["--action", "note-grep", "--ref", vref, "--pattern", "(a+)+$", "--regex", "true", "--no_daemon", "true"], env=env)
        assert rc != 0 and redos_res["code"] == "E_PARAM_INVALID", f"ReDoS regex not blocked: {redos_res}"

        # 9c. P0-3: KekNoteRegistry._raw_enc_rows cache invalidation after external write
        from storage.note_kek_store import KekNoteRegistry
        from storage.vault_kek_store import load_or_init_kek

        st_dir = runtime_dir / "state"
        mkek = load_or_init_kek(st_dir / "atlas-vaults.kek")
        hot_nreg = KekNoteRegistry("gewu-atlas", st_dir / "atlas-notes.enc", mkek)
        ext_nreg = KekNoteRegistry("gewu-atlas", st_dir / "atlas-notes.enc", mkek)
        hot_nreg._ensure_raw_enc_rows()
        ext_map = ext_nreg.upsert_paths(vref, ["20-AI-Zone/20-Memory/22-Atomics/External-Write-Note.md"])
        assert "20-AI-Zone/20-Memory/22-Atomics/External-Write-Note.md" in ext_map
        hot_map = hot_nreg.upsert_paths(vref, ["20-AI-Zone/20-Memory/22-Atomics/Hot-Write-Note.md"])
        assert "20-AI-Zone/20-Memory/22-Atomics/External-Write-Note.md" in hot_map, "Hot KekNoteRegistry rolled back external write!"
        assert "20-AI-Zone/20-Memory/22-Atomics/Hot-Write-Note.md" in hot_map
        hot_nreg.remove_paths(vref, ["20-AI-Zone/20-Memory/22-Atomics/External-Write-Note.md", "20-AI-Zone/20-Memory/22-Atomics/Hot-Write-Note.md"])

        # 9d. P0-4: batch partial failure records change_id (undoable) and closes auto-created DOC ticket
        partial_batch = {
            "action": "batch",
            "ref": vref,
            "project_ref": pref,
            "no_daemon": True,
            "steps": [
                {"id": "ok1", "action": "note-create", "args": {"project_ref": pref, "zone": "workbench", "subdir": "ws-tester/drafts", "title": "半途成功草稿", "summary": "s", "content": "PARTIAL_STEP_1"}},
                {"id": "fail2", "action": "note-get", "args": {"note_ref": "nte_999999"}},
            ],
        }
        rc, p_bres, _ = _run(CLI_PATH, ["--json-stdin", "-"], env=env, stdin_text=json.dumps(partial_batch, ensure_ascii=False))
        assert rc != 0 and p_bres["code"] == "E_BATCH_PARTIAL_FAILED", f"expected E_BATCH_PARTIAL_FAILED: {p_bres}"
        assert p_bres["data"].get("change_id"), f"partial failed batch missing change_id: {p_bres['data']}"
        assert p_bres["data"].get("doc_ticket", {}).get("status") == "done", f"partial batch left orphan DOC ticket: {p_bres['data'].get('doc_ticket')}"
        p_note_ref = p_bres["data"]["results"][0]["data"]["note_ref"]
        rc, pu_res, _ = _run(CLI_PATH, ["--action", "undo", "--ref", vref, "--change_id", p_bres["data"]["change_id"], "--no_daemon", "true"], env=env)
        assert rc == 0 and pu_res.get("ok"), f"undo of partial batch failed: {pu_res}"
        rc, p_gone, _ = _run(CLI_PATH, ["--action", "note-get", "--ref", vref, "--note_ref", p_note_ref, "--no_daemon", "true"], env=env)
        assert rc != 0, "partially created note should be undone!"

        # 9e. P0-5: note-update enforces 21-Library read-only body & managed block guards
        rc, lib_upd_bad, _ = _run(
            CLI_PATH,
            ["--action", "note-update", "--ref", vref, "--note_ref", lib_apos_ref, "--content", "试图篡改原文正文", "--no_daemon", "true"],
            env=env,
        )
        assert rc != 0 and lib_upd_bad["code"] == "E_READONLY_LIBRARY", f"note-update did not block library body edit: {lib_upd_bad}"
        rc, moc_upd_bad, _ = _run(
            CLI_PATH,
            ["--action", "note-update", "--ref", vref, "--note_ref", moc_ref, "--content", "# 破坏受控块的全文覆盖", "--no_daemon", "true"],
            env=env,
        )
        assert rc != 0 and moc_upd_bad["code"] == "E_MANAGED_BLOCK_PROTECTED", f"note-update did not block managed block removal: {moc_upd_bad}"

        # 9f. P0-6 & P2-1: delivery_gate uses master KEK (index cache stays valid) & project-brief populates project metadata
        ni_file = st_dir / "notes_index" / f"{vref}.ni1"
        rc, pb_vault, _ = _run(CLI_PATH, ["--action", "project-brief", "--ref", vref, "--no_daemon", "true"], env=env)
        assert rc == 0 and len(pb_vault["data"].get("projects") or []) >= 1, f"project-brief vault projects list empty: {pb_vault['data']}"
        assert pb_vault["data"]["projects"][0].get("title"), f"project-brief missing project title: {pb_vault['data']['projects']}"
        ni_mtime_before = ni_file.stat().st_mtime_ns
        rc, dg_res, _ = _run(CLI_PATH, ["--action", "guard", "--ref", vref, "--project_ref", pref, "--mode", "delivery", "--note_refs", moc_ref, "--no_daemon", "true"], env=env)
        assert rc == 0 and dg_res.get("ok")
        review = dg_res["data"]["semantic_evaluation"]
        assert review["review_status"] == "pending"
        assert review["reviewed_refs"] == [] and review["unreviewed_refs"] == [moc_ref]
        prompt = review["review_prompt"]
        assert prompt == review["subagent_blind_review_prompt"]
        assert all(section in prompt for section in ("## atomic", "## moc", "## artifact", "尝试反证", "PARTIAL"))
        assert review["item_review_template"]["verdict"] is None
        assert "source_locations" in review["chain_review_template"]
        assert ni_file.stat().st_mtime_ns == ni_mtime_before, "guard --mode delivery corrupted/rewrote notes_index cache with wrong KEK!"

        # 9g. P0-9 & P2-4: yamlmini YAML 1.2 & nested seq mapping, volume guard ./gewu.command & pre_tool_use_guard wildcard/bash write
        from notes.atlas_yamlmini import yaml_load
        from guard.pre_tool_use_guard import is_protected_path_value, _extract_bash_system_write_targets
        from storage.atlas_volume_guard import inspect_tool_call as vol_inspect

        ym = yaml_load("url: https://example.com/a#sec\nflag_no: NO\nflag_true: true\nprovenance:\n  - source_file: \"[[A]]\"\n    slice_files:\n      - \"[[S1]]\"\n      - \"[[S2]]\"\n")
        assert ym["url"] == "https://example.com/a#sec", f"yamlmini stripped #anchor without preceding space: {ym}"
        assert ym["flag_no"] == "NO" and ym["flag_true"] is True, f"yamlmini YAML 1.2 boolean mismatch: {ym}"
        assert ym["provenance"][0]["slice_files"] == ["[[S1]]", "[[S2]]"], f"yamlmini 2nd-key nested seq failed: {ym}"
        assert is_protected_path_value("GewuAtlas/.local/*.enc") is True, "wildcard protected path bypassed!"
        assert _extract_bash_system_write_targets("echo x > GewuAtlas/scripts/facade/atlas_cli.py") == ["GewuAtlas/scripts/facade/atlas_cli.py"]
        assert vol_inspect("Bash", {"command": "./gewu.command atlas --action note-list --ref vlt_01"}) is None
        assert vol_inspect("Bash", {"command": "./gewu.command atlas --action note-list\ncat Volumes/GewuAtlas-data/x"}) is not None

    print(json.dumps({"ok": True, "suite": "atlas_efficiency_daemon_selftest", "checks_passed": 17}, ensure_ascii=False))
    return 0


import unittest as _unittest


class AtlasEfficiencyDaemonTests(_unittest.TestCase):
    def test_efficiency_daemon_and_static_audit_regressions(self) -> None:
        self.assertEqual(main(), 0)


if __name__ == "__main__":
    sys.exit(main())
