# 规则：格物域上层应用契约防越轨规范（Layer 1）

适用对象：GewuForge 自身及由 GewuForge 设计、生成、维护或验收的一切上层 App。工程约束入口：[应用工程约束基线](../contracts/app-engineering-baseline-v1.md)。该基线随独立域分发，完整设计背景另见 Forge 开发版资料。

## 不变量

1. **`AGENTS.md` 体积与配比**：App 级 `AGENTS.md` 必须控制在 `12000` 字节以内，约 `40%` 声明全局不变量，约 `60%` 维护「场景 → 必调 `SKILL.md`」路由表，严禁暴露内部 SQL 表结构、物理 `.db` 路径或密文前缀。
2. **`SKILL.md` 填空题卡片**：每个原子 `SKILL.md` 必须给出固定调用骨架与 `2`～`4` 个语义槽位、允许枚举与 `1` 行合法示范（`description` 采用中文主触发词 + 英文关键词双语对齐）；超过 `5`～`6` 个参数必须拆分为句柄串联的微 Skill；单域超过 `6`～`8` 个技能时按「原子能力 + `*-bus` 总线 + `*-style` 排版呈现 + 专用只读 `search/qa`」分层拆分，禁止让模型自拼 SQL、Shell 重定向、`jq` 过滤或宿主机绝对路径。
3. **四通道灵活传参 + 非阻塞边界**：面向 Agent 的门面脚本必须支持 `CLI Flag > --set k=v > --payload/--json-stdin > 安全默认值` 四通道兼容与 `--template` 领空表；仅在显式出现 `-`/`--json-stdin` 时读取 `stdin`（防宿主未发 `EOF` 导致无参命令悬挂死锁）；缺参返回 `E_PARAM_MISSING` + `fill_template`，未知多余字段以 `E_PARAM_UNKNOWN_FIELD` 拒绝，路径遍历与 SQL 碎片以 `E_PARAM_INVALID` 拒绝；内部执行一律 `shell=False` 并以 `python3 -I` 隔离拉起隐身核心。
4. **Handle-First 托管短句柄与零路径出口**：对外采用「逻辑句柄解耦」视角，仅返回 `rec_01`/`vlt_01` 等无特征短句柄与平实说明（「由运行时内部托管，Agent 侧无可读内容，无需也不可解析；请直接传入 `--ref` 槽位」）；出口经零路径门禁校验（`assert_zero_path_leak`），严禁输出物理存储路径、`enc:v1:` 密文前缀或「警告：禁止探查」挑逗性文本；面向人类的只读检索禁止倾倒底层脚本、JSON 原包或无依据脑补。
5. **隐身目录与反大象悖论**：隐身核心（`app_core/` 或 `scripts/app_core/`）与运行态（`.local/state/`、`.local/logs/`）必须配置 `.cursorignore`、`.claudeignore` 与宿主 `Read` `deny` / `PreToolUse` 拦截；隐身目录内**严禁放置任何禁止访问警示 `README`**。
6. **吞堆栈与双态胶囊反循环**：入口与核心最外层必须 `try...except` 吞掉一切原生 Traceback，完整堆栈只写 `.local/logs/`；正常态输出 `Triad Capsule`（`ORBIT` 必含自然退出汇报分支），异常态三分流 `GUARD: PARAM_FIX` / `GUARD: INPUT_REQUIRED` / `GUARD: HALTED`；同指纹连续失败 `2` 次或无变化只读调用连续 `3` 次即硬熔断为 `GUARD: HALTED`；禁止向 Agent 暴露关闭胶囊的开关参数。
7. **Git 边界与内容卷人机共生**：密钥、Token、`.vault_kek` 与加密数据库（`.enc.db` / `vault.enc`）严禁进入任何 Git 提交、Stash、孤儿分支或历史记录；单靠 `Read` `deny` 挡不住 `git show` / `git log -p` / `git diff`，必须同步由 `PreToolUse` 拦截针对隐藏目录的 Git 历史探查命令（或经 `deploy-runtime` 移出工作区）。涉及用户直改的内容型独立数据卷（`Volumes/<AppDirectory>-data`）若启用内容版本库，应托管于受保护 `.local/state/` 下并忽略暂存/归档/下载 `source/` 区，识别人类手工删改（只清理断链，**严禁擅自恢复人类已删除文件**）。
8. **执行路由、工单协同、排版与提交信息裁量边界**：生成 App（即一个独立的格物域）必须同时内置 [执行路由契约](../contracts/execution-routing-v1.md)、[工单协同与系统变更契约](../contracts/sys-change-ticket-v1.md)、[Markdown 分级排版契约](../contracts/markdown-formatting-v1.md) 与 [提交信息契约](../contracts/commit-message-v1.md)（配 `scripts/git/validate_commit.py` / `scripts/git/validate_message.py`），明确本域不集成 `GewuTrack`，业务直接使用门面、任务开始自动建票、`SYS`/`DOC`/`MSG` 等工单的多轮交互与 Close 签阅时机、系统变更执行「开始基线/Commit → 变更与实测 → 完成 Commit → 结单」闭环、Markdown 分档排版混排边界，以及 Git 提交中哪些字段格式被契约锁定、哪些语义内容由 Agent 自主决定填写。

## 例外与违例处理

- 若因历史遗留或跨版本兼容暂时无法满足某条，必须在验收报告与 `contracts/app-security-profile.json` 中显式标注为未决缺口，不得隐瞒或伪称合规。
- 违反上述任一不变量时，`check_contract_rail.py` 与 `blueprint.py` 必须拒绝通过。
