---
doc_type: "usecase"
uc_id: "UC-05D-diffuse-deep-research"
title: "UC-05D 扩散研究：模拟 Gemini Deep Research 广撒网与全量入库路书"
scenarios: "扩散研究、广撒网建库、资料全量下载入库、问题域尚未收敛、领域基线语料库、Gemini Deep Research、研究计划先审后研、D 型任务"
tags: ["DeepResearch", "Diffuse", "D", "Workflow", "UseCase", "Ingest"]
updated: "2026-11-20"
updated_by: "monorepo"
---

## 1. 适用场景与触发信号

- 用户要求「按 Gemini Deep Research 方案/工作流开展课题研究」，或需要在 GewuAtlas 中模拟「设定主题与资料来源 → 生成研究计划待审 → 后台自主多轮检索与入库 → 综合报告交付 → 交互追问/定稿」五步闭环时必读。
- 触发词：Gemini Deep Research、研究计划大纲、先审阅再开始研究、多源检索入库、L3 课题全流程。

## 2. 高频损耗 → 直通对策

| 损耗点 | 上次表现 | 下次直通做法 |
| :--- | :--- | :--- |
| **跳过研究计划确认直接开搜建课题** | 收到研究请求后直接建课题、跑全网检索与写报告，违背 Deep Research 第 2 步“先给大纲待人类审阅修改”约定 | **第 1 轮只读盘点 + 呈报大纲**：先用 `project-list` 与 `note-search` 盘点默认库既有资料，确认信源边界并输出结构化研究计划大纲，明确等待人类确认「开始研究」后再建 `prj_XX` 启动检索 |
| **只抓仓库 README 导致正文深度不足** | `IDS-RAM_4_0` 等 GitHub 仓库的 `README.md` 仅为章节目录链接，直接萃取会缺少具体机理文本；猜测子路径则偶发 404 | **两批递进 `ingest`**：首轮抓官方入口与 `README.md` 并读切片拿真实 `.md` 路径，第二轮直抓核心正文章节；遇 `E_URL_NETWORK`（404）直接跳过保留成功项 |
| **无节制广撒网导致底库信噪比下降** | 盲目抓取大量重复或低质网页，稀释后续 `atomic-extract` 质量 | **设定停止条件与去噪门槛**：单批次新增有效信息低于阈值、核心维度覆盖率达标或触达预设 URL 上限时立即停止入库，转入萃取 |
| **`note-list` 误传 `--limit` 与分区单复数混淆** | 传 `--limit 30` 触发 `E_PARAM_UNKNOWN_FIELD`，传 `--zone atomic` 查不到刚萃取的卡片 | `note-list` 不传 `--limit`，原子区固定传复数 `--zone atomics`，或在 Python 脚本中统一过滤 `doc_type` |
| **`atomic-extract` 脚本硬取 `data["note_ref"]` 报 KeyError** | 批量萃取脚本直接读取 `res["data"]["note_ref"]` 导致第 1 张成功后脚本中断 | 批处理脚本校验 `res["code"] == "OK_ATOMIC_EXTRACTED"` 即可，萃取完成后统一调一次 `note-list --zone atomics` 拿全部句柄 |
| **外部入库摘要超 200 字拖垮 `delivery_gate`** | 外部 URL 自动生成的 `summary` 达 217 字符，`guard --mode delivery` 的 `schema_integrity` 判 `0/15`（总分 45/60） | 遇 `schema_integrity` 失分立即跑 `guard --mode schema` 定位具体 `nte_XX`，用 `note-update --summary` 压至 200 字内即回满 `60/60` |
| **正文去标签后未明示 `HYP` 与估算边界** | 正文遵循极简可读性去掉了机械标签，但工程延迟估算与未来趋势研判未标注假说边界，盲审真实性被扣 1 分 | 在涉及 `HYP` 卡片或量级推演的章节末尾加轻量化 `> [!NOTE] 假说与估算边界` 提示，兼顾流畅阅读与审慎边界 |

## 3. 黄金流水线与高分交付配方（Gemini Deep Research 5 步映射到 GewuAtlas）

1. **Step 1–2（定源与出大纲 · 1 回合）**：`project-list` + `note-search` 盘点库内既有资产 → 输出「主题与资料来源盘点 + 结构化研究计划大纲」，同步向人类确认信源边界（是否限定特定外部域名、排除过时年份或挂载额外私有库），等人类确认「开始研究」。
2. **Step 3A（建域 + 并行检索 + 递进摄取与反思补搜 · 2–3 回合）**：`project-create` 开立 `prj_XX` 与 `ws-<agent>`；并行多路 `search_web` 覆盖核心维度；分 2–3 批 `ingest --auto_slice true` 抓取官方正文（Zero Polling 等后台任务唤醒），并用 `python3 - << 'PY'` 批量 `note-get` 精读切片。精读后执行**缺口评估与动态补搜**：若发现关键新线索或冲突观点则触发补充检索；满足停止条件（核心维度覆盖达标、新增有效信息低于阈值或达到 URL 上限）即收敛停止。
3. **Step 3B（串行萃取 + 链检 + MOC 织网 · 1–2 回合）**：单脚本串行 `atomic-extract` 提炼 `OBJ/SYN/HYP` 原子卡 → `note-link --mode fix` + `guard --mode links` → `moc-sync` + `note-update` 回填综述与闭环评估（保留 `<!-- atlas:moc-index:start/end -->`）。
4. **Step 4A（正交报告 + 四项硬检 · 1–2 回合）**：在 `ws-<agent>/drafts` 按确认后的研究计划顶层维度，拆分为 N 篇正交子报告合批写入（例如技术类课题可按「标准/机理篇 + 核心技术协同篇 + 生态选型决策篇」拆分；正文零机械标签与行内句柄、含对比表与安全双引号 Mermaid、文末集中列 `nte_XX`）；脚本内自动校验控制字符/代码围栏/路径泄漏/全量 `nte_XX` 存在性，并跑 `guard --mode delivery`（若遇 schema 超长，立刻 `guard --mode schema` + `note-update` 修至 `60/60`）。
5. **Step 4B & Step 5（独立盲审 + 采收 + 等待定稿 · 2 回合）**：唤起独立只读 `research` Subagent 盲审（零轮询等唤醒，实测 98/100 `PASS`）；单脚本完成「盲审微修 → 串行 `artifact-adopt` → 最终 `guard --mode delivery` 60/60」，呈报交付回执并请人类确认 `artifact-finalize` 定稿或继续追问深化。

## 4. 与「聚焦研究」的分工与串联接口（UC-06F）

- **扩散研究（本卡）**：广撒网。先把相关资料尽量下载齐全、切片入库、织网，交付后由人类在库内长期自由检索与二次研究。适合问题域尚未收敛、资料需长期复用、或要为一个领域建基线语料库。
- **聚焦研究（UC-06F）**：先对齐再检索。以确认过的问题范围为准收集证据，交付一份可直接决策的成品，速度快、噪音低。
- **选择与串联信号**：问题能一句话说清且要求快 → 聚焦；问题边界模糊、要建库备查 → 扩散。两者可串联：先用扩散产出 `prj_XX`、原子卡与 MOC 地图作为底库，下游 UC-06F 可直接挂载该底库并跳过公网初扫，直接进入需求对齐与决策件生成。
