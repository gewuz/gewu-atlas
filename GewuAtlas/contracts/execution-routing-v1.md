# 契约：Atlas 独立执行与维护路由（gewu.execution-routing/1 · ws-5 围墙与验货版）

本域不集成 GewuTrack，不提供 Track 任务创建、阶段调度或 prepare/invoke/receipt 业务适配器。Forge 自身的 Track 不受影响。

## 1. 业务执行与分级交付闸门（L1 / L2 / L3）

日常与多步业务均通过 `./gewu atlas --action ...` 执行；Agent 在受控空间（`20-AI-Zone` 与 `30-Projects/prj_XX` 的 `ws-<agent>` 工作台）内可自主组合算子，不绑死中间步骤顺序，由算子出口胶囊（Capsule）顺风车反馈与分级验货闸门保障交付质量：

- **L1 普通单步任务**（记日记、单卡属性微调、回收站操作、列目录）：写算子内置 Schema 与路径围墙校验，返回 `OK_*` 即代表合规，零额外评估直接交付。
- **L2 局部轻任务**（多卡检索问答、单篇入库切片、单卡创作）：由 `note-search`（`[QA_GATE: SNIPPET_ONLY]`）与 `note-get`（`[QA_GATE: GROUNDED]`）胶囊引导实读全文，输出前按通用 3 问（完整性、真实性、可读性）脑内自检即可交付。
- **L3 重度复合任务**（开课题多源调研、批量入库+萃取+织网、跨区整理）：写算子出口自动顺风车挂载 `delivery_gate` 状态，收尾必须调用 `./gewu atlas --action guard --ref <vlt_XX> --mode delivery` 完成 **60 分算子硬评估**（空间净空 15 + Schema 合规 15 + 溯源与双链真值 15 + 审批边界 15），再执行 **40 分语义质量评估**（若 Agent 具备 Subagent 调用能力则优先唤起独立只读 Subagent 盲审，无 Subagent 能力则降级为结构化自我评估），分别记录硬评和语义 PASS/FAIL，两项均通过方可交付；漏答核心问题、关键结论无依据、拼贴检索碎片或泄露物理路径均阻断交付。

## 2. 系统维护

通过 SYS 工单锁定起点提交 → 本域 manage 批量维护 → 按影响范围测试 → 完成提交 → 结单。保留真实授权、差异与测试证据。普通 manage 不是事务；未知或部分成功先查公开文件与回执，不盲目重投。不另建长期任务状态机。

## 3. 停止与边界

遇 `GUARD: INPUT_REQUIRED` 或 `GUARD: HALTED` 停止相关调用并报告。版本冲突重新核对实际内容，不沿用旧审批。历史 Track 任务和运行态不删除、不自动迁移或继续派发。启动、分发、独立安装与发布测试无需 Track 文件、版本或摘要。
