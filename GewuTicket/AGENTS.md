# GewuTicket（格物工单）

由 1 个纯脚本技能包与多个无执行位数据卷装配的多 Agent 与人协同工单域，支持可扩展分类、阻塞与完工后双维审阅、7天冷归档及只读/影子读写挂载。

## 全局不变量（40%）

- 统一门面入口：工作区根执行 `./gewu ticket <cmd>`（或本域目录 `./gewu <cmd>`），快捷直通 `./gewu ticket --action ...`；底层兼容 `python3 -I -B scripts/facade/app_facade.py` 与 `python3 -I -B bootstrap.py`。
- 严禁使用原生 `cat`/`ls`/`find`/`sqlite3`/`sed` 探测或修改 `scripts/app_core/`、`.local/` 与 `.runtime/`。
- 托管短句柄（如 `rec_01`）在 Agent 侧无可读内容，直接传入 `--ref` 槽位；遇 `GUARD: HALTED` 或 `INPUT_REQUIRED` 立即停止重试并向人类汇报。
- 所有行为约束遵循 [契约防越轨规则](rules/rule-app-contract-rail.md) 与 [安全分级](contracts/app-security-profile.json)。
- **执行路由裁定**：依据 [执行路由契约](contracts/execution-routing-v1.md)，本 App 为独立格物域（零 `GewuTrack` 耦合）：日常工单与门面动作走统一网关与双态胶囊；受管源码维护走 `SYS` 票与本域 `manage` 闭环。
- **提交信息契约**：依据 [提交信息契约](contracts/commit-message-v1.md)，由 Agent 自主填写 `type/scope/subject/body` 真实内容，并由 `scripts/git/validate_commit.py` 验格与防泄漏。
- **域系统文件批处理收口提交**：管理本域 `rules/`、`skills/`、`scripts/`、`contracts/` 与 `AGENTS.md` 必须通过 `scripts/manage/app_manage.py` 执行；允许在同一任务中连续批量修改或增删多个系统文件，无需每修一个文件就提交，待本次任务批处理全部完成后统一执行一次 `python3 -I -B scripts/manage/app_manage.py commit` 完成验测与 Git 提交。
- 技能包严禁夹带本地二进制；数据卷所有静态文件严格无执行位（0644/0600），支持 ro/rw/shadow-rw 挂载
- 数据卷严格遵循二级分类（可扩展）→ 三级状态桶（active 共置 open/in_progress、paused、closed、超 7 天归档 archive）
- 人类审阅采用阻塞继续前（pre_review）与完工后非阻塞（post_review）双维正交状态机，防重放校验原票 SHA-256

## 场景路由表（60%）

| 业务场景 | 必调入口 |
| :--- | :--- |
| 主业务任务 | [ticket-orchestrator](skills/ticket-orchestrator/SKILL.md) |
| 实战经验路书（避坑与高分协同配方） | [usecase-for-agent/README.md](usecase-for-agent/README.md) / `./gewu ticket --action usecase --mode list\|match` |
| 立项建票（选择卷、分类与是否阻塞预审） | [ticket-create](skills/ticket-create/SKILL.md) |
| 只读查阅、队列检索与分类策略查看 | [ticket-triage](skills/ticket-triage/SKILL.md) |
| 认领接手、多轮回复、暂停恢复与弃权/撤回 | [ticket-flow](skills/ticket-flow/SKILL.md) |
| 人类预审/完工审阅、结单归档、7天扫盘与影子合入 | [ticket-gate](skills/ticket-gate/SKILL.md) |
| 统一网关总览与能力自省 | `./gewu ticket capabilities` / [cli-manifest.json](contracts/cli-manifest.json) |
| 目标驱动执行路由与双态胶囊契约 | [execution-routing-v1.md](contracts/execution-routing-v1.md) |
| 工单协作（SYS/DOC/MSG/TASK/SLIP 多轮与 Close）及系统两阶段提交 | [sys-change-ticket-v1.md](contracts/sys-change-ticket-v1.md) |
| Markdown 分级排版、混排与文本写作契约 | [markdown-formatting-v1.md](contracts/markdown-formatting-v1.md) |
| 撰写并校验 Git 提交信息 | `./gewu ticket validate-commit --message-file <path> --agent <ID>` / [commit-message-v1.md](contracts/commit-message-v1.md) |
| 按场景分流执行自测与告警门禁 | `./gewu ticket test --scenario smoke\|security\|staging\|release`（兼容 `scripts/checks/run_app_tests.py`） |
| 管理本域 rules/skills/scripts/AGENTS.md 并提交 | `./gewu ticket manage inventory\|upsert-object\|remove-object\|update-agents\|commit` |
| 域分发打包与独立目录结构 | `./gewu ticket dist inspect\|plan\|build\|dist\|verify` / [data-layout-profile.json](contracts/data-layout-profile.json) |
| 隔离安装全量或 Domain-only dist | `./gewu install-dist check-env\|install`（或 `scripts/distribution/install_dist.py`） |
| 运行态与数据安全备份校验 | `./gewu ticket backup backup\|list\|verify\|restore` |
| 环境初始化与运行态迁出 | `./gewu ticket boot inspect\|init-runtime\|deploy-runtime`（兼容 `bootstrap.py`） |
| 工单核心命令行与填空模板（`--action` / `--template`） | `./gewu ticket --action <...>` / `./gewu ticket --template`（底层 `scripts/ticket/ticket_cli.py`） |
| 托管短句柄与安全微交互（`facade`） | `./gewu ticket facade --action <put\|exec\|list\|status>`（兼容 `scripts/facade/app_facade.py`） |
