# Markdown：确定性结构规范

默认 normalize 处理空行、标题间隔、列表缩进、表格与字面符号；保留代码块内容，必要时闭合未闭合栅格。check 只检查不写回。

```bash
./gewu atlas --action fmt --ref <vlt_01> --note_ref <nte_01> --normalize true
./gewu atlas --action fmt --ref <vlt_01> --note_ref <nte_01> --normalize true --check true
```

各格式共享表格要求：GFM 表头与分隔行齐全，单元格换行压平，字面管道转义，全空列剔除，跨列/跨行填充必须核对原始语义；HTML 表格先于正文脱水处理。无法确定的残表不能靠补空格猜成新数据。

仅在 deep/明确精修任务中，由 Agent 校对标题层级、行内枚举、引用、图片导读与异源残留。使用 fmt-window/fmt-apply-window 做局部替换，保留未选正文与元数据。不是每次默认转换都强制语义改写，也不承诺固定秒数。

验收正文事实、代码、表格列数与双链；原文差异需有依据，无法确认的内容保留并说明。使用 schema-check/vfs-links 检查对应卡片与引用。

职责与调用总则以 [atlas-fmt](../SKILL.md) 及 [commands.yaml](../../../dicts/commands.yaml) 与 [rule-core-vfs.md](../../../rules/rule-core-vfs.md) 为准。本参考的语义加工步骤仅在 deep 或明确要求相应加工时执行。
