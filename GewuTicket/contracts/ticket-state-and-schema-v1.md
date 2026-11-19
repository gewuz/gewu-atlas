# 契约：格物工单状态机、单据 Schema 与 CLI 接口（`gewu.ticket-schema/1`）

本契约定义 `GewuTicket` 单据结构、双维状态跃迁规则、7 天归档策略及对外胶囊返回约定。

## 1. Frontmatter 最小契约字段

| 字段 | 类型 / 合法值 | 语义说明 |
| :--- | :--- | :--- |
| `doc_type` | `"ticket"` | 固定单据类型 |
| `ticket_id` | `TICKET-YYYYMMDDThhmm-<CAT>-<agent>[-suffix]` | POSIX 安全时间戳防撞单号 |
| `category` | `[A-Z][A-Z0-9_-]{1,15}` | 须已在卷 `_meta/categories.json` 注册 |
| `status` | `open \| in_progress \| paused \| done \| cancelled` | 主流程状态，决定所在三级状态目录 |
| `pre_review_status` | `not_required \| pending \| approved \| rejected` | 阻塞继续前审阅状态 |
| `post_review_status` | `not_required \| pending_review \| approved \| changes_requested` | 完工后非阻塞审阅状态 |
| `need_human` | `boolean` | 显式人工阻断标志（`pre_review` 通过后自动清零） |
| `created_by` / `to_agent` / `picked_by` | `string` | 建单方、指派接收方（默认 `all`）、当前接手方 |
| `closed_at` / `archived` / `archived_at` | `ISO8601 / boolean` | 结单时间（用于 7 天归档判定）与冷归档标记 |

## 2. 分类策略声明（`_meta/categories.json`）

每个分类条目声明：`title`、`template`（`structured_5sec | freeform | light_msg`）、`allow_write_scope`、`allow_patch`、`pre_review_gate`（`none | before_pickup | before_done | both`）、`post_review_policy`（`none | required | optional`）、`archive_after_days`（默认 `7`）、`purge_archive_after_days`（默认 `30`）、`hold_archive_if_post_unreviewed`（默认 `true`）。

## 3. 7 天归档与 30 天超期删除扫描（`archive-sweep`）

每次执行 `done`、`cancel` 或显式 `archive-sweep` 时：
1. 扫描 `<CAT>/closed/` 下 `now - closed_at > archive_after_days`（默认 `7` 天）的单据：
   - 若 `post_review_status == "pending_review"` 且 `hold_archive_if_post_unreviewed == true`，保留在 `closed/` 并返回 `held_unreviewed` 列表；
   - 否则原子迁入 `<CAT>/archive/`，写 `archived: true` 与 `archived_at`。
2. 扫描 `<CAT>/archive/` 下 `now - closed_at > purge_archive_after_days`（默认 `30` 天，即 1 个月）的历史归档单据并物理删除，返回 `purged_from_archive` 列表并刷新 `INDEX.md`。

## 4. 门面四通道与退出码映射

`scripts/ticket/ticket_cli.py` 严格执行四通道参数合并（`CLI Flag > --set k=v > --payload/--json-stdin > 默认值`）与 `--template` 领空表：
- 正常完成：返回 `Triad Capsule`（`ORBIT` 必含直接向人类汇报现状分支），Exit Code `0`；
- 参数错误/未知字段：返回 `GUARD: PARAM_FIX`（附最小 `fill_template`），Exit Code `2`；
- 缺人类审批/只读拒写/权限或碰撞阻断：返回 `GUARD: INPUT_REQUIRED` 或 `GUARD: HALTED`，Exit Code `1`；
- 同指纹连续失败 2 次或只读空转 3 次：硬断路器触发 `GUARD: HALTED`（`E_HALT_LOOP_DETECTED`），Exit Code `2`。


## 可靠写入与轻量协作补充（2026-11-16）

- 默认归档周期为 **7 天**；本工作区当前五个分类均为 7 天。过期删除是独立策略。显式设置其他卷的归档天数时，必须传入有效分类策略，不能只改卷元信息。
- `init-volume` 对已初始化卷只验证并返回现有配置，不覆盖分类或工单；策略变更走 `register-category`，保留其他策略字段。
- 同卷、同分类、同建单主体、同 `key` 的在途（active/paused）请求按首次请求摘要复用；内容不同报 `E_KEY_CONFLICT`。已结单后允许同 key 新开下一轮。旧票没有 request_key 时须先查票手工复用。
- 工单修改在卷锁内记录写前镜像；异常回滚，进程中断由下一次 rw 挂载恢复，ro 遇未恢复事务返回 `E_RECOVERY_REQUIRED`。保证协作程序锁内的恢复一致性，不承诺外部无锁编辑器的事务隔离或断电存储硬件保证。
- 影子目录不得与底卷相同或互相包含。合入在影子锁、底卷锁内检查漂移并执行可恢复批量写入；成功后保留影子供核对，查回底卷后可 `shadow-drop` 清空已跟踪增量，锁目录保留。不重复合入同一旧基线。
- 工单用于人机沟通与 Agent 留底，不是身份认证系统。人类明确指令可由 Agent 转录为 `reply` 或审阅依据，并按指令修改工单；保留来源、决定和执行结果，不伪造授权。派单收口遵守创建者签阅或人类明确委托，使用现有对话记录留痕，不增加身份服务或新的审批状态机。
- 循环检测按有效请求（包括 stdin 内容）与会话区分；可用 `GEWU_TICKET_SESSION_ID` 指定会话，否则使用宿主提供的 `CODEX_THREAD_ID` 或调用父进程。记录 5 分钟失效，不记录原始正文。不同请求不应误用同一计数。
