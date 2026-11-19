# 契约：Git 提交信息格式与 Agent 自主填写边界（`gewu.commit-message/1`）

本契约定义 Git 提交信息中**哪些部分由契约刚性锁定（不可违背或伪造）**、**哪些内容由 Agent 根据实际改动自主决策填写**，随每一份由 GewuForge 生成的 App 一同注入，并由配套校验器在提交前执行检查。

## 1. 契约锁定与禁止项（Contract-Fixed & Forbidden）

1. **首行格式锁定**：`^([a-z][a-z0-9-]*)\(([a-z][a-z0-9-]*)\): \[([^\]\r\n]+)\] (\S.*)$`。
   - `[Agent]` 身份框内必须严格等于调用校验器时 `--agent` 传入的当前运行时自报身份，不得伪造或冒充他人/人类签批。
2. **正文分隔与编码锁定**：
   - 若包含多行正文，**第 2 行必须为空行**；
   - 必须通过 UTF-8 文件传入校验器（`--message-file <utf8_abs_path> --agent <Agent身份>`），保留原始换行，严禁将多行内容直接拼入 Shell 字符串；
   - 文件大小 ≤ `65536` 字节，禁止包含 `\x00`。
3. **内容红线（严禁泄漏与伪造）**：
   - **零凭据与零内部泄漏**：严禁写入密钥、Token、`.vault_kek`、`.enc.db` 原文、`SQLite format 3`、`enc:v1:`、`PRAGMA key`、`-----BEGIN ` 私钥头或隐身 `.local/` 内部敏感数据；
   - **事实保守红线**：未运行的测试不得写为通过；开发候选或局部接线不得宣称为全量正式发布；提交正文与 Git 作者均不构成人类授权。
4. **验证与执行解耦**：校验器只验格式、自报身份一致性与泄漏红线（输出 `commit_executed: false`），**绝不替人类执行 `git add` 或 `git commit`**；不存在 Git 仓库时只报告事实，不自动 `git init`。

## 2. Agent 自主决定填写的内容（Agent-Discretionary Fields）

在不触犯第 1 节硬边界的前提下，以下字段与语义内容**不由脚本写死模板，完全由 Agent 核对暂存区真实 `git diff` 后自主决定并撰写**：

| 自主决策域 | 格式要求 | Agent 填写裁量权与事实依据 |
| :--- | :--- | :--- |
| **`type`（变更类型）** | `[a-z][a-z0-9-]*`（推荐：`feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `security`） | 由 Agent 根据本次**主要改动意图**自行定性（例如新增功能选 `feat`，修复缺陷选 `fix`，规则/契约完善选 `docs` 或 `feat`，安全加固选 `security`）。 |
| **`scope`（影响域）** | `[a-z][a-z0-9-]*`（推荐：`core`, `skill`, `rule`, `contract`, `facade`, `runtime`, `test`, `entry`） | 由 Agent 根据实际暂存文件路径与模块归属自行命名；仅覆盖**本次真实包含的改动范围**，不夸大域范围。 |
| **`subject`（首行 `<动宾说明>`）** | 非空单行（推荐 ≤ 72 字符，中文/英文均可） | 由 Agent 用动宾短语真实提炼本次核心成果（禁止空泛写 `update files` 或 `fix bug`）。 |
| **`body`（多行正文详情）** | 第 2 行为空行，第 3 行起自由组织（推荐中文分节） | Agent **自主决定是否写正文及写哪些小节**，建议包含：<br>- **变更依据（Why）**：说明解决的业务问题或边界缺口；<br>- **路由选择（Routing）**：如实说明为何走 `GewuTrack` 轨迹长任务或仅走门面/局部调整；<br>- **核验与范围（Verification）**：列明实际执行的测试/体检命令与精确结果；<br>- **未决事项（Pending）**：若有未验边界或遗留限制，如实披露。 |

### 2.1 提交时机、分轨作用域与历史保护红线（迁移自 116）

1. **改前感知与显式暂存**：
   - 提交前先核对 `git status --short`；发现非本主体产生的未完工草稿或外部修改，**立即停手请示**，严禁覆盖或裹挟。
   - 只按明确相关路径显式 `git add <path...>`；禁止盲目 `git add -A` / `git add .` 混轨或吞入 `.gitignore` 外误建的临时草稿与密钥资产。
2. **双轨与作用域对照**：
   - **工作区系统/工程轨**：按 [sys-change-ticket-v1.md](sys-change-ticket-v1.md) 执行任务开始自动建 `SYS` 票、锁定/执行开始 Commit（`start_commit`）→ 变更与测试验收 → 完成 Commit（`finish_commit`，或拆分为系统/测试提交）→ `done` 结单；常用 `<type>(<scope>)`：`feat`/`fix`/`refactor`/`docs`/`test`/`chore`/`security` 搭配 `sys`/`core`/`skill`/`rule`/`contract`/`facade`/`runtime`/`test`/`entry`/`workspace`。
   - **受版控知识内容轨**（仅当某知识 Vault 启用 Git 版本追踪时）：遵循四态提交纪律（人类指令 / 体检修复获批后 / `> 20` 篇大批量自动快照 / 人类手工无损修剪收口），日常 `<= 10` 篇修改保留工作区不自主提交；推荐前缀 `feat(lib)`（文献）、`feat(atom)`（原子卡）、`feat(moc)`（织网）、`feat(artifact)`（成果）、`refactor(content)`（升阶/改名/清理修剪）。
3. **署名与历史不可重写红线**：
   - 执行 `git commit` 时须使 Author/Committer 与提交首行 `[<Agent>]` 保持同一真实身份（推荐配合 `git -c user.name=<Agent> -c user.email=<agent>@gewu.local` 或等价环境变量，不得冒名或署「系统」）；
   - 严禁 `git commit --no-verify` 绕过门禁、严禁 `git push --force`、严禁 `rebase`/`amend` 改写他人或已共享历史；撤销已交付提交一律新增反向提交。

## 3. 校验调用方式

```bash
python3 -I -B scripts/git/validate_commit.py --message-file <utf8_abs_path> --agent <Agent身份>
```
（在 `GewuForge` 本身工作区对应 `GewuForge/scripts/git/validate_message.py`。）
