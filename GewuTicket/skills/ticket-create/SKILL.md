---
name: ticket-create
description: 在指定数据卷初始化工单卷、动态注册工单分类或创建新工单（建单）。
---

# 格物工单 · 建单与卷配置

先以 `--template` 查看缺省槽位。支持 `--content -` 从标准输入传入正文摘要。

## 固定调用骨架（2～4 个语义槽位）

- 初始化数据卷：`python3 -I -B scripts/ticket/ticket_cli.py --action init-volume --volume <卷路径> --mount-mode <rw|ro|shadow-rw>`
- 注册扩展分类：`python3 -I -B scripts/ticket/ticket_cli.py --action register-category --volume <卷路径> --category <大写分类> --title <分类名>`
- 创建新工单：`python3 -I -B scripts/ticket/ticket_cli.py --action out --volume <卷路径> --category <分类> --from <主体> --title <标题>`

## 允许枚举与示范

- `--mount-mode`：`rw` | `ro` | `shadow-rw`
- `--category`：已注册大写分类（默认预置 `SYS` | `DOC` | `MSG` | `TASK` | `SLIP`，支持 `register-category` 扩展）
- 可选扩展槽位（`--set` / `--payload` 传入）：`to_agent`、`project_id`、`need_human`、`write_scope`、`shadow_dir`

```bash
python3 -I -B scripts/ticket/ticket_cli.py --action out --volume ./Volumes/GewuTicket-data --category DOC --from Antigravity --title "整理知识卷标索引"
```


初始化已存在的卷不会重置配置。默认归档为 7 天。

同一任务重试建单时使用稳定 `--key <任务键>`，同主体、分类的在途原票可直接复用；同 key 的请求正文或范围不同会报告冲突，应查阅原票并通过 reply 追加进展，不能换 key 掩盖冲突。旧票无 request_key 时先查单复用。
