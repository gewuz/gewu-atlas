# Agent 实战经验路书（usecase-for-agent）

> **给 Agent 的先行指引**：本目录存放历次真实任务沉淀下来的**高效率避坑路书与高分交付配方（Usecase Playbooks）**。
> - **开工前（读经验）**：处理联网入库、原子萃取、多篇专题研究或遇到异常卡壳时，先阅读下方索引表（或调 `./gewu atlas --action usecase --mode match --query "<关键词>"`），直接复用前人验证过的「损耗 → 对策」与「黄金流水线」，可省去 30%~50% 的交互往返。
> - **复盘时（写心得）**：当任务出现重试波折、或人类提示「记录本次心得 / 生成 usecase」时，调用 `./gewu atlas --action usecase --mode upsert` 将「高频损耗 → 下次直通对策 + 质量配方」沉淀或合并到对应卡片中。

## 实战 Usecase 索引表

<!-- usecase-index:begin -->
| 经验卡 ID (`uc_id`) | 标题 | 适用场景与触发词 | 标签 | 最近更新 |
| :--- | :--- | :--- | :--- | :--- |
| `UC-01-web-ingest-and-extract` | [联网抓源、切片与原子萃取避坑路书](UC-01-web-ingest-and-extract.md) | 网页/URL抓取入库、ingest --auto_slice、fmt 运行时预检、atomic-extract 句柄类型防错 | `Ingest`, `URL`, `Slice`, `Extract`, `Preflight` | 2026-11-19 |
| `UC-02-deep-research-and-delivery` | [多报告深度研究、前置盲审与高分交付路书](UC-02-deep-research-and-delivery.md) | 课题研究(prj_XX)、多篇报告撰写、草稿期前置验收、引用真值核验与 artifact-adopt/finalize | `Research`, `Artifact`, `BlindReview`, `HighScore`, `Verification` | 2026-11-18 |
| `UC-03-methodology-extract-and-multi-moc` | [方法论萃取与多 MOC 自述漫游路书](UC-03-methodology-extract-and-multi-moc.md) | 已入库资料提炼方法论、多主题 MOC 生成、漫游自述与扩展建议回填 | `Methodology`, `MOC`, `Roam`, `Extract`, `Governance`, `Reflection` | 2026-11-19 |
<!-- usecase-index:end -->

## 标准三段式记录规范

1. **`## 1. 适用场景与触发信号`**：简明列出何时应查阅本卡。
2. **`## 2. 高频损耗 → 直通对策`**：采用 `损耗点 | 上次表现 | 下次直通做法` 对比表，只写可执行命令与参数边界，不写冗长检讨。
3. **`## 3. 黄金流水线与高分交付配方`**：给出最少步数的算子组合序列与冲刺 85+ 分的质量锚点。
