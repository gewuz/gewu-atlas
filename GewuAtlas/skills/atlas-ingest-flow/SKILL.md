---
name: atlas-ingest-flow
description: Atlas 知识入库、多模态转换、自适应切片、批量原子萃取（batch）、MOC 局部补丁织网（note-patch）与热内核加速（daemon）全流程编排（daemon start -> ingest -> slice -> extract -> approve -> moc-sync -> daemon stop）：统一支持新文档入库、URL 抓取、DropZone 处理与知识织网。
---

# 入库与加工 · 摄取切片、原子萃取与 MOC 织网

## 1. 空间归口与推荐高速配方

1. **热内核预热、定库与课题工作台**：批量入库/萃取前建议先跑 `./gewu atlas daemon start`（零宿主 MCP 依赖，收尾跑 `daemon stop`）；再调 `vault` 拿 `ref`；多方向/多报告研究先调 `project-create --ref <vlt_XX> --title "<课题>" --agent <名>`（自动分配 `ws-<agent>` 工作台，或调 `workbench-workspace`）。严禁在工作区根目录自建 `sources/`、`reports/`、`99-Assets`。
2. **统一摄取 `ingest`（权威源直入 `21-Library` + 一步切片）**：
   - 查看收件箱：`ingest --ref <vlt_XX> --mode list`
   - 批量 URL 或本地文件一步转译 + AST 切片 + 生成 TOC：
     ```bash
     ./gewu atlas --action ingest --ref <vlt_01> [--project_ref <prj_01>] --urls "<URL1>,<URL2>" --auto_slice true
     ./gewu atlas --action ingest --ref <vlt_01> [--project_ref <prj_01>] --file_name <文件名> --auto_slice true
     ```
   - 同一库、同一课题、处理参数相同的 URL，合并成一次 `ingest --urls` 调用。
   - 单条 URL 报 `E_URL_NETWORK` / `E_URL_POLICY`（403/404/超时）时自动跳过失败项并保留成功结果，勿重试失败 URL；`--deep true` 长篇精修配合 `fmt-window` / `fmt-apply-window` / `fmt-assets` 与 `note-slice`。
   - **双口径短页/目录闸门、2~3 层深爬与采集台账（`[INGEST_GATE]`）**：下载正文默认核验总净字数与去链接纯正文净字数（`prose_chars < 300`，可用 `--min_chars 0` 关闭）——① **不超过 300 字且无有效外链的空壳页**由顺风车直接物理删除（`discarded_too_short`，不入库）；② **短页或纯目录页含外链**（`thin_with_links` / `index_candidate`，不切片防锁死）由 `[INGEST_GATE]` 胶囊提醒 Agent 检查内容，筛选目标子链接生成新 URL 列表，按 `--depth 2` / `--depth 3`（硬上限 `3` 层）再次调 `url-ingest`；③ 确认无独立正文价值的目录页须调 `trash --mode delete` 清理。每次采集自动记入工作台 `url-ingest-log.md` 并返回 `ingest_log_ref`，跨轮回看用 `note-get --ref <vlt_01> --note_ref <ingest_log_ref> --tail 40`，**严禁在 `tmp` 或根目录自建日志文件**。
3. **原子萃取 `atomic-extract`（蒸馏独立命题，禁照抄切片）**：
   - 先用 `note-get --note_refs` 实读证据。按[原子卡写作与审阅规则](../../rules/rule-core-methodology.md)提炼一个主张，写清依据、推理和适用边界，按材料决定标题和段落。OBJ 写条款或观测及其条件，SUB 写决策目标与取舍，SYN 写跨来源推导及前提，HYP 写待检验预测与反证条件。
   - 例：假设来源记录了两台机器的标记文件读数，可以把尚待验证的解释写成一个自然段。实际使用时替换为已读材料与真实句柄。
     ```bash
     ./gewu atlas --action atomic-extract --ref <vlt_01> --note_ref <来源句柄> --slice_refs <切片句柄> --epistemic_type HYP --title "文件读取可能仍在本机执行" --summary "远端任务读到本机标记，可能源于文件服务仍绑定本机。" --content "任务切换到远端后读出的标记仍与本机相同，但两台机器可能恰好存有同样的内容。可在同一路径分别写入不同标记，再重复读取。若稳定返回远端标记，就削弱了文件服务仍绑定本机的解释。本次检验只覆盖文件读取。"
     ```
   - 算子自动不在原子卡 YAML Frontmatter 中写入 `sources` / `slices`，并在正文末尾自动生成 `## 参考来源与知识库索引`（写法 A：列表式 Obsidian 双链 + `` `nte_XX` `` 句柄留痕）；返回体直接附带 `content_sha256`。多张原子卡萃取推荐封装进一次 `./gewu atlas --action batch --ref <vlt_01> --json-stdin`（单锁合并快照与闸门，支持 `$step_id.note_ref` 句柄串联）。`auto` 域直出 `active`；**`supervised` 域生成 `pending` 候选并在 `INPUT_REQUIRED` 停下**（严禁传 `--auto_approve true` 绕过），获人类确认后带 `expected_sha256` 调 `atomic-approve`。
4. **MOC 织网与自主漫游建图**：
   - 转正后用 `moc-sync` 刷新受控的折叠索引。Agent 实读来源，在索引前围绕一个具体问题写成文章：从现象或实例出发，说明机制、证据和适用边界，用 `[[目标|句中自然措辞]]` 将引用嵌入论证。用 `note-patch` 编辑正文，保留索引标记。
   - 末章 `## 逻辑闭环评估与扩展研究建议` 回到开篇问题，交代哪些判断已有证据、哪一步仍靠推断。每个重要缺口说明要补读什么材料或做什么对照，以及哪种结果会支持或改变判断。由 Agent 根据正文作出评估，卡片数量仅供盘点。
   - **自主漫游发现新 MOC**：执行 `moc-sync --ref <vlt_01> [--project_ref <prj_01>] --mode roam` 扫描未归属成熟原子簇（默认 `>= 3` 张），顺枢纽卡用 `note-search --mode deep` / `note-get` 漫游实读后再建图补全。

## 2. 交付验货闸门（算子 60 分 + Subagent/自评 40 分）

1. **算子硬评估（60分底线）**：`moc-sync` / `atomic-extract` 返回胶囊自带顺风车 `[DELIVERY_GATE]` 状态；复合入库收尾必须运行 `./gewu atlas --action guard --ref <vlt_01> --mode delivery [--project_ref <prj_01>]` 核验场地净空、Schema、双链闭合与 `provenance` 溯源真值（满分 60/60）。
2. **语义评估（40分，优先 Subagent 盲审，无 Subagent 则自评）**：
   - **完整性（15分）**：有效来源是否均已切片、萃取并织入 MOC？
   - **真实性/蒸馏度（15分）**：原子卡是否提炼为高信息密度的独立命题（而非原样照搬切片段落），且 `provenance` 准确指向真实来源？
   - **可读性与脱敏（10分）**：卡片结构清晰、Mermaid 闭合、零物理路径。

全局路由见 [AGENTS.md](../../AGENTS.md)，方法论与状态门禁见 [rule-core-methodology.md](../../rules/rule-core-methodology.md)、[rule-arch-workflows-ipc.md](../../rules/rule-arch-workflows-ipc.md) 与 [rule-core-state-identity.md](../../rules/rule-core-state-identity.md)。
