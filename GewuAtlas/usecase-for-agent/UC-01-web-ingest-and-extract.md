---
doc_type: "usecase"
uc_id: "UC-01-web-ingest-and-extract"
title: "联网抓源、切片与原子萃取避坑路书"
scenarios: "网页/URL抓取入库、ingest --auto_slice、fmt 运行时预检、atomic-extract 多源 provenance 与逗号切片防错、note-link 批修与相对图片断链清理"
tags: ["Ingest", "URL", "Slice", "Extract", "Preflight", "Provenance", "LinkFix"]
updated: "2026-11-22"
updated_by: "Codex"
---

## 1. 适用场景与触发信号

- 收到外部网页/PDF/URL 批量入库、AST 切片（`ingest --auto_slice`）与 `atomic-extract` 原子萃取任务时必读。
- 触发词：抓源、入库、切片、萃取、多源 provenance、双链修复、笔记搬运、官方文档站、多篇文献沉淀。

## 2. 高频损耗 → 直通对策

| 损耗点 | 上次表现 | 下次直通做法 |
| :--- | :--- | :--- |
| **单批 URL 过多超时** | 6+ 个 URL 一批易超出门面 120s 阈值被转入后台 | 单批 `--urls` 严格控制在 **≤ 5 条以内**，配合 `--auto_slice true` 一步完成转译与切片 |
| **无效/反爬源白占槽位** | npm、OpenAI 官网博客（403 Cloudflare）、Investopedia 返回反爬或 404 | 投递前预筛：**开源项目优先改用 GitHub raw README / `docs/*.md` URL**，官方文档站优先；遇 `E_URL_NETWORK` 立即跳过不重试 |
| **萃取句柄类型混填** | 把 `library` 原文句柄混进 `--slice_refs` 触发 `E_PARAM_INVALID`（一次连废 4 张卡） | 先核对句柄类型：`--note_ref` 填 TOC 或 Library 句柄，**`--slice_refs` 仅填切片句柄**；从 `note-list` 看 `doc_type`，不要凭 `nte_` 前缀猜 |
| **多源萃取 `--provenance` 与 `--slice_refs` 混用** | 跨源萃取同时传 `--provenance` 与 `--slice_refs`/`--toc_refs`，触发 `E_PARAM_INVALID: 不可混用` | 跨源原子卡一律**只传 `--note_ref <主TOC/原文>` + `--provenance '[{"source_ref":"nte_XX","toc_ref":"nte_YY","slice_refs":["nte_ZZ"]}]'`**，严禁再叠加 `--slice_refs` 或 `--toc_refs` |
| **切片 `note_name` 含英文逗号导致 `provenance` 失配** | 标题带 `,` 的切片（如 `Many_brains,_many_hands`、`The_spine,_package_by_package`）在 `provenance` 逗号序列化时被劈成两段，报 `slices 与 provenance 派生结果不一致` | 萃取前用脚本扫描候选切片 `"," in note_name`：**含英文逗号的切片移出 `provenance.slice_refs`**（保留同源 `source_ref`/`toc_ref` 及其他无逗号切片），正文照常引用该切片句柄即可 |
| **萃取正文写 `[[nte_XX\|短名]]` 遗留悬空双链** | `atomic-extract` 不自动展开正文短句柄双链，导致 `guard --mode delivery` 因悬空链跌至 `35~45/60` | 批量萃取后立刻跑 1 步 `./gewu.command atlas --action note-link --ref <vlt_XX> --mode fix --project_ref <prj_XX>` 一次性批修全部短句柄双链，或在正文改用行内 `（nte_XX）` |
| **GitHub README 相对图片路径触发断链扣分** | 入库的 `README.md` 含 `<img src=".github/images/logo-dark.svg">`，在 `library` 与 `_概述` 切片各产生 1 条悬空链导致闸门扣分 | 用 `guard --mode links` 定位相对 `.svg`/`.png` 目标，通过 `note-update` 直接替换为 `https://raw.githubusercontent.com/<org>/<repo>/<branch>/...` 完整外链，一步回满 `60/60` |
| **萃取 `--note_ref` 实为必填** | 仅带 `--slice_refs` 裸调被 `E_PARAM_MISSING` 拦截并消耗熔断重试额度 | `atomic-extract` 一律显式带 `--note_ref`（TOC 或 Library 句柄），跨源溯源交给 `--provenance` 承担 |
| **原子卡求多或遇 `GUARD: HALTED` 硬拦截** | 连续萃取第 4~5+ 张卡片时可能触发限流、熔断或 `E_HALT_INTERNAL` 内部拦截 | 每个核心主题精选 **3~5 张高密度原子卡**；一旦遇 `GUARD: HALTED` **立即停手零重试**，汇报现状并提供「方案 A：基于已落盘原子卡推进 MOC 与报告承载剩余论点」请人类拍板 |
| **运行时依赖缺失** | 首个 `ingest` 直接报 `E_RUNTIME_DEPENDENCY`（`.runtime/fmt-venv` 未就绪） | 预检阶段并行跑 `./gewu.command atlas --action fmt --setup`（幂等秒回），**再发首个 ingest** |
| **占位/测试卡污染生产库** | 为试参数建了测试卡，事后 `trash` 报 `E_VFS_REFERENCED` 被回链锁死 | 校验参数一律用 `--action <act> --template` 或只读 action；**绝不在生产库建测试卡** |
| **批间 Git 提交冲突** | 每批 `ingest` 结束均触发 DOC Git 提交 | 批与批**严格串行**；后台等待时依赖系统 Reactive Wakeup，不手工轮询 |

## 3. 黄金流水线与高分交付配方

1. **并行预检三连**：`vault` 取库句柄、`capabilities` 核能力、`fmt --setup` 补运行时（三者只读/幂等，可并行）。
2. **分批快摄**：`./gewu.command atlas --action ingest --ref <vlt_01> [--project_ref <prj_01>] --urls "<URL1>,<URL2>" --auto_slice true`（每批 ≤ 5 条，串行执行，总有效源控制在 6~10 篇、活跃切片 < 100 片）。
3. **证据映射与逗号预检先于萃取**：用 `python3 - << 'PY'` 紧凑管道批量 `note-get` 实读切片，建立 `精确事实 -> 精确切片句柄` 映射表，并同步筛出 `"," in note_name` 的切片以防 `provenance` 失配。
4. **单脚本串行萃取 + 一步修链**：多源卡传 `--note_ref` + `--provenance`（禁混 `--slice_refs`）；萃取完成后紧接运行 `note-link --mode fix --project_ref <prj_XX>` 与 `guard --mode links` 扫清相对图片伪断链，确保进入 MOC 前已稳居 `60/60`。
5. **原子卡分层与熔断边界**：事实或规范落 `OBJ`，经验取舍落 `SUB`，跨源推导落 `SYN`，待证解释落 `HYP`。一卡一主张，写清依据、推理和适用边界，段落按材料组织；同错连败 2 次或遇 `GUARD: HALTED` 立即停手汇报，不盲试。
