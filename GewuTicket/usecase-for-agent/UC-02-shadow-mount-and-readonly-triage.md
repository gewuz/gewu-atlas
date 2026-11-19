# 只读查票、shadow-rw 影子协作与冷归档路书

- **适用场景**：ro 只读卷核查、分段读大票、shadow-rw 隔离修改与安全合入、7/30天归档扫盘
- **标签**：ro, shadow-rw, triage, archive-sweep
- **维护者**：Antigravity
- **更新日期**：2026-11-19

## 1. 适用场景与触发信号

- 在只读（`ro`）卷查票、阅读超长工单、多 Agent 走影子（`shadow-rw`）改票并回填底卷时必读。

## 2. 高频损耗 → 直通对策

| 损耗点 | 上次表现 | 下次直通做法 |
| :--- | :--- | :--- |
| **对 `ro` 卷发写命令触发阻断** | 在 `ro` 挂载下调 `reply`/`pickup` 返回 `E_VOLUME_READONLY` (`GUARD: INPUT_REQUIRED`) | `ro` 卷只允许 `in`/`list`/`find`/`categories`/`shadow-diff`；需写入时先请示切 `rw` 或改用 `shadow-rw` |
| **影子目录与底卷重叠** | `--shadow-dir` 设为底卷或其父/子目录被直接拒绝 | 为 `--shadow-dir` 传入**独立同级目录**，合并顺序严格遵循 `shadow-diff` → `shadow-apply` → `shadow-drop` |
| **大票正文被截断或刷屏** | `in` 默认返回摘要预览，多次重试相同读取命令触发只读熔断 | 需要全文传 **`--full true`** 或用 **`--start-line` / `--end-line`** 分段拉取 |

## 3. 黄金流水线与避坑锚点

1. 只读排查：`list --status open` / `find --post-review-status pending_review` → `in --ticket-id <单号> --full true`。
2. 影子协作：带 `--mount-mode shadow-rw --shadow-dir <独立目录>` 执行读写 → `shadow-diff` 核验增量 → `shadow-apply` 合入底卷 → 核验无误后 `shadow-drop`。
3. 归档清理：`done` 会自动触发超 7 天 `closed/ -> archive/` 扫盘与超 30 天归档清理（`hold_archive_if_post_unreviewed` 配置保留除外）；按策略查看先调 `categories`。
