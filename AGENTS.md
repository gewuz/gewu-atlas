# 格物工作区入口（发行清理版 · GewuAtlas + GewuTicket）

## 1. 全局架构哲学与不变量

- **空间围墙 + 过程自治 + 结果验货**：
  - **划定空间**：所有知识加工与研究草稿均在受控数据卷（`20-AI-Zone/` 或课题 `30-Projects/prj_XX` 的 `ws-<agent>` 工作台）内进行；工作区根目录保持净空（严禁新建 `*-research/`、`reports/`、`sources/`、`99-Assets` 等业务目录或双份落盘）。
  - **过程放手**：在合法边界内，Agent 可根据现场情况自由组合算子、试错与迭代，不强制机械教条的步骤顺序。
  - **分级验货（60分算子硬底线 + 40分 Subagent/自评软量表）**：
    - **L1 简单单步任务**（记日记、单卡微调、回收站操作）：写算子内置校验，返回 `OK_*` 即合规直交（0 额外开销）。
    - **L2 局部轻任务**（多卡检索问答、单篇入库、单卡创作）：由胶囊 `[QA_GATE]` / `[DELIVERY_GATE]` 顺风车核验，输出前按通用三维量表（完整性 15 + 真实性 15 + 可读性与零路径 10）脑内自检即可交付。
    - **L3 复合重度任务**（开课题多源调研、批量入库+切片+萃取+织网）：收尾必过 `guard --mode delivery` 算子硬评估（60/60），并优先调用独立只读 **Subagent** 盲审打分（无 Subagent 能力则降级为显式自我评估），分别记录硬评与语义审阅结果；两项均通过方可交付。漏答核心诉求、关键结论无依据、拼贴检索碎片或泄露物理路径均判语义审阅失败。
- **统一网关与安全红线**：
  - 所有业务通过 `./gewu.command <domain> <cmd>`（Windows 为 `.\gewu.cmd <domain> <cmd>`，或 `./gewu <domain> <cmd>`）执行；先用 `list-domains` 与各域 `capabilities` 核对能力。
  - 禁止探测或直接修改 `app_core/`、`scripts/app_core/`、`.local/`、`.runtime/` 与 `instances/`；不在回复或命令中拼接、复述本机绝对路径；托管句柄直接传参；遇 `GUARD: INPUT_REQUIRED` 或 `GUARD: HALTED` 立即停手汇报。
- **主次分工**：本工作区以 **GewuAtlas** 为核心知识工作台，**GewuTicket** 提供高价值协同与审批支撑。

<!-- forge:gewu-atlas:entry -->
## 2. GewuAtlas 域入口（空间围墙 + 8 个意图技能 + 实战 Usecase 路书 + 胶囊验货）

入口：[GewuAtlas](GewuAtlas/AGENTS.md)；命令：`./gewu.command atlas capabilities` / `./gewu.command atlas --template`。

边界：
- **实战路书先行（`usecase-for-agent` 入口声明）**：接手复杂入库、萃取或专题研究前，**可先阅读 [GewuAtlas/usecase-for-agent/README.md](GewuAtlas/usecase-for-agent/README.md)**（或调 `./gewu.command atlas --action usecase --mode match --query "<任务词>"`）直接复用前人沉淀的「损耗→对策表」与「高分流水线」；若任务过程出现异常或人类要求记录本次心得，激活 [atlas-usecase](GewuAtlas/skills/atlas-usecase/SKILL.md) 调 `--action usecase --mode upsert` 沉淀经验。
- **查库循证（`[QA_GATE]` 胶囊锁定）**：知识问答先调 `vault` + `note-search` + `note-get --note_refs ...` 批量实读正文后写成通顺分析文章并标注 `nte_XX` 句柄；未命中时诚实声明缺口，禁凭记忆瞎编或直接拼贴检索摘要。
- **课题工作台围墙**：专向/多篇研究先调 `project-create --ref <vlt_XX> --title "<课题>" --agent <名>`（自动分配 `ws-<agent>` 工作台），外部文献经 `ingest --urls ... --auto_slice true` 归口；每次写算子出口自动顺风车检验根目录净空与双链闭合（`[DELIVERY_GATE]`，草稿区走 $O(1)$ 脏卡增量校验，跨入正式区与 `guard --mode delivery` 走全库 60/60 硬验货）。
- **热内核加速与低回合原语（免宿主 MCP 配置，开箱即用）**：L2/L3 多步任务建议开工 `./gewu.command atlas daemon start`（收工 `daemon stop`，空闲默认 10 分钟自动自毁，期间所有常规 `./gewu.command atlas --action ...` 自动复用内存热 KEK 与索引，未启动或沙箱受限自动降级直跑；支持 MCP 的宿主亦可选 `./gewu.command atlas mcp`）；局部改长文/MOC 优先用 `note-patch`（自动保护受控块），多步写优先用 `batch`（单锁合并闸门，支持 `$step_id.field` 串联），全局探查优先用 `project-brief`、`note-grep` 与 `note-get --outline true`。
- **零物理路径**：Atlas 数据只经逻辑句柄访问，禁原生扫描 `Volumes/GewuAtlas-data`；向人类汇报标题与句柄。
<!-- /forge:gewu-atlas:entry -->

<!-- forge:gewu-ticket:entry -->
## 3. GewuTicket 域入口（`SYS` 强前置 + 正式审阅派单）

入口：[GewuTicket](GewuTicket/AGENTS.md)；命令：`./gewu.command ticket capabilities` / `./gewu.command ticket --template`。

边界：
- **日常内容任务免手建（防信息爆炸）**：普通查库、入库、萃取与写作由 `GewuAtlas` 底层按需自动随附 `DOC` 票，日常简单内容任务严禁额外手动建票。
- **系统文件改造必先开 `SYS` 票（禁止无单先改）**：凡修改任何域的 `AGENTS.md`、`rules/`、`contracts/`、`skills/`、`scripts/`、`dicts/`、`templates/` 或工作区根 `AGENTS.md` / `gewu`，**必须在修改任何文件前**先确认 Git 基线并向 `./Volumes/GewuTicket-data` 开立 `SYS` 工单，取得人类 `pre` 审阅批准进入 `in_progress` 后方可施工；验收与 Finish Commit 后再 `done` 结单：
  `./gewu.command ticket --action out --volume ./Volumes/GewuTicket-data --category SYS --from <Agent> --key <task-key> --title "<系统变更摘要>" --write-scope "<作用域>"`
<!-- /forge:gewu-ticket:entry -->
