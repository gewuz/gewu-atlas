---
name: atlas-note
description: Atlas 写与整理（note / vfs / artifact / journal / slip / trash）：创建、更新、规范化、跨区移动复制、统一软删与回收站恢复（trash）、工作台草稿采收与定稿（artifact-adopt/finalize）、固定双区日记记录（journal）与行内书签批注（slip）。
---

# 写与整理 · 工作台创作、目录整理、成果定稿与日记书签

## 1. 空间围墙与推荐配方（过程自治，写出口自动顺风车验货）

- **工作台空间边界**：
  - 零散记录落 `20-AI-Zone/`；专向/多报告研究先经 `project-create --ref <vlt_XX> --title "<课题>" --agent <agent>`（或 `workbench-workspace`）分配 `ws-<agent>`，再用 `note-create --ref <vlt_XX> [--project_ref <prj_XX>] --zone workbench --subdir "ws-<agent>/drafts" --title "<标题>" --summary "<摘要>" --by "<agent>"` 创作。
  - **围墙红线**：严禁在工作区根目录自建研究/报告目录双份写盘，严禁 `--subdir 99-Assets`；`21-Library/` 原文只读。
- **高频工具组合**：
  - **改写 / 升阶 / 废弃 / 格式化**：`note-update`、`note-promote`、`note-deprecate`、`note-format`；正文或表格内直接写 `[[nte_XX]]` 或 `[[标题]]` 自动解析双链，断链用 `note-link --mode fix` 批量修复。
  - **搬迁 / 复制 / 回收站**：`vfs-move`、`vfs-copy`；软删与恢复统一用 `./gewu atlas --action trash --ref <vlt_XX> --mode delete|list|restore`（`--note_ref <nte_XX>` 或 `--trash_ref <trs_XX>`）。
  - **工作台成果准入与定稿**：草稿入正式区用 `artifact-adopt --ref <vlt_XX> [--project_ref <prj_XX>] --note_ref <nte_XX> [--target_zone artifacts|library]`；经人类确认后调 `artifact-finalize`。
  - **日记 / 书签 / 协同**：日记用 `journal --ref <vlt_XX> [--mode show|today|append|archive]`；书签用 `slip --ref <vlt_XX> --mode scan|commit|list|get|update|delete`；正式协同用 `ticket-create` / `ticket-list`。

## 2. 分级交付验货闸门（顺风车 `[DELIVERY_GATE]` + Subagent/自评）

- **L1 单步微操作**（记日记、改标签、单卡软删）：写算子返回 `[DELIVERY_GATE: PASS (60/60)]` 即合规直交（0 额外开销）。
- **L2/L3 创作与多卡整理**：
  1. **算子硬评估（60分底线）**：观察最后一步写算子返回的 `delivery_gate`（或显式跑 `guard --mode delivery --ref <vlt_XX>`），若提示 `BLOCKED`（如存在野目录或断链），按 `ORBIT` 指引修复至 `60/60 PASS`。
  2. **语义评估（40分）**：L3 成果报告若有 Subagent 能力优先唤起独立只读 Subagent 按「完整性 15 + 真实性 15 + 可读性与零路径 10」盲审；无 Subagent 则主 Agent 自检达标（总分 ≥ 60）后交付。

全局路由见 [AGENTS.md](../../AGENTS.md)，规范见 [rule-core-vfs.md](../../rules/rule-core-vfs.md)、[rule-core-schema.md](../../rules/rule-core-schema.md) 与 [rule-core-linking.md](../../rules/rule-core-linking.md)。
