---
doc_type: "usecase"
uc_id: "UC-00-cold-start-onboarding"
title: "格物志冷启动新手引导与常见玩法速查路书"
scenarios: "第一次冷启动、探索格物志能做什么、询问下一步做什么、13个高频场景提示词与精确指令速查"
tags: ["Onboarding", "ColdStart", "QuickStart", "Prompts", "Commands", "Discovery"]
updated: "2026-11-20"
updated_by: "researcher"
---

## 1. 适用场景与触发信号

- **适用对象**：首次打开格物志（GewuAtlas）的新用户，或对知识库当前状态感到好奇、询问「接下来可以做什么」的探索者。
- **触发词**：冷启动、第一次用、格物志能干什么、下一步做什么、有哪些功能/玩法、怎么开始、推荐几个场景、常用指令。
- **Agent 接待原则**：当用户问「下一步做什么」时，**不要甩出干瘪参数表**；应先只读探查当前库与笔记现状（`vault` + `note-list` + `project-list`），再结合进度挑选 **3~4 个最有获得感的场景**，附上可直接复制发送的「示范提示词」激发探索欲。

## 2. 格物志高频玩法速查（13 个核心场景 · 提示词与精确指令）

> 提示：工作区根统一使用 `./gewu atlas`（macOS 亦可用 `./gewu.command atlas`，Windows 用 `.\gewu.cmd atlas`）；所有数据均通过逻辑句柄（`vlt_XX` / `prj_XX` / `nte_XX`）安全托管，不暴露底层物理路径。

| # | 场景名称 | 你可以这样对 Agent 说（示范提示词） | 简明说明 | 对应精确指令（Agent 内部执行） |
| :--- | :--- | :--- | :--- | :--- |
| 1 | **库体检与探路** | 「帮我看看现在有哪些知识库、笔记和研究课题？」 | 一步盘点库句柄、已有笔记与在途课题 | `./gewu atlas --action vault` 配合 `--action note-list --ref vlt_01` |
| 2 | **新建或接管知识库** | 「帮我新建一个叫『AI 工程』的知识库，并开启 Obsidian 看板」 | 创建独立 Vault 或无损接管存量 Obsidian 库 | `./gewu atlas --action vault-create --title "AI 工程" --obsidian_base true`（接管用 `vault-import`） |
| 3 | **格物每日记** | 「帮我在今天的格物日记里记一条：复盘了 Agent Loop 状态机」 | 自动定位当天双区日记并追加 AI/人类笔记 | `./gewu atlas --action journal --ref vlt_01 --mode append --content "<记录内容>"`（查今日用 `--mode today`） |
| 4 | **文献行内批注** | 「扫描我在 `nte_XX` 里画的书签标记，生成独立批注卡」 | 捕获阅读时随手写下的行内书签并沉淀为卡片 | `./gewu atlas --action slip --ref vlt_01 --mode scan --note_ref <nte_XX>`（提交用 `--mode commit`） |
| 5 | **本地文件入库切片** | 「把 DropZone 里的 `架构白皮书.pdf` 转译成笔记并自动切片」 | 多模态解析 PDF/Word/MD 为结构化长文与章节切片 | `./gewu atlas --action ingest --ref vlt_01 --file_name "架构白皮书.pdf" --auto_slice true` |
| 6 | **网页文章批量抓源** | 「把这 3 篇官方文档链接抓进知识库，帮我自动切片」 | 自动清洗网页正文（<300 字防垃圾闸门）并生成 TOC | `./gewu atlas --action ingest --ref vlt_01 --urls "<URL1>,<URL2>" --auto_slice true` |
| 7 | **知识库循证问答** | 「根据库里的资料，对比分析不同 Agent Loop 的优缺点」 | BM25 + 双链图谱加权检索，只读实读后有据作答 | `./gewu atlas --action note-search --ref vlt_01 --query "<关键词>" --mode passage`（跨库加 `--all_vaults true`） |
| 8 | **提炼高密度原子卡** | 「从 `nte_XX` 这份资料里提炼 3 张核心概念与边界原子卡」 | 将长文蒸馏为 `OBJ/SUB/SYN/HYP` 独立命题卡片 | `./gewu atlas --action atomic-extract --ref vlt_01 --note_ref <nte_XX> --epistemic_type OBJ --title "<标题>" --summary "<断言>" --tags "<Tags>" --content "<正文>"` |
| 9 | **严选审批转正** | 「把刚才待审的候选原子卡 `nte_XX` 核对无误后转正」 | 在 `supervised` 模式下凭 `sha256` 摘要把关入正式库 | `./gewu atlas --action atomic-approve --ref vlt_01 --note_ref <nte_XX> --expected_sha256 <摘要>` |
| 10 | **自动织网与漫游** | 「把带 `Agent` 标签的卡片织成一张主题地图（MOC）」 | 自动聚合关联原子卡、生成 Mermaid 脑图并发现新簇 | `./gewu atlas --action moc-sync --ref vlt_01 --title "<主题名>" --tag "<Tag>"`（自主发现用 `--mode roam`） |
| 11 | **独立课题深度研究** | 「开一个独立课题深度调研『RAG 评测体系』，交付 3 篇专题报告」 | 建工作台隔离创作、盲审打分、采收并经确认定稿 | `./gewu atlas --action project-create --ref vlt_01 --title "<课题名>" --agent researcher` → `note-create` → `artifact-adopt` → `artifact-finalize` |
| 12 | **断链修复与体检** | 「帮我全面体检一下知识库，顺便把悬空双链修好」 | 扫描图谱孤岛、Schema 合规与断链并一键修复 | `./gewu atlas --action guard --ref vlt_01 --mode health`（修链用 `note-link --mode fix`，图谱用 `graph-report`） |
| 13 | **软删回收与后悔药** | 「刚才那张卡删错了 / 改乱了，帮我撤回到上一步」 | 回收站可逆恢复，或按 `chg_XX` 快照无损回滚改写 | `./gewu atlas --action trash --ref vlt_01 --mode restore --trash_ref <trs_XX>`（撤改用 `undo --change_id <chg_XX>`） |

## 3. 黄金体验路径与「下一步做什么」引导配方

### A. 新手 4 阶好奇心阶梯（零门槛渐进体验）

1. **第 1 步 · 30 秒破冰（看家底 + 记一笔）**：先跑 `vault` 与 `journal --mode today` 认识当前库，再用 `journal --mode append` 记下第一条灵感，立刻在 Obsidian 中看到结构化日记落盘。
2. **第 2 步 · 3 分钟喂料（抓网页 / 丢文件）**：挑 1~2 篇最想精读的技术长文或 PDF，执行 `ingest --auto_slice true`；几秒内收获清洗全文、骨架 TOC 与章节切片，立即体验 `note-search --mode passage` 精准问答。
3. **第 3 步 · 10 分钟炼金（萃取 + 织网）**：对感兴趣的长文做 `atomic-extract` 提炼 3 张原子卡，再用 `moc-sync` 一键长出带 Mermaid 脑图的主题地图，感受笔记自己「连成网」的爽感。
4. **第 4 步 · 完整研究闭环（开课题 + 多报告交付）**：直接丢出一个研究主题，让 Agent 按 `project-create → 批量入库 → 萃取织网 → 工作台多篇报告 → 60/60 验货 + 只读盲审 → artifact-adopt` 跑通一次端到端科研交付。

### B. 当用户问「下一步做什么？」时的标准引导话术模板

1. **先盘点现状（1 句事实）**：基于 `vault` / `note-list` / `project-list` 告知用户当前处于哪一阶段（例如：「当前库已有 1 份长文但尚未萃取原子卡」或「已有 3 篇报告待您确认定稿」）。
2. **再按兴趣推 3 个分支选项（可直接复制提示词）**：
   - **想继续深入当前成果**：给出衔接当前句柄的动作（如定稿 `artifact-finalize`、按 MOC 扩展建议追查新方向）；
   - **想换个轻快玩法**：推荐「记一条格物日记」「丢一篇网页/PDF 自动切片」或「对现有资料做一次问答测试」；
   - **想挑战硬核能力**：推荐「开启新课题做 3 篇深度报告」或「用 `moc-sync --mode roam` 自动发现隐藏知识簇」。
3. **复盘与沉淀提示**：若用户对输出风格或流程有新偏好，顺手提示可通过 `/atlas-usecase` 沉淀到经验卡，让下一次协作更懂自己。
