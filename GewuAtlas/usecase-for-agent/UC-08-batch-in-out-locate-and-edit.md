---
doc_type: "usecase"
uc_id: "UC-08-batch-in-out-locate-and-edit"
title: "批处理入库、出库定位与跨卡修改避坑路书"
scenarios: "批处理入库、批处理出库、纯只读batch快路径、$step_id.note_refs串联、note-grep --with_section、note-patch --ops_json与--note_refs跨卡替换、atomic-extract多源合批、沙箱热内核快速降级"
tags: ["BatchOps", "Ingest", "Locate", "NotePatch", "NoteGrep", "ReadOnlyBatch", "Pitfalls", "Performance"]
updated: "2026-11-22"
updated_by: "Antigravity"
---

## 1. 适用场景与触发信号

- 需要高吞吐完成**批处理入库**（批量 `note-create` 互链草稿、多源 `atomic-extract` 合批萃取、跨卡 `note-patch --note_refs` 断链净化、`note-patch` 精修 + `artifact-adopt` 一站式采收）或**批处理出库/定位精读**（纯只读 `batch` 快路径、一屏全景 + `note-grep --with_section` + `$g.note_refs` 串联大纲提取）。
- 触发词：批处理入库、批处理出库、只读合批、batch、--json-stdin、note-patch、ops_json、--note_refs、--with_section、note-grep、热内核、沙箱降级、避坑指南。

## 2. 高频损耗 → 直通对策（实战避坑指南）

| 损耗点 | 常见踩坑表现 | 下次直通做法 |
| :--- | :--- | :--- |
| **盲猜新算子参数或翻源码** | 未查模板直接拼 `batch` / `note-patch` 参数反复报错，或花数十步翻看脚本源码核对字段 | 直接一次查清模板：`./gewu atlas --action batch --template` 与 `--action note-patch --template`（已内置完整可抄写 JSON 示例），严禁翻源码 |
| **`note-patch --ops_json` 漏写 `"op"` 键** | 传入 `[{"section":"## 结论","content":"..."}]` 触发 `E_PARAM_INVALID` 或 `E_BATCH_PARTIAL_FAILED` | `--ops_json` 每项**必须**显式包含 `"op"` 字段（枚举仅限 `replace` / `section` / `append` / `prepend`）；若 `--section` 或 `--old_text` 未命中，报错已直接附带当前可用标题大纲，无需多跑一轮 `note-get` |
| **沙箱禁 `AF_UNIX` 时死磕热内核** | 受限沙箱调用 `daemon start` 报 `E_DAEMON_SANDBOX_RESTRICTED`（现已 `<0.2s` 快速失败）仍重试 | **遇 `E_DAEMON_SANDBOX_RESTRICTED` 零重试**，常规命令会自动无感降级直跑；多步读/写改用原生 `batch` 合批（实测 21 步写仅 7s）即可拿满核心收益；非受限终端再开 `daemon start --preload-vault <vlt_XX>` 叠加提速 |
| **把外部网络/重型 IO 塞进 `batch`** | 将 `ingest` / `fmt` / `doc-sync` 放进 `batch` 被白名单拒绝 | 外部抓源、文档转换与 Git 同步需独立复用调用方实时网络/代理环境，必须在 `batch` 之外执行（且天然旁路热内核） |
| **只读探查与写步骤混在同一个 `batch`** | 探查步骤附带写操作导致触发独占锁、DOC 票、快照与末尾验货，白交写事务税 | **读写分批**：全只读步骤（`project-brief / note-grep / note-search / note-list / note-get / health-check / schema-check / vfs-links` 及 `note-patch --dry_run true`）单独打包进只读 `batch`，自动走**共享锁 + 0 DOC 票 + 0 快照 + 0 验货**快路径（冷启 4 步实测仅 `0.56s`） |
| **`batch` 内嵌 `atomic-extract` 混参** | 同一步同时传 `provenance` 与 `slice_refs`/`toc_refs` 触发 `E_PARAM_INVALID` | 多源萃取只传 `note_ref` + `provenance`（原生 JSON 数组或 JSON 字符串均可，支持多源对象数组）；单次 `batch` 最多 **50 步**，超量按 30~50 步切分 |
| **批量清断链仍“全量读 + 整篇 `note-update`”** | 20 篇笔记改同一类悬空外链，传输几万字全文且极易破坏受控块或误改只读区 | 先用 `note-patch --note_refs nte_01,nte_02 --old_text "<旧>" --new_text "<新>" --replace_all true --dry_run true` 零落盘预演，确认命中/跳过数后去掉 `--dry_run` 一次性落盘（未命中自动跳过，逐卡强制保护 `21-Library` 只读、受控块与日记人类区） |
| **误判 `note-search` 默认出库粒度** | 以为 `slice` / `library` / `toc` 也会返整节，遗漏上下文 | 默认 `--mode passage` 规则：`atomic`/`slip` 返**全文**（上限 4000 字），`moc`/`artifact` 返**完整章节**（上限 2000 字），`slice`/`library`/`toc` 返**单个段落**（上限 1200 字）；需直接取长文整节用 `note-grep --with_section true` 或 `note-get --outline true` + `--start_line/--end_line` |

## 3. 批处理出库与入库黄金流水线（可直接抄写）

### A. 批处理出库 / 定位精读（纯只读 `batch` 快路径：1 步完成全景 + 章节命中 + 大纲串联）

```bash
cat <<'EOF' | ./gewu atlas --action batch --ref vlt_01 --project_ref prj_01 --json-stdin
{
  "steps": [
    {"id": "b",  "action": "project-brief", "args": {"project_ref": "prj_01"}},
    {"id": "g1", "action": "note-grep",     "args": {"project_ref": "prj_01", "pattern": "核心关键词", "with_section": true, "max_matches": 15}},
    {"id": "o",  "action": "note-get",      "args": {"note_refs": "$g1.note_refs", "outline": true}}
  ]
}
EOF
```

- 说明：`note-grep` / `note-search` / `note-list` 自动向后续步骤导出 `$step_id.note_refs`（逗号分隔去重句柄）与 `$step_id.top_note_ref`；全只读步骤自动走共享锁、零 DOC 票、零快照快路径。

### B. 批处理入库 / 多卡创作、萃取、清洗与采收（写事务 `batch`：单锁单票单快照）

1. **外部文献归口（在 `batch` 外独立执行）**：`./gewu atlas --action ingest --ref vlt_01 --project_ref prj_01 --urls "<URL1>,<URL2>" --auto_slice true`（每批 ≤ 5 条，避开反爬/403 站点）。
2. **多源原子萃取合批（1 次 `batch`）**：将 3~8 张 `atomic-extract` 打包进 `batch --json-stdin`，多源仅传 `provenance: [{"source_ref":"nte_XX","toc_ref":"nte_YY","slice_refs":["nte_ZZ"]}, ...]`。
3. **互链草稿批量创作（1 次 `batch` + `$s1.note_ref` 串联）**：

```bash
cat <<'EOF' | ./gewu atlas --action batch --ref vlt_01 --project_ref prj_01 --json-stdin
{
  "steps": [
    {"id": "m", "action": "note-create", "args": {"project_ref": "prj_01", "zone": "workbench", "subdir": "ws-agent/drafts", "title": "主稿", "summary": "摘要", "content": "## 1. 背景\n...\n## 2. 结论\n初版"}},
    {"id": "a", "action": "note-create", "args": {"project_ref": "prj_01", "zone": "workbench", "subdir": "ws-agent/drafts", "title": "关联卡A", "summary": "摘要", "content": "引用 [[$m.note_ref]]"}}
  ]
}
EOF
```

4. **跨卡统一清洗（先预演再落盘）**：`./gewu atlas --action note-patch --ref vlt_01 --note_refs nte_01,nte_02 --old_text "<旧片段>" --new_text "<新片段>" --replace_all true --dry_run true` 核对无误后去掉 `--dry_run` 执行。
5. **精修 + 采收一站式收官（1 次 `batch`）**：将 `note-patch`（`ops_json` 必填 `"op"`，自动保护受控块）与 `artifact-adopt` 串入同一 `batch`；末尾核验 `./gewu atlas --action guard --ref vlt_01 --project_ref prj_01 --mode delivery` 拿到 `60/60 PASS`；若开工拉起过热内核则执行 `./gewu atlas daemon stop` 收工。
