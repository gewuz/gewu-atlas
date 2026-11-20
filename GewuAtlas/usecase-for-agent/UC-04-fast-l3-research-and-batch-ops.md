---
doc_type: "usecase"
uc_id: "UC-04-fast-l3-research-and-batch-ops"
title: "L3 深度研究极速流水线与批量防返工路书"
scenarios: "自主深度研究提速、双任务复盘、正文去标签与文末集中溯源、python3 Heredoc 防错、多卡批量萃取、MOC 索引保留、一次过 95+ 盲审"
tags: ["Performance", "BatchOps", "Research", "LaTeX", "BlindReview", "Optimization", "Heredoc", "Readability"]
updated: "2026-11-20"
updated_by: "researcher"
---

## 1. 适用场景与触发信号

- 接手 L3 复合深度调研（多批 URL 摄取 + 原子萃取 + MOC 织网 + 多篇正交系列报告撰写 + 独立盲审与采收交付），融合 `prj_01`（DeepSeek Harness Memory，205+ 切片重负载对比基线）与 `prj_02`（Agent Harness Loop，87 切片精简极速流水线，首审 96/100 PASS）两次实战复盘，追求**低回合数、零格式返工、一次过 95+ 分盲审**时必读。
- 触发词：自主深度研究、提速优化、多篇调研报告、批量萃取、减少往返耗时、LaTeX 与 heredoc 防错、盲审一次过、双任务复盘。

## 2. 高频损耗 → 直通对策（两次任务对照沉淀）

| 损耗点 | 上次表现（实测对照） | 下次直通做法 |
| :--- | :--- | :--- |
| **逐条单步调写算子拉爆回合数** | 任务 1 初期拆成 12+ 个独立模型回合，仅模型思考与往返耗掉 **786s（71 回合）**；任务 2 改用单脚本串行合批后全流程压至约 **15 活跃回合** | **单脚本串行合批**：把多卡 `atomic-extract`、MOC 富化 + 3 篇 `note-create` + 四项硬检、以及「盲审微修 + 3 次 `artifact-adopt` + 终审 `guard`」分别封装为 1 个 `python3 - << 'PY'` 脚本串行执行——既守 DOC Git 串行不冲突，又将回合数砍半以上 |
| **单课题源过多撑爆活跃切片数** | 任务 1 摄入 18 篇源（含 10 万字 HTML）拆出 **205+ 切片**，拖慢每次 `delivery_gate` 扫盘与 Git 提交；任务 2 精选 **8 篇高密度一手源（87 切片）** 全程丝滑 | **严控单课题有效源 6~10 篇、活跃切片 < 100 片**：优先直取官方文档与 GitHub raw Markdown，避免整本超长网页灌水；arXiv 直抓 `/html/<id>` 不抓 `/abs/<id>` |
| **Bash `$'...'` 毁坏 LaTeX 与 `python3 -c` 引号报错** | Bash `$'...'` 把 `\t`/`\r`/`\a` 转为控制字符致盲审 `FAIL`（返工 ~145s）；macOS 系统 Python (< 3.12) 在 `python3 -c` f-string 内遇转义引号报 `SyntaxError` | **统一采用 `python3 - << 'PY' ... PY` 单引号 Heredoc + 原始字符串 `r\"\"\"...\"\"\"`**：彻底杜绝 shell `$` 展开、反斜杠转义与控制字符污染，并在脚本内自动校验 `ord(c) < 32` 为 0 与 `$` 成对 |
| **多源 `provenance` 混参或切片名含逗号报错** | 任务 2 萃取时先后触发「`--provenance` 与 `--slice_refs` 不可混用」及「切片 `note_name` 含 `,` 致派生不一致」，白耗 2 轮试错 | 跨源只传 `--note_ref` + `--provenance`；萃取前用脚本对候选切片执行 `"," in note_name` 预筛，含逗号切片不进 `provenance.slice_refs`（仅在正文引用），萃取后顺手跑 `note-link --mode fix` |
| **MOC 自动索引标记名凭记忆猜错** | 误以为标记是 `<!-- moc-index:begin -->`，实测算子输出为 `<!-- atlas:moc-index:start -->` / `<!-- atlas:moc-index:end -->` | 回填 MOC 综述前先 `note-get` 实读原文，**严格按 `<!-- atlas:moc-index:start -->` 至 `<!-- atlas:moc-index:end -->` 截取保留自动索引块**，在其上下填充宏观综述与 `## 逻辑闭环评估与扩展研究建议` |
| **跨源伪断链拖低 `delivery_gate` 得分** | 任务 1 遇 arXiv `[[v1]]` 伪双链；任务 2 遇 GitHub README 相对图片 `.github/images/logo-dark.svg` 与萃取短句柄双链，致闸门跌至 `35~45/60` | 写报告前先跑 `guard --mode links` 清零全库断链：短句柄用 `note-link --mode fix` 批修，相对图片路径与伪双链直接用 `note-update` 替换为完整外链，保证草稿期即达 `60/60` |
| **成果正文堆砌行内引用与 `[客观]` 类标签损伤可读性** | 任务 2 报告正文穿插 `【官方事实】`/`【综合推断】` 与密集 `（nte_XX）`，既破坏视觉流畅又导致语言风格为凑标注变形，且易引发邻近切片标偏 | **成果文档正文严禁行内句柄与 `[客观]`/`【官方事实】` 等机械标签**：以自然流畅的同行专家语言交代归属（如「Anthropic 实测表明……」「基于上述机制可推算……」），全部 `nte_XX` 精确句柄与依据对照统一沉底至文末 `## 参考来源与知识库索引`（动笔前仍须建精确映射表核实真值） |
| **盲审后精修与采收拆成多轮往返** | 若盲审签发 `PASS` 但附带 3~4 处非阻断精修建议，分步改草稿、再采收、再跑 `guard` 易增 3+ 回合 | **精修 + 采收 + 终审三合一脚本**：收到盲审 `PASS` 后，在单个 `python3 - << 'PY'` 脚本内顺序完成「patch 精修草稿 → 串行 `artifact-adopt` 全部报告 → 最终 `guard --mode delivery` 复核 `60/60`」一次收官 |
| **盲审等待期多余轮询与 `GUARD: HALTED` 处置** | 任务 1 设多轮 60s 定时器白耗 179s；任务 2 第 5 张萃取触发 `GUARD: HALTED`（`E_HALT_INTERNAL`） | 异步后台任务与只读 Subagent 均**零轮询、直接交出控制权等 Reactive Wakeup**；遇 `GUARD: HALTED` 严禁重试，立即汇报进度并给出「Plan A（现有原子卡织网 + 报告承接剩余论点）vs Plan B（停等排查）」由人类秒拍板 |

## 3. 黄金流水线与极速高分配方（经两次任务实测：~15 活跃回合、一次过 95+ 分）

1. **并行侦察 + 建课题（1~2 回合）**：同步执行 `project-create`、`fmt --setup` 与 3~4 路 `search_web` 拿准权威文档与 GitHub 真实分支名（避开 403 站点与 `arxiv.org/abs/`）。
2. **精简两批快摄（2 回合）**：每批 ≤ 5 条高质量直连 URL 串行 `ingest --auto_slice true`，单课题锁定 6~10 份一手源、活跃切片 < 100 片。
3. **Python Heredoc 紧凑管道一次读完证据（1~2 回合）**：用 `python3 - << 'PY'` 批量 `note-get` 截断读正文并输出 `note_name`（同时筛查逗号切片），固化「精确事实 → 精确切片句柄」映射表。
4. **单脚本串行萃取 + 链检清零（1~2 回合）**：单个 `python3 - << 'PY'` 顺序萃取 3~5 张高密度原子卡（多源仅用 `--provenance`），紧接 `note-link --mode fix` + `guard --mode links` 扫清相对图片伪断链，确保立即站稳 `60/60`；若遇 `GUARD: HALTED` 立刻停手呈报 Plan A/B。
5. **MOC 织网与综述回填合步（1 回合）**：`moc-sync` 产出后实读保留 `<!-- atlas:moc-index:start --> ... <!-- atlas:moc-index:end -->`，同脚本 `note-update` 回填顶部宏观综述与底部闭环评估及扩展建议。
6. **单脚本批量写三篇正交报告 + 前置四项硬检（1 回合）**：在 `ws-<agent>/drafts` 按「机制原理篇 / 全景对比篇 / 决策与边界篇」合批写入（正文零行内句柄与机械标签、安全双引号 Mermaid、2 组可复算案例、独立边界小节、文末集中溯源索引），同脚本自动完成「① 控制字符=0 与 `$`/代码围栏闭合；② 全量 `nte_XX` 批量存在性校验；③ 物理路径正则=0；④ `guard --mode delivery` 60/60」。
7. **首轮盲审即过 + 精修采收终审三合一（2 回合）**：带着四项全绿回执与精确证据索引唤起独立只读 `research` Subagent 盲审（零轮询等 Reactive Wakeup）一次拿 95+ `PASS`；随后用单个 Python 脚本完成「盲审可选微修 → 串行 `artifact-adopt` → 最终 `guard --mode delivery` 60/60」，向人类交付精炼回执并等待 `artifact-finalize` 定稿确认。
