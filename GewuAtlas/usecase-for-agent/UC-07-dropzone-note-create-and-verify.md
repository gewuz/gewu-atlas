---
doc_type: "usecase"
uc_id: "UC-07-dropzone-note-create-and-verify"
title: "DropZone 归档写入与单卡交付验货避坑路书"
scenarios: "DropZone/agent-chat 写笔记、note-create 参数自省与 status 枚举校验、外部同步笔记 Frontmatter 防踩坑、全库既有断链触发 BLOCKED 时的单卡定向核验"
tags: ["DropZone", "NoteCreate", "Schema", "Template", "DeliveryGate", "Links"]
updated: "2026-11-21"
updated_by: "Antigravity"
---

## 1. 适用场景与触发信号

- **适用场景**：向 `00-DropZone/<subdir>`（如 `agent-chat`）或任意受控分区调用 `note-create` 归档对话总结、随记草稿或方法论笔记，并在写入后完成精准验货。
- **触发词**：放到 DropZone、存入 `00-DropZone/agent-chat`、写一篇笔记到收件箱、`note-create`、归档总结。

## 2. 高频损耗 → 直通对策

| 损耗点 | 上次表现 | 下次直通做法 |
| :--- | :--- | :--- |
| **照抄外部同步笔记的 `status: draft` 触发参数拦截** | 读取 `00-DropZone/agent-chat` 内由外部 Web Chat + GitHub MCP 写入的笔记看到 `status: draft`，调用 `note-create --status draft` 直接触发 `E_PARAM_INVALID`（同错连败 2 次即熔断） | 切勿照搬外部工具同步进来的 Frontmatter 值；写卡前对齐 `GewuAtlas/dicts/metadata.yaml` 的 `status` 白名单（仅允许 `active` / `pending` / `deprecated` / `archived`），DropZone 待分拣笔记统一传 `--doc_type memo --status pending` |
| **盲目 `grep` 探查动作参数浪费轮次** | 执行 `atlas capabilities \| grep` 与 `grep "note-create:" commands.yaml` 均无命中（返回码 1），额外消耗 3 轮只读命令才查齐参数 | 查任意动作的必填与可选槽位，**第一步直接跑 `./gewu.command atlas --action <动作名> --template`**（如 `--action note-create --template`），单步获取完整参数清单 |
| **单卡写入被全库既有断链触发 `BLOCKED`** | 新建单卡后出口胶囊提示 `[DELIVERY_GATE: BLOCKED (45/60)] 存在 5 处悬空双链`，易误判为本卡问题或越权改动无关历史笔记 | 单卡写入后如全库顺风车报断链，立即运行 `./gewu.command atlas --action vfs-links --ref <vlt_XX> --note_ref <nte_XX>` 做单卡定向核验；确认 `dangling_count: 0` 且 `boundary_clean: true` 后，向人类如实区分「本卡无断链」与「库内既有历史断链」，严禁修改无关文件 |

## 3. 黄金流水线与高分交付配方

1. **一次摸底与参数自省**：并行调用 `./gewu.command atlas --action vault` 与 `./gewu.command atlas --action note-create --template`；如需参考目标子目录既有标签与命名风格，补跑一次 `note-list --ref <vlt_XX> --zone dropzone`（但 `doc_type`、`status` 等枚举字段一律以 `metadata.yaml` 白名单为准）。
2. **合规写卡**：按 `atlas-style`（盘古空格、标点外置、序号互斥、零物理路径）组织正文，执行 `./gewu.command atlas --action note-create --ref <vlt_XX> --zone dropzone --subdir "<subdir>" --doc_type memo --status pending --tags "<tags>" --by "<agent>" --title "<标题>" --summary "<摘要>" --content "<正文>"`。
3. **单卡定界验货**：若写卡出口 `delivery_gate` 因全库既有断链提示 `BLOCKED`，补跑 `./gewu.command atlas --action vfs-links --ref <vlt_XX> --note_ref <nte_XX>` 验证本卡 `dangling_count: 0`，确认空间边界洁净后直接向人类汇报 `vlt_XX`、`nte_XX` 与标题即可。
