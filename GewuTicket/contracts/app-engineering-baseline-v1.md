# 接口契约：格物域工程基线（`gewu.app-engineering-baseline/2`）

本基线适用于 `GewuForge` 与所有由 `GewuForge` 锻造的新格物域。

## 1. 六大硬性工程支柱

1. **`contracts/` 统一接口面**：所有用于「管理其他域」与「被工作区网关/平台管理」的接口清单与契约（`cli-manifest.json`、`object-catalog.json`、`testing-profile.json`、`app-security-profile.json`、`data-layout-profile.json`、`bootstrap-profile.json`、`execution-routing-v1.md`、`commit-message-v1.md` 等）统一收口于 `<Domain>/contracts/`（统一网关 `./gewu` 兼容 `connect/`）。
2. **目标驱动 + 空间围墙**：禁止在工作区根目录散落临时调研/草稿/构建目录；所有未完成域的阶段性状态通过域内断点（`.forge-checkpoint.json` + `CHECKPOINT.md`）持久化。
3. **高内聚意图技能 + 路书外置**：顶层技能不超过 8 个（超 8 个须聚合或下沉至 `references/`），高频避坑配方与实战流水线外置至 `<Domain>/usecase-for-agent/`，`AGENTS.md` 严控 ≤ `12000` 字节。
4. **受控算子 + 胶囊顺风车**：算子必须吞掉原始堆栈，返回 Triad/Guard Capsule；写算子出口自动顺风车搭载交付门禁分数与断点坐标。
5. **60/40 分级交付验货**：`L1` 单步直交、`L2` 胶囊+自检、`L3` 复合域构建/重构必过 **60 分脚本硬评估（`guard --mode delivery`）+ 40 分 Subagent 盲审/自评软量表**（总分 ≥ 60 且硬评估 60/60 方可交付）。
6. **零 `GewuTrack` 依赖**：新域与 `GewuForge` 均独立运行，不绑定 `GewuTrack/MANIFEST.json`，不强制委托外部 Track 运行器；系统变更由 `GewuTicket`（`SYS` 票）与 Git 闭环保障。
