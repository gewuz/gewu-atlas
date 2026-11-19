---
doc_type: "usecase"
uc_id: "UC-03-methodology-extract-and-multi-moc"
title: "方法论萃取与多 MOC 自述漫游路书"
scenarios: "已入库资料提炼方法论、多主题 MOC 生成、漫游自述与扩展建议回填"
tags: ["Methodology", "MOC", "Roam", "Extract", "Governance", "Reflection"]
updated: "2026-11-19"
updated_by: "doubao"
---

# 方法论萃取与多 MOC 自述漫游路书

## 1. 适用场景与触发信号

- 已有入库资料（Library/Slices）需要提炼方法论，并按主题生成多张 MOC、补写漫游自述与扩展建议时必读。
- 触发词：方法论萃取、主题地图、多个 MOC、漫游建图、知识地图、自述路径、逻辑闭环评估。

## 2. 高频损耗 → 直通对策

| 损耗点 | 上次表现 | 下次直通做法 |
| :--- | :--- | :--- |
| **切片 provenance 校验误伤** | 句柄类型正确（note_ref 为 Library、slice_refs 均为切片），仍报 `E_PARAM_INVALID: slices 与 provenance 派生结果不一致`；单切片、换 note_ref 均失败，同批其他源却正常 | 默认走**单源萃取**：`--note_ref <library|toc>` 且**不带 `--slice_refs`**，正文内用 `nte_XX` 逐个引用；不要为了"证据更全"硬凑 slice_refs。根因未定位，暂标记"标题含括号/&/超长 wikilink 的切片更易触发"，仅为相关性观察。 |
| **想预演却无 dry_run** | `atomic-extract --dry_run true` 直接报"此动作不支持 --dry_run" | 参数合法性只查 `--template`；非要试写就用 30 字探针内容，事后 `note-deprecate --move_to_attic true`。`trash` 会被 TOC 回链阻断（`E_VFS_REFERENCED`），别纠缠。 |
| **doc_type 凭直觉填错** | `note-create --doc_type note` 报"未知 doc_type: note" | workbench 区固定 `--doc_type workbench`；`artifact-adopt` 成功后自动转 `artifact`。填之前看 `metadata.yaml` 的 `fixed_value`。 |
| **MOC 新建缺主题名** | `moc-sync` 报 `E_PARAM_MISSING`，要求显式 `--title/--name/--topic` | 新建图必须给 `--title`；只有刷新已有图才可省略。 |
| **roam 聚类过于粗糙** | 10 张方法卡因共享基础标签"萃取"，被漫游聚成一个"萃取知识地图"大簇，suggested_tag 无信息量 | roam 只作发现参考。多 MOC 用**人工策划的交叉标签**（Modeling/Governance/Delivery/SemanticLayer），先 `--dry_run true` 核对 `matched_count` 与 `included_refs`，再逐张写图。 |
| **小簇撑不起一张图** | Delivery 初始只有 2 张卡 | 用 `note-update --tags` 补 1~2 个交叉标签（如把行业案例卡加 Delivery），同时保留 `Atom,萃取` 基础标签；每图 3~5 张为宜。 |
| **MOC 占位符残留** | 自动图底部留"（待 Agent 结合已读卡片…）""（待 Agent 提出 2~4 条…）" | 生成后用脚本读原文，只替换这两处占位符并在 H1 后插入"漫游自述"，**保留 atlas 索引块与 mermaid**；不要整篇重写，否则丢自动索引。 |
| **想交文件但围墙禁止** | 根目录不能建 `reports/`，`present_files` 又没有可交路径 | 报告走 `note-create(workbench) → artifact-adopt`，交付用 `nte_XX` 句柄；不擅自往根目录导出 Markdown，也不做双份落盘。 |
| **长写操作被转后台** | 单次 atomic-extract 超过 15s 自动转 background，前台空等 | 批量萃取/建图写成串行 shell 函数，整体 `run_in_background`，再用 `TaskOutput` 收口；不要并发写同一课题，避免 Git 提交冲突。 |

## 3. 黄金流水线与高分交付配方

1. **预检三连**：`vault` 取库、`fmt --setup` 补运行时、`project-list` 查重；再 `project-create --default_mode auto` 开课题并拿 `ws-<agent>/drafts`。
2. **入库按 UC-01**：单批 URL ≤ 5 条、串行、`--auto_slice true`；ISO 官网 403 时改走 `webstore.iec.ch` 预览页或标准站镜像。
3. **证据地图先于萃取**：`note-get --note_refs` 批量实读切片，先写 `claim → nte_XX` 映射，再动笔；厂商自述、联盟报告、预印本分别标"第三方实现/综合推断"。
4. **方法卡固定十件套**：CQ 驱动、六阶段流程、三层架构、顶层复用、语义层先行、不空白起步、语义契约治理、LLM 三纪律、定义权/版本/验收、90 天最小切片；按 OBJ/SYN/HYP 分层，每卡"断言→依据→边界"。
5. **萃取默认单源**：`atomic-extract --note_ref <library>`，不带 `--slice_refs`；正文引用必须与真实读过的切片一致。
6. **多 MOC 两步走**：先 `moc-sync --title <主题> --tag <标签> --dry_run true` 验证簇，再正式写图；总图用 `Methodology`，子图用 `Modeling/Governance/Delivery/SemanticLayer`。
7. **回填自述**：每图补"漫游自述（Agent 路径）"、一段"论证链闭环情况"、3 条可执行扩展建议；保留自动索引与脑图。
8. **验货两段**：`guard --mode delivery --project_ref <prj>` 拿 60/60；再按完整性/真实性/可读性做 40 分自评，两者分开报。
9. **复盘沉淀**：任务结束把新损耗并入本卡，避免下次重复试错。
## 4. 复盘心得（2026-11-19 记录）

### 4.1 三次同类错误，同一个根因

- **把自然语言直觉当接口契约**：证据要「全」、文档类型该叫 `note`、预演该叫 `dry_run`。三次都不是能力问题，是没查契约。
- **下次直通**：任何写操作前做 30 秒接口侦察——读 `--template`、看 `metadata.yaml` 的 `fixed_value`、确认是否有 `--dry_run`。

### 4.2 证据执念的代价

- 为凑 `--slice_refs` 连废 4 张卡；改成「单源 `--note_ref` + 正文 `nte_XX` 引用」后一次通过。
- **结论**：先求通过，再求完备。证据强度由正文引用决定，不由参数堆叠决定。

### 4.3 试错必须有成本上限

- 在生产库建探针卡，事后 `trash` 被 TOC 回链锁死，只能 `note-deprecate` 进 attic。
- **下次直通**：只读优先；必须试写时用 30 字探针，并预留废弃通道；同类错误连败 2 次立即停手。

### 4.4 漫游是雷达，不是地图

- roam 按基础标签「萃取」把 10 张方法卡聚成一个「萃取知识地图」，suggested_tag 没有信息量。
- **真正的漫游要带假设跳线**：问题→模型→运行、复用→治理、判断→验证。由叙事决定切图，不由算子决定切图。

### 4.5 报告的价值在判断与边界

- 覆盖五条路线只是资料汇编；写成「什么条件下选哪条、什么条件下会失败」才是研究。
- 每条结论标三级真值；厂商数字只作方向性证据，不上升为基准。

### 4.6 下次开工检查清单

1. 接口侦察：`--template`、枚举值、`--dry_run` 支持情况。
2. 萃取默认单源，`slice_refs` 非必需；正文引用必须与实读切片一致。
3. 写操作串行 + 后台 + `TaskOutput`；生产库不建测试卡。
4. 多 MOC 先 `--dry_run` 看 `matched_count` 与 `included_refs`，再写图。
5. 每张 MOC 必须有自述路径、闭环判断、三条缺口，否则只是目录。
6. 开工先定交付形态（句柄 / 文件 / 在线文档），别到最后才发现无处可交。
7. 验货分两段：guard 60 是合规底线，40 分自评才是内容质量。
