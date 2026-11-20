# 契约：受控 URL 多层采集、顺风车空壳自动清理与胶囊引导规范（`gewu.url-ingest-crawl/1`）

本契约规范 `GewuAtlas` 在执行 `url-ingest`（及 `ingest --urls`）抓取外部网页时的内容质量门禁、2～3 层有限深爬流程、无实质内容网页的自动/引导删除机制，以及出口胶囊（Capsule）提醒协议。

---

## 1. 双口径内容质检与顺风车自动删除（`min_chars = 300`）

每次 `url-ingest` 下载并转译网页为临时 Markdown 后，出口顺风车必须立即计算两项净字数指标与外链集合：

1. **总净字数（`char_count`）**：剔除空白字符与基础 Markdown 符号（`# * \` > | - _`）后的字符数。
2. **去链接纯正文净字数（`prose_chars`）**：先剥离 Markdown 图片嵌入、Markdown 超链接 `[文本](URL)` 与裸 `http(s)://` URL，再计算净字符数，用于识别「总字数超过 300 但全篇仅为目录链接列表」的页面。
3. **三态分流规则**（`min_chars` 默认 `300`，传 `--min_chars 0` 可显式关闭）：
   - **空壳页直接顺风车删除（`discarded_too_short`）**：当 `char_count < min_chars` 或 `prose_chars < min_chars`，且页面内有效外链数 `== 0` 时，顺风车**直接删除本地临时文件**（不落盘 `21-Library/`、不执行 `note-slice`），并在采集台账 `url-ingest-log.md` 记为已抛弃。
   - **短页 / 纯目录页含外链（`thin_with_links`）**：当 `char_count < min_chars` 或 `prose_chars < min_chars`，且有效外链数 `> 0` 时，不污染 `21-Library/` 正文库，删除临时文件并将内容预览（`preview`）、提取外链清单（`extracted_links`，上限 `30` 条）与层级信息返回给 Agent 检查。
   - **高链接密度边界目录页（`index_candidate`）**：当 `prose_chars >= min_chars`，但外链数 `>= 5` 且 `prose_chars < max(600, link_count * 35)` 时，暂落盘供 Agent 核查全文，但**强制跳过 `--auto_slice`**（避免生成 TOC 与切片造成回链锁死），并在返回体标记 `index_candidate: true`。

---

## 2. 2～3 层有限深爬契约（`--depth 1..3`）

- `url-ingest` 支持 `--depth`（默认 `1`，合法范围 `1..3`）与可选 `--parent_url`；传入 `--depth > 3` 或 `< 1` 时由门面以 `E_PARAM_INVALID` 硬拦截，严禁无限递归爬取。
- **Agent 决策驱动（禁止无脑全站爬）**：
  1. **第 1 层（`--depth 1`）**：抓取入口 URL；若返回 `thin_with_links` 或 `index_candidate`，由 Agent 检查预览/内容与外链相关性，筛选出目标正文链接生成新的 URL 列表（单批建议 `<= 5` 条）。
  2. **第 2～3 层（`--depth 2` / `--depth 3`）**：将筛选后的 URL 列表再次传入 `url-ingest --depth 2`（必要时至 `--depth 3`）抓取实质正文；若在第 3 层仍仅获得目录链接（`depth_limit_reached: true`），必须停止继续下钻并向人类汇报。
- 每一层的采集状态、层级（`depth`）、`prose_chars/char_count` 与候选外链均追加写入工作台 `url-ingest-log.md`，作为去重与审计依据。

---

## 3. 无实质内容网页清理契约

- `< 300` 字且无有效外链的空壳页，以及 `prose_chars < 300` 的纯目录页，由顺风车在落盘前自动物理删除。
- 对于进入 `index_candidate` 的边界目录页（`nte_XX`），Agent 在检查内容并完成第 2/3 层 URL 提取后，若确认其不具备独立正文研究价值，**必须在进入 `atomic-extract` 或 `guard --mode delivery` 前执行 `./gewu atlas --action trash --ref <vlt_XX> --mode delete --note_ref <nte_XX>` 删除**，保证 `21-Library/` 仅沉淀高信息密度原文。

---

## 4. 出口胶囊 `[INGEST_GATE]` 提醒协议

`url-ingest`（含 `ingest --urls`）返回的三位一体胶囊必须依据本次质检结果携带 `[INGEST_GATE]` 状态提醒：

- **顺风车清零通报（`ANCHOR`）**：每次采集在 `ANCHOR` 明确写明顺风车已检查双口径 `<300` 字阈值，并通报本次自动删除的空壳页数量（`discarded_count`）。
- **需追查目录外链且 `depth < 3`**：`STATE` 标注 `[INGEST_GATE: INSPECT_LINKS (depth=d/3)]`，`ORBIT` 首条给出检查内容/外链后发起 `--depth d+1` 采集的精确命令；若存在 `index_candidate` 句柄，同步给出确认无实质内容后调用 `trash --mode delete` 删除的指令。
- **达到 3 层上限**：`STATE` 标注 `[INGEST_GATE: DEPTH_CAP_REACHED (3/3)]`，提醒停止下钻并清理无用目录页。
- **全部为实质正文**：`STATE` 标注 `[INGEST_GATE: SUBSTANCE_READY]`，可正常进入切片与 `atomic-extract`。
