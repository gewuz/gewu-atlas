# 规则：按场景分级的测试与告警门禁（`rule-testing-strategy/1`）

本规则适用于 `GewuForge` 自身及所有由 `blueprint.py` 产出的 App 格物域，明确何时运行最小必要测试切片、错误与告警的三态裁定边界，以及如何结合 App 实际能力裁剪 `GewuTrack` 校验范围。

## 1. 五档测试场景（避免每次机械全跑）

| 场景档位 | 适用时机 | 必跑 App / Forge 切片 | 必跑 `GewuTrack/` 切片 |
| :--- | :--- | :--- | :--- |
| **`S1: lint-rail`** | 修改 `AGENTS.md`、Rule、Contract、`SKILL.md`、Commit 规则 | 清单/索引检查 + `check_contract_rail.py --probe-facade` + 受影响 `SKILL.md` 结构检 + `test_structure_and_rail` | **不跑**（仅校验 `MANIFEST.json` 摘要无漂移） |
| **`S2: unit-operator`** | 修改单个脚本、门面、断路器、校验器或单算子模块 | 仅映射受影响模块的对应测试切片（及关联安全反例） | **按所改算子映射单个/成对切片**（如仅改 `object_flow` 只跑 `test_object_mapping` + `test_object_integration`） |
| **`S3: app-staging`** | 新建或改建 App 的分步搭建、间断规划与功能追加中 | `check_contract_rail.py --stage drafting`（或 App `run_app_tests.py --scenario staging`）+ 目标分级冒烟 | 仅跑 `MANIFEST.json` 摘要核对（不构成签名验证） + App **实际声明使用的能力**冒烟 |
| **`S4: system-upgrade`** | 跨模块重构、蓝图注入调整、`app_core` 安全分级改动、发布前终验 | 全量单元测试套 + `package.py inventory` + `check_contract_rail.py --stage release --probe-facade` | 跑协议与架构核心切片（`test_runtime_architecture`、`test_round2_acceptance`、`test_protocol_and_forge`） |
| **`S5: track-upgrade`** | 升级或替换根目录平铺的 `GewuTrack/` 内核文件 | `S4` 全量终验 + 全部 `*-track-profile.json` / `bootstrap-profile.json` 摘要核对 | **`GewuTrack/tests/run_validation.py` 全量套件** |
| **`track-validation`** | Forge 验收明确延期，仅需先收口 Track | 不跑 Forge 单测；仍执行 Contract Rail，未完成的 Forge 验收必须披露 | **`GewuTrack/tests/run_validation.py` 全量套件** |

### 1.1 `SYS` 变更收口的分级测试与全量升格门禁（对齐 `sys-change-ticket-v1` §4）

- **默认相关单测（`< 3` 交叉且首跑通过）**：`SYS` 系统改动涉及 `< 3` 个独立模块/子域/契约接口交叉，且不触及 `app_core` 安全加密核、全局蓝图、`GewuTrack/` 内核升级或正式发布终验时，收口仅需跑通**受影响模块相关单元测试 + `check-rail`/`lint-rail`**，不必每次全量回归。
- **升格全量条件（命中任一即必跑 `S4`/`S5` 或 `--scenario release`）**：
  1. **交叉影响 `>= 3` 个**模块/子域/契约接口；
  2. **第一次相关单元测试未通过**（修复问题后必须跑一次全量回归收口）；
  3. 触及安全核心、全局模板、`GewuTrack/` 内核升级或发布终验。
- **范围透明**：局部相关单测收口时，Commit 正文与工单 `§6 完成反馈` 须明确写明实际运行的单测切片名称与结果。

## 2. 错误与告警三态裁定（Error vs Warning Gate）

1. **`ERROR`（硬阻断，必须立即修复，任何阶段均不可绕过）**：
   - 安全与隐身核心破损：`app_core/` 或 `.local/` 存在警示性 `README`（大象悖论）；`SKILL.md` / `AGENTS.md` / 门面 / Commit 出现密态前缀、内部路径、数据库魔数或私钥头泄漏；`PreToolUse` 钩子漏拦探测或 `git show/log -p` 命令；Git 历史或 Stash 存在密钥/`.enc.db` 轨迹；AAD 密文防移植失效；门面泄漏原生 `Traceback`。
   - 契约与结构破损：`AGENTS.md` 超过 `12000` 字节；门面未拒收未知字段；指纹断路器未在阈值内熔断；`GewuTrack` `unknown`/版本冲突被转译成 `PARAM_FIX` 诱导重试而非 `GUARD: HALTED`；受管清单/摘要失配或越界。
2. **`WARN_DISCLOSE`（不阻断单步执行，但必须如实告知人类）**：
   - 环境或宿主前提尚不完整导致未完全验证的边界：如当前目录尚未 `git init`（`git_history_zero_trace: "not_a_git_repo"`）、Layer-2 跨 UID / 生产宿主装载尚未在真实跨账号环境验证、平台特定跳过用例（须精确披露总数与跳过原因）。
   - **禁止静默忽略**：必须原样写入报告的 `warnings_disclose` 与汇报正文，严禁以「全部通过」掩盖未验边界。
3. **`WARN_STAGED_IGNORE`（仅限 `--stage drafting` 建设期可忽略）**：
   - 仅当明确处于**新 App 搭建或功能分步补充过程中**时，允许将预期未就绪项降级为建设期暂缺（输出于 `ignored_staged_warnings`）：
     - 尚未建齐全部规划中的 `SKILL.md`（`no_skills_yet`）；
     - 选定 `binary-hybrid`/`binary-direct` 时尚未执行 `init-runtime` 编译 `.local/bin/gewu_vault_exec`（`layer2_binary_not_compiled_yet`，但源码存在时 `whitebox_sha256` 漂移仍属 `ERROR`）；
     - 尚未初始化/迁出运行态（`runtime_not_initialized`）。
   - **终验自动收口**：进入 `--stage release`（或 `release_flow` / `forge-evaluate` 终验）后，所有建设期忽略项自动取消豁免，转为 `ERROR` 或 `WARN_DISCLOSE`。

## 3. 脚本与测试的四通道上下文防越界矩阵（Display vs Disk vs stdin vs `:memory:`）

| 传输/存储通道 | 允许内容 | 严禁内容（防 Agent 越界） |
| :--- | :--- | :--- |
| **`stdout`（展示给 Agent）** | 状态码（`pass/fail`、`OK_*`、`E_*`）、托管句柄（`rec_01`）、测例统计数、`warnings_disclose` 标签、脱敏胶囊回执 | 原始 Python `Traceback`、`File "..."` 源码行号、`app_core` 内部模块/类名/表结构、KEK/密文特征、真实业务秘密明文 |
| **磁盘落盘（分区隔离）** | 1. 明面契约与门面脚本；<br>2. `.local/state/` `0600` 双重加密容器；<br>3. `.local/logs/` `0600` 排错堆栈与 `human-only` 审计（专供人类）；<br>4. 隔离测试临时目录 `TemporaryDirectory`（退出即删） | 明文 SQLite（`.db` / `-wal` / `-shm`）、`/tmp` 明文业务载荷、`app_core`/`.local` 警示性 `README`、密态文件入 Git 历史 |
| **`stdin` / 匿名管道 FD** | 一切测试或业务高敏载荷（如 `--content -`、`--payload -`）、Layer-2 `GEWU_PAYLOAD_FD` 白盒参数（以及 Git 提交多行正文 UTF-8 受控文件传入） | 把秘密/正文拼入 CLI `argv`（会暴露在 `ps` 进程表、Shell 历史和命令回显中） |
| **纯 `:memory:` 内存交换** | `sqlite3.connect(":memory:")` + `PRAGMA temp_store=MEMORY`；`HKDF` 派生的 `db_key`/`field_dek`；`execute_on_handle` 与 `--self-test-security` 单条解密与 AAD 反例消费；C11 `mlock`+零化缓冲区 | 把解密后载荷或密钥留存到盘面或打印回 `stdout` |

## 4. 新建 App 格物域的 `GewuTrack` 裁剪测试原则

1. **普通薄门面 / 单仓微交互 App**（无对象仓库、无多环境分发、无多 Skill 注册子系统）：日常仅验 `MANIFEST.json` 摘要 + App 自身 `smoke / security` 场景，**不挂接** `forge_object` / `forge_build` / `forge_release` 重型适配器套件。
2. **按能力清单逐项挂接**：仅在 App `contracts/testing-profile.json` 显式声明了 `track_capabilities`（如 `object` / `build` / `entry` / `release`）时，才映射对应的 `GewuTrack` 切片并在 `release` 阶段触发校验。
