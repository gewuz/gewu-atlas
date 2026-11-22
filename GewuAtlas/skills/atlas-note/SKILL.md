---
name: atlas-note
description: Atlas 写与整理（note / note-patch / batch / daemon / vfs / artifact / journal / slip / trash）：创建、局部补丁更新（note-patch）、多步事务批处理（batch）、热内核加速（daemon start/stop）、跨区移动复制、统一软删与回收站恢复（trash）、工作台草稿采收与定稿（artifact-adopt/finalize）、固定双区日记记录（journal）与行内书签批注（slip）。
---

# 写与整理 · 工作台创作、目录整理、成果定稿与日记书签

## 1. 空间围墙与推荐配方（过程自治，写出口自动顺风车验货）

- **工作台空间边界**：
  - 零散记录落 `20-AI-Zone/`；专向/多报告研究先经 `project-create --ref <vlt_XX> --title "<课题>" --agent <agent>`（或 `workbench-workspace`）分配 `ws-<agent>`，再用 `note-create --ref <vlt_XX> [--project_ref <prj_XX>] --zone workbench --subdir "ws-<agent>/drafts" --title "<标题>" --summary "<摘要>" --by "<agent>"` 创作。
  - **围墙红线**：严禁在工作区根目录自建研究/报告目录双份写盘，严禁 `--subdir 99-Assets`；`21-Library/` 原文只读。
- **高频工具组合**：
  - **局部补丁（首选改长文）与事务批处理**：
    - 局部增量编辑：`note-patch --ref <vlt_XX> --note_ref <nte_XX> --old_text "<旧片段>" --new_text "<新片段>"`（或 `--section "## 结论" --content "<新正文>"` / `--mode append|prepend --content "<追加内容>"` / 原子多段补丁 `--ops_json '[{"op":"section","section":"## 结论","content":"..."},{"op":"append","content":"..."}]'`，每项必填 `"op": "replace|section|append|prepend"`；跨多卡批量替换可用 `--note_refs nte_01,nte_02` 配合 `--dry_run true` 预演，未命中标题时报错直接附大纲），自动保护 MOC/Workbench 受控块、原文只读与日记人类区，免整篇重写；
    - 多步事务合批：`cat <<'EOF' | ./gewu atlas --action batch --ref <vlt_XX> [--project_ref <prj_XX>] --json-stdin`（传入 `{"steps":[{"id":"s1","action":"note-create","args":{...}}]}`，支持 `$s1.note_ref` / `$g.note_refs` 前序串联；全只读批次自动走共享锁零 DOC 票快路径，写批次合并锁、DOC 票、快照与末尾验货）；
    - 多步任务热内核提速：开工 `./gewu atlas daemon start`（零宿主 MCP 依赖，自动加速所有常规算子），收工 `./gewu atlas daemon stop`（或空闲自动退出）。
  - **改写 / 升阶 / 废弃 / 格式化**：`note-update`、`note-promote`、`note-deprecate`、`note-format`；正文或表格内直接写 `[[nte_XX]]` 或 `[[标题]]` 自动解析双链，断链用 `note-link --mode fix` 批量修复。
  - **搬迁 / 复制 / 回收站**：`vfs-move`、`vfs-copy`；软删与恢复统一用 `./gewu atlas --action trash --ref <vlt_XX> --mode delete|list|restore`（`--note_ref <nte_XX>` 或 `--trash_ref <trs_XX>`）。
  - **工作台成果准入与定稿**：草稿入正式区用 `artifact-adopt --ref <vlt_XX> [--project_ref <prj_XX>] --note_ref <nte_XX> [--target_zone artifacts|library]`；经人类确认后调 `artifact-finalize`。
  - **日记 / 书签 / 协同**：日记用 `journal --ref <vlt_XX> [--mode show|today|append|archive]`；书签用 `slip --ref <vlt_XX> --mode scan|commit|list|get|update|delete`；正式协同用 `ticket-create` / `ticket-list`。

## 2. 分级交付验货闸门（顺风车 `[DELIVERY_GATE]` + Subagent/自评）

- **L1 单步微操作**（记日记、改标签、单卡软删）：写算子返回 `[DELIVERY_GATE: PASS (60/60)]` 即合规直交（0 额外开销）。
- **L2/L3 创作与多卡整理**：
  1. **算子硬评估（60分底线）**：观察最后一步写算子返回的 `delivery_gate`（或显式跑 `guard --mode delivery --ref <vlt_XX>`），若提示 `BLOCKED`（如存在野目录或断链），按 `ORBIT` 指引修复至 `60/60 PASS`。
  2. **语义评估（40分）**：L3 成果报告若有 Subagent 能力优先唤起独立只读 Subagent 按「完整性 15 + 真实性 15 + 可读性与零路径 10」盲审；无 Subagent 则主 Agent 按同一标准自评；分别记录硬评与语义审阅结果；两项均通过方可交付。漏答核心诉求、关键结论无依据、拼贴检索碎片或泄露物理路径均判语义审阅失败。

全局路由见 [AGENTS.md](../../AGENTS.md)，规范见 [rule-core-vfs.md](../../rules/rule-core-vfs.md)、[rule-core-schema.md](../../rules/rule-core-schema.md) 与 [rule-core-linking.md](../../rules/rule-core-linking.md)。
