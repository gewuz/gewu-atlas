---
name: atlas-qa
description: Atlas 找与读（search / locate / list / read / batch-get / outline / note-grep / project-brief / file-read / links / answer）：单步查库定位、跨库或单库检索、一屏课题全景（project-brief）、受控跨卡正则检索（note-grep）、长文大纲行号读取（--outline）、批量读取多卡正文、工作台源码直读与多源只读循证成文问答。
---

# 找与读 · 检索、批量实读与循证成文交付

## 1. 三档检索模式（按场景直选，告别盲目读全文）

进库先用 `./gewu atlas --action vault` 拿库句柄（多轮深挖建议先执行 `./gewu atlas daemon start` 预热内存索引，结束再 `daemon stop`），再按场景选用 `note-search --mode <locate|passage|deep>`（默认 `passage`）：

| 模式 (`--mode`) | 适用场景 | 返回载荷 | 后续动作 |
| :--- | :--- | :--- | :--- |
| **`locate`** | 找卡片位置、判重、准备更新/搬迁/加双链 | `note_ref`、`title`、`doc_type`、`summary`、`updated` | 极省 Token；需看正文时再按 `nte_XX` 调 `note-get` |
| **`passage`**（默认） | 常规问答、查概念/参数/结论 | `note_ref`、`title`、`score`（0–100）、`doc_type`、`updated`、`section`、**按文档类型自适应提取的 `passage`（原子/书签卡返全文、MOC/Artifact 返命中完整章节、其他文档返命中完整段落）** | 胶囊返回 `[QA_GATE: PASSAGE_READY]`：**常规问答可直接基于 `passage` 与 `nte_XX` 作答，无需二次 `note-get`**；仅当 `passage_truncated=true` 或需跨卡深度综合时补读全文 |
| **`deep`** | 专题深挖、溯源上下游、梳理图谱脉络 | `note_ref`、`title`、`score`、`doc_type`、`summary`、`updated`、**入链 Top 3（`in_links_top3`）**、**出链 Top 3（`out_links_top3`）** | 顺高分入链（上游 MOC/引用方）与出链（下游证据）调用 `note-get --note_refs` 精准下钻 |

- **按需批量实读全文与低回合读原语**：
  - 批量读正文：`./gewu atlas --action note-get --ref <vlt_XX> --note_refs nte_01,nte_02,nte_03`
  - 长文先看大纲与行号：`./gewu atlas --action note-get --ref <vlt_XX> --note_ref <nte_01> --outline true`（再按 `--section` 或 `--start_line/--end_line` 精准精读）
  - 跨卡精准 grep（字面/正则）：`./gewu atlas --action note-grep --ref <vlt_XX> --pattern "<关键词或正则>" [--regex true] [--with_section true] [--project_ref <prj_XX>]`（加 `--with_section true` 直接带回命中所属完整章节，免二次 `note-get`）
  - 一屏全景与只读合批：`./gewu atlas --action project-brief --ref <vlt_XX> [--project_ref <prj_XX>]`；多项只读探查可直接打包进 `batch --json-stdin`（自动走共享锁、零 DOC 票/零快照快路径，支持 `$g.note_refs` 将 grep/search 命中串联进下一步 `note-get`）
- 需看 YAML 原文或分段时传 `--note_ref <nte_01> --raw true [--start_line 1 --end_line 200]`。
- 全库或课题域盘点清单用 `note-list --ref <vlt_XX> [--project_ref <prj_XX>] [--zone ...]`；只读全库图谱与孤岛统计用 `graph-report --ref <vlt_XX>`，全库双链与死链巡检用 `vfs-links --ref <vlt_XX>`；读工作台源码/非 Markdown 文本用 `file-read --ref <vlt_XX> [--project_ref <prj_XX>] --zone workbench [--subdir <目录>] [--file_name <文件名>]`；收件箱用 `ingest --ref <vlt_XX> --mode list`；日记用 `journal`；书签用 `slip --mode get`。

## 2. 检索引擎算法声明与选卡决策指南（BM25 + 双链拓扑图增益）

`note-search` 底层运行与 `116-gwf-v2-R6` 对齐的**词元 BM25 + 双链拓扑图增益**离线检索引擎，Agent 应依据该排序机理构造查询并挑选命中卡片：

1. **核心排序算法（先文本 BM25，再 Top-50 图增益重排）**：
   - **三层词元协议（免分词）**：显式 `--terms "词A,词B"` 优先 > 空白/逗号/分号自动切分（ASCII 双引号 `"exact phrase"` 保序保留） > 整串子串兜底。多词元采用 OR 召回累加，命中多词元自然升权。
   - **字段加权 BM25**（$k_1 = 1.2, b = 0.75$，Lucene 式 IDF）：`title (8.0) > aliases (6.0) > summary (3.0) > tags (2.0) > body (1.0)`；文档长度按 5 字段总字符数归一化，**短小精炼的 `MOC` / `atomic` 天然优于冗余长文（`library` / `slice`）**。
   - **Top-50 双链拓扑乘法增益**（`--boost hybrid` 默认开启）：对 BM25 前 50 名候选计算
     $$\text{FinalScore} = \text{BM25} \times \Bigl(1 + \alpha \cdot \ln(1 + \text{InDegree}) + \beta \cdot \ln(1 + P_{\text{2-Hop}})\Bigr)$$
     默认 $\alpha = 0.15$（1 跳全库入链权威度，含正文双链与 Frontmatter `map:` 归属）、$\beta = 0.08$（Top-50 候选簇内 2 跳共引凝聚度）。跨库 `--all_vaults true` 联合检索按原始融合分 `score_raw`（`--boost text` 时按 `bm25_score`）全局排序。
2. **为什么往往「MOC 排前列、核心 Atomic 次之」，但并非绝对**：
   - 引擎**没有硬编码 `MOC > atomic` 类型加权**；在宏观/主题类查询下，`MOC` 因关联全簇卡片且被大量 `atomic` 通过 `map:` 反向指向（高 `InDegree` 与高 `Cluster_2-Hop`），通常稳居前列，高被引核心 `atomic` 紧随其后；
   - 但在**精准概念点查**时，若某张 `atomic` 的 `title`（8x）或 `aliases`（6x）精确命中而 `MOC` 仅在正文略提，该 `atomic` 的 BM25 基础分可显著反超 `MOC` 跃居第一。
3. **Agent 选卡与调参实战规则**：
   - **看全貌 / 梳理架构与对比**：优先选 `doc_type=moc`（或高 `score` 且 `direct_links` 密集的枢纽卡）作入口，再用 `note-get` 实读并顺 `direct_links` / `two_hop_bridges` 下钻核心 `atomic`。
   - **查精确定义 / 参数机制**：优先选 `doc_type=atomic` 中 `bm25_score` 高、且为 `status=active` + `type=OBJ/SYN` 的卡片；遇 `type=HYP` 或 `status=pending` 须降权并注明待核验。
   - **核验一手细节**：当 `MOC` / `atomic` 颗粒度不够时，用 `--doc_type slice` 或 `--doc_type library` 回溯原文卷宗，或配合 `--hops 2` / `--hops 3` 查看桥接与 `graph_metrics`。
   - **常用精准控制参数**：
     - 显式切词：`--terms '"GraphRAG exact", 算法'`；关闭图增益做纯文本对照：`--boost text`；调整图权重：`--alpha 0.2 --beta 0.12`。
     - 载荷与阈值控制：`--hops 0|1|2|3`（0 省 Token 只看命题，2 含桥接，3 含 3 跳图指标，兼容 `--deep true`）、`--min_score 40`、`--no_snippet true`、`--snippet_width 120`、`--summary_width 80`、`--fields ...`、`--link_fields ...`、`--no_cache true`（严格零磁盘写缓存）。
     - 过滤与穿透：`--doc_type`、`--epistemic_type`、`--status active`、`--exclude_zones journal`、`--exclude_project_refs prj_02`；默认隔离 `workbench` / `private` / `attic` / `deprecated`，严格只查已转正定稿可加 `--exclude_pending true --exclude_draft true`；穿透私有或历史归档加 `--private true` / `--include_archived true`。

## 3. 交付验货闸门（60 分算子真值 + 40 分通用三维量表）

- **算子硬底线（60/60）**：已通过 `note-get` 实读被引用卡片（或调用 `guard --mode delivery --ref <vlt_XX> --note_refs nte_01,nte_02` 核验引用句柄真值，杜绝虚构句柄）。
- **语义质量评估（40 分，L2 脑内自检 / L3 综合问答优先调 Subagent 盲审，无则自评）**：
  1. **完整性（15分）**：是否完整回应了用户原话诉求，写成「结论先行 → 核心界定 → 机理对比 → 适用边界」的连贯文章（严禁直接拼贴搜索列表充数）？
  2. **真实性（15分）**：核心论点是否句句绑定已实读的 `nte_XX` 句柄并区分 `active`/`pending`/`workbench` 真值状态？若检索 0 命中（`[QA_GATE: ZERO_HITS]`），是否明确声明「当前知识库中暂未收录相关资料」？
  3. **可读性与脱敏（10分）**：散文流畅、零本机物理路径泄漏（本机查看仅提示打开 `Volumes/GewuAtlas-data/`）。

全局路由见 [AGENTS.md](../../AGENTS.md)，规范见 [rule-core-linking.md](../../rules/rule-core-linking.md)、[rule-arch-workflows-ipc.md](../../rules/rule-arch-workflows-ipc.md) 与 [rule-core-formatting-tiers.md](../../rules/rule-core-formatting-tiers.md)，排版配合 [atlas-style](../atlas-style/SKILL.md)。
