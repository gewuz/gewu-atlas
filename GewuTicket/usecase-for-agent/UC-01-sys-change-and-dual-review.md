# SYS 系统变更闭环与双维审阅防重放路书

- **适用场景**：修改系统源码/契约/技能前的 SYS 建单、pre/post 审阅、develop 授权与 done 结单
- **标签**：SYS, review, develop, sha256, done
- **维护者**：Antigravity
- **更新日期**：2026-11-19

## 1. 适用场景与触发信号

- 涉及任何域的 `AGENTS.md`、`rules/`、`contracts/`、`skills/`、`scripts/`、`dicts/`、`templates/`、`tests/` 修改时必读。

## 2. 高频损耗 → 直通对策

| 损耗点 | 上次表现 | 下次直通做法 |
| :--- | :--- | :--- |
| **`review` / `develop` 哈希参数报错** | 误传 `--approval-sha256` 触发 `E_PARAM_UNKNOWN_FIELD` 消耗唯一重试额度 | 合法槽位名为 **`--expected-sha256`**；不确定槽位时先调 `--template` |
| **连续授权时报 SHA 失配** | `review --stage pre` 后直接沿用旧哈希调 `develop` 失败 | `review` 与 `develop` 都会写入对话流并改变原票 SHA-256，**下一步前必先跑 `--action in` 刷新 `approval_sha256`** |
| **重试建单造成重复工单** | 失败重试换标题建多张同类票 | 传入稳定 **`--key <task-key>`** 复用同主体同分类在途原票 |
| **`SYS` 票 `done` 未核销 `[L1]` 断点** | 结单漏传 `--set complete_l1=true` 导致断点未勾选 | 系统变更验收与 Finish Commit 完成后，结单附 **`--set complete_l1=true --note "finish_commit=<sha>; ..."`** |

## 3. 黄金流水线与避坑锚点

1. `git rev-parse --short HEAD` 记下 `start_commit`。
2. `./gewu ticket --action out --volume ./Volumes/GewuTicket-data --category SYS --from <Agent> --key <key> --title "<摘要>" --write-scope "<作用域>"`。
3. 拿到人类明确放行后：`review --stage pre --outcome approve --expected-sha256 <sha1>` → `in` 取 `<sha2>` → `develop --expected-sha256 <sha2> --write-scope "<作用域>"` → `pickup`。
4. 施工、验测并完成 Git 提交后：`reply --note "finish_commit=<sha>; ..."` → `done --set complete_l1=true`。
