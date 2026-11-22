"""Public ingest regression for concurrent preparation and stable replay."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from facade import atlas_cli as cli
from pipeline import atlas_fmt_engine as fmt
from pipeline import atlas_slicer_engine as slicer


class BatchIngestTests(unittest.TestCase):
    def test_partial_batch_prepares_outside_lock_and_reuses_handles(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            (root / "Volumes/GewuAtlas-data").mkdir(parents=True)
            environment = {"GEWU_ATLAS_WORKSPACE_ROOT": str(root), "GEWU_APP_RUNTIME_DIR": str(root / "rt"),
                           "GEWU_TICKET_VOLUME": str(root / "tickets")}
            with patch.dict(os.environ, environment):
                subprocess.run([sys.executable, "-I", "-B", str(cli.APP_DIR.parent / "GewuTicket/scripts/ticket/ticket_cli.py"), "--action", "init-volume",
                                "--volume", environment["GEWU_TICKET_VOLUME"], "--mount-mode", "rw"],
                               capture_output=True, check=True)
                self.check_ingest()

    def check_ingest(self):
        def call(**args):
            rc, payload = cli.run_cli_request(["--json-stdin", "-"], stdin_text=json.dumps(args))
            return rc, json.loads(payload)["data"]

        ref = call(action="vault-create", title="Batch test")[1]["ref"]
        project = call(action="project-create", ref=ref, title="Batch project", agent="tester")[1]["project_ref"]
        urls = ["https://example.org/" + name for name in ("one", "bad", "two")]
        active = threading.Event()
        rendezvous = threading.Barrier(2, timeout=10)
        state_lock = cli._atlas_state_lock

        @contextmanager
        def tracked_lock(*args, **kwargs):
            with state_lock(*args, **kwargs):
                active.set()
                try:
                    yield
                finally:
                    active.clear()

        def download(vault, url, stage, **kwargs):
            self.assertFalse(active.is_set())
            if url == urls[1]:
                raise fmt.VaultStoreError("E_URL_NETWORK", "fixture unavailable")
            rendezvous.wait()
            path = stage / (url.rsplit("/", 1)[1] + ".txt")
            path.write_text("Example source material. " * 100, encoding="utf-8")
            return path

        def fetch(items):
            return call(action="ingest", ref=ref, project_ref=project, urls=items, auto_slice=True)

        with patch.object(cli, "_atlas_state_lock", tracked_lock), patch.object(fmt, "_download_url_safe", download), \
             patch.object(slicer, "_scan_vault_markdowns", wraps=slicer._scan_vault_markdowns) as scan:
            _, first = fetch(urls)
            self.assertEqual([r["source_name"] for r in first["results"]], [urls[0], urls[2]])
            self.assertEqual([r["code"] for r in first["blocked"]], ["E_URL_NETWORK"])
            self.assertEqual(scan.call_count, 1)
        refs = [row["note_ref"] for row in first["results"]]
        self.assertEqual(len(set(refs)), 2)
        with patch.object(fmt, "_download_url_safe", side_effect=AssertionError("replay downloaded")), \
             patch.object(fmt, "_resolve_fmt_python", side_effect=AssertionError("replay probed converter")):
            rc, replay = fetch([urls[0], urls[2]])
        self.assertEqual(rc, 0)
        self.assertEqual([r["note_ref"] for r in replay["results"]], refs)
        for before, after in zip(first["auto_slices"], replay["auto_slices"]):
            self.assertTrue(after["already_sliced"])
            self.assertEqual(before["toc_note_ref"], after["toc_note_ref"])
            self.assertEqual([s["note_ref"] for s in before["slices"]], [s["note_ref"] for s in after["slices"]])

        # Commit a competing request between the first request's preflight and publication.
        competing = ["https://example.org/three", "https://example.org/four"]
        winner = {}
        prepare = fmt.UrlPreparation.prepare

        def interleaved(plan, temp):
            prepare(plan, temp)
            with patch.object(fmt.UrlPreparation, "prepare", prepare):
                rc, data = fetch(competing)
            self.assertEqual(rc, 0)
            winner.update(data)

        with patch.object(fmt.UrlPreparation, "prepare", interleaved), patch.object(fmt, "_download_url_safe", download):
            rc, losing = fetch(competing)
        self.assertEqual(rc, 0)
        self.assertTrue(all(r["already_ingested"] for r in losing["results"]))
        self.assertEqual([r["note_ref"] for r in losing["results"]], [r["note_ref"] for r in winner["results"]])
        self.assertEqual([r["toc_note_ref"] for r in losing["auto_slices"]],
                         [r["toc_note_ref"] for r in winner["auto_slices"]])
