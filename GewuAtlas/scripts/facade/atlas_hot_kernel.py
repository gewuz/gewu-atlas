#!/usr/bin/env python3
"""In-process hot-memory kernel for GewuAtlas (shared by AtlasHotDaemon & Atlas MCP Server).

Security & Consistency Invariants:
- KEK material and decrypted handle/index caches live ONLY in process RAM; zero plaintext on disk.
- Disk `.enc` registries & `.md` vault files remain the Single Source of Truth (SSOT):
  - Registry instances reload automatically if `.kek` or `.enc` file `(mtime_ns, size)` changes.
  - Note index & search caches validate `(mtime_ns, size)` and run `scandir_vault_markdowns` stat-diff
    so external human edits in Obsidian are reflected immediately.
- Strict environment whitelist: never accepts `PYTHONPATH` / `DYLD_*` / `LD_*` from clients.
- Calling `close()` clears all in-memory KEK / registry / index cache references immediately.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
APP_DIR = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

SAFE_CLIENT_ENV_KEYS = frozenset(
    {
        "GEWU_SESSION_ID",
        "GEWU_ATLAS_SESSION_ID",
        "CODEX_THREAD_ID",
        "GEWU_ATLAS_WORKSPACE_ROOT",
        "GEWU_APP_RUNTIME_DIR",
        "GEWU_APP_KEK",
        "GEWU_TICKET_VOLUME",
    }
)


def _stat_sig(path: Path) -> Tuple[int, int]:
    try:
        st = path.stat()
        return int(st.st_mtime_ns), int(st.st_size)
    except OSError:
        return -1, -1


class AtlasHotKernel:
    """Thread-safe hot execution engine keeping KEK registries and index caches warm in RAM."""

    def __init__(self, *, max_cached_vaults: int = 8) -> None:
        self._lock = threading.Lock()
        self._max_cached_vaults = max(1, int(max_cached_vaults))
        self._started_at = time.monotonic()
        self._last_activity = self._started_at
        self._requests_served = 0
        self._hot_vaults: set[str] = set()
        # Keyed by (str(state_dir), app_id) -> dict(kek_sig, kek, vault_reg, note_reg, proj_reg)
        self._registry_pool: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._closed = False
        self._enable_hot_caches()

    def _enable_hot_caches(self) -> None:
        from notes import atlas_search_engine
        from storage import note_index_cache

        note_index_cache.set_hot_cache_enabled(True, max_entries=self._max_cached_vaults)
        atlas_search_engine.set_hot_cache_enabled(True, max_entries=self._max_cached_vaults)

    def _get_or_warm_registries(self, state_dir: Path, app_id: str) -> Dict[str, Any]:
        from storage.note_kek_store import KekNoteRegistry
        from storage.project_kek_store import KekProjectRegistry
        from storage.vault_kek_store import KekVaultRegistry, load_or_init_kek

        pool_key = (str(state_dir.resolve()), app_id)
        kek_path = state_dir / "atlas-vaults.kek"
        env_kek = os.environ.get("GEWU_APP_KEK", "").strip()
        kek_sig = ("env", env_kek) if env_kek else ("file", _stat_sig(kek_path))
        vault_enc_sig = _stat_sig(state_dir / "atlas-vaults.enc")
        proj_enc_sig = _stat_sig(state_dir / "atlas-projects.enc")

        slot = self._registry_pool.get(pool_key)
        if slot is None or slot.get("kek_sig") != kek_sig:
            kek = load_or_init_kek(kek_path)
            slot = {
                "kek_sig": kek_sig,
                "kek": kek,
                "vault_enc_sig": vault_enc_sig,
                "proj_enc_sig": proj_enc_sig,
                "vault_reg": KekVaultRegistry(app_id, state_dir / "atlas-vaults.enc", kek),
                "note_reg": KekNoteRegistry(app_id, state_dir / "atlas-notes.enc", kek),
                "proj_reg": KekProjectRegistry(app_id, state_dir / "atlas-projects.enc", kek),
            }
            if len(self._registry_pool) >= 4:
                oldest = next(iter(self._registry_pool))
                self._registry_pool.pop(oldest, None)
            self._registry_pool[pool_key] = slot
            return slot

        # Reload vault/project registry wrappers if an external CLI process mutated their .enc files.
        # (KekNoteRegistry already performs built-in (mtime_ns, size) invalidation on self._mem_cache.)
        kek = slot["kek"]
        if slot.get("vault_enc_sig") != vault_enc_sig:
            slot["vault_reg"] = KekVaultRegistry(app_id, state_dir / "atlas-vaults.enc", kek)
            slot["vault_enc_sig"] = vault_enc_sig
        if slot.get("proj_enc_sig") != proj_enc_sig:
            slot["proj_reg"] = KekProjectRegistry(app_id, state_dir / "atlas-projects.enc", kek)
            slot["proj_enc_sig"] = proj_enc_sig
        return slot

    def _apply_safe_env(self, session_env: Optional[Dict[str, str]]) -> Dict[str, Optional[str]]:
        saved: Dict[str, Optional[str]] = {}
        if session_env is None:
            return saved
        for k in SAFE_CLIENT_ENV_KEYS:
            saved[k] = os.environ.get(k)
            v = session_env.get(k)
            if v is None or v == "":
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        return saved

    def _restore_env(self, saved: Dict[str, Optional[str]]) -> None:
        for k, old_v in saved.items():
            if old_v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old_v

    def execute_cli(
        self,
        raw_argv: List[str],
        *,
        stdin_text: Optional[str] = None,
        session_env: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, str]:
        """Execute a GewuAtlas CLI request in-process against warm KEK & index caches."""
        from facade import atlas_cli

        with self._lock:
            if self._closed:
                raise RuntimeError("AtlasHotKernel is closed")
            self._last_activity = time.monotonic()
            saved = self._apply_safe_env(session_env)
            try:
                state_dir, _ = atlas_cli._runtime_dirs()
                app_id = atlas_cli._app_id()
                hot_slot = self._get_or_warm_registries(state_dir, app_id)
                code, stdout_line = atlas_cli.run_cli_request(
                    list(raw_argv),
                    stdin_text=stdin_text,
                    hot_registries=hot_slot,
                )
                # Refresh enc stat signatures after our own write so next hit stays warm
                hot_slot["vault_enc_sig"] = _stat_sig(state_dir / "atlas-vaults.enc")
                hot_slot["proj_enc_sig"] = _stat_sig(state_dir / "atlas-projects.enc")
                self._requests_served += 1
                self._last_activity = time.monotonic()
                # Track touched vault handle opportunistically without parsing large payloads
                for i, tok in enumerate(raw_argv):
                    if tok in ("--ref", "-r") and i + 1 < len(raw_argv):
                        v = str(raw_argv[i + 1]).strip()
                        if v.startswith("vlt_"):
                            self._hot_vaults.add(v)
                return code, stdout_line
            finally:
                self._restore_env(saved)

    def preload_vault(
        self,
        vault_ref: str,
        *,
        session_env: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Pre-warm KEK registries and note index cache for a given opaque vault handle."""
        from facade import atlas_cli
        from storage.note_index_cache import build_backlink_map, reconcile_note_index
        from storage.vault_volume import resolve_safe_vault_dir

        with self._lock:
            if self._closed:
                return {"preloaded": False, "reason": "closed"}
            saved = self._apply_safe_env(session_env)
            try:
                state_dir, _ = atlas_cli._runtime_dirs()
                ws_root = atlas_cli._workspace_root()
                app_id = atlas_cli._app_id()
                hot_slot = self._get_or_warm_registries(state_dir, app_id)
                row = hot_slot["vault_reg"].get_vault(ref=vault_ref)
                if row.get("deleted"):
                    return {"preloaded": False, "vault_ref": vault_ref, "reason": "deleted"}
                vdir = resolve_safe_vault_dir(ws_root, str(row["rel_path"]))
                entries, mapping = reconcile_note_index(
                    vault_dir=vdir,
                    vault_ref=vault_ref,
                    note_reg=hot_slot["note_reg"],
                    kek=hot_slot["kek"],
                    app_id=app_id,
                    cache_dir=state_dir / "notes_index",
                )
                build_backlink_map(entries=entries, handle_map=mapping)
                self._hot_vaults.add(vault_ref)
                self._last_activity = time.monotonic()
                return {"preloaded": True, "vault_ref": vault_ref, "note_count": len(entries)}
            finally:
                self._restore_env(saved)

    def stats(self, *, idle_timeout: float = 0.0) -> Dict[str, Any]:
        from notes import atlas_search_engine
        from storage import note_index_cache

        now = time.monotonic()
        idx_stats = note_index_cache.get_hot_cache_stats()
        srch_stats = atlas_search_engine.get_hot_cache_stats()
        total_hits = int(idx_stats["hits"]) + int(srch_stats["hits"])
        total_misses = int(idx_stats["misses"]) + int(srch_stats["misses"])
        denom = total_hits + total_misses
        idle_elapsed = max(0.0, now - self._last_activity)
        idle_rem = max(0.0, float(idle_timeout) - idle_elapsed) if idle_timeout > 0 else None
        return {
            "uptime_seconds": round(max(0.0, now - self._started_at), 2),
            "idle_seconds": round(idle_elapsed, 2),
            "idle_timeout_seconds": int(idle_timeout),
            "idle_remaining_seconds": round(idle_rem, 2) if idle_rem is not None else None,
            "requests_served": self._requests_served,
            "hot_vaults": sorted(self._hot_vaults),
            "index_cache": idx_stats,
            "search_cache": srch_stats,
            "cache_hit_ratio": round(total_hits / denom, 4) if denom > 0 else None,
        }

    def close(self) -> None:
        from notes import atlas_search_engine
        from storage import note_index_cache

        with self._lock:
            self._closed = True
            self._registry_pool.clear()
            self._hot_vaults.clear()
            note_index_cache.set_hot_cache_enabled(False)
            atlas_search_engine.set_hot_cache_enabled(False)
