# GewuTicket Agent 实战经验路书（`usecase-for-agent`）

> **给 Agent 的先行指引**：本目录存放历次真实协同任务沉淀下来的**避坑路书与标准流水线**。
> - **开工前（读经验）**：处理 `SYS` 系统变更审批、双维审阅防重放、`ro`/`shadow-rw` 挂载或遇到胶囊熔断前，先检索下方索引（或调 `./gewu ticket --action usecase --mode match --query "<关键词>"`）。
> - **复盘时（写心得）**：遇参数返工、SHA 冲突或新避坑配方时，调 `./gewu ticket --action usecase --mode upsert` 沉淀并自动刷新本索引。

## 实战 Usecase 索引表

<!-- usecase-index:begin -->
| 经验卡 ID (`uc_id`) | 标题 | 适用场景与触发词 | 标签 | 最近更新 |
| :--- | :--- | :--- | :--- | :--- |
| `UC-01-sys-change-and-dual-review` | [SYS 系统变更闭环与双维审阅防重放路书](UC-01-sys-change-and-dual-review.md) | 修改系统源码/契约/技能前的 SYS 建单、pre/post 审阅、develop 授权与 done 结单 | `SYS`, `review`, `develop`, `sha256`, `done` | 2026-11-19 |
| `UC-02-shadow-mount-and-readonly-triage` | [只读查票、shadow-rw 影子协作与冷归档路书](UC-02-shadow-mount-and-readonly-triage.md) | ro 只读卷核查、分段读大票、shadow-rw 隔离修改与安全合入、7/30天归档扫盘 | `ro`, `shadow-rw`, `triage`, `archive-sweep` | 2026-11-19 |
| `UC-03-breaker-capsule-and-zero-leak` | [四通道传参、双态胶囊防熔断与零泄漏路书](UC-03-breaker-capsule-and-zero-leak.md) | 领空模板校验、防同参连续报错/空转熔断、长正文 stdin 传入及 GUARD 拦截处理 | `capsule`, `breaker`, `stdin`, `template`, `guard` | 2026-11-19 |
<!-- usecase-index:end -->

## 标准三段式记录规范

1. **`## 1. 适用场景与触发信号`**：简明列出何时应查阅本卡。
2. **`## 2. 高频损耗 → 直通对策`**：采用 `损耗点 | 上次表现 | 下次直通做法` 对比表，只写可执行命令与参数边界。
3. **`## 3. 黄金流水线与避坑锚点`**：给出最少步数的工单动作序列与门禁防错检查项。
