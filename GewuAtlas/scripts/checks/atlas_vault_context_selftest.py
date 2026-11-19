"""Vault routing isolation and gateway regressions using synthetic independent data."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from storage.atlas_vault_context import VaultContext, session_id
from storage.vault_kek_store import KekVaultRegistry, VaultStoreError


class VaultContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.kek = os.urandom(32)
        self.reg = KekVaultRegistry("test-context", self.root / "registry.enc", self.kek)
        for name in ("alpha", "beta"):
            self.reg.create_vault(vault_id=name, rel_path="Volumes/GewuAtlas-data/" + name,
                                  title=name, obsidian_base=False, default_mode="auto")
        self.ctx = VaultContext(self.root, "test-context", self.kek, self.reg)

    def test_default_new_sessions_and_one_off(self):
        self.ctx.command("vault-default-set", None, "vlt_01")
        self.assertEqual(self.ctx.resolve("a")["ref"], "vlt_01")
        self.assertEqual(self.ctx.resolve("a", "vlt_02")["selection_source"], "explicit")
        self.ctx.command("vault-default-set", None, "vlt_02")
        self.assertEqual(self.ctx.resolve("a")["ref"], "vlt_01")
        self.assertEqual(self.ctx.resolve("b")["ref"], "vlt_02")
        self.assertEqual(self.ctx.command("vault-reset", "a")["ref"], "vlt_02")

    def test_session_switch_and_restart(self):
        self.ctx.command("vault-use", "a", "vlt_01")
        self.ctx.command("vault-use", "b", "vlt_02")
        other = VaultContext(self.root, "test-context", self.kek, self.reg)
        self.assertEqual(other.resolve("a")["ref"], "vlt_01")
        self.assertEqual(other.resolve("b")["ref"], "vlt_02")

    def test_rename_and_deleted_do_not_fallback(self):
        self.ctx.command("vault-use", "a", "vlt_01")
        self.reg.update_vault(ref="vlt_01", new_vault_id="renamed", title="new")
        self.assertEqual(self.ctx.resolve("a")["ref"], "vlt_01")
        self.reg.delete_vault(ref="vlt_01")
        self.ctx.command("vault-default-set", None, "vlt_02")
        with self.assertRaises(VaultStoreError) as caught:
            self.ctx.resolve("a")
        self.assertEqual(caught.exception.code, "E_VAULT_CONTEXT_UNAVAILABLE")
        self.assertEqual(self.ctx.resolve("a", "vlt_02")["ref"], "vlt_02")
        with self.assertRaises(VaultStoreError):
            self.ctx.resolve("a")

    def test_unbound_session_is_not_rebound_by_default(self):
        self.assertIsNone(self.ctx.command("vault-context", "a")["ref"])
        self.ctx.command("vault-default-set", None, "vlt_01")
        self.assertIsNone(self.ctx.command("vault-context", "a")["ref"])
        self.assertEqual(self.ctx.command("vault-context", "b")["ref"], "vlt_01")

    def test_missing_session_and_legacy(self):
        self.assertEqual(self.ctx.resolve(None, allow_legacy_federated=True)["selection_source"], "legacy_federated")
        self.ctx.command("vault-default-set", None, "vlt_01")
        with self.assertRaises(VaultStoreError) as caught:
            self.ctx.resolve(None, allow_legacy_federated=True)
        self.assertEqual(caught.exception.code, "E_VAULT_SESSION_REQUIRED")
        self.assertEqual(self.ctx.resolve(None, "vlt_02")["ref"], "vlt_02")

    def test_atomic_default_and_switch(self):
        self.ctx.command("vault-default-set", "a", "vlt_01", switch=True)
        with self.assertRaises(VaultStoreError):
            self.ctx.command("vault-default-set", None, "vlt_02", switch=True)
        self.assertEqual(self.ctx.command("vault-default-get", None)["default_ref"], "vlt_01")
        self.assertEqual(self.ctx.resolve("a")["ref"], "vlt_01")

    def test_concurrent_sessions_no_lost_updates(self):
        def select(i):
            ctx = VaultContext(self.root, "test-context", self.kek, self.reg)
            return ctx.command("vault-use", "thread-" + str(i), "vlt_01" if i % 2 else "vlt_02")
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(select, range(24)))
        for i in range(24):
            self.assertEqual(self.ctx.resolve("thread-" + str(i))["ref"], "vlt_01" if i % 2 else "vlt_02")

    def test_ciphertext_tamper_no_reset_and_workspace_isolation(self):
        self.ctx.command("vault-use", "private-session", "vlt_01")
        raw = self.ctx.path.read_bytes()
        self.assertNotIn(b"private-session", raw)
        self.assertNotIn(b"vlt_01", raw)
        if sys.platform != "win32":
            self.assertEqual(self.ctx.path.stat().st_mode & 0o777, 0o600)
        other = VaultContext(self.root / "other", "test-context", self.kek, self.reg)
        self.assertIsNone(other.command("vault-context", "private-session")["ref"])
        self.ctx.path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
        with self.assertRaises(VaultStoreError) as caught:
            self.ctx.resolve("private-session")
        self.assertEqual(caught.exception.code, "E_HALT_SECURITY")

    def test_parallel_default_updates_preserve_existing_sessions(self):
        self.ctx.command("vault-use", "existing", "vlt_01")
        def change(i):
            ctx = VaultContext(self.root, "test-context", self.kek, self.reg)
            ctx.command("vault-default-set", None, "vlt_01" if i % 2 else "vlt_02")
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(change, range(12)))
        self.assertEqual(self.ctx.resolve("existing")["ref"], "vlt_01")
        self.assertIn(self.ctx.resolve("new")["ref"], ("vlt_01", "vlt_02"))

    def test_frozen_explicit_target_ignores_later_context(self):
        from facade.atlas_cli import _parse_channels
        self.ctx.command("vault-use", "a", "vlt_01")
        frozen = json.dumps({"action": "journal-append", "ref": self.ctx.resolve("a")["ref"], "content": "fixed"})
        self.ctx.command("vault-use", "a", "vlt_02")
        self.ctx.command("vault-default-set", None, "vlt_02")
        parsed = _parse_channels(["--json-stdin", "-"], frozen)
        self.assertEqual(self.ctx.resolve("a", parsed["ref"])["ref"], "vlt_01")

    def test_parameter_channels_and_conflicting_options(self):
        from facade.atlas_cli import _parse_channels, _fingerprint
        parsed = _parse_channels(["--payload", '{"session":"payload","all_vaults":true}',
                                  "--set", "session=set", "--session", "explicit", "--all-vaults", "false"], None)
        self.assertEqual(parsed["session"], "explicit")
        self.assertFalse(parsed["all_vaults"])
        self.assertNotEqual(_fingerprint(["--session", "a", "--action", "note-list"]),
                            _fingerprint(["--session", "b", "--action", "note-list"]))
        self.assertIsInstance(_fingerprint(["--session", "../invalid"]), str)

    def test_missing_volume_never_recreates_or_falls_back(self):
        self.ctx.command("vault-use", "a", "vlt_01")
        ctx = VaultContext(self.root, "test-context", self.kek, self.reg, available=lambda row: False)
        with self.assertRaises(VaultStoreError) as caught:
            ctx.resolve("a")
        self.assertEqual(caught.exception.code, "E_VAULT_CONTEXT_UNAVAILABLE")

    def test_host_precedence(self):
        env = {"GEWU_ATLAS_SESSION_ID": "adapter", "CODEX_THREAD_ID": "codex"}
        self.assertEqual(session_id({"session": "explicit"}, env), "explicit")
        self.assertEqual(session_id({}, env), "adapter")
        self.assertEqual(session_id({}, {"CODEX_THREAD_ID": "codex"}), "codex")
        with self.assertRaises(VaultStoreError):
            session_id({"session": "../bad"}, {})


class VaultContextGatewayTests(unittest.TestCase):
    def test_host_auto_binding_and_negative_guards(self):
        with tempfile.TemporaryDirectory() as td:
            env = dict(os.environ)
            env.pop("GEWU_ATLAS_SESSION_ID", None)
            env.update(CODEX_THREAD_ID="host-a", GEWU_APP_RUNTIME_DIR=td + "/rt",
                       GEWU_ATLAS_WORKSPACE_ROOT=td + "/ws")
            Path(td + "/ws").mkdir()
            gateway = SCRIPTS.parents[1] / "gewu"
            def call(action, expected=None, **kw):
                proc = subprocess.run([sys.executable, "-I", "-B", str(gateway), "atlas", "--payload",
                                       json.dumps(dict(action=action, **kw))], capture_output=True, text=True,
                                      cwd=gateway.parent, env=env, timeout=30)
                result = json.loads(proc.stdout)
                if expected:
                    self.assertEqual(result["code"], expected, result)
                    self.assertNotEqual(proc.returncode, 0)
                else:
                    self.assertEqual(proc.returncode, 0, result)
                return result
            a = call("vault-create", title="Host A")["data"]["ref"]
            b = call("vault-create", title="Host B")["data"]["ref"]
            call("vault-default-set", ref=a)
            self.assertEqual(call("vault-context")["data"]["ref"], a)
            call("vault-default-set", ref=b)
            self.assertEqual(call("vault-context")["data"]["ref"], a)
            env["CODEX_THREAD_ID"] = "host-b"
            self.assertEqual(call("vault-context")["data"]["ref"], b)
            call("note-search", "E_PARAM_INVALID", ref=a, all_vaults=True, query="x")
            call("journal-append", "E_PARAM_INVALID", all_vaults=True, content="must not write")
            call("vault-context", "E_PARAM_INVALID", ref=a)
            call("vault-delete", ref=b)
            blocked = call("journal-append", "E_VAULT_CONTEXT_UNAVAILABLE", content="must not redirect")
            self.assertEqual(blocked["structuredContent"]["capsule"]["mode"], "INPUT_REQUIRED")
            env["CODEX_THREAD_ID"] = "host-unbound"
            call("vault-default-set", ref=a)
            call("vault-use", ref=a)
            self.assertEqual(call("vault-context")["data"]["ref"], a)

    def test_real_gateway_routing_and_content_isolation(self):
        with tempfile.TemporaryDirectory() as td:
            env = {k: v for k, v in os.environ.items() if k not in
                   {"CODEX_THREAD_ID", "GEWU_ATLAS_SESSION_ID", "GEWU_APP_RUNTIME_DIR", "GEWU_ATLAS_WORKSPACE_ROOT"}}
            env["GEWU_APP_RUNTIME_DIR"] = td + "/runtime"
            env["GEWU_ATLAS_WORKSPACE_ROOT"] = td + "/workspace"
            Path(env["GEWU_ATLAS_WORKSPACE_ROOT"]).mkdir()
            gateway = SCRIPTS.parents[1] / "gewu"
            def call(action, **kw):
                proc = subprocess.run([sys.executable, "-I", "-B", str(gateway), "atlas", "--json-stdin", "-"],
                                      input=json.dumps(dict(action=action, **kw)), text=True, capture_output=True,
                                      cwd=gateway.parent, env=env, timeout=30)
                result = json.loads(proc.stdout)
                self.assertEqual(proc.returncode, 0, result)
                self.assertTrue(result["ok"], result)
                return result["data"]
            a = call("vault-create", title="Context A")["ref"]
            b = call("vault-create", title="Context B")["ref"]
            call("vault-default-set", ref=a)
            self.assertEqual(call("vault-context", session="a")["ref"], a)
            call("vault-use", session="b", ref=b)
            call("journal-append", session="a", content="alphaUniqueContext")
            call("journal-append", session="b", content="betaUniqueContext")
            aa = call("journal-today", session="a")
            bb = call("journal-today", session="b")
            self.assertIn("alphaUniqueContext", aa["ai_section"])
            self.assertNotIn("betaUniqueContext", aa["ai_section"])
            self.assertIn("betaUniqueContext", bb["ai_section"])
            self.assertNotIn("alphaUniqueContext", bb["ai_section"])
            call("vault-default-set", ref=b)
            self.assertEqual(call("vault-context", session="a")["ref"], a)
            self.assertEqual(call("vault-context", session="new")["ref"], b)
            call("journal-append", session="a", ref=b, content="oneOffContext")
            self.assertEqual(call("vault-context", session="a")["ref"], a)
            local = call("note-search", session="a", query="betaUniqueContext", private=True)
            self.assertEqual(local["count"], 0)
            federation = call("note-search", session="a", query="betaUniqueContext", all_vaults=True, private=True)
            self.assertGreater(federation["count"], 0)
            self.assertEqual({hit["vault_ref"] for hit in federation["hits"]}, {b})
            self.assertEqual(federation["vault_selection"]["selection_source"], "explicit_federated")
            self.assertEqual(call("vault-reset", session="a")["ref"], b)


if __name__ == "__main__":
    unittest.main()
