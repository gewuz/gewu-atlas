#!/usr/bin/env python3
"""Isolated self-test for GewuAtlas Slice 2 (Note KEK handles, YAML/Schema, Promote/Deprecate, BM25, Health Check)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]


def _assert_no_note_path_leak(stdout_text: str, forbidden: list[str]) -> None:
    for item in forbidden:
        if item and item in stdout_text:
            raise AssertionError(f"path leaked to stdout: {item!r} in {stdout_text}")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        rt = tmp / "rt"
        ws = tmp / "ws"
        ws.mkdir()

        env = dict(os.environ)
        env["GEWU_APP_RUNTIME_DIR"] = str(rt)
        env["GEWU_ATLAS_WORKSPACE_ROOT"] = str(ws)

        def run_cli(*argv: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, "-I", "-B", str(SCRIPTS_DIR / "facade/atlas_cli.py"), *argv],
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
            )

        p_v = run_cli("--action", "vault-create", "--vault", "slice2-box", "--title", "知识切片库")
        assert p_v.returncode == 0, p_v.stdout
        vref = json.loads(p_v.stdout)["data"]["ref"]

        # 1. Create active draft in slices zone and reference in moc zone
        p_n1 = run_cli(
            "--action", "note-create",
            "--ref", vref,
            "--zone", "slices",
            "--doc_type", "slice",
            "--status", "active",
            "--title", "KEK密码学隔离",
            "--summary", "使用HKDF与XChaCha20-Poly1305隔离静态映射",
            "--tags", "KEK,Security",
            "--content", "本文总结 [[KEK密码学隔离]] 与零路径短句柄机制。",
        )
        assert p_n1.returncode == 0, p_n1.stdout
        n1_doc = json.loads(p_n1.stdout)
        n1_ref = n1_doc["data"]["note_ref"]
        assert n1_ref.startswith("nte_")
        _assert_no_note_path_leak(p_n1.stdout, ["slice2-box", "24-Slices", ".md", "Volumes/", str(ws)])

        p_n2 = run_cli(
            "--action", "note-create",
            "--ref", vref,
            "--zone", "moc",
            "--doc_type", "moc",
            "--title", "安全架构地图",
            "--summary", "汇总密码学隔离与门禁",
            "--tags", "MOC,Security",
            "--content", "相关原子构想：[[KEK密码学隔离]]。",
        )
        assert p_n2.returncode == 0, p_n2.stdout
        moc_ref = json.loads(p_n2.stdout)["data"]["note_ref"]

        # 2. List & Get with backlinks mapped to note_ref
        p_l = run_cli("--action", "note-list", "--ref", vref)
        assert p_l.returncode == 0, p_l.stdout
        l_doc = json.loads(p_l.stdout)
        assert l_doc["data"]["count"] >= 2
        _assert_no_note_path_leak(p_l.stdout, ["24-Slices", "23-MOC", "05-Journal", ".md", "Volumes/"])

        p_g = run_cli("--action", "note-get", "--ref", vref, "--note_ref", n1_ref)
        assert p_g.returncode == 0, p_g.stdout
        g_doc = json.loads(p_g.stdout)
        assert moc_ref in g_doc["data"]["backlink_refs"], g_doc["data"]["backlink_refs"]
        _assert_no_note_path_leak(p_g.stdout, ["24-Slices", ".md", "Volumes/"])

        # 3. Promote to atomics + journal provenance + AAD re-key + wikilink check
        p_pr = run_cli("--action", "note-promote", "--ref", vref, "--note_ref", n1_ref, "--target_zone", "atomics")
        assert p_pr.returncode == 0, p_pr.stdout
        pr_doc = json.loads(p_pr.stdout)
        assert pr_doc["data"]["zone"] == "atomics"
        assert pr_doc["data"]["doc_type"] == "atomic"
        assert pr_doc["data"]["key_version"] == 2
        assert (ws / "Volumes/GewuAtlas-data/slice2-box/20-AI-Zone/20-Memory/22-Atomics/KEK密码学隔离.md").is_file()

        # 4. BM25 Search via opaque handle
        p_s = run_cli("--action", "note-search", "--ref", vref, "--query", "XChaCha20 短句柄")
        assert p_s.returncode == 0, p_s.stdout
        s_doc = json.loads(p_s.stdout)
        assert s_doc["data"]["count"] >= 1
        assert s_doc["data"]["hits"][0]["note_ref"] == n1_ref
        _assert_no_note_path_leak(p_s.stdout, ["22-Atomics", "24-Slices", ".md", "Volumes/"])

        # 5. Health Check (zero broken wikilinks or schema errors)
        p_hc = run_cli("--action", "health-check", "--ref", vref)
        assert p_hc.returncode == 0, p_hc.stdout
        hc_doc = json.loads(p_hc.stdout)
        assert hc_doc["data"]["healthy"] is True, hc_doc
        assert hc_doc["data"]["schema_issue_count"] == 0
        assert hc_doc["data"]["broken_wikilink_count"] == 0

        # 6. Pending card promotion must be rejected
        p_pend = run_cli(
            "--action", "note-create",
            "--ref", vref,
            "--zone", "dropzone",
            "--title", "未审草稿",
            "--tags", "Draft",
            "--content", "待审测试内容",
        )
        assert p_pend.returncode == 0
        pend_ref = json.loads(p_pend.stdout)["data"]["note_ref"]
        p_fail_pr = run_cli("--action", "note-promote", "--ref", vref, "--note_ref", pend_ref, "--target_zone", "atomics")
        assert p_fail_pr.returncode == 1 and "E_PARAM_CONFLICT" in p_fail_pr.stdout

        # 7. Deprecate moves card cleanly to attic
        p_dep = run_cli("--action", "note-deprecate", "--ref", vref, "--note_ref", pend_ref)
        assert p_dep.returncode == 0, p_dep.stdout
        dep_doc = json.loads(p_dep.stdout)
        assert dep_doc["data"]["status"] == "deprecated" and dep_doc["data"]["zone"] == "attic"

    print(json.dumps({"ok": True, "selftest": "gewu-atlas-note-slice2/1"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
