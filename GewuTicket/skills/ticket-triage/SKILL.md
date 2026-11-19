---
name: ticket-triage
description: 只读查阅工单详情、列出在途/待审/暂停/归档队列、检索工单及对比影子增量差异（兼容 ro 只读卷）。
---

# 格物工单 · 只读查阅与检索

本卡片严格只读，可在 `ro`、`rw` 与 `shadow-rw` 挂载模式下安全执行。

## 固定调用骨架（2～4 个语义槽位）

- 查看单票详情与 `approval_sha256`（加 `--full` 返回完整工单正文与 `raw_text`，或用 `--start-line`/`--end-line` 分段）：`python3 -I -B scripts/ticket/ticket_cli.py --action in --volume <卷路径> --ticket-id <单号> [--full] [--start-line <起>] [--end-line <止>] [--mount-mode <rw|ro|shadow-rw>]`
- 队列列表筛选：`python3 -I -B scripts/ticket/ticket_cli.py --action list --volume <卷路径> [--category <分类>] [--status <状态>]`
- 关键词/审阅态检索：`python3 -I -B scripts/ticket/ticket_cli.py --action find --volume <卷路径> [--query <关键词>] [--post-review-status <待审态>]`
- 查看分类策略或影子差异：`python3 -I -B scripts/ticket/ticket_cli.py --action <categories|shadow-diff> --volume <卷路径> [--shadow-dir <影子路径>]`

## 允许枚举与示范

- `--status`：`open` | `in_progress` | `paused` | `done` | `cancelled`
- `--post-review-status`：`not_required` | `pending_review` | `approved` | `changes_requested`

```bash
python3 -I -B scripts/ticket/ticket_cli.py --action list --volume ./Volumes/GewuTicket-data --status open
```
