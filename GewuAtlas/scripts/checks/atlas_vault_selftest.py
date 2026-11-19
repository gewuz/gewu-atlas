#!/usr/bin/env python3
"""Isolated self-test for GewuAtlas KEK Zero-Path Vault CRUD and Data-Volume Guard."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from storage.vault_kek_store import (  # noqa: E402
    KekVaultRegistry,
    VaultStoreError,
    aead_chacha20_poly1305_decrypt,
    aead_chacha20_poly1305_encrypt,
    aead_xchacha20_poly1305_decrypt,
    aead_xchacha20_poly1305_encrypt,
    load_or_init_kek,
    validate_rel_vault_path,
)


def _assert_no_leak(stdout_text: str, forbidden: list[str]) -> None:
    for item in forbidden:
        if item and item in stdout_text:
            raise AssertionError(f"path/secret leaked to stdout: {item!r} in {stdout_text}")


def _test_rfc8439_vector() -> None:
    key = bytes.fromhex(
        "808182838485868788898a8b8c8d8e8f"
        "909192939495969798999a9b9c9d9e9f"
    )
    nonce = bytes.fromhex("070000004041424344454647")
    aad = bytes.fromhex("50515253c0c1c2c3c4c5c6c7")
    pt = b"Ladies and Gentlemen of the class of '99: If I could offer you only one tip for the future, sunscreen would be it."
    expected_ct = bytes.fromhex(
        "d31a8d34648e60db7b86afbc53ef7ec2"
        "a4aded51296e08fea9e2b5a736ee62d6"
        "3dbea45e8ca9671282fafb69da92728b"
        "1a71de0a9e060b2905d6a5b67ecd3b36"
        "92ddbd7f2d778b8c9803aee328091b58"
        "fab324e4fad675945585808b4831d7bc"
        "3ff4def08e4b7a9de576d26586cec64b"
        "6116"
    )
    expected_tag = bytes.fromhex("1ae10b594f09e26a7e902ecbd0600691")
    ct, tag = aead_chacha20_poly1305_encrypt(key, nonce, pt, aad)
    assert ct == expected_ct, "RFC 8439 ciphertext mismatch"
    assert tag == expected_tag, "RFC 8439 tag mismatch"
    dec = aead_chacha20_poly1305_decrypt(key, nonce, ct, aad, tag)
    assert dec == pt, "RFC 8439 decrypt mismatch"

    xnonce = os.urandom(24)
    xct, xtag = aead_xchacha20_poly1305_encrypt(key, xnonce, pt, aad)
    assert aead_xchacha20_poly1305_decrypt(key, xnonce, xct, aad, xtag) == pt
    try:
        aead_xchacha20_poly1305_decrypt(key, xnonce, xct, b"tampered-aad", xtag)
        raise AssertionError("tampered AAD must fail")
    except VaultStoreError:
        pass


def _test_kek_store_and_cli() -> None:
    for bad in ("/abs/path", "../escape", "Volumes/Other/v", "Volumes/GewuAtlas-data/bad/sub"):
        try:
            validate_rel_vault_path(bad)
            raise AssertionError(f"bad path accepted: {bad}")
        except VaultStoreError:
            pass

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        rt = tmp / "rt"
        ws = tmp / "ws"
        ws.mkdir()

        kek = load_or_init_kek(rt / "state" / "atlas-vaults.kek")
        reg = KekVaultRegistry("gewu-atlas", rt / "state" / "atlas-vaults.enc", kek)
        created = reg.create_vault(
            vault_id="alpha",
            rel_path="Volumes/GewuAtlas-data/alpha",
            title="Alpha Vault",
            obsidian_base=True,
            default_mode="auto",
        )
        assert created["ref"] == "vlt_01"
        enc_bytes = (rt / "state" / "atlas-vaults.enc").read_bytes()
        assert b"SQLite " + b"format 3" not in enc_bytes
        assert b"Volumes/GewuAtlas-data/alpha" not in enc_bytes

        foreign = KekVaultRegistry("foreign-app", rt / "state" / "atlas-vaults.enc", kek)
        try:
            foreign.list_vaults()
            raise AssertionError("cross-app AAD must fail")
        except VaultStoreError:
            pass

        env = dict(os.environ)
        env["GEWU_APP_RUNTIME_DIR"] = str(rt)
        env["GEWU_ATLAS_WORKSPACE_ROOT"] = str(ws)

        def run_cli(*argv: str, stdin_text: str | None = None) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, "-I", "-B", str(SCRIPTS_DIR / "facade/atlas_cli.py"), *argv],
                input=stdin_text,
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
            )

        p_tpl = run_cli("--template")
        assert p_tpl.returncode == 0 and "fill_template" in p_tpl.stdout
        _assert_no_leak(p_tpl.stdout, ["Volumes/", "GewuAtlas-data", ".trash", str(ws)])

        # Test opaque auto-generated vault slug (without passing --vault)
        p_auto = run_cli("--action", "vault-create", "--title", "盲化库", "--obsidian-base", "true")
        assert p_auto.returncode == 0, p_auto.stdout
        a_doc = json.loads(p_auto.stdout)
        assert a_doc["data"]["ref"] == "vlt_02"
        assert a_doc["data"]["path_sealed"] is True
        assert a_doc["data"]["new_conversation_recommended"] is True
        assert a_doc["structuredContent"]["capsule"]["terminal"] is True
        assert "新对话" in a_doc["structuredContent"]["capsule"]["state"]
        assert "rel_path" not in a_doc["data"] and "vault_id" not in a_doc["data"]
        _assert_no_leak(p_auto.stdout, ["Volumes/", "GewuAtlas-data", ".trash", str(ws)])

        p_c = run_cli("--action", "vault-create", "--vault", "beta", "--title", "Beta 库", "--obsidian-base", "true")
        assert p_c.returncode == 0, p_c.stdout
        c_doc = json.loads(p_c.stdout)
        assert c_doc["data"]["ref"] == "vlt_03"
        assert "rel_path" not in c_doc["data"] and "vault_id" not in c_doc["data"]
        _assert_no_leak(p_c.stdout, ["beta", "Volumes/", "GewuAtlas-data", ".trash", str(ws)])
        assert (ws / "Volumes/GewuAtlas-data/beta/10-Index/01-Atomics-常青原子卡.base").is_file()
        assert (ws / "Volumes/GewuAtlas-data/beta/20-AI-Zone/Manifest-AI-Zone.md").is_file()

        p_l = run_cli("--action", "vault-list")
        assert p_l.returncode == 0, p_l.stdout
        _assert_no_leak(p_l.stdout, ["alpha", "beta", "Volumes/", "GewuAtlas-data", ".trash", str(ws)])

        p_g = run_cli("--action", "vault-get", "--ref", "vlt_03")
        assert p_g.returncode == 0, p_g.stdout
        g_doc = json.loads(p_g.stdout)
        assert g_doc["data"]["disk"]["present_subdirs"] == g_doc["data"]["disk"]["expected_subdirs"]
        assert g_doc["data"]["disk"]["present_subdirs"] >= 18
        _assert_no_leak(p_g.stdout, ["beta", "Volumes/", "GewuAtlas-data", ".trash", str(ws)])

        p_u = run_cli("--action", "vault-update", "--ref", "vlt_03", "--new-vault", "beta-v2", "--title", "Beta V2")
        assert p_u.returncode == 0, p_u.stdout
        u_doc = json.loads(p_u.stdout)
        assert u_doc["data"]["key_version"] == 2
        _assert_no_leak(p_u.stdout, ["beta", "beta-v2", "Volumes/", "GewuAtlas-data", ".trash", str(ws)])
        assert (ws / "Volumes/GewuAtlas-data/beta-v2/20-AI-Zone/Manifest-AI-Zone.md").is_file()

        p_d = run_cli("--action", "vault-delete", "--ref", "vlt_03")
        assert p_d.returncode == 0, p_d.stdout
        d_doc = json.loads(p_d.stdout)
        assert d_doc["data"]["status"] == "deleted"
        assert "trash_rel_path" not in d_doc["data"]
        _assert_no_leak(p_d.stdout, ["beta", "beta-v2", "Volumes/", "GewuAtlas-data", ".trash", str(ws)])

        p_unk = run_cli("--action", "status", "--force")
        assert p_unk.returncode == 2 and "E_PARAM_UNKNOWN_FIELD" in p_unk.stdout

        p_b1 = run_cli("--action", "vault-get", "--ref", "vlt_99")
        assert p_b1.returncode == 1
        p_b2 = run_cli("--action", "vault-get", "--ref", "vlt_99")
        assert p_b2.returncode == 2 and "E_HALT_LOOP_DETECTED" in p_b2.stdout
        p_b3 = run_cli("--action", "vault-get", "--ref", "vlt_99")
        assert p_b3.returncode == 2 and "E_HALT_LOOP_DETECTED" in p_b3.stdout


def _test_volume_guard() -> None:
    guard = SCRIPTS_DIR / "storage/atlas_volume_guard.py"

    def run_guard(tool: str, payload: dict) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", "-B", str(guard), "--self-test-tool", tool, "--self-test-input", json.dumps(payload)],
            capture_output=True,
            text=True,
            timeout=10,
        )

    for tool, payload in (
        ("bash", {"command": "ls Volumes/GewuAtlas-data/alpha"}),
        ("bash", {"command": "find Volumes -maxdepth 3"}),
        ("bash", {"command": "cat Volumes/GewuAtlas-data/alpha/10-Index/README.md"}),
        ("read_file", {"path": "Volumes/GewuAtlas-data/alpha/10-Index/README.md"}),
        ("bash", {"command": "cat GewuAtlas/.local/state/atlas-vaults.enc"}),
        ("bash", {"command": "git show HEAD:GewuAtlas/scripts/app_core/vault_store.py"}),
    ):
        res = run_guard(tool, payload)
        assert res.returncode == 2 and '"block"' in res.stdout, f"expected block for {tool} {payload}, got {res.stdout}"

    for tool, payload in (
        ("bash", {"command": "./gewu atlas --action vault-list"}),
        ("bash", {"command": "git status"}),
    ):
        res = run_guard(tool, payload)
        assert res.returncode == 0 and '"allow"' in res.stdout, f"expected allow for {tool} {payload}, got {res.stdout}"


def main() -> int:
    _test_rfc8439_vector()
    _test_kek_store_and_cli()
    _test_volume_guard()
    print(json.dumps({"ok": True, "selftest": "gewu-atlas-vault-kek/1", "plan_a_zero_path_and_guard": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
