# 接口契约：目标驱动执行路由与双态胶囊（`gewu.execution-routing/2`）

## 1. 架构定位（零 `GewuTrack` 耦合）

`GewuForge` 与其锻造的新格物域已与 `GewuTrack` 彻底解绑，所有域生命周期与日常操作统一走两条正交轨道：

1. **系统治理轨（`SYS` 工单 + Git 基线）**：
   - 修改任何域的系统文件（`AGENTS.md`、`rules/`、`contracts/`、`skills/`、`scripts/`、`dicts/`、`templates/`、`tests/`）前，必须先在 `GewuTicket`（`./Volumes/GewuTicket-data`）开立 `SYS` 票并取得人类 `pre` 审阅批准进入 `in_progress`。
   - 完工后通过 **60 分算子硬评估 + 40 分 Subagent/自评软量表** 验货并执行 Finish Commit，再 `done` 结单。
2. **受控算子与胶囊顺风车轨（Unified Facade + Triad/Guard Capsule）**：
   - 所有目标驱动动作、断点保存/恢复、对象维护与验收均通过统一网关 `./gewu <domain> --action <...>` 执行。
   - 每次算子出口必须返回结构化 **Triad Capsule**（`STATE` / `ORBIT` / `ANCHOR`）或 **Guard Capsule**（`PARAM_FIX` / `INPUT_REQUIRED` / `HALTED`），并在写算子出口顺风车搭载 `[FORGE_GATE: PASS/BLOCKED]` 与 `[CHECKPOINT: <stage>]`。

## 2. 三级门禁与熔断红线

| 门禁状态 | 退出码 | 语义与动作 |
| :--- | :--- | :--- |
| `TRIAD`（正常推进） | `0` | 给出当前坐标、可选下一步与顺风车闸门/断点锚点 |
| `GUARD: PARAM_FIX` | `1` | 缺参或参数非法，附最小填空模板；同指纹连败 2 次自动升格 `GUARD: HALTED` |
| `GUARD: INPUT_REQUIRED` | `1` | 需人类确认（如 `--cli_alias`、数据卷策略、未完结断点强行覆盖或高风险发布），立即停手等待确认 |
| `GUARD: HALTED` | `2` | 安全越界、非法字段、循环空转或内部异常（堆栈收敛至 `.local/logs/`），**禁止重试**，立即向人类汇报 |
