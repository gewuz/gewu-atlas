#!/usr/bin/env python3
"""Contract-Rail compliant, Zero-Path Opaque-Handle CLI facade delegate for GewuAtlas."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if sys.platform != "win32":
    import fcntl
else:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
APP_DIR = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

_EARLY_STDIN_LOADED = False
_EARLY_STDIN_CACHE: Optional[str] = None

if __name__ == "__main__":
    # Ultra-light fast-path: forward straight to live AtlasHotDaemon BEFORE importing heavy modules.
    _raw_early = sys.argv[1:]
    if _raw_early and not any(t in ("--template", "--describe", "-h", "--help") for t in _raw_early):
        try:
            from facade.atlas_daemon import try_forward_to_daemon as _try_daemon_fwd

            def _early_stdin_loader() -> Optional[str]:
                global _EARLY_STDIN_LOADED, _EARLY_STDIN_CACHE
                if _EARLY_STDIN_LOADED:
                    return _EARLY_STDIN_CACHE
                _EARLY_STDIN_LOADED = True
                needs = any(
                    tok == "-" or (tok.startswith("--") and tok.endswith("=-")) or tok in ("--json-stdin", "--json_stdin")
                    for tok in _raw_early
                )
                _EARLY_STDIN_CACHE = sys.stdin.read() if (needs and not sys.stdin.isatty()) else None
                return _EARLY_STDIN_CACHE

            _fwd_res = _try_daemon_fwd(_raw_early, stdin_loader=_early_stdin_loader)
            if _fwd_res is not None:
                _fwd_code, _fwd_out = _fwd_res
                sys.stdout.write(_fwd_out if _fwd_out.endswith("\n") else (_fwd_out + "\n"))
                sys.exit(_fwd_code)
        except Exception:
            pass

def _lazy(mod_name: str, attr_name: str):
    """Lazy-import proxy so read-only CLI actions skip heavy unused engine modules."""
    _cached = None

    def _proxy(*args: Any, **kwargs: Any) -> Any:
        nonlocal _cached
        if _cached is None:
            import importlib

            _cached = getattr(importlib.import_module(mod_name), attr_name)
        return _cached(*args, **kwargs)

    return _proxy


engine_fix_vault_links = _lazy("pipeline.atlas_graph_engine", "fix_vault_links")
engine_graph_report = _lazy("pipeline.atlas_graph_engine", "graph_report")
engine_journal_append = _lazy("pipeline.atlas_graph_engine", "journal_append")
engine_journal_archive = _lazy("pipeline.atlas_graph_engine", "journal_archive")
engine_journal_show = _lazy("pipeline.atlas_graph_engine", "journal_show")
engine_journal_today = _lazy("pipeline.atlas_graph_engine", "journal_today")
engine_moc_sync = _lazy("pipeline.atlas_graph_engine", "moc_sync")
engine_moc_roam_discover = _lazy("pipeline.atlas_graph_engine", "moc_roam_discover")
engine_note_link = _lazy("pipeline.atlas_graph_engine", "note_link")

engine_backfill_provenance = _lazy("pipeline.atlas_extractor_engine", "backfill_provenance")
engine_extract_discovery = _lazy("pipeline.atlas_extractor_engine", "extract_discovery")
engine_mark_source_done = _lazy("pipeline.atlas_extractor_engine", "mark_source_done")
engine_note_approve = _lazy("pipeline.atlas_extractor_engine", "note_approve")
engine_note_extract = _lazy("pipeline.atlas_extractor_engine", "note_extract")
engine_repair_toc_ledger = _lazy("pipeline.atlas_extractor_engine", "repair_toc_ledger")

engine_run_fmt = _lazy("pipeline.atlas_fmt_engine", "run_fmt")
engine_inbox_list = _lazy("pipeline.atlas_fmt_engine", "inbox_list")
engine_run_url_ingest = _lazy("pipeline.atlas_fmt_engine", "run_url_ingest")
engine_setup_fmt = _lazy("pipeline.atlas_fmt_engine", "setup_fmt_environment")

KekTrashRegistry = _lazy("pipeline.atlas_vfs_engine", "KekTrashRegistry")
engine_vfs_copy = _lazy("pipeline.atlas_vfs_engine", "vfs_copy")
engine_vfs_create = _lazy("pipeline.atlas_vfs_engine", "vfs_create")
engine_vfs_links = _lazy("pipeline.atlas_vfs_engine", "vfs_links")
engine_vfs_move = _lazy("pipeline.atlas_vfs_engine", "vfs_move")
engine_vfs_trash = _lazy("pipeline.atlas_vfs_engine", "vfs_trash")
engine_vfs_trash_list = _lazy("pipeline.atlas_vfs_engine", "vfs_trash_list")
engine_vfs_untrash = _lazy("pipeline.atlas_vfs_engine", "vfs_untrash")
engine_vfs_validate = _lazy("pipeline.atlas_vfs_engine", "vfs_validate")

engine_note_format = _lazy("notes.atlas_md_normalizer", "note_format")

engine_file_read = _lazy("notes.atlas_note_engine", "file_read")
engine_health_check = _lazy("notes.atlas_note_engine", "health_check")
engine_note_create = _lazy("notes.atlas_note_engine", "note_create")
engine_note_deprecate = _lazy("notes.atlas_note_engine", "note_deprecate")
engine_note_get = _lazy("notes.atlas_note_engine", "note_get")
engine_note_get_batch = _lazy("notes.atlas_note_engine", "note_get_batch")
engine_note_list = _lazy("notes.atlas_note_engine", "note_list")
engine_note_promote = _lazy("notes.atlas_note_engine", "note_promote")
engine_note_slice = _lazy("notes.atlas_note_engine", "note_slice")
engine_note_update = _lazy("notes.atlas_note_engine", "note_update")
engine_note_update_batch = _lazy("notes.atlas_note_engine", "note_update_batch")
engine_note_patch = _lazy("notes.atlas_note_engine", "note_patch")
engine_note_patch_batch = _lazy("notes.atlas_note_engine", "note_patch_batch")
engine_note_grep = _lazy("notes.atlas_note_engine", "note_grep")
engine_project_brief = _lazy("notes.atlas_note_engine", "project_brief")
engine_schema_check = _lazy("notes.atlas_note_engine", "schema_check")
engine_schema_list = _lazy("notes.atlas_note_engine", "schema_list")

engine_note_search = _lazy("notes.atlas_search_engine", "note_search")
engine_note_search_all_vaults = _lazy("notes.atlas_search_engine", "note_search_all_vaults")

resolve_domain_scope = _lazy("facade.atlas_domain_scope", "resolve_domain_scope")

engine_slip_commit = _lazy("collab.atlas_slip_engine", "slip_commit")
engine_slip_delete = _lazy("collab.atlas_slip_engine", "slip_delete")
engine_slip_get = _lazy("collab.atlas_slip_engine", "slip_get")
engine_slip_list = _lazy("collab.atlas_slip_engine", "slip_list")
engine_slip_scan = _lazy("collab.atlas_slip_engine", "slip_scan")
engine_slip_update = _lazy("collab.atlas_slip_engine", "slip_update")

engine_archive_list = _lazy("pipeline.atlas_workflow_engine", "archive_list")
engine_archive_pack = _lazy("pipeline.atlas_workflow_engine", "archive_pack")
engine_artifact_adopt = _lazy("pipeline.atlas_workflow_engine", "artifact_adopt")
engine_artifact_finalize = _lazy("pipeline.atlas_workflow_engine", "artifact_finalize")
engine_clean_attachments = _lazy("pipeline.atlas_workflow_engine", "clean_attachments")
engine_note_route = _lazy("pipeline.atlas_workflow_engine", "note_route")

DOC_COMMIT_THRESHOLD = 20
KekChangeRegistry = _lazy("notes.atlas_change_engine", "KekChangeRegistry")
_collect_vault_changes = _lazy("notes.atlas_change_engine", "_collect_vault_changes")
_collect_targeted_changes = _lazy("notes.atlas_change_engine", "_collect_targeted_changes")
capture_vault_snapshot = _lazy("notes.atlas_change_engine", "capture_vault_snapshot")
capture_targeted_snapshot = _lazy("notes.atlas_change_engine", "capture_targeted_snapshot")
configure_vault_doc_remote = _lazy("notes.atlas_change_engine", "configure_vault_doc_remote")
doc_git_status_summary = _lazy("notes.atlas_change_engine", "doc_git_status_summary")
estimate_affected_files = _lazy("notes.atlas_change_engine", "estimate_affected_files")
get_vault_doc_remote = _lazy("notes.atlas_change_engine", "get_vault_doc_remote")
init_vault_git_repo = _lazy("notes.atlas_change_engine", "init_vault_git_repo")
inspect_vault_doc_git_state = _lazy("notes.atlas_change_engine", "inspect_vault_doc_git_state")
engine_retention_sweep = _lazy("notes.atlas_change_engine", "retention_sweep")
run_vault_doc_commit = _lazy("notes.atlas_change_engine", "run_vault_doc_commit")
sync_vault_doc_remote = _lazy("notes.atlas_change_engine", "sync_vault_doc_remote")
engine_trash_purge = _lazy("notes.atlas_change_engine", "trash_purge")

engine_apply_workbench_preset = _lazy("pipeline.atlas_project_engine", "apply_workbench_preset")
engine_list_workbench_presets = _lazy("pipeline.atlas_project_engine", "list_workbench_presets")
engine_project_archive = _lazy("pipeline.atlas_project_engine", "project_archive")
engine_project_create = _lazy("pipeline.atlas_project_engine", "project_create")
engine_project_get = _lazy("pipeline.atlas_project_engine", "project_get")
engine_project_history = _lazy("pipeline.atlas_project_engine", "project_history")
engine_project_list = _lazy("pipeline.atlas_project_engine", "project_list")
engine_project_merge = _lazy("pipeline.atlas_project_engine", "project_merge")
engine_project_publish = _lazy("pipeline.atlas_project_engine", "project_publish")
engine_project_rename = _lazy("pipeline.atlas_project_engine", "project_rename")
engine_project_reopen = _lazy("pipeline.atlas_project_engine", "project_reopen")
engine_project_restore_links = _lazy("pipeline.atlas_project_engine", "project_restore_links")
engine_project_split = _lazy("pipeline.atlas_project_engine", "project_split")
engine_project_trash = _lazy("pipeline.atlas_project_engine", "project_trash")
engine_project_undo = _lazy("pipeline.atlas_project_engine", "project_undo")
engine_project_unpublish = _lazy("pipeline.atlas_project_engine", "project_unpublish")
engine_project_untrash = _lazy("pipeline.atlas_project_engine", "project_untrash")
engine_workbench_workspace = _lazy("pipeline.atlas_project_engine", "workbench_workspace")
sync_and_check_project_manifest = _lazy("pipeline.atlas_project_engine", "sync_and_check_project_manifest")

create_atlas_ticket = _lazy("collab.ticket_bridge", "create_atlas_ticket")
ensure_doc_ticket = _lazy("collab.ticket_bridge", "ensure_doc_ticket")
ensure_slip_ticket = _lazy("collab.ticket_bridge", "ensure_slip_ticket")
list_atlas_tickets = _lazy("collab.ticket_bridge", "list_atlas_tickets")
update_doc_ticket_progress = _lazy("collab.ticket_bridge", "update_doc_ticket_progress")

from storage.note_kek_store import KekNoteRegistry  # noqa: E402
from storage.note_index_cache import reconcile_note_index  # noqa: E402
from storage.project_kek_store import KekProjectRegistry  # noqa: E402
from storage.vault_kek_store import (  # noqa: E402
    HANDLE_RE,
    KekVaultRegistry,
    VaultStoreError,
    load_or_init_kek,
)
from storage.vault_volume import (  # noqa: E402
    adopt_vault_tree,
    discover_unregistered_isomorphic_vaults,
    get_vault_subdirs,
    init_vault_tree,
    inspect_vault_tree,
    make_vault_rel_path,
    normalize_vault_id,
    purge_vault_tree,
    rename_vault_tree,
    resolve_safe_vault_dir,
    restore_vault_tree,
    sync_obsidian_bases,
    sync_obsidian_bases_detailed,
    sync_obsidian_templates,
    sync_obsidian_templates_detailed,
    sync_vault_layout_dirs,
    trash_vault_tree,
)

from storage.atlas_vault_context import VaultContext, CONTEXT_ACTIONS, session_id

ACTIONS = (
    "vault",
    "ingest",
    "trash",
    "journal",
    "slip",
    "guard",
    "vault-context", "vault-use", "vault-reset", "vault-default-get", "vault-default-set",
    "status",
    "vault-create",
    "vault-import",
    "vault-list",
    "vault-get",
    "vault-update",
    "vault-delete",
    "vault-restore",
    "index-sync",
    "note-create",
    "inbox-list",
    "note-list",
    "note-get",
    "file-read",
    "note-update",
    "note-patch",
    "note-grep",
    "project-brief",
    "batch",
    "note-promote",
    "note-slice",
    "note-route",
    "atomic-extract",
    "atomic-approve",
    "note-format",
    "note-deprecate",
    "note-search",
    "health-check",
    "journal-today",
    "journal-show",
    "journal-append",
    "journal-archive",
    "note-link",
    "graph-report",
    "moc-sync",
    "artifact-adopt",
    "artifact-finalize",
    "clean-attachments",
    "archive-list",
    "archive-pack",
    "fmt",
    "attachment-import",
    "fmt-window",
    "fmt-apply-window",
    "fmt-assets",
    "url-ingest",
    "vfs-create",
    "vfs-move",
    "vfs-copy",
    "vfs-trash",
    "vfs-trash-list",
    "vfs-untrash",
    "vfs-validate",
    "vfs-links",
    "project-create",
    "project-list",
    "project-get",
    "project-rename",
    "project-merge",
    "project-split",
    "project-undo",
    "project-history",
    "project-publish",
    "project-unpublish",
    "project-restore-links",
    "project-archive",
    "project-reopen",
    "project-trash",
    "project-untrash",
    "workbench-preset",
    "workbench-workspace",
    "slip-scan",
    "slip-commit",
    "slip-list",
    "slip-get",
    "slip-update",
    "slip-delete",
    "schema-check",
    "schema-list",
    "ticket-create",
    "ticket-list",
    "change-list",
    "change-get",
    "undo",
    "doc-status",
    "doc-commit",
    "doc-remote",
    "doc-sync",
    "trash-purge",
    "retention-sweep",
    "delivery-check",
    "usecase",
)
READ_ONLY_ACTIONS = frozenset(
    {
        "status",
        "vault-list",
        "vault-get",
        "inbox-list",
        "note-list",
        "note-get",
        "note-grep",
        "project-brief",
        "file-read",
        "note-search",
        "health-check",
        "schema-check",
        "schema-list",
        "journal-show",
        "graph-report",
        "vfs-trash-list",
        "vfs-validate",
        "vfs-links",
        "archive-list",
        "project-list",
        "project-get",
        "project-history",
        "slip-scan",
        "slip-list",
        "slip-get",
        "ticket-list",
        "change-list",
        "change-get",
        "doc-status",
    }
)

KNOWN_SLOTS: Dict[str, str] = {
    "session": "str",
    "all_vaults": "bool",
    "switch_current": "bool",
    "action": "str",
    "vault": "str",
    "ref": "str",
    "new_vault": "str",
    "title": "str",
    "obsidian_base": "bool",
    "seal_rename": "bool",
    "default_mode": "str",
    "include_deleted": "bool",
    "purge": "bool",
    "note_ref": "str",
    "target_ref": "str",
    "alias": "str",
    "zone": "str",
    "target_zone": "str",
    "doc_type": "str",
    "summary": "str",
    "tags": "str",
    "tag": "str",
    "aliases": "str",
    "content": "str",
    "status": "str",
    "on_conflict": "str",
    "superseded_by_ref": "str",
    "move_to_attic": "bool",
    "query": "str",
    "top_k": "int",
    "category": "str",
    "scope": "str",
    "level": "int",
    "check": "bool",
    "epistemic_type": "str",
    "apply": "bool",
    "project_ref": "str",
    "source_refs": "str",
    "project_id": "str",
    "new_project_id": "str",
    "workbench_conflict": "str",
    "trace": "str",
    "uc_id": "str",
    "scenarios": "str",
    "thought": "str",
    "plan_revision": "str",
    "add_combined_from": "bool",
    "archive_sources": "bool",
    "include_archived": "bool",
    "private": "bool",
    "split_plan": "str",
    "note_refs": "str",
    "op_id": "str",
    "url": "str",
    "urls": "str",
    "file_name": "str",
    "by": "str",
    "dry_run": "bool",
    "input_file": "str",
    "end_offset": "int",
    "pdf_mode": "str",
    "dpi": "int",
    "threshold": "int",
    "skip_decorative": "bool",
    "decorative_size": "int",
    "decorative_dim": "int",
    "encoding": "str",
    "dump_json": "bool",
    "dump_payload": "bool",
    "no_symbols": "bool",
    "review_mode": "str",
    "reviewer": "str",
    "deep": "bool",
    "annotate_images": "bool",
    "normalize": "bool",
    "setup": "bool",
    "trash_ref": "str",
    "force": "bool",
    "offset": "int",
    "terms": "str",
    "fields": "str",
    "adaptive": "bool",
    "min_chars": "int",
    "depth": "int",
    "parent_url": "str",
    "target_chars": "int",
    "max_chars": "int",
    "prefix": "str",
    "mode": "str",
    "topic": "str",
    "ticket_id": "str",
    "subdir": "str",
    "expected_sha256": "str",
    "marker_text": "str",
    "marker_id": "str",
    "slip_kind": "str",
    "source_note_ref": "str",
    "source_url": "str",
    "links": "str",
    "batch_key": "str",
    "mark_done": "bool",
    "create_ticket": "bool",
    "on_links": "str",
    "target_subdir": "str",
    "strict": "bool",
    "date": "str",
    "month": "str",
    "days": "int",
    "preset": "str",
    "change_id": "str",
    "agent": "str",
    "include_undone": "bool",
    "include_done": "bool",
    "slice_refs": "str",
    "toc_refs": "str",
    "provenance": "str",
    "name": "str",
    "prism": "str",
    "reason": "str",
    "with_section": "bool",
    "raw": "bool",
    "start_line": "int",
    "end_line": "int",
    "head": "int",
    "tail": "int",
    "auto_slice": "bool",
    "auto_approve": "bool",
    "boost": "str",
    "alpha": "float",
    "beta": "float",
    "hops": "int",
    "min_score": "int",
    "snippet_width": "int",
    "summary_width": "int",
    "no_snippet": "bool",
    "no_cache": "bool",
    "link_fields": "str",
    "exclude_pending": "bool",
    "exclude_draft": "bool",
    "exclude_zones": "str",
    "exclude_project_refs": "str",
    "set_meta": "str",
    "batch_json": "str",
    "section": "str",
    "with_content": "bool",
    "remote_url": "str",
    "branch": "str",
    "auto_push": "bool",
    "push": "bool",
    "direction": "str",
    "fetch": "bool",
    "clear": "bool",
    "bootstrap": "str",
    "git_layout": "str",
    "old_text": "str",
    "new_text": "str",
    "replace_all": "bool",
    "ops_json": "str",
    "allow_managed_block": "bool",
    "outline": "bool",
    "pattern": "str",
    "regex": "bool",
    "case_sensitive": "bool",
    "context_lines": "int",
    "max_matches": "int",
    "include_workbench": "bool",
    "include_slices": "bool",
    "max_notes": "int",
    "steps": "str",
    "on_error": "str",
    "no_daemon": "bool",
}

_SQL_FRAGMENT_RE = re.compile(
    r"\b(SELECT\s+.+\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|DROP\s+TABLE|UNION\s+SELECT)\b",
    re.I,
)


def _runtime_dirs() -> Tuple[Path, Path]:
    custom = os.environ.get("GEWU_APP_RUNTIME_DIR", "").strip()
    base = Path(custom) if custom else (APP_DIR / ".local")
    return base / "state", base / "logs"


def _workspace_root() -> Path:
    override = os.environ.get("GEWU_ATLAS_WORKSPACE_ROOT", "").strip()
    if override:
        return Path(override).resolve()
    return APP_DIR.parent.resolve()


def _app_id() -> str:
    meta = APP_DIR / "app.json"
    if meta.is_file():
        return str(json.loads(meta.read_text("utf-8")).get("app_id", "gewu-atlas"))
    return "gewu-atlas"


def _triad(state: str, orbit: List[str], anchor: str, *, terminal: bool = False) -> Dict[str, Any]:
    options = [opt.strip() for opt in orbit if opt.strip()]
    if not any("汇报" in opt or "结束" in opt or "收口" in opt for opt in options):
        options.append("流程阻断或本轮目标已满足: 直接向人类汇报现状即可")
    return {
        "mode": "TRIAD",
        "state": state,
        "terminal": terminal,
        "orbit_options": options,
        "anchor": anchor,
        "rendered_text": (
            "🧭 长牵引绳胶囊 (Triad Capsule)\n"
            f"📍 当前坐标 (STATE): {state}\n"
            f"🚀 可选推进 (ORBIT): {' | '.join(options)}\n"
            f"⚓ 物理锚点 (ANCHOR): {anchor}"
        ),
    }


def _guard(
    mode: str,
    reason: str,
    action_guidance: str,
    anchor: str,
    *,
    fill_template: Optional[Dict[str, Any]] = None,
    attempt_remaining: Optional[int] = None,
) -> Dict[str, Any]:
    if mode == "INPUT_REQUIRED":
        header = f"⏸️ 等待人工输入 (GUARD: INPUT_REQUIRED): {reason}"
        orbit_options = [action_guidance, "若人类暂不批准或取消: 立即向人类汇报现状并停止写入"]
    elif mode == "HALTED":
        header = f"❌ 中断拦截 (GUARD: HALTED | 禁止重试): {reason}"
        orbit_options = ["立即向人类汇报拦截原因与当前坐标（禁止重试）"]
    else:
        rem_tag = f" | 剩余重试: {max(0, attempt_remaining)}" if attempt_remaining is not None else ""
        header = f"❌ 中断拦截 (GUARD: PARAM_FIX{rem_tag}): {reason}"
        orbit_options = [action_guidance, "立即向人类汇报现状并等待决定"]

    lines = ["🛡️ 门禁熔断胶囊 (Guard Capsule)", header]
    if mode == "PARAM_FIX" and fill_template:
        lines.append(f"📝 最小填空模板: {json.dumps(fill_template, ensure_ascii=False)}")
    lines.append(f"🎯 行动指引 (ACTION): {action_guidance}")
    lines.append(f"⚓ 物理锚点 (ANCHOR): {anchor}")
    return {
        "mode": mode,
        "state": reason,
        "terminal": mode == "HALTED",
        "orbit_options": orbit_options,
        "fill_template": fill_template,
        "attempt_remaining": attempt_remaining,
        "anchor": anchor,
        "rendered_text": "\n".join(lines),
    }


def _envelope(
    ok: bool,
    code: str,
    capsule: Dict[str, Any],
    *,
    data: Optional[Dict[str, Any]] = None,
    missing_slots: Optional[List[str]] = None,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "ok": ok,
        "code": code,
        "isError": not ok,
        "data": data or {},
        "structuredContent": {"capsule": capsule},
        "reminder": capsule["rendered_text"],
    }
    if missing_slots:
        out["missing_slots"] = missing_slots
    if capsule.get("fill_template") is not None:
        out["fill_template"] = capsule["fill_template"]
    return out


def _coerce_bool(raw: Any, key: str) -> bool:
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    raise VaultStoreError("E_PARAM_INVALID", f"参数 {key} 必须为布尔值 (true/false)", exit_code=1)


_FREE_TEXT_SLOTS = frozenset({
    "content", "summary", "title", "batch_json", "thought", "trace",
    "reason", "set_meta", "provenance", "split_plan", "marker_text", "query",
})


def _check_safe_text(key: str, val: str) -> str:
    if "\x00" in val:
        raise VaultStoreError("E_PARAM_INVALID", f"参数 {key} 含非法空字节", exit_code=1)
    if key in _FREE_TEXT_SLOTS:
        return val
    if ".." in Path(val).parts:
        raise VaultStoreError("E_PARAM_INVALID", f"参数 {key} 含非法路径字符", exit_code=1)
    if _SQL_FRAGMENT_RE.search(val):
        raise VaultStoreError("E_PARAM_INVALID", f"参数 {key} 含禁止的 SQL 碎片", exit_code=1)
    return val


def _handle_seal(kek: bytes, app_id: str, entry: Dict[str, Any]) -> str:
    msg = f"{app_id}|{entry['ref']}|{entry['rel_path']}|v{entry.get('key_version', 1)}".encode("utf-8")
    return "hmac-sha256:" + hmac.new(kek, msg, hashlib.sha256).hexdigest()[:16]


def _public_entry(kek: bytes, app_id: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    """Strip all filesystem paths and directory names; expose only opaque handle & safe metadata."""
    out: Dict[str, Any] = {
        "ref": entry["ref"],
        "title": entry["title"],
        "summary": entry.get("summary", ""),
        "obsidian_base": bool(entry.get("obsidian_base", True)),
        "default_mode": entry.get("default_mode", "auto"),
        "status": entry.get("status", "active"),
        "key_version": int(entry.get("key_version", 1)),
        "handle_seal": _handle_seal(kek, app_id, entry),
        "path_sealed": True,
    }
    for ts_key in ("created_at", "updated_at", "deleted_at"):
        if entry.get(ts_key):
            out[ts_key] = entry[ts_key]
    return out


_BODY_PAYLOAD_KEYS = frozenset({"content", "raw_text", "snippet", "passage", "matches", "title", "summary", "pattern", "query", "section_content", "section_heading", "diff"})
_WIKILINK_SPAN_RE = re.compile(r"\[\[[^\]\r\n]+\]\]")


def _split_meta_and_body_payloads(obj: Any, *, in_body: bool = False) -> Tuple[Any, List[Any]]:
    """Separate structured metadata/capsules from user note-body text fields for dual-layer leak checking."""
    bodies: List[Any] = []
    if in_body:
        bodies.append(obj)
        return None, bodies
    if isinstance(obj, dict):
        meta_out: Dict[str, Any] = {}
        for k, v in obj.items():
            if k in _BODY_PAYLOAD_KEYS:
                bodies.append(v)
            else:
                m_sub, b_sub = _split_meta_and_body_payloads(v, in_body=False)
                meta_out[k] = m_sub
                bodies.extend(b_sub)
        return meta_out, bodies
    if isinstance(obj, list):
        meta_list: List[Any] = []
        for item in obj:
            m_sub, b_sub = _split_meta_and_body_payloads(item, in_body=False)
            meta_list.append(m_sub)
            bodies.extend(b_sub)
        return meta_list, bodies
    return obj, bodies


def _assert_zero_path_leak(env: Dict[str, Any], ws_root: Path, sealed_paths: List[str]) -> None:
    meta_part, body_parts = _split_meta_and_body_payloads(env)
    sealed_clean = [p for p in sealed_paths if p]

    # Layer 1 (Strict metadata/handles/capsules): never allow any volume, .trash, ws_root, or sealed paths.
    rendered_meta = json.dumps(meta_part, ensure_ascii=False)
    meta_forbidden = [
        "GewuAtlas-data",
        "Volumes/",
        "Volumes\\",
        ".trash",
        str(ws_root),
    ] + sealed_clean
    for tok in meta_forbidden:
        if tok in rendered_meta:
            raise VaultStoreError("E_HALT_SECURITY", "路径隔离防线拦截：输出含受控目录线索", exit_code=2)

    # Layer 2 (Note body & diff payloads): always block real host/vault paths even inside wikilinks,
    # and block bare sealed relative paths outside Obsidian's legitimate `[[dir/Note|Alias]]` disambiguation syntax,
    # while allowing legitimate prose terms like Obsidian `.trash/` or macOS `/Volumes/`.
    if body_parts:
        rendered_body = json.dumps(body_parts, ensure_ascii=False)
        body_hard_forbidden = [
            "GewuAtlas-data",
            "Volumes/GewuAtlas-data",
            "Volumes\\GewuAtlas-data",
            str(ws_root),
        ]
        for tok in body_hard_forbidden:
            if tok in rendered_body:
                raise VaultStoreError("E_HALT_SECURITY", "路径隔离防线拦截：输出含受控目录线索", exit_code=2)
        if sealed_clean:
            body_no_wikilinks = _WIKILINK_SPAN_RE.sub("[[...]]", rendered_body)
            for tok in sealed_clean:
                if tok in body_no_wikilinks:
                    raise VaultStoreError("E_HALT_SECURITY", "路径隔离防线拦截：输出含受控目录线索", exit_code=2)


def _parse_channels(raw_argv: List[str], stdin_text: Optional[str]) -> Dict[str, Any]:
    payload_vals: Dict[str, Any] = {}
    set_vals: Dict[str, Any] = {}
    cli_vals: Dict[str, Any] = {}

    i = 0
    while i < len(raw_argv):
        tok = raw_argv[i]
        if not tok.startswith("--"):
            raise VaultStoreError("E_PARAM_INVALID", f"不支持位置参数: {tok}", exit_code=1)
        if "=" in tok:
            k_raw, v = tok[2:].split("=", 1)
            has_inline = True
        else:
            k_raw = tok[2:]
            v = None
            has_inline = False

        key = k_raw.replace("-", "_")
        if key in ("payload", "json_stdin"):
            if not has_inline:
                if key == "json_stdin" and (i + 1 >= len(raw_argv) or raw_argv[i + 1].startswith("--")):
                    v = "-"
                elif i + 1 >= len(raw_argv):
                    raise VaultStoreError("E_PARAM_MISSING", f"--{k_raw} 缺少 JSON 内容或 -", exit_code=1)
                else:
                    v = raw_argv[i + 1]
                    i += 1
            raw_json = stdin_text if v == "-" else v
            if not raw_json or not raw_json.strip():
                raise VaultStoreError("E_PARAM_MISSING", f"--{k_raw} 载荷为空", exit_code=1)
            obj = json.loads(raw_json)
            if not isinstance(obj, dict):
                raise VaultStoreError("E_PARAM_INVALID", "JSON 载荷必须为对象", exit_code=1)
            for pk, pv in obj.items():
                nk = str(pk).replace("-", "_")
                if nk not in KNOWN_SLOTS:
                    raise VaultStoreError("E_PARAM_UNKNOWN_FIELD", f"未知字段: {pk}", exit_code=2)
                payload_vals[nk] = pv
            i += 1
            continue

        if key == "set":
            if not has_inline:
                if i + 1 >= len(raw_argv):
                    raise VaultStoreError("E_PARAM_MISSING", "--set 缺少 k=v", exit_code=1)
                v = raw_argv[i + 1]
                i += 1
            if "=" not in str(v):
                raise VaultStoreError("E_PARAM_INVALID", "--set 必须为 k=v 格式", exit_code=1)
            sk, sv = str(v).split("=", 1)
            nk = sk.strip().replace("-", "_")
            if nk not in KNOWN_SLOTS:
                raise VaultStoreError("E_PARAM_UNKNOWN_FIELD", f"未知字段: {sk}", exit_code=2)
            set_vals[nk] = sv
            i += 1
            continue

        if key not in KNOWN_SLOTS:
            raise VaultStoreError("E_PARAM_UNKNOWN_FIELD", f"未知字段: --{k_raw}", exit_code=2)

        if not has_inline:
            if i + 1 >= len(raw_argv) or raw_argv[i + 1].startswith("--"):
                if KNOWN_SLOTS[key] == "bool":
                    v = "true"
                else:
                    raise VaultStoreError("E_PARAM_MISSING", f"参数 --{k_raw} 缺少取值", exit_code=1)
            else:
                v = raw_argv[i + 1]
                i += 1
        if v == "-":
            if stdin_text is None:
                raise VaultStoreError("E_PARAM_MISSING", f"参数 --{k_raw} 指定 - 但 stdin 为空", exit_code=1)
            v = stdin_text.strip()
        cli_vals[key] = v
        i += 1

    merged: Dict[str, Any] = {
        "obsidian_base": True,
        "default_mode": "auto",
        "include_deleted": False,
        "purge": False,
    }
    merged.update(payload_vals)
    merged.update(set_vals)
    merged.update(cli_vals)
    merged["_provided_keys"] = sorted(set(payload_vals) | set(set_vals) | set(cli_vals))

    for bkey in (
        "all_vaults", "switch_current",
        "obsidian_base",
        "include_deleted",
        "purge",
        "move_to_attic",
        "add_combined_from",
        "archive_sources",
        "include_archived",
        "private",
        "check",
        "apply",
        "dry_run",
        "deep",
        "annotate_images",
        "skip_decorative",
        "dump_json",
        "dump_payload",
        "no_symbols",
        "normalize",
        "setup",
        "force",
        "adaptive",
        "mark_done",
        "create_ticket",
        "raw",
        "auto_slice",
        "auto_approve",
        "no_snippet",
        "no_cache",
        "exclude_pending",
        "exclude_draft",
        "with_content",
        "auto_push",
        "push",
        "fetch",
        "clear",
        "replace_all",
        "allow_managed_block",
        "outline",
        "regex",
        "case_sensitive",
        "include_workbench",
        "include_slices",
        "no_daemon",
    ):
        if bkey in merged:
            merged[bkey] = _coerce_bool(merged[bkey], bkey)
    for ikey in ("top_k", "level", "offset", "min_chars", "depth", "target_chars", "max_chars", "days", "dpi", "threshold", "decorative_size", "decorative_dim", "end_offset", "start_line", "end_line", "head", "tail", "hops", "min_score", "snippet_width", "summary_width", "context_lines", "max_matches", "max_notes"):
        if ikey in merged and merged[ikey] is not None:
            try:
                merged[ikey] = int(merged[ikey])
            except (ValueError, TypeError) as exc:
                raise VaultStoreError("E_PARAM_INVALID", f"--{ikey} 必须为整数", exit_code=1) from exc
    for fkey in ("alpha", "beta"):
        if fkey in merged and merged[fkey] is not None:
            try:
                merged[fkey] = float(merged[fkey])
            except (ValueError, TypeError) as exc:
                raise VaultStoreError("E_PARAM_INVALID", f"--{fkey} 必须为浮点数", exit_code=1) from exc
    for skey, sval in list(merged.items()):
        if isinstance(sval, str):
            merged[skey] = _check_safe_text(skey, sval)
    if "force" in merged and merged.get("action") not in (
        "vfs-trash",
        "moc-sync",
        "index-sync",
        "doc-commit",
        "doc-sync",
        "undo",
        "guard",
    ):
        raise VaultStoreError("E_PARAM_UNKNOWN_FIELD", "未知字段: --force", exit_code=2)
    return merged


from facade.atlas_harness_state import (
    audit_note_truth_guards,
    load_session_breaker,
    load_session_reads,
    maybe_compact_capsule,
    record_session_reads,
    save_session_breaker,
    session_fingerprint,
)


def _extract_session_fp(raw_argv: List[str], stdin_text: Optional[str] = None) -> str:
    try:
        intent = _parse_channels(raw_argv, stdin_text)
        raw_sid = str(intent.get("session") or "")
    except (VaultStoreError, ValueError, TypeError):
        raw_sid = ""
    raw_sid = raw_sid or os.environ.get("GEWU_ATLAS_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or "stateless"
    return session_fingerprint(raw_sid)


def _fingerprint(raw_argv: List[str], stdin_text: Optional[str] = None) -> str:
    # Fingerprint normalized intent, including stdin; never store plaintext payload.
    try:
        intent = _parse_channels(raw_argv, stdin_text)
    except (VaultStoreError, ValueError, TypeError):
        intent = {"argv": raw_argv, "stdin": stdin_text}
    session_key = str(intent.get("session") or os.environ.get("GEWU_ATLAS_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or "stateless")
    intent["_session_fingerprint"] = hashlib.sha256(session_key.encode()).hexdigest()
    canon = json.dumps({"skill": "gewu-atlas-cli", "intent": intent}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _load_breaker(path: Path) -> Dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return {}


def _save_breaker(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.chmod(path, 0o600)


def _extract_action_from_argv(raw_argv: Sequence[str]) -> str:
    toks = list(raw_argv)
    for idx, tok in enumerate(toks):
        if tok == "--action" and idx + 1 < len(toks):
            return toks[idx + 1].strip()
        if tok.startswith("--action="):
            return tok.split("=", 1)[1].strip()
    return ""


_ACTION_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "vault-import": {
        "command": './gewu atlas --action vault-import [--vault <folder>] [--title "<知识库名称>"] [--seal_rename true|false] [--obsidian_base true]',
        "fill_template": {
            "action": "vault-import",
            "title": "<知识库名称（可选，省略时读取原 Manifest 或通用名称）>",
            "obsidian_base": True,
            "seal_rename": False,
        },
        "anchor": "同构 Vault 放入 Volumes/GewuAtlas-data/ 后应在一次性窗口执行接管：省略 --vault 时仅在存在唯一未纳管同构库时盲采；拿到 vlt_XX 后请关闭本对话并开新对话日常使用。",
    },
    "atomic-extract": {
        "command": './gewu atlas --action atomic-extract --ref <vlt_01> --note_ref <toc_或_library句柄> [--slice_refs <slc_01,slc_02>] --epistemic_type <OBJ|SUB|SYN|HYP> --title "<原子命题标题>" --summary "<一句核心断言>" --tags "<tag1,tag2>" --content "<围绕单一主张说明依据、推理和适用边界>" [--provenance \'[{"source_ref":"nte_XX","toc_ref":"nte_YY","slice_refs":["nte_ZZ"]}]\']',
        "fill_template": {
            "action": "atomic-extract",
            "ref": "<vlt_01>",
            "note_ref": "<toc_01_或_lib_01>",
            "slice_refs": "<slc_01,slc_02>",
            "epistemic_type": "<OBJ|SUB|SYN|HYP>",
            "title": "<原子命题标题（凝练单一概念/结论，禁止照搬切片标题）>",
            "summary": "<一句可验证核心断言>",
            "tags": "<Tag1,Tag2>",
            "content": "<一卡一主张，写清依据、推理和适用边界；标题与段落按需组织>",
            "provenance": '[{"source_ref":"<lib_01>","toc_ref":"<toc_01>","slice_refs":["<slc_01>"]}]',
        },
        "anchor": "须先读取 21-Library / 24-Slices 内容并自行提炼，再传入必填 --title 与蒸馏 --content（禁止留空复制切片原文）；OBJ 写事实与条件，SUB 写取舍，SYN 写跨来源推断，HYP 写预测及反证条件。",
    },
    "ingest": {
        "command": './gewu atlas --action ingest --ref <vlt_01> [--project_ref <prj_01>] (--urls "<URL1>,<URL2>" | --file_name <文件名>) [--auto_slice true] [--target_zone library]',
        "fill_template": {
            "action": "ingest",
            "ref": "<vlt_01>",
            "project_ref": "<prj_01>",
            "urls": "<https://url1,https://url2>",
            "auto_slice": True,
            "target_zone": "library",
        },
        "anchor": "正式研究外部文献默认入库 21-Library（省略 --target_zone 即为 library），再接 note-slice 与 atomic-extract；workbench/ws-<agent>/source 仅用于临时草采。",
    },
    "note-link": {
        "command": "./gewu atlas --action note-link --ref <vlt_01> (--mode fix [--project_ref <prj_01> | --note_refs <nte_01,nte_02>] | --note_ref <nte_01> --target_ref <nte_02>)",
        "fill_template": {
            "action": "note-link",
            "ref": "<vlt_01>",
            "mode": "<link|fix>",
            "project_ref": "<prj_01>",
            "note_ref": "<nte_01>",
            "target_ref": "<nte_02>",
        },
        "anchor": "写正文/表格时支持直接写 [[nte_XX]] 或 [[标题]] 自动转规范双链；体检断链后可用 --mode fix 一键批修，无需自写脚本。",
    },
    "batch": {
        "command": "./gewu atlas --action batch --ref <vlt_01> [--project_ref <prj_01>] --json-stdin",
        "fill_template": {
            "action": "batch",
            "ref": "<vlt_01>",
            "project_ref": "<prj_01>",
            "on_error": "stop",
            "steps": [
                {
                    "id": "s1",
                    "action": "note-create",
                    "args": {
                        "project_ref": "<prj_01>",
                        "zone": "workbench",
                        "subdir": "ws-<agent>/drafts",
                        "title": "<主稿标题>",
                        "summary": "<一句话摘要>",
                        "content": "## 1. 背景\n...",
                    },
                },
                {
                    "id": "s2",
                    "action": "note-create",
                    "args": {
                        "project_ref": "<prj_01>",
                        "zone": "workbench",
                        "subdir": "ws-<agent>/drafts",
                        "title": "<关联卡标题>",
                        "summary": "<摘要>",
                        "content": "关联主稿 [[$s1.note_ref]]。",
                    },
                },
            ],
        },
        "examples": [
            'cat <<\'EOF\' | ./gewu atlas --action batch --ref vlt_01 --project_ref prj_01 --json-stdin\n{"steps":[{"id":"s1","action":"note-create","args":{"project_ref":"prj_01","zone":"workbench","subdir":"ws-agent/drafts","title":"主稿","summary":"摘要","content":"## 1. 背景\\n正文"}},{"id":"s2","action":"note-create","args":{"project_ref":"prj_01","zone":"workbench","subdir":"ws-agent/drafts","title":"引用卡","summary":"摘要","content":"引用 [[$s1.note_ref]]"}}]}\nEOF',
            'cat <<\'EOF\' | ./gewu atlas --action batch --ref vlt_01 --project_ref prj_01 --json-stdin\n{"steps":[{"id":"b","action":"project-brief","args":{"project_ref":"prj_01"}},{"id":"g","action":"note-grep","args":{"project_ref":"prj_01","pattern":"100 MiB","with_section":true}},{"id":"r","action":"note-get","args":{"note_refs":"$g.note_refs","outline":true}}]}\nEOF',
            './gewu atlas --action batch --ref vlt_01 --project_ref prj_01 --steps \'[{"id":"p1","action":"note-patch","args":{"note_ref":"nte_01","section":"## 结论","content":"新结论"}}]\'',
        ],
        "anchor": "推荐用 --json-stdin 管道传入 {\"steps\":[{\"id\":\"s1\",\"action\":\"...\",\"args\":{...}}]}（亦可经 --steps 或 --batch_json 传）；后续步骤支持 $s1.note_ref（或搜索/grep 步骤的 $g.note_refs / $g.top_note_ref）串联；全只读步骤自动走共享锁零 DOC 票/零快照快路径；写批处理单次最多 50 步，合并加锁/DOC票/快照/验货。",
    },
    "note-patch": {
        "command": './gewu atlas --action note-patch --ref <vlt_01> (--note_ref <nte_01> | --note_refs <nte_01,nte_02>) (--old_text "<旧片段>" --new_text "<新片段>" | --section "## 结论" --content "<新正文>" | --mode append|prepend --content "<追加内容>" | --ops_json \'[{"op":"section","section":"## 结论","content":"..."},{"op":"append","content":"..."}]\') [--dry_run true]',
        "fill_template": {
            "action": "note-patch",
            "ref": "<vlt_01>",
            "note_ref": "<nte_01>",
            "ops_json": [
                {"op": "section", "section": "## 结论", "content": "<替换为新的章节正文>"},
                {"op": "append", "content": "## 补充引用\n<末尾追加的新章节或段落>"},
            ],
        },
        "examples": [
            './gewu atlas --action note-patch --ref vlt_01 --note_ref nte_01 --old_text "旧句子" --new_text "新句子"',
            './gewu atlas --action note-patch --ref vlt_01 --note_ref nte_01 --section "## 4. 结论" --content "更新后的结论正文"',
            './gewu atlas --action note-patch --ref vlt_01 --note_ref nte_01 --mode append --content "## 5. 补充\\n详见 [[nte_02]]"',
            './gewu atlas --action note-patch --ref vlt_01 --note_ref nte_01 --ops_json \'[{"op":"section","section":"## 4. 结论","content":"新结论"},{"op":"append","content":"## 5. 补充\\n详见 [[nte_02]]"}]\'',
            './gewu atlas --action note-patch --ref vlt_01 --note_refs nte_01,nte_02 --old_text "[[外站断链]]" --new_text "外站断链" --replace_all true --dry_run true',
        ],
        "anchor": "多段原子补丁 --ops_json 每项必须含 \"op\" 键（replace/section/append/prepend）；支持 --note_refs 跨多卡受控批量替换（配 --dry_run true 预演，未命中自动跳过）；未找到标题或文本时报错直接附带当前可用标题大纲；自动保护受控块、21-Library 原文只读与日记人类区。",
    },
    "note-grep": {
        "command": './gewu atlas --action note-grep --ref <vlt_01> --pattern "<关键词或正则>" [--project_ref <prj_01>] [--regex true|false] [--with_section true] [--zone <library|slices|atomics|moc|artifacts|workbench>] [--context_lines 1] [--max_matches 50]',
        "fill_template": {
            "action": "note-grep",
            "ref": "<vlt_01>",
            "project_ref": "<prj_01>",
            "pattern": "<关键词或正则>",
            "regex": False,
            "with_section": False,
            "context_lines": 1,
            "max_matches": 50,
        },
        "examples": [
            './gewu atlas --action note-grep --ref vlt_01 --project_ref prj_01 --pattern "100 MiB" --with_section true',
            './gewu atlas --action note-grep --ref vlt_01 --project_ref prj_01 --pattern "TODO|FIXME" --regex true --include_workbench true',
        ],
        "anchor": "跨卡字面量或正则检索，仅返回逻辑句柄 nte_XX、行号与上下文片段（零物理路径）；加 --with_section true 可直接带回命中所属的完整章节标题与正文，免二次 note-get。",
    },
    "project-brief": {
        "command": "./gewu atlas --action project-brief --ref <vlt_01> [--project_ref <prj_01>] [--include_slices false] [--max_notes 200]",
        "fill_template": {
            "action": "project-brief",
            "ref": "<vlt_01>",
            "project_ref": "<prj_01>",
            "include_slices": False,
            "max_notes": 200,
        },
        "examples": [
            "./gewu atlas --action project-brief --ref vlt_01 --project_ref prj_01",
            "./gewu atlas --action project-brief --ref vlt_01 --project_ref prj_01 --include_slices true",
        ],
        "anchor": "一屏返回分区统计、核心卡片句柄（MOC/Artifacts/Library/Atomics/Workbench）与双链骨架；默认省略切片明细以节省上下文。",
    },
    "note-get": {
        "command": "./gewu atlas --action note-get --ref <vlt_01> (--note_ref <nte_01> | --note_refs <nte_01,nte_02>) [--outline true] [--start_line 1 --end_line 80]",
        "fill_template": {
            "action": "note-get",
            "ref": "<vlt_01>",
            "note_refs": "<nte_01,nte_02>",
            "outline": False,
        },
        "examples": [
            "./gewu atlas --action note-get --ref vlt_01 --note_refs nte_01,nte_02 --outline true",
            "./gewu atlas --action note-get --ref vlt_01 --note_ref nte_01 --start_line 1 --end_line 80",
        ],
        "anchor": "传 --outline true 仅返回标题大纲与行号（不返回全文，大幅省上下文）；支持 --note_refs 逗号分隔批量读取，以及 --start_line / --end_line 分段精读。",
    },
}


def _find_dict_action_spec(action_name: str) -> Optional[Dict[str, Any]]:
    try:
        p = APP_DIR / "dicts" / "commands.yaml"
        lines = p.read_text(encoding="utf-8").splitlines()
        cur: Optional[Dict[str, Any]] = None
        for ln in lines:
            m_name = re.match(r'^\s*-\s+name:\s*["\']?([^"\']+)["\']?\s*$', ln)
            if m_name:
                if cur and cur.get("name") == action_name:
                    return cur
                cur = {"name": m_name.group(1).strip(), "slots": []}
                continue
            if cur is None:
                continue
            m_kv = re.match(r"^\s+(mode|summary|slots):\s*(.+?)\s*$", ln)
            if not m_kv:
                continue
            k, v = m_kv.group(1), m_kv.group(2)
            if k == "slots" and v.startswith("["):
                cur["slots"] = json.loads(v)
            else:
                cur[k] = v.strip().strip('"\'')
        if cur and cur.get("name") == action_name:
            return cur
    except Exception:
        pass
    return None


def _template_payload(raw_argv: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    req_action = _extract_action_from_argv(raw_argv or [])
    if req_action and req_action in ACTIONS:
        spec = _find_dict_action_spec(req_action)
        curated = _ACTION_TEMPLATES.get(req_action)
        raw_slots = list((spec or {}).get("slots") or [])
        slot_entries: List[Dict[str, Any]] = []
        auto_tpl: Dict[str, Any] = {"action": req_action}
        for s in raw_slots:
            s_str = str(s).strip()
            optional = s_str.startswith("[") and s_str.endswith("]")
            name = s_str.strip("[]").lstrip("-")
            slot_entries.append({"name": name, "required": not optional, "flag": s_str})
            if not optional:
                auto_tpl[name] = "<vlt_01>" if name == "ref" else f"<{name}>"
        fill = (curated or {}).get("fill_template") or auto_tpl
        cmd = (curated or {}).get("command") or (f"./gewu atlas --action {req_action} " + " ".join(str(s) for s in raw_slots))
        data = {
            "schema": "gewu.fill-template/1",
            "action": req_action,
            "summary": str((spec or {}).get("summary") or req_action),
            "mode": str((spec or {}).get("mode") or ""),
            "command": cmd,
            "fill_template": fill,
            "slots": slot_entries,
        }
        if (curated or {}).get("examples"):
            data["examples"] = list(curated["examples"])
        anchor = (curated or {}).get("anchor") or "按 fill_template 传入参数；所有路径均通过句柄（--ref / --project_ref / --note_ref）托管隔离。"
        cap = _triad(
            f"已返回动作 `{req_action}` 的专用参数模板与用法（零副作用）",
            [f"执行动作: {cmd}", "核对必填字段后发起调用"],
            anchor,
            terminal=False,
        )
        return _envelope(True, "OK_TEMPLATE", cap, data=data)

    recommended_actions = [
        "vault",
        "project-create",
        "project-list",
        "project-get",
        "project-brief",
        "workbench-workspace",
        "note-search",
        "note-grep",
        "note-list",
        "note-get",
        "note-create",
        "note-patch",
        "note-update",
        "batch",
        "ingest",
        "note-slice",
        "atomic-extract",
        "atomic-approve",
        "moc-sync",
        "artifact-adopt",
        "artifact-finalize",
        "guard",
    ]
    data = {
        "schema": "gewu.fill-template/1",
        "command": "./gewu atlas --action <vault|project-list|note-search|note-get|note-create|note-update|ingest|guard> [--ref <vlt_01>]",
        "recommended_actions": recommended_actions,
        "legacy_aliases_supported": True,
        "fill_template": {
            "action": "<vault|project-list|note-search|note-get|note-create|note-update|ingest|guard>",
            "title": "<知识库名称>",
            "obsidian_base": True,
        },
        "vault_context": {"priority": ["explicit", "session", "default_on_first_use"], "session_sources": ["--session", "GEWU_ATLAS_SESSION_ID", "CODEX_THREAD_ID"], "actions": sorted(CONTEXT_ACTIONS), "all_vaults": "note-search only; explicit federation", "switch_current": "vault-default-set only"},
        "slots": [
            {"name": "session", "kind": "str", "required": False, "description": "稳定会话 ID；缺省使用宿主上下文"},
            {"name": "all_vaults", "kind": "bool", "required": False, "description": "显式跨库检索，仅 note-search"},
            {"name": "switch_current", "kind": "bool", "required": False, "description": "设置默认时同时切换当前会话"},
            {"name": "action", "kind": "enum", "required": True, "choices": list(ACTIONS), "description": "动作类型", "example": "vault-create"},
            {"name": "ref", "kind": "ref", "required": False, "choices": [], "description": "托管 Vault 逻辑短句柄（如 vlt_01）", "example": "vlt_01"},
            {"name": "vault", "kind": "str", "required": False, "choices": [], "description": "可选库槽位标识；省略时由运行时自动分配独立槽位", "example": "main"},
            {"name": "title", "kind": "str", "required": False, "choices": [], "description": "Vault 展示标题", "example": "格物主库"},
            {"name": "obsidian_base", "kind": "bool", "required": False, "choices": [True, False], "description": "是否生成 Obsidian .base 看板", "example": True},
        ],
    }
    cap = _triad(
        "已返回 GewuAtlas 最小填空模板（传入 --action <动作名> --template 可直查任意动作完整参数）",
        ["按动作查模板: ./gewu atlas --action atomic-extract --template", "按 fill_template 传入 ./gewu atlas --action ..."],
        "接口采用逻辑句柄（--ref <vlt_01>）托管存储拓扑以保障重命名与双链不断裂；本机查看原目录请由用户在 Finder / Obsidian 打开。",
        terminal=False,
    )
    return _envelope(True, "OK_TEMPLATE", cap, data=data)


def raw_argv_keys(raw_argv: List[str]) -> set[str]:
    keys: set[str] = set()
    for tok in raw_argv:
        if tok.startswith("--"):
            k = tok[2:].split("=", 1)[0].replace("-", "_")
            keys.add(k)
    return keys


@contextmanager
def _atlas_state_lock(state_dir: Path, *, shared: bool = False):
    """Serialize concurrent CLI operations across POSIX and Windows; read-only ops take LOCK_SH."""
    from storage.vault_kek_store import registry_file_lock

    with registry_file_lock(state_dir / ".atlas-state", shared=shared):
        yield


def _normalize_unified_action(action: str, args: Dict[str, Any]) -> str:
    """Resolve merged ergonomic verbs (vault/ingest/trash/journal/slip/guard) onto canonical operators."""
    mode = str(args.get("mode") or "").strip().lower()
    if action == "vault":
        m = mode or "summary"
        if m == "summary":
            return "vault"
        mapping = {
            "list": "vault-list",
            "get": "vault-get",
            "use": "vault-use",
            "default-set": "vault-default-set",
            "reset": "vault-reset",
            "create": "vault-create",
            "import": "vault-import",
            "update": "vault-update",
        }
        if m not in mapping:
            raise VaultStoreError("E_PARAM_INVALID", f"vault 不支持 mode={m}，可选: summary/list/get/use/default-set/reset/create/import/update", exit_code=1)
        return mapping[m]
    if action == "ingest":
        m = mode or "auto"
        if m == "list":
            return "inbox-list"
        if m == "import" or (m == "auto" and str(args.get("input_file") or "").strip()):
            return "attachment-import"
        if m == "url" or (m == "auto" and (str(args.get("url") or "").strip() or args.get("urls")) and not str(args.get("file_name") or "").strip() and not str(args.get("note_ref") or "").strip()):
            return "url-ingest"
        if m == "route" or (m == "auto" and str(args.get("note_ref") or "").strip() and str(args.get("target_zone") or "").strip() and not str(args.get("file_name") or "").strip()):
            return "note-route"
        if m in ("auto", "fmt"):
            return "fmt"
        raise VaultStoreError("E_PARAM_INVALID", f"ingest 不支持 mode={m}，可选: auto/list/import/url/fmt/route", exit_code=1)
    if action == "trash":
        if not mode:
            if str(args.get("trash_ref") or "").strip():
                m = "restore"
            elif any(str(args.get(k) or "").strip() for k in ("note_ref", "note_refs", "file_name", "subdir", "project_ref")) or str(args.get("scope") or "").strip().lower() == "vault":
                m = "delete"
            else:
                m = "list"
        else:
            m = mode
        if m == "list":
            return "vfs-trash-list"
        scope_val = str(args.get("scope") or "").strip().lower()
        if m == "restore":
            if scope_val == "project":
                return "project-untrash"
            if scope_val == "vault":
                return "vault-restore"
            return "vfs-untrash"
        if m == "delete":
            if scope_val == "vault":
                return "vault-delete"
            if str(args.get("project_ref") or "").strip() and not any(str(args.get(k) or "").strip() for k in ("note_ref", "note_refs", "file_name", "subdir", "zone")):
                return "project-trash"
            return "vfs-trash"
        raise VaultStoreError("E_PARAM_INVALID", f"trash 不支持 mode={m}，可选: delete/list/restore", exit_code=1)
    if action == "journal":
        m = mode or ("append" if str(args.get("content") or "").strip() else "show")
        mapping = {"show": "journal-show", "today": "journal-today", "append": "journal-append", "archive": "journal-archive"}
        if m not in mapping:
            raise VaultStoreError("E_PARAM_INVALID", f"journal 不支持 mode={m}，可选: show/today/append/archive", exit_code=1)
        return mapping[m]
    if action == "slip":
        m = mode or "list"
        mapping = {"scan": "slip-scan", "commit": "slip-commit", "list": "slip-list", "get": "slip-get", "update": "slip-update", "delete": "slip-delete"}
        if m not in mapping:
            raise VaultStoreError("E_PARAM_INVALID", f"slip 不支持 mode={m}，可选: scan/commit/list/get/update/delete", exit_code=1)
        return mapping[m]
    if action == "guard":
        m = mode or "health"
        mapping = {
            "health": "health-check",
            "schema": "schema-check",
            "schema-list": "schema-list",
            "validate": "vfs-validate",
            "links": "vfs-links",
            "doc-status": "doc-status",
            "doc-commit": "doc-commit",
            "doc-remote": "doc-remote",
            "doc-sync": "doc-sync",
            "clean-attachments": "clean-attachments",
            "archive-list": "archive-list",
            "archive-pack": "archive-pack",
            "trash-purge": "trash-purge",
            "retention-sweep": "retention-sweep",
            "change-list": "change-list",
            "change-get": "change-get",
            "undo": "undo",
            "delivery": "delivery-check",
            "verify": "delivery-check",
        }
        if m not in mapping:
            raise VaultStoreError("E_PARAM_INVALID", f"guard 不支持 mode={m}", exit_code=1)
        return mapping[m]
    return action


def _execute_action(
    args: Dict[str, Any],
    *,
    hot_registries: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Dict[str, Any], List[str]]:
    action = str(args.get("action", "")).strip()
    ws_root = _workspace_root()
    if not action:
        cap = _guard(
            "PARAM_FIX",
            "E_PARAM_MISSING: 缺少必填参数 --action",
            "请补充 --action 后重试（先用 --template 查看模板）。",
            "同指纹连续失败 2 次将触发硬熔断。",
            fill_template={"action": "<vault|note-search|note-list|note-get|file-read|ingest|trash|guard>"},
            attempt_remaining=1,
        )
        return 1, _envelope(False, "E_PARAM_MISSING", cap, missing_slots=["action"]), []
    if action not in ACTIONS:
        cap = _guard(
            "PARAM_FIX",
            f"E_PARAM_INVALID: 不支持的 action '{action}'",
            f"请从允许枚举中选择: {', '.join(ACTIONS)}",
            "仅接受声明动作名，严禁自造动作。",
            fill_template={"action": "<vault|note-search|note-list|note-get|file-read|ingest|trash|guard>"},
            attempt_remaining=1,
        )
        return 1, _envelope(False, "E_PARAM_INVALID", cap), []
    action = _normalize_unified_action(action, args)
    if args.get("dry_run") and not (
        action in {
            "trash-purge", "retention-sweep", "workbench-workspace", "fmt",
            "url-ingest", "journal-archive", "archive-pack", "workbench-preset", "project-undo",
            "moc-sync", "note-patch",
        }
        or (action == "note-link" and str(args.get("mode") or "link").strip().lower() == "fix")
    ):
        raise VaultStoreError("E_PARAM_INVALID", "此动作不支持 --dry_run；未执行写入。请查动作帮助，不得把预演当授权执行。", exit_code=1)

    state_dir, _ = _runtime_dirs()
    is_shared = _is_non_mutating_vault_op(action, args) or action in ("vault", "status", "vault-list", "vault-get", "schema-list", "ticket-list")
    if action == "url-ingest" and (args.get("urls") or args.get("url")) and not args.get("dry_run"):
        from pipeline.atlas_fmt_engine import UrlPreparation, active_preparation
        import tempfile
        with _atlas_state_lock(state_dir):
            plan = _execute_action_locked(action, args, ws_root, state_dir, hot_registries=hot_registries,
                                          plan_urls=True)
            if not isinstance(plan, UrlPreparation):
                return plan
        with tempfile.TemporaryDirectory(prefix="atlas-url-batch-", dir=state_dir) as temp:
            plan.prepare(Path(temp).resolve())
            token = active_preparation.set(plan)
            try:
                with _atlas_state_lock(state_dir):
                    return _execute_action_locked(action, args, ws_root, state_dir)
            finally:
                active_preparation.reset(token)
    with _atlas_state_lock(state_dir, shared=is_shared):
        return _execute_action_locked(action, args, ws_root, state_dir, hot_registries=hot_registries)


def _execute_action_locked(
    action: str,
    args: Dict[str, Any],
    ws_root: Path,
    state_dir: Path,
    *,
    plan_urls: bool = False,
    hot_registries: Optional[Dict[str, Any]] = None,
):
    app_id = _app_id()
    kek = hot_registries["kek"] if (hot_registries and hot_registries.get("kek") is not None) else load_or_init_kek(state_dir / "atlas-vaults.kek")
    reg = hot_registries["vault_reg"] if (hot_registries and hot_registries.get("vault_reg") is not None) else KekVaultRegistry(app_id, state_dir / "atlas-vaults.enc", kek)
    all_existing = reg.list_vaults(include_deleted=True)
    sealed_paths = [
        p
        for row in all_existing
        for p in (row.get("rel_path", ""), row.get("trash_rel_path") or "")
        if p
    ]

    sid = session_id(args)
    context = VaultContext(state_dir, app_id, kek, reg,
                           available=lambda row: resolve_safe_vault_dir(ws_root, row["rel_path"]).is_dir())
    if args.get("all_vaults") and (action != "note-search" or args.get("ref") or args.get("vault")):
        raise VaultStoreError("E_PARAM_INVALID", "all_vaults 仅用于未指定单库的 note-search", 1)
    if args.get("switch_current") and action != "vault-default-set":
        raise VaultStoreError("E_PARAM_INVALID", "switch_current 仅用于 vault-default-set", 1)
    if action in CONTEXT_ACTIONS:
        if args.get("vault") or (args.get("ref") and action not in {"vault-use", "vault-default-set"}):
            raise VaultStoreError("E_PARAM_INVALID", "上下文动作仅在选择库时接受 --ref", 1)
        result = context.command(action, sid, str(args.get("ref") or "") or None,
                                 bool(args.get("switch_current", False)))
        cap = _triad("知识库选择已确认", ["直接向人类汇报现状"], "仅更新选择偏好，不移动数据或建立跨库依赖。", terminal=True)
        return 0, _envelope(True, "OK_VAULT_CONTEXT", cap, data=result), sealed_paths

    if action == "vault":
        try:
            ctx_res = context.command("vault-context", sid, None, False)
            ctx_res["session_bound"] = True
        except VaultStoreError as exc:
            if exc.code != "E_VAULT_SESSION_REQUIRED":
                raise
            ctx_res = context.command("vault-default-get", sid, None, False)
            ctx_res["session_bound"] = False
        rows = reg.list_vaults(include_deleted=bool(args.get("include_deleted", False)))
        items = []
        for r in rows:
            disk = inspect_vault_tree(ws_root, r["rel_path"]) if not r["deleted"] else {"exists_on_disk": False}
            item = _public_entry(kek, app_id, r)
            item["exists_on_disk"] = bool(disk["exists_on_disk"])
            items.append(item)
        active = [r for r in all_existing if not r.get("deleted")]
        cap = _triad(
            f"已加载当前库上下文与全部托管 Vault 清单（活跃 {len(active)} 个）[只读完成]",
            [
                "列出课题: ./gewu atlas --action project-list --ref <vlt_01>",
                "检索笔记: ./gewu atlas --action note-search --query <关键词>",
            ],
            "单步返回当前绑定、默认库与全部逻辑句柄，无需连续调用 vault-context 与 vault-list。",
            terminal=True,
        )
        return 0, _envelope(
            True,
            "OK_VAULT_SUMMARY",
            cap,
            data={
                "vault_context": ctx_res,
                "active_vaults": len(active),
                "total_vaults": len(all_existing),
                "count": len(items),
                "vaults": items,
                "path_sealed": True,
            },
        ), sealed_paths

    if action == "status":
        active = [r for r in all_existing if not r.get("deleted")]
        cap = _triad(
            f"GewuAtlas 注册索引正常：活跃 Vault={len(active)}，总登记={len(all_existing)} [只读完成]",
            [
                "新建知识库: ./gewu atlas --action vault-create --title <名称>",
                "列出知识库: ./gewu atlas --action vault-list",
                "直接向人类汇报现状",
            ],
            "知识库按逻辑短句柄统一调度，存储布局由运行时托管以保持迁移与索引一致性。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_STATUS", cap, data={"app_id": app_id, "active_vaults": len(active), "total_vaults": len(all_existing), "path_sealed": True}), sealed_paths

    if action == "vault-create":
        vid_raw = str(args.get("vault", "")).strip()
        if not vid_raw:
            vid = f"vlt-{secrets.token_hex(4)}"
        else:
            vid = normalize_vault_id(vid_raw)
        rel_path = make_vault_rel_path(vid)
        sealed_paths.append(rel_path)
        title = str(args.get("title") or "未命名知识库").strip()
        obsidian_base = bool(args.get("obsidian_base", True))
        default_mode = str(args.get("default_mode", "auto")).strip()
        if default_mode not in ("auto", "supervised"):
            raise VaultStoreError("E_PARAM_INVALID", "--default-mode 仅支持 auto 或 supervised", exit_code=1)

        if any(v["vault_id"] == vid for v in all_existing):
            raise VaultStoreError("E_PARAM_CONFLICT", "目标 Vault 已在 KEK 注册库中存在", exit_code=1)

        tree_info = init_vault_tree(ws_root, rel_path, title=title, obsidian_base=obsidian_base, default_mode=default_mode)
        entry = reg.create_vault(
            vault_id=vid,
            rel_path=rel_path,
            title=title,
            obsidian_base=obsidian_base,
            default_mode=default_mode,
            summary=str(args.get("summary") or ""),
        )
        vdir_new = resolve_safe_vault_dir(ws_root, rel_path)
        doc_git = init_vault_git_repo(state_dir, entry["ref"], vdir_new)
        pub = _public_entry(kek, app_id, entry)
        pub["subdirs_created"] = tree_info["subdirs_created"]
        pub["obsidian_base_files"] = tree_info["obsidian_base_files"]
        pub["doc_git"] = doc_git
        pub["new_conversation_recommended"] = True
        cap = _triad(
            f"已创建托管 Vault（句柄 {entry['ref']}，标题「{title}」），建议开启新对话继续日常使用",
            [
                f"请提示用户开启新对话，并在新对话中使用逻辑句柄 --ref {entry['ref']} 执行检索与笔记操作",
                f"如需复核骨架完整性（可选）: ./gewu atlas --action vault-get --ref {entry['ref']}",
            ],
            f"新对话仅持有逻辑句柄 {entry['ref']}，避免建库目录参数干扰后续跨环境读写；本机挂载 Obsidian 由用户直接在 Finder 打开即可。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_VAULT_CREATED", cap, data=pub), sealed_paths

    if action == "vault-import":
        vid_raw = str(args.get("vault", "")).strip()
        default_mode = str(args.get("default_mode", "auto")).strip()
        if default_mode not in ("auto", "supervised"):
            raise VaultStoreError("E_PARAM_INVALID", "--default-mode 仅支持 auto 或 supervised", exit_code=1)
        obsidian_base = bool(args.get("obsidian_base", True))
        seal_rename = bool(args.get("seal_rename", False))
        title_arg = str(args.get("title") or "").strip() or None
        taken_ids = [str(v["vault_id"]) for v in all_existing]
        registered_rels = [str(v.get("rel_path") or "") for v in all_existing]

        if vid_raw:
            candidate_name = vid_raw
        else:
            candidates = discover_unregistered_isomorphic_vaults(ws_root, taken_ids, registered_rels)
            if not candidates:
                raise VaultStoreError(
                    "E_NOT_FOUND",
                    "未检测到未纳管的同构 Vault；请确认目录已放入数据卷并包含 20-AI-Zone 等同构骨架（勿放入非同构目录）",
                    exit_code=1,
                )
            if len(candidates) > 1:
                raise VaultStoreError(
                    "E_PARAM_CONFLICT",
                    f"检测到 {len(candidates)} 个未纳管同构候选目录，为防误选已拦截；请仅保留 1 个待接管目录或通过 --vault 明确指定并在完成后新开对话",
                    exit_code=1,
                )
            candidate_name = candidates[0].name

        adopted = adopt_vault_tree(
            ws_root,
            candidate_name,
            title=title_arg,
            obsidian_base=obsidian_base,
            default_mode=default_mode,
            seal_rename=seal_rename,
            taken_vault_ids=taken_ids,
        )
        sealed_paths.append(adopted["rel_path"])
        if adopted["old_rel_path"] != adopted["rel_path"]:
            sealed_paths.append(adopted["old_rel_path"])
        if len(candidate_name) >= 4:
            sealed_paths.append(candidate_name)

        entry = reg.create_vault(
            vault_id=adopted["vault_id"],
            rel_path=adopted["rel_path"],
            title=adopted["title"],
            obsidian_base=obsidian_base,
            default_mode=default_mode,
            summary=str(args.get("summary") or ""),
        )
        vdir_new = adopted["vault_dir"]
        doc_git = init_vault_git_repo(state_dir, entry["ref"], vdir_new)

        note_reg = KekNoteRegistry(app_id, state_dir / "atlas-notes.enc", kek)
        proj_reg = KekProjectRegistry(app_id, state_dir / "atlas-projects.enc", kek)
        p_root = vdir_new / "30-Projects"
        proj_rels = (
            [
                f"30-Projects/{c.name}"
                for c in sorted(p_root.iterdir())
                if not c.is_symlink() and c.is_dir() and not c.name.startswith(".")
            ]
            if p_root.is_dir()
            else []
        )
        pmap = proj_reg.sync_projects(entry["ref"], proj_rels)
        _, n_map = reconcile_note_index(
            vault_dir=vdir_new,
            vault_ref=entry["ref"],
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            cache_dir=state_dir / "notes_index",
        )

        pub = _public_entry(kek, app_id, entry)
        pub["added_layout_dirs_count"] = adopted["added_layout_dirs_count"]
        pub["synced_base_files"] = adopted["synced_base_files"]
        pub["preserved_base_files_count"] = adopted["preserved_base_files_count"]
        pub["notes_indexed"] = len(n_map)
        pub["projects_indexed"] = len(pmap)
        pub["seal_renamed"] = adopted["seal_renamed"]
        pub["doc_git"] = doc_git
        pub["new_conversation_recommended"] = True
        cap = _triad(
            f"已一次性安全接管同构 Vault（句柄 {entry['ref']}，标题「{adopted['title']}」，纳管 {len(n_map)} 篇笔记 / {len(pmap)} 个课题），建议关闭本窗口并开启新对话",
            [
                f"请立即结束当前一次性导入会话，在新对话中使用逻辑句柄 --ref {entry['ref']} 继续操作",
                f"在新对话检查库内笔记: ./gewu atlas --action note-list --ref {entry['ref']}",
            ],
            f"已隔离物理目录线索并完成 KEK 注册与句柄索引；新对话仅持有 --ref {entry['ref']}。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_VAULT_IMPORTED", cap, data=pub), sealed_paths

    if action == "vault-list":
        include_deleted = bool(args.get("include_deleted", False))
        rows = reg.list_vaults(include_deleted=include_deleted)
        items = []
        for r in rows:
            disk = inspect_vault_tree(ws_root, r["rel_path"]) if not r["deleted"] else {"exists_on_disk": False}
            item = _public_entry(kek, app_id, r)
            item["exists_on_disk"] = bool(disk["exists_on_disk"])
            items.append(item)
        cap = _triad(
            f"已列出 {len(items)} 个托管 Vault 逻辑句柄 [只读完成]",
            [
                "查验指定 Vault: ./gewu atlas --action vault-get --ref <vlt_01>",
                "直接向人类汇报现状",
            ],
            "接口统一以逻辑句柄（vlt_xx）和展示标题交付，存储位置由运行时托管以保持迁移与索引一致；用户本地请在 Finder / Obsidian 中查看。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_VAULT_LIST", cap, data={"count": len(items), "vaults": items, "path_sealed": True}), sealed_paths

    if action == "ticket-create":
        t_title = str(args.get("title") or "").strip()
        if not t_title:
            raise VaultStoreError("E_PARAM_MISSING", "ticket-create 需要 --title", exit_code=1)
        res = create_atlas_ticket(ws_root, category=str(args.get("category") or "DOC"), title=t_title)
        cap = _triad(
            f"已通过 GewuTicket 跨域网关登记工单「{t_title}」",
            ["列出工单: ./gewu atlas --action ticket-list", "直接向人类汇报现状"],
            "工单动作全程委托 ./gewu ticket 独立域维护。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_TICKET_CREATED", cap, data=res), sealed_paths

    if action == "ticket-list":
        res = list_atlas_tickets(ws_root)
        cap = _triad(
            f"已从 GewuTicket 读取 {res.get('count', 0)} 条工单记录 [只读完成]",
            ["新建工单: ./gewu atlas --action ticket-create --category DOC --title <标题>", "直接向人类汇报现状"],
            "工单动作全程委托 ./gewu ticket 独立域维护。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_TICKET_LIST", cap, data=res), sealed_paths

    if action == "usecase":
        from notes.atlas_usecase_engine import (
            usecase_delete,
            usecase_get,
            usecase_list,
            usecase_match,
            usecase_upsert,
        )
        uc_mode = str(args.get("mode") or "list").strip().lower()
        if uc_mode == "list":
            res = usecase_list(APP_DIR)
            cap = _triad(
                f"已列出 {res['count']} 张 Agent 实战经验路书（usecase-for-agent）[只读完成]",
                [
                    "按任务关键词匹配路书: ./gewu atlas --action usecase --mode match --query <关键词>",
                    "读取单张路书详情: ./gewu atlas --action usecase --mode get --uc_id <UC-01-...>",
                    "沉淀/更新本次任务心得: ./gewu atlas --action usecase --mode upsert --uc_id <UC-XX> --title <标题> --content <正文>",
                ],
                "入口索引位于 GewuAtlas/usecase-for-agent/README.md，开工前查阅可省去 30%~50% 试错往返。",
                terminal=True,
            )
            return 0, _envelope(True, "OK_USECASE_LIST", cap, data=res), sealed_paths
        if uc_mode == "get":
            res = usecase_get(APP_DIR, str(args.get("uc_id") or args.get("id") or ""))
            cap = _triad(
                f"已读取实战经验路书 {res['uc_id']}（「{res['title']}」）[只读完成]",
                ["按路书中的「高频损耗→直通对策」与「黄金流水线」执行当前任务"],
                "实战路书提炼自历次高分交付与避坑复盘，优先于盲目试错。",
                terminal=True,
            )
            return 0, _envelope(True, "OK_USECASE_GET", cap, data=res), sealed_paths
        if uc_mode == "match":
            res = usecase_match(APP_DIR, str(args.get("query") or ""), top_k=int(args.get("top_k") or 3))
            cap = _triad(
                f"已按关键词匹配到 {res['count']} 张实战经验路书 [只读完成]",
                ["直接参照命中路书的黄金流水线与对策表推进业务"],
                "已返回匹配路书全文，无需重复读取。",
                terminal=True,
            )
            return 0, _envelope(True, "OK_USECASE_MATCH", cap, data=res), sealed_paths
        if uc_mode in ("upsert", "create", "update"):
            res = usecase_upsert(
                APP_DIR,
                uc_id=str(args.get("uc_id") or args.get("id") or ""),
                title=str(args.get("title") or ""),
                scenarios=str(args.get("scenarios") or args.get("summary") or ""),
                tags=args.get("tags"),
                content=str(args.get("content") or ""),
                by=str(args.get("by") or args.get("agent") or "ai"),
            )
            cap = _triad(
                f"已{('更新' if res['operation'] == 'updated' else '新建')}实战经验路书 {res['uc_id']} 并同步刷新 usecase-for-agent/README.md 索引",
                ["查看最新路书清单: ./gewu atlas --action usecase --mode list", "直接向人类汇报沉淀结果"],
                "经验卡已落盘至 GewuAtlas/usecase-for-agent/ 并自动更新 README.md 导航表，下次会话开场即可复用。",
                terminal=True,
            )
            return 0, _envelope(True, "OK_USECASE_UPSERTED", cap, data=res), sealed_paths
        if uc_mode == "delete":
            res = usecase_delete(APP_DIR, str(args.get("uc_id") or args.get("id") or ""))
            cap = _triad(
                f"已删除实战经验路书 {res['uc_id']} 并同步刷新 usecase-for-agent/README.md",
                ["查看最新路书清单: ./gewu atlas --action usecase --mode list"],
                "已从 usecase-for-agent/ 移除并更新索引。",
                terminal=True,
            )
            return 0, _envelope(True, "OK_USECASE_DELETED", cap, data=res), sealed_paths
        raise VaultStoreError("E_PARAM_INVALID", f"usecase 不支持 mode={uc_mode}，可选: list/get/match/upsert/delete", exit_code=1)

    if action == "schema-list":
        dt_arg = str(args.get("doc_type") or "").strip() or None
        res = engine_schema_list(doc_type=dt_arg)
        cap = _triad(
            f"已加载 GewuAtlas 权威 YAML 字典（metadata={res['dict_versions']['metadata']}，layout={res['dict_versions']['layout']}，含 {len(res['schemas'])} 类 Schema）[只读完成]",
            ["按字典巡检指定 Vault: ./gewu atlas --action schema-check --ref <vlt_01>", "直接向人类汇报现状"],
            "字典文件统一置于 GewuAtlas/dicts/，为写入校验与防污染巡检的单一真源。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_SCHEMA_LIST", cap, data=res), sealed_paths

    if action == "workbench-preset" and not str(args.get("preset") or "").strip() and not str(args.get("ref") or "").strip() and not str(args.get("vault") or "").strip():
        presets = engine_list_workbench_presets()
        cap = _triad(
            f"已列出 {len(presets)} 套工作台双层结构预设（multi-agent / engineering / academic / solo）[只读完成]",
            ["为指定课题应用预设: ./gewu atlas --action workbench-preset --ref <vlt_01> --project_ref <prj_01> --preset <engineering|academic|multi-agent|solo>"],
            "预设仅规划工作台 L1 空间与 L2 工序目录及 README 路标；工作台内允许自由存放未加 YAML 的原始 note 笔记。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_WORKBENCH_PRESET_LIST", cap, data={"mode": "list", "count": len(presets), "presets": presets, "path_sealed": True}), sealed_paths

    if action == "fmt" and bool(args.get("setup", False)):
        res = engine_setup_fmt(force=bool(args.get("apply", False)))
        cap = _triad(
            f"fmt 多模态转换运行环境就绪（状态 {res['setup']}，已核验 {res['packages_verified']} 个第三方依赖）",
            ["对指定知识库执行转译: ./gewu atlas --action fmt --ref <vlt_01>", "直接向人类汇报现状"],
            "所有第三方依赖均隔离在受控运行时 .runtime/fmt-venv，不污染全局环境或分发包。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_FMT_SETUP", cap, data=res), sealed_paths

    vault_id_arg = str(args.get("vault", "")).strip() or None
    ref_arg = str(args.get("ref", "")).strip() or None
    if ref_arg and not HANDLE_RE.fullmatch(ref_arg):
        raise VaultStoreError("E_PARAM_INVALID", "非法句柄格式（期望如 vlt_01）", exit_code=1)
    if vault_id_arg:
        vault_id_arg = normalize_vault_id(vault_id_arg)
    if vault_id_arg and ref_arg:
        row = reg.get_vault(vault_id=vault_id_arg)
        if row["ref"] != ref_arg:
            raise VaultStoreError("E_PARAM_CONFLICT", "vault 与 ref 指向不同知识库", 1)
    explicit_ref = ref_arg or (str(reg.get_vault(vault_id=vault_id_arg)["ref"]) if vault_id_arg else None)
    if args.get("all_vaults"):
        selection = {"ref": None, "selection_source": "explicit_federated"}
    else:
        selection = context.resolve(sid, explicit_ref, allow_legacy_federated=(action == "note-search"))
    args["_vault_selection"] = selection
    ref_arg = selection["ref"]
    if ref_arg:
        args["ref"] = ref_arg
    if action == "note-search" and not vault_id_arg and not ref_arg:
        top_k = int(args.get("top_k") or 10)
        note_reg = hot_registries["note_reg"] if (hot_registries and hot_registries.get("note_reg") is not None) else KekNoteRegistry(app_id, state_dir / "atlas-notes.enc", kek)
        active_rows = [r for r in reg.list_vaults(include_deleted=False) if not r.get("deleted")]
        vault_entries = [
            (str(r["ref"]), str(r.get("title") or ""), resolve_safe_vault_dir(ws_root, str(r["rel_path"])))
            for r in active_rows
        ]
        ex_zones = [z.strip() for z in str(args.get("exclude_zones") or "").split(",") if z.strip()] or None
        hits, note_paths = engine_note_search_all_vaults(
            vault_entries=vault_entries,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            query=str(args.get("query") or ""),
            terms_arg=args.get("terms"),
            zone_filter=args.get("zone"),
            doc_type_filter=args.get("doc_type"),
            epistemic_type_filter=args.get("epistemic_type"),
            status_filter=args.get("status"),
            exclude_zones=ex_zones,
            include_archived=bool(args.get("include_archived", False)),
            include_private=bool(args.get("private", False)),
            exclude_pending=bool(args.get("exclude_pending", False)),
            exclude_draft=bool(args.get("exclude_draft", False)),
            mode=args.get("mode"),
            deep=bool(args.get("deep", False)),
            hops=args.get("hops"),
            boost=str(args.get("boost") or "hybrid"),
            alpha=float(args["alpha"]) if args.get("alpha") is not None else 0.15,
            beta=float(args["beta"]) if args.get("beta") is not None else 0.08,
            min_score=int(args.get("min_score") or 0),
            snippet_width=int(args.get("snippet_width") or 160),
            summary_width=int(args.get("summary_width") or 0),
            no_snippet=bool(args.get("no_snippet", False)),
            no_cache=bool(args.get("no_cache", False)),
            with_content=bool(args.get("with_content", False)),
            fields_arg=args.get("fields"),
            link_fields_arg=args.get("link_fields"),
            cache_dir=state_dir / "search_cache",
            top_k=top_k,
        )
        sealed_paths.extend(note_paths)
        eff_s_mode = (str(args.get("mode") or "").strip().lower()) or ("deep" if bool(args.get("deep", False)) else "passage")
        if not hits:
            qa_tag = "[QA_GATE: ZERO_HITS]"
            qa_anchor = "联合检索 0 命中。[QA_GATE: ZERO_HITS] 若确无相关资料，须向人类如实声明「当前知识库中暂未收录相关资料」，严禁凭记忆编造库内事实。"
            qa_orbit = ["调整关键词或改用 --mode locate 排查", "直接向人类声明库内暂无资料"]
        elif eff_s_mode == "locate":
            qa_tag = "[QA_GATE: LOCATE_ONLY]"
            qa_anchor = "定位模式仅返回位置句柄、类型、摘要与更新日期；如需回答内容问题，请改用 --mode passage 或调 note-get --note_refs 读正文。"
            qa_orbit = ["按需改用段落检索: ./gewu atlas --action note-search --all_vaults true --mode passage --query '<关键词>'", "批量实读命中卡正文: ./gewu atlas --action note-get --ref <vlt_01> --note_refs <nte_01,nte_02>"]
        elif eff_s_mode == "deep":
            qa_tag = "[QA_GATE: DEEP_GRAPH]"
            qa_anchor = "深度模式已返回摘要及入链/出链 Top 3（in_links_top3 / out_links_top3）；可顺高分入出链句柄调用 note-get --note_refs 下钻溯源。"
            qa_orbit = ["顺高分入出链实读正文: ./gewu atlas --action note-get --ref <vlt_01> --note_refs <nte_01,nte_02>", "直接向人类汇报图谱脉络"]
        else:
            qa_tag = "[QA_GATE: PASSAGE_READY]"
            qa_anchor = "已返回命中完整段落（passage）与归一化分数（score）。常规问答可直接基于命中段与 nte_XX 句柄作答，无需再读全文；仅当 passage_truncated=true 或需跨卡深度综合时，再按需调 note-get --note_refs 补读。"
            qa_orbit = ["直接基于命中完整段落与 nte_XX 句柄循证作答", "如需完整上下文再补读: ./gewu atlas --action note-get --ref <vlt_01> --note_refs <nte_01,nte_02>"]
        cap = _triad(
            f"已跨 {len(vault_entries)} 个活跃 Vault 完成联合检索（mode={eff_s_mode}），命中 {len(hits)} 条 {qa_tag} [只读完成]",
            qa_orbit,
            qa_anchor,
            terminal=True,
        )
        return (
            0,
            _envelope(
                True,
                "OK_NOTE_SEARCH_FEDERATED",
                cap,
                data={"federated": True, "mode": eff_s_mode, "vault_count": len(vault_entries), "count": len(hits), "hits": hits, "path_sealed": True},
            ),
            sealed_paths,
        )
    if not vault_id_arg and not ref_arg:
        cap = _guard(
            "PARAM_FIX",
            f"E_PARAM_MISSING: {action} 需要 --ref <vlt_01>",
            "请传入托管短句柄 --ref vlt_xx 后重试。",
            "同指纹连续失败 2 次将触发硬熔断。",
            fill_template={"action": action, "ref": "<vlt_01>"},
            attempt_remaining=1,
        )
        return 1, _envelope(False, "E_PARAM_MISSING", cap, missing_slots=["ref"]), sealed_paths

    if action == "vault-get":
        entry = reg.get_vault(vault_id=vault_id_arg, ref=ref_arg)
        disk_raw = inspect_vault_tree(ws_root, entry["rel_path"]) if not entry["deleted"] else {
            "exists_on_disk": False,
            "present_subdirs": 0,
            "expected_subdirs": len(get_vault_subdirs()),
            "obsidian_base_count": 0,
            "obsidian_base_files": [],
        }
        disk_safe = {
            "exists_on_disk": bool(disk_raw["exists_on_disk"]),
            "present_subdirs": int(disk_raw["present_subdirs"]),
            "expected_subdirs": int(disk_raw["expected_subdirs"]),
            "obsidian_base_count": int(disk_raw["obsidian_base_count"]),
            "obsidian_base_files": list(disk_raw["obsidian_base_files"]),
        }
        pub = _public_entry(kek, app_id, entry)
        pub["disk"] = disk_safe
        if not entry["deleted"] and disk_safe["exists_on_disk"]:
            pub["doc_git"] = doc_git_status_summary(state_dir, entry["ref"], resolve_safe_vault_dir(ws_root, entry["rel_path"]))
        cap = _triad(
            f"已查验 Vault 句柄 {entry['ref']}（标题「{entry['title']}」，状态 {entry['status']}）[只读完成]",
            [
                f"更新配置: ./gewu atlas --action vault-update --ref {entry['ref']} --title <新名称>",
                "直接向人类汇报现状",
            ],
            "目录骨架与 .base 视图状态已核验；接口按逻辑句柄解耦物理存储路径，用户本机查看原目录请直接在 Finder / Obsidian 打开。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_VAULT_GET", cap, data=pub), sealed_paths

    if action == "vault-update":
        cur = reg.get_vault(vault_id=vault_id_arg, ref=ref_arg)
        if cur.get("deleted"):
            raise VaultStoreError("E_PARAM_INVALID", "已软删除 Vault 不可更新", exit_code=1)
        new_vault = str(args.get("new_vault", "")).strip() or None
        new_rel = None
        if new_vault:
            new_vault = normalize_vault_id(new_vault)
            if new_vault != cur["vault_id"]:
                new_rel = make_vault_rel_path(new_vault)
                sealed_paths.append(new_rel)
                rename_vault_tree(ws_root, cur["rel_path"], new_rel)

        title_arg = args.get("title")
        new_title = str(title_arg).strip() if title_arg is not None else None
        argv_keys = set(args.get("_provided_keys", args))
        obs_arg = args.get("obsidian_base") if ("obsidian_base" in argv_keys or "obsidian_base" in args and new_vault is None and new_title is None and "default_mode" not in argv_keys) else None
        if "obsidian_base" in argv_keys or "obsidian-base" in argv_keys:
            obs_arg = bool(args.get("obsidian_base"))
        elif not any(k in argv_keys for k in ("obsidian_base",)):
            obs_arg = None
        mode_arg = args.get("default_mode") if "default_mode" in argv_keys else None
        if mode_arg is not None and mode_arg not in ("auto", "supervised"):
            raise VaultStoreError("E_PARAM_INVALID", "--default-mode 仅支持 auto 或 supervised", exit_code=1)

        updated = reg.update_vault(
            vault_id=cur["vault_id"],
            new_vault_id=new_vault,
            new_rel_path=new_rel,
            title=new_title,
            summary=args.get("summary") if "summary" in argv_keys else None,
            obsidian_base=obs_arg,
            default_mode=mode_arg,
        )
        if mode_arg is not None:
            from notes.atlas_note_engine import _atomic_write_md
            from notes.atlas_yamlmini import replace_frontmatter_field
            manifest = resolve_safe_vault_dir(ws_root, updated["rel_path"]) / "20-AI-Zone" / "Manifest-AI-Zone.md"
            if manifest.is_file() and not manifest.is_symlink():
                _atomic_write_md(manifest, replace_frontmatter_field(manifest.read_text(encoding="utf-8"), "default_mode", mode_arg))
        if updated["obsidian_base"]:
            vdir = resolve_safe_vault_dir(ws_root, updated["rel_path"])
            sync_obsidian_bases(vdir)
            sync_obsidian_templates(vdir)

        pub = _public_entry(kek, app_id, updated)
        cap = _triad(
            f"已更新 Vault 句柄 {updated['ref']}（version=v{updated['key_version']}）",
            [
                f"查看更新后详情: ./gewu atlas --action vault-get --ref {updated['ref']}",
                "直接向人类汇报现状",
            ],
            "配置更新与托管槽位重绑已原子落盘，逻辑句柄保持稳定。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_VAULT_UPDATED", cap, data=pub), sealed_paths

    if action == "vault-delete":
        cur = reg.get_vault(vault_id=vault_id_arg, ref=ref_arg)
        purge = bool(args.get("purge", False))
        if purge:
            raise VaultStoreError(
                "E_HALT_VAULT_PURGE_DISABLED",
                "整库硬清除已停用；仅支持保留内容的软删除与恢复，禁止重试。",
                exit_code=2,
            )

        trash_rel = trash_vault_tree(ws_root, cur["rel_path"], cur["vault_id"])
        sealed_paths.append(trash_rel)
        res = reg.delete_vault(vault_id=cur["vault_id"], purge=False, trash_rel_path=trash_rel)
        pub = {
            "ref": res["ref"],
            "status": "deleted",
            "deleted_at": res.get("deleted_at"),
            "path_sealed": True,
        }
        cap = _triad(
            f"已软删除 Vault 句柄 {cur['ref']}（已转入隔离回收槽）",
            [
                f"如需恢复: ./gewu atlas --action vault-restore --ref {cur['ref']}",
                "直接向人类汇报现状",
            ],
            "默认软删除将原库转入运行时托管回收槽，避免误删不可恢复。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_VAULT_DELETED", cap, data=pub), sealed_paths

    if action == "vault-restore":
        cur = reg.get_vault(vault_id=vault_id_arg, ref=ref_arg)
        if not cur.get("deleted"):
            raise VaultStoreError("E_PARAM_CONFLICT", "Vault 当前处于活跃状态，无需恢复", exit_code=1)
        restore_vault_tree(ws_root, cur["rel_path"], cur.get("trash_rel_path"))
        res = reg.restore_vault(vault_id=cur["vault_id"])
        if res.get("obsidian_base"):
            vdir = resolve_safe_vault_dir(ws_root, res["rel_path"])
            sync_obsidian_bases(vdir)
            sync_obsidian_templates(vdir)
        pub = _public_entry(kek, app_id, res)
        cap = _triad(
            f"已从隔离回收槽恢复 Vault 句柄 {pub['ref']}（标题「{pub['title']}」）",
            [
                f"查看知识库状态: ./gewu atlas --action vault-get --ref {pub['ref']}",
                "直接向人类汇报现状",
            ],
            "原库目录骨架已原样迁回并同步 .base 看板，逻辑句柄稳定不变。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_VAULT_RESTORED", cap, data=pub), sealed_paths

    # index-sync
    if action == "index-sync":
        cur = reg.get_vault(vault_id=vault_id_arg, ref=ref_arg)
        if cur.get("deleted"):
            raise VaultStoreError("E_PARAM_INVALID", "已软删除 Vault 不可同步看板", exit_code=1)
        vdir = resolve_safe_vault_dir(ws_root, cur["rel_path"])
        added_layout_dirs = sync_vault_layout_dirs(vdir)
        overwrite_bases = bool(args.get("force", False))
        synced, preserved = sync_obsidian_bases_detailed(vdir, overwrite=overwrite_bases)
        tpl_synced, tpl_preserved = sync_obsidian_templates_detailed(vdir, overwrite=overwrite_bases)
        updated = reg.update_vault(vault_id=cur["vault_id"], obsidian_base=True)
        pub = {
            "ref": updated["ref"],
            "synced": synced,
            "preserved": preserved,
            "preserved_count": len(preserved),
            "templates_synced": tpl_synced,
            "templates_preserved": tpl_preserved,
            "templates_preserved_count": len(tpl_preserved),
            "overwrote_existing": overwrite_bases,
            "added_layout_dirs_count": added_layout_dirs,
            "path_sealed": True,
        }
        preserved_hint = f"，已保护跳过现存人工看板 {len(preserved)} 个" if preserved else ""
        tpl_hint = f"、同步 {len(tpl_synced)} 个 90-System/01-Templates 文档模板"
        cap = _triad(
            f"已为 Vault 句柄 {cur['ref']} 同步 {len(synced)} 个 10-Index 看板文件{preserved_hint}{tpl_hint}并校验骨架（新增目录 {added_layout_dirs} 个）",
            [f"查看 Vault: ./gewu atlas --action vault-get --ref {cur['ref']}", "直接向人类汇报现状"],
            "默认保护人类已在 10-Index/*.base 与 90-System/01-Templates/*.md 修改过的文件不予覆盖（仅在显式 --force true 时重刷模板），同时增量补齐 dicts/layout.yaml 缺失目录。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_INDEX_SYNCED", cap, data=pub), sealed_paths

    # Note lifecycle & search actions scoped to a KEK-resolved Vault
    cur = reg.get_vault(vault_id=vault_id_arg, ref=ref_arg)
    if cur.get("deleted"):
        raise VaultStoreError("E_PARAM_INVALID", "已软删除 Vault 不可执行笔记操作", exit_code=1)
    vdir = resolve_safe_vault_dir(ws_root, cur["rel_path"])
    vref = str(cur["ref"])
    sealed_paths.append(str(cur["rel_path"]))
    note_reg = hot_registries["note_reg"] if (hot_registries and hot_registries.get("note_reg") is not None) else KekNoteRegistry(app_id, state_dir / "atlas-notes.enc", kek)
    proj_reg = hot_registries["proj_reg"] if (hot_registries and hot_registries.get("proj_reg") is not None) else KekProjectRegistry(app_id, state_dir / "atlas-projects.enc", kek)
    project_ref_arg = str(args.get("project_ref") or "").strip() or None
    scope_arg = str(args.get("scope") or "auto").strip() or "auto"
    ticket_id_arg = str(args.get("ticket_id") or "").strip() or None
    token = None
    if plan_urls:
        from pipeline.atlas_fmt_engine import active_preparation, plan_url_preparation
        plan = plan_url_preparation(vdir, vref, proj_reg, args)
        if plan is not None and plan.missing:
            if ticket_id_arg:
                ensure_doc_ticket(ws_root, vdir, task_key=f"auto:{vref}:{action}", title=f"Atlas 内容变更 ({action})",
                                  actor=str(args.get("by") or args.get("agent") or "GewuAtlas"), ticket_id=ticket_id_arg)
            args["ref"] = vref
            return plan
        if plan is not None:
            token = active_preparation.set(plan)
    try:
        return _run_governed_vault_action(
            action=action,
            args=args,
            ws_root=ws_root,
            state_dir=state_dir,
            vdir=vdir,
            vref=vref,
            kek=kek,
            app_id=app_id,
            note_reg=note_reg,
            proj_reg=proj_reg,
            project_ref_arg=project_ref_arg,
            scope_arg=scope_arg,
            ticket_id_arg=ticket_id_arg,
            sealed_paths=sealed_paths,
        )
    finally:
        if token is not None:
            active_preparation.reset(token)


_BATCH_READ_ONLY_ACTIONS = frozenset(
    {
        "note-get",
        "note-list",
        "note-search",
        "note-grep",
        "project-brief",
        "health-check",
        "schema-check",
        "vfs-links",
    }
)


def _extract_batch_steps_safe(args: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    raw_steps = args.get("steps")
    if raw_steps is None and args.get("batch_json") is not None:
        bj = args.get("batch_json")
        try:
            parsed_bj = json.loads(str(bj)) if isinstance(bj, str) else bj
        except Exception:
            return None
        raw_steps = parsed_bj.get("steps") if isinstance(parsed_bj, dict) else parsed_bj
    elif isinstance(raw_steps, str):
        try:
            raw_steps = json.loads(raw_steps)
        except Exception:
            return None
    if isinstance(raw_steps, list) and raw_steps and all(isinstance(s, dict) for s in raw_steps):
        return raw_steps
    return None


def _is_read_only_batch(args: Dict[str, Any]) -> bool:
    steps = _extract_batch_steps_safe(args)
    if not steps:
        return False
    for st in steps:
        s_act = str(st.get("action") or "").strip()
        if s_act in _BATCH_READ_ONLY_ACTIONS:
            continue
        if s_act == "note-patch" and isinstance(st.get("args"), dict) and bool(st["args"].get("dry_run", False)):
            continue
        return False
    return True


def _is_in_place_batch_only(args: Dict[str, Any]) -> bool:
    steps = _extract_batch_steps_safe(args)
    if not steps:
        return False
    allowed = _BATCH_READ_ONLY_ACTIONS | {"note-patch"}
    return all(str(st.get("action") or "").strip() in allowed for st in steps)


def _is_non_mutating_vault_op(action: str, args: Dict[str, Any]) -> bool:
    if action == "batch" and _is_read_only_batch(args):
        return True
    if action == "note-patch" and bool(args.get("dry_run", False)):
        return True
    if action == "atomic-extract" and str(args.get("mode") or "extract") in ("list-pending", "unextracted"):
        return True
    if action in ("fmt-window", "fmt-assets"):
        return True
    if action in READ_ONLY_ACTIONS:
        return True
    if action == "clean-attachments" and not bool(args.get("apply", False)):
        return True
    if action == "vfs-untrash" and not str(args.get("trash_ref") or "").strip():
        return True
    if action == "project-untrash" and not str(args.get("trash_ref") or "").strip():
        return True
    if action == "project-undo" and bool(args.get("dry_run", False)):
        return True
    if action == "workbench-preset" and not str(args.get("preset") or "").strip():
        return True
    if action == "workbench-workspace" and str(args.get("mode") or "allocate").strip().lower() == "list":
        return True
    if action in ("atomic-extract", "note-slice", "note-format", "journal-archive", "trash-purge", "retention-sweep", "workbench-workspace") and bool(args.get("dry_run", False)):
        return True
    if action == "note-link" and str(args.get("mode") or "").strip().lower() == "fix" and bool(args.get("dry_run", False)):
        return True
    if action == "moc-sync" and (bool(args.get("check", False)) or bool(args.get("dry_run", False)) or str(args.get("mode") or "").strip().lower() == "roam"):
        return True
    if action == "atomic-approve" and str(args.get("mode") or "").strip().lower() in ("list-pending", "discover"):
        return True
    if action == "fmt" and (bool(args.get("dry_run", False)) or bool(args.get("check", False))):
        return True
    return False


def _attach_data_fields(env: Dict[str, Any], updates: Dict[str, Any]) -> None:
    if not updates:
        return
    data = env.get("data")
    if isinstance(data, dict):
        data.update(updates)
    sc = env.get("structuredContent")
    if isinstance(sc, dict) and isinstance(sc.get("data"), dict):
        sc["data"].update(updates)


def _blocking_broken_links(link_res: Dict[str, Any], note_reg: KekNoteRegistry, vref: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Filter out historical 05-Journal audit timeline breadcrumbs so they do not block DOC human-sync."""
    journal_refs = {
        nref
        for rel, nref in note_reg.list_vault_handles(vref).items()
        if rel.startswith("05-Journal/")
    }
    blocking = [
        d for d in (link_res.get("broken") or []) if str(d.get("source_note_ref") or "") not in journal_refs
    ]
    ref_notes = sorted({str(d.get("source_note_ref") or "") for d in blocking if d.get("source_note_ref")})
    return blocking, ref_notes



_SPRAWL_DIR_RE = re.compile(r"^(?:reports|sources|99-assets|drafts|output|[a-z0-9_-]+-research)$", re.I)


def _scan_workspace_sprawl(ws_root: Path) -> List[str]:
    """Return unauthorized research/report directories created directly at workspace root."""
    sprawl: List[str] = []
    try:
        if not ws_root.is_dir():
            return []
        for child in sorted(ws_root.iterdir(), key=lambda p: p.name):
            name = child.name
            if name.startswith("."):
                continue
            if child.is_dir() and _SPRAWL_DIR_RE.match(name):
                sprawl.append(name)
    except OSError:
        return []
    return sprawl


def _is_workbench_rel(rel: str) -> bool:
    norm = str(rel or "").strip().strip("/")
    return "/30-Workbench/" in f"/{norm}/"


def _count_dirty_dangling_links(
    *,
    vdir: Path,
    vref: str,
    note_reg: KekNoteRegistry,
    dirty_rels: List[str],
) -> int:
    from notes.atlas_wikilink_tools import split_wikilink_inner
    from notes.atlas_yamlmini import extract_frontmatter, extract_wikilinks, resolve_wikilink

    fm_ref_fields = ("source", "sources", "toc", "slices", "supersedes", "superseded_by", "source_file", "extracted_to", "map", "moc")
    handle_map = note_reg.list_vault_handles(vref)
    dangling = 0
    for rel in dirty_rels:
        if not rel or rel.startswith("05-Journal/"):
            continue
        fp = vdir / rel
        if not fp.is_file() or fp.is_symlink():
            continue
        try:
            txt = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta, body, has_fm = extract_frontmatter(txt)
        targets = list(extract_wikilinks(body))
        if has_fm and isinstance(meta, dict):
            for fk in fm_ref_fields:
                fval = meta.get(fk)
                items = [fval] if isinstance(fval, str) else (fval if isinstance(fval, list) else [])
                for item in items:
                    if not isinstance(item, str):
                        continue
                    s = item.strip()
                    if s.startswith("[[") and s.endswith("]]"):
                        t_inner, _, _ = split_wikilink_inner(s[2:-2])
                        if t_inner:
                            targets.append(t_inner)
                    elif s:
                        targets.append(s)
        for target in targets:
            t_clean = str(target or "").strip()
            if not t_clean:
                continue
            # Ignore external relative paths or attachment file links if present on disk
            if t_clean.startswith(("./", "../")):
                continue
            if not resolve_wikilink(t_clean, rel, handle_map):
                # Check attachments folder for embedded image/file wikilinks
                att_hits = list((vdir / "99-Assets").rglob(Path(t_clean).name)) if (vdir / "99-Assets").is_dir() else []
                if not att_hits:
                    dangling += 1
    return dangling


def _piggyback_delivery_gate(
    *,
    ws_root: Path,
    vdir: Path,
    vref: str,
    note_reg: KekNoteRegistry,
    proj_reg: Optional[KekProjectRegistry] = None,
    project_ref: Optional[str] = None,
    actor: str = "ai",
    action: str,
    env: Dict[str, Any],
    kek: Optional[bytes] = None,
    app_id: Optional[str] = None,
    cache_dir: Optional[Path] = None,
    batch_key: str = "",
    dirty_only: bool = False,
    dirty_rels: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Piggyback lightweight boundary & link check onto mutating operator responses.

    Manifest delivery history is appended ONLY on formal deliverable events (`artifact-adopt` to artifacts,
    `artifact-finalize`), never spamming noisy entries for intermediate drafts/edits.
    For intermediate `zone=workbench` drafts (`dirty_only=True`), validates only workspace root sprawl and
    the touched draft's own wikilinks in O(1); full-vault 60/60 check runs on formal transitions & `guard`.
    """
    sprawl = _scan_workspace_sprawl(ws_root)
    gate_scope = "dirty_only" if (dirty_only and dirty_rels) else "full_vault"
    if gate_scope == "dirty_only":
        dangling_cnt = _count_dirty_dangling_links(
            vdir=vdir,
            vref=vref,
            note_reg=note_reg,
            dirty_rels=list(dirty_rels or []),
        )
    else:
        link_res = engine_vfs_links(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            cache_dir=cache_dir,
        )
        dangling_cnt = int(link_res.get("dangling_count", 0))
    boundary_ok = len(sprawl) == 0
    links_ok = dangling_cnt == 0
    score = 60
    issues: List[str] = []
    if not boundary_ok:
        score -= 15
        issues.append(f"工作区根目录存在未授权自建目录: {', '.join(sprawl)}（须移入工作台或清理）")
    if not links_ok:
        score -= 15
        issues.append(f"存在 {dangling_cnt} 处悬空双链（可用 note-link --mode fix 或 note-update 修复）")

    data_dict = env.get("data") if isinstance(env.get("data"), dict) else {}
    cand_refs: List[str] = []
    for key_r in ("note_ref", "moc_ref"):
        val_r = str(data_dict.get(key_r) or "").strip()
        if val_r and val_r not in cand_refs:
            cand_refs.append(val_r)
    ext_list = data_dict.get("extracted")
    for item_ext in (ext_list if isinstance(ext_list, list) else []):
        if isinstance(item_ext, dict):
            er = str(item_ext.get("note_ref") or "").strip()
            if er and er not in cand_refs and str(item_ext.get("status") or "") == "active":
                cand_refs.append(er)
    app_list = data_dict.get("approved_refs")
    for app_r in (app_list if isinstance(app_list, list) else []):
        ar = str(app_r).strip()
        if ar and ar not in cand_refs:
            cand_refs.append(ar)

    deliv_rels: List[str] = []
    eff_proj_rel = ""
    eff_proj_ref = project_ref or ""

    is_formal_delivery = (
        action == "artifact-finalize"
        or (action == "artifact-adopt" and str(data_dict.get("zone") or "").lower() == "artifacts")
        or (action == "moc-sync" and not bool(data_dict.get("dry_run", False)) and str(data_dict.get("mode") or "").strip().lower() != "roam")
        or (action == "atomic-approve")
        or (action == "atomic-extract" and str(data_dict.get("review_mode") or "") != "supervised" and len(cand_refs) > 0)
        or (action == "note-promote" and str(data_dict.get("zone") or "").lower() in ("atomics", "moc"))
    )

    for cref in cand_refs:
        try:
            nrec = note_reg.resolve_handle(vref, cref)
            nrel = str(nrec.get("note_rel_path") or "")
            if nrel:
                if is_formal_delivery and ("/10-Artifacts/" in nrel or "/23-MOC/" in nrel or "/22-Atomics/" in nrel):
                    deliv_rels.append(nrel)
                if not eff_proj_rel and nrel.startswith("30-Projects/"):
                    parts = nrel.split("/")
                    if len(parts) >= 2:
                        eff_proj_rel = f"30-Projects/{parts[1]}"
        except Exception:
            pass

    if not eff_proj_rel and eff_proj_ref and proj_reg is not None:
        try:
            prec = proj_reg.resolve_handle(vref, eff_proj_ref)
            eff_proj_rel = str(prec.get("project_rel_path") or "")
        except Exception:
            eff_proj_rel = ""

    if is_formal_delivery and not deliv_rels:
        is_formal_delivery = False

    manifest_checked = False
    manifest_ok = True
    deliverable_links: List[str] = []
    if eff_proj_rel:
        try:
            pdir = vdir / eff_proj_rel
            if pdir.is_dir():
                if not eff_proj_ref and proj_reg is not None:
                    proj_root = vdir / "30-Projects"
                    all_proj_rels = sorted(
                        f"30-Projects/{d.name}"
                        for d in proj_root.iterdir()
                        if d.is_dir() and not d.is_symlink() and not d.name.startswith(".")
                    ) if proj_root.is_dir() else [eff_proj_rel]
                    pmap = proj_reg.sync_projects(vref, all_proj_rels)
                    eff_proj_ref = pmap.get(eff_proj_rel, pdir.name)
                mres = sync_and_check_project_manifest(
                    vault_dir=vdir,
                    vault_ref=vref,
                    proj_dir=pdir,
                    project_ref=eff_proj_ref or pdir.name,
                    note_reg=note_reg,
                    agent_name=actor,
                    action_name=action,
                    gate_score=score,
                    gate_passed=(score == 60),
                    gate_issues=issues,
                    deliverable_rels=deliv_rels or None,
                    append_history=is_formal_delivery,
                    batch_key=batch_key,
                    fast_check=(not is_formal_delivery),
                )
                manifest_checked = True
                deliverable_links = list(mres.get("deliverable_links") or [])
                manifest_batch_id = str(mres.get("batch_id") or "")
                manifest_batch_state = str(mres.get("batch_state") or "")
                if not mres.get("ok"):
                    manifest_ok = False
                    score = max(0, score - 15)
                    issues.append(str(mres.get("issue") or "课题 Manifest 校验失败"))
                elif mres.get("manifest_updated"):
                    all_md_now = [
                        p.relative_to(vdir).as_posix()
                        for p in vdir.rglob("*.md")
                        if p.is_file() and not p.is_symlink() and not any(part.startswith(".") for part in p.relative_to(vdir).parts)
                    ]
                    note_reg.sync_paths(vref, sorted(all_md_now))
        except Exception:
            pass
    passed = (score == 60) and manifest_ok
    gate_summary: Dict[str, Any] = {
        "scope": gate_scope,
        "operator_score": score,
        "operator_max": 60,
        "operator_passed": passed,
        "boundary_clean": boundary_ok,
        "manifest_checked": manifest_checked,
        "manifest_ok": manifest_ok,
        "manifest_history_appended": is_formal_delivery and manifest_checked,
        "manifest_batch_id": locals().get("manifest_batch_id", ""),
        "manifest_batch_state": locals().get("manifest_batch_state", ""),
        "deliverable_links": deliverable_links,
        "sprawl_items": sprawl,
        "dangling_link_count": dangling_cnt,
        "issues": issues,
        "eval_routing": "subagent_if_available_else_self_eval",
        "rubric_40": {
            "completeness": 15,
            "groundedness": 15,
            "readability_and_zero_path": 10,
        },
    }
    cap = env.get("structuredContent", {}).get("capsule") if isinstance(env.get("structuredContent"), dict) else None
    if isinstance(cap, dict):
        old_anchor = str(cap.get("anchor") or "")
        orbit_list = list(cap.get("orbit_options") or [])
        if not passed:
            cap["terminal"] = False
            fix_cmd = f"清理越界目录或修复断链后复核: ./gewu atlas --action guard --ref {vref} --mode delivery"
            if fix_cmd not in orbit_list:
                orbit_list.insert(0, fix_cmd)
            cap["orbit_options"] = orbit_list
            cap["anchor"] = f"🚫 [DELIVERY_GATE: BLOCKED ({score}/60)] {'; '.join(issues)} | {old_anchor}"
        else:
            cap["anchor"] = (
                f"{old_anchor} [DELIVERY_GATE: PASS (60/60) | "
                "L1单步直交；L2/L3收尾若有Subagent能力优先调Subagent盲审，否则按完整性/真实性/可读性自评交付]"
            )
        env["reminder"] = (
            "🧭 长牵引绳胶囊 (Triad Capsule)\n"
            f"📍 当前坐标 (STATE): {cap.get('state', '')}\n"
            f"🚀 可选推进 (ORBIT): {' | '.join(cap.get('orbit_options') or [])}\n"
            f"⚓ 物理锚点 (ANCHOR): {cap['anchor']}"
        )
        cap["rendered_text"] = env["reminder"]
    return gate_summary


def _evaluate_delivery_gate(
    *,
    ws_root: Path,
    state_dir: Path,
    vdir: Path,
    vref: str,
    note_reg: KekNoteRegistry,
    proj_reg: KekProjectRegistry,
    kek: Optional[bytes] = None,
    project_ref: Optional[str] = None,
    note_ref_arg: Optional[str] = None,
    note_refs_arg: Any = None,
    actor: str = 'ai',
    batch_key: str = '',
    session_fp: str = '',
) -> Tuple[Dict[str, Any], List[str]]:
    """Full 60-point operator hard gate + 40-point Subagent/Self-eval rubric protocol."""
    sealed: List[str] = []
    # 1. Spatial boundary (15 pts)
    sprawl = _scan_workspace_sprawl(ws_root)
    vfs_res = engine_vfs_validate(vault_dir=vdir, vault_ref=vref)
    vfs_violations = list(vfs_res.get("violations") or [])
    spatial_issues: List[str] = []
    if sprawl:
        spatial_issues.append(f"工作区根目录存在违规自建目录: {', '.join(sprawl)}")
    if vfs_violations:
        spatial_issues.append(f"VFS 拓扑告警 {len(vfs_violations)} 处")
    spatial_score = 15 if not spatial_issues else (5 if not sprawl else 0)

    # 2. Schema & Metadata integrity (15 pts)
    target_single = (note_ref_arg or "").strip() or None
    sc_report, sc_paths = engine_schema_check(
        vault_dir=vdir,
        vault_ref=vref,
        note_reg=note_reg,
        note_ref=target_single,
        strict=True,
    )
    sealed.extend(sc_paths)
    schema_err_cnt = (
        int(sc_report.get("schema_issue_count", 0))
        + int(sc_report.get("unknown_field_issue_count", 0))
        + int(sc_report.get("zone_mismatch_issue_count", 0))
    )
    schema_issues: List[str] = []
    if schema_err_cnt > 0:
        schema_issues.append(f"YAML Schema/未知字段/分区错配共 {schema_err_cnt} 处")
    schema_score = 15 if schema_err_cnt == 0 else 0

    # 3. Truth & Link integrity (15 pts)
    req_refs: List[str] = []
    if target_single:
        req_refs.append(target_single)
    if note_refs_arg:
        for part in str(note_refs_arg).split(","):
            p = part.strip()
            if p and p not in req_refs:
                req_refs.append(p)
    handle_map = note_reg.list_vault_handles(vref)
    rel_by_handle = {h: rel for rel, h in handle_map.items()}
    session_reads = load_session_reads(state_dir, session_fp, vref) if session_fp else set()
    unverified_refs: List[str] = []
    verified_notes: List[Dict[str, str]] = []
    unclosed_mermaid_refs: List[str] = []
    copy_paste_suspects: List[str] = []
    unread_cited_refs: List[str] = []
    for rref in req_refs:
        rel_p = rel_by_handle.get(rref)
        if not rel_p or not (vdir / rel_p).is_file():
            unverified_refs.append(rref)
        else:
            sealed.append(rel_p)
            raw_md = (vdir / rel_p).read_text(encoding="utf-8", errors="replace")
            from notes.atlas_yamlmini import extract_frontmatter
            fm, body, _ = extract_frontmatter(raw_md)
            verified_notes.append({
                "note_ref": rref,
                "title": str(fm.get("title") or ""),
                "doc_type": str(fm.get("doc_type") or ""),
                "status": str(fm.get("status") or ""),
            })
            fences = re.findall(r"^```(\w*)", body, flags=re.M)
            in_mermaid = False
            for ftag in fences:
                if not in_mermaid and ftag.lower() == "mermaid":
                    in_mermaid = True
                elif in_mermaid and ftag == "":
                    in_mermaid = False
            if in_mermaid:
                unclosed_mermaid_refs.append(rref)
            cp_issues, unread_refs = audit_note_truth_guards(
                vdir=vdir,
                note_ref=rref,
                fm=fm,
                body=body,
                handle_map=handle_map,
                session_reads=session_reads,
            )
            copy_paste_suspects.extend(cp_issues)
            for ur in unread_refs:
                if ur not in unread_cited_refs:
                    unread_cited_refs.append(ur)

    eff_kek = kek if kek is not None else load_or_init_kek(state_dir / "atlas-vaults.kek")
    link_res = engine_vfs_links(vault_dir=vdir, vault_ref=vref, note_reg=note_reg, note_ref=target_single, kek=eff_kek, app_id=note_reg.app_id, cache_dir=state_dir / "notes_index")
    dangling_cnt = int(link_res.get("dangling_count", 0))
    truth_issues: List[str] = []
    if unverified_refs:
        truth_issues.append(f"存在未核验/虚构的笔记句柄: {', '.join(unverified_refs)}")
    if dangling_cnt > 0:
        truth_issues.append(f"存在 {dangling_cnt} 处悬空双链")
    if unclosed_mermaid_refs:
        truth_issues.append(f"笔记 {', '.join(unclosed_mermaid_refs)} 存在未闭合的 mermaid 代码块")
    if copy_paste_suspects:
        truth_issues.extend(copy_paste_suspects)
    truth_score = 15 if not truth_issues else 0

    # 4. Governance & Sovereign Guard (15 pts)
    doc_git = doc_git_status_summary(state_dir, vref, vdir, note_reg=note_reg)
    gov_issues: List[str] = []
    if int(doc_git.get("dirty_count", 0)) > 0 and dangling_cnt > 0:
        gov_issues.append("检测到未提交修改且伴随悬空断链，请先清理断链再收口")
    gov_score = 15 if not gov_issues else 5

    # Check and update project Manifest(s)
    manifest_reports: List[Dict[str, Any]] = []
    prelim_score = spatial_score + schema_score + truth_score + gov_score
    prelim_issues = spatial_issues + schema_issues + truth_issues + gov_issues
    v_ref_ids = [vn["note_ref"] for vn in verified_notes]
    if project_ref:
        try:
            prec = proj_reg.resolve_handle(vref, project_ref)
            pdir = vdir / str(prec["project_rel_path"])
            p_rel_prefix = str(prec["project_rel_path"]).rstrip("/") + "/"
            p_deliv_rels = [
                rel_by_handle[r]
                for r in v_ref_ids
                if rel_by_handle.get(r, "").startswith(p_rel_prefix)
                and any(z in rel_by_handle.get(r, "") for z in ("/10-Artifacts/", "/23-MOC/", "/22-Atomics/"))
            ]
            m_rep = sync_and_check_project_manifest(
                vault_dir=vdir,
                vault_ref=vref,
                proj_dir=pdir,
                project_ref=project_ref,
                note_reg=note_reg,
                agent_name=actor,
                action_name="delivery-check",
                gate_score=prelim_score,
                gate_passed=(prelim_score == 60 and len(prelim_issues) == 0),
                gate_issues=prelim_issues,
                verified_refs=v_ref_ids,
                deliverable_rels=p_deliv_rels or None,
                append_history=True,
                batch_key=batch_key,
            )
            manifest_reports.append(m_rep)
            if not m_rep.get("ok"):
                spatial_issues.append(str(m_rep.get("issue") or "课题 Manifest 异常"))
                spatial_score = max(0, spatial_score - 10)
        except Exception as exc:
            spatial_issues.append(f"课题 {project_ref} Manifest 检查失败: {getattr(exc, 'message', str(exc))}")
            spatial_score = max(0, spatial_score - 10)
    else:
        proj_root = vdir / "30-Projects"
        if proj_root.is_dir():
            p_rels = [f'30-Projects/{p.name}' for p in sorted(proj_root.iterdir(), key=lambda x: x.name) if p.is_dir() and not p.name.startswith('.')]
            pmap = proj_reg.sync_projects(vref, p_rels)
            pid_to_handles: Dict[str, List[str]] = {}
            for p_sub in sorted(proj_root.iterdir(), key=lambda p: p.name):
                if p_sub.is_dir() and not p_sub.name.startswith("."):
                    rel_key = f"30-Projects/{p_sub.name}"
                    p_handle = pmap.get(rel_key, p_sub.name)
                    p_rel_prefix = rel_key.rstrip("/") + "/"
                    p_deliv_rels = [
                        rel_by_handle[r]
                        for r in v_ref_ids
                        if rel_by_handle.get(r, "").startswith(p_rel_prefix)
                        and any(z in rel_by_handle.get(r, "") for z in ("/10-Artifacts/", "/23-MOC/", "/22-Atomics/"))
                    ]
                    m_rep = sync_and_check_project_manifest(
                        vault_dir=vdir,
                        vault_ref=vref,
                        proj_dir=p_sub,
                        project_ref=p_handle,
                        note_reg=note_reg,
                        agent_name=actor,
                        action_name="delivery-check",
                        gate_score=prelim_score,
                        gate_passed=(prelim_score == 60 and len(prelim_issues) == 0),
                        gate_issues=prelim_issues,
                        verified_refs=v_ref_ids,
                        deliverable_rels=p_deliv_rels or None,
                        append_history=True,
                        batch_key=batch_key,
                    )
                    manifest_reports.append(m_rep)
                    rep_pid = str(m_rep.get("project_id") or p_sub.name).strip().lower()
                    if rep_pid:
                        pid_to_handles.setdefault(rep_pid, []).append(p_handle)
                    if not m_rep.get("ok"):
                        spatial_issues.append(str(m_rep.get("issue") or "课题 Manifest 异常"))
                        spatial_score = max(0, spatial_score - 10)
            for dup_pid, dup_handles in sorted(pid_to_handles.items()):
                if len(dup_handles) > 1:
                    spatial_issues.append(
                        f"检测到重复课题标识 project_id={dup_pid}（句柄: {', '.join(dup_handles)}），请迁移内容后执行 project-trash 清理冗余课题"
                    )
                    spatial_score = max(0, spatial_score - 10)
    # Re-sync note registry if any Manifest was updated
    if any(m.get("manifest_updated") for m in manifest_reports):
        all_md_now = [
            p.relative_to(vdir).as_posix()
            for p in vdir.rglob("*.md")
            if p.is_file() and not p.is_symlink() and not any(part.startswith(".") for part in p.relative_to(vdir).parts)
        ]
        note_reg.sync_paths(vref, sorted(all_md_now))

    total_score = spatial_score + schema_score + truth_score + gov_score
    all_issues = spatial_issues + schema_issues + truth_issues + gov_issues
    passed = total_score == 60 and len(all_issues) == 0

    report = {
        "vault_ref": vref,
        "project_ref": project_ref or "",
        "operator_score": total_score,
        "operator_max": 60,
        "pass_line": 60,
        "operator_passed": passed,
        "dimensions": {
            "spatial_boundary": {"score": spatial_score, "max": 15, "ok": not spatial_issues, "issues": spatial_issues, "sprawl_items": sprawl, "manifests_checked": len(manifest_reports), "manifest_reports": manifest_reports},
            "schema_integrity": {"score": schema_score, "max": 15, "ok": not schema_issues, "issues": schema_issues, "checked_notes": sc_report.get("checked_notes", 0)},
            "truth_and_links": {
                "score": truth_score,
                "max": 15,
                "ok": not truth_issues,
                "issues": truth_issues,
                "verified_notes": verified_notes,
                "unverified_refs": unverified_refs,
                "dangling_count": dangling_cnt,
                "copy_paste_suspects": copy_paste_suspects,
                "session_read_refs_count": len(session_reads),
                "unread_cited_refs": unread_cited_refs,
            },
            "governance_guard": {"score": gov_score, "max": 15, "ok": not gov_issues, "issues": gov_issues},
        },
        "issues": all_issues,
        "semantic_evaluation": {
            "max_score": 40,
            "review_status": "pending",
            "required_for_l3": True,
            "routing_policy": "subagent_if_available_else_self_eval",
            "when_to_run": "L3 复合研究/多卡变更必跑；L2 问答或单卡创作脑内快速核对；L1 单步原子操作免跑",
            "rubric": [
                {"id": "completeness", "name": "完整性 (Completeness)", "points": 15, "criteria": "对照用户原始请求，核心诉求是否全部落实（无漏项、非半成品列表拼贴）"},
                {"id": "groundedness", "name": "真实性 (Groundedness)", "points": 15, "criteria": "核心结论是否句句绑定已实读/核验的 nte_XX 句柄且区分真值状态；未查到资料是否如实声明缺口"},
                {"id": "readability", "name": "可读性与脱敏 (Readability & Zero-Path)", "points": 10, "criteria": "散文流畅、结构清晰（结论→界定→机理→边界），且全文零本机物理路径泄漏"},
            ],
        },
        "path_sealed": True,
    }
    review = report["semantic_evaluation"]
    policy = (APP_DIR / "rules" / "rule-delivery-review.md").read_text(encoding="utf-8")
    review["review_prompt"] = "依据原始任务、待交付正文、引用来源与硬评回执完成以下审阅。\n" + policy
    review["subagent_blind_review_prompt"] = review["review_prompt"]
    review["reviewed_refs"] = []
    review["unreviewed_refs"] = [
        note["note_ref"] for note in verified_notes
        if note["doc_type"].lower() in {"atomic", "moc", "artifact"}
    ]
    review["item_review_template"] = {
        "note_ref": None, "doc_type": None, "scores": dict.fromkeys(
            ("completeness", "groundedness", "readability")
        ), "verdict": None, "passage": None, "reason": None, "revision": None,
    }
    review["chain_review_template"] = {
        "claim_location": None, "source_locations": [], "finding": None,
        "effect_on_verdict": None,
    }
    review["unresolved_refs"] = unverified_refs
    return report, sealed


def _reconcile_registries_after_remote_pull(
    *,
    vdir: Path,
    vref: str,
    state_dir: Path,
    note_reg: KekNoteRegistry,
    proj_reg: KekProjectRegistry,
    kek: bytes,
    app_id: str,
) -> Dict[str, int]:
    """Refresh KEK project & note registries after remote commits update Vault files."""
    p_root = vdir / "30-Projects"
    proj_rels = (
        [
            f"30-Projects/{c.name}"
            for c in sorted(p_root.iterdir())
            if not c.is_symlink() and c.is_dir() and not c.name.startswith(".")
        ]
        if p_root.is_dir()
        else []
    )
    pmap = proj_reg.sync_projects(vref, proj_rels)
    _, n_map = reconcile_note_index(
        vault_dir=vdir,
        vault_ref=vref,
        note_reg=note_reg,
        kek=kek,
        app_id=app_id,
        cache_dir=state_dir / "notes_index",
    )
    return {"projects_indexed": len(pmap), "notes_indexed": len(n_map)}


def _run_governed_vault_action(
    *,
    action: str,
    args: Dict[str, Any],
    ws_root: Path,
    state_dir: Path,
    vdir: Path,
    vref: str,
    kek: bytes,
    app_id: str,
    note_reg: KekNoteRegistry,
    proj_reg: KekProjectRegistry,
    project_ref_arg: Optional[str],
    scope_arg: str,
    ticket_id_arg: Optional[str],
    sealed_paths: List[str],
) -> Tuple[int, Dict[str, Any], List[str]]:
    if action == "doc-remote":
        provided = set(args.get("_provided_keys", args))
        clear_flag = bool(args.get("clear", False))
        remote_url_arg = str(args.get("remote_url")).strip() if "remote_url" in provided and args.get("remote_url") is not None else None
        branch_arg = str(args.get("branch")).strip() if "branch" in provided and args.get("branch") is not None else None
        auto_push_arg = bool(args.get("auto_push")) if "auto_push" in provided else None
        git_layout_arg = str(args.get("git_layout")).strip().lower() if "git_layout" in provided and args.get("git_layout") is not None else None
        fetch_flag = bool(args.get("fetch", False))
        if clear_flag or remote_url_arg is not None or branch_arg is not None or auto_push_arg is not None or git_layout_arg is not None:
            res = configure_vault_doc_remote(
                state_dir=state_dir,
                vault_ref=vref,
                vault_dir=vdir,
                remote_url=remote_url_arg,
                branch=branch_arg,
                auto_push=auto_push_arg,
                git_layout=git_layout_arg,
                clear=clear_flag,
            )
            if fetch_flag and res.get("configured"):
                res.update(get_vault_doc_remote(state_dir, vref, vdir, fetch=True))
        else:
            res = get_vault_doc_remote(state_dir, vref, vdir, fetch=fetch_flag)
            res["included_scopes"] = ["dropzone", "journal"]
            res["ignored_scopes"] = ["obsidian", "archives", "source", "trash"]
            res["path_sealed"] = True
        res["vault_ref"] = vref
        if res.get("configured"):
            st_msg = (
                f"Vault {vref} DOC 远端同步配置已就绪（branch={res.get('branch')}，"
                f"auto_push={res.get('auto_push')}，ahead={res.get('ahead', 0)}/behind={res.get('behind', 0)}）"
            )
            orbit = [
                f"执行 DOC 与远端双向同步: ./gewu atlas --action doc-sync --ref {vref}",
                f"查看 DOC 与远端状态: ./gewu atlas --action doc-status --ref {vref} --fetch true",
                "直接向人类汇报现状",
            ]
        else:
            st_msg = f"Vault {vref} DOC 远端尚未绑定（仅本地 detached DOC Git 追踪）"
            orbit = [
                f"绑定 GitHub 远端: ./gewu atlas --action doc-remote --ref {vref} --remote_url https://github.com/<owner>/<repo>.git --branch main --auto_push true",
                "直接向人类汇报现状",
            ]
        cap = _triad(
            st_msg,
            orbit,
            "DOC 轨与 GitHub 同步策略已统一：包含 00-DropZone 与 05-Journal，排除 .obsidian、source、归档与回收区，认证优先复用 gh auth git-credential，零明文 Token 与零物理路径。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_DOC_REMOTE", cap, data=res), sealed_paths

    if action == "doc-commit":
        commit_mode = str(args.get("mode") or "standard").strip() or "standard"
        if commit_mode == "doc-commit":
            commit_mode = "standard"
        if commit_mode not in ("standard", "human-sync"):
            raise VaultStoreError("E_PARAM_INVALID", "doc-commit 仅支持 --mode standard|human-sync", exit_code=1)
        actor = str(args.get("by") or args.get("agent") or "Agent").strip() or "Agent"
        force_commit = bool(args.get("force", False))
        provided = set(args.get("_provided_keys", args))
        push_arg: Optional[bool] = bool(args.get("push")) if "push" in provided else None
        pre_state = inspect_vault_doc_git_state(state_dir, vref, vdir, note_reg=note_reg)
        if commit_mode == "human-sync" and pre_state.get("dirty_count", 0) > 0 and not force_commit:
            link_res = engine_vfs_links(vault_dir=vdir, vault_ref=vref, note_reg=note_reg, kek=kek, app_id=app_id, cache_dir=state_dir / "notes_index")
            blocking_broken, ref_notes = _blocking_broken_links(link_res, note_reg, vref)
            dangling_cnt = len(blocking_broken)
            if dangling_cnt > 0:
                cap = _guard(
                    "INPUT_REQUIRED",
                    f"E_DOC_HUMAN_DELETE_DANGLING_LINKS: 检测到人类手工删除/改名并引发 {dangling_cnt} 处悬空断链，已按 116 状态 2 暂停提交收口",
                    (
                        "严禁恢复或重建已被人类删除的文件！请先使用 note-update / note-deprecate 清理受影响引用方卡片"
                        f"（{', '.join(ref_notes[:10]) or '见 vfs-links'}）中的失效双链后再执行 doc-commit --mode human-sync；"
                        "若人类确认按现状强行提交，请加传 --force true。"
                    ),
                    "GUARD: INPUT_REQUIRED | 保护人类删除操作 · 禁止还原已删文件",
                )
                guard_data = {
                    "vault_ref": vref,
                    "commit_mode": commit_mode,
                    "requires_approval": True,
                    "anti_restore_policy": "never_restore_human_deleted_or_edited_files",
                    "dirty_count": pre_state.get("dirty_count", 0),
                    "added_count": pre_state.get("added_count", 0),
                    "modified_count": pre_state.get("modified_count", 0),
                    "deleted_count": pre_state.get("deleted_count", 0),
                    "renamed_count": pre_state.get("renamed_count", 0),
                    "dangling_count": dangling_cnt,
                    "referencing_note_refs": ref_notes[:20],
                    "path_sealed": True,
                }
                return 1, _envelope(False, "E_DOC_HUMAN_DELETE_DANGLING_LINKS", cap, data=guard_data), sealed_paths
        res = run_vault_doc_commit(
            state_dir=state_dir,
            vault_ref=vref,
            vault_dir=vdir,
            agent=actor,
            ticket_id=ticket_id_arg or "",
            reason=str(args.get("reason") or ("reconcile human manual edits and deletions" if commit_mode == "human-sync" else "manual DOC track checkpoint")),
            phase="post",
            commit_mode=commit_mode,
            push=push_arg,
        )
        remote_sync = res.get("remote_sync") if isinstance(res.get("remote_sync"), dict) else {}
        if remote_sync.get("head_changed"):
            res["reconciled_index"] = _reconcile_registries_after_remote_pull(
                vdir=vdir, vref=vref, state_dir=state_dir, note_reg=note_reg, proj_reg=proj_reg, kek=kek, app_id=app_id
            )
        post_status = doc_git_status_summary(state_dir, vref, vdir, note_reg=note_reg)
        res["vault_ref"] = vref
        res["clean_tree"] = bool(post_status.get("clean"))
        res["dirty_count_after"] = int(post_status.get("dirty_count", 0))
        res["path_sealed"] = True
        head_s = res.get("commit_short") or res.get("head_short") or "clean"
        rs_status = str(remote_sync.get("status") or "skipped_unconfigured")
        rs_hint = ""
        if remote_sync.get("configured"):
            rs_hint = f"，远端同步={rs_status}"
        cap = _triad(
            f"Vault {vref} DOC 轨 Git {'状态 4 人工收口' if commit_mode == 'human-sync' else ''}提交{'完成' if res.get('committed') else '无需新增提交'}（HEAD={head_s}，工作树洁净={res['clean_tree']}{rs_hint}）",
            [
                f"查看 DOC 与远端状态: ./gewu atlas --action doc-status --ref {vref}",
                f"显式双向同步: ./gewu atlas --action doc-sync --ref {vref}",
                "直接向人类汇报现状",
            ],
            "已按 116 规范保留人类手工修改/删除结果并执行统一 DOC+GitHub 策略（包含 00-DropZone，排除 .obsidian/source/归档/回收区）；远端离线或分叉时本地提交仍保全。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_DOC_COMMIT", cap, data=res), sealed_paths

    if action == "doc-sync":
        direction = str(args.get("direction") or "both").strip().lower() or "both"
        if direction not in ("both", "pull", "push"):
            raise VaultStoreError("E_PARAM_INVALID", "doc-sync 的 --direction 仅支持 both|pull|push", exit_code=1)
        bootstrap = str(args.get("bootstrap") or "").strip().lower()
        if bootstrap not in ("", "adopt-remote"):
            raise VaultStoreError("E_PARAM_INVALID", "doc-sync 的 --bootstrap 仅支持 adopt-remote", exit_code=1)
        actor = str(args.get("by") or args.get("agent") or "Agent").strip() or "Agent"
        force_sync = bool(args.get("force", False))

        remote_cfg = get_vault_doc_remote(state_dir, vref, vdir, fetch=False)
        if not remote_cfg.get("configured"):
            cap = _guard(
                "INPUT_REQUIRED",
                f"E_DOC_REMOTE_UNCONFIGURED: Vault {vref} 尚未绑定 GitHub/Git 远端仓库",
                f"请先执行 ./gewu atlas --action doc-remote --ref {vref} --remote_url <https://github.com/owner/repo.git> --branch main --auto_push true 后再同步。",
                "GUARD: INPUT_REQUIRED | 未配置远端不可盲目同步",
            )
            return 1, _envelope(False, "E_DOC_REMOTE_UNCONFIGURED", cap, data={"vault_ref": vref, "configured": False, "path_sealed": True}), sealed_paths

        pre_state = inspect_vault_doc_git_state(state_dir, vref, vdir, note_reg=note_reg)
        dirty_before = int(pre_state.get("dirty_count", 0))
        local_commit_info: Optional[Dict[str, Any]] = None

        if dirty_before > 0 and bootstrap != "adopt-remote":
            if not force_sync:
                link_res = engine_vfs_links(vault_dir=vdir, vault_ref=vref, note_reg=note_reg, kek=kek, app_id=app_id, cache_dir=state_dir / "notes_index")
                blocking_broken, ref_notes = _blocking_broken_links(link_res, note_reg, vref)
                dangling_cnt = len(blocking_broken)
                if dangling_cnt > 0:
                    cap = _guard(
                        "INPUT_REQUIRED",
                        f"E_DOC_HUMAN_DELETE_DANGLING_LINKS: 同步前检测到 {dangling_cnt} 处悬空断链，已按 116 状态 2 阻断同步",
                        (
                            "严禁恢复或重建已被人类删除的文件！请先清理受影响引用方卡片"
                            f"（{', '.join(ref_notes[:10]) or '见 vfs-links'}）中的失效双链后再执行 doc-sync；"
                            "若人类确认按现状强行同步，请加传 --force true。"
                        ),
                        "GUARD: INPUT_REQUIRED | 保护人类删除操作 · 禁止还原已删文件",
                    )
                    return 1, _envelope(
                        False,
                        "E_DOC_HUMAN_DELETE_DANGLING_LINKS",
                        cap,
                        data={
                            "vault_ref": vref,
                            "requires_approval": True,
                            "dangling_count": dangling_cnt,
                            "referencing_note_refs": ref_notes[:20],
                            "path_sealed": True,
                        },
                    ), sealed_paths
            local_commit_info = run_vault_doc_commit(
                state_dir=state_dir,
                vault_ref=vref,
                vault_dir=vdir,
                agent=actor,
                ticket_id=ticket_id_arg or "",
                reason=str(args.get("reason") or "pre-sync DOC checkpoint"),
                phase="post",
                commit_mode="human-sync" if pre_state.get("has_deletions") else "standard",
                push=False,
            )

        sync_res = sync_vault_doc_remote(
            state_dir=state_dir,
            vault_ref=vref,
            vault_dir=vdir,
            direction=direction,
            bootstrap=bootstrap,
            allow_rebase=True,
            force=force_sync,
        )
        sync_res["vault_ref"] = vref
        sync_res["direction"] = direction
        sync_res["local_commit"] = local_commit_info
        sync_res["path_sealed"] = True

        if not sync_res.get("synced"):
            st = str(sync_res.get("status") or "remote_error")
            code = f"E_DOC_SYNC_{st.upper()}"
            detail = str(sync_res.get("detail") or "远端同步受阻")
            if st == "unrelated_histories":
                guidance = f"请确认远端内容后执行 ./gewu atlas --action doc-sync --ref {vref} --bootstrap adopt-remote 对齐远端历史（严禁强推覆盖远端）。"
            elif st == "auth_required":
                guidance = "请在终端执行 gh auth login && gh auth setup-git 完成认证后重试 doc-sync。"
            else:
                guidance = detail
            cap = _guard("INPUT_REQUIRED", f"{code}: {detail}", guidance, "GUARD: INPUT_REQUIRED | Fail-Closed 防覆盖保护")
            return 1, _envelope(False, code, cap, data=sync_res), sealed_paths

        if sync_res.get("head_changed"):
            sync_res["reconciled_index"] = _reconcile_registries_after_remote_pull(
                vdir=vdir, vref=vref, state_dir=state_dir, note_reg=note_reg, proj_reg=proj_reg, kek=kek, app_id=app_id
            )
            sync_res["added_layout_dirs_count"] = sync_vault_layout_dirs(vdir)

        post_status = doc_git_status_summary(state_dir, vref, vdir, note_reg=note_reg)
        sync_res["clean_tree"] = bool(post_status.get("clean"))
        sync_res["dirty_count_after"] = int(post_status.get("dirty_count", 0))
        cap = _triad(
            f"Vault {vref} DOC 轨与远端同步完成（status={sync_res.get('status')}，HEAD={sync_res.get('head_short') or 'clean'}，branch={sync_res.get('branch')}）",
            [
                f"查看同步后状态: ./gewu atlas --action doc-status --ref {vref}",
                f"列出库内笔记: ./gewu atlas --action note-list --ref {vref}",
                "直接向人类汇报现状",
            ],
            "已按统一 DOC+GitHub 策略完成同步并自动刷新 KEK 课题与笔记索引，严禁 force push 覆盖远端历史。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_DOC_SYNC", cap, data=sync_res), sealed_paths

    # Manifest synchronization uses the exclusive lock but must not trigger a second delivery gate.
    if action == "delivery-check" or _is_non_mutating_vault_op(action, args):
        if ticket_id_arg:
            if action.startswith("slip-"):
                ensure_slip_ticket(ws_root, vdir, task_key=f"check:{vref}:{action}", title=f"查验工单 ({action})", ticket_id=ticket_id_arg)
            else:
                ensure_doc_ticket(ws_root, vdir, task_key=f"check:{vref}:{action}", title=f"查验工单 ({action})", ticket_id=ticket_id_arg)
        return _dispatch_vault_action(
            action=action,
            args=args,
            ws_root=ws_root,
            state_dir=state_dir,
            vdir=vdir,
            vref=vref,
            kek=kek,
            app_id=app_id,
            note_reg=note_reg,
            proj_reg=proj_reg,
            project_ref_arg=project_ref_arg,
            scope_arg=scope_arg,
            ticket_id_arg=ticket_id_arg,
            sealed_paths=sealed_paths,
        )

    actor = str(args.get("by") or args.get("agent") or "GewuAtlas").strip() or "GewuAtlas"
    self_managed_doc = action in ("note-route", "artifact-adopt", "artifact-finalize")
    is_slip = action in ("slip-commit", "slip-update", "slip-delete")
    pre_ticket: Optional[Dict[str, Any]] = None
    auto_created_ticket = False

    if is_slip:
        batch_key = str(args.get("batch_key") or "").strip()
        if ticket_id_arg or not (action == "slip-delete" and not bool(args.get("apply", False))):
            pre_ticket = ensure_slip_ticket(
                ws_root,
                vdir,
                task_key=batch_key or f"auto:{vref}:{action}",
                title=f"Slip 内容变更 ({action})",
                summary=f"删写覆盖必建票：{action}",
                actor=actor,
                ticket_id=ticket_id_arg,
            )
            ticket_id_arg = str(pre_ticket.get("ticket_id") or "") or ticket_id_arg
            auto_created_ticket = (args.get("ticket_id") is None) and (not batch_key) and (action == "slip-update")
    elif self_managed_doc:
        if ticket_id_arg:
            pre_ticket = ensure_doc_ticket(
                ws_root,
                vdir,
                task_key=f"auto:{vref}:{action}",
                title=f"Atlas 内容变更 ({action})",
                actor=actor,
                ticket_id=ticket_id_arg,
            )
    else:
        pre_ticket = ensure_doc_ticket(
            ws_root,
            vdir,
            task_key=f"auto:{vref}:{action}",
            title=f"Atlas 内容变更 ({action})",
            summary=f"删写覆盖必建票：{action}",
            actor=actor,
            ticket_id=ticket_id_arg,
        )
        auto_created_ticket = (ticket_id_arg is None)

    bound_tid = str((pre_ticket or {}).get("ticket_id") or ticket_id_arg or "")
    chg_reg = KekChangeRegistry(app_id, state_dir, vref, kek)
    est_count = estimate_affected_files(
        action=action,
        vault_dir=vdir,
        args=args,
        proj_reg=proj_reg,
        vault_ref=vref,
    )
    prior_touched = chg_reg.ticket_touched_files(bound_tid) if bound_tid else []
    crossed_pre_threshold = (
        est_count > DOC_COMMIT_THRESHOLD
        or (
            bool(bound_tid)
            and len(prior_touched) <= DOC_COMMIT_THRESHOLD
            and len(prior_touched) + est_count > DOC_COMMIT_THRESHOLD
            and not chg_reg.ticket_precommitted(bound_tid)
        )
    )
    pre_commit_info: Optional[Dict[str, Any]] = None
    if crossed_pre_threshold:
        pre_commit_info = run_vault_doc_commit(
            state_dir=state_dir,
            vault_ref=vref,
            vault_dir=vdir,
            agent=actor,
            ticket_id=bound_tid,
            phase="pre",
            file_count=est_count,
        )
        if bound_tid:
            chg_reg.mark_ticket_precommitted(bound_tid)

    record_diff_actions = action not in ("undo", "trash-purge", "retention-sweep", "archive-pack")
    include_journal = action.startswith("journal-") or str(args.get("zone") or "").strip().lower() == "journal"
    if not include_journal and args.get("note_ref"):
        try:
            _nrec = note_reg.resolve_handle(vref, str(args.get("note_ref")).strip())
            include_journal = str(_nrec.get("note_rel_path") or "").startswith("05-Journal/")
        except Exception:
            pass
    eager_bin = action in {
        "vfs-trash", "vfs-move", "vfs-copy", "vfs-untrash",
        "clean-attachments", "archive-pack", "project-trash", "project-split",
        "project-merge", "project-unpublish", "trash-purge", "retention-sweep",
    }
    targeted_rels: Optional[List[str]] = None
    post_targeted_create = False
    if record_diff_actions and action in ("note-update", "note-patch"):
        cand_refs: List[str] = []
        if args.get("note_ref"):
            cand_refs.extend([x.strip() for x in str(args["note_ref"]).split(",") if x.strip()])
        if args.get("note_refs"):
            cand_refs.extend([x.strip() for x in str(args["note_refs"]).split(",") if x.strip()])
        if args.get("batch_json"):
            try:
                bj = json.loads(str(args["batch_json"])) if isinstance(args["batch_json"], str) else args["batch_json"]
                if isinstance(bj, list):
                    for it in bj:
                        if isinstance(it, dict) and it.get("note_ref"):
                            cand_refs.append(str(it["note_ref"]).strip())
            except Exception:
                pass
        if cand_refs:
            hmap_inv = {ref: rel for rel, ref in note_reg.list_vault_handles(vref).items()}
            resolved_rels = [hmap_inv[r] for r in dict.fromkeys(cand_refs) if r in hmap_inv]
            if len(resolved_rels) == len(dict.fromkeys(cand_refs)):
                targeted_rels = resolved_rels
    elif record_diff_actions and action == "note-create":
        # note-create uses O_EXCL new-file creation; avoid O(N) full-vault pre-scan.
        post_targeted_create = True

    if not record_diff_actions or post_targeted_create:
        pre_snap = {}
    elif targeted_rels is not None:
        pre_snap = capture_targeted_snapshot(vdir, targeted_rels)
    else:
        pre_snap = capture_vault_snapshot(
            vdir,
            include_journal=include_journal,
            cache_dir=chg_reg.base_dir,
            eager_binary_bytes=eager_bin,
        )

    rc, env, ret_sealed = _dispatch_vault_action(
        action=action,
        args=args,
        ws_root=ws_root,
        state_dir=state_dir,
        vdir=vdir,
        vref=vref,
        kek=kek,
        app_id=app_id,
        note_reg=note_reg,
        proj_reg=proj_reg,
        project_ref_arg=project_ref_arg,
        scope_arg=scope_arg,
        ticket_id_arg=ticket_id_arg,
        sealed_paths=sealed_paths,
    )
    data = env.get("data") if isinstance(env.get("data"), dict) else {}
    is_partial_or_paused = (rc != 0) or bool(data.get("requires_approval"))

    # Resolve final bound ticket info from self-managed actions if applicable
    doc_tinfo = pre_ticket
    if doc_tinfo is None and isinstance(data.get("doc_ticket"), dict):
        doc_tinfo = data["doc_ticket"]
        bound_tid = str(doc_tinfo.get("ticket_id") or bound_tid)
    elif not bound_tid and isinstance(data.get("ticket_id"), str):
        bound_tid = str(data["ticket_id"])

    if post_targeted_create and data.get("note_ref"):
        try:
            _created_rec = note_reg.resolve_handle(vref, str(data["note_ref"]).strip())
            _created_rel = str(_created_rec.get("note_rel_path") or "").strip()
            if _created_rel:
                targeted_rels = [_created_rel]
        except Exception:
            targeted_rels = None

    should_collect_diff = record_diff_actions and (
        not is_partial_or_paused
        or (action == "batch" and int(data.get("succeeded") or 0) > 0)
    )
    if not should_collect_diff:
        changes = []
    elif targeted_rels is not None:
        changes = _collect_targeted_changes(vdir, pre_snap, targeted_rels)
    else:
        changes = _collect_vault_changes(vdir, pre_snap, include_journal=include_journal, cache_dir=chg_reg.base_dir)

    extra_updates: Dict[str, Any] = {}
    if changes:
        chg_summary = chg_reg.record_change(
            vault_dir=vdir,
            action=action,
            actor=actor,
            ticket_id=bound_tid,
            changes=changes,
        )
        extra_updates["change_id"] = chg_summary["change_id"]
        extra_updates["change_summary"] = chg_summary
        extra_updates["undo_hint"] = chg_summary["undo_hint"]

        if doc_tinfo is not None and not self_managed_doc and not (is_slip and action in ("slip-commit", "slip-delete")):
            prog = update_doc_ticket_progress(
                ws_root,
                vdir,
                ticket_info=doc_tinfo,
                note=(
                    f"{action} 改动 {chg_summary['file_count']} 项"
                    f"（+{chg_summary['lines_added']}/-{chg_summary['lines_removed']}），"
                    f"变更句柄 `{chg_summary['change_id']}`，撤回：`{chg_summary['undo_hint']}`"
                ),
                actor=actor,
                mark_done=auto_created_ticket and not bool(data.get("requires_approval")),
            )
            extra_updates["doc_ticket"] = {k: v for k, v in prog.items() if k != "ticket_rel_path"}
    elif doc_tinfo is not None and not self_managed_doc and not is_slip:
        if is_partial_or_paused and auto_created_ticket and not bool(data.get("requires_approval")):
            prog = update_doc_ticket_progress(
                ws_root,
                vdir,
                ticket_info=doc_tinfo,
                note=f"{action} 未产生落盘改动（code={env.get('code')}），自动收口工单",
                actor=actor,
                mark_done=True,
            )
            extra_updates["doc_ticket"] = {k: v for k, v in prog.items() if k != "ticket_rel_path"}
        else:
            extra_updates["doc_ticket"] = {k: v for k, v in doc_tinfo.items() if k != "ticket_rel_path"}

    if is_partial_or_paused:
        if extra_updates:
            _attach_data_fields(env, extra_updates)
        return rc, env, ret_sealed

    cumul_cnt = len(chg_reg.ticket_touched_files(bound_tid)) if bound_tid else len(changes)
    if (
        pre_commit_info is not None
        or len(changes) > DOC_COMMIT_THRESHOLD
        or cumul_cnt > DOC_COMMIT_THRESHOLD
    ):
        post_commit_info = run_vault_doc_commit(
            state_dir=state_dir,
            vault_ref=vref,
            vault_dir=vdir,
            agent=actor,
            ticket_id=bound_tid,
            reason=action,
            phase="post",
            file_count=max(est_count, len(changes), cumul_cnt),
        )
        extra_updates["doc_git"] = {
            "pre_commit": pre_commit_info,
            "post_commit": post_commit_info,
        }

    if action in (
        "note-create",
        "note-update",
        "note-patch",
        "note-promote",
        "atomic-extract",
        "atomic-approve",
        "moc-sync",
        "artifact-adopt",
        "artifact-finalize",
        "batch",
    ):
        changed_rels_for_gate = [str(c.get("rel_path") or "") for c in changes if isinstance(c, dict) and c.get("rel_path")]
        if not changed_rels_for_gate and targeted_rels:
            changed_rels_for_gate = list(targeted_rels)
        is_wb_only_change = bool(changed_rels_for_gate) and all(_is_workbench_rel(r) for r in changed_rels_for_gate)
        is_dirty_only_eligible = bool(changed_rels_for_gate) and (
            (action in ("note-create", "note-update", "note-patch") and is_wb_only_change)
            or action == "note-patch"
            or (action == "batch" and (is_wb_only_change or _is_in_place_batch_only(args)))
        )
        extra_updates["delivery_gate"] = _piggyback_delivery_gate(
            ws_root=ws_root,
            vdir=vdir,
            vref=vref,
            note_reg=note_reg,
            proj_reg=proj_reg,
            project_ref=project_ref_arg,
            actor=actor,
            action=action,
            env=env,
            kek=kek,
            app_id=app_id,
            cache_dir=state_dir / "notes_index",
            batch_key=str(args.get("batch_key") or ticket_id_arg or "").strip(),
            dirty_only=is_dirty_only_eligible,
            dirty_rels=changed_rels_for_gate if is_dirty_only_eligible else None,
        )
    _attach_data_fields(env, extra_updates)
    return rc, env, ret_sealed


def _attach_auto_slices(
    res: Dict[str, Any],
    *,
    vault_dir: Path,
    vault_ref: str,
    note_reg: KekNoteRegistry,
    proj_reg: KekProjectRegistry,
    kek: bytes,
    app_id: str,
    project_ref: Optional[str],
    scope: str,
    args: Dict[str, Any],
    sealed_paths: List[str],
) -> None:
    """Optionally run deterministic AST slicing on ingested notes in the same transaction."""
    cand_refs: List[str] = []
    for key in ("results", "routed"):
        rows = res.get(key)
        if isinstance(rows, list):
            for item in rows:
                if (
                    isinstance(item, dict)
                    and item.get("note_ref")
                    and item.get("status") != "blocked"
                    and not item.get("index_candidate")
                ):
                    cand_refs.append(str(item["note_ref"]))
    if res.get("note_ref"):
        # Skip single index_candidate so borderline directory notes are not locked by TOC/slice backlinks.
        results_list = res.get("results") or []
        if not (isinstance(results_list, list) and len(results_list) == 1 and isinstance(results_list[0], dict) and results_list[0].get("index_candidate")):
            cand_refs.append(str(res["note_ref"]))
    cand_refs = list(dict.fromkeys(cand_refs))
    if not cand_refs:
        res["auto_slices"] = []
        return
    level_val = int(args.get("level") or 2)
    use_adaptive = bool(args.get("adaptive", "level" not in args))
    sliced_rows: List[Dict[str, Any]] = []
    slice_errors: List[Dict[str, Any]] = []
    batch_state = None
    if use_adaptive and len(cand_refs) > 1:
        from pipeline.atlas_slicer_engine import SliceBatch, note_slice_adaptive
        batch_state = SliceBatch(vault_dir)
    for nref in cand_refs:
        try:
            common = dict(vault_dir=vault_dir, vault_ref=vault_ref, note_reg=note_reg, proj_reg=proj_reg,
                          app_id=app_id, note_ref=nref, project_ref=project_ref, scope=scope, idempotent=True)
            if batch_state is None:
                sres, spaths = engine_note_slice(**common, kek=kek, level=level_val, adaptive=use_adaptive,
                    min_chars=args.get("min_chars"), max_chars=args.get("max_chars"),
                    target_chars=args.get("target_chars"), prefix=args.get("prefix"))
            else:
                sres, spaths = note_slice_adaptive(**common, batch=batch_state,
                    min_chars=300 if args.get("min_chars") is None else args["min_chars"],
                    max_chars=4000 if args.get("max_chars") is None else args["max_chars"],
                    target_chars=args.get("target_chars"), custom_prefix=args.get("prefix"))
            sealed_paths.extend(spaths)
            sliced_rows.append(
                {
                    "source_note_ref": nref,
                    "toc_note_ref": sres.get("toc_note_ref"),
                    "slice_count": sres.get("slice_count", 0),
                    "slices": sres.get("slices", []),
                    **({"already_sliced": True} if sres.get("already_sliced") else {}),
                }
            )
        except VaultStoreError as exc:
            slice_errors.append({"source_note_ref": nref, "code": exc.code, "reason": exc.message})
        except Exception as exc:
            slice_errors.append({"source_note_ref": nref, "code": "E_SLICE_FAILED", "reason": str(exc)[:160]})
        if batch_state is not None and slice_errors and slice_errors[-1]["source_note_ref"] == nref:
            batch_state = SliceBatch(vault_dir)
    if batch_state is not None:
        mapping = note_reg.sync_paths(vault_ref, sorted(batch_state.paths))
        sealed_paths.extend(batch_state.paths)
        for row in sliced_rows:
            row["toc_note_ref"] = mapping.get(row["toc_note_ref"])
            for card in row["slices"]:
                card["note_ref"] = mapping[card["note_ref"]]
    res["auto_slices"] = sliced_rows
    if slice_errors:
        res["slice_errors"] = slice_errors


def _dispatch_vault_action(
    *,
    action: str,
    args: Dict[str, Any],
    ws_root: Path,
    state_dir: Path,
    vdir: Path,
    vref: str,
    kek: bytes,
    app_id: str,
    note_reg: KekNoteRegistry,
    proj_reg: KekProjectRegistry,
    project_ref_arg: Optional[str],
    scope_arg: str,
    ticket_id_arg: Optional[str],
    sealed_paths: List[str],
) -> Tuple[int, Dict[str, Any], List[str]]:
    if action == "change-list":
        chg_reg = KekChangeRegistry(app_id, state_dir, vref, kek)
        res = chg_reg.list_changes(
            ticket_id=str(args.get("ticket_id") or "").strip(),
            include_undone=bool(args.get("include_undone", True)),
            limit=int(args.get("top_k") or 50),
        )
        cap = _triad(
            f"Vault {vref} 变更台账检索完成（共 {res['count']} 条 chg_XX 记录）[只读完成]",
            [
                f"查看详情/Diff: ./gewu atlas --action change-get --ref {vref} --change_id <chg_01>",
                f"执行后悔药撤回: ./gewu atlas --action undo --ref {vref} --change_id <chg_01>",
            ],
            "所有删写覆盖均记录前置快照（KEK 密文隔离）及行级 diff 明细。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_CHANGE_LIST", cap, data=res), sealed_paths

    if action == "change-get":
        chg_id = str(args.get("change_id") or args.get("op_id") or "").strip()
        if not chg_id:
            raise VaultStoreError("E_PARAM_MISSING", "change-get 需要 --change_id <chg_01>", exit_code=1)
        chg_reg = KekChangeRegistry(app_id, state_dir, vref, kek)
        res = chg_reg.get_change(chg_id)
        cap = _triad(
            f"已读取变更 {chg_id}（{res['action']}，文件={res['file_count']}，+{res['lines_added']}/-{res['lines_removed']}）[只读完成]",
            [f"执行后悔药撤回: ./gewu atlas --action undo --ref {vref} --change_id {chg_id}", "直接向人类汇报现状"],
            "已按句柄脱敏物理路径并返回行级 unified diff；完整明文日志存 Vault 90-System/92-Logs/Changes/。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_CHANGE_GET", cap, data=res), sealed_paths

    if action == "undo":
        chg_id = str(args.get("change_id") or args.get("op_id") or "").strip()
        if not chg_id:
            raise VaultStoreError("E_PARAM_MISSING", "undo 需要 --change_id <chg_01>", exit_code=1)
        chg_reg = KekChangeRegistry(app_id, state_dir, vref, kek)
        trash_reg = KekTrashRegistry(app_id, state_dir / "atlas-trash.enc", kek)
        res, u_paths = chg_reg.undo_change(
            vault_dir=vdir,
            chg_id=chg_id,
            note_reg=note_reg,
            trash_reg=trash_reg,
            force=bool(args.get("force", False)),
        )
        sealed_paths.extend(u_paths)
        if res.get("requires_approval"):
            cap = _guard(
                "INPUT_REQUIRED",
                f"E_UNDO_CONFLICT: 变更 {chg_id} 之后部分文件被人类手工修改或删除（{len(res['conflicts'])} 项冲突），已强制拦截以免误覆盖/恢复人类行为",
                (
                    "严禁 Agent 擅自加 --force 恢复已被人类删除的文件或覆盖人类修改！请先向人类汇报冲突清单；"
                    f"仅当人类明确授权覆盖时才可执行: ./gewu atlas --action undo --ref {vref} --change_id {chg_id} --force true"
                ),
                "GUARD: INPUT_REQUIRED | 保护人类修改与删除 · 禁止擅自覆盖",
            )
            return 1, _envelope(False, "E_UNDO_CONFLICT", cap, data=res), sealed_paths
        cap = _triad(
            f"后悔药撤回 {chg_id} 成功（还原 {res['restored_count']} 项，新建项转入回收站 {res['trashed_created_count']} 项）",
            [f"查看最新笔记清单: ./gewu atlas --action note-list --ref {vref}", "直接向人类汇报现状"],
            "全过程无损恢复：被改/删文档逐字节还原，撤回时清理的新建文件已转入托管回收站（trs_XX）。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_UNDO_APPLIED", cap, data=res), sealed_paths

    if action == "trash-purge":
        days_val = int(args.get("days")) if args.get("days") is not None else 30
        dry_run_val = bool(args.get("dry_run", False))
        trash_reg = KekTrashRegistry(app_id, state_dir / "atlas-trash.enc", kek)
        res = engine_trash_purge(
            vault_dir=vdir,
            vault_ref=vref,
            trash_reg=trash_reg,
            days=days_val,
            dry_run=dry_run_val,
        )
        mode_lbl = "演练预览" if res["dry_run"] else "已清理"
        cap = _triad(
            f"Vault {vref} 回收站（>{res['retention_days']}天）{mode_lbl}：托管记录 {res['purged_indexed_count']} 项，未登记旧件 {res['purged_unindexed_count']} 项",
            [f"全量生命周期清扫: ./gewu atlas --action retention-sweep --ref {vref}", "直接向人类汇报现状"],
            "仅清理超过保留天数阈值的过期回收项，未满期人类误删笔记完整保留。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_TRASH_PURGED", cap, data=res), sealed_paths

    if action == "retention-sweep":
        days_val = int(args.get("days")) if args.get("days") is not None else 30
        dry_run_val = bool(args.get("dry_run", False))
        trash_reg = KekTrashRegistry(app_id, state_dir / "atlas-trash.enc", kek)
        chg_reg = KekChangeRegistry(app_id, state_dir, vref, kek)
        res = engine_retention_sweep(
            vault_dir=vdir,
            vault_ref=vref,
            state_dir=state_dir,
            app_id=app_id,
            kek=kek,
            trash_reg=trash_reg,
            chg_reg=chg_reg,
            days=days_val,
            dry_run=dry_run_val,
        )
        mode_lbl = "演练预览" if res["dry_run"] else "已清扫"
        cap = _triad(
            f"Vault {vref} 30天过期资产{mode_lbl}：回收站={res['trash_purged_indexed'] + res['trash_purged_unindexed']}、后悔药快照={res['undo_purged']}、变更日志={res['change_logs_purged']}、卷内旧票={res['vault_tickets_purged']}",
            [f"查看回收站现状: ./gewu atlas --action vfs-trash-list --ref {vref}", "直接向人类汇报现状"],
            "严格保留未满保留期（默认30天）的所有回收站、后悔药与工单记录；05-Journal 日记仅归档不删除。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_RETENTION_SWEPT", cap, data=res), sealed_paths

    if action == "attachment-import":
        from pipeline.atlas_fmt_engine import import_attachment
        res = import_attachment(ws_root=ws_root, vault_dir=vdir, vault_ref=vref,
                                input_file=str(args.get("input_file") or ""))
        cap = _triad("附件已登记到受管收件箱", [f"转换附件: ./gewu atlas --action fmt --ref {vref} --file_name {res['file_name']}", "直接汇报导入结果"], "保留原附件；不覆盖不同内容同名文件，不执行附件。", terminal=True)
        return 0, _envelope(True, "OK_ATTACHMENT_IMPORTED", cap, data=res), sealed_paths

    if action == "workbench-workspace":
        res, wb_paths = engine_workbench_workspace(
            vault_dir=vdir,
            vault_ref=vref,
            proj_reg=proj_reg,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            project_ref=project_ref_arg,
            scope=scope_arg,
            agent=str(args.get("agent") or args.get("by") or args.get("alias") or "").strip() or None,
            topic=str(args.get("topic") or args.get("title") or "").strip() or None,
            mode=str(args.get("mode") or "allocate").strip(),
            dry_run=bool(args.get("dry_run", False)),
        )
        sealed_paths.extend(wb_paths)
        if res.get("mode") == "list":
            cap = _triad(
                f"已列出 {res['count']} 个 Agent 个人工作空间（命名规范 {res['naming_convention']}）[只读完成]",
                [f"分配个人空间: ./gewu atlas --action workbench-workspace --ref {vref} --agent <name> [--project_ref <prj_01>]"],
                "每个个人空间内设 drafts/、91-Temp/ 与已纳入 DOC Git 忽略的 source/。",
                terminal=True,
            )
            return 0, _envelope(True, "OK_WORKBENCH_WORKSPACE_LIST", cap, data=res), sealed_paths
        subs = res["subdirs"]
        proj_flag = f" --project_ref {project_ref_arg}" if res.get("domain_scope") == "project" and project_ref_arg else ""
        cap = _triad(
            f"已为 {res['agent']} 在 {res['project_id']} 工作台分配个人目录 {res['workspace_id']}（含 drafts / 91-Temp / source）",
            [
                f"写草稿: ./gewu atlas --action note-create --ref {vref}{proj_flag} --zone workbench --subdir {subs['drafts']} --title <标题>",
                f"正式资料入文献库并切片: ./gewu atlas --action ingest --ref {vref}{proj_flag} --urls <URL1,URL2> --auto_slice true",
            ],
            "正式研究源文档默认入 21-Library；ws-<agent>/source 仅作临时草采缓存。严禁在工作区根目录或知识库外自建目录，严禁在课题下自建 99-Assets。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_WORKBENCH_WORKSPACE_ALLOCATED", cap, data=res), sealed_paths

    if action == "file-read":
        res, fpaths = engine_file_read(
            vault_dir=vdir,
            vault_ref=vref,
            proj_reg=proj_reg,
            project_ref=project_ref_arg,
            scope=scope_arg,
            zone=str(args.get("zone") or "workbench").strip(),
            subdir=str(args.get("subdir") or "").strip() or None,
            file_name=str(args.get("file_name") or "").strip() or None,
            start_line=args.get("start_line"),
            end_line=args.get("end_line"),
            head=args.get("head"),
            tail=args.get("tail"),
        )
        sealed_paths.extend(fpaths)
        if res["mode"] == "list":
            cap = _triad(
                f"已列出 {res['zone']} 区 {res['count']} 个文件（{res['domain_scope']}）[只读完成]",
                [f"读取完整原文: ./gewu atlas --action file-read --ref {vref} --zone {res['zone']} --file_name <文件名>"],
                "仅列出受控目录相对文件名；读取文本文件可传 --head / --tail 或 --start_line / --end_line 分段。",
                terminal=True,
            )
            return 0, _envelope(True, "OK_FILE_LIST", cap, data=res), sealed_paths
        cap = _triad(
            f"已读取受控文件「{res['file_name']}」（{res['returned_start_line']}-{res['returned_end_line']}/{res['total_lines']} 行）[只读完成]",
            [f"列举同区文件: ./gewu atlas --action file-read --ref {vref} --zone {res['zone']}", "直接向人类汇报现状"],
            "已校验非符号链接与 UTF-8 编码并屏蔽物理路径。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_FILE_READ", cap, data=res), sealed_paths

    if action in ("fmt-window", "fmt-apply-window", "fmt-assets"):
        from pipeline.atlas_fmt_refine import read_window, apply_window, note_assets
        common = dict(vault_dir=vdir, vault_ref=vref, note_ref=str(args.get("note_ref") or ""), note_reg=note_reg)
        if action == "fmt-window":
            res = read_window(**common, offset=int(args.get("offset") or 0), max_chars=int(args.get("max_chars") or 3000))
        elif action == "fmt-assets":
            res = note_assets(**common, ws_root=ws_root, file_name=args.get("file_name"))
        else:
            res = apply_window(**common, offset=int(args.get("offset") or 0), end_offset=int(args.get("end_offset") or 0), content=str(args.get("content") or ""), expected_sha256=str(args.get("expected_sha256") or ""))
        cap = _triad("已完成格式精加工辅助动作", ["按 atlas-fmt 技能继续核对下一窗口或图片", "向用户汇报实际完成范围"], "脚本不执行语义判断；此回执不代表 deep 加工完成。", terminal=True)
        return 0, _envelope(True, "OK_FMT_REFINEMENT", cap, data=res), sealed_paths

    def _build_url_ingest_capsule(vref: str, res: Dict[str, Any]) -> Dict[str, Any]:
        blk = int(res.get("blocked_count") or 0)
        conv = int(res.get("converted_count") or 0)
        thin_cnt = int(res.get("thin_with_links_count") or 0)
        disc_cnt = int(res.get("discarded_count") or 0)
        idx_refs = [str(x) for x in (res.get("index_candidate_refs") or []) if str(x)]
        depth_val = int(res.get("depth") or 1)
        max_d = int(res.get("max_depth") or 3)
        depth_capped = bool(res.get("depth_limit_reached", depth_val >= max_d))
        log_ref = str(res.get("ingest_log_ref") or "")

        need_inspect = bool(thin_cnt > 0 or idx_refs)
        if need_inspect and not depth_capped:
            gate_tag = f"[INGEST_GATE: INSPECT_LINKS (depth={depth_val}/{max_d})]"
        elif need_inspect and depth_capped:
            gate_tag = f"[INGEST_GATE: DEPTH_CAP_REACHED ({depth_val}/{max_d})]"
        else:
            gate_tag = "[INGEST_GATE: SUBSTANCE_READY]"

        orbit_url: List[str] = []
        if need_inspect and not depth_capped:
            next_d = depth_val + 1
            orbit_url.append(
                f"检查内容与 extracted_links 筛选目标 URL 后发起第 {next_d} 层爬取: "
                f"./gewu atlas --action url-ingest --ref {vref} --urls '<筛选后URL>' --depth {next_d}"
            )
        if idx_refs:
            orbit_url.append(
                f"确认目录/无实质内容页后立即删除 ({','.join(idx_refs)}): "
                f"./gewu atlas --action trash --ref {vref} --mode delete --note_ref {idx_refs[0]}"
            )
        elif need_inspect and depth_capped:
            orbit_url.append("已达 3 层深爬上限，停止展开外链并向人类汇报需指定的精确入口 URL")
        if log_ref:
            orbit_url.append(f"回看采集日志尾部: ./gewu atlas --action note-get --ref {vref} --note_ref {log_ref} --tail 40")
        orbit_url.append(f"查看知识库笔记: ./gewu atlas --action note-list --ref {vref}")

        anchor_parts = [
            f"已执行 SSRF 校验、转译与双口径 <=300 字顺风车质检（已自动删除 {disc_cnt} 个 <=300 字且无有效外链的空壳网页）。"
        ]
        if need_inspect:
            anchor_parts.append(
                "检测到纯目录或高链接密度页：禁止直接当正文萃取；检查内容筛选链接进入下层 url-ingest（上限 3 层），无独立价值目录页须删除。"
            )
        if log_ref:
            anchor_parts.append(f"采集台账已记入 {log_ref}（用 note-get --tail 40 查看，严禁在 tmp 自建日志）。")

        return _triad(
            f"已完成 Vault {vref} URL 采集（L{depth_val}/{max_d} · 成功 {conv}、目录待追查 {thin_cnt + len(idx_refs)}、自动删除 {disc_cnt}、拦截 {blk}） {gate_tag}",
            orbit_url,
            " ".join(anchor_parts),
            terminal=True,
        )

    if action == "fmt":
        res = engine_run_fmt(
            vault_dir=vdir,
            vault_ref=vref,
            ws_root=ws_root,
            proj_reg=proj_reg,
            note_reg=note_reg,
            note_ref=str(args.get("note_ref") or "").strip() or None,
            project_ref=project_ref_arg,
            scope=scope_arg,
            url=str(args.get("url") or "").strip() or None,
            urls=args.get("urls"),
            file_name=str(args.get("file_name") or "").strip() or None,
            by=str(args.get("by") or "").strip() or None,
            dry_run=bool(args.get("dry_run", False)),
            deep=bool(args.get("deep", False)),
            annotate_images=bool(args.get("annotate_images", False)),
            fmt_options={k: args[k] for k in ("pdf_mode", "dpi", "threshold", "skip_decorative", "decorative_size", "decorative_dim", "encoding", "dump_json", "dump_payload", "no_symbols") if k in args},
            normalize=bool(args.get("normalize", False)),
            check=bool(args.get("check", False)),
            ticket_id=ticket_id_arg,
            target_zone=str(args.get("target_zone") or "").strip() or None,
            subdir=str(args.get("subdir") or "").strip() or None,
            min_chars=args.get("min_chars"),
            depth=args.get("depth"),
            parent_url=str(args.get("parent_url") or "").strip() or None,
        )
        if bool(args.get("auto_slice", False)) and not bool(args.get("dry_run", False)) and not bool(args.get("deep", False)) and not bool(args.get("check", False)):
            _attach_auto_slices(
                res,
                vault_dir=vdir, vault_ref=vref, note_reg=note_reg, proj_reg=proj_reg, kek=kek, app_id=app_id,
                project_ref=project_ref_arg, scope=scope_arg, args=args, sealed_paths=sealed_paths,
            )
        cnt = int(res.get("converted_count", 0))
        blk = int(res.get("blocked_count", 0))
        if res.get("mode") in ("url", "url-batch", "url-list"):
            cap = _build_url_ingest_capsule(vref, res)
        else:
            cap = _triad(
                f"已完成 Vault {vref} 多模态 fmt 转译（模式 {res.get('mode')}，成功 {cnt} 项，拦截 {blk} 项）",
                [f"查看知识库笔记: ./gewu atlas --action note-list --ref {vref}", "继续执行 note-slice / atomic-extract 或向人类汇报"],
                "已启用完整第三方多模态转译引擎，原件冷归档至 99-Assets/98-Archives/{YYMM}/，附件统一存放 99-Assets/10-Attachments/。",
                terminal=True,
            )
        return (1 if blk else 0), _envelope(not blk, "E_FMT_PARTIAL" if blk else "OK_FMT", cap, data=res), sealed_paths

    if action == "url-ingest":
        res = engine_run_url_ingest(
            vault_dir=vdir,
            vault_ref=vref,
            ws_root=ws_root,
            proj_reg=proj_reg,
            note_reg=note_reg,
            note_ref=str(args.get("note_ref") or "").strip() or None,
            file_name=str(args.get("file_name") or "").strip() or None,
            url=str(args.get("url") or "").strip() or None,
            urls=args.get("urls"),
            project_ref=project_ref_arg,
            scope=scope_arg,
            by=str(args.get("by") or "").strip() or None,
            dry_run=bool(args.get("dry_run", False)),
            deep=bool(args.get("deep", False)),
            ticket_id=ticket_id_arg,
            target_zone=str(args.get("target_zone") or "").strip() or None,
            subdir=str(args.get("subdir") or "").strip() or None,
            min_chars=args.get("min_chars"),
            depth=args.get("depth"),
            parent_url=str(args.get("parent_url") or "").strip() or None,
        )
        if bool(args.get("auto_slice", False)) and not bool(args.get("dry_run", False)) and not bool(args.get("deep", False)):
            _attach_auto_slices(
                res,
                vault_dir=vdir, vault_ref=vref, note_reg=note_reg, proj_reg=proj_reg, kek=kek, app_id=app_id,
                project_ref=project_ref_arg, scope=scope_arg, args=args, sealed_paths=sealed_paths,
            )
        blk = int(res.get("blocked_count") or 0)
        conv = int(res.get("converted_count") or 0)
        thin_cnt = int(res.get("thin_with_links_count") or 0)
        disc_cnt = int(res.get("discarded_count") or 0)
        all_failed = bool(res.get("mode") == "url-batch" and conv == 0 and thin_cnt == 0 and disc_cnt == 0 and blk > 0 and not bool(args.get("dry_run", False)))
        cap = _build_url_ingest_capsule(vref, res)
        return (1 if all_failed else 0), _envelope(not all_failed, "E_URL_INGEST_FAILED" if all_failed else "OK_URL_INGEST", cap, data=res), sealed_paths

    if action.startswith("slip-"):
        if action == "slip-scan":
            res, slip_paths = engine_slip_scan(
                vault_dir=vdir,
                vault_ref=vref,
                note_reg=note_reg,
                proj_reg=proj_reg,
                kek=kek,
                app_id=app_id,
                ws_root=ws_root,
                note_ref=str(args.get("note_ref") or "").strip() or None,
                project_ref=project_ref_arg,
                zone_filter=str(args.get("zone") or "").strip() or None,
                subdir=str(args.get("subdir") or "").strip() or None,
                file_name=str(args.get("file_name") or "").strip() or None,
                create_ticket=bool(args.get("create_ticket", False)),
                ticket_id=ticket_id_arg,
                batch_key=str(args.get("batch_key") or "").strip() or None,
            )
            sealed_paths.extend(slip_paths)
            cap = _triad(
                f"已完成 Vault {vref} 书签扫描（扫描 {res['scanned_files']} 篇，命中 {res['matched_files']} 篇，共 {res['marker_count']} 个 ==@指令== / @@ 标记块）[只读完成]",
                [
                    f"提交生成的书签卡片并回写双链: ./gewu atlas --action slip-commit --ref {vref} --source_note_ref <nte_01> --marker_id <slp_xx> --title <标题> --content <正文>",
                    "如为下载指令先执行 url-ingest --target_zone workbench --subdir source，再通过 --links 挂载至 slip-commit",
                ],
                "已识别 ==@指令==（兼容全半角及历史 @@...@@），自动跳过普通 ==高亮==、Frontmatter、代码围栏与反引号；支持按 --note_ref / --file_name / --zone+--subdir / --project_ref 精准限定扫描范围。",
                terminal=True,
            )
            return 0, _envelope(True, "OK_SLIP_SCAN", cap, data=res), sealed_paths

        if action == "slip-commit":
            res, slip_paths = engine_slip_commit(
                vault_dir=vdir,
                vault_ref=vref,
                note_reg=note_reg,
                proj_reg=proj_reg,
                kek=kek,
                app_id=app_id,
                ws_root=ws_root,
                title=str(args.get("title") or ""),
                content=str(args.get("content") or ""),
                summary=str(args.get("summary") or "").strip() or None,
                tags=args.get("tags"),
                aliases=args.get("aliases"),
                slip_kind=str(args.get("slip_kind") or "").strip() or None,
                status=str(args.get("status") or "active"),
                source_note_ref=str(args.get("source_note_ref") or args.get("note_ref") or "").strip() or None,
                marker_text=str(args.get("marker_text") or "").strip() or None,
                marker_id=str(args.get("marker_id") or "").strip() or None,
                expected_sha256=str(args.get("expected_sha256") or "").strip() or None,
                source_url=str(args.get("source_url") or args.get("url") or "").strip() or None,
                links=args.get("links") or args.get("note_refs"),
                project_ref=project_ref_arg,
                scope=scope_arg,
                ticket_id=ticket_id_arg,
                batch_key=str(args.get("batch_key") or "").strip() or None,
                mark_done=bool(args.get("mark_done", False)),
            )
            sealed_paths.extend(slip_paths)
            cap = _triad(
                f"已在 Vault {vref} 25-Slip 创建书签「{res['title']}」（句柄 {res['note_ref']}，层级={res['nesting_depth']}，双向链接={res['backlink_added']}）",
                [f"读取书签卡片: ./gewu atlas --action slip-get --ref {vref} --note_ref {res['note_ref']}", "直接向人类汇报"],
                "已按 slip-<YYMMDD>-<4hex>-<slug> 命名规范落盘，Frontmatter aliases 与原文档 [[slip|alias]] 已保持原书签标记块一致。",
                terminal=False,
            )
            return 0, _envelope(True, "OK_SLIP_COMMITTED", cap, data=res), sealed_paths

        if action == "slip-list":
            items, slip_paths = engine_slip_list(
                vault_dir=vdir,
                vault_ref=vref,
                note_reg=note_reg,
                proj_reg=proj_reg,
                kek=kek,
                app_id=app_id,
                project_ref=project_ref_arg,
                status_filter=args.get("status"),
                slip_kind_filter=args.get("slip_kind"),
            )
            sealed_paths.extend(slip_paths)
            cap = _triad(
                f"已列出 Vault {vref} 内 {len(items)} 条 25-Slip 书签 [只读完成]",
                [f"读取详情: ./gewu atlas --action slip-get --ref {vref} --note_ref <nte_01>", "直接向人类汇报现状"],
                "25-Slip 独立于 20-Memory 存放轻量书签批注，支持逻辑句柄检索与嵌套。",
                terminal=True,
            )
            return 0, _envelope(True, "OK_SLIP_LIST", cap, data={"vault_ref": vref, "count": len(items), "slips": items, "path_sealed": True}), sealed_paths

        if action == "slip-get":
            nref = str(args.get("note_ref") or "").strip()
            if not nref:
                raise VaultStoreError("E_PARAM_MISSING", "slip-get 需要 --note_ref <nte_01>", exit_code=1)
            pub, slip_paths = engine_slip_get(
                vault_dir=vdir,
                vault_ref=vref,
                note_ref=nref,
                note_reg=note_reg,
                kek=kek,
                app_id=app_id,
            )
            sealed_paths.extend(slip_paths)
            cap = _triad(
                f"已读取书签 {nref}「{pub['title']}」（别名 {len(pub.get('aliases') or [])} 个，出链 {len(pub.get('outgoing_links') or [])} 条，入链 {len(pub.get('backlink_refs') or [])} 条）[只读完成]",
                [f"更新书签: ./gewu atlas --action slip-update --ref {vref} --note_ref {nref} --summary <摘要>", "直接向人类汇报现状"],
                "物理路径已密封，返回体含反向双链与正文。",
                terminal=True,
            )
            return 0, _envelope(True, "OK_SLIP_GET", cap, data=pub), sealed_paths

        if action == "slip-update":
            nref = str(args.get("note_ref") or "").strip()
            if not nref:
                raise VaultStoreError("E_PARAM_MISSING", "slip-update 需要 --note_ref <nte_01>", exit_code=1)
            pub, slip_paths = engine_slip_update(
                vault_dir=vdir,
                vault_ref=vref,
                note_ref=nref,
                note_reg=note_reg,
                kek=kek,
                app_id=app_id,
                title=args.get("title"),
                summary=args.get("summary"),
                tags=args.get("tags"),
                aliases=args.get("aliases"),
                slip_kind=args.get("slip_kind"),
                status=args.get("status"),
                content=args.get("content"),
            )
            sealed_paths.extend(slip_paths)
            cap = _triad(
                f"已更新书签 {nref}「{pub['title']}」",
                [f"查看详情: ./gewu atlas --action slip-get --ref {vref} --note_ref {nref}", "直接向人类汇报"],
                "已校验 Slip Frontmatter Schema 并原子落盘。",
                terminal=True,
            )
            return 0, _envelope(True, "OK_SLIP_UPDATED", cap, data=pub), sealed_paths

        # slip-delete
        trash_reg = KekTrashRegistry(app_id, state_dir / "atlas-trash.enc", kek)
        res, slip_paths = engine_slip_delete(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            trash_reg=trash_reg,
            ws_root=ws_root,
            note_ref=str(args.get("note_ref") or "").strip() or None,
            note_refs=args.get("note_refs"),
            source_note_ref=str(args.get("source_note_ref") or "").strip() or None,
            marker_text=str(args.get("marker_text") or "").strip() or None,
            marker_id=str(args.get("marker_id") or "").strip() or None,
            apply=bool(args.get("apply", False)),
            ticket_id=ticket_id_arg,
            batch_key=str(args.get("batch_key") or "").strip() or None,
        )
        sealed_paths.extend(slip_paths)
        if res.get("requires_approval"):
            cap = _guard(
                "INPUT_REQUIRED",
                f"E_SLIP_DELETE_CONFIRM: 待删除 {res['target_count']} 条书签（涉及 {res['inbound_reference_files']} 篇引用文档共 {res['total_inbound_links']} 处双链），已拦截等待人类批准（未修改任何文件）",
                f"请向用户展示待删书签与受影响笔记后，附带 --apply true 重新执行以清除全部入向双链并移入回收站。",
                "GUARD: INPUT_REQUIRED | 需人类批准后执行清理",
            )
            return 1, _envelope(False, "E_SLIP_DELETE_CONFIRM", cap, data=res), sealed_paths
        cap = _triad(
            f"已批准清理 {res['deleted_count']} 条书签（回收句柄 {','.join(res['trash_refs'])}，已同步清除 {res['cleaned_files_count']} 篇文档中 {res['removed_links_count']} 处双链）",
            [f"执行体检: ./gewu atlas --action health-check --ref {vref}", "直接向人类汇报"],
            "已采用方案 A 完成双链净清理与托管软删，零断链残留。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_SLIP_DELETED", cap, data=res), sealed_paths

    if action.startswith("vfs-"):
        trash_reg = KekTrashRegistry(app_id, state_dir / "atlas-trash.enc", kek)
        if action == "vfs-create":
            title_val = str(args.get("title") or "").strip()
            if not title_val:
                raise VaultStoreError("E_PARAM_MISSING", "vfs-create 需要 --title <标题>", exit_code=1)
            res, vfs_paths = engine_vfs_create(
                vault_dir=vdir,
                vault_ref=vref,
                note_reg=note_reg,
                proj_reg=proj_reg,
                kek=kek,
                app_id=app_id,
                zone=str(args.get("zone") or "dropzone"),
                title=title_val,
                content=str(args.get("content") or ""),
                doc_type=str(args.get("doc_type") or ""),
                summary=str(args.get("summary") or ""),
                tags=args.get("tags"),
                project_ref=project_ref_arg,
                subdir=str(args.get("subdir") or "").strip() or None,
            )
            sealed_paths.extend(vfs_paths)
            cap = _triad(
                f"VFS 已独占创建笔记（句柄 {res['note_ref']}，分区 {res['zone']}）",
                [f"查看笔记: ./gewu atlas --action note-get --ref {vref} --note_ref {res['note_ref']}", "直接向人类汇报"],
                "已校验 O_EXCL 防覆盖与 AI-Zone 占位符红线。",
                terminal=False,
            )
            return 0, _envelope(True, "OK_VFS_CREATE", cap, data=res), sealed_paths

        if action == "vfs-move":
            nref = str(args.get("note_ref") or "").strip() or None
            sub_arg = str(args.get("subdir") or "").strip() or None
            zone_arg = str(args.get("zone") or "").strip() or None
            tzone_arg = str(args.get("target_zone") or ("" if sub_arg else (zone_arg or ""))).strip()
            tsub_arg = str(args.get("target_subdir") or "").strip() or None
            on_links_arg = str(args.get("on_links") or "cascade").strip() or "cascade"
            if not nref and not sub_arg:
                raise VaultStoreError("E_PARAM_MISSING", "vfs-move 需要 --note_ref <nte_01>，或同时传入 --zone <分区> --subdir <子目录>", exit_code=1)
            res, vfs_paths = engine_vfs_move(
                vault_dir=vdir,
                vault_ref=vref,
                note_reg=note_reg,
                proj_reg=proj_reg,
                kek=kek,
                app_id=app_id,
                note_ref=nref,
                target_zone=tzone_arg,
                project_ref=project_ref_arg,
                scope=scope_arg,
                new_title=str(args.get("title") or "").strip() or None,
                source_zone=zone_arg if sub_arg else None,
                subdir=sub_arg,
                target_subdir=tsub_arg,
                on_links=on_links_arg,
            )
            sealed_paths.extend(vfs_paths)
            is_folder = res.get("mode") == "folder-moved"
            dg_cnt = int(res.get("downgraded_links_count") or 0)
            cc_cnt = int(res.get("cascaded_count") or 0)
            if is_folder:
                msg = (
                    f"VFS 已迁移目录「{res['folder_name']}」（含 {res['moved_files_count']} 个文件；"
                    + (f"降级 {dg_cnt} 处引用为纯文本" if on_links_arg.lower() == "downgrade" else f"级联改写 {cc_cnt} 处引用")
                    + "）"
                )
                next_cmds = [f"检查全库链接: ./gewu atlas --action vfs-links --ref {vref}", "直接向人类汇报"]
            else:
                msg = (
                    f"VFS 已迁移笔记 {nref} 并"
                    + (f"将 {dg_cnt} 处引用降级为纯文本" if on_links_arg.lower() == "downgrade" else f"级联改写 {cc_cnt} 处引用")
                )
                next_cmds = [f"检查全库链接: ./gewu atlas --action vfs-links --ref {vref} --note_ref {nref}", "直接向人类汇报"]
            cap = _triad(
                msg,
                next_cmds,
                "已执行画布/同名消歧预检、正文护栏双通道改写或纯文本降级与失败回滚保护。",
                terminal=False,
            )
            return 0, _envelope(True, "OK_VFS_MOVE", cap, data=res), sealed_paths

        if action == "vfs-copy":
            nref = str(args.get("note_ref") or "").strip()
            if not nref:
                raise VaultStoreError("E_PARAM_MISSING", "vfs-copy 需要 --note_ref <nte_01>", exit_code=1)
            res, vfs_paths = engine_vfs_copy(
                vault_dir=vdir,
                vault_ref=vref,
                note_reg=note_reg,
                proj_reg=proj_reg,
                kek=kek,
                app_id=app_id,
                note_ref=nref,
                target_zone=str(args.get("target_zone") or args.get("zone") or "").strip(),
                project_ref=project_ref_arg,
                scope=scope_arg,
                new_title=str(args.get("title") or "").strip() or None,
            )
            sealed_paths.extend(vfs_paths)
            cap = _triad(
                f"VFS 已复制笔记 {nref} -> 新句柄 {res['note_ref']}",
                [f"查看副本: ./gewu atlas --action note-get --ref {vref} --note_ref {res['note_ref']}", "直接向人类汇报"],
                "已通过目标拓扑护栏、成果区准入及 O_EXCL 防覆盖校验。",
                terminal=False,
            )
            return 0, _envelope(True, "OK_VFS_COPY", cap, data=res), sealed_paths

        if action == "vfs-trash":
            res, vfs_paths = engine_vfs_trash(
                vault_dir=vdir,
                vault_ref=vref,
                note_reg=note_reg,
                trash_reg=trash_reg,
                proj_reg=proj_reg,
                note_ref=str(args.get("note_ref") or "").strip() or None,
                file_name=str(args.get("file_name") or "").strip() or None,
                zone=str(args.get("zone") or "").strip() or None,
                subdir=str(args.get("subdir") or "").strip() or None,
                project_ref=project_ref_arg,
                scope=scope_arg,
                on_links=str(args.get("on_links") or "").strip(),
                force=bool(args.get("force", False)),
                apply=bool(args.get("apply", False)),
            )
            sealed_paths.extend(vfs_paths)
            if res.get("requires_approval"):
                kind_label = "目录" if res.get("kind") == "dir" else "对象"
                cap = _guard(
                    "INPUT_REQUIRED",
                    f"E_VFS_DELETE_CONFIRM: 待软删{kind_label}「{res['name']}」存在 {res['referenced_count']} 处入向引用/画布连接，将全部降级为纯文本后再删改（未修改任何文件）",
                    "请向用户展示受影响引用文档后，附带 --on_links downgrade --apply true 重新执行以将双链降级为纯文本并移入回收站。",
                    "GUARD: INPUT_REQUIRED | 需人类批准后执行降级并删除",
                )
                return 1, _envelope(False, "E_VFS_DELETE_CONFIRM", cap, data=res), sealed_paths
            cap = _triad(
                f"已软删至受控回收站（句柄 {res['trash_ref']}，原引用 {res['referenced_count']} 处，降级 {res.get('downgraded_links_count', 0)} 处双链）",
                [f"查看/恢复回收站: ./gewu atlas --action vfs-untrash --ref {vref}", "直接向人类汇报"],
                "物理回收路径已由 KEK 句柄遮蔽，可用 vfs-untrash 恢复。",
                terminal=False,
            )
            return 0, _envelope(True, "OK_VFS_TRASH", cap, data=res), sealed_paths

        if action == "vfs-trash-list":
            res, vfs_paths = engine_vfs_trash_list(
                vault_dir=vdir,
                vault_ref=vref,
                trash_reg=trash_reg,
                query=str(args.get("query") or "").strip(),
                kind_filter=str(args.get("doc_type") or args.get("category") or "").strip() or None,
                offset=int(args.get("offset") or 0),
                limit=int(args.get("top_k") or 20),
            )
            sealed_paths.extend(vfs_paths)
            cap = _triad(
                f"回收站共 {res['total']} 项（当前列出 {res['count']} 项）[只读完成]",
                [
                    f"恢复笔记或目录: ./gewu atlas --action vfs-untrash --ref {vref} --trash_ref <trs_01>",
                    "直接向人类汇报现状",
                ],
                "物理回收路径已由 KEK 短句柄 trs_XX 遮蔽。",
                terminal=True,
            )
            return 0, _envelope(True, "OK_VFS_TRASH_LIST", cap, data=res), sealed_paths

        if action == "vfs-untrash":
            res, vfs_paths = engine_vfs_untrash(
                vault_dir=vdir,
                vault_ref=vref,
                note_reg=note_reg,
                proj_reg=proj_reg,
                trash_reg=trash_reg,
                kek=kek,
                app_id=app_id,
                trash_ref=str(args.get("trash_ref") or "").strip() or None,
                target_zone=str(args.get("target_zone") or args.get("zone") or "").strip() or None,
                project_ref=project_ref_arg,
                scope=scope_arg,
                query=str(args.get("query") or "").strip(),
                offset=int(args.get("offset") or 0),
                limit=int(args.get("top_k") or 20),
            )
            sealed_paths.extend(vfs_paths)
            is_list = res.get("mode") == "list"
            cap = _triad(
                f"回收站共 {res['total']} 项（当前列出 {len(res.get('items') or [])} 项）" if is_list else f"已从回收站恢复 {res['trash_ref']}",
                [f"恢复某项: ./gewu atlas --action vfs-untrash --ref {vref} --trash_ref trs_01 --target_zone dropzone"] if is_list else [f"查看笔记/库状态: ./gewu atlas --action note-list --ref {vref}", "直接向人类汇报"],
                "支持按 --query 过滤与 --target_zone 定向还原。",
                terminal=is_list,
            )
            return 0, _envelope(True, "OK_VFS_UNTRASH", cap, data=res), sealed_paths

        if action == "vfs-validate":
            res = engine_vfs_validate(vault_dir=vdir, vault_ref=vref)
            cap = _triad(
                f"VFS 全库拓扑与命名校验完成（状态: {'通过' if res['ok'] else '发现告警'}，告警 {len(res['violations'])} 处）",
                [f"扫描悬空双链: ./gewu atlas --action vfs-links --ref {vref}", "直接向人类汇报"],
                "已巡检顶层骨架、冻结区完整性、扁平目录不变量、AI-Zone 占位符及命名规范。",
                terminal=True,
            )
            return 0, _envelope(True, "OK_VFS_VALIDATE", cap, data=res), sealed_paths

        if action == "vfs-links":
            res = engine_vfs_links(
                vault_dir=vdir,
                vault_ref=vref,
                note_reg=note_reg,
                note_ref=str(args.get("note_ref") or "").strip() or None,
                kek=kek,
                app_id=app_id,
                cache_dir=state_dir / "notes_index",
            )
            doc_git = doc_git_status_summary(state_dir, vref, vdir, note_reg=note_reg)
            res["doc_git"] = doc_git
            orbit = [f"查看全库图谱报告: ./gewu atlas --action graph-report --ref {vref}", "直接向人类汇报"]
            if int(doc_git.get("dirty_count", 0)) > 0:
                if int(res.get("dangling_count", 0)) == 0:
                    orbit.insert(0, f"116 状态 4 人工改动收口: ./gewu atlas --action doc-commit --ref {vref} --mode human-sync")
                else:
                    orbit.insert(0, "严禁恢复人类已删除文件；请修复引用方卡断链后再 doc-commit --mode human-sync")
            cap = _triad(
                f"VFS 链接健壮性巡检完成（悬空断链 {res['dangling_count']}，同名歧义 {res['ambiguous_count']}，DOC待提交={doc_git.get('dirty_count', 0)}）",
                orbit,
                "按句柄遮蔽路径返回断链与同名消歧巡检结果；人类删除文件引发的断链须通过清理引用方解决，严禁反向恢复被删文件。",
                terminal=True,
            )
            return 0, _envelope(True, "OK_VFS_LINKS", cap, data=res), sealed_paths

    if action == "doc-status":
        fetch_flag = bool(args.get("fetch", False))
        git_state = inspect_vault_doc_git_state(state_dir, vref, vdir, note_reg=note_reg, fetch=fetch_flag)
        link_res = engine_vfs_links(vault_dir=vdir, vault_ref=vref, note_reg=note_reg, kek=kek, app_id=app_id, cache_dir=state_dir / "notes_index")
        blocking_broken, ref_notes = _blocking_broken_links(link_res, note_reg, vref)
        dangling_cnt = len(blocking_broken)
        raw_dangling_cnt = int(link_res.get("dangling_count", 0))
        dirty_cnt = int(git_state.get("dirty_count", 0))
        sync_ready = bool(git_state.get("initialized")) and dangling_cnt == 0
        remote_info = git_state.get("remote_sync") if isinstance(git_state.get("remote_sync"), dict) else {}
        remote_tag = ""
        if remote_info.get("configured"):
            remote_tag = (
                f"，远端={remote_info.get('branch')}(auto_push={remote_info.get('auto_push')},"
                f"+{remote_info.get('ahead', 0)}/-{remote_info.get('behind', 0)})"
            )
        else:
            remote_tag = "，远端=未绑定"
        if dirty_cnt == 0:
            rec_state = "CLEAN"
            orbit = [f"查看 Vault 全库体检: ./gewu atlas --action health-check --ref {vref}", "直接向人类汇报现状"]
            if remote_info.get("configured") and (int(remote_info.get("ahead", 0)) > 0 or int(remote_info.get("behind", 0)) > 0 or remote_info.get("unrelated_histories")):
                orbit.insert(0, f"执行远端同步: ./gewu atlas --action doc-sync --ref {vref}")
            elif not remote_info.get("configured"):
                orbit.insert(0, f"绑定 GitHub 远端: ./gewu atlas --action doc-remote --ref {vref} --remote_url <url>")
            state_desc = f"Vault {vref} DOC Git 工作树干净（HEAD={git_state.get('head_short') or 'init'}，零未提交修改{remote_tag}）[只读完成]"
        elif dangling_cnt == 0:
            rec_state = "STATE_4_HUMAN_SYNC_READY"
            orbit = [
                f"116 状态 4 收口并保持 Git 树干净: ./gewu atlas --action doc-commit --ref {vref} --mode human-sync",
                "直接向人类汇报现状",
            ]
            if remote_info.get("configured"):
                orbit.insert(1, f"双向同步远端: ./gewu atlas --action doc-sync --ref {vref}")
            state_desc = (
                f"Vault {vref} 检测到 {dirty_cnt} 项未提交变更（+{git_state.get('added_count', 0)}/~{git_state.get('modified_count', 0)}/"
                f"-{git_state.get('deleted_count', 0)}/R{git_state.get('renamed_count', 0)}）且知识卡零断链（116 状态 4 可直接收口{remote_tag}）[只读完成]"
            )
        else:
            rec_state = "STATE_2_REPAIR_BEFORE_SYNC"
            orbit = [
                f"排查断链引用方: ./gewu atlas --action vfs-links --ref {vref}",
                "严禁恢复人类删除文件；先清理引用方失效双链后再执行 doc-commit --mode human-sync",
                "直接向人类汇报现状",
            ]
            state_desc = (
                f"Vault {vref} 检测到 {dirty_cnt} 项未提交变更且存在 {dangling_cnt} 处悬空断链（116 状态 2，严禁恢复已删文件{remote_tag}）[只读完成]"
            )
        pub = dict(git_state)
        pub.update(
            {
                "vault_ref": vref,
                "dangling_count": dangling_cnt,
                "raw_dangling_count": raw_dangling_cnt,
                "referencing_note_refs": ref_notes[:20],
                "ambiguous_count": int(link_res.get("ambiguous_count", 0)),
                "sync_ready": sync_ready,
                "reconcile_state": rec_state,
                "anti_restore_policy": "never_restore_human_deleted_or_edited_files",
                "path_sealed": True,
            }
        )
        cap = _triad(
            state_desc,
            orbit,
            "严格尊重并保护人类手工增改删行为；所有变更项仅以逻辑句柄与分区汇总返回，不暴露物理路径。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_DOC_STATUS", cap, data=pub), sealed_paths

    if action == "note-create":
        pub, note_paths = engine_note_create(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            zone=str(args.get("zone") or ""),
            doc_type=str(args.get("doc_type") or ""),
            title=str(args.get("title") or ""),
            summary=str(args.get("summary") or ""),
            tags=args.get("tags"),
            aliases=args.get("aliases"),
            content=str(args.get("content") or ""),
            status=str(args.get("status") or ""),
            proj_reg=proj_reg,
            project_ref=project_ref_arg,
            private=bool(args.get("private", False)),
            subdir=str(args.get("subdir") or "").strip() or None,
            by_actor=str(args.get("by") or ""),
            cache_dir=state_dir / "notes_index",
        )
        sealed_paths.extend(note_paths)
        scope_hint = f"，归属课题 {pub['project_id']}" if pub.get("project_id") else ""
        cap = _triad(
            f"已在 Vault {vref} 创建笔记（句柄 {pub['note_ref']}，分区 {pub['zone']}{scope_hint}）",
            [f"查看笔记: ./gewu atlas --action note-get --ref {vref} --note_ref {pub['note_ref']}", "直接向人类汇报现状"],
            "分区路径与日志挂载已由运行时按逻辑句柄完成。",
            terminal=False,
        )
        return 0, _envelope(True, "OK_NOTE_CREATED", cap, data=pub), sealed_paths

    if action == "inbox-list":
        if project_ref_arg:
            raise VaultStoreError("E_PARAM_INVALID", "inbox-list列当前库收件箱，不接受project_ref过滤；课题笔记用note-list", exit_code=1)
        result = engine_inbox_list(vault_dir=vdir, vault_ref=vref, offset=int(args.get("offset") or 0), top_k=int(args.get("top_k") or 100))
        cap = _triad(f"收件箱共 {result['total']} 项原料 [只读完成]",
                     ["按实际file_name调用fmt；普通Markdown用note-list获取句柄后note-route", "直接汇报清单"],
                     "包含二进制附件；不返回物理目录。同名歧义项不自动处理。", terminal=True)
        return 0, _envelope(True, "OK_INBOX_LIST", cap, data=result), sealed_paths

    if action == "note-list":
        items, note_paths = engine_note_list(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            zone_filter=args.get("zone"),
            doc_type_filter=args.get("doc_type"),
            status_filter=args.get("status"),
            proj_reg=proj_reg,
            project_ref=project_ref_arg,
        )
        sealed_paths.extend(note_paths)
        cap = _triad(
            f"已从 Vault {vref} 列出 {len(items)} 条逻辑笔记句柄 [只读完成]",
            [f"读取单卡: ./gewu atlas --action note-get --ref {vref} --note_ref <nte_01>", "直接向人类汇报现状"],
            "接口按逻辑句柄 nte_XX 与语义元数据返回（跨分区迁移不失效）；如需本地查看文件请由用户在 Finder / Obsidian 打开。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_NOTE_LIST", cap, data={"vault_ref": vref, "count": len(items), "notes": items, "path_sealed": True}), sealed_paths

    if action == "note-search":
        top_k = int(args.get("top_k") or 10)
        pid_filter = (
            str(
                resolve_domain_scope(
                    vault_dir=vdir,
                    vault_ref=vref,
                    proj_reg=proj_reg,
                    explicit_project_ref=project_ref_arg,
                    scope="project",
                )["project_id"]
            )
            if project_ref_arg
            else None
        )
        ex_zones = [z.strip() for z in str(args.get("exclude_zones") or "").split(",") if z.strip()] or None
        ex_pids = [
            str(
                resolve_domain_scope(
                    vault_dir=vdir,
                    vault_ref=vref,
                    proj_reg=proj_reg,
                    explicit_project_ref=pr.strip(),
                    scope="project",
                )["project_id"]
            )
            for pr in str(args.get("exclude_project_refs") or "").split(",")
            if pr.strip()
        ] or None
        hits, note_paths = engine_note_search(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            query=str(args.get("query") or ""),
            terms_arg=args.get("terms"),
            zone_filter=args.get("zone"),
            project_id_filter=pid_filter,
            doc_type_filter=args.get("doc_type"),
            epistemic_type_filter=args.get("epistemic_type"),
            status_filter=args.get("status"),
            exclude_zones=ex_zones,
            exclude_project_ids=ex_pids,
            include_archived=bool(args.get("include_archived", False)),
            include_private=bool(args.get("private", False)),
            exclude_pending=bool(args.get("exclude_pending", False)),
            exclude_draft=bool(args.get("exclude_draft", False)),
            mode=args.get("mode"),
            deep=bool(args.get("deep", False)),
            hops=args.get("hops"),
            boost=str(args.get("boost") or "hybrid"),
            alpha=float(args["alpha"]) if args.get("alpha") is not None else 0.15,
            beta=float(args["beta"]) if args.get("beta") is not None else 0.08,
            min_score=int(args.get("min_score") or 0),
            snippet_width=int(args.get("snippet_width") or 160),
            summary_width=int(args.get("summary_width") or 0),
            no_snippet=bool(args.get("no_snippet", False)),
            no_cache=bool(args.get("no_cache", False)),
            with_content=bool(args.get("with_content", False)),
            fields_arg=args.get("fields"),
            link_fields_arg=args.get("link_fields"),
            cache_dir=state_dir / "search_cache",
            top_k=top_k,
        )
        sealed_paths.extend(note_paths)
        eff_s_mode = (str(args.get("mode") or "").strip().lower()) or ("deep" if bool(args.get("deep", False)) else "passage")
        if not hits:
            qa_tag = "[QA_GATE: ZERO_HITS]"
            qa_anchor = "当前库检索 0 命中。[QA_GATE: ZERO_HITS] 可加 --all_vaults true 或用 note-list 排查；若确无资料须明确声明缺口，严禁瞎编库内事实。"
            qa_orbit = [f"改用跨库检索或 note-list: ./gewu atlas --action note-list --ref {vref}", "向人类明确声明库内暂无资料"]
        elif eff_s_mode == "locate":
            qa_tag = "[QA_GATE: LOCATE_ONLY]"
            qa_anchor = "定位模式仅返回位置句柄、类型、摘要与更新日期；如需回答内容问题，请改用 --mode passage 或调 note-get --note_refs 读正文。"
            qa_orbit = [f"改用段落检索直接作答: ./gewu atlas --action note-search --ref {vref} --mode passage --query '<关键词>'", f"批量实读命中卡正文: ./gewu atlas --action note-get --ref {vref} --note_refs <nte_01,nte_02>"]
        elif eff_s_mode == "deep":
            qa_tag = "[QA_GATE: DEEP_GRAPH]"
            qa_anchor = "深度模式已返回摘要及入链/出链 Top 3（in_links_top3 / out_links_top3）；可顺高分入出链句柄调用 note-get --note_refs 下钻溯源。"
            qa_orbit = [f"顺高分入出链实读正文: ./gewu atlas --action note-get --ref {vref} --note_refs <nte_01,nte_02>", "直接向人类汇报图谱脉络"]
        else:
            qa_tag = "[QA_GATE: PASSAGE_READY]"
            qa_anchor = "已返回命中完整段落（passage）与归一化分数（score）。常规问答可直接基于命中段与 nte_XX 句柄作答，无需再读全文；仅当 passage_truncated=true 或需跨卡深度综合时，再按需调 note-get --note_refs 补读。"
            qa_orbit = ["直接基于命中完整段落与 nte_XX 句柄循证作答", f"如需完整上下文再补读: ./gewu atlas --action note-get --ref {vref} --note_refs <nte_01,nte_02>"]
        cap = _triad(
            f"已在 Vault {vref} 完成检索（mode={eff_s_mode}），命中 {len(hits)} 条 {qa_tag} [只读完成]",
            qa_orbit,
            qa_anchor,
            terminal=True,
        )
        return 0, _envelope(True, "OK_NOTE_SEARCH", cap, data={"vault_ref": vref, "mode": eff_s_mode, "count": len(hits), "hits": hits, "path_sealed": True}), sealed_paths

    if action == "health-check":
        strict_flag = bool(args.get("strict", False))
        report, note_paths = engine_health_check(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            strict=strict_flag,
        )
        doc_git = doc_git_status_summary(state_dir, vref, vdir, note_reg=note_reg)
        report["doc_git"] = doc_git
        sealed_paths.extend(note_paths)
        orbit = [f"执行严格字典防污染巡检: ./gewu atlas --action schema-check --ref {vref}", "修复异常卡或直接向人类汇报体检结果"]
        if int(doc_git.get("dirty_count", 0)) > 0:
            orbit.insert(
                0,
                f"116 DOC 轨状态检查/收口: ./gewu atlas --action doc-status --ref {vref}"
                if int(report.get("broken_wikilink_count", 0)) > 0
                else f"116 状态 4 人工改动收口: ./gewu atlas --action doc-commit --ref {vref} --mode human-sync",
            )
        cap = _triad(
            f"Vault {vref} 体检完成：笔记={report['total_notes']}，Schema异常={report['schema_issue_count']}，未登记字段={report['unknown_field_issue_count']}，位置错配={report['zone_mismatch_issue_count']}，断链={report['broken_wikilink_count']}，DOC未提交={doc_git.get('dirty_count', 0)} [只读完成]",
            orbit,
            "体检基于 GewuAtlas/dicts/ 权威字典执行，异常全部按 nte_XX 句柄归集；严禁恢复人类手工删除或修改过的文件。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_HEALTH_CHECK", cap, data=report), sealed_paths

    if action == "schema-check":
        strict_flag = bool(args.get("strict", True))
        target_nref = str(args.get("note_ref") or "").strip() or None
        report, note_paths = engine_schema_check(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            note_ref=target_nref,
            strict=strict_flag,
        )
        sealed_paths.extend(note_paths)
        cap = _triad(
            f"Vault {vref} YAML 字典核查巡检完成（检查 {report['checked_notes']} 张，Schema={report['schema_issue_count']}，未知污染字段={report['unknown_field_issue_count']}，路径错配={report['zone_mismatch_issue_count']}，洁净={report['clean']}）[只读完成]",
            [f"查看权威字典规范: ./gewu atlas --action schema-list", "使用 note-update / vfs-move 修复告警卡或直接向人类汇报"],
            "基于 GewuAtlas/dicts/metadata.yaml 与 layout.yaml 校验必填属性、枚举词汇、正则格式、未登记污染字段与分区路径一致性。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_SCHEMA_CHECK", cap, data=report), sealed_paths

    if action == "delivery-check":
        report, dc_paths = _evaluate_delivery_gate(
            ws_root=ws_root,
            state_dir=state_dir,
            vdir=vdir,
            vref=vref,
            note_reg=note_reg,
            proj_reg=proj_reg,
            kek=kek,
            project_ref=project_ref_arg,
            note_ref_arg=str(args.get("note_ref") or "").strip() or None,
            note_refs_arg=args.get("note_refs"),
            actor=str(args.get("by") or args.get("agent") or "ai").strip() or "ai",
            batch_key=str(args.get("batch_key") or ticket_id_arg or "").strip(),
            session_fp=session_fingerprint(str(args.get("session") or os.environ.get("GEWU_ATLAS_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or "stateless")),
        )
        sealed_paths.extend(dc_paths)
        score = report["operator_score"]
        passed = report["operator_passed"]
        if passed:
            cap = _triad(
                f"Vault {vref} 交付闸门算子硬评估通过（得分 {score}/60，空间净空/Schema/双链真值/审批边界全绿）",
                [
                    "若具备 Subagent 能力：唤起只读 Subagent 按 semantic_evaluation.review_prompt 逐件盲审并核对跨卡证据链（满分 40 分），单列语义 PASS/FAIL，硬评与语义均通过方可交付",
                    "若无 Subagent 能力：对照 semantic_evaluation.review_prompt 逐件自评并核对跨卡证据链（完整性/真实性/可读性），按同一标准记录语义 PASS/FAIL，两项均通过方可交付",
                ],
                "算子 60 分硬底线已签发 PASS；请完成剩余 40 分语义质量评估（Subagent 优先，自评降级）后正式交付。",
                terminal=True,
            )
        else:
            cap = _triad(
                f"Vault {vref} 交付闸门算子评估未过及格线（得分 {score}/60，发现 {len(report['issues'])} 项阻塞问题）[待整改]",
                [
                    f"按 issues 列表清理野目录、修复 Schema 或执行 ./gewu atlas --action note-link --ref {vref} --mode fix 修复断链",
                    f"整改后重新验货: ./gewu atlas --action guard --ref {vref} --mode delivery",
                ],
                f"🚫 算子硬底线未通过（{'; '.join(report['issues'])}），严禁带病交付！请先按指引修复至 60/60。",
                terminal=False,
            )
        return 0, _envelope(True, "OK_DELIVERY_CHECK", cap, data=report), sealed_paths

    if action == "journal-today":
        res, note_paths = engine_journal_today(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            date=str(args.get("date") or "").strip() or None,
        )
        sealed_paths.extend(note_paths)
        state_lbl = "已新建今日双区日志" if res["created"] else "已读取今日日志"
        cap = _triad(
            f"{state_lbl}（日期 {res['date']}，句柄 {res['journal_ref']}）",
            [
                f"追加 AI 日志条目: ./gewu atlas --action journal-append --ref {vref} --content <记录>",
                "直接向人类汇报现状",
            ],
            "采用 # 人类记录 与 # AI日志 分区模板，自动写入完整隔离人类手写内容。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_JOURNAL_TODAY", cap, data=res), sealed_paths

    if action == "journal-show":
        res, note_paths = engine_journal_show(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            date=str(args.get("date") or "").strip() or None,
        )
        sealed_paths.extend(note_paths)
        cap = _triad(
            f"已只读读取日期 {res['date']} 日志（句柄 {res['journal_ref']}，归档={res['archived']}）[只读完成]",
            [
                f"追加日志条目: ./gewu atlas --action journal-append --ref {vref} --content <记录>",
                "直接向人类汇报现状",
            ],
            "已同步解析人类记录与 AI 日志分区。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_JOURNAL_SHOW", cap, data=res), sealed_paths

    if action == "journal-archive":
        raw_days = args.get("days")
        days_val = int(raw_days) if raw_days is not None else int(args.get("level") or 14)
        res, note_paths = engine_journal_archive(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            days=days_val,
            dry_run=bool(args.get("dry_run", False)),
        )
        sealed_paths.extend(note_paths)
        mode_lbl = "演练预览" if res["dry_run"] else "已归档"
        cap = _triad(
            f"日志按月冷归档{mode_lbl}：阈值 {res['cutoff_date']} 前共 {res['archived_count']} 篇（保留近期 {res['skipped_recent_count']} 篇）",
            [
                f"查询某日归档日志: ./gewu atlas --action journal-show --ref {vref} --date <YYYY-MM-DD>",
                "直接向人类汇报现状",
            ],
            "历史日志严格遵循「只归档不删除」按 05-Journal/YYYY/MM/ 结构冷存，自动补全 journal 属性且不改写人类正文。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_JOURNAL_ARCHIVED", cap, data=res), sealed_paths

    if action == "journal-append":
        res, note_paths = engine_journal_append(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            content=str(args.get("content") or ""),
            target_note_ref=str(args.get("note_ref") or "").strip() or None,
        )
        sealed_paths.extend(note_paths)
        cap = _triad(
            f"已记录今日格物日志（日志句柄 {res['journal_ref']}）",
            [f"查看日志正文: ./gewu atlas --action note-get --ref {vref} --note_ref {res['journal_ref']}", "直接向人类汇报现状"],
            "日志已按日期自动追加至当日日志卡片并更新图谱关联。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_JOURNAL_APPENDED", cap, data=res), sealed_paths

    if action == "graph-report":
        res, note_paths = engine_graph_report(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            zone_filter=args.get("zone"),
        )
        sealed_paths.extend(note_paths)
        cap = _triad(
            f"Vault {vref} 图谱与代谢统计：总卡={res['total_notes']}，双向关联边={res['edge_count']}，孤岛卡={res['orphan_count']}，待分拣={res['inbox_triage_count']} [只读完成]",
            [f"连接孤岛卡: ./gewu atlas --action note-link --ref {vref} --note_ref <nte_01> --target_ref <nte_02>", "直接向人类汇报现状"],
            "知识图谱节点与待分拣队列均按逻辑句柄（nte_XX）聚合，可直接衔接阅读、链接或升阶。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_GRAPH_REPORT", cap, data=res), sealed_paths

    if action == "moc-sync":
        if str(args.get("mode") or "").strip().lower() == "roam":
            min_sz = int(args.get("top_k") or 3) if args.get("top_k") is not None else 3
            res, note_paths = engine_moc_roam_discover(
                vault_dir=vdir,
                vault_ref=vref,
                note_reg=note_reg,
                proj_reg=proj_reg,
                project_ref=project_ref_arg,
                scope=scope_arg,
                min_cluster_size=min_sz,
            )
            sealed_paths.extend(note_paths)
            cc = int(res.get("candidate_count") or 0)
            cap = _triad(
                f"自主漫游建图扫描完成（未归属原子卡={res['unmapped_atomic_count']}，成熟候选簇={cc}）[只读完成]",
                [
                    f"顺枢纽卡漫游实读: ./gewu atlas --action note-get --ref {vref} --note_refs <member_refs>",
                    f"为成熟簇建图: ./gewu atlas --action moc-sync --ref {vref} --title '<主题>' --tag '<suggested_tag>'",
                ],
                "实读候选簇的来源，用 moc-sync 刷新附录，再用 note-patch 在附录前撰写文章，末章说明尚待验证的判断。",
                terminal=True,
            )
            return 0, _envelope(True, "OK_MOC_ROAM_DISCOVER", cap, data=res), sealed_paths
        res, note_paths = engine_moc_sync(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            title=str(args.get("title") or ""),
            name=args.get("name"),
            tag_filter=args.get("tag"),
            topic_filter=args.get("topic"),
            prism_filter=args.get("prism"),
            zone_filter=args.get("zone"),
            target_note_ref=str(args.get("note_ref") or "").strip() or None,
            proj_reg=proj_reg,
            project_ref=project_ref_arg,
            scope=scope_arg,
            by_actor=str(args.get("by") or ""),
            force=bool(args.get("force", False)),
            dry_run=bool(args.get("dry_run", False) or args.get("check", False)),
        )
        sealed_paths.extend(note_paths)
        if res.get("dry_run"):
            cap = _triad(
                f"MOC 预览「{res['title']}」（纳入={res['matched_count']}，map+={res['map_added_count']}/-={res['map_removed_count']}）[只读]",
                [f"写入 MOC: ./gewu atlas --action moc-sync --ref {vref} --title '{res['title']}'", "汇报现状"],
                "未写盘；已校验域边界、元数据与逻辑闭环结构信号。",
                terminal=True,
            )
            return 0, _envelope(True, "OK_MOC_SYNCED", cap, data=res), sealed_paths
        cap = _triad(
            f"MOC 已同步「{res['title']}」（{res['moc_ref']}，纳入={res['matched_count']}，map+={res['map_added_count']}/-={res['map_removed_count']}）",
            [
                res["authoring_instruction"],
                "核对正文引用与末章证据缺口后，再向人类汇报。",
            ],
            "用 note-patch 撰写或维护索引外正文。同步只确认附录刷新，文章质量由 Agent 审阅。",
            terminal=False,
        )
        return 0, _envelope(True, "OK_MOC_SYNCED", cap, data=res), sealed_paths

    if action == "note-route":
        res, note_paths = engine_note_route(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            proj_reg=proj_reg,
            kek=kek,
            app_id=app_id,
            note_ref=str(args.get("note_ref") or "").strip() or None,
            project_ref=project_ref_arg,
            scope=scope_arg,
            on_conflict=str(args.get("on_conflict") or "abort"),
            ws_root=ws_root,
            ticket_id=ticket_id_arg,
        )
        sealed_paths.extend(note_paths)
        if bool(args.get("auto_slice", False)) and not bool(args.get("dry_run", False)):
            _attach_auto_slices(
                res,
                vault_dir=vdir, vault_ref=vref, note_reg=note_reg, proj_reg=proj_reg, kek=kek, app_id=app_id,
                project_ref=project_ref_arg, scope=scope_arg, args=args, sealed_paths=sealed_paths,
            )
        cap = _triad(
            f"DropZone 分拣完成：已路由 {res['routed_count']} 篇、暂扣 {res['held_count']} 篇、冲突 {res['collision_count']} 篇",
            [f"查看文献列表: ./gewu atlas --action note-list --ref {vref} --zone library", "对暂扣条目指定 --project_ref 后重试"],
            "已自动补齐 library Frontmatter、保留/分配逻辑句柄并写入当日日志。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_NOTE_ROUTED", cap, data=res), sealed_paths

    if action == "clean-attachments":
        do_apply = bool(args.get("apply", False))
        trash_reg = KekTrashRegistry(app_id, state_dir / "atlas-trash.enc", kek)
        res, extra_paths = engine_clean_attachments(
            vault_dir=vdir,
            vault_ref=vref,
            apply=do_apply,
            trash_reg=trash_reg,
        )
        if do_apply:
            res["auto_purged_30d"] = engine_trash_purge(
                vault_dir=vdir,
                vault_ref=vref,
                trash_reg=trash_reg,
                days=30,
                dry_run=False,
            )
        sealed_paths.extend(extra_paths)
        mode_hint = "已移入隔离回收站" if do_apply else "只读盘点完成（传 --apply true 执行清理）"
        cap = _triad(
            f"Vault {vref} 孤岛附件与系统垃圾清扫：{mode_hint}（闲置附件={res['orphan_attachment_count']}，系统垃圾={res['junk_count']}）",
            [f"执行清理: ./gewu atlas --action clean-attachments --ref {vref} --apply true"] if not do_apply else ["直接向人类汇报清理结果"],
            "已保留所有被正文/Canvas/.base 引用的附件，未泄露具体文件名。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_CLEAN_ATTACHMENTS", cap, data=res), sealed_paths

    if action == "archive-list":
        res, arc_paths = engine_archive_list(
            vault_dir=vdir,
            vault_ref=vref,
            proj_reg=proj_reg,
            project_ref=project_ref_arg,
        )
        sealed_paths.extend(arc_paths)
        cap = _triad(
            f"Vault {vref} 冷归档舱原件盘点完成（共 {res['count']} 件，累计 {res['total_bytes']} 字节）[只读完成]",
            [f"查看文献库: ./gewu atlas --action note-list --ref {vref} --zone library", "直接向人类汇报现状"],
            "已汇总 AI-Zone 与各课题 98-Archives 待冷备原件清单（物理路径已屏蔽）。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_ARCHIVE_LIST", cap, data=res), sealed_paths

    if action == "archive-pack":
        res, arc_paths = engine_archive_pack(
            vault_dir=vdir,
            vault_ref=vref,
            proj_reg=proj_reg,
            project_ref=project_ref_arg,
            month=str(args.get("month") or "").strip() or None,
            dry_run=bool(args.get("dry_run", False)),
            purge=bool(args.get("purge", False)),
        )
        sealed_paths.extend(arc_paths)
        mode_hint = "演练预览" if res["dry_run"] else "已封装"
        cap = _triad(
            f"Vault {vref} 冷归档月度封装{mode_hint}（共 {res['bundle_count']} 个包，涵盖 {res['packed_file_count']} 件原件，清理散件 {res['purged_file_count']} 件）",
            [
                f"查看当前冷归档清单: ./gewu atlas --action archive-list --ref {vref}",
                "直接向人类汇报现状",
            ],
            "封包附带 SHA-256 清单与零符号链接校验；默认保留散件，传入 --purge true 可在封包成功后清理原散件。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_ARCHIVE_PACKED", cap, data=res), sealed_paths

    if action == "atomic-extract":
        ext_mode = str(args.get("mode") or "extract").strip().lower()
        if ext_mode in ("list-pending", "unextracted"):
            res, disc_paths = engine_extract_discovery(
                vault_dir=vdir,
                vault_ref=vref,
                note_reg=note_reg,
                proj_reg=proj_reg,
                kek=kek,
                app_id=app_id,
                mode=ext_mode,
                project_ref=project_ref_arg,
                include_done=bool(args.get("include_done", False)),
            )
            sealed_paths.extend(disc_paths)
            if ext_mode == "list-pending":
                disc_state = f"{vref} 待审原子卡 {res['count']} 条 [只读]"
            else:
                disc_state = f"{vref} 待萃取 {res['count']} 条（未开始={res.get('unstarted_count', 0)}/萃取中={res.get('in_progress_count', 0)}/已完={res.get('done_count', 0)}）[只读]"
            cap = _triad(
                disc_state,
                ["继续 atomic-extract 或 atomic-approve", "汇报现状"],
                "已返回 KEK 句柄列表。",
                terminal=True,
            )
            return 0, _envelope(True, "OK_EXTRACT_DISCOVERY", cap, data=res), sealed_paths

    if action == "workbench-preset":
        res, wb_paths = engine_apply_workbench_preset(
            vault_dir=vdir,
            vault_ref=vref,
            proj_reg=proj_reg,
            note_reg=note_reg,
            preset=str(args.get("preset") or "").strip() or None,
            project_ref=project_ref_arg,
            scope=scope_arg,
            dry_run=bool(args.get("dry_run", False)),
        )
        sealed_paths.extend(wb_paths)
        if res.get("mode") == "list":
            cap = _triad(
                f"已列出 {res['count']} 套工作台预设模板（multi-agent / engineering / academic / solo）[只读完成]",
                [f"应用到课题或 AI-Zone 工作台: ./gewu atlas --action workbench-preset --ref {vref} --preset <engineering|academic|multi-agent|solo> [--project_ref <prj_01>]"],
                "工作台内允许自由存放未加 YAML 的原始 note 笔记；切换预设不覆盖已有文件。",
                terminal=True,
            )
            return 0, _envelope(True, "OK_WORKBENCH_PRESET_LIST", cap, data=res), sealed_paths
        dry_lbl = "演练预览" if res["dry_run"] else "已搭建"
        cap = _triad(
            f"工作台预设「{res['preset_name']}」{dry_lbl}（新建 {res['created_dir_count']} 个工序子目录，README={res['readme_action']}）",
            [f"创建工作台草稿笔记: ./gewu atlas --action note-create --ref {vref} --zone workbench --title <标题> [--project_ref <prj_01>]"],
            "已按预设生成 L1 空间与 L2 工序目录及 README 协作公约，完整保留已有用户自定义内容。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_WORKBENCH_PRESET_APPLIED", cap, data=res), sealed_paths

    if action == "project-brief":
        res, b_paths = engine_project_brief(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            proj_reg=proj_reg,
            kek=kek,
            app_id=app_id,
            project_ref=project_ref_arg,
            include_slices=bool(args.get("include_slices", False)),
            max_notes=int(args.get("max_notes") or 200),
            cache_dir=state_dir / "notes_index",
        )
        sealed_paths.extend(b_paths)
        scope_lbl = f"课题 {project_ref_arg}" if project_ref_arg else f"知识库 {vref}"
        cap = _triad(
            f"已生成 {scope_lbl} 一屏全景图（共 {res['total_notes']} 张笔记，返回 {res['returned_notes']} 张核心卡，{res['edge_count']} 条双链边）[只读完成]",
            [
                f"按句柄批量精读: ./gewu atlas --action note-get --ref {vref} --note_refs <nte_01,nte_02>",
                f"跨卡精准 grep: ./gewu atlas --action note-grep --ref {vref} --pattern '<关键词>'",
                "直接向人类汇报现状",
            ],
            "单步汇聚分区统计、卡片句柄与双链骨架；切片默认仅计数以节省上下文（传 --include_slices true 展开）。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_PROJECT_BRIEF", cap, data=res), sealed_paths

    if action == "note-grep":
        res, g_paths = engine_note_grep(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            pattern=str(args.get("pattern") or args.get("query") or ""),
            regex=bool(args.get("regex", False)),
            case_sensitive=bool(args.get("case_sensitive", False)),
            context_lines=int(args["context_lines"]) if args.get("context_lines") is not None else 1,
            max_matches=int(args.get("max_matches") or 50),
            zone=args.get("zone"),
            project_ref=project_ref_arg,
            proj_reg=proj_reg,
            doc_type=args.get("doc_type"),
            private=bool(args.get("private", False)),
            include_archived=bool(args.get("include_archived", False)),
            include_workbench=bool(args.get("include_workbench", False)),
            with_section=bool(args.get("with_section", False)),
            cache_dir=state_dir / "notes_index",
        )
        sealed_paths.extend(g_paths)
        cap = _triad(
            f"跨卡精准检索完成（命中 {res['matched_notes']} 篇笔记共 {res['total_matches']} 处匹配）[只读完成]",
            [
                f"按行段精读命中卡: ./gewu atlas --action note-get --ref {vref} --note_ref <nte_01> --start_line 1 --end_line 80",
                "直接向人类汇报现状",
            ],
            "仅返回逻辑句柄与行号上下文，零物理路径暴露；默认遵守 private/attic/workbench 访问隔离。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_NOTE_GREP", cap, data=res), sealed_paths

    if action == "batch":
        _BATCH_ALLOWED = frozenset(
            {
                "note-get", "note-list", "note-search", "note-grep", "project-brief",
                "note-create", "note-update", "note-patch", "note-promote", "note-format",
                "note-slice", "atomic-extract", "moc-sync", "note-link", "artifact-adopt",
                "vfs-create", "vfs-move", "vfs-copy", "journal-append", "slip-commit", "slip-update",
                "health-check", "schema-check", "vfs-links",
            }
        )
        raw_steps = args.get("steps")
        if raw_steps is None and args.get("batch_json") is not None:
            bj = args.get("batch_json")
            try:
                parsed_bj = json.loads(str(bj)) if isinstance(bj, str) else bj
            except Exception as exc:
                raise VaultStoreError("E_PARAM_INVALID", f"--batch_json JSON 解析失败: {exc}", exit_code=1) from exc
            raw_steps = parsed_bj.get("steps") if isinstance(parsed_bj, dict) else parsed_bj
        elif isinstance(raw_steps, str):
            try:
                raw_steps = json.loads(raw_steps)
            except Exception as exc:
                raise VaultStoreError("E_PARAM_INVALID", f"--steps JSON 解析失败: {exc}", exit_code=1) from exc
        if not isinstance(raw_steps, list) or not raw_steps:
            raise VaultStoreError("E_PARAM_MISSING", "batch 需要提供非空 steps 数组（经 --steps、--batch_json 或 --json-stdin 传入）", exit_code=1)
        if len(raw_steps) > 50:
            raise VaultStoreError("E_PARAM_INVALID", "单次 batch 最多允许 50 个步骤", exit_code=1)

        on_error = str(args.get("on_error") or "stop").strip().lower()
        if on_error not in ("stop", "continue"):
            raise VaultStoreError("E_PARAM_INVALID", "--on_error 仅支持 stop 或 continue", exit_code=1)

        step_ref_re = re.compile(r"\$([A-Za-z_][A-Za-z0-9_-]*)\.([A-Za-z_][A-Za-z0-9_]*)")
        step_outputs: Dict[str, Dict[str, Any]] = {}

        def _resolve_batch_val(val: Any) -> Any:
            if isinstance(val, str):
                def _sub(m: re.Match[str]) -> str:
                    sid_ref, fld = m.group(1), m.group(2)
                    if sid_ref not in step_outputs or fld not in step_outputs[sid_ref]:
                        raise VaultStoreError("E_BATCH_REF_NOT_FOUND", f"batch 前序句柄引用无法解析: ${sid_ref}.{fld}", exit_code=1)
                    return str(step_outputs[sid_ref][fld])
                return step_ref_re.sub(_sub, val)
            if isinstance(val, list):
                return [_resolve_batch_val(x) for x in val]
            if isinstance(val, dict):
                return {k: _resolve_batch_val(v) for k, v in val.items()}
            return val

        results_log: List[Dict[str, Any]] = []
        stopped_at: Optional[str] = None
        requires_approval_hit = False
        succeeded = 0
        failed = 0
        prepared_steps = []
        step_positions = {}
        for idx, step in enumerate(raw_steps, start=1):
            if not isinstance(step, dict):
                raise VaultStoreError("E_PARAM_INVALID", f"batch 第 {idx} 步必须为对象", exit_code=1)
            sid_lbl = str(step.get("id") or f"step_{idx}").strip()
            if sid_lbl in step_positions:
                raise VaultStoreError("E_PARAM_INVALID", f"batch 第 {idx} 步与第 {step_positions[sid_lbl]} 步 ID 重复: {sid_lbl}", exit_code=1)
            step_positions[sid_lbl] = idx
            s_act = str(step.get("action") or "").strip()
            if s_act not in _BATCH_ALLOWED:
                raise VaultStoreError(
                    "E_BATCH_ACTION_FORBIDDEN",
                    f"batch 步骤 {sid_lbl} 的动作 '{s_act}' 不在批处理白名单内（高危/需单独人工拍板动作禁止混入 batch）",
                    exit_code=1,
                )
            s_args_raw = step.get("args", {})
            if not isinstance(s_args_raw, dict):
                raise VaultStoreError("E_PARAM_INVALID", f"batch 步骤 {sid_lbl} 的 args 必须为对象", exit_code=1)
            for key in s_args_raw:
                if str(key).replace("-", "_") not in KNOWN_SLOTS:
                    raise VaultStoreError("E_PARAM_UNKNOWN_FIELD", f"batch 步骤 {sid_lbl} 含未知字段: {key}", exit_code=2)
            prepared_steps.append((sid_lbl, s_act, s_args_raw))

        for sid_lbl, s_act, s_args_raw in prepared_steps:
            s_args: Dict[str, Any] = {
                "action": s_act,
                "ref": vref,
                "by": args.get("by"),
                "agent": args.get("agent"),
                "project_ref": project_ref_arg,
                "scope": scope_arg,
                "ticket_id": ticket_id_arg,
            }
            try:
                resolved_step_args = _resolve_batch_val(s_args_raw)
                for k, v in resolved_step_args.items():
                    nk = str(k).replace("-", "_")
                    slot_kind = KNOWN_SLOTS.get(nk)
                    if slot_kind == "bool" and not isinstance(v, bool):
                        v = _coerce_bool(v, nk)
                    elif slot_kind == "int" and v is not None and not isinstance(v, int):
                        try:
                            v = int(v)
                        except (ValueError, TypeError) as exc:
                            raise VaultStoreError("E_PARAM_INVALID", f"batch 步骤 {sid_lbl} 参数 --{nk} 必须为整数", exit_code=1) from exc
                    s_args[nk] = v
                s_args["_provided_keys"] = sorted(set(s_args.keys()))
                s_proj_ref = str(s_args.get("project_ref") or "").strip() or None
                s_scope = str(s_args.get("scope") or "auto").strip() or "auto"
                s_rc, s_env, s_sealed = _dispatch_vault_action(
                    action=s_act,
                    args=s_args,
                    ws_root=ws_root,
                    state_dir=state_dir,
                    vdir=vdir,
                    vref=vref,
                    kek=kek,
                    app_id=app_id,
                    note_reg=note_reg,
                    proj_reg=proj_reg,
                    project_ref_arg=s_proj_ref,
                    scope_arg=s_scope,
                    ticket_id_arg=ticket_id_arg,
                    sealed_paths=[],
                )
                for sp in s_sealed:
                    if sp not in sealed_paths:
                        sealed_paths.append(sp)
                s_data = s_env.get("data") if isinstance(s_env.get("data"), dict) else {}
                s_export = dict(s_data)
                if s_act in ("note-grep", "note-search", "note-list"):
                    hit_list = s_data.get("results") or s_data.get("notes") or s_data.get("hits") or []
                    if isinstance(hit_list, list):
                        found_refs = [
                            str(it.get("note_ref") or "").strip()
                            for it in hit_list
                            if isinstance(it, dict) and str(it.get("note_ref") or "").strip()
                        ]
                        uniq_refs = list(dict.fromkeys(found_refs))
                        s_export["note_refs"] = ",".join(uniq_refs)
                        if uniq_refs:
                            s_export["top_note_ref"] = uniq_refs[0]
                if s_rc == 0:
                    step_outputs[sid_lbl] = s_export
                s_entry: Dict[str, Any] = {
                    "id": sid_lbl,
                    "action": s_act,
                    "ok": s_rc == 0,
                    "code": s_env.get("code"),
                    "data": s_data,
                }
                results_log.append(s_entry)
                if s_rc == 0:
                    succeeded += 1
                else:
                    failed += 1
                if bool(s_data.get("requires_approval")):
                    requires_approval_hit = True
                    stopped_at = sid_lbl
                    break
                if s_rc != 0 and on_error == "stop":
                    stopped_at = sid_lbl
                    break
            except VaultStoreError as exc:
                failed += 1
                results_log.append(
                    {
                        "id": sid_lbl,
                        "action": s_act,
                        "ok": False,
                        "code": exc.code,
                        "error": exc.message,
                    }
                )
                hard_stop = exc.code in ("E_PRE_REVIEW_REQUIRED", "E_DOMAIN_FROZEN", "E_JOURNAL_PROTECTED", "E_SECURITY_VIOLATION")
                if hard_stop or on_error == "stop":
                    stopped_at = sid_lbl
                    break

        all_ok = (failed == 0) and (not requires_approval_hit)
        is_ro_batch = _is_read_only_batch(args)
        batch_data: Dict[str, Any] = {
            "vault_ref": vref,
            "read_only": is_ro_batch,
            "total_steps": len(raw_steps),
            "executed_steps": len(results_log),
            "succeeded": succeeded,
            "failed": failed,
            "stopped_at": stopped_at,
            "requires_approval": requires_approval_hit,
            "results": results_log,
            "path_sealed": True,
        }
        if requires_approval_hit:
            cap = _guard(
                "INPUT_REQUIRED",
                f"batch 在步骤 {stopped_at} 触发人工审批门禁（已完成 {succeeded}/{len(raw_steps)} 步）",
                "呈报人类确认后再继续后续步骤，严禁跳过审批。",
                "GUARD: INPUT_REQUIRED | batch 已在门禁处安全中止",
            )
            return 0, _envelope(True, "OK_BATCH_PAUSED_APPROVAL", cap, data=batch_data), sealed_paths
        if not all_ok:
            cap = _guard(
                "PARAM_FIX",
                f"batch 执行部分失败（成功 {succeeded}，失败 {failed}，停于 {stopped_at}）",
                "查看 results 中失败步骤的错误码修正后重试剩余步骤。",
                "已完成步骤均已落盘并记入统一变更句柄（可按需 undo）。",
            )
            return 1, _envelope(False, "E_BATCH_PARTIAL_FAILED", cap, data=batch_data), sealed_paths
        if is_ro_batch:
            cap = _triad(
                f"batch 只读合批完成（{succeeded}/{len(raw_steps)} 步全部成功）[只读完成]",
                ["按需继续精读或局部补丁改写", "直接向人类汇报现状"],
                "纯只读快路径：共享锁、零 DOC 票、零快照与零写闸门开销，进程内复用热索引。",
                terminal=True,
            )
        else:
            cap = _triad(
                f"batch 事务批处理完成（{succeeded}/{len(raw_steps)} 步全部成功）",
                ["按需继续或向人类汇报交付结果"],
                "整批共享单次状态锁、单张 DOC 票、单份合并变更快照与末尾合并验货。",
                terminal=True,
            )
        return 0, _envelope(True, "OK_BATCH_DONE", cap, data=batch_data), sealed_paths

    if action.startswith("project-"):
        return _handle_project_action(
            action=action,
            args=args,
            ws_root=ws_root,
            state_dir=state_dir,
            vdir=vdir,
            vref=vref,
            kek=kek,
            app_id=app_id,
            note_reg=note_reg,
            sealed_paths=sealed_paths,
        )

    note_ref = str(args.get("note_ref") or "").strip()
    if action == "note-get" and (args.get("note_refs") or "," in note_ref):
        batch_refs = args.get("note_refs") or note_ref
        batch_res, note_paths = engine_note_get_batch(
            vault_dir=vdir,
            vault_ref=vref,
            note_refs=batch_refs,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            raw=bool(args.get("raw", False)),
            start_line=args.get("start_line"),
            end_line=args.get("end_line"),
            head=args.get("head"),
            tail=args.get("tail"),
            section=args.get("section"),
            outline=bool(args.get("outline", False)),
            cache_dir=state_dir / "notes_index",
        )
        sealed_paths.extend(note_paths)
        if bool(args.get("outline", False)):
            cap = _triad(
                f"已批量提取 {batch_res['count']} 张笔记大纲与行号区间 [只读完成]",
                [f"按需拉取目标章节: ./gewu atlas --action note-get --ref {vref} --note_ref <nte_01> --section '<标题>'"],
                "outline 模式省略全文正文以极度节省上下文；如需回答细节请按 --section 或 --start_line/--end_line 精读。",
                terminal=True,
            )
        else:
            cap = _triad(
                f"已批量读取 {batch_res['count']} 张笔记 [QA_GATE: GROUNDED (60/60)] [只读完成]",
                ["熔炼成文并标注 nte_XX 句柄后向人类交付（L3 综合研究优先调 Subagent 盲审，无则按完整性/真实性/可读性自评）"],
                "单步返回多张卡片完整正文与 content_sha256。[QA_GATE: GROUNDED (60/60)] 请写成连贯分析文章（禁罗列搜索片段，禁露物理路径）。",
                terminal=True,
            )
        return 0, _envelope(True, "OK_NOTE_GET_BATCH", cap, data=batch_res), sealed_paths

    if action == "note-update" and (args.get("batch_json") or (args.get("note_refs") and not note_ref) or "," in note_ref):
        argv_keys = set(args.get("_provided_keys", args))
        batch_res, note_paths = engine_note_update_batch(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            batch_json=args.get("batch_json"),
            note_refs=args.get("note_refs") or (note_ref if "," in note_ref else None),
            title=args.get("title") if "title" in argv_keys else None,
            summary=args.get("summary") if "summary" in argv_keys else None,
            tags=args.get("tags") if "tags" in argv_keys else None,
            status=args.get("status") if "status" in argv_keys else None,
            content=args.get("content") if "content" in argv_keys else None,
            set_meta=args.get("set_meta") if "set_meta" in argv_keys else None,
            allow_managed_block=bool(args.get("allow_managed_block", False)),
            cache_dir=state_dir / "notes_index",
        )
        sealed_paths.extend(note_paths)
        cap = _triad(
            f"已批量事务更新 {batch_res['count']} 张笔记",
            [f"批量回读核验: ./gewu atlas --action note-get --ref {vref} --note_refs {','.join(batch_res['updated_refs'][:5])}", "直接向人类汇报现状"],
            "已在内存中预检全部目标卡片哈希与 YAML Schema 后集中原子落盘，并单次增量刷新加密索引。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_NOTE_UPDATED_BATCH", cap, data=batch_res), sealed_paths

    if action == "note-link" and str(args.get("mode") or "link").strip().lower() == "fix":
        res, note_paths = engine_fix_vault_links(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            proj_reg=proj_reg,
            note_ref=note_ref or None,
            note_refs=args.get("note_refs"),
            project_ref=project_ref_arg,
            dry_run=bool(args.get("dry_run", False)),
        )
        sealed_paths.extend(note_paths)
        cap = _triad(
            f"已完成批量双链修复（扫描 {res['scanned_files']} 篇，修复 {res['fixed_file_count']} 篇共 {res['fixed_link_count']} 处，剩余断链 {res['remaining_broken_count']} 处）",
            [f"校验全库链接: ./gewu atlas --action guard --ref {vref} --mode links", "直接向人类汇报现状"],
            "已自动识别 [[nte_XX]] / [[标题]] / 别名 / 空格 slug 并生成 Obsidian 表内与正文合法双链，无需手工脚本重写。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_LINKS_FIXED", cap, data=res), sealed_paths

    if action == "note-patch" and (args.get("note_refs") or "," in note_ref):
        argv_keys = set(args.get("_provided_keys", args))
        batch_refs = args.get("note_refs") or note_ref
        is_dry = bool(args.get("dry_run", False))
        bres, bpaths = engine_note_patch_batch(
            vault_dir=vdir,
            vault_ref=vref,
            note_refs=batch_refs,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            old_text=args.get("old_text") if "old_text" in argv_keys else None,
            new_text=args.get("new_text") if "new_text" in argv_keys else None,
            replace_all=bool(args.get("replace_all", False)),
            section=args.get("section") if "section" in argv_keys else None,
            mode=args.get("mode") if "mode" in argv_keys else None,
            content=args.get("content") if "content" in argv_keys else None,
            ops_json=args.get("ops_json") if "ops_json" in argv_keys else None,
            allow_managed_block=bool(args.get("allow_managed_block", False)),
            title=args.get("title") if "title" in argv_keys else None,
            summary=args.get("summary") if "summary" in argv_keys else None,
            tags=args.get("tags") if "tags" in argv_keys else None,
            status=args.get("status") if "status" in argv_keys else None,
            set_meta=args.get("set_meta") if "set_meta" in argv_keys else None,
            dry_run=is_dry,
            cache_dir=state_dir / "notes_index",
        )
        sealed_paths.extend(bpaths)
        verb = "预演跨卡补丁" if is_dry else "已完成跨卡批量补丁"
        code = "OK_NOTE_PATCH_BATCH_DRY_RUN" if is_dry else ("OK_NOTE_PATCH_BATCH" if bres["failed"] == 0 else "E_NOTE_PATCH_BATCH_PARTIAL")
        ok_flag = (bres["failed"] == 0)
        cap = _triad(
            f"{verb}（共 {bres['total']} 篇：命中 {bres['patched']}，跳过 {bres['skipped']}，受阻 {bres['failed']}）",
            ["确认无误后去掉 --dry_run 正式落盘" if is_dry else "按需核验或向人类汇报现状"],
            "逐卡校验 21-Library 原文只读、受控块与日记人类区保护；未命中目标文本的卡片自动跳过不中断。",
            terminal=True,
        )
        return (0 if ok_flag else 1), _envelope(ok_flag, code, cap, data=bres), sealed_paths

    if not note_ref:
        cap = _guard(
            "PARAM_FIX",
            f"E_PARAM_MISSING: {action} 需要 --note_ref <nte_01>",
            "请先调用 note-list 获取目标 --note_ref 句柄后重试。",
            "同指纹连续失败 2 次将触发硬熔断。",
            fill_template={"action": action, "ref": vref, "note_ref": "<nte_01>"},
            attempt_remaining=1,
        )
        return 1, _envelope(False, "E_PARAM_MISSING", cap, missing_slots=["note_ref"]), sealed_paths

    if action == "note-get":
        is_outline = bool(args.get("outline", False))
        pub, note_paths = engine_note_get(
            vault_dir=vdir,
            vault_ref=vref,
            note_ref=note_ref,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            raw=bool(args.get("raw", False)),
            start_line=args.get("start_line"),
            end_line=args.get("end_line"),
            head=args.get("head"),
            tail=args.get("tail"),
            section=args.get("section"),
            outline=is_outline,
            cache_dir=state_dir / "notes_index",
        )
        sealed_paths.extend(note_paths)
        if is_outline:
            cap = _triad(
                f"已提取笔记 {note_ref} 大纲（标题「{pub['title']}」，共 {len(pub.get('toc_outline') or [])} 个标题节，{pub.get('total_lines', 0)} 行）[只读完成]",
                [
                    f"按章节精读: ./gewu atlas --action note-get --ref {vref} --note_ref {note_ref} --section '<标题>'",
                    f"局部补丁改写: ./gewu atlas --action note-patch --ref {vref} --note_ref {note_ref} --section '<标题>' --content '<新正文>'",
                ],
                "outline 模式仅返 Frontmatter 与标题行号范围，避免长文撑爆上下文。",
                terminal=True,
            )
        else:
            cap = _triad(
                f"已读取笔记 {note_ref}（标题「{pub['title']}」，分区 {pub['zone']}）[QA_GATE: GROUNDED (60/60)] [只读完成]",
                [f"局部更新笔记: ./gewu atlas --action note-patch --ref {vref} --note_ref {note_ref} --old_text '<旧片段>' --new_text '<新片段>'", "直接向人类汇报现状"],
                "正文与正反向双链已通过逻辑句柄提取，无需关心具体子目录位置。[QA_GATE: GROUNDED (60/60)] 作答时请标注句柄并保持散文连贯、零物理路径。",
                terminal=True,
            )
        return 0, _envelope(True, "OK_NOTE_GET", cap, data=pub), sealed_paths

    if action == "note-patch":
        argv_keys = set(args.get("_provided_keys", args))
        is_dry = bool(args.get("dry_run", False))
        pub, note_paths = engine_note_patch(
            vault_dir=vdir,
            vault_ref=vref,
            note_ref=note_ref,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            old_text=args.get("old_text") if "old_text" in argv_keys else None,
            new_text=args.get("new_text") if "new_text" in argv_keys else None,
            replace_all=bool(args.get("replace_all", False)),
            section=args.get("section") if "section" in argv_keys else None,
            mode=args.get("mode") if "mode" in argv_keys else None,
            content=args.get("content") if "content" in argv_keys else None,
            ops_json=args.get("ops_json") if "ops_json" in argv_keys else None,
            allow_managed_block=bool(args.get("allow_managed_block", False)),
            title=args.get("title") if "title" in argv_keys else None,
            summary=args.get("summary") if "summary" in argv_keys else None,
            tags=args.get("tags") if "tags" in argv_keys else None,
            status=args.get("status") if "status" in argv_keys else None,
            set_meta=args.get("set_meta") if "set_meta" in argv_keys else None,
            expected_sha256=str(args.get("expected_sha256") or "").strip() or None,
            dry_run=is_dry,
            cache_dir=state_dir / "notes_index",
        )
        sealed_paths.extend(note_paths)
        ops_str = ", ".join(pub.get("patch_ops_applied") or [])
        if is_dry:
            cap = _triad(
                f"已预演补丁 {note_ref}（模拟应用 {ops_str}，字数 {pub.get('chars_before')}→{pub.get('chars_after')}，未落盘）[只读完成]",
                ["去掉 --dry_run 正式落盘", "直接向人类汇报现状"],
                "预演已通过受控块、只读区与 Schema 校验，零磁盘写入。",
                terminal=True,
            )
            return 0, _envelope(True, "OK_NOTE_PATCH_DRY_RUN", cap, data=pub), sealed_paths
        cap = _triad(
            f"已局部补丁更新笔记 {note_ref}（应用 {ops_str}，字数 {pub.get('chars_before')}→{pub.get('chars_after')}）",
            [f"按需查看大纲: ./gewu atlas --action note-get --ref {vref} --note_ref {note_ref} --outline true", "直接向人类汇报现状"],
            "已校验受控块保护、YAML Schema 与双链归属并原子写回，无需整篇重写。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_NOTE_PATCHED", cap, data=pub), sealed_paths

    if action == "note-update":
        argv_keys = set(args.get("_provided_keys", args))
        pub, note_paths = engine_note_update(
            vault_dir=vdir,
            vault_ref=vref,
            note_ref=note_ref,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            title=args.get("title") if "title" in argv_keys else None,
            summary=args.get("summary") if "summary" in argv_keys else None,
            tags=args.get("tags") if "tags" in argv_keys else None,
            status=args.get("status") if "status" in argv_keys else None,
            content=args.get("content") if "content" in argv_keys else None,
            set_meta=args.get("set_meta") if "set_meta" in argv_keys else None,
            expected_sha256=str(args.get("expected_sha256") or "").strip() or None,
            allow_managed_block=bool(args.get("allow_managed_block", False)),
            cache_dir=state_dir / "notes_index",
        )
        sealed_paths.extend(note_paths)
        cap = _triad(
            f"已更新笔记 {note_ref}（状态 {pub['status']}）",
            [f"查看更新后内容: ./gewu atlas --action note-get --ref {vref} --note_ref {note_ref}", "直接向人类汇报现状"],
            "已校验 YAML Schema 并原子写回，句柄保持不变。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_NOTE_UPDATED", cap, data=pub), sealed_paths

    if action == "note-promote":
        pub, note_paths = engine_note_promote(
            vault_dir=vdir,
            vault_ref=vref,
            note_ref=note_ref,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            target_zone=str(args.get("target_zone") or "atomics"),
            new_doc_type=args.get("doc_type"),
            on_conflict=str(args.get("on_conflict") or "abort"),
            proj_reg=proj_reg,
            project_ref=project_ref_arg,
            scope=scope_arg,
        )
        sealed_paths.extend(note_paths)
        scope_label = f"课题 {pub['project_id']} 域" if pub.get("domain_scope") == "project" else "全局 AI-Zone"
        cap = _triad(
            f"已将笔记 {note_ref} 升阶至 {scope_label} 的 {pub['zone']} 分区（级联更新 {pub['wikilink_cascaded_files']} 个双链文件）",
            [f"运行体检: ./gewu atlas --action health-check --ref {vref}", "直接向人类汇报现状"],
            f"升阶分区调整与全库双链重连已完成，后续仍直接沿用 {note_ref} 句柄操作即可。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_NOTE_PROMOTED", cap, data=pub), sealed_paths

    if action == "note-slice":
        level_val = int(args.get("level") or 2)
        res, note_paths = engine_note_slice(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            proj_reg=proj_reg,
            kek=kek,
            app_id=app_id,
            note_ref=note_ref,
            project_ref=project_ref_arg,
            scope=scope_arg,
            level=level_val,
            adaptive=bool(args.get("adaptive", False)),
            min_chars=args.get("min_chars"),
            max_chars=args.get("max_chars"),
            target_chars=args.get("target_chars"),
            prefix=args.get("prefix"),
        )
        sealed_paths.extend(note_paths)
        scope_label = f"课题 {res['project_id']}（24-Slices）" if res.get("domain_scope") == "project" else "全局 AI-Zone（24-Slices）"
        mode_desc = f"自适应 AST（{res['min_chars']}~{res['max_chars']} 字）" if res.get("mode") == "adaptive" else f"H{level_val}"
        cap = _triad(
            f"已将 {note_ref} 按 {mode_desc} 切分为 {res['slice_count']} 张切片并生成 TOC 导航（落点：{scope_label}）",
            [
                f"查看导航卡: ./gewu atlas --action note-get --ref {vref} --note_ref {res['toc_note_ref']}",
                f"查看同域笔记: ./gewu atlas --action note-list --ref {vref}",
            ],
            "所有切片与 TOC 均已按来源域自动归位并分配逻辑句柄，不泄露物理路径。",
            terminal=False,
        )
        return 0, _envelope(True, "OK_NOTE_SLICED", cap, data=res), sealed_paths

    if action == "atomic-extract":
        ext_mode = str(args.get("mode") or "extract").strip().lower()
        if ext_mode not in ("extract", "mark-done", "backfill-provenance", "repair-toc-ledger"):
            raise VaultStoreError(
                "E_PARAM_INVALID",
                f"atomic-extract 不支持 mode={ext_mode}，可选: extract / list-pending / unextracted / mark-done / backfill-provenance / repair-toc-ledger",
            )
        if ext_mode == "mark-done":
            pub, done_paths = engine_mark_source_done(
                vault_dir=vdir,
                vault_ref=vref,
                note_reg=note_reg,
                proj_reg=proj_reg,
                kek=kek,
                app_id=app_id,
                note_ref=note_ref,
                project_ref=project_ref_arg,
            )
            sealed_paths.extend(done_paths)
            rem_tocs = int(pub.get("pending_sibling_tocs") or 0)
            if "pending_sibling_tocs" in pub and rem_tocs > 0:
                md_state = f"已标完 TOC {note_ref}（余 {rem_tocs} 兄弟 TOC，原文未标完）"
                md_anchor = "同域兄弟 TOC 全完结后自动联动父原文 processed=true。"
            elif pub.get("parent_processed"):
                md_state = f"已标完 {note_ref} 并联动父原文 processed=true"
                md_anchor = "unextracted 将跳过该 TOC 及父原文。"
            else:
                md_state = f"已标完 {note_ref}（processed=true）"
                md_anchor = "unextracted 默认跳过。"
            cap = _triad(
                md_state,
                [f"查待萃取: ./gewu atlas --action atomic-extract --ref {vref} --mode unextracted", "汇报现状"],
                md_anchor,
                terminal=True,
            )
            return 0, _envelope(True, "OK_ATOMIC_MARKED_DONE", cap, data=pub), sealed_paths
        if ext_mode == "backfill-provenance":
            pub, bf_paths = engine_backfill_provenance(
                vault_dir=vdir,
                vault_ref=vref,
                note_reg=note_reg,
                proj_reg=proj_reg,
                kek=kek,
                app_id=app_id,
                note_ref=note_ref,
                project_ref=project_ref_arg,
                scope=scope_arg,
                by_actor=str(args.get("by") or ""),
            )
            sealed_paths.extend(bf_paths)
            cap = _triad(
                f"已回填 {note_ref} 溯源（groups={pub['provenance_group_count']}）",
                [f"回读原子卡: ./gewu atlas --action note-get --ref {vref} --note_ref {note_ref}", "汇报现状"],
                "已校验边界并更新 provenance/sources/toc/slices。",
                terminal=True,
            )
            return 0, _envelope(True, "OK_ATOMIC_PROVENANCE_BACKFILLED", cap, data=pub), sealed_paths
        if ext_mode == "repair-toc-ledger":
            pub, rep_paths = engine_repair_toc_ledger(
                vault_dir=vdir,
                vault_ref=vref,
                note_reg=note_reg,
                proj_reg=proj_reg,
                kek=kek,
                app_id=app_id,
                note_ref=note_ref,
                project_ref=project_ref_arg,
                scope=scope_arg,
            )
            sealed_paths.extend(rep_paths)
            cap = _triad(
                f"已校准 TOC {note_ref} 单向溯源统计（derived_atomics={pub['repaired_count']}）",
                [f"回读 TOC: ./gewu atlas --action note-get --ref {vref} --note_ref {note_ref}", "汇报现状"],
                "已按单向流动契约清理旧 extracted_to 并实时反查下游原子卡。",
                terminal=True,
            )
            return 0, _envelope(True, "OK_ATOMIC_LEDGER_REPAIRED", cap, data=pub), sealed_paths
        res, note_paths = engine_note_extract(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            proj_reg=proj_reg,
            kek=kek,
            app_id=app_id,
            note_ref=note_ref,
            project_ref=project_ref_arg,
            scope=scope_arg,
            title=str(args.get("title") or ""),
            summary=str(args.get("summary") or ""),
            epistemic_type=str(args.get("epistemic_type") or args.get("doc_type") or "SYN"),
            tags=args.get("tags"),
            aliases=args.get("aliases"),
            content=str(args.get("content") or ""),
            review_mode=str(args.get("review_mode") or "inherit"),
            slice_refs=args.get("slice_refs"),
            toc_refs=args.get("toc_refs"),
            provenance_arg=args.get("provenance"),
            by_actor=str(args.get("by") or ""),
            auto_approve=bool(args.get("auto_approve", False)),
        )
        sealed_paths.extend(note_paths)
        scope_label = f"课题 {res['project_id']}（22-Atomics）" if res.get("domain_scope") == "project" else "全局 AI-Zone（22-Atomics）"
        warn_msg = f" ⚠ {'; '.join(res['warnings'])}" if res.get("warnings") else ""
        cap = _triad(
            f"{note_ref} -> {res['extracted_count']} 张原子卡（{scope_label}）",
            [
                f"查原子卡: ./gewu atlas --action note-list --ref {vref} --zone atomics",
                "supervised 停待批准；auto 可核对后继续",
            ],
            f"已写单向 sources/slices 与日志（上游原文只读零回写）。{warn_msg}".strip(),
            terminal=False,
        )
        if res["review_mode"] == "supervised":
            cap = _guard("INPUT_REQUIRED", f"候选 pending（{res['extracted_count']}张）{warn_msg}", "呈报候选与出处，获批后传 expected_sha256 执行 atomic-approve。", "GUARD: INPUT_REQUIRED | 禁越权自批或织网")
        return 0, _envelope(True, "OK_ATOMIC_EXTRACTED", cap, data=res), sealed_paths

    if action == "atomic-approve":
        pub, note_paths = engine_note_approve(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            note_ref=note_ref,
            summary_override=args.get("summary"),
            proj_reg=proj_reg,
            project_ref=project_ref_arg,
            reviewer=str(args.get("reviewer") or "auto"),
            expected_sha256=str(args.get("expected_sha256") or ""),
        )
        sealed_paths.extend(note_paths)
        cap = _triad(
            f"已审核转正原子卡 {note_ref}（status=active，reviewed_by={pub['reviewed_by']}）",
            [f"查看原子卡: ./gewu atlas --action note-get --ref {vref} --note_ref {note_ref}", "直接向人类汇报现状"],
            ("已从待审状态转正并写入当日日志。" if pub.get("journal_status") == "recorded" else "原子卡已转正，但日志未确认；请核对回执，不得重投批准。"),
            terminal=True,
        )
        return 0, _envelope(True, "OK_ATOMIC_APPROVED", cap, data=pub), sealed_paths

    if action == "note-format":
        check_only = bool(args.get("check", False))
        pub, note_paths = engine_note_format(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            note_ref=note_ref,
            check_only=check_only,
        )
        sealed_paths.extend(note_paths)
        mode_hint = "结构检查完成" if check_only else ("已结构化写回" if pub["wrote"] else "结构已合规无需改写")
        cap = _triad(
            f"笔记 {note_ref} Markdown {mode_hint}（共修正 {pub['total_fixes']} 处结构空白/列表/标题层级）",
            [f"读取规范后正文: ./gewu atlas --action note-get --ref {vref} --note_ref {note_ref}", "直接向人类汇报现状"],
            "Frontmatter 与代码围栏内部原样保留，仅调整 Markdown 结构骨架。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_NOTE_FORMATTED", cap, data=pub), sealed_paths

    if action == "artifact-adopt":
        target_zone_arg = str(args.get("target_zone") or "artifacts").strip().lower()
        pub, note_paths = engine_artifact_adopt(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            proj_reg=proj_reg,
            kek=kek,
            app_id=app_id,
            note_ref=note_ref,
            project_ref=project_ref_arg,
            scope=scope_arg,
            target_zone=target_zone_arg,
            title_override=args.get("title"),
            summary_override=args.get("summary"),
            on_conflict=str(args.get("on_conflict") or "abort"),
            by_actor=str(args.get("by") or ""),
            ws_root=ws_root,
            ticket_id=ticket_id_arg,
        )
        sealed_paths.extend(note_paths)
        dest_lbl = "文献舱 (21-Library)" if pub.get("target_zone") == "library" else "成果舱 (10-Artifacts)"
        scope_label = f"课题 {pub['project_id']} {dest_lbl}" if pub.get("domain_scope") == "project" else f"AI-Zone {dest_lbl}"
        if pub.get("target_zone") == "library":
            cap = _triad(
                f"已采收 {note_ref} -> {scope_label}（library，级联={pub['wikilink_cascaded_files']}）",
                [f"切片: ./gewu atlas --action note-slice --ref {vref} --note_ref {note_ref}", f"回读: ./gewu atlas --action note-get --ref {vref} --note_ref {note_ref}"],
                "processed=false，句柄不变。",
                terminal=True,
            )
        else:
            cap = _triad(
                f"已将 {note_ref} 沉淀至 {scope_label}（stage=draft，级联 {pub['wikilink_cascaded_files']} 个双链文件）",
                [f"成果定稿: ./gewu atlas --action artifact-finalize --ref {vref} --note_ref {note_ref}", f"查看成果: ./gewu atlas --action note-get --ref {vref} --note_ref {note_ref}"],
                "已写入 stage=draft 与 Artifact 标准属性，句柄不变。",
                terminal=False,
            )
        return 0, _envelope(True, "OK_ARTIFACT_ADOPTED", cap, data=pub), sealed_paths

    if action == "artifact-finalize":
        pub, note_paths = engine_artifact_finalize(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            note_ref=note_ref,
            by_actor=str(args.get("by") or ""),
            ws_root=ws_root,
            ticket_id=ticket_id_arg,
        )
        sealed_paths.extend(note_paths)
        cap = _triad(
            f"Artifact 成果 {note_ref}（「{pub['title']}」）已定稿（stage=final）",
            [f"运行全库体检: ./gewu atlas --action health-check --ref {vref}", "直接向人类汇报现状"],
            "已在 05-Artifacts 看板标记为 final 定稿并写入当日日志。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_ARTIFACT_FINALIZED", cap, data=pub), sealed_paths

    if action == "note-link":
        res, note_paths = engine_note_link(
            vault_dir=vdir,
            vault_ref=vref,
            note_reg=note_reg,
            note_ref=note_ref,
            target_ref=str(args.get("target_ref") or "").strip(),
            alias=args.get("alias"),
        )
        sealed_paths.extend(note_paths)
        cap = _triad(
            f"已在 {note_ref} 与 {res['target_ref']} 之间建立双链关联",
            [f"查看图谱统计: ./gewu atlas --action graph-report --ref {vref}", "直接向人类汇报现状"],
            "标准 Obsidian 裸双链已写入并同步全库索引。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_NOTE_LINKED", cap, data=res), sealed_paths

    # note-deprecate
    argv_keys = set(args.get("_provided_keys", args))
    move_attic = bool(args.get("move_to_attic", True)) if "move_to_attic" in argv_keys else True
    pub, note_paths = engine_note_deprecate(
        vault_dir=vdir,
        vault_ref=vref,
        note_ref=note_ref,
        note_reg=note_reg,
        kek=kek,
        app_id=app_id,
        superseded_by_ref=args.get("superseded_by_ref"),
        move_to_attic=move_attic,
        proj_reg=proj_reg,
    )
    sealed_paths.extend(note_paths)
    cap = _triad(
        f"已废弃笔记 {note_ref}（当前分区 {pub['zone']}，状态 {pub['status']}）",
        [f"列出全部笔记: ./gewu atlas --action note-list --ref {vref}", "直接向人类汇报现状"],
        "废弃横幅、状态变更与句柄重绑已原子完成。",
        terminal=True,
    )
    return 0, _envelope(True, "OK_NOTE_DEPRECATED", cap, data=pub), sealed_paths


def _parse_str_list(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    s = str(raw).strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            loaded = json.loads(s)
            if isinstance(loaded, list):
                return [str(x).strip() for x in loaded if str(x).strip()]
        except Exception:
            pass
    return [x.strip() for x in s.split(",") if x.strip()]


def _handle_project_action(
    *,
    action: str,
    args: Dict[str, Any],
    ws_root: Path,
    state_dir: Path,
    vdir: Path,
    vref: str,
    kek: bytes,
    app_id: str,
    note_reg: KekNoteRegistry,
    sealed_paths: List[str],
) -> Tuple[int, Dict[str, Any], List[str]]:
    proj_reg = KekProjectRegistry(app_id, state_dir / "atlas-projects.enc", kek)

    if action == "project-create":
        pub, p_paths = engine_project_create(
            vault_dir=vdir,
            vault_ref=vref,
            proj_reg=proj_reg,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            title=str(args.get("title") or ""),
            project_id=args.get("project_id"),
            summary=args.get("summary"),
            tags=args.get("tags"),
            default_mode=str(args.get("default_mode") or "auto"),
            private=bool(args.get("private", False)),
            preset=str(args.get("preset") or "").strip() or None,
        )
        sealed_paths.extend(p_paths)
        agent_for_wb = str(args.get("agent") or args.get("by") or "").strip()
        if agent_for_wb:
            wb_res, wb_paths = engine_workbench_workspace(
                vault_dir=vdir,
                vault_ref=vref,
                proj_reg=proj_reg,
                note_reg=note_reg,
                kek=kek,
                app_id=app_id,
                project_ref=pub["project_ref"],
                scope="project",
                agent=agent_for_wb,
                topic=None,
                mode="allocate",
                dry_run=False,
            )
            sealed_paths.extend(wb_paths)
            pub["workspace"] = {
                "workspace_id": wb_res.get("workspace_id"),
                "subdirs": wb_res.get("subdirs"),
            }
        cap = _triad(
            f"已创建课题「{pub['title']}」（句柄 {pub['project_ref']}）",
            [
                f"申请工作台专属工作区: ./gewu atlas --action workbench-workspace --ref {vref} --project_ref {pub['project_ref']} --agent <name>",
                f"查看课题详情: ./gewu atlas --action project-get --ref {vref} --project_ref {pub['project_ref']}",
                "直接向人类汇报现状",
            ],
            "课题已按三舱位（10-Artifacts / 20-Memory / 30-Workbench + Manifest）展开，写草稿/分析前须先经 workbench-workspace 申请 ws-<agent> 目录，严禁在课题内自建 99-Assets。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_PROJECT_CREATED", cap, data=pub), sealed_paths

    if action == "project-list":
        items, p_paths = engine_project_list(
            vault_dir=vdir,
            vault_ref=vref,
            proj_reg=proj_reg,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            include_archived=bool(args.get("include_archived", False)),
        )
        sealed_paths.extend(p_paths)
        cap = _triad(
            f"已列出 Vault {vref} 内 {len(items)} 个课题（句柄 prj_XX）[只读完成]",
            [
                f"查看课题: ./gewu atlas --action project-get --ref {vref} --project_ref <prj_01>",
                "直接向人类汇报现状",
            ],
            "课题/主题统一通过 prj_XX 句柄检索与调度，不暴露物理目录。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_PROJECT_LIST", cap, data={"vault_ref": vref, "count": len(items), "projects": items, "path_sealed": True}), sealed_paths

    if action == "project-history":
        ops = engine_project_history(state_dir=state_dir, vault_ref=vref)
        cap = _triad(
            f"已查询 Vault {vref} 的课题合并/拆分历史记录（共 {len(ops)} 条）[只读完成]",
            [
                f"执行后悔药回滚: ./gewu atlas --action project-undo --ref {vref} --op_id <op_01>",
                "直接向人类汇报现状",
            ],
            "有历史前后像证明的快照可直接撤回；旧快照须先预览并确认恢复内容。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_PROJECT_HISTORY", cap, data={"vault_ref": vref, "count": len(ops), "operations": ops, "path_sealed": True}), sealed_paths

    if action == "project-undo":
        res, p_paths = engine_project_undo(
            ws_root=ws_root,
            state_dir=state_dir,
            vault_dir=vdir,
            vault_ref=vref,
            proj_reg=proj_reg,
            note_reg=note_reg,
            op_id=args.get("op_id"),
            mode=str(args.get("mode") or "standard"),
            dry_run=bool(args.get("dry_run", False)),
            expected_sha256=str(args.get("expected_sha256") or ""),
        )
        sealed_paths.extend(p_paths)
        if res.get("dry_run"):
            cap = _triad("旧快照恢复预览已生成，尚未修改内容", ["展示差异并取得用户对本次预览的确认", "确认后原样传入 expected_sha256 执行"], res["warning"], terminal=True)
            return 0, _envelope(True, "OK_PROJECT_RECOVERY_PREVIEW", cap, data=res), sealed_paths
        cap = _triad(
            (f"已按核对后的存量快照恢复 {res['undone_op_id']}，历史完整性未验证；工单 {res['undo_ticket_id']}"
             if res.get("recovery_mode") == "legacy-recovery" else f"后悔药已生效：已将操作 {res['undone_op_id']} 完整回滚至操作前状态，并写入回滚工单 {res['undo_ticket_id']}"),
            [
                f"核对课题列表: ./gewu atlas --action project-list --ref {vref}",
                f"核对全库健康: ./gewu atlas --action health-check --ref {vref}",
            ],
            "所有受影响课题的 Artifact / Memory / Workbench 与目录结构已按快照还原。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_PROJECT_UNDONE", cap, data=res), sealed_paths

    if action == "project-merge":
        from pipeline.atlas_project_engine import MergeConflictPause

        src_refs = _parse_str_list(args.get("source_refs"))
        try:
            res, p_paths = engine_project_merge(
                ws_root=ws_root,
                state_dir=state_dir,
                vault_dir=vdir,
                vault_ref=vref,
                proj_reg=proj_reg,
                note_reg=note_reg,
                kek=kek,
                app_id=app_id,
                source_refs=src_refs,
                target_ref=str(args.get("target_ref") or "").strip() or None,
                title=args.get("title"),
                project_id=args.get("project_id") or args.get("new_project_id"),
                summary=args.get("summary"),
                on_conflict=str(args.get("on_conflict") or "ask").strip().lower(),
                workbench_conflict=str(args.get("workbench_conflict") or "rename").strip().lower(),
                add_combined_from=bool(args.get("add_combined_from", True)),
                archive_sources=bool(args.get("archive_sources", True)),
            )
            sealed_paths.extend(p_paths)
            tprj = res["target_project"]
            mc = res["moved_counts"]
            cap = _triad(
                f"已完成课题合并 →「{tprj['title']}」（{tprj['project_ref']}）：Artifacts={mc['artifacts']}，Memory={mc['memory']}，Slip={mc.get('slip', 0)}，Workbench={mc['workbench']}（自动重命名 {len(res['workbench_renames'])}），工单 {res['ticket_id']}",
                [
                    f"如不满意可直接回滚: ./gewu atlas --action project-undo --ref {vref} --op_id {res['op_id']}",
                    f"查看合并后课题: ./gewu atlas --action project-get --ref {vref} --project_ref {tprj['project_ref']}",
                ],
                f"已通过工单记录审计并写入 combined_from；Workbench 冲突件已按 -from-<src> 保留两份。",
                terminal=True,
            )
            return 0, _envelope(True, "OK_PROJECT_MERGED", cap, data=res), sealed_paths
        except MergeConflictPause as pause:
            cap = _guard(
                "INPUT_REQUIRED",
                f"E_MERGE_CONFLICT_CONFIRM: 合并前查重发现 {len(pause.conflicts)} 项重名/重复文件，已安全中断（未修改任何文件）",
                "请向用户展示冲突清单并确认处理方式后，附带 --on_conflict <rename|skip|overwrite> 重新执行。",
                "若确认留存两份建议 --on_conflict rename；跳过重复项用 skip；以新代旧用 overwrite（合并均留有后悔药快照）。",
            )
            return (
                1,
                _envelope(
                    False,
                    "E_MERGE_CONFLICT_CONFIRM",
                    cap,
                    data={
                        "vault_ref": vref,
                        "conflict_count": len(pause.conflicts),
                        "conflicts": pause.conflicts,
                        "workbench_planned_renames": pause.workbench_planned_renames,
                        "allowed_resolutions": ["rename", "skip", "overwrite", "merge", "supersede"],
                        "path_sealed": True,
                    },
                ),
                sealed_paths,
            )

    if action == "project-untrash":
        trash_reg = KekTrashRegistry(app_id, state_dir / "atlas-trash.enc", kek)
        res, p_paths = engine_project_untrash(
            vault_dir=vdir,
            vault_ref=vref,
            proj_reg=proj_reg,
            note_reg=note_reg,
            trash_reg=trash_reg,
            kek=kek,
            app_id=app_id,
            trash_ref=str(args.get("trash_ref") or "").strip() or None,
        )
        sealed_paths.extend(p_paths)
        is_list = res.get("mode") == "list"
        cap = _triad(
            f"已列出回收站中 {res['count']} 个已软删课题" if is_list else f"已恢复课题「{res['title']}」（新句柄 {res['project_ref']}）",
            [
                f"恢复指定课题: ./gewu atlas --action project-untrash --ref {vref} --trash_ref <trs_01>"
                if is_list
                else f"查看课题详情: ./gewu atlas --action project-get --ref {vref} --project_ref {res['project_ref']}",
                "直接向人类汇报现状",
            ],
            "支持按 trs_XX 句柄恢复整课题目录并自动重绑 prj_XX 与 nte_XX 句柄。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_PROJECT_UNTRASHED", cap, data=res), sealed_paths

    pref = str(args.get("project_ref") or "").strip()
    if not pref:
        cap = _guard(
            "PARAM_FIX",
            f"E_PARAM_MISSING: {action} 需要 --project_ref <prj_01>",
            "请先调用 project-list 获取 --project_ref 后重试。",
            "同指纹连续失败 2 次将触发硬熔断。",
            fill_template={"action": action, "ref": vref, "project_ref": "<prj_01>"},
            attempt_remaining=1,
        )
        return 1, _envelope(False, "E_PARAM_MISSING", cap, missing_slots=["project_ref"]), sealed_paths

    if action == "project-get":
        pub, p_paths = engine_project_get(
            vault_dir=vdir,
            vault_ref=vref,
            project_ref=pref,
            proj_reg=proj_reg,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
        )
        sealed_paths.extend(p_paths)
        cnts = pub["counts"]
        cap = _triad(
            f"已查看课题 {pref}（标题「{pub['title']}」，Artifacts={cnts['artifacts']}，Memory={cnts['memory']}，Workbench={cnts['workbench']}）[只读完成]",
            [
                f"申请工作台目录: ./gewu atlas --action workbench-workspace --ref {vref} --project_ref {pref} --agent <name>",
                "执行合并/拆分或直接向人类汇报现状",
            ],
            "所有课题卡片均绑定逻辑句柄；开展写稿/研究须先进入 30-Workbench/ws-<agent>，严禁在课题内新建 99-Assets。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_PROJECT_GET", cap, data=pub), sealed_paths

    if action == "project-rename":
        pub, p_paths = engine_project_rename(
            vault_dir=vdir,
            vault_ref=vref,
            project_ref=pref,
            proj_reg=proj_reg,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            new_title=args.get("title"),
            new_project_id=args.get("new_project_id") or args.get("project_id"),
        )
        sealed_paths.extend(p_paths)
        cap = _triad(
            f"已安全重命名课题 {pref}：标题「{pub['previous_title']}」→「{pub['title']}」（更新 {pub['updated_project_notes']} 张卡、级联 {pub['wikilink_cascaded_files']} 个双链）",
            [f"查看课题详情: ./gewu atlas --action project-get --ref {vref} --project_ref {pref}", "直接向人类汇报现状"],
            "历史工单不可变回执保持原状未篡改，课题与笔记句柄稳定不变。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_PROJECT_RENAMED", cap, data=pub), sealed_paths

    if action == "project-publish":
        pub, p_paths = engine_project_publish(
            vault_dir=vdir,
            vault_ref=vref,
            project_ref=pref,
            proj_reg=proj_reg,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
        )
        sealed_paths.extend(p_paths)
        cap = _triad(
            f"已公开课题「{pub['title']}」（{pref}，同步更新 {pub['updated_notes']} 篇内部卡片）",
            [f"查看课题: ./gewu atlas --action project-get --ref {vref} --project_ref {pref}", "直接向人类汇报现状"],
            "已将 Manifest 与 Artifacts/Memory/Slip 卡片置为 private: false。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_PROJECT_PUBLISHED", cap, data=pub), sealed_paths

    if action == "project-unpublish":
        res, p_paths = engine_project_unpublish(
            state_dir=state_dir,
            vault_dir=vdir,
            vault_ref=vref,
            project_ref=pref,
            proj_reg=proj_reg,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
            on_links=str(args.get("on_links") or ""),
            apply=bool(args.get("apply", False)),
        )
        sealed_paths.extend(p_paths)
        if res.get("paused"):
            cap = _guard(
                "INPUT_REQUIRED",
                f"E_PROJECT_UNPUBLISH_CONFIRM: 课题 {pref} 存在 {res['incoming_external_count']} 处外部入向双链引用，已暂停下架（未修改任何文件）",
                "请向用户汇报受影响笔记后，附带 --on_links downgrade --apply true 重新执行以将外部双链降级并记录可逆快照。",
                "GUARD: INPUT_REQUIRED | 降级快照支持后续通过 project-restore-links 恢复",
            )
            return 1, _envelope(False, "E_PROJECT_UNPUBLISH_CONFIRM", cap, data=res), sealed_paths
        cap = _triad(
            f"已将课题「{res['title']}」（{pref}）转为私有下架态（降级 {res['downgraded_links_count']} 处外部双链）",
            [
                f"如需复原被降级双链: ./gewu atlas --action project-restore-links --ref {vref} --project_ref {pref}",
                "直接向人类汇报现状",
            ],
            "已设置 private: true 与 private_since，并将外部双链降级审计存入受控运行时。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_PROJECT_UNPUBLISHED", cap, data=res), sealed_paths

    if action == "project-restore-links":
        res, p_paths = engine_project_restore_links(
            state_dir=state_dir,
            vault_dir=vdir,
            vault_ref=vref,
            project_ref=pref,
            proj_reg=proj_reg,
            note_reg=note_reg,
            op_id=str(args.get("op_id") or "").strip() or None,
        )
        sealed_paths.extend(p_paths)
        cap = _triad(
            f"已根据快照 {res['snapshot_id']} 还原 {res['restored_files']} 篇文档中的 {res['restored_links']} 处外部双链（跳过 {res['skipped_links']} 处）",
            [f"查看课题: ./gewu atlas --action project-get --ref {vref} --project_ref {pref}", "直接向人类汇报现状"],
            "已按降级审计快照无损还原可匹配的双链。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_PROJECT_LINKS_RESTORED", cap, data=res), sealed_paths

    if action == "project-archive":
        pub, p_paths = engine_project_archive(
            vault_dir=vdir,
            vault_ref=vref,
            project_ref=pref,
            proj_reg=proj_reg,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
        )
        sealed_paths.extend(p_paths)
        cap = _triad(
            f"已归档冻结课题「{pub['title']}」（{pref}，status=archived）",
            [f"重新激活课题: ./gewu atlas --action project-reopen --ref {vref} --project_ref {pref}", "直接向人类汇报现状"],
            "归档课题默认从常规 project-list 隐藏，传 --include_archived true 可见。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_PROJECT_ARCHIVED", cap, data=pub), sealed_paths

    if action == "project-reopen":
        pub, p_paths = engine_project_reopen(
            vault_dir=vdir,
            vault_ref=vref,
            project_ref=pref,
            proj_reg=proj_reg,
            note_reg=note_reg,
            kek=kek,
            app_id=app_id,
        )
        sealed_paths.extend(p_paths)
        cap = _triad(
            f"已重新开启课题「{pub['title']}」（{pref}，status=active）",
            [f"查看课题: ./gewu atlas --action project-get --ref {vref} --project_ref {pref}", "直接向人类汇报现状"],
            "课题已恢复活跃调度。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_PROJECT_REOPENED", cap, data=pub), sealed_paths

    if action == "project-trash":
        trash_reg = KekTrashRegistry(app_id, state_dir / "atlas-trash.enc", kek)
        res, p_paths = engine_project_trash(
            vault_dir=vdir,
            vault_ref=vref,
            project_ref=pref,
            proj_reg=proj_reg,
            note_reg=note_reg,
            trash_reg=trash_reg,
            on_links=str(args.get("on_links") or ""),
            apply=bool(args.get("apply", False)),
        )
        sealed_paths.extend(p_paths)
        if res.get("paused"):
            hint_cmd = "--on_links downgrade --apply true" if res["referenced_count"] > 0 else "--apply true"
            cap = _guard(
                "INPUT_REQUIRED",
                f"E_PROJECT_TRASH_CONFIRM: 软删课题 {pref}（「{res['title']}」，外部引用 {res['referenced_count']} 处）需明确确认后执行（未修改任何文件）",
                f"请向用户确认后附带 {hint_cmd} 重新执行。",
                "GUARD: INPUT_REQUIRED | 软删后可通过 project-untrash --trash_ref <trs_01> 恢复",
            )
            return 1, _envelope(False, "E_PROJECT_TRASH_CONFIRM", cap, data=res), sealed_paths
        cap = _triad(
            f"已软删课题「{res['title']}」至受控回收站（句柄 {res['trash_ref']}，降级 {res['downgraded_links_count']} 处外部引用）",
            [f"恢复该课题: ./gewu atlas --action project-untrash --ref {vref} --trash_ref {res['trash_ref']}", "直接向人类汇报现状"],
            "整课题目录已受控移入回收站并保留目录完整性。",
            terminal=True,
        )
        return 0, _envelope(True, "OK_PROJECT_TRASHED", cap, data=res), sealed_paths

    # project-split
    raw_plan = args.get("split_plan")
    branches: List[Dict[str, Any]] = []
    if raw_plan is not None:
        if isinstance(raw_plan, list):
            branches = [b for b in raw_plan if isinstance(b, dict)]
        elif isinstance(raw_plan, str) and raw_plan.strip():
            try:
                loaded = json.loads(raw_plan)
            except Exception as exc:
                raise VaultStoreError("E_PARAM_INVALID", f"--split_plan JSON 解析失败: {exc}", exit_code=1) from exc
            if not isinstance(loaded, list):
                raise VaultStoreError("E_PARAM_INVALID", "--split_plan 必须为 JSON 数组", exit_code=1)
            branches = [b for b in loaded if isinstance(b, dict)]
    elif str(args.get("title") or "").strip():
        branches = [
            {
                "title": str(args.get("title") or "").strip(),
                "project_id": str(args.get("new_project_id") or args.get("project_id") or "").strip(),
                "summary": str(args.get("summary") or "").strip(),
                "tags": _parse_str_list(args.get("tags")),
                "keywords": _parse_str_list(args.get("query")),
                "note_refs": _parse_str_list(args.get("note_refs")),
            }
        ]

    res, p_paths = engine_project_split(
        ws_root=ws_root,
        state_dir=state_dir,
        vault_dir=vdir,
        vault_ref=vref,
        proj_reg=proj_reg,
        note_reg=note_reg,
        kek=kek,
        app_id=app_id,
        project_ref=pref,
        branches=branches,
    )
    sealed_paths.extend(p_paths)
    cap = _triad(
        f"已完成课题拆分：从 {pref} 拆出 {len(res['split_targets'])} 个子主题（迁移 {res['total_moved']} 项，工单 {res['ticket_id']}）",
        [
            f"如不满意可直接回滚: ./gewu atlas --action project-undo --ref {vref} --op_id {res['op_id']}",
            f"查看最新课题清单: ./gewu atlas --action project-list --ref {vref}",
        ],
        "已写入 split_from 溯源并在 Ticket 登记审计，随时可通过 op_id 后悔药回滚。",
        terminal=True,
    )
    return 0, _envelope(True, "OK_PROJECT_SPLIT", cap, data=res), sealed_paths


def _needs_stdin(raw_argv: List[str]) -> bool:
    return any(
        tok == "-" or (tok.startswith("--") and tok.endswith("=-")) or tok in ("--json-stdin", "--json_stdin")
        for tok in raw_argv
    )



def _active_gewu_log_level(ws_root: Path) -> str:
    env_lvl = os.environ.get("GEWU_LOG_LEVEL", "").strip().upper()
    if env_lvl in ("DEBUG", "INFO", "WARN", "ERROR"):
        return env_lvl
    vol_meta = ws_root / "Volumes" / "GewuLog-data" / "volume.json"
    if vol_meta.is_file():
        try:
            return str(json.loads(vol_meta.read_text("utf-8")).get("default_level") or "INFO").upper()
        except Exception:
            return "INFO"
    return "INFO"


def _attach_debug_trace_prompt(env: Dict[str, Any], ws_root: Path) -> None:
    if _active_gewu_log_level(ws_root) != "DEBUG":
        return
    cap = env.get("structuredContent", {}).get("capsule") if isinstance(env.get("structuredContent"), dict) else None
    if isinstance(cap, dict):
        cur_anc = str(cap.get("anchor") or "")
        if "[DEBUG_TRACE" not in cur_anc:
            cap["anchor"] = f"{cur_anc} [DEBUG_TRACE: 下步调用请附带 --trace '触发信号=>当前正向决策'（勿复述错误，仅记前向行动）]"
            if "rendered_text" in cap:
                cap["rendered_text"] = str(cap["rendered_text"]) + " [DEBUG_TRACE: 下步调用请附带 --trace '触发信号=>当前正向决策']"
                env["reminder"] = cap["rendered_text"]


def _emit_dual_source_log(
    *,
    ws_root: Path,
    state_dir: Path,
    args: Dict[str, Any],
    action: str,
    rc: int,
    env: Dict[str, Any],
) -> None:
    """Dual-source leveled telemetry:
    - Operator records objective past/errors/reminders (so Agent never repeats negative mistakes).
    - Agent (only in DEBUG mode) contributes forward-looking `Signal => Decision` trace via --trace/--thought.
    """
    try:
        log_vol = ws_root / "Volumes" / "GewuLog-data"
        vol_meta_path = log_vol / "volume.json"
        env_lvl = os.environ.get("GEWU_LOG_LEVEL", "").strip().upper()
        if not vol_meta_path.is_file() and not env_lvl:
            return
        active_lvl = _active_gewu_log_level(ws_root)
        rank_map = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
        threshold = rank_map.get(active_lvl, 20)

        cap = env.get("structuredContent", {}).get("capsule") if isinstance(env.get("structuredContent"), dict) and isinstance(env.get("structuredContent", {}).get("capsule"), dict) else {}
        code = str(env.get("code") or "")
        anchor = str(cap.get("ANCHOR") or cap.get("anchor") or "")
        reason = str(cap.get("reason") or cap.get("STATE") or cap.get("state") or "")
        agent_trace = str(args.get("trace") or args.get("thought") or "").strip()
        plan_rev = str(args.get("plan_revision") or "").strip()

        last_rem_file = state_dir / "atlas-last-reminder.json"
        prior_rem: Dict[str, Any] = {}
        if last_rem_file.is_file():
            try:
                prior_rem = json.loads(last_rem_file.read_text("utf-8"))
            except Exception:
                prior_rem = {}

        state_dir.mkdir(parents=True, exist_ok=True)
        if rc != 0 or "SNIPPET_ONLY" in anchor or "BLOCKED" in anchor or "INPUT_REQUIRED" in str(cap.get("kind", "")):
            rem_snap = {
                "prev_action": action or "unknown",
                "prev_code": code,
                "prev_reminder": (reason + " | " + anchor)[:200],
            }
            last_rem_file.write_text(json.dumps(rem_snap, ensure_ascii=False), encoding="utf-8")
        elif last_rem_file.is_file():
            try:
                last_rem_file.unlink()
            except OSError:
                pass

        is_wakeup = bool(prior_rem and rc == 0)
        if rc != 0:
            lvl = "ERROR" if rc >= 2 else "WARN"
            ev = "MISTAKE_FIX"
        elif plan_rev:
            lvl = "INFO"
            ev = "PLAN_REVISE"
        elif "BLOCKED" in anchor:
            lvl = "WARN"
            ev = "GATE_EVAL"
        elif action == "delivery-check":
            lvl = "INFO"
            ev = "GATE_EVAL"
        elif is_wakeup:
            lvl = "DEBUG" if not agent_trace else "INFO"
            ev = "REMINDER_WAKEUP"
        elif agent_trace:
            lvl = "DEBUG"
            ev = "THOUGHT"
        elif action not in READ_ONLY_ACTIONS and action not in CONTEXT_ACTIONS:
            lvl = "INFO"
            ev = "OP_CALL"
        else:
            lvl = "DEBUG"
            ev = "OP_CALL"

        if rank_map.get(lvl, 10) < threshold:
            return

        log_scripts = ws_root / "GewuLog" / "scripts"
        if not (log_scripts / "log" / "log_engine.py").is_file():
            return
        if str(log_scripts) not in sys.path:
            sys.path.insert(0, str(log_scripts))
        from log.log_engine import record_log_entry

        data_dict = env.get("data") if isinstance(env.get("data"), dict) else {}
        d_gate = data_dict.get("delivery_gate") if isinstance(data_dict.get("delivery_gate"), dict) else {}
        op_score = data_dict.get("operator_score") if action == "delivery-check" else d_gate.get("operator_score")

        mistake_str = ""
        reminder_str = ""
        if rc != 0:
            mistake_str = f"{action or 'cmd'} -> {code}: {reason[:120]}"
            reminder_str = str(cap.get("action_guidance") or anchor)[:160]
        elif is_wakeup:
            mistake_str = f"前态停留在 {prior_rem.get('prev_action')} ({prior_rem.get('prev_code')})"
            reminder_str = str(prior_rem.get("prev_reminder") or "")[:160]

        record_log_entry(
            log_vol,
            level=lvl,
            event=ev,
            source="dual" if (agent_trace and (mistake_str or reminder_str)) else ("agent" if agent_trace else "operator"),
            by=str(args.get("by") or args.get("agent") or "ai"),
            domain="atlas",
            action_name=action or "unknown",
            ref=str(args.get("project_ref") or args.get("note_ref") or args.get("ref") or ""),
            msg=f"[{action or 'cmd'}] {code}" + (f" | {agent_trace}" if agent_trace else ""),
            thought=agent_trace,
            plan_revision=plan_rev,
            mistake=mistake_str,
            reminder=reminder_str,
            action_taken=f"{action} ({code})" if rc == 0 else "",
            operator_score=op_score,
            force=True,
        )
    except Exception:
        pass


def run_cli_request(
    raw_argv: List[str],
    *,
    stdin_text: Optional[str] = None,
    hot_registries: Optional[Dict[str, Any]] = None,
) -> Tuple[int, str]:
    """Execute one CLI request in-process and return `(exit_code, json_stdout_line)`.

    Shared by cold CLI `main()`, `AtlasHotDaemon`, and `atlas_mcp_server.py`.
    """
    state_dir, log_dir = _runtime_dirs()
    breaker_path = state_dir / "atlas-breaker.json"
    ws_root = _workspace_root()

    if any(t in ("--template", "--describe", "-h", "--help") for t in raw_argv):
        return 0, json.dumps(_template_payload(raw_argv), ensure_ascii=False) + "\n"

    session_fp = _extract_session_fp(raw_argv, stdin_text)
    fp = _fingerprint(raw_argv, stdin_text)
    bstate = load_session_breaker(breaker_path, session_fp)
    last_fp = bstate.get("fp")
    fail_streak = int(bstate.get("fail_streak", 0)) if last_fp == fp else 0
    ro_streak = int(bstate.get("ro_streak", 0)) if last_fp == fp else 0
    last_ok_action = str(bstate.get("last_ok_action") or "")
    ok_streak = int(bstate.get("ok_streak", 0))

    if fail_streak >= 2 or ro_streak >= 3:
        cap = _guard(
            "HALTED",
            "E_HALT_LOOP_DETECTED: 检测到同指纹重复失败或空转只读调用，触发硬熔断",
            "立即停止重试并向人类汇报现状。",
            "GUARD: HALTED | 禁止重试",
        )
        return 2, json.dumps(_envelope(False, "E_HALT_LOOP_DETECTED", cap), ensure_ascii=False) + "\n"

    try:
        args = _parse_channels(raw_argv, stdin_text)
        action = str(args.get("action", "")).strip()
        is_ro = _is_non_mutating_vault_op(action, args)
        code, env, sealed_paths = _execute_action(args, hot_registries=hot_registries)
        if args.get("_vault_selection"):
            env.setdefault("data", {})["vault_selection"] = args["_vault_selection"]
        _assert_zero_path_leak(env, ws_root, sealed_paths)
        next_ok_streak = 0
        if code != 0:
            next_streak = fail_streak + 1
            save_session_breaker(
                breaker_path,
                session_fp,
                {"fp": fp, "fail_streak": next_streak, "ro_streak": 0, "last_ok_action": "", "ok_streak": 0},
            )
            cap_mode = str(env.get("structuredContent", {}).get("capsule", {}).get("mode") or "")
            if next_streak >= 2 and cap_mode != "HALTED":
                orig_code = str(env.get("code") or "E_FAIL")
                orig_state = str(env.get("structuredContent", {}).get("capsule", {}).get("state") or "")
                cap = _guard(
                    "HALTED",
                    f"{orig_code}: {orig_state}（同指纹连续拦截 {next_streak} 次，已升格硬熔断 E_HALT_LOOP_DETECTED）",
                    "立即停止重试与换参绕试；向人类汇报拦截原因与当前坐标。",
                    "GUARD: HALTED | 禁止重试",
                )
                env = _envelope(False, "E_HALT_LOOP_DETECTED", cap)
                _assert_zero_path_leak(env, ws_root, sealed_paths)
                code = 2
        else:
            next_ok_streak = (ok_streak + 1) if (action and action == last_ok_action) else 1
            save_session_breaker(
                breaker_path,
                session_fp,
                {
                    "fp": fp,
                    "fail_streak": 0,
                    "ro_streak": (ro_streak + 1) if is_ro else 0,
                    "last_ok_action": action,
                    "ok_streak": next_ok_streak,
                },
            )
            # Record opaque note_refs actually read in this session for read-before-write audit
            if action == "note-get" and isinstance(env.get("data"), dict):
                d_get = env["data"]
                v_get = str(d_get.get("vault_ref") or args.get("ref") or "").strip()
                if v_get:
                    read_refs: List[str] = []
                    if d_get.get("note_ref"):
                        read_refs.append(str(d_get["note_ref"]))
                    if isinstance(d_get.get("notes"), list):
                        for item in d_get["notes"]:
                            if isinstance(item, dict) and item.get("note_ref"):
                                read_refs.append(str(item["note_ref"]))
                    if read_refs:
                        record_session_reads(state_dir, session_fp, v_get, read_refs)
        maybe_compact_capsule(env, consecutive_same_ok=next_ok_streak)
        _assert_zero_path_leak(env, ws_root, sealed_paths)
        return code, json.dumps(env, ensure_ascii=False) + "\n"
    except VaultStoreError as exc:
        next_streak = fail_streak + 1
        save_session_breaker(
            breaker_path,
            session_fp,
            {"fp": fp, "fail_streak": next_streak, "ro_streak": 0, "last_ok_action": "", "ok_streak": 0},
        )
        if exc.code in ("E_VAULT_CONTEXT_REQUIRED", "E_VAULT_CONTEXT_UNAVAILABLE", "E_VAULT_SESSION_REQUIRED"):
            mode = "INPUT_REQUIRED"
            guidance = "请明确选择知识库或接入稳定会话 ID；停止相关操作，不自动回退。"
            anchor = "GUARD: INPUT_REQUIRED | 需明确库选择"
        elif exc.code in ("E_UNDO_CONFLICT", "E_UNDO_UNVERIFIED"):
            mode = "INPUT_REQUIRED"
            guidance = "保留当前内容并向人类报告；需核对恢复方案，禁止自动重试。"
            anchor = "GUARD: INPUT_REQUIRED | 禁止覆盖当前内容或重复执行"
        elif exc.code in ("E_URL_NETWORK", "E_URL_POLICY"):
            mode = "INPUT_REQUIRED"
            guidance = "停止重试该失败 URL；可先记录不可达/拒访状态，并继续采集其余不同有效 URL（无需中止整批研究），或等待人类替换来源。"
            anchor = "GUARD: INPUT_REQUIRED | 禁止重试该失败 URL，可继续其余有效来源"
        elif exc.code == "E_PRE_REVIEW_REQUIRED":
            mode = "INPUT_REQUIRED"
            guidance = "呈报人类完成批准后执行，禁绕过门禁。"
            anchor = "GUARD: INPUT_REQUIRED | 需人类授权"
        elif exc.code == "E_DOMAIN_FROZEN":
            mode = "INPUT_REQUIRED"
            guidance = "域已冻结；汇报人类或获准先 project-reopen 再操作，禁直接重试。"
            anchor = "GUARD: INPUT_REQUIRED | E_DOMAIN_FROZEN"
        elif exc.code == "E_JOURNAL_PROTECTED":
            mode = "INPUT_REQUIRED"
            guidance = "日记只读/追加，禁写/迁/删/切/萃；请改用只读或另建卡片。"
            anchor = "GUARD: INPUT_REQUIRED | E_JOURNAL_PROTECTED"
        elif exc.code == "E_PROJECT_ALREADY_EXISTS":
            mode = "PARAM_FIX"
            guidance = "禁止重复新建同名或同 project_id 课题；请直接复用提示中的 --project_ref，或先 project-rename 改名 / project-trash 软删。"
            anchor = "先 project-list 确认已有课题句柄；若建错请 project-rename / project-trash，禁止另起炉灶重复建课题。"
        elif exc.code == "E_SECURITY_VIOLATION":
            mode = "HALTED"
            guidance = "越界或符号链接拦截；立即停手并汇报人类，禁重试。"
            anchor = "GUARD: HALTED | 禁止重试"
        elif (exc.exit_code == 2 and exc.code.startswith("E_HALT")) or next_streak >= 2:
            mode = "HALTED"
            guidance = "立即停止重试；向人类汇报拦截原因与当前坐标。"
            anchor = "GUARD: HALTED | 禁止重试"
        else:
            mode = "PARAM_FIX"
            guidance = "修正参数后重试或汇报人类。"
            if exc.code == "E_PARAM_MISSING" and "note-slice" in exc.message:
                anchor = "先运行 note-slice 切片，再按 --action atomic-extract --template 传入 --title 与 --content"
            elif "atomic-extract --template" in exc.message:
                anchor = "先用 --action atomic-extract --template 核对必填 --title/--content，禁止直接复制切片"
            else:
                anchor = "同错连败2次熔断。"
        err_code = "E_HALT_LOOP_DETECTED" if (next_streak >= 2 and mode == "HALTED" and not exc.code.startswith(("E_HALT", "E_SECURITY_")) and exc.code not in ("E_PRE_REVIEW_REQUIRED", "E_DOMAIN_FROZEN", "E_JOURNAL_PROTECTED")) else exc.code
        cap = _guard(
            mode,
            f"{exc.code}: {exc.message}",
            guidance,
            anchor,
            attempt_remaining=None if mode in ("HALTED", "INPUT_REQUIRED") else max(0, 2 - next_streak),
        )
        out_code = 2 if mode == "HALTED" else exc.exit_code
        err_env = _envelope(False, err_code, cap)
        return out_code, json.dumps(err_env, ensure_ascii=False) + "\n"
    except Exception:  # noqa: BLE001
        log_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(log_dir, 0o700)
        log_file = log_dir / "atlas-cli-errors.log"
        fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(traceback.format_exc() + "\n")
        os.chmod(log_file, 0o600)
        save_session_breaker(
            breaker_path,
            session_fp,
            {"fp": fp, "fail_streak": fail_streak + 1, "ro_streak": 0, "last_ok_action": "", "ok_streak": 0},
        )
        cap = _guard(
            "HALTED",
            "E_HALT_INTERNAL: 算子执行遇到内部异常，现场堆栈已收敛至受保护日志",
            "立即停止重试并向人类汇报。",
            "GUARD: HALTED | 禁止重试",
        )
        return 2, json.dumps(_envelope(False, "E_HALT_INTERNAL", cap), ensure_ascii=False) + "\n"


def main() -> int:
    raw_argv = sys.argv[1:]
    if _EARLY_STDIN_LOADED:
        stdin_text = _EARLY_STDIN_CACHE
    else:
        stdin_text = sys.stdin.read() if _needs_stdin(raw_argv) and not sys.stdin.isatty() else None
    code, out_line = run_cli_request(raw_argv, stdin_text=stdin_text)
    sys.stdout.write(out_line)
    return code


if __name__ == "__main__":
    sys.exit(main())
