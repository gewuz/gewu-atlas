# 规则：格物工单数据卷拓扑、双维审阅与协同不变量

适用对象：`GewuTicket`（格物工单）技能包及其挂载的一切工单数据卷（Volume）。

## 1. 技能包与数据卷物理边界

1. **技能包零二进制**：`GewuTicket` 技能包仅包含声明式规则/契约、`SKILL.md` 编排卡片与纯 Python 3.9+ 标准库可信算子；严禁夹带本地编译二进制。
2. **数据卷严格无执行位**：挂载卷内所有静态资产（Markdown 工单、`INDEX.md`、`_meta/*.json`、补丁与冲突证据及可选视图文件）禁止携带任何可执行权限位（`mode & 0o111 == 0`），发现可执行文件立即拒绝挂载（`E_VOLUME_EXEC_BIT_FORBIDDEN`）。
3. **三种挂载模式**：
   - `rw`（正常读写）：按内核锁与版本校验读写卷内工单；
   - `ro`（只读挂载）：仅允许 `in`/`list`/`find`/`categories` 等只读操作，任何写动作返回 `E_VOLUME_READONLY`；
   - `shadow-rw`（增量影子读写）：底卷严格只读不动，新增与修改落入独立隔离影子目录，删除/迁出记录于 `_shadow_meta.json` 删除白标（`tombstone`），支持 `shadow-diff` 审计、`shadow-apply` 基线防漂移合入与 `shadow-drop` 丢弃。

## 2. 二级分类与三级状态目录拓扑

1. **分类可扩展（去硬编码）**：工单分类由卷内 `_meta/categories.json` 声明注册（默认预置 `SYS`/`DOC`/`MSG`/`TASK`/`SLIP`），新增分类仅登记策略配置，不修改状态机源码。
2. **三级目录按状态分桶**：每个分类目录 `<CATEGORY>/` 下固定且仅设四个状态桶：
   - `active/`：`open`（待接手）与 `in_progress`（进行中）共用此目录，认领与弃权不搬移文件；
   - `paused/`：`paused`（明确暂缓搁置）工单；
   - `closed/`：刚结单的 `done` 与 `cancelled` 工单（7 天缓冲期）；
   - `archive/`：`closed/` 内停留超过 `archive_after_days`（默认 7 天）的已结单冷归档；`archive/` 内**结单时间（`closed_at`）超过 30 天（1 个月）的历史归档工单将在 `archive-sweep` / `done` / `cancel` 时自动删除**。

## 3. 双维正交审阅与收口不变量

1. **阻塞继续前审阅（`pre_review_status`）**：当分类配置 `pre_review_gate`（`before_pickup`/`before_done`/`both`）或建单置位 `--need-human` 时，未获人类 `approve` 前硬阻断对应动作，返回 `GUARD: INPUT_REQUIRED`。
2. **完工后非阻塞审阅（`post_review_status`）**：当分类配置 `post_review_policy = "required"` 时，`done` 立即结单并迁入 `closed/`，**不阻塞任务流转**，同时置位 `pending_review`。若 `hold_archive_if_post_unreviewed = true`，超 7 天仍未审阅的单据暂缓沉入 `archive/` 并在台账告警。
3. **版本防重放与内核锁**：人类审阅（`review`）与系统开发授权（`develop`）必须校验当前原票字节 `expected_sha256`，内容漂移或旧哈希重放一律拒绝；并发写操作采用跨平台非阻塞内核文件锁（POSIX `fcntl` / Windows `LockFileEx`）。
