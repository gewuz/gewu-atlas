---
doc_type: rule
title: "元数据治理与 Schema 规范"
summary: "双层正交 Frontmatter 模型、多源 provenance 一致性、署名审核规范与轻量日记约束"
status: active
tags: [Rule, Schema, Metadata]
updated: 2026-11-17
version: "2.4.0"
---

# 元数据治理与 Schema 规范

本规范定义全库 Markdown 文档的 YAML Frontmatter 结构与校验不变量。[metadata.yaml](../dicts/metadata.yaml) 是全库字段类型、必填项与枚举词表的唯一事实源（SSOT），任何分歧以字典定义为准。

## 1. 双层正交 Frontmatter 模型

全库结构化文档的元数据分为两层：

1. **第一层（共有基石字段）**：
   - `doc_type`：文档类型枚举（如 `manifest`、`library`、`slice`、`toc`、`atomic`、`moc`、`artifact`、`workbench`、`slip`、`journal`）；
   - `title`：人类可读标题；
   - `summary`：一句话核心定义或导读（`atomic`、`library`、`moc`、`artifact` 必填，长度不超过 `200` 字）；
   - `status`：真理性状态（`pending` / `active` / `deprecated`）；
   - `tags`：标签数组，**严禁出现纯数字标签**或无意义噪声词；
   - `updated`：最后修订日期（`YYYY-MM-DD`）。
2. **第二层（类型专属字段與域溯源）**：
   - 各 `doc_type` 仅允许使用字典登记的专属字段，严禁跨类型混用或自创未登记污染键；
   - 处于课题域（`30-Projects/<id>/`）内的知识生产链文档持有 `project_id` 字段，保障单卡脱离物理目录后仍具备自包含课题归属溯源。

## 2. 多源证据（`provenance`）与派生字段一致性

- 原子卡支持从多篇文献、多个 `TOC` 骨架与多个切片中提炼证据，以 Obsidian 属性面板兼容的 `provenance` 字符串列表保存结构化分组关系（每条编码为 `"source=[[...]] | toc=[[...]] | slices=[[...]],[[...]]"`，省略空段；系统同时向后兼容解析历史对象列表）。
- 顶层 `sources`、`toc`、`slices` 由 `provenance` 组内关系自动去重派生，顶层 `source` 保留首个主来源的兼容标量。
- 归属校验严格核对切片的 `source_doc` / `map` 与 `TOC` 的 `source_doc`；分组关系与顶层派生字段不一致时由 Schema 巡检拦截。多源汇聚不改变原子卡「一事一卡」的单一命题粒度。

## 3. 审核与署名字段真实性红线

- 九类知识生产链文档采用 `created_by`、`updated_by`、`reviewed_by` 署名三元组：
  - `reviewed_by: ai` 表示 `auto` 模式下智能体按规范自审入库；
  - `reviewed_by: human` 表示经人类显式审批转正；
  - `supervised` 监督模式下的 `pending` 候选卡不填写最终审核值。
- 智能体严禁伪造 `reviewed_by: human`，严禁在未经人类真实批准及版本摘要核验（`--expected_sha256`）的情况下将待审卡转正。

## 4. 轻量日记与自由工作台豁免

- **格物日记（`05-Journal/` / `doc_type: journal`）**：固定携带 `private: true`，**严禁写入 `status`、`reviewed_by`、`stage` 等知识定性或流程审批字段**。
- **工作台草稿（`30-Workbench/`）**：允许存放无 YAML Frontmatter 的自由格式研究草记；仅在正式采收进入 `21-Library/` 或 `10-Artifacts/` 时补齐合规 YAML 元数据。
