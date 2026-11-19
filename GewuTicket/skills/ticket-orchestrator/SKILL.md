---
name: ticket-orchestrator
description: 格物工单总控编排：按任务阶段路由至建单、查票、流转或审阅归档微技能，并遵守数据卷挂载与执行路由契约。
---

# 格物工单总控编排

先核对 [工单卷与协作规则](../../rules/rule-ticket-volume-and-ipc.md) 与 [工单状态契约](../../contracts/ticket-state-and-schema-v1.md)。若任务涉及修改受管源码/规则/技能，按 [execution-routing-v1.md](../../contracts/execution-routing-v1.md) 与 [sys-change-ticket-v1.md](../../contracts/sys-change-ticket-v1.md) 走 `SYS` 票与 `./gewu ticket manage` 闭环；日常工单协同走下表微技能。

## 场景路由表

| 协同场景 | 必调微技能 |
| :--- | :--- |
| 初始化数据卷、注册新分类或立项建单 | [ticket-create](../ticket-create/SKILL.md) |
| 只读查看单据详情、队列筛选、分类策略或影子差异 | [ticket-triage](../ticket-triage/SKILL.md) |
| 认领接手、多轮回复、暂停/恢复、弃权或取消 | [ticket-flow](../ticket-flow/SKILL.md) |
| 人类预审/完工审阅、开发授权、结单归档、7天扫盘与影子合入 | [ticket-gate](../ticket-gate/SKILL.md) |

## 领空表速检

```bash
python3 -I -B scripts/ticket/ticket_cli.py --template
```

遇 `GUARD: INPUT_REQUIRED` 或 `GUARD: HALTED`（`禁止重试`）立即停止并向人类汇报，严禁换参数绕过或探测运行态目录。
