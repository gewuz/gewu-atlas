---
name: atlas-style
description: GewuAtlas 格物志文本排版、语言风格、语气语态、Mermaid 图表绘制与 Obsidian / 对话双通道呈现底座技能。当撰写或整理 document/atomic/moc/artifact 笔记、交付深度研究报告、生成知识结构图（flowchart/sequenceDiagram/mindmap 等）或向人类输出长篇结构化回答时遵守本规范。
---

# 格物志写作、排版与图表底座（atlas-style）

定义知识卡片与研究交付物的排版、语态与图表标准（`./gewu atlas --action note-create|note-update|note-format`）。

## 1. 语言风格与语气语态（同行专家）

- **同行专家定位**：以冷静、克制、高信息密度的**同行专家**语态写作；结论先行，论据紧随，显式交代适用边界与反例。
- **量词修辞禁令**：严禁使用空洞宏大叙事、感叹号堆砌、夸张副词或“赋能/抓手/闭环生态”等黑话套话；用具体机制、数字和句柄引用（`[[nte_XX]]`）说话。
- **反模式规避**：禁止把检索命中列表或原始碎片直接拼贴当作回答；禁止在正文中暴露底层物理路径或内部状态文件名。

## 2. 排版铁律（盘古空格、标点外置与序号三通道互斥）

1. **盘古空格**：中英文、中文与数字、中文与行内代码之间保留 1 个半角空格（如 `使用 BM25 算法`），标点符号前后不加多余空格。
2. **标点外置熔断**：粗体 `**...**`、斜体或双链 `[[...]]` 内部末尾严禁包裹中文句号、逗号、冒号等标点，必须遵循**标点外置**（正确：`**核心命题**：内容`，错误：`**核心命题：**内容`），防止 Markdown 解析粘连失效。
3. **序号三通道互斥**：同一层级标题或列表严禁混用两套序号（如禁止 `- 1. 第一点` 或 `## 1. 一、标题`），Markdown 列表符、阿拉伯数字与中文序号三通道互斥，择一使用。

## 3. 安全 Mermaid 图表规范

- 仅使用标准 `flowchart TD/LR`、`sequenceDiagram`、`stateDiagram-v2`、`classDiagram`、`erDiagram`；
- **节点标签必加双引号**：凡含括号、冒号、斜杠或空格的节点文本，必须写为 `A["节点名称 (补充说明)"]`，严禁裸写 `A[节点 (说明)]`；
- 代码块必须成对闭合（`guard --mode delivery` 会自动校验未闭合的 `mermaid` 代码块）。

## 4. 交付质量自检（配合 40 分软量表中的可读性 10 分）

- 交卷前确认：标题层级连续（`##` → `###`）、表格双链已安全转义、标点外置合规、Mermaid 闭合、全文零本机绝对路径。

全局路由见 [AGENTS.md](../../AGENTS.md)，分级排版细则见 [rule-core-formatting-tiers.md](../../rules/rule-core-formatting-tiers.md) 与 [rule-core-methodology.md](../../rules/rule-core-methodology.md)。
