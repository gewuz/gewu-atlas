# Agent 实战经验路书（usecase-for-agent）

> **给 Agent 的先行指引**：本目录存放历次真实任务沉淀下来的**高效率避坑路书与高分交付配方（Usecase Playbooks）**。
> - **开工前（读经验）**：处理联网入库、原子萃取、多篇专题研究或遇到异常卡壳时，先阅读下方索引表（或调 `./gewu atlas --action usecase --mode match --query "<关键词>"`），直接复用前人验证过的「损耗 → 对策」与「黄金流水线」，可省去 30%~50% 的交互往返。
> - **复盘时（写心得）**：当任务出现重试波折、或人类提示「记录本次心得 / 生成 usecase」时，调用 `./gewu atlas --action usecase --mode upsert` 将「高频损耗 → 下次直通对策 + 质量配方」沉淀或合并到对应卡片中。

## 实战 Usecase 索引表

<!-- usecase-index:begin -->
| 经验卡 ID (`uc_id`) | 标题 | 适用场景与触发词 | 标签 | 最近更新 |
| :--- | :--- | :--- | :--- | :--- |
| `UC-00-cold-start-onboarding` | [格物志冷启动新手引导与常见玩法速查路书](UC-00-cold-start-onboarding.md) | 第一次冷启动、探索格物志能做什么、询问下一步做什么、13个高频场景提示词与精确指令速查 | `Onboarding`, `ColdStart`, `QuickStart`, `Prompts`, `Commands`, `Discovery` | 2026-11-22 |
| `UC-01-web-ingest-and-extract` | [联网抓源、切片与原子萃取避坑路书](UC-01-web-ingest-and-extract.md) | 网页/URL抓取入库、ingest --auto_slice、fmt 运行时预检、atomic-extract 多源 provenance 与逗号切片防错、note-link 批修与相对图片断链清理 | `Ingest`, `URL`, `Slice`, `Extract`, `Preflight`, `Provenance`, `LinkFix` | 2026-11-20 |
| `UC-02-deep-research-and-delivery` | [多报告深度研究、前置盲审与高分交付路书](UC-02-deep-research-and-delivery.md) | 课题研究(prj_XX)、多篇报告撰写、正文去机械标签与行内句柄、文末集中溯源、草稿期前置验收与 artifact-adopt/finalize | `Research`, `Artifact`, `BlindReview`, `HighScore`, `Verification`, `Readability` | 2026-11-20 |
| `UC-03-methodology-extract-and-multi-moc` | [方法论萃取与多 MOC 自述漫游路书](UC-03-methodology-extract-and-multi-moc.md) | 已入库资料提炼方法论、多主题 MOC 生成、多源 provenance 精准溯源、漫游自述与扩展建议回填 | `Methodology`, `MOC`, `Roam`, `Extract`, `Governance`, `Reflection`, `Provenance` | 2026-11-20 |
| `UC-04-fast-l3-research-and-batch-ops` | [L3 深度研究极速流水线与批量防返工路书](UC-04-fast-l3-research-and-batch-ops.md) | 自主深度研究提速、热内核 daemon start/stop、原生 batch 事务合批、note-patch 局部改写、project-brief/note-grep/outline 低回合读、双任务复盘、正文去标签与文末集中溯源、多卡批量萃取、MOC 索引保护、一次过 95+ 盲审 | `Performance`, `BatchOps`, `Daemon`, `NotePatch`, `Research`, `LaTeX`, `BlindReview`, `Optimization`, `Heredoc`, `Readability` | 2026-11-22 |
| `UC-05D-diffuse-deep-research` | [UC-05D 扩散研究：模拟 Gemini Deep Research 广撒网与全量入库路书](UC-05D-diffuse-deep-research.md) | 扩散研究、广撒网建库、资料全量下载入库、问题域尚未收敛、领域基线语料库、Gemini Deep Research、研究计划先审后研、D 型任务 | `DeepResearch`, `Diffuse`, `D`, `Workflow`, `UseCase`, `Ingest` | 2026-11-20 |
| `UC-06F-focused-deep-research` | [UC-06F 聚焦研究：ChatGPT Deep Research 五步对齐与决策件交付路书](UC-06F-focused-deep-research.md) | 聚焦研究、问题域已收敛、3–5 问对齐、章节级大纲、决策级白皮书、快速交付、选型类课题、F 型任务 | `DeepResearch`, `Focused`, `F`, `Workflow`, `UseCase`, `DecisionBrief`, `Pitfalls` | 2026-11-20 |
| `UC-07-dropzone-note-create-and-verify` | [DropZone 归档写入与单卡交付验货避坑路书](UC-07-dropzone-note-create-and-verify.md) | DropZone/agent-chat 写笔记、note-create 参数自省与 status 枚举校验、外部同步笔记 Frontmatter 防踩坑、全库既有断链触发 BLOCKED 时的单卡定向核验 | `DropZone`, `NoteCreate`, `Schema`, `Template`, `DeliveryGate`, `Links` | 2026-11-21 |
| `UC-08-batch-in-out-locate-and-edit` | [批处理入库、出库定位与跨卡修改避坑路书](UC-08-batch-in-out-locate-and-edit.md) | 批处理入库、批处理出库、纯只读batch快路径、$step_id.note_refs串联、note-grep --with_section、note-patch --ops_json与--note_refs跨卡替换、atomic-extract多源合批、沙箱热内核快速降级 | `BatchOps`, `Ingest`, `Locate`, `NotePatch`, `NoteGrep`, `ReadOnlyBatch`, `Pitfalls`, `Performance` | 2026-11-22 |
<!-- usecase-index:end -->

## 标准三段式记录规范

1. **`## 1. 适用场景与触发信号`**：简明列出何时应查阅本卡。
2. **`## 2. 高频损耗 → 直通对策`**：采用 `损耗点 | 上次表现 | 下次直通做法` 对比表，只写可执行命令与参数边界，不写冗长检讨。
3. **`## 3. 黄金流水线与高分交付配方`**：给出最少步数的算子组合序列与冲刺 85+ 分的质量锚点。
