---
doc_type: "usecase"
uc_id: "UC-01-web-ingest-and-extract"
title: "联网抓源、切片与原子萃取避坑路书"
scenarios: "网页/URL抓取入库、ingest --auto_slice、fmt 运行时预检、atomic-extract 句柄类型防错"
tags: ["Ingest", "URL", "Slice", "Extract", "Preflight"]
updated: "2026-11-19"
updated_by: "researcher"
---

# 联网抓源、切片与原子萃取避坑路书

## 1. 适用场景与触发信号

- 收到外部网页/PDF/URL 批量入库、AST 切片（`ingest --auto_slice`）与 `atomic-extract` 原子萃取任务时必读。
- 触发词：抓源、入库、切片、萃取、笔记搬运、官方文档站、多篇文献沉淀。

## 2. 高频损耗 → 直通对策

| 损耗点 | 上次表现 | 下次直通做法 |
| :--- | :--- | :--- |
| **单批 URL 过多超时** | 6+ 个 URL 一批易超出门面 120s 阈值被转入后台 | 单批 `--urls` 严格控制在 **≤ 5 条以内**，配合 `--auto_slice true` 一步完成转译与切片 |
| **无效/反爬源白占槽位** | npm 页面报 403、Investopedia / MBA 智库返回反爬空壳 | 投递前预筛：**开源包优先改用 GitHub raw README URL**，官方文档站优先；遇 `E_URL_NETWORK` 立即跳过不重试 |
| **萃取句柄类型混填** | 把 `library` 原文句柄混进 `--slice_refs` 触发 `E_PARAM_INVALID`（一次连废 4 张卡） | 先核对句柄类型：`--note_ref` 填 TOC 或 Library 句柄，**`--slice_refs` 仅填切片句柄**；`note-list` 输出里看 `doc_type`，不要凭 `nte_` 前缀猜 |
| **萃取 `--note_ref` 实为必填** | 字典把 `--note_ref` 标为可选 `[--note_ref]`，仅带 `--slice_refs` 调用被 `E_PARAM_MISSING` 拦截并消耗熔断重试额度（剩 1 次） | `atomic-extract` 一律显式带 `--note_ref`（TOC 或 Library 句柄）；跨源综合卡的溯源交给 `--slice_refs` 承担，`--note_ref` 任选一个主要来源 TOC 即可，不要裸调 |
| **原子卡求多卡壳** | 连续萃取第 4+ 张细碎卡片触发熔断或锁死 | 每个核心主题精选 **3~5 张高密度原子卡**（按 OBJ/SYN/HYP 分层）；遇局部阻断即转由报告正文承载，保持主流程不停 |
| **运行时依赖缺失** | 首个 `ingest` 直接报 `E_RUNTIME_DEPENDENCY`（`.runtime/fmt-venv` 未就绪），门禁重试额度仅剩 1 次 | 预检阶段先跑 `{cmd:atlas} --action fmt --setup`（幂等，已装则秒回 `installed`），**再发首个 ingest**；勿把唯一重试额度浪费在依赖上 |
| **占位/测试卡污染生产库** | 为试参数建了"测试标题"卡，事后 `trash` 报 `E_VFS_REFERENCED`（被 TOC 与目录页回链锁死），只能 `note-deprecate` 留在 attic | 校验参数一律用 `--template` 或只读 action；**绝不在生产库里建测试卡**，如已建则直接 `note-deprecate`（勿纠缠 `trash`） |
| **文档站入口靠猜 URL** | `sitemap.xml` 返回 link dead，逐页试错 | 直接取目录页 `reference/subsystems/`（或同级 index），拿到全部子系统清单后按命名规则拼子页 URL |
| **批间 Git 提交冲突** | 每批 ingest 结束都会触发一次 DOC Git 提交 | 批与批**严格串行**；单批用 `run_in_background` 提交后 `TaskOutput` 等结果，不要在前台空耗 120s |

## 3. 黄金流水线与高分交付配方

1. **并行预检三连**：`vault` 取库句柄、`capabilities` 核能力、`fmt --setup` 补运行时（三者互不依赖，可并行）。
2. **分批快摄**：`./gewu atlas --action ingest --ref <vlt_01> [--project_ref <prj_01>] --urls "<URL1>,<URL2>" --auto_slice true`（每批 ≤ 5 条，串行执行）。
3. **证据映射先于萃取**：`note-get --note_refs nte_01,nte_02,...` 一次批量读切片，建好 `claim -> slice_ref` 映射再调 `atomic-extract`。
4. **ref 类型自检**：萃取前把候选句柄过一次 `note-list`，按 `doc_type` 分流；`note_ref` 走 TOC/Library，`slice_refs` 只放切片。
5. **原子卡分层**：官方机制事实落 `OBJ`，跨源综合落 `SYN`，可验证的判断落 `HYP`，每卡固定「断言 → 依据 → 边界」三段。
6. **熔断即停手**：同一个错连败 2 次（`attempt_remaining` 归零）立即停下汇报现状，不换写法重试。
