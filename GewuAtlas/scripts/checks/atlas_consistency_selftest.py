#!/usr/bin/env python3
"""Isolated regression probes for Atlas identity, undo safety and link relocation."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from storage.note_kek_store import KekNoteRegistry
from storage.vault_kek_store import VaultStoreError, aead_xchacha20_poly1305_encrypt
from pipeline import atlas_project_engine as project
from pipeline import atlas_vfs_engine as vfs
from pipeline import atlas_fmt_engine as fmt


class AtlasConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="atlas-consistency-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.kek = b"test fixture key material only!!!"[:32]

    def test_direct_vault_purge_fails_before_touching_any_tree(self):
        from storage.vault_volume import purge_vault_tree
        active = self.root / "Volumes/GewuAtlas-data/active"
        trash = self.root / "Volumes/GewuAtlas-data/.trash/retained"
        for tree in (active, trash):
            tree.mkdir(parents=True)
            (tree / "preserve.md").write_text("keep all bytes")
        before = project._tree_fingerprint(self.root)
        with self.assertRaises(VaultStoreError) as caught:
            purge_vault_tree(self.root, "Volumes/GewuAtlas-data/active", "Volumes/GewuAtlas-data/.trash/retained")
        self.assertEqual(caught.exception.code, "E_HALT_VAULT_PURGE_DISABLED")
        self.assertEqual(project._tree_fingerprint(self.root), before)

    def test_frozen_yaml_reference_fields_are_preflighted(self):
        from notes.atlas_note_engine import _preflight_note_moves
        source = self.root / "00-DropZone/A.md"
        source.parent.mkdir(); source.write_text("source bytes")
        frozen = self.root / "30-Projects/frozen/20-Memory/21-Library/reference.md"
        frozen.parent.mkdir(parents=True)
        for field in vfs._REF_FIELDS:
            for yaml_value in ((" A", "\n  - A") if field in vfs._REF_LIST_FIELDS else (" A",)):
                with self.subTest(field=field, yaml_value=yaml_value):
                    frozen.write_text("---\n"+field+":"+yaml_value+"\n---\n")
                    before = project._tree_fingerprint(self.root)
                    with patch.object(vfs, "_is_rel_frozen", side_effect=lambda root, rel: "frozen" if rel.startswith("30-Projects/frozen/") else None):
                        with self.assertRaises(VaultStoreError) as error:
                            _preflight_note_moves(self.root, {"00-DropZone/A.md": "00-DropZone/B.md"})
                    self.assertEqual(error.exception.code, "E_DOMAIN_FROZEN")
                    self.assertEqual(project._tree_fingerprint(self.root), before)

    def test_frozen_canvas_reference_preflight_preserves_bytes(self):
        from notes.atlas_note_engine import _preflight_note_moves
        source = self.root / "00-DropZone/target.md"
        source.parent.mkdir(); source.write_text("source bytes")
        canvas = self.root / "30-Projects/frozen/30-Workbench/board.canvas"
        canvas.parent.mkdir(parents=True)
        canvas.write_text(json.dumps({"nodes": [{"id": "1", "type": "file", "file": "00-DropZone/target.md"}]}))
        before = project._tree_fingerprint(self.root)
        with patch.object(vfs, "_is_rel_frozen", side_effect=lambda root, rel: "frozen" if rel.startswith("30-Projects/frozen/") else None):
            with self.assertRaises(VaultStoreError) as error:
                _preflight_note_moves(self.root, {"00-DropZone/target.md": "20-AI-Zone/30-Workbench/target.md"})
        self.assertEqual(error.exception.code, "E_DOMAIN_FROZEN")
        self.assertEqual(project._tree_fingerprint(self.root), before)

    def registry(self):
        return KekNoteRegistry("audit-fixture", self.root / "notes.bin", self.kek)

    def test_deleted_handle_never_reused_across_restart(self):
        first = self.registry().sync_paths("vlt_01", ["00-DropZone/a.md"])["00-DropZone/a.md"]
        self.registry().sync_paths("vlt_01", [])
        new = self.registry().sync_paths("vlt_01", ["00-DropZone/a.md"])["00-DropZone/a.md"]
        self.assertNotEqual(first, new)
        with self.assertRaises(VaultStoreError):
            self.registry().resolve_handle("vlt_01", first)

    def test_legacy_container_carries_forward_maximum(self):
        reg = self.registry()
        reg.sync_paths("vlt_01", ["00-DropZone/a.md", "00-DropZone/b.md"])
        rows = reg._load_records()
        # Old encrypted envelope deliberately lacks a high-water field.
        nonce = b"n" * 24
        raw = json.dumps({"app_id": reg.app_id, "schema": "gewu.atlas-notes/1", "rows": rows}).encode()
        ct, tag = aead_xchacha20_poly1305_encrypt(reg._kek, nonce, raw, reg._file_aad())
        reg.enc_path.write_bytes(b"GWNA1" + nonce + tag + ct)
        self.registry().sync_paths("vlt_01", [])
        new = self.registry().sync_paths("vlt_01", ["00-DropZone/c.md"])
        self.assertEqual(new["00-DropZone/c.md"], "nte_03")

    def test_handle_capacity_fails_without_reusing_identity(self):
        reg = self.registry(); reg._last_note_seq = 999999; reg._save_records([])
        with self.assertRaises(VaultStoreError) as error:
            self.registry().sync_paths("vlt_01", ["00-DropZone/a.md"])
        self.assertEqual(error.exception.code, "E_HALT_CAPACITY")

    def snapshot(self, *, external=False):
        vault = self.root / "vault"; state = self.root / "state"
        rels = ["30-Projects/alpha", "30-Projects/beta"]
        for rel in rels:
            path = vault / rel; path.mkdir(parents=True)
            (path / "note.md").write_text("before " + rel)
        extra = "20-AI-Zone/20-Memory/21-Library/reference.md"
        (vault / extra).parent.mkdir(parents=True)
        (vault / extra).write_text("external before")
        meta = project._save_pre_op_snapshot(state_dir=state, vault_ref="vlt_01", op_id="op_01",
                    op_kind="merge", title="fixture", vault_dir=vault, affected_project_rels=rels,
                    external_backups={extra: b"external before"})
        for rel in rels:
            (vault / rel / "note.md").write_text("after " + rel)
        if external:
            (vault / extra).write_text("external after")
        doc = json.loads(meta.read_text())
        project._seal_project_snapshot(meta, vault, doc)
        return vault, meta, meta.parent / "op_01", json.loads(meta.read_text()), extra

    def test_missing_later_snapshot_leaves_all_projects_intact(self):
        vault, meta, snap, doc, _ = self.snapshot()
        shutil.rmtree(snap / "projects/beta")
        before = project._tree_fingerprint(vault)
        with self.assertRaises(VaultStoreError):
            project._apply_project_undo(vault, snap, meta, doc)
        self.assertEqual(project._tree_fingerprint(vault), before)

    def test_corrupt_snapshot_leaves_live_bytes_intact(self):
        vault, meta, snap, doc, _ = self.snapshot()
        (snap / "projects/beta/note.md").write_text("corrupt")
        before = project._tree_fingerprint(vault)
        with self.assertRaises(VaultStoreError):
            project._apply_project_undo(vault, snap, meta, doc)
        self.assertEqual(project._tree_fingerprint(vault), before)

    def test_undo_rejects_added_edited_and_deleted_content(self):
        for kind in ("add", "edit", "delete"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as td:
                oldroot = self.root; self.root = Path(td)
                try:
                    vault, meta, snap, doc, _ = self.snapshot()
                    target = vault / "30-Projects/alpha/note.md"
                    if kind == "add": (target.parent / "new.md").write_text("human work")
                    elif kind == "edit": target.write_text("human work")
                    else: target.unlink()
                    before = project._tree_fingerprint(vault)
                    with self.assertRaises(VaultStoreError) as error:
                        project._apply_project_undo(vault, snap, meta, doc)
                    self.assertEqual(error.exception.code, "E_UNDO_CONFLICT")
                    self.assertEqual(project._tree_fingerprint(vault), before)
                finally: self.root = oldroot

    def test_legacy_snapshot_requires_manual_verification(self):
        vault, meta, snap, doc, _ = self.snapshot(); doc.pop("snapshot_version")
        before = project._tree_fingerprint(vault)
        with self.assertRaises(VaultStoreError) as error:
            project._apply_project_undo(vault, snap, meta, doc)
        self.assertEqual(error.exception.code, "E_UNDO_UNVERIFIED")
        self.assertEqual(project._tree_fingerprint(vault), before)

    def test_undo_preserves_unrelated_external_edits(self):
        vault, meta, snap, doc, extra = self.snapshot()
        (vault / extra).write_text("later unrelated human edit")
        project._apply_project_undo(vault, snap, meta, doc)
        self.assertEqual((vault / extra).read_text(), "later unrelated human edit")
        self.assertEqual((vault / "30-Projects/alpha/note.md").read_text(), "before 30-Projects/alpha")

    def test_undo_blocks_changed_external_reference(self):
        vault, meta, snap, doc, extra = self.snapshot(external=True)
        (vault / extra).write_text("human edit")
        before = project._tree_fingerprint(vault)
        with self.assertRaises(VaultStoreError): project._apply_project_undo(vault, snap, meta, doc)
        self.assertEqual(project._tree_fingerprint(vault), before)

    def test_restore_write_failure_recovers_current_content(self):
        vault, meta, snap, doc, _ = self.snapshot()
        before = project._tree_fingerprint(vault)
        real_copy = shutil.copytree
        calls = []
        def failing_copy(src, dst, *args, **kwargs):
            calls.append(str(src))
            if len(calls) == 2: raise OSError("synthetic full disk")
            return real_copy(src, dst, *args, **kwargs)
        with patch.object(project.shutil, "copytree", side_effect=failing_copy):
            with self.assertRaises(OSError): project._apply_project_undo(vault, snap, meta, doc)
        self.assertEqual(project._tree_fingerprint(vault), before)
        self.assertEqual(json.loads(meta.read_text())["status"], "undo_failed_restored")
        self.assertTrue((snap / "undo-current/projects/alpha/note.md").is_file())

    def test_unresolved_recovery_is_exempt_from_retention(self):
        from notes.atlas_change_engine import _purge_expired_project_undo
        vault, meta, snap, doc, _ = self.snapshot()
        doc["created_at"] = "2000-01-01T00:00:00+00:00"
        doc["status"] = "undo_started"
        meta.write_text(json.dumps(doc))
        count = _purge_expired_project_undo(self.root / "state", "vlt_01", 30, False)
        self.assertEqual(count, 0)
        self.assertTrue(meta.is_file())
        self.assertTrue(snap.is_dir())

    def test_undo_keeps_complete_large_current_backup(self):
        vault, meta, snap, doc, _ = self.snapshot()
        target = vault / "30-Projects/alpha/large.bin"
        payload = b"x" * (2 * 1024 * 1024 + 17)
        target.write_bytes(payload)
        project._seal_project_snapshot(meta, vault, doc)
        project._apply_project_undo(vault, snap, meta, doc)
        self.assertFalse(target.exists())
        self.assertEqual((snap / "undo-current/projects/alpha/large.bin").read_bytes(), payload)

    def test_snapshot_symlink_rejected_before_live_mutation(self):
        vault, meta, snap, doc, _ = self.snapshot()
        backup = snap / "projects/alpha/note.md"
        backup.unlink(); backup.symlink_to(vault / "30-Projects/alpha/note.md")
        before = project._tree_fingerprint(vault)
        with self.assertRaises(VaultStoreError): project._apply_project_undo(vault, snap, meta, doc)
        self.assertEqual(project._tree_fingerprint(vault), before)

    def legacy_snapshot(self, external=True):
        vault, meta, snap, doc, extra = self.snapshot(external=external)
        doc.pop('snapshot_version'); doc.pop('post_projects')
        for p in doc['affected_projects']: p.pop('pre_tree')
        for p in doc['external_backups']:
            p.pop('pre_sha256'); p.pop('post_sha256')
        meta.write_text(json.dumps(doc))
        return vault, meta, snap, doc, extra

    def test_legacy_preview_is_read_only_and_exposes_actual_changes(self):
        vault, meta, snap, doc, extra = self.legacy_snapshot()
        before = project._tree_fingerprint(self.root)
        _, view = project._legacy_recovery_plan(vault, snap, doc)
        self.assertEqual(project._tree_fingerprint(self.root), before)
        self.assertEqual(view['changed_count'], 3)
        self.assertFalse(view['historical_integrity_verified'])
        self.assertEqual(len(view['expected_sha256']), 64)
        self.assertNotIn(str(vault), json.dumps(view))

    def test_legacy_confirmed_recovery_keeps_current_bytes_and_original_metadata(self):
        vault, meta, snap, doc, extra = self.legacy_snapshot()
        _, view = project._legacy_recovery_plan(vault, snap, doc)
        project._apply_project_undo(vault, snap, meta, doc, expected_sha256=view['expected_sha256'])
        self.assertEqual((vault/extra).read_text(), 'external before')
        self.assertEqual((snap/'undo-current/projects/alpha/note.md').read_text(), 'after 30-Projects/alpha')
        saved = json.loads(meta.read_text())
        self.assertNotIn('snapshot_version', saved)
        self.assertNotIn('post_projects', saved)
        self.assertEqual(saved['recovery_review']['basis'], 'explicit-current-state-review')
        self.assertFalse(saved['recovery_review']['historical_integrity_verified'])

    def test_legacy_stale_confirmation_blocks_live_backup_and_metadata_changes(self):
        for kind in ('live', 'backup', 'metadata'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as td:
                old = self.root; self.root = Path(td)
                try:
                    vault, meta, snap, doc, _ = self.legacy_snapshot()
                    _, view = project._legacy_recovery_plan(vault, snap, doc)
                    if kind == 'live': (vault/'30-Projects/alpha/later.md').write_text('new')
                    elif kind == 'backup': (snap/'projects/alpha/note.md').write_text('changed backup')
                    else: doc['title'] = 'changed metadata'
                    before = project._tree_fingerprint(vault)
                    with self.assertRaises(VaultStoreError) as error:
                        project._apply_project_undo(vault, snap, meta, doc, expected_sha256=view['expected_sha256'])
                    self.assertEqual(error.exception.code, 'E_UNDO_CONFLICT')
                    self.assertEqual(project._tree_fingerprint(vault), before)
                    self.assertFalse((snap/'undo-current').exists())
                finally: self.root = old

    def test_legacy_missing_backup_and_symlink_are_rejected(self):
        vault, meta, snap, doc, _ = self.legacy_snapshot()
        shutil.rmtree(snap/'projects/beta')
        with self.assertRaises(VaultStoreError): project._legacy_recovery_plan(vault, snap, doc)
        (snap/'projects/beta').symlink_to(vault/'30-Projects/beta', target_is_directory=True)
        with self.assertRaises(VaultStoreError): project._legacy_recovery_plan(vault, snap, doc)

    def test_legacy_preview_preserves_journal_and_system_logs(self):
        vault, meta, snap, doc, _ = self.legacy_snapshot()
        for rel in ('05-Journal/today.md', '90-System/92-Logs/audit.md'):
            p = vault/rel; p.parent.mkdir(parents=True); p.write_text('retain')
            doc['external_backups'].append({'rel_path':rel,'blob_name':'absent-protected.bin'})
        _, view = project._legacy_recovery_plan(vault, snap, doc)
        self.assertEqual(view['protected_entries_skipped'], 2)
        project._apply_project_undo(vault, snap, meta, doc, expected_sha256=view['expected_sha256'])
        self.assertEqual((vault/'05-Journal/today.md').read_text(), 'retain')
        self.assertEqual((vault/'90-System/92-Logs/audit.md').read_text(), 'retain')

    def test_legacy_preview_binary_is_bounded_and_incomplete_backup_blocks(self):
        vault, meta, snap, doc, _ = self.legacy_snapshot()
        (vault/'30-Projects/alpha/large.bin').write_bytes(b'\xff'*9000)
        _, view = project._legacy_recovery_plan(vault, snap, doc)
        binary = [i for i in view['changed_items'] if i['name']=='large.bin'][0]
        self.assertEqual(binary['current'], {'binary':True, 'bytes':9000})
        (snap/'undo-current').mkdir()
        with self.assertRaises(VaultStoreError): project._legacy_recovery_plan(vault, snap, doc)

    def test_project_undo_without_op_is_governed_mutation(self):
        from facade.atlas_cli import _is_non_mutating_vault_op
        self.assertFalse(_is_non_mutating_vault_op('project-undo', {}))
        self.assertTrue(_is_non_mutating_vault_op('project-undo', {'dry_run':True, 'mode':'legacy-recovery'}))

    def test_outgoing_relative_links_and_code_examples(self):
        old = "00-DropZone/a.md"; new = "20-AI-Zone/20-Memory/21-Library/a.md"
        raw = '[B](./b.md#part) [B](b.md) ![pic](./photo.png) [[./b#part|B]]\n`[B](./b.md)`\n```md\n[B](./b.md)\n```\n'
        out = vfs._rewrite_prose(raw, lambda segment: vfs._rewrite_moved_links(segment, old, new, {old:new}))
        self.assertIn('[B](../../../00-DropZone/b.md#part)', out)
        self.assertIn('![pic](../../../00-DropZone/photo.png)', out)
        self.assertIn('[[../../../00-DropZone/b#part|B]]', out)
        self.assertIn('`[B](./b.md)`', out)
        self.assertIn('```md\n[B](./b.md)\n```', out)

    def test_folder_internal_links_rebased_once(self):
        moves = {"00-DropZone/old/a.md":"00-DropZone/new/a.md", "00-DropZone/old/b.md":"00-DropZone/new/b.md"}
        out = vfs._rewrite_moved_links('[B](./b.md) [[./b|B]]', "00-DropZone/old/a.md", moves["00-DropZone/old/a.md"], moves)
        self.assertEqual(out, '[B](./b.md) [[./b|B]]')

    def test_manage_secret_marker_detection_remains_enabled(self):
        from manage import app_manage
        for marker in ("SECRET" + ":", "API_KEY" + "=", "TOKEN" + "=", "BEGIN" + " PRIVATE KEY"):
            self.assertIn("secret_leak_marker", app_manage._scan_forbidden("scripts/probe.py", marker + "fixture", allow_raw_sql=True))

    def test_manage_status_in_nested_domain_excludes_sibling(self):
        from manage import app_manage
        repo = self.root / "repository"; domain = repo / "Atlas"
        (domain / "scripts").mkdir(parents=True)
        (repo / "Ticket/scripts").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        (domain / "scripts/change.py").write_text("# fixture\n")
        (repo / "Ticket/scripts/other.py").write_text("# sibling\n")
        with patch.object(app_manage, "APP_ROOT", domain):
            status = app_manage._git_status_files()
        self.assertEqual(status["pending_system_files"], ["scripts/change.py"])
        self.assertIn("Ticket/scripts/other.py", status["other_dirty_files"])

    def test_network_preview_does_not_resolve_dns(self):
        with patch("socket.getaddrinfo", side_effect=AssertionError("no DNS in preview")):
            fmt.check_url(self.root, "https://example.com/article", resolve_dns=False)
        with self.assertRaises(VaultStoreError) as error:
            fmt.check_url(self.root, "http://127.0.0.1/private", resolve_dns=False)
        self.assertEqual(error.exception.code, "E_URL_POLICY")

    def test_network_error_is_not_internal_error(self):
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("unavailable")):
            with self.assertRaises(VaultStoreError) as error: fmt.check_url(self.root, "https://example.com/article")
        self.assertEqual(error.exception.code, "E_URL_NETWORK")


class AtlasConsistencyGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="atlas-gateway-consistency-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name); (root / "ws").mkdir()
        self.env = dict(os.environ, GEWU_ATLAS_WORKSPACE_ROOT=str(root / "ws"), GEWU_APP_RUNTIME_DIR=str(root / "rt"))
        self.cli = SCRIPTS / "facade/atlas_cli.py"
        self.ref = self.call("vault-create", vault="audit", title="Audit")["ref"]

    def call(self, action, **kwargs):
        args = [sys.executable, "-I", "-B", str(self.cli), "--action", action]
        for key, value in kwargs.items(): args += ["--"+key, str(value)]
        p = subprocess.run(args, env=self.env, capture_output=True, text=True, timeout=40)
        doc = json.loads(p.stdout)
        self.assertEqual(p.returncode, 0, doc.get("code"))
        return doc["data"]

    def test_vault_purge_rejected_active_deleted_and_journal_preserved(self):
        self.call("journal-today", ref=self.ref, date="2026-11-15")
        ws = Path(self.env["GEWU_ATLAS_WORKSPACE_ROOT"])
        for deleted in (False, True):
            if deleted:
                self.call("vault-delete", ref=self.ref)
            before = project._tree_fingerprint(ws)
            result = subprocess.run(
                [sys.executable, "-I", "-B", str(self.cli), "--action", "vault-delete",
                 "--ref", self.ref, "--purge", "true"],
                env=self.env, capture_output=True, text=True, timeout=40,
            )
            data = json.loads(result.stdout)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(data["code"], "E_HALT_VAULT_PURGE_DISABLED")
            self.assertIn("GUARD: HALTED", result.stdout)
            self.assertEqual(project._tree_fingerprint(ws), before)
        self.call("vault-restore", ref=self.ref)
        self.call("journal-show", ref=self.ref, date="2026-11-15")

    def test_search_preserves_business_tree_with_runtime_cache(self):
        note = self.call("note-create", ref=self.ref, zone="workbench", title="Search boundary", content="uniquequerytoken")
        ws = Path(self.env["GEWU_ATLAS_WORKSPACE_ROOT"])
        before = project._tree_fingerprint(ws)
        self.call("note-search", ref=self.ref, query="uniquequerytoken", deep="true")
        self.assertEqual(project._tree_fingerprint(ws), before)

    def same_title_link_fixture(self):
        projects, targets, sources = [], [], []
        for name in ("alpha", "beta"):
            prj = self.call("project-create", ref=self.ref, project_id=name, title=name)["project_ref"]
            projects.append(prj)
            self.call("note-create", ref=self.ref, project_ref=prj, zone="workbench", title="Peer", content="peer-"+name)
            targets.append(self.call("note-create", ref=self.ref, project_ref=prj, zone="workbench", title="Same", content="marker-"+name+"\n\n[[../30-Workbench/Peer#Details|peer]]")["note_ref"])
            sources.append(self.call("note-create", ref=self.ref, zone="workbench", title="Incoming-"+name, content="Source "+name+"\n\n```md\n[[Same]]\n```\n\n`[[Same]]`")["note_ref"])
        return projects, targets, sources

    def test_same_title_handle_links_and_merge_rename_keep_identity(self):
        projects, targets, sources = self.same_title_link_fixture()
        for source, target in zip(sources, targets):
            self.assertTrue(self.call("note-link", ref=self.ref, note_ref=source, target_ref=target)["wikilink_added"])
            self.assertFalse(self.call("note-link", ref=self.ref, note_ref=source, target_ref=target)["wikilink_added"])
        # Both distinct targets may be added from one source; stem equality is not identity.
        self.assertTrue(self.call("note-link", ref=self.ref, note_ref=sources[0], target_ref=targets[1])["wikilink_added"])
        before = [self.call("note-get", ref=self.ref, note_ref=t) for t in targets]
        self.assertEqual(before[0]["backlink_refs"], [sources[0]])
        self.assertEqual(set(before[1]["backlink_refs"]), set(sources))
        merged = self.call("project-merge", ref=self.ref, source_refs=",".join(projects), project_id="merged", title="Merged", on_conflict="rename")
        first = self.call("note-get", ref=self.ref, note_ref=sources[0])
        second = self.call("note-get", ref=self.ref, note_ref=sources[1])
        self.assertEqual({x["note_ref"] for x in first["outgoing_links"]}, set(targets))
        self.assertEqual([x["note_ref"] for x in second["outgoing_links"]], [targets[1]])
        for i, target in enumerate(targets):
            after = self.call("note-get", ref=self.ref, note_ref=target)
            self.assertIn("marker-"+("alpha", "beta")[i], after["content"])
            self.assertEqual([x["note_ref"] for x in after["outgoing_links"]], [x["note_ref"] for x in before[i]["outgoing_links"]])
            self.assertIn("#Details|peer", after["content"])
        self.assertIn("```md\n[[Same]]\n```", first["content"])
        self.assertIn("`[[Same]]`", first["content"])
        self.assertEqual(self.call("health-check", ref=self.ref)["broken_wikilink_count"], 0)
        self.assertEqual(self.call("vfs-links", ref=self.ref)["broken_count"], 0)
        self.call("project-undo", ref=self.ref, op_id=merged["op_id"])
        for i, target in enumerate(targets):
            restored = self.call("note-get", ref=self.ref, note_ref=target)
            self.assertEqual(restored["content_sha256"], before[i]["content_sha256"])
        self.assertEqual({x["note_ref"] for x in self.call("note-get", ref=self.ref, note_ref=sources[0])["outgoing_links"]}, set(targets))


    def test_legacy_ambiguous_link_is_not_resolved_or_mutated_by_merge(self):
        projects, targets, sources = self.same_title_link_fixture()
        legacy = self.call("note-create", ref=self.ref, zone="workbench", title="Legacy", content="[[Same]]")["note_ref"]
        original = self.call("note-get", ref=self.ref, note_ref=legacy)
        link = original["outgoing_links"][0]
        self.assertIsNone(link["note_ref"])
        self.assertEqual(link["resolution"], "ambiguous")
        self.assertEqual(set(link["candidate_refs"]), set(targets))
        p = subprocess.run([sys.executable, "-I", "-B", str(self.cli), "--action", "project-merge", "--ref", self.ref, "--source_refs", ",".join(projects), "--title", "Merged", "--on_conflict", "rename"], env=self.env, capture_output=True, text=True, timeout=40)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stdout)["code"], "E_AMBIGUOUS_LINK")
        self.assertEqual(self.call("note-get", ref=self.ref, note_ref=legacy)["content_sha256"], original["content_sha256"])
        for target in targets:
            self.assertNotIn(legacy, self.call("note-get", ref=self.ref, note_ref=target)["backlink_refs"])
        self.assertEqual(len(self.call("project-list", ref=self.ref)["projects"]), 2)

    def test_canonical_link_survives_route_promote_vfs_project_split_and_undo(self):
        prj = self.call("project-create", ref=self.ref, project_id="origin", title="Origin")["project_ref"]
        source = self.call("note-create", ref=self.ref, zone="workbench", title="External", content="keep example `[[Original]]`")["note_ref"]
        target = self.call("note-create", ref=self.ref, zone="dropzone", title="Original", summary="Complete summary", content="moving content")["note_ref"]
        self.call("note-link", ref=self.ref, note_ref=source, target_ref=target)
        def check():
            note = self.call("note-get", ref=self.ref, note_ref=source)
            self.assertEqual([x["note_ref"] for x in note["outgoing_links"]], [target])
            self.assertIn("`[[Original]]`", note["content"])
            self.assertEqual(self.call("vfs-links", ref=self.ref)["broken_count"], 0)
        self.call("note-route", ref=self.ref, note_ref=target, project_ref=prj, scope="project")
        check()
        self.call("note-promote", ref=self.ref, note_ref=target, target_zone="atomics")
        check()
        self.call("vfs-move", ref=self.ref, note_ref=target, target_zone="library")
        check()
        self.call("project-rename", ref=self.ref, project_ref=prj, new_project_id="renamed")
        check()
        original = self.call("note-get", ref=self.ref, note_ref=target)
        external = self.call("note-get", ref=self.ref, note_ref=source)
        split = self.call("project-split", ref=self.ref, project_ref=prj, title="Branch", new_project_id="branch", note_refs=target)
        check()
        self.call("project-undo", ref=self.ref, op_id=split["op_id"])
        check()
        self.assertEqual(self.call("note-get", ref=self.ref, note_ref=target)["content_sha256"], original["content_sha256"])
        self.assertEqual(self.call("note-get", ref=self.ref, note_ref=source)["content_sha256"], external["content_sha256"])
        draft = self.call("note-create", ref=self.ref, zone="workbench", title="Draft", content="draft body")["note_ref"]
        source2 = self.call("note-create", ref=self.ref, zone="workbench", title="External draft", content="inbound")["note_ref"]
        self.call("note-link", ref=self.ref, note_ref=source2, target_ref=draft)
        self.call("artifact-adopt", ref=self.ref, note_ref=draft, project_ref=prj)
        self.assertEqual([x["note_ref"] for x in self.call("note-get", ref=self.ref, note_ref=source2)["outgoing_links"]], [draft])
        self.assertEqual(self.call("vfs-links", ref=self.ref)["broken_count"], 0)

    def test_frozen_references_refuse_all_movers_before_business_changes(self):
        for operation in ("promote", "route", "adopt", "vfs", "folder", "rename", "merge", "split"):
            with self.subTest(operation=operation):
                self.setUp()  # A fresh backend per rejection; never retry a guarded request.
                active = self.call("project-create", ref=self.ref, project_id="active", title="Active")["project_ref"]
                other = self.call("project-create", ref=self.ref, project_id="other", title="Other")["project_ref"]
                protected = self.call("project-create", ref=self.ref, project_id="protected", title="Protected")["project_ref"]
                zone = "dropzone" if operation == "route" else ("library" if operation == "promote" else "workbench")
                args = dict(ref=self.ref, project_ref=active, zone=zone, title="Moving", content="unchanged moving bytes", summary="Complete summary")
                if operation == "folder":
                    args["subdir"] = "draft"
                    target = self.call("vfs-create", **args)["note_ref"]
                else:
                    target = self.call("note-create", **args)["note_ref"]
                source = self.call("note-create", ref=self.ref, project_ref=protected, zone="library", title="Frozen reference", content="immutable reference")["note_ref"]
                self.call("note-link", ref=self.ref, note_ref=source, target_ref=target)
                self.call("project-archive", ref=self.ref, project_ref=protected)
                originals = {n: self.call("note-get", ref=self.ref, note_ref=n) for n in (target, source)}
                projects_before = self.call("project-list", ref=self.ref, include_archived="true")
                actions = {
                    "promote": ("note-promote", dict(note_ref=target, target_zone="atomics")),
                    "route": ("note-route", dict(note_ref=target, project_ref=active)),
                    "adopt": ("artifact-adopt", dict(note_ref=target, project_ref=active)),
                    "vfs": ("vfs-move", dict(note_ref=target, target_zone="library")),
                    "folder": ("vfs-move", dict(project_ref=active, zone="workbench", subdir="draft", target_subdir="moved")),
                    "rename": ("project-rename", dict(project_ref=active, new_project_id="renamed")),
                    "merge": ("project-merge", dict(source_refs=active+","+other, title="Merged", on_conflict="rename")),
                    "split": ("project-split", dict(project_ref=active, title="Branch", note_refs=target)),
                }
                action, params = actions[operation]
                cmd = [sys.executable, "-I", "-B", str(self.cli), "--action", action, "--ref", self.ref]
                for key, value in params.items(): cmd += ["--"+key, str(value)]
                result = subprocess.run(cmd, env=self.env, capture_output=True, text=True, timeout=40)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(json.loads(result.stdout)["code"], "E_DOMAIN_FROZEN", result.stdout)
                for n, original in originals.items():
                    self.assertEqual(self.call("note-get", ref=self.ref, note_ref=n), original)
                self.assertEqual(self.call("project-list", ref=self.ref, include_archived="true"), projects_before)

    def test_frozen_code_examples_and_unaffected_references_do_not_block_move(self):
        protected = self.call("project-create", ref=self.ref, project_id="protected", title="Protected")["project_ref"]
        target = self.call("note-create", ref=self.ref, zone="workbench", title="Moving", content="moving")["note_ref"]
        other = self.call("note-create", ref=self.ref, zone="workbench", title="Staying", content="staying")["note_ref"]
        ordinary = self.call("note-create", ref=self.ref, zone="workbench", title="Live ref", content="live")["note_ref"]
        exact = self.call("note-link", ref=self.ref, note_ref=ordinary, target_ref=target)["wikilink"]
        frozen = self.call("note-create", ref=self.ref, project_ref=protected, zone="library", title="Examples", content="```md\n"+exact+"\n```\n\n`"+exact+"`")["note_ref"]
        self.call("note-link", ref=self.ref, note_ref=frozen, target_ref=other)
        self.call("project-archive", ref=self.ref, project_ref=protected)
        before = self.call("note-get", ref=self.ref, note_ref=frozen)
        self.call("vfs-move", ref=self.ref, note_ref=target, target_zone="library")
        self.assertEqual(self.call("note-get", ref=self.ref, note_ref=frozen), before)
        self.assertEqual([x["note_ref"] for x in self.call("note-get", ref=self.ref, note_ref=ordinary)["outgoing_links"]], [target])

    def legacy_operation(self):
        a = self.call('project-create', ref=self.ref, project_id='alpha', title='Alpha')['project_ref']
        b = self.call('project-create', ref=self.ref, project_id='beta', title='Beta')['project_ref']
        merged = self.call('project-merge', ref=self.ref, source_refs=a+','+b, project_id='merged', title='Merged', on_conflict='rename')
        # Only synthetic fixture runtime is inspected to emulate an installed old writer.
        files = list(Path(self.env['GEWU_APP_RUNTIME_DIR']).rglob(merged['op_id']+'.json'))
        self.assertEqual(len(files), 1)
        meta = files[0]; doc = json.loads(meta.read_text())
        doc.pop('snapshot_version'); doc.pop('post_projects')
        for entry in doc['affected_projects']: entry.pop('pre_tree')
        for entry in doc['external_backups']:
            entry.pop('pre_sha256'); entry.pop('post_sha256')
        meta.write_text(json.dumps(doc))
        return merged, meta

    def test_legacy_gateway_preview_confirm_restore_and_history(self):
        merged, meta = self.legacy_operation()
        op = merged['op_id']
        history = self.call('project-history', ref=self.ref)['operations']
        self.assertTrue(history[-1]['can_preview_legacy_recovery'])
        before = project._tree_fingerprint(Path(self.env['GEWU_ATLAS_WORKSPACE_ROOT']))
        preview = self.call('project-undo', ref=self.ref, op_id=op, mode='legacy-recovery', dry_run=True)
        self.assertEqual(project._tree_fingerprint(Path(self.env['GEWU_ATLAS_WORKSPACE_ROOT'])), before)
        result = self.call('project-undo', ref=self.ref, op_id=op, mode='legacy-recovery', expected_sha256=preview['expected_sha256'])
        self.assertEqual(result['recovery_mode'], 'legacy-recovery')
        self.assertEqual(json.loads(meta.read_text())['status'], 'undone')
        self.assertFalse(self.call('project-history', ref=self.ref)['operations'][-1]['can_preview_legacy_recovery'])

    def test_legacy_gateway_missing_confirmation_is_input_required(self):
        merged, meta = self.legacy_operation()
        result = subprocess.run([sys.executable, '-I', '-B', str(self.cli), '--action', 'project-undo', '--ref', self.ref, '--op_id', merged['op_id'], '--mode', 'legacy-recovery'], env=self.env, capture_output=True, text=True, timeout=40)
        doc = json.loads(result.stdout)
        self.assertEqual(doc['code'], 'E_UNDO_UNVERIFIED')
        self.assertEqual(doc['structuredContent']['capsule']['mode'], 'INPUT_REQUIRED')
        self.assertEqual(json.loads(meta.read_text())['status'], 'applied')
        self.assertFalse((meta.parent/merged['op_id']/'undo-current').exists())

    def test_folder_move_preserves_all_handles(self):
        refs = [self.call("vfs-create", ref=self.ref, zone="workbench", subdir="draft", title=title, content="body")["note_ref"] for title in ("alpha", "beta")]
        self.call("vfs-move", ref=self.ref, zone="workbench", subdir="draft", target_subdir="moved")
        for ref in refs: self.assertEqual(self.call("note-get", ref=self.ref, note_ref=ref)["note_ref"], ref)

    def test_project_rename_updates_external_path_link_and_preserves_handle(self):
        pref = self.call("project-create", ref=self.ref, project_id="alpha", title="Alpha")["project_ref"]
        note = self.call("vfs-create", ref=self.ref, project_ref=pref, zone="library", title="inner", content="inside")["note_ref"]
        outside = self.call("vfs-create", ref=self.ref, zone="library", title="outer", content="[[30-Projects/alpha/20-Memory/21-Library/inner#part|Alias]]")["note_ref"]
        self.call("project-rename", ref=self.ref, project_ref=pref, new_project_id="beta")
        self.assertEqual(self.call("note-get", ref=self.ref, note_ref=note)["note_ref"], note)
        body = self.call("note-get", ref=self.ref, note_ref=outside)["content"]
        self.assertIn("[[30-Projects/beta/20-Memory/21-Library/inner#part|Alias]]", body)

    def test_project_undo_refuses_later_work_through_gateway(self):
        a = self.call("project-create", ref=self.ref, project_id="alpha", title="Alpha")["project_ref"]
        b = self.call("project-create", ref=self.ref, project_id="beta", title="Beta")["project_ref"]
        merged = self.call("project-merge", ref=self.ref, source_refs=a+","+b, project_id="merged", title="Merged", on_conflict="rename")
        added = self.call("vfs-create", ref=self.ref, project_ref=merged["target_project"]["project_ref"], zone="workbench", title="Later work", content="preserve me")["note_ref"]
        result = subprocess.run([sys.executable, "-I", "-B", str(self.cli), "--action", "project-undo", "--ref", self.ref, "--op_id", merged["op_id"]], env=self.env, capture_output=True, text=True, timeout=40)
        doc = json.loads(result.stdout)
        self.assertEqual(doc["code"], "E_UNDO_CONFLICT")
        self.assertEqual(doc["structuredContent"]["capsule"]["mode"], "INPUT_REQUIRED")
        self.assertEqual(self.call("note-get", ref=self.ref, note_ref=added)["content"], "preserve me")


if __name__ == "__main__":
    unittest.main(verbosity=2)

class AtlasFormatPrimitiveTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        self.tmp = tempfile.TemporaryDirectory(prefix='atlas-fmt-regression-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'vault'; self.vault.mkdir()
        self.path = self.vault / '20-AI-Zone/20-Memory/21-Library/source.md'
        self.path.parent.mkdir(parents=True)
        self.path.write_text('---\ndoc_type: library\ntitle: Source\nstatus: active\n---\n\n## First\n\nFact 42.\n\n## Last\n\nFact 73.\n')
        rel = self.path.relative_to(self.vault).as_posix()
        self.reg = SimpleNamespace(resolve_handle=lambda v,n: {'note_rel_path':rel})
        self.common = dict(vault_dir=self.vault,vault_ref='vlt_01',note_ref='nte_01',note_reg=self.reg)

    def test_refinement_compare_and_swap_preserves_other_bytes(self):
        from pipeline.atlas_fmt_refine import read_window, apply_window
        w = read_window(**self.common)
        original = self.path.read_text()
        replacement = w['content'].replace('Fact 42.', 'Fact **42**.')
        result = apply_window(**self.common,offset=w['offset'],end_offset=w['end_offset'],content=replacement,expected_sha256=w['expected_sha256'])
        self.assertEqual(self.path.read_text(), original.replace('Fact 42.', 'Fact **42**.'))
        self.assertEqual(result['next_offset'],w['offset']+len(replacement))
        with self.assertRaises(VaultStoreError):
            apply_window(**self.common,offset=w['offset'],end_offset=w['end_offset'],content='stale',expected_sha256=w['expected_sha256'])
        self.assertIn('Fact **42**.',self.path.read_text())

    def test_refinement_window_is_read_only_and_rejects_invalid_range(self):
        from pipeline.atlas_fmt_refine import read_window, apply_window
        original=self.path.read_bytes();w=read_window(**self.common)
        self.assertEqual(self.path.read_bytes(),original)
        with self.assertRaises(VaultStoreError):
            apply_window(**self.common,offset=0,end_offset=10**6,content='wrong',expected_sha256=w['expected_sha256'])
        self.assertEqual(self.path.read_bytes(),original)

    def test_asset_preview_requires_parent_reference_and_no_symlink(self):
        from pipeline.atlas_fmt_refine import note_assets
        base=self.vault/'99-Assets/10-Attachments';base.mkdir(parents=True)
        (base/'known.png').write_bytes(b'synthetic image')
        (base/'unrelated.png').write_bytes(b'private unrelated')
        self.path.write_text(self.path.read_text()+'\n![[known.png]]\n')
        got=note_assets(**self.common,ws_root=self.root)
        self.assertEqual([a['file_name'] for a in got['assets']],['known.png'])
        with self.assertRaises(VaultStoreError):note_assets(**self.common,ws_root=self.root,file_name='unrelated.png')
        result=note_assets(**self.common,ws_root=self.root,file_name='known.png')
        self.assertEqual((self.root/result['preview_file']).read_bytes(),b'synthetic image')
        self.assertNotIn(str(self.vault),json.dumps(result))

    def test_import_idempotence_conflict_and_symlink(self):
        inputs=self.root/'inputs';inputs.mkdir();src=inputs/'sample.txt';src.write_text('one')
        kw=dict(ws_root=self.root,vault_dir=self.vault,vault_ref='vlt_01',input_file='inputs/sample.txt')
        self.assertTrue(fmt.import_attachment(**kw)['imported'])
        self.assertFalse(fmt.import_attachment(**kw)['imported'])
        src.write_text('two')
        with self.assertRaises(VaultStoreError):fmt.import_attachment(**kw)
        self.assertEqual((self.vault/'00-DropZone/sample.txt').read_text(),'one')
        src.unlink();src.symlink_to(self.path)
        with self.assertRaises(VaultStoreError):fmt.import_attachment(**kw)

    def test_format_parameters_and_packaged_dependencies(self):
        self.assertIn('--mode',fmt._format_arguments('pdf_converter.py',{'pdf_mode':'paper'}))
        self.assertEqual(fmt._format_arguments('note_refiner.py',{'encoding':'cp1252'}),['--encoding','cp1252'])
        manifest=json.loads(fmt.REQUIREMENTS_PATH.read_text())
        self.assertTrue(manifest['packages'])
        self.assertEqual(fmt.REQUIREMENTS_PATH.parent.name,'contracts')

    def test_four_channels_update_provided_keys_and_fingerprint(self):
        from facade.atlas_cli import _parse_channels, _fingerprint
        a=json.dumps({'action':'note-update','ref':'vlt_01','note_ref':'nte_01','summary':'one'})
        b=json.dumps({'action':'note-update','ref':'vlt_01','note_ref':'nte_01','summary':'two'})
        parsed=_parse_channels(['--json-stdin','-'],a)
        self.assertIn('summary',parsed['_provided_keys'])
        self.assertNotIn('default_mode',parsed['_provided_keys'])
        self.assertNotEqual(_fingerprint(['--json-stdin','-'],a),_fingerprint(['--json-stdin','-'],b))
