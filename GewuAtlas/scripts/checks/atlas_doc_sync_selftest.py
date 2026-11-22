#!/usr/bin/env python3
"""Offline end-to-end selftest for unified GewuAtlas DOC Git & GitHub remote sync policy."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

APP_DIR = Path(__file__).resolve().parents[2]
CLI_SCRIPT = APP_DIR / "scripts" / "facade" / "atlas_cli.py"


def _run_cli(ws_root: Path, runtime_dir: Path, argv: List[str]) -> tuple[int, Dict[str, Any], str]:
    env = dict(os.environ)
    env["GEWU_ATLAS_WORKSPACE_ROOT"] = str(ws_root)
    env["GEWU_APP_RUNTIME_DIR"] = str(runtime_dir)
    env["GEWU_ATLAS_SESSION_ID"] = "selftest-doc-sync"
    proc = subprocess.run(
        [sys.executable, "-I", "-B", str(CLI_SCRIPT), *argv],
        cwd=str(ws_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    try:
        parsed = json.loads(proc.stdout)
    except Exception as exc:  # pragma: no cover
        raise AssertionError(f"Non-JSON CLI output (rc={proc.returncode}): stdout={proc.stdout!r}, stderr={proc.stderr!r}") from exc
    return proc.returncode, parsed, proc.stdout


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=TestPeer",
            "-c",
            "user.email=peer@gewu.local",
            *args,
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )


class TestAtlasDocRemoteSync(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws_root = self.root / "ws"
        self.runtime_dir = self.root / "runtime"
        self.ws_root.mkdir(parents=True)
        self.runtime_dir.mkdir(parents=True)

        # Bare remote repository
        self.bare_remote = self.root / "remote-gwa.git"
        self.bare_remote.mkdir()
        _git(self.bare_remote, "init", "--bare", "-b", "main")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _vault_dir(self) -> Path:
        data_root = self.ws_root / "Volumes" / "GewuAtlas-data"
        dirs = [p for p in data_root.iterdir() if p.is_dir() and not p.name.startswith(".")]
        self.assertEqual(len(dirs), 1)
        return dirs[0]

    def test_unified_exclusions_in_vault_layout_auto_push_and_conflict_guard(self) -> None:
        # 1. Create Vault
        rc, env, raw = _run_cli(self.ws_root, self.runtime_dir, ["--action", "vault-create", "--title", "SyncTest"])
        self.assertEqual(rc, 0, raw)
        vref = env["data"]["ref"]
        vdir = self._vault_dir()
        self.assertIn("dropzone", env["data"]["doc_git"]["included_scopes"])
        self.assertIn("obsidian", env["data"]["doc_git"]["ignored_scopes"])

        # 2. Reject plaintext credentials in remote_url
        rc_bad, env_bad, _ = _run_cli(
            self.ws_root,
            self.runtime_dir,
            ["--action", "doc-remote", "--ref", vref, "--remote_url", "https://ghp_secret@github.com/u/r.git"],
        )
        self.assertNotEqual(rc_bad, 0)
        self.assertEqual(env_bad["code"], "E_PARAM_INVALID")

        # 3. Bind remote + switch to in-vault layout for Obsidian Git plugin compatibility
        rc_rem, env_rem, raw_rem = _run_cli(
            self.ws_root,
            self.runtime_dir,
            [
                "--action",
                "doc-remote",
                "--ref",
                vref,
                "--remote_url",
                str(self.bare_remote),
                "--branch",
                "main",
                "--git_layout",
                "in-vault",
            ],
        )
        self.assertEqual(rc_rem, 0, raw_rem)
        self.assertEqual(env_rem["code"], "OK_DOC_REMOTE")
        self.assertTrue(env_rem["data"]["configured"])
        self.assertTrue(env_rem["data"]["auto_push"])
        self.assertEqual(env_rem["data"]["mode"], "in_vault_git")
        self.assertTrue((vdir / ".git" / "HEAD").is_file())
        # Zero-path leak check: local bare remote path must be masked
        self.assertNotIn(str(self.bare_remote), raw_rem)
        self.assertTrue(str(env_rem["data"]["remote_url"]).startswith("local-remote:"))

        # 4. Write files across included (00-DropZone) & excluded (.obsidian, source, 98-Archives, .trash)
        (vdir / "00-DropZone" / "agent-chat").mkdir(parents=True, exist_ok=True)
        (vdir / "00-DropZone" / "agent-chat" / "chat-01.md").write_text("# DropZone Sync\nhello\n", encoding="utf-8")
        (vdir / ".obsidian").mkdir(parents=True, exist_ok=True)
        (vdir / ".obsidian" / "workspace.json").write_text('{"x":1}\n', encoding="utf-8")
        (vdir / "98-Archives").mkdir(parents=True, exist_ok=True)
        (vdir / "98-Archives" / "old.txt").write_text("archive\n", encoding="utf-8")
        (vdir / "source").mkdir(parents=True, exist_ok=True)
        (vdir / "source" / "raw.bin").write_text("src\n", encoding="utf-8")
        (vdir / ".trash" / "trashed.md").write_text("trash\n", encoding="utf-8")

        # 5. doc-commit with default auto_push=true pushes DropZone & excludes .obsidian/archives/source/trash
        rc_cm, env_cm, raw_cm = _run_cli(
            self.ws_root,
            self.runtime_dir,
            ["--action", "doc-commit", "--ref", vref, "--reason", "push dropzone test"],
        )
        self.assertEqual(rc_cm, 0, raw_cm)
        self.assertTrue(env_cm["data"]["committed"])
        self.assertTrue(env_cm["data"]["remote_sync"]["pushed"])
        self.assertEqual(env_cm["data"]["remote_sync"]["status"], "pushed")

        tracked = _git(self.bare_remote, "ls-tree", "-r", "--name-only", "main").stdout.splitlines()
        self.assertIn("00-DropZone/agent-chat/chat-01.md", tracked)
        self.assertFalse(any(p.startswith(".obsidian/") for p in tracked))
        self.assertFalse(any(p.startswith("98-Archives/") for p in tracked))
        self.assertFalse(any(p.startswith("source/") for p in tracked))
        self.assertFalse(any(p.startswith(".trash/") for p in tracked))

        # 6. Simulate another device (Gemini MCP / Mobile) pushing a new DropZone file
        peer = self.root / "peer"
        _git(self.root, "clone", str(self.bare_remote), str(peer))
        (peer / "00-DropZone" / "agent-chat" / "from-mobile.md").write_text(
            "---\ndoc_type: memo\ntitle: \"Mobile Note\"\nstatus: draft\n---\n\nhello from mobile\n",
            encoding="utf-8",
        )
        _git(peer, "add", "-A")
        _git(peer, "commit", "-m", "mobile: add dropzone note")
        _git(peer, "push", "origin", "main")

        # 7. doc-sync pulls peer commit cleanly & reconciles index
        rc_sy, env_sy, raw_sy = _run_cli(
            self.ws_root,
            self.runtime_dir,
            ["--action", "doc-sync", "--ref", vref, "--direction", "both"],
        )
        self.assertEqual(rc_sy, 0, raw_sy)
        self.assertTrue(env_sy["data"]["pulled"])
        self.assertTrue((vdir / "00-DropZone" / "agent-chat" / "from-mobile.md").is_file())
        self.assertIn("reconciled_index", env_sy["data"])

        # 8. Simulate conflicting edit on the same file -> fail-closed diverged guard (no force push)
        (peer / "00-DropZone" / "agent-chat" / "chat-01.md").write_text("# DropZone Sync\npeer-line\n", encoding="utf-8")
        _git(peer, "add", "-A")
        _git(peer, "commit", "-m", "peer conflicting edit")
        _git(peer, "push", "origin", "main")

        (vdir / "00-DropZone" / "agent-chat" / "chat-01.md").write_text("# DropZone Sync\nlocal-line\n", encoding="utf-8")
        rc_cm2, env_cm2, raw_cm2 = _run_cli(
            self.ws_root,
            self.runtime_dir,
            ["--action", "doc-commit", "--ref", vref, "--push", "false", "--reason", "local edit"],
        )
        self.assertEqual(rc_cm2, 0, raw_cm2)
        self.assertTrue(env_cm2["data"]["committed"])
        self.assertEqual(env_cm2["data"]["remote_sync"]["status"], "skipped_by_flag")

        rc_div, env_div, raw_div = _run_cli(
            self.ws_root,
            self.runtime_dir,
            ["--action", "doc-sync", "--ref", vref, "--direction", "both"],
        )
        self.assertNotEqual(rc_div, 0, raw_div)
        self.assertEqual(env_div["code"], "E_DOC_SYNC_DIVERGED")
        # Local working tree must remain clean (rebase aborted cleanly)
        self.assertEqual((vdir / "00-DropZone" / "agent-chat" / "chat-01.md").read_text(encoding="utf-8"), "# DropZone Sync\nlocal-line\n")

    def test_bootstrap_adopt_remote_for_unrelated_histories(self) -> None:
        # Populate remote with an independent existing Vault history
        seed = self.root / "seed"
        seed.mkdir()
        _git(seed, "init", "-b", "main")
        (seed / ".gitignore").write_text("!00-DropZone/\n!00-DropZone/**\n.obsidian/\n.trash/\n98-Archives/\nsource/\n", encoding="utf-8")
        (seed / "00-DropZone" / "agent-chat").mkdir(parents=True)
        (seed / "00-DropZone" / "agent-chat" / "remote-doc.md").write_text(
            "---\ndoc_type: memo\ntitle: \"Remote Seed\"\nstatus: draft\n---\n\nbody\n",
            encoding="utf-8",
        )
        (seed / "05-Journal").mkdir(parents=True)
        (seed / "05-Journal" / "2026-11-21.md").write_text("# Journal\n", encoding="utf-8")
        (seed / "30-Projects" / "2611-agent-chat").mkdir(parents=True)
        (seed / "30-Projects" / "2611-agent-chat" / "Manifest-2611-agent-chat.md").write_text(
            "---\ndoc_type: manifest\ntitle: \"Agent Chat\"\nstatus: active\n---\n",
            encoding="utf-8",
        )
        _git(seed, "add", "-A")
        _git(seed, "commit", "-m", "vault backup: remote seed")
        _git(seed, "remote", "add", "origin", str(self.bare_remote))
        _git(seed, "push", "-u", "origin", "main")

        # Create fresh local vault
        rc, env, raw = _run_cli(self.ws_root, self.runtime_dir, ["--action", "vault-create", "--title", "FreshVault"])
        self.assertEqual(rc, 0, raw)
        vref = env["data"]["ref"]
        vdir = self._vault_dir()

        # Bind remote + in-vault layout
        rc_r, _, raw_r = _run_cli(
            self.ws_root,
            self.runtime_dir,
            ["--action", "doc-remote", "--ref", vref, "--remote_url", str(self.bare_remote), "--git_layout", "in-vault"],
        )
        self.assertEqual(rc_r, 0, raw_r)

        # Plain doc-sync must fail-closed on unrelated histories
        rc_u, env_u, raw_u = _run_cli(self.ws_root, self.runtime_dir, ["--action", "doc-sync", "--ref", vref])
        self.assertNotEqual(rc_u, 0, raw_u)
        self.assertEqual(env_u["code"], "E_DOC_SYNC_UNRELATED_HISTORIES")

        # Explicit --bootstrap adopt-remote cleanly adopts remote history & reconciles registries
        rc_b, env_b, raw_b = _run_cli(
            self.ws_root,
            self.runtime_dir,
            ["--action", "doc-sync", "--ref", vref, "--bootstrap", "adopt-remote"],
        )
        self.assertEqual(rc_b, 0, raw_b)
        self.assertEqual(env_b["data"]["status"], "bootstrapped_from_remote")
        self.assertTrue((vdir / "00-DropZone" / "agent-chat" / "remote-doc.md").is_file())
        self.assertTrue((vdir / ".gitignore").is_file())
        self.assertGreaterEqual(env_b["data"]["reconciled_index"]["projects_indexed"], 1)
        self.assertGreaterEqual(env_b["data"]["reconciled_index"]["notes_indexed"], 1)


if __name__ == "__main__":
    unittest.main()
