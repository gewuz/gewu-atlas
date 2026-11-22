---
name: atlas-ops
description: Atlas 交付验货闸门、运维体检、自托管热内核守护进程（daemon start/status/stop）、防污染审计、冷归档、快照撤回与 DOC+GitHub 统一同步（guard）：统一覆盖 delivery / health / schema / validate / links / doc-status / doc-commit / doc-remote / doc-sync / clean-attachments / archive-pack / trash-purge / retention-sweep / change-list / change-get / undo / daemon。
---

# 验货与运维 · 60分交付硬闸门、巡检同步与后悔药

提供复合任务收尾时的 **60 分算子硬评估（`guard --mode delivery`）**，以及全库体检、人工改动同步、冷归档与 `chg_XX` 撤回。

## 1. 统一入口 `guard` 模式路由表

通过 `./gewu atlas --action guard --ref <vlt_01> --mode <子模式>` 执行：

| 场景 | `--mode` 取值 | 作用与关键参数 |
| :--- | :--- | :--- |
| **交付验货闸门（60分硬底线）** | `delivery`（或 `verify`） | **一键检验四维硬指标**：① 工作区根目录净空、VFS 拓扑与课题 `Manifest` 台账完整性（15分，自动将本次检查记录与三舱位计数同步写入课题 `Manifest-*.md`）；② YAML Schema 与字典合规（15分）；③ 引用句柄（`--note_refs`）真实性、Mermaid 闭合与零断链（15分）；④ 审批边界（15分），并输出 40 分 Subagent/自评量表协议 |
| 综合体检与 Schema 字典 | `health` / `schema` / `schema-list` | `[--note_ref]`、`[--doc_type]`、`[--strict true]` |
| VFS 拓扑与双链扫描 | `validate` / `links` | `[--note_ref]`（发现断链可用 `note-link --mode fix` 修复） |
| 人类删改识别与 DOC+GitHub 统一同步 | `doc-status` / `doc-commit` / `doc-remote` / `doc-sync` | `doc-remote`: `[--remote_url] [--branch main] [--auto_push true\|false] [--git_layout in-vault\|detached] [--clear true]`；`doc-commit`: `--mode standard\|human-sync [--push true\|false] [--force true] [--reason]`（默认 `auto_push=true`）；`doc-sync`: `[--direction both\|pull\|push] [--bootstrap adopt-remote] [--force true]`（包含 `00-DropZone/`，排除 `.obsidian/`、`98-Archives/`、`source/` 与回收区） |
| 孤岛附件清理与冷备封包 | `clean-attachments` / `archive-list` / `archive-pack` | `[--apply true]`、`[--month YYYYMM]`、`[--dry_run true]` |
| 变更台账与 `chg_XX` 后悔药 | `change-list` / `change-get` / `undo` | `--change_id <chg_01>`，无损还原误改内容 |
| 回收站与 30 天快照清扫 | `trash-purge` / `retention-sweep` | `[--days 30]`、`[--dry_run true]` |
| **自托管热内核与可选 MCP** | 独立路由 `./gewu atlas daemon` | `daemon start [--idle-timeout 600] [--preload-vault vlt_01]` / `status` / `stop` / `restart`（多步任务开工拉起、收工停机，零宿主 MCP 依赖；支持 MCP 的宿主亦可用 `./gewu atlas mcp`） |

## 2. 双轨评估协同说明（算子 60 分 + Subagent/自评 40 分）

- 当 `guard --mode delivery` 返回 `operator_passed: true (60/60)` 后：
  - **若有 Subagent 能力**：复制返回体 `semantic_evaluation.subagent_blind_review_prompt`，连同用户原始请求与待交付成果交给独立只读 Subagent 盲审打分（0~40分）；
  - **若无 Subagent 能力**：主 Agent 对照 `semantic_evaluation.rubric`（完整性 15 + 真实性 15 + 可读性与零路径 10）自检过线后交付。
- **红线提醒**：检测到人类手工删除引发断链时，严禁恢复已删文件；须先用 `note-update` 清理引用方断链后再 `guard --mode doc-commit --mode human-sync`。`05-Journal/` 日记永久豁免物理删除。

全局路由见 [AGENTS.md](../../AGENTS.md)，规范见 [rule-arch-multi-agent-collab.md](../../rules/rule-arch-multi-agent-collab.md) 与 [rule-core-vfs.md](../../rules/rule-core-vfs.md)。
