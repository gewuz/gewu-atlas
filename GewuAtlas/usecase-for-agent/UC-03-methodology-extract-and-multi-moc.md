---
doc_type: "usecase"
uc_id: "UC-03-methodology-extract-and-multi-moc"
title: "方法论萃取与多 MOC 自述漫游路书"
scenarios: "已入库资料提炼方法论、多主题 MOC 生成、多源 provenance 精准溯源、漫游自述与扩展建议回填"
tags: ["Methodology", "MOC", "Roam", "Extract", "Governance", "Reflection", "Provenance"]
updated: "2026-11-20"
updated_by: "researcher"
---

## 1. 适用场景与触发信号

- 已有入库资料（Library/Slices）需要提炼方法论，并按主题生成多张 MOC、补写漫游自述与扩展建议时必读。
- 触发词：方法论萃取、主题地图、多个 MOC、漫游建图、知识地图、自述路径、逻辑闭环评估。

## 2. 高频损耗 → 直通对策

| 损耗点 | 上次表现 | 下次直通做法 |
| :--- | :--- | :--- |
| **切片 provenance 校验误伤** | 传 `--slice_refs` 或多源 `--provenance` 时报 `E_PARAM_INVALID: slices 与 provenance 派生结果不一致`，早期曾被迫一刀切退回「一律单源不带 slice」 | **已查明两大真实诱因并直通解决**：① 多源卡只能传 `--note_ref` + `--provenance '[...]'`，**禁混传 `--slice_refs`/`--toc_refs`**；② `note_name` **含英文逗号 `,`** 的切片会在逗号序列化时被劈断，萃取前用脚本扫描 `"," in note_name` 移出 `slice_refs`（仍可在正文或文末引用），其余无逗号切片均可正常完成精准切片溯源（详见 `UC-01`） |
| **想预演却无 dry_run** | `atomic-extract --dry_run true` 直接报「此动作不支持 `--dry_run`」 | 参数合法性只查 `--template`；非要试写就用 30 字探针内容，事后 `note-deprecate --move_to_attic true`（`trash` 会被 TOC 回链阻断） |
| **`doc_type` 凭直觉填错** | `note-create --doc_type note` 报「未知 `doc_type`: note」 | `workbench` 区固定 `--doc_type workbench`；`artifact-adopt` 成功后自动转 `artifact`；填之前核对 `metadata.yaml` |
| **MOC 新建缺主题名** | `moc-sync` 报 `E_PARAM_MISSING`，要求显式 `--title/--name/--topic` | 新建主题图必须传 `--title`；只有刷新已有图才可省略 |
| **`roam` 聚类过于粗糙** | 多张方法卡因共享基础标签「萃取」被聚成一个大簇，`suggested_tag` 信息量低 | `roam` 只作发现参考；多 MOC 用**人工策划的交叉标签**（如 Modeling/Governance/Delivery），先 `--dry_run true` 核对 `matched_count` 与 `included_refs` 再逐张写图 |
| **小簇撑不起一张图** | 子簇初始只有 2 张卡 | 用 `note-update --tags` 补 1~2 个交叉标签（保留基础标签）；每张 MOC 纳入 **3~5 张**高密度原子卡为宜 |
| **MOC 占位符残留** | 自动图底部留待补充占位句，或整篇重写弄丢自动索引 | 生成后 `note-get` 实读原文，**保留 `<!-- atlas:moc-index:start --> ... <!-- atlas:moc-index:end -->` 与 Mermaid 脑图**，仅在其上下回填综述、漫游自述与 2~4 条扩展建议 |
| **想交文件但围墙禁止** | 根目录不能建 `reports/` | 报告走 `note-create(workbench) → artifact-adopt`，交付用 `nte_XX` 句柄；禁止往根目录导出双份文件 |
| **同库并发写撞锁与后台等待** | 同库并发调多个写算子撞上目录级非阻塞协作锁（`IPC-LOCK`）或 `DOC` Git 索引锁，且把「串行避开 Git 冲突」等底层术语报给用户 | **单脚本循环串行写**：把多卡萃取与多图同步写进单个 `python3 - << 'PY'` 脚本 `for` 循环顺序执行（1 回合完事、零锁竞争）；异步等待直接依赖系统 Reactive Wakeup；向人类汇报进度只讲业务阶段，不暴露锁/Git 等内部实现术语 |

## 3. 黄金流水线与高分交付配方

1. **预检三连**：`vault` 取库、`fmt --setup` 补运行时、`project-list` 查重；再 `project-create` 开课题并拿 `ws-<agent>/drafts`。
2. **入库按 `UC-01`**：单批 URL ≤ 5 条、串行、`--auto_slice true`；单课题控制在 6~10 份一手源、活跃切片 < 100 片。
3. **证据地图与逗号预检先于萃取**：`note-get --note_refs` 批量实读切片，建立 `claim -> 精确切片句柄` 映射，并同步筛出 `"," in note_name` 的切片。
4. **方法卡精选分层**：每个主题精选 **3~5 张**高密度卡（按 `OBJ/SYN/HYP` 分层，每卡「断言 → 依据 → 边界」），不硬凑数量；跨源综合卡用 `--note_ref` + `--provenance`（排除含逗号切片、不混传 `--slice_refs`），单源卡传 `--note_ref` + 无逗号 `--slice_refs`。
5. **单脚本串行写 + 修链**：在 1 个 `python3` 脚本内串行完成萃取，紧接跑 `note-link --mode fix` 清零正文短句柄断链。
6. **多 MOC 两步走与自述回填**：先 `moc-sync --title <主题> --tag <标签> --dry_run true` 验证簇，再正式写图；逐图保留自动索引块并回填宏观综述、逻辑闭环评估与 2~4 条扩展研究建议。
7. **验货两段与纯净正文**：`guard --mode delivery --project_ref <prj>` 拿 `60/60`；报告正文保持流畅人类散文（零 `[客观]` 标签与行内 `nte_XX`，溯源沉底文末），经只读 Subagent 盲审（40 分）过线后 `artifact-adopt`。

## 4. 复盘心得（2026-11-19 记录 · 2026-11-20 校准）

### 4.1 接口契约先行

- 任何写操作前先查 `--template` 与枚举值（如 `doc_type`、`--dry_run` 支持情况），不靠自然语言直觉猜参数。

### 4.2 精准溯源校准（2026-11-20 修正）

- 2026-11-19 复盘曾因未定位 `slices 与 provenance 派生结果不一致` 根因而建议「一律退回单源不传 `slice_refs`」；经 2026-11-20 实测已查明根因为「混传 `--provenance`/`--slice_refs`」与「切片 `note_name` 含英文逗号」，现已恢复**多源 `--provenance` 精准切片溯源**为首选规范（见第 2、3 节）。

### 4.3 试错有成本上限

- 只读优先、不在生产库建测试卡；同错连败 2 次或遇 `GUARD: HALTED` 立即停手汇报。

### 4.4 漫游是雷达，不是地图

- `roam` 用于发现潜在聚簇；正式多 MOC 应围绕问题与逻辑跳线设计交叉标签，并补充自述路径、闭环判断与可执行缺口。

### 4.5 下次开工检查清单

1. 接口侦察：`--template`、枚举值、`--dry_run` 支持情况。
2. 萃取前扫 `"," in note_name`，跨源用纯 `--provenance`，每个主题精选 3~5 张，写后顺手 `note-link --mode fix`。
3. 同一库写操作统一收拢在单个 `python3` 脚本内循环串行（遵循 `IPC-LOCK` 非阻塞锁约束），对外播报不泄露内部锁/Git 术语。
4. 多 MOC 先 `--dry_run` 核对 `matched_count` 与 `included_refs`，正式写入后保留自动索引块并回填闭环评估。
5. 验货分两段：`guard --mode delivery` 60 分是底线，只读盲审 / 自评 40 分决定内容与可读性质量。
