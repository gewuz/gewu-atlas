# 契约：格物工单自动开立、多轮结单与 SYS 开始/完成提交规范（`gewu.sys-change-ticket/1`）

本契约定义 `GewuForge` 平台及各格物域（Domain）统一使用 `GewuTicket` 在**每次任务开始时自动建票/复用工单**、开展多轮交互与结单签阅（Close），以及 `SYS` 系统文件变更**开始 Commit → 变更与实测 → 完成 Commit → 结单**的刚性行为规范。

---

## 1. 基础工单分类定位与执行边界（SYS / DOC / MSG / TASK / SLIP）

工单主分类统一围绕 `SYS`、`DOC`、`MSG`、`TASK`、`SLIP` 五类基础角色运作（若业务域动态扩展了自定义分类，须在卷 `_meta/categories.json` 显式声明其模板、门禁与归档策略）：

| 分类 | 核心用途与适用场景 | 权限与执行边界 |
| :--- | :--- | :--- |
| **`SYS`** | 平台与任意格物域**系统文件**（`AGENTS.md`、`rules/`、`contracts/`、`skills/`、`scripts/`、`templates/`、`app_core/`、`dicts/`、`bootstrap.py`、`gewu`、`tests/`）的设计、开发、重构、测试与缺陷修复 | 遵循 `structured_5sec` 模板、前置预审门禁与「开始 Commit → 变更与实测 → 完成 Commit → 结单」闭环；结单前须清零未授权任务项（支持 `complete_l1=true` 核销 `[L1]`）并在 `§6` 写明含双 Commit 哈希的 `完成反馈` |
| **`DOC`** | 文档、知识内容、静态数据卷或业务长流程（多阶段验收、批量材料逐项跟踪、中断恢复与跨 Agent 接力） | 可关联目标域或课题 `project_id`；仅授予授权业务内容范围，**严禁借 `DOC` 票直接修改系统文件**（发现需改系统时须另建 `SYS` 票）；日常单次只读问答不强制建票 |
| **`MSG`** | 通知、方案讨论、评议、审阅告知与跨主体交接 | 采用轻模板；**严禁通过 `MSG` 授予系统开发权限、写范围或补丁导出**；`MSG` 结单只表示消息传达或本轮讨论已收敛，不代表关联 `SYS` / `DOC` 执行任务已获批或完工 |
| **`TASK`** | 通用任务分配、协作追踪与任务进度记录（流程与 `MSG` 相同：无强制前后审阅门禁、7 天归档，显式标为 `TASK`） | 采用 `freeform` 任务清单模板；支持 `--to-agent` 定向派单、`reply` 进度打卡与创建者/委派方签阅收口；**严禁通过 `TASK` 授予系统开发权限或替代 `SYS` 票改动系统源码** |
| **`SLIP`** | 书签批注（`@@...@@`）扫描、批量任务执行、增删改查审计与批处理记录（流程与 `TASK`/`MSG` 同级轻量） | 采用 `freeform` 模板；一次批量 `slip` 扫描/执行默认开或复用 1 张 `SLIP` 票，汇总各 `slip_ref` 与操作回执；**严禁借 `SLIP` 票修改系统源码** |

### 1.1 任务开始自动建票与复用边界（默认开工建票）

**总则**：除单次纯只读问答/零改动查询外，**每次接到明确新任务并开始执行前，Agent 必须先查 `./Volumes/GewuTicket-data` 在途工单（未初始化时先 `init-volume`），按稳定 `--key` 自动建立新工单或复用同一在途原票**，禁止无票空跑任务：

| 任务特征 / 触发场景 | 自动建票策略 | 开工与审批约定 |
| :--- | :--- | :--- |
| **系统文件设计/开发/重构/缺陷修复/测试更新**（触及 `AGENTS.md`、`rules/`、`contracts/`、`skills/`、`scripts/`、`templates/`、`app_core/`、`dicts/`、`bootstrap.py`、`gewu`、`tests/`） | **开始时自动建/复用 `SYS` 票**（写明 `start_commit` 与 `write_scope`） | 启动即建票；若人类尚未在对话中明示批准本次方案与开工指令，停在 `open` 待人类签批 `pre` 审阅后再 `pickup`；若人类已于当前对话明示批复方案与开工，可据该真实指令转录 `review --stage pre --outcome approve --by User`（附 `--expected-sha256` 与原指令依据）→ `develop` → `pickup`（**无人类真实授权严禁私自代签**） |
| **已授权内容长流程 / 批量核验任务**（跨多独立业务阶段且需阶段验收如 `route → fmt → slice → extract → weave`；批量材料需逐件追踪如批量 `note-route >= 2` / `fmt` / `url-ingest` / `project-merge\|split\|undo`；或需中断恢复/接力） | **自动建并接手 `DOC` 票（免二次建票审批）** | 按稳定 `--key` 复用或新建 `DOC` 票并立即 `pickup` 进入 `in_progress`；发现需改系统时另起 `SYS` 票 |
| **`slip` 书签批注批处理**（扫描文档/目录内 `@@...@@` 并批量生成/更新/删除 `25-Slip` 卡片与回链） | **每批自动开 1 张 `SLIP` 票** | 按批建 1 张或复用同批 `SLIP` 票汇总记录 `slip_ref` 与结果（内部调 `url-ingest` 不额外嵌套 `DOC` 票） |
| **常规执行/拆解/跨主体派工任务**（不触及系统源码且无需 `DOC` 完工后审阅留档） | **自动建/复用 `TASK` 票** | 启动任务即建/复用 `TASK` 票并 `pickup` 跟踪进度，完成后写回执 `done` 结单 |
| **纯只读单次问答 / 零写盘检索**（不修改系统也不落库内容，且未传入 `--ticket_id`） | **默认免建票** | 仅只读查询不机械建票；用户显式要求建票留痕或传 `--ticket_id` 时照常建/记票 |
| **跨主体通知、方案讨论、审阅告知、交接沟通** | **按需建 `MSG` 票** | 仅记沟通与签阅结论；涉及真实执行时须关联对应 `SYS` / `DOC` / `TASK` 原票 |

---

## 2. 多轮交互与状态维持规则

1. **一票贯穿全周期，允许多轮往返**：
   - 一张工单承载完整任务或单一讨论主题，允许多轮 `--action reply` 追加真实进展、验收证据、评审反馈与返修要求；
   - 不得因进入测试、评审或返修阶段而另起平行新票或自造状态文件。
2. **等待响应 / 核验 / 返修时保持 `in_progress`**：
   - 只要仍存在未决问题、待补充实测证据、待返修事项或待创建者签阅，工单必须留在 `active/` 并维持 `status: in_progress`，同时在 `§1 断点` 或 `§4 接手任务` 写明未决项与下一接续人；
   - **`reply` 只追加事实记录，不改变工单状态、接手人或自动结单**；禁止因为接收方回复了“已阅”或单轮进展即直接 `done`。
3. **显式暂缓与条件恢复（`pause` / `resume`）**：
   - 日常短暂等待回复或单轮核验保持 `in_progress`；仅在明确暂不推进时，由当前接手者（或 `open` 票创建者/指定接收者）执行 `--action pause --reason ... --resume-when ...` 迁入 `paused/`；
   - 恢复条件实际落实后，再执行 `--action resume --note ...` 退回 `open`；`SYS` 工单恢复后须重新核验人类前置放行再 `pickup`。
4. **跨主体接力（`abort` → `pickup`）**：
   - 原接手者先把已完成工作、产出位置与下一步断点写入工单，再执行 `--action abort` 退回 `open`，由新接手者核对后 `--action pickup`；严禁静默改写 `picked_by`。

---

## 3. 何时 Close（签阅收口、`done`/`cancelled` 判定与冷归档）

关闭工单（移入 `<CAT>/closed/`）分为**正常完成（`done`）**与**未完成终止（`cancelled`）**两类，严禁把未完成或中止任务标为 `done`。

### 3.1 按协作场景判定 Close 时机

| 协作场景 | 交互与核验动作 | 合法 Close 时机与执行方式 |
| :--- | :--- | :--- |
| **自建自接**（`created_by == picked_by`） | 按约定完成产物修改与实测验证，清零 `§1` 任务项 | 写入含 `完成反馈` 的 `§6` 记录并核对无误后，直接 `--action done` 结单，不额外空转他人签阅 |
| **派发他人**（`created_by != picked_by`） | 接手者通过 `--action reply` 提交产物清单、实测结果与未决项，保留 `in_progress` 并交创建者核对 | **等待创建者核对后方可 Close**：创建者核验产物后回复明确“签阅通过，可结单（Close）”或“退回继续处理”。因门禁要求 `done` 的 `--by` 匹配当前 `picked_by`，创建者未接手时可在签阅回复中明确委托当前接手者代办 `--action done`，或先 `abort` → `pickup` 交还创建者再执行 `done`；**接收者不得未经创建者签阅擅自 `done`** |
| **`MSG` / `TASK` / `SLIP` 消息与轻量任务** | 接收者明确回复“已阅/已执行及必要意见”或双方经多轮 `reply` 收敛任务/批处理结论 | 创建者确认消息、任务或本批 `slip` 处理达成，并在回复中写明是否已有下游 `SYS` / `DOC` 工单承接（若有）后，按委托或接手主体执行 `--action done` |
| **持续返修后决定放弃 / 中止** | 多轮讨论或返修确认当前方案不再推进 | 写明已完成部分、未完成范围与终止原因，执行 `--action cancel` 以 `status: cancelled` 迁入 `closed/` |

### 3.2 结单门禁、双维审阅与 7 天归档

- **结单前阻塞门禁（Fail-Closed）**：若分类策略启用 `before_done` / `both` 预审门禁或存在 `need_human: true`，必须持有有效 `pre_review_status: approved` 方可 `done`；`SYS` 结构化模板还必须满足唯一 `## 1 断点`、未授权 `L0/L1/L2=0` 与 `§6` 非空 `完成反馈`。
- **完工后非阻塞审阅（`post_review`）**：`post_review_policy: required` 的分类在 `done` 后即可进入 `closed/` 且标为 `post_review_status: pending_review`，不阻塞主任务结论交付；在人类补签 `post` 审阅前，7 天 `archive-sweep` 将把未复审单据保留在 `closed/`（`held_unreviewed`）防漏。
- **7 天冷归档、30 天过期删除与重开（`archive-sweep` / `reopen`）**：
  - `closed/` 内单据在结单满 `archive_after_days`（默认 `7` 天）且无未完结后审留存阻断时，于后续 `done`、`cancel` 或 `archive-sweep` 时自动迁入 `<CAT>/archive/`。已归档单据只读不可改；若需基于原票复工，必须执行 `--action reopen` 拉回 `active/open`。
  - **`archive/` 内结单时间（`closed_at`）超过 `purge_archive_after_days`（默认 `30` 天 / 1 个月）的历史归档单据将在 `archive-sweep`、`done`、`cancel` 时自动删除**。

---

## 4. `SYS` 系统文件变更强制四步闭环（开始 Commit → 变更与实测 → 完成 Commit → 结单）

对 `GewuForge/` 或任意域的系统文件执行任何修改时，必须在上述流转框架内严格遵守以下四步（**方案 A：两端 Commit 闭环**）：

1. **开始 Commit + 自动建/接 `SYS` 票（Start / Baseline Commit）**：
   - 动手修改任何系统文件前，先核对 `git status --short`：
     - 若工作区已干净，记录当前 `HEAD` 哈希为本次任务起点 `start_commit=<sha1>`；
     - 若工作区存在先前遗留或未归档的既定改动（先确认非外部冲突草稿），**先执行一次开工基线提交（Start Commit）**或显式清理隔离，锁定干净 `start_commit=<sha1>`，严禁将历史遗留变更混入本次 `SYS` 任务提交。
   - 随即将任务自动立项或按 `--key` 复用到共享工单数据卷（默认 `./Volumes/GewuTicket-data`，未初始化先 `init-volume`）：
     ```bash
     python3 -I -B GewuTicket/scripts/ticket/ticket_cli.py --action out --volume ./Volumes/GewuTicket-data --category SYS --from <Agent> --key <task-key> --title "<系统变更摘要>" --write-scope "<作用域>" --content "start_commit=<sha1>; <改动范围与验收口径>"
     ```
   - 启用 `before_pickup` 预审门禁时：若人类尚未批准，则停在 `open` 等候签批；若已获得人类明示授权，执行 `--action review --stage pre --outcome approve --by User --expected-sha256 <哈希> --note "<真实授权依据>"`，并完成 `--action develop` 绑定作用域与 `--action pickup --by <Agent>` 进入 `in_progress`。
2. **更新系统文件、补充单测与分级验测（Implementation & Tiered Verification）**：
   - 完成规则、契约、技能、脚本、字典或模板修改（涉 `GewuForge/` 同步跑 `evolution_log.py record-forge` 记账；修改可复用脚手架时经 `upgrade-scaffolding` 同步各受管域），并同步增改单元测试。
   - **相关单测优先，按交叉规模与首测结果决定是否升格全量**：
     - **日常局部改动（`< 3` 个交叉点且首跑全绿）**：无需每次机械跑耗时全量套件；运行**相关单元测试（受影响模块单测及关联安全反例） + 轻量级 Rail 巡检（`check-rail` / `lint-rail`）**全部通过后，即可收口提交；
     - **触发全量测试条件（满足任一即升格跑 `test --scenario release` / `S4` / `S5` 全量套件）**：
       1. **交叉 `>= 3`**：本次变更涉及 **`>= 3`** 个独立模块、格物域或契约节点交叉联动；
       2. **首测未通过**：第一轮相关单元测试出现失败（修复后必须升格运行全量回归，防连带破损）；
       3. **核心安全/基线升级兜底**：触及 `app_core` 加密分级、全局蓝图脚手架、`GewuTrack/` 内核升级或显式执行发布前终验（详见 [rule-testing-strategy.md](../rules/rule-testing-strategy.md)）。
   - **实测范围诚实披露**：无论执行相关单测还是全量套件，提交正文与 `§6 完成反馈` 都必须精确写明实跑测试命令与覆盖范围，禁止将相关单测误报为全量通过。
3. **完成 Commit（Finish Commit）**：
   - 核对真实 `git status --short` 与 `git diff`，依据 [commit-message-v1.md](commit-message-v1.md) 撰写 UTF-8 提交说明（正文必须标注关联 `SYS` 单号、`start_commit` 及实跑验证命令与精确结果），经 `validate-commit` 校验通过后按显式路径 `git add -- <path...>` 执行**完成 Commit**，取得 `finish_commit=<sha2>`。（若单次任务明确要求拆分为系统提交 + 测试提交，亦可分两次收口提交并在结单记录中一并写明。）
4. **核销任务项并结单（Close SYS Ticket）**：
   - 满足 §3 Close 条件（含 `§1` 任务勾选、`§6` 非空 `完成反馈` 及派发场景创建者签阅）后，核销已完成 `[L1]` 断点（可传 `--set complete_l1=true`）并写入起点/终点 Commit 哈希与实测摘要结单（`done`）：
     ```bash
     python3 -I -B GewuTicket/scripts/ticket/ticket_cli.py --action done --volume ./Volumes/GewuTicket-data --ticket-id <TICKET-...> --by <Agent> --set complete_l1=true --note "完成反馈：start_commit=<sha1>; finish_commit=<sha2>; tests=<实测摘要>"
     ```

### 4.1 双轨隔离、独立内容仓 `DOC` 提交与 30 天保留契约

- **双轨物理隔离与独立 Vault Git**：
  - **系统/工程轨（`SYS`）**：由工作区主 `.git` 仓管理源码、`rules/`、`contracts/`、`skills/`、`scripts/`、`tests/`、`AGENTS.md` 与根网关；主仓通过 `.gitignore` 严格隔离 `Volumes/`、`.local/` 与 `.runtime/`。
  - **知识内容轨（`DOC`）**：每个 Vault 在 `vault-create` 时自动初始化**隔离在运行态 `state_dir/atlas-git/<vault_ref>.git`（`0700`）中的独立 DOC Git 仓**（通过 `--git-dir`/`--work-tree` 映射 Vault 工作树，不在 CLI 返回物理路径；若人类已在 Vault 内手动建有 `.git` 则自动复用）。默认通过 `info/exclude` 忽略 `00-DropZone/`、`99-Assets/98-Archives/`、`source/`（含 `30-Workbench/source/` 与各 Agent 个人空间 `ws-*/source/`）、`.trash/`、`__pycache__/` 与 `.DS_Store`。
  - 严禁在同一提交内混入系统轨与内容轨改动；提交前须区分轨道并仅在各自独立仓内完成提交。
- **`DOC` 轨强制建票与超 20 文件前后双提交**：
  - Vault 内一切**删除、写入、覆盖与搬迁**动作**必须建票**（绑定调用方 `DOC`/`SLIP` 票或由运行时自动立项/复用 `DOC` 票），同时记录前置 KEK 密文快照（`chg_XX`）、`90-System/92-Logs/Changes/` 行级 Unified Diff 明细与一键后悔药（`--action undo --change_id chg_XX`）；软删除统一进入 `.trash/`（`trs_XX`）。
  - 单次操作预计修改或同一 `DOC` 工单累计改写 **超过 `20` 个文件** 时：**在执行写入之前先执行一次 `DOC` 基线提交（`pre` commit）保护存量人类笔记，写入完成后再执行一次 `DOC` 成果提交（`post` commit）**。
- **1 个月（30 天）定期清理与日记零删除红线**：
  - `.trash/` 回收站条目（已排除误入的 `journal` 日记）、KEK 后悔药快照与 `90-System/92-Logs/Changes/` 变更明细，以及卷内/主工单卷 `archive/` 中结单超 30 天的历史工单，默认保留 **30 天**（到期由 `trash-purge` / `retention-sweep` / `archive-sweep` 清理）。
  - **日记（`05-Journal/`）遵循「只归档不删除」**：超出保留期仅通过 `journal-archive` 按 `YYYY/MM/` 冷归档，永久保留，严禁删除、软删入废纸篓、废弃或纳入 30 天清理（DOC 轨 Git 同步持续追踪 `05-Journal/`）。
- **内容轨四态提交时机与十文件阈值**：
  1. **态 1（人类指令触发）**：人类明确发出「提交 / 保存 / commit」等指令并经 `health-check` / `vfs-links` 校验无断链后，统一聚合提交。
  2. **态 2（体检修复触发）**：巡检发现断链、死链或孤岛附件并经人类批准修复后，立即单独触发修复提交结案（如 `refactor(content)`）。
  3. **态 3（大规模变动自动快照）**：单次内容批处理受影响文件 `> 20` 篇时（或大迁移前置快照），检查通过后主动提交快照护盘；日常 `<= 10` 篇轻量修改一律默认保留工作区，不主动擅自提交。
  4. **态 4（人类手工修剪收口）**：感知人类手工增删修剪知识卡且体检返回零断链零破损时，主动固化收口；发现断链则转态 2 呈报。
- **无 Git 环境自适应降级**：宿主未安装 Git 或工作区未初始化 `.git` 时，严禁擅自 `git init` 或循环调用失败 `git` 命令；如实汇报 `not_a_git_repo`，工单流转、VFS 事务与知识代谢流水线按纯文本契约照常运行。

---

## 5. 刚性禁止项（Fail-Closed）

- **禁止无单先改**：不得在未建/复用对应工单（`SYS` 且未获前置放行）的情况下先修改系统文件。
- **禁止跳过开始基线或完成后 Commit**：`SYS` 任务必须锁定干净基线（有遗留改动先做 Start Commit）并在完成全部变更与测试验收后执行 Finish Commit 后方可 `done` 结单。
- **禁止静默跨过签阅或假报 `done`**：派发他人的工单未获创建者签阅不得由接收方私自 `done`；未完成终止不得标为 `done`。
- **禁止伪造人类审阅或越权建仓**：无真实人类授权严禁代签 `pre_review`；无 `.git` 仓库时严禁未经用户明确授权擅自 `git init`，须如实汇报阻塞。
