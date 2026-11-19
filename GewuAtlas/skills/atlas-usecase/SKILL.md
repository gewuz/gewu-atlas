---
name: atlas-usecase
description: Atlas 实战经验路书管理与复用（usecase-for-agent CRUD：list / get / match / upsert / delete）：开工前查阅 usecase-for-agent/README.md 复用前人避坑对策与高分配方，异常复盘或人类要求记录心得时增删改查 Usecase 经验卡。
---

# 实战路书 · Usecase 增删改查与复盘沉淀（usecase-for-agent）

统一管理 [usecase-for-agent/README.md](../../usecase-for-agent/README.md) 下的 Agent 实战避坑路书与高分交付配方。

## 1. 何时读取与何时记录

- **开工前读取（省去 30%~50% 试错）**：接手批量网页入库、原子萃取或多篇课题研究前，先读 [usecase-for-agent/README.md](../../usecase-for-agent/README.md) 索引表，或直接调用 `--action usecase --mode match` 获取匹配路书。
- **复盘时记录（人类提示“记录本次心得/生成 usecase”或经历多次交互纠偏后）**：
  - 先用 `list` / `match` 查看是否已有同类场景卡；
  - **同类合并升级（推荐）**：对已有 `uc_id` 执行 `upsert`，将新发现的「损耗点 → 下次直通做法」并入表格；
  - **新场景立卡**：传入新 `uc_id`（如 `UC-03-xxx`）执行 `upsert`，算子自动刷新 `README.md` 索引表。

## 2. 核心命令模板（免 `--ref` 跨库通用）

```bash
# 1. 按当前任务关键词匹配实战经验卡（直接返回匹配卡正文）
./gewu atlas --action usecase --mode match --query "网页入库 切片 萃取"

# 2. 列出全部 Usecase 索引或读取指定经验卡
./gewu atlas --action usecase --mode list
./gewu atlas --action usecase --mode get --uc_id UC-01-web-ingest-and-extract

# 3. 新增或更新（Upsert）实战心得并自动刷新 README.md 索引
./gewu atlas --action usecase --mode upsert --uc_id UC-03-custom-flow   --title "<路书标题>" --scenarios "<适用场景与触发词>" --tags "Tag1,Tag2" --by "<agent>"   --content "## 1. 适用场景与触发信号
...

## 2. 高频损耗 → 直通对策
| 损耗点 | 上次表现 | 下次直通做法 |
| :--- | :--- | :--- |
| ... | ... | ... |

## 3. 黄金流水线与高分交付配方
1. ..."

# 4. 删除过时经验卡
./gewu atlas --action usecase --mode delete --uc_id UC-03-custom-flow
```

全局入口声明见 [AGENTS.md](../../AGENTS.md)，规范见 [rule-arch-workflows-ipc.md](../../rules/rule-arch-workflows-ipc.md) 与 [rule-core-methodology.md](../../rules/rule-core-methodology.md)。
