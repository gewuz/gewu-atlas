---
name: atlas-obsidian
description: Obsidian 开放格式与查询语法规范统一入口（Obsidian Flavored Markdown .md、Obsidian Bases .base、JSON Canvas .canvas、Obsidian Dataview DQL/DataviewJS）：编写或校验 wikilinks 双链、嵌入（![[...]]）、Callout、YAML 属性、标签、高亮、.base 视图/过滤/公式/汇总、.canvas 思维导图/流程图节点与连线，以及 dataview/dataviewjs 查询块与内联表达式时激活。
---

# Obsidian 开放格式与查询规范统一入口（`atlas-obsidian`）

本技能整合官方 [obsidianmd/obsidian-skills](https://github.com/obsidianmd/obsidian-skills) 三项开放格式规范与 [Obsidian Dataview](https://blacksmithgu.github.io/obsidian-dataview/) 查询规范，按「精简入口 + `references/` 按需查阅」结构组织。执行具体格式或查询任务前，**请按目标类型读取对应参考文档**：

## 1. 规范路由表（按需读取 `references/`）

| 目标格式 / 能力 | 适用场景 | 详细规范入口 |
| :--- | :--- | :--- |
| **Obsidian Markdown (`.md`)** | 双链（`[[Note]]` / 别名 / 标题锚点 / 块引用）、嵌入（`![[...]]`）、Callout（`> [!note]`）、YAML 属性、嵌套标签、高亮、注释、数学公式与 Mermaid 内链 | [references/obsidian-markdown.md](references/obsidian-markdown.md) |
| **Obsidian Bases (`.base`)** | 原生数据库式看板（`table` / `cards` / `list` / `map` 视图）、`filters`（`and/or/not`）、`formulas` 计算属性、`summaries` 聚合汇总与 `![[MyBase.base]]` 嵌入 | [references/obsidian-bases.md](references/obsidian-bases.md) |
| **JSON Canvas (`.canvas`)** | 可视化白板、概念图、流程图与项目看板（`nodes`: `text/file/link/group`、`edges` 连线、16 位小写十六进制 ID、坐标布局与校验清单） | [references/json-canvas.md](references/json-canvas.md) |
| **Obsidian Dataview (`dataview` / `dataviewjs`)** | DQL 动态查询块（`TABLE` / `LIST` / `TASK` / `CALENDAR`）、`FROM`/`WHERE`/`GROUP BY`/`FLATTEN`/`SORT`/`LIMIT`、内联字段（`[k:: v]`）、内联表达式（`` `= ...` `` / `` `$= ...` ``）与 `dv.*` JS API | [references/obsidian-dataview.md](references/obsidian-dataview.md) |

## 2. 与 GewuAtlas 工程红线的衔接约定

1. **受控写盘红线**：在 `GewuAtlas` 知识库中新建/修改 `.md` 笔记必须通过 `./gewu atlas` 门面算子执行（零物理路径盲化），严禁直接用原生 shell 绕过算子读写 `Volumes/GewuAtlas-data/`。
2. **高亮与 Slip 批注区分**：普通 `==重点内容==` 为人类阅读高亮；`==@指令==`（兼容全半角 `＝`/`＠` 及历史 `@@指令@@`）为 `GewuAtlas` 的 `25-Slip` 书签批注指令，提交后将自动替换为 `[[Slip-...|原指令]]`。
3. **双链、看板与查询同步**：`.md` 内部引用统一使用双链（`[[Note]]`），外部网页使用 `[text](url)`；`.base` 看板位于 `10-Index/`（可用 `index-sync` 维护）；`.canvas` 节点引用的笔记在 `vfs-move` / `project-merge` 时会自动级联更新路径；在笔记内写 `dataview` 查询时优先用 `FROM` 限定课题或标签范围，避免全库扫描卡顿。

全局路由见 [AGENTS.md](../../AGENTS.md)，规范见 [rule-core-linking.md](../../rules/rule-core-linking.md)、[rule-core-schema.md](../../rules/rule-core-schema.md) 与 [rule-core-formatting-tiers.md](../../rules/rule-core-formatting-tiers.md)。
