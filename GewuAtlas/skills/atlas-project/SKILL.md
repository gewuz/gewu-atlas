---
name: atlas-project
description: Atlas 知识库（vault）与课题域（project_ref）管理、课题一屏全景（project-brief）、多轮研究工作台协调、课题合并拆分与状态流转（create / list / get / brief / rename / merge / split / undo / publish / archive / trash / workbench-workspace / workbench-preset）。
---

# 库与课题 · 空间分配、生命周期与工作台管理

## 1. 核心空间配方

- **知识库切换与维护**：
  - 一步查看当前上下文与全部库：`./gewu atlas --action vault`（支持 `--mode use|reset|default-get|default-set|delete|restore`）；新建库用 `vault-create`，同构存量库就地安全接管用 `vault-import`（支持一次性新窗口盲采，可选 `--seal_rename true` 盲化目录），更新配置用 `vault-update`，同步索引与 Obsidian 视图用 `index-sync`。
- **课题域开立与工作台一键分配（围墙入口）**：
  - 创建课题并顺手分配个人工作台（推荐）：
    ```bash
    ./gewu atlas --action project-create --ref <vlt_01> --title "<课题名称>" --agent <agent_name> [--default_mode auto|supervised] [--preset engineering|academic|multi-agent|solo]
    ```
    返回体 `data.workspace.subdirs` 直接提供 `ws-<agent>/drafts` 与 `ws-<agent>/source`，无需再单独往返调用 `workbench-workspace`（如需单独管理工作台或预设，仍可调 `workbench-workspace` 与 `workbench-preset`）。
- **课题查询、一屏全景、重构与状态流转**：
  - 查询与全景：`project-brief`（一屏返回分区统计、核心卡片句柄目录与双链骨架，替代多步探查）、`project-list`、`project-get`、`project-rename`、`project-history`；
  - 合并、拆分与撤回：`project-merge`、`project-split`、后悔药 `project-undo`；
  - 公开、退网、归档与回收：`project-publish`（须人类确认）、`project-unpublish`、`project-restore-links`、`project-archive`、`project-reopen`、`project-trash`（配合 `trash --mode restore --scope project` 恢复）。

## 2. 交付验货闸门

- **L1 单步管理动作**（建库、建课题、列课题）：算子返回 `OK_*` 即完成，零额外评估。
- **L3 课题级研究交付与重构（合并/拆分/公开前）**：
  1. 运行 `./gewu atlas --action guard --ref <vlt_01> --mode delivery --project_ref <prj_01>` 确认课题内零非法目录、零断链、Schema 100% 合规（60/60 分）；
  2. 若准备交付课题最终成果报告，优先调用只读 Subagent（无则自评）核验「完整性、真实性、可读性」；硬评 60/60 与独立记录的语义 PASS 均为交付前提。

全局路由见 [AGENTS.md](../../AGENTS.md)，规范见 [rule-core-state-identity.md](../../rules/rule-core-state-identity.md)、[rule-core-vfs.md](../../rules/rule-core-vfs.md) 与 [rule-arch-multi-agent-collab.md](../../rules/rule-arch-multi-agent-collab.md)。
