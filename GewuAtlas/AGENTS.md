# 格物志（GewuAtlas · ws-5 围墙与分级验货版）

本域采用 **空间围墙（受控卷与工作台）+ 实战路书（[usecase-for-agent/README.md](usecase-for-agent/README.md)）+ 工具攻略（8 个意图 Skill 与 Core 算子）+ 分级双轨验货（60 分算子硬底线 + 40 分 Subagent/自评软量表）** 架构。Agent 在指定空间内可自主规划与组合工具，通过算子返回胶囊（`[QA_GATE]` / `[DELIVERY_GATE]`）实时校准并保障最终交付物合格。进库先调 `./gewu atlas --action vault` 一步取得当前上下文与全部库句柄。

## 1. 空间围墙与分级交付闸门

0. **实战经验路书入口（`usecase-for-agent` · 开工可读、复盘可写）**：
   - **开工前读路书**：本域在 [usecase-for-agent/README.md](usecase-for-agent/README.md) 中维护了实战避坑路书与高分交付配方；接手联网入库、原子萃取或多篇专题研究前，推荐先阅读该 `README.md` 或调用 `./gewu atlas --action usecase --mode match --query "<关键词>"` 复用最佳流水线。
   - **异常/复盘写心得**：当运行任务出现重试波折、多次交互才更正交付物，或人类要求「记录本次心得 / 生成 usecase」时，激活 [atlas-usecase](skills/atlas-usecase/SKILL.md) 调用 `./gewu atlas --action usecase --mode upsert` 增删改查经验卡（自动同步 `README.md` 索引）。
1. **工作台空间围墙（在哪干活）**：
   - 零散单条记录未指定课题时落 `20-AI-Zone/`；**凡专向调研、多方向分析或提交多篇报告的自主研究任务**，调用 `project-create --ref <vlt_XX> --title "<课题名>" --agent <名>` 在 `30-Projects/` 开立课题 `prj_XX` 并一步分配 `ws-<agent>` 工作台（或单独调 `workbench-workspace`），使用 `--subdir ws-<agent>/drafts` 创作。
   - **根目录绝对净空**：严禁在工作区根目录自建 `*-research/`、`reports/`、`sources/`，严禁在课题下自建 `99-Assets` 或使用原生 shell 在 `Volumes/` 创建文件，严禁双份落盘。外部文献统一通过 `ingest --urls "<URL1>,<URL2>" --auto_slice true` 归口。
2. **三级自适应交付验货（怎么交卷：60 分算子硬评估 + 40 分 Subagent/自评）**：
   - **L1 普通单步任务**（记日记、删移单卡、改标签）：写算子内置 Schema 与围墙校验，返回 `OK_*` 即代表合规，**0 额外开销直接交付**。
   - **L2 局部轻任务**（查库问答、单篇入库、单卡创作）：
     - 问答默认由 `note-search`（`--mode passage`，返回 `[QA_GATE: PASSAGE_READY]` 完整命中段）直接循证作答；仅当需要完整上下文或跨卡深挖（`--mode deep`）时再调 `note-get --note_refs ...` 补读全文；
     - 输出前在脑内对照 **通用三维量表（① 完整性 15分：答全用户原话诉求；② 真实性 15分：结论绑定真实 `nte_XX` 句柄且未命中时诚实声明；③ 可读性 10分：连贯成文禁拼贴列表、零物理路径）** 自检后交付。
   - **L3 复合重度任务**（开课题多源调研、批量入库+切片+萃取+织网）：
     - 每次写算子出口自动顺风车返回 `delivery_gate` 状态；收尾可显式调用 `./gewu atlas --action guard --ref <vlt_XX> --mode delivery [--project_ref <prj_XX>] [--note_refs ...]` 完成 **60 分算子硬评估**（空间净空 15 + Schema 15 + 双链与引用真值 15 + 审批边界 15）；
     - 随后执行 **40 分语义评估**：**若当前 Agent 具备 Subagent 调用能力，优先唤起独立只读 Subagent 按三维量表盲审打分；若无 Subagent 能力，则由主 Agent 显式自我评估**，总分 ≥ 60 且无硬伤方可交付。
3. **统一门面与零路径盲化**：工作区根执行 `./gewu atlas <cmd>`（或 `./gewu.command atlas <cmd>`）；严禁原生探测 `Volumes/GewuAtlas-data/`、`scripts/app_core/`、`.local/`、`.runtime/`；对外只传短句柄（`vlt_XX`/`prj_XX`/`nte_XX`/`slp_XX`/`trs_XX`）；遇 `GUARD: INPUT_REQUIRED` 或 `GUARD: HALTED` 立即停手汇报。
4. **高效执行约定（自托管热内核 + 局部/批处理原语，免宿主 MCP 依赖）**：
   - **热内核启停**：L2/L3 多步任务建议开工 `./gewu atlas daemon start [--preload-vault <vlt_XX>]`、收工 `./gewu atlas daemon stop`（空闲默认 600s 自动自毁清空内存）；运行期间所有常规 `--action` 自动复用内存热索引与热 KEK，未启动或沙箱受限自动无感降级（支持 MCP 的宿主亦可选 `./gewu atlas mcp`）。
   - **少回合高密度原语**：局部改长文/MOC 优先用 `note-patch`（自动保护受控块、原文只读与日记人类区）；多步连续写入优先用 `batch`（单锁、合并 `DOC` 票与 `chg_XX` 后悔药、支持 `$step_id.field` 串联）；全局摸底优先用 `project-brief`、`note-grep` 与 `note-get --outline true`。草稿区（`zone=workbench`）写入自动走 $O(1)$ 脏卡顺风车，跨入正式区才跑全库 60/60 验货。

## 2. 行为规范（Rules）与配置字典（Dicts）唯一来源

| 规范或字典 | 唯一权威来源 |
| :--- | :--- |
| 执行边界、会话定域、受控写入、工单门禁、版本摘要绑定、协作锁与 URL 投递 | [rule-arch-workflows-ipc.md](rules/rule-arch-workflows-ipc.md) |
| 多智能体署名、SYS/DOC 双轨版本控制、人类手工删改保护（状态 4/2）与 30 天清理 | [rule-arch-multi-agent-collab.md](rules/rule-arch-multi-agent-collab.md) |
| 状态/容器/身份三元正交、课题分形同构、冻结拦截与成果两阶段准入 | [rule-core-state-identity.md](rules/rule-core-state-identity.md) |
| 虚拟文件系统、零路径盲化、子目录白名单/扁平区红线、日记只归档不删与碰撞策略 | [rule-core-vfs.md](rules/rule-core-vfs.md) |
| 双链语法、管道别名、双向溯源闭环、搬迁级联/降级与死链孤岛治理 | [rule-core-linking.md](rules/rule-core-linking.md) |
| 双层正交 Frontmatter、多源 `provenance` 一致性与轻量日记约束 | [rule-core-schema.md](rules/rule-core-schema.md) / [metadata.yaml](dicts/metadata.yaml) |
| 思维模型化合、认识论四分类（`OBJ/SUB/SYN/HYP`）、萃取透镜与 MOC 宏观叙事 | [rule-core-methodology.md](rules/rule-core-methodology.md) / [methodology.yaml](dicts/methodology.yaml) |
| Tier 1–3 分级排版、盘古留白、标点外置熔断与安全 Mermaid | [rule-core-formatting-tiers.md](rules/rule-core-formatting-tiers.md) |
| 全库目录拓扑骨架与门面动作参数字典（含 Core 主干与交付验货动作） | [layout.yaml](dicts/layout.yaml) / [commands.yaml](dicts/commands.yaml) |

## 3. 业务技能（8 个高内聚 Skill）与 Core 动作路由

| 分类 | 业务场景与触发意图 | 推荐技能与核心算子入口 |
| :--- | :--- | :--- |
| **找与读** | 查库定位、跨库/单库检索、批量读卡（`--note_refs`/`--outline`）、跨卡精查（`note-grep`）、一屏全景（`project-brief`）、源码直读（`file-read`）与循证问答 | [atlas-qa](skills/atlas-qa/SKILL.md) / `./gewu atlas --action vault\|note-search\|note-grep\|project-brief\|note-list\|note-get\|file-read\|graph-report\|vfs-links` |
| **写与整理** | 笔记创建、局部补丁（`note-patch`）、事务批处理（`batch`）、目录搬迁、软删恢复（`trash`）、采收定稿、日记与书签 | [atlas-note](skills/atlas-note/SKILL.md) / `./gewu atlas --action note-create\|note-patch\|note-update\|batch\|note-promote\|note-deprecate\|vfs-move\|vfs-copy\|trash\|artifact-adopt\|artifact-finalize\|journal\|slip\|ticket-*` |
| **入库加工** | 新文档/URL/DropZone 入库（`ingest --auto_slice` 一步转译切片）、`deep` 精修、原子萃取、审批转正与 `moc-sync` 织网 | [atlas-ingest-flow](skills/atlas-ingest-flow/SKILL.md) / `./gewu atlas --action ingest\|fmt-window\|fmt-apply-window\|fmt-assets\|note-slice\|atomic-extract\|atomic-approve\|moc-sync\|note-link` |
| **库与课题** | 库切换与创建/接管（`vault`/`vault-create`/`vault-import`）、课题域 CRUD/合并拆分撤回/公开退网冻结与工作台空间 | [atlas-project](skills/atlas-project/SKILL.md) / `./gewu atlas --action vault\|vault-create\|vault-import\|vault-update\|index-sync\|project-*\|workbench-*` |
| **验货与运维** | 交付闸门 60 分算子硬评估（`guard --mode delivery`）、字典体检、断链校验、DOC Git 同步与 `chg_XX` 后悔药 | [atlas-ops](skills/atlas-ops/SKILL.md) / `./gewu atlas --action guard --mode delivery\|health\|schema\|validate\|links\|doc-status\|doc-commit\|clean-attachments\|archive-*\|undo\|retention-sweep` |
| **实战路书** | 开工前查阅 [usecase-for-agent/README.md](usecase-for-agent/README.md) 避坑对策、任务复盘增删改查 Usecase 经验卡 | [atlas-usecase](skills/atlas-usecase/SKILL.md) / `./gewu atlas --action usecase --mode list\|get\|match\|upsert\|delete` |
| **排版底座** | Tier 1–3 分级写作排版、安全 Mermaid 图谱与 Obsidian/对话双通道呈现 | [atlas-style](skills/atlas-style/SKILL.md) |
| **Obsidian 格式** | Obsidian Markdown（`.md` 双链/嵌入/Callout/高亮）、Bases（`.base` 视图/过滤/公式）、JSON Canvas（`.canvas` 白板节点/连线）与 Dataview（`dataview`/`dataviewjs` DQL 动态查询与内联表达式）规范 | [atlas-obsidian](skills/atlas-obsidian/SKILL.md)（`references/` 按需查阅） |

## 4. 始终保留的安全与协作边界

- **系统轨与内容轨分离（`SYS` 强前置）**：修改任何系统文件（`AGENTS.md`、`rules/`、`contracts/`、`skills/`、`scripts/`、`dicts/`、`tests/`）前，**必须先**在 `./Volumes/GewuTicket-data` 开立 `SYS` 票并取得人类 `pre` 审阅批准进入 `in_progress`（禁止无单先改），改毕经 `./gewu atlas manage` 与 Finish Commit 收口结单；日常内容写动作自动绑定 `DOC` 票、`supervised` 候选审批（绑定 `expected_sha256`、禁 `--auto_approve`）、软删降级、成果定稿与课题公开须等待人类拍板。
- **原文与人类改动保护**：`21-Library/` 原文只读；`05-Journal/` 只归档不删；遇人类手工删除引发断链先清引用方，**严禁复活人类已删文件**。

## 5. 平台自省与工程底座

| 用途 | 命令 / 底座入口 |
| :--- | :--- |
| 能力自省与最小填空模板 | `./gewu atlas capabilities` / `./gewu atlas --template` / [commands.yaml](dicts/commands.yaml) / [cli-manifest.json](contracts/cli-manifest.json) |
| 平台防越轨、测试与提交基线 | [rule-app-contract-rail.md](rules/rule-app-contract-rail.md) / [rule-testing-strategy.md](rules/rule-testing-strategy.md) / [execution-routing-v1.md](contracts/execution-routing-v1.md) / [commit-message-v1.md](contracts/commit-message-v1.md) |
| 域自测、台账管理、热内核与备份 | `./gewu atlas daemon start\|status\|stop`（多步任务热内核加速，零宿主 MCP 依赖，可选 `./gewu atlas mcp`） / `./gewu atlas test --scenario smoke\|security\|domain\|staging\|release` / `./gewu atlas manage ...` / `./gewu atlas backup\|boot ...` |
