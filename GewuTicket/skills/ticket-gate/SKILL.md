---
name: ticket-gate
description: 办理人类预审与完工后审阅（review）、开发授权（develop/leave）、结单（done）、重开（reopen）、7天归档扫盘及影子合入。
---

# 格物工单 · 审阅门禁、结单与归档

人类审阅与开发授权前，须先调用 `ticket-triage` 的 `in` 取得当前 `approval_sha256` 并原样填入 `--expected-sha256`。

## 固定调用骨架（2～4 个语义槽位）

- 人类预审 / 完工审阅：`./gewu ticket --action review --volume <卷路径> --ticket-id <单号> --by <审阅人> --expected-sha256 <原票哈希> --note <决定说明>`（配合 `--set stage=pre|post --set outcome=approve|reject`）
- 结单 / 人类重开：`./gewu ticket --action <done|reopen> --volume <卷路径> --ticket-id <单号> --by <主体>`（`SYS` 票核销 `[L1]` 断点可附 `--set complete_l1=true --note "完成反馈：..."`）
- 系统开发授权：先查单获取当前摘要，再执行 `./gewu ticket --action develop --volume <卷路径> --ticket-id <单号> --by <主体> --expected-sha256 <当前原票哈希> --note "<真实授权依据>"`。卷、单号和主体沿用查单上下文，真正需要决策的槽位为当前摘要与授权依据。
- 退出开发：`./gewu ticket --action leave --volume <卷路径> --ticket-id <单号> --by <主体>`；不与 develop 合并模板，leave 不产生新授权。
- 7天冷归档及30天归档清理 / 影子合入丢弃：`./gewu ticket --action <archive-sweep|shadow-apply|shadow-drop> --volume <卷路径>`

## 允许枚举与示范

- `--action`：`review` | `done` | `reopen` | `develop` | `leave` | `archive-sweep` | `shadow-apply` | `shadow-drop`
- `done` 会自动顺带触发本卷超 7 天 `closed/` → `archive/` 扫盘，以及 `archive/` 中结单超 30 天单据的自动删除（待完工后审阅且配置保留的除外）

```bash
./gewu ticket --action done --volume ./Volumes/GewuTicket-data --ticket-id TICKET-20261114T1600-DOC-antigravity --by Antigravity
```

授权依据必须来自用户真实指令；摘要只校验票据版本，不证明人类身份。review/develop 后重新查单取得新摘要，禁止重复使用已变化的票据哈希。


## 人机协作与查回

用户在当前对话明确要求修改、签阅或结单时，Agent 可转录该真实指令并执行；在 reply/note 中记录指令来源、决定和实际结果即可，不要求另建身份认证流程。派单由创建者确认或有人类明确委托后收口；不得凭 Agent 猜测代替人类决定。

结单后调用 `in` 核对状态。默认归档为 7 天，以 `categories` 返回的有效策略为准；归档和过期删除分开理解。

影子高级操作需显式沿用 `--mount-mode shadow-rw --shadow-dir <独立目录>`。合入成功后查回底卷确认结果，再 `shadow-drop` 清空已跟踪影子增量；目录和锁保留。遇冲突先呈报，不更换参数强行覆盖。
