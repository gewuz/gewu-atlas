# 四通道传参、双态胶囊防熔断与零泄漏路书

- **适用场景**：领空模板校验、防同参连续报错/空转熔断、长正文 stdin 传入及 GUARD 拦截处理
- **标签**：capsule, breaker, stdin, template, guard
- **维护者**：Antigravity
- **更新日期**：2026-11-19

## 1. 适用场景与触发信号

- 传入大段正文 `--note`/`--content`、遇到 `GUARD: PARAM_FIX` / `INPUT_REQUIRED` / `HALTED` 时必读。

## 2. 高频损耗 → 直通对策

| 损耗点 | 上次表现 | 下次直通做法 |
| :--- | :--- | :--- |
| **同参连续试错触发 `E_HALT_LOOP_DETECTED`** | 缺参报错后不改槽位或同参数反复 `list` 超过 3 次（300 秒窗口内）被硬熔断 | 参数错了先跑 **`--template`** 核对合法字段；不要在同一只读查询上无变化连轮 3 次以上 |
| **命令行参数泄露或转义失败** | 长文本/含特殊符号说明直接拼进 `--note` 命令行 | 使用 `--note -` / `--content -` 经 stdin 传入，或用 `--json-stdin` |
| **遇到 `GUARD: INPUT_REQUIRED` 强行绕过** | 出现 `E_PRE_REVIEW_REQUIRED` / `E_VERSION_MISMATCH` 后试图换号/重开绕过 | 立即停手并呈报人类确认/刷新 `expected_sha256`；任何强绕行为都会触发并发冲突拒单 |

## 3. 黄金流水线与避坑锚点

1. 首次接手先调 `./gewu ticket --template` 拿齐全部枚举与槽位，避免白耗重试额度。
2. 每次写操作出口自动附带 `[DELIVERY_GATE]` 空间体检结果，确保工作区根目录保持净空。
