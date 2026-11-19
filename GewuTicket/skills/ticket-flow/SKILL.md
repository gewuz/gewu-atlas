---
name: ticket-flow
description: 执行工单认领（pickup）、多轮回复（reply）、暂停与恢复（pause/resume）、弃权（abort）及取消（cancel）。
---

# 格物工单 · 在途流转与交接

`open` 与 `in_progress` 共居 `<CATEGORY>/active/`；`pause` 迁往 `paused/`；`cancel` 迁往 `closed/`。未通过阻塞继续前审阅（`pre_review_status`）时 `pickup` 将被阻断。

## 固定调用骨架（2～4 个语义槽位）

- 认领接手：`python3 -I -B scripts/ticket/ticket_cli.py --action pickup --volume <卷路径> --ticket-id <单号> --by <主体>`
- 多轮回复：`python3 -I -B scripts/ticket/ticket_cli.py --action reply --volume <卷路径> --ticket-id <单号> --by <主体> --note <发言>`
- 暂缓搁置 / 恢复：`python3 -I -B scripts/ticket/ticket_cli.py --action <pause|resume> --volume <卷路径> --ticket-id <单号> --by <主体> --note <原因或落实依据>`
- 弃权退回 open / 取消：`python3 -I -B scripts/ticket/ticket_cli.py --action <abort|cancel> --volume <卷路径> --ticket-id <单号> --by <主体>`

## 允许枚举与示范

- `--action`：`pickup` | `reply` | `pause` | `resume` | `abort` | `cancel`
- `pause` 可附 `--set resume_when=<恢复条件>`；`abort` 人类解死锁可附 `--set force=true`

```bash
python3 -I -B scripts/ticket/ticket_cli.py --action reply --volume ./Volumes/GewuTicket-data --ticket-id TICKET-20261114T1600-DOC-antigravity --by Antigravity --note "已完成阶段一切片校验"
```
