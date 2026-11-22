---
doc_type: rule
title: "元数据治理与 Schema 规范"
summary: "双层正交 Frontmatter 模型、单向溯源链路（sources/slices）、署名审核规范与轻量日记约束"
status: active
tags: [Rule, Schema, Metadata]
updated: 2026-11-22
version: "2.5.0"
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

## 2. 单向溯源链路与零冗余约束

- 五类核心文档（`library`、`slice`/`toc`、`atomic`、`moc`、`artifact`）采用**单向流动（DAG）链接模型**，严禁双向往复回写，反向关系由 Obsidian 反向链接与运行时索引实时计算：
  - `library`（入库即只读原料）：不携带指向下游卡片的 `extracted_to`、`map` 或 `moc`；
  - `slice` / `toc`：单向经 `source_doc` 指向所属 `library`，`toc` 不回写 `extracted_to`；
  - `atomic`：**不在 YAML Frontmatter 中写入 `sources`、`slices`、`provenance`、`source`、`toc`、`moc`、`map`**，统一在正文最后一章 `## 参考来源与知识库索引` 采用**写法 A：列表式（兼顾 Obsidian 双链跳转 + 句柄留痕）**单向记录文献与切片双链及 `nte_XX` 句柄；
  - `moc`：仅在正文单向收纳 `[[atomic]]` 等卡片，不回写原子卡 YAML；
  - `artifact`：**不在 YAML Frontmatter 中写入 `sources`、`slices` 等溯源字段**，统一在正文最后一章 `## 参考来源与知识库索引` 采用**写法 A：列表式（兼顾 Obsidian 双链跳转 + 句柄留痕）**单向记录依据的课题总纲、一手文献、原子卡与 MOC。
- **历史卡片宽容兼容**：存量老卡片中残留的 `sources`、`slices`、`provenance`、`source`、`toc`、`moc`、`map`、`extracted_to` 字段由读取与校验层宽容兼容，不再因历史派生不一致报错；新建与更新卡片统一采用上述极简末章双链溯源模型。多源汇聚仍不改变原子卡「一事一卡」的单一命题粒度。

## 3. 审核与署名字段真实性红线

- 九类知识生产链文档采用 `created_by`、`updated_by`、`reviewed_by` 署名三元组：
  - `reviewed_by: ai` 表示 `auto` 模式下智能体按规范自审入库；
  - `reviewed_by: human` 表示经人类显式审批转正；
  - `supervised` 监督模式下的 `pending` 候选卡不填写最终审核值。
- 智能体严禁伪造 `reviewed_by: human`，严禁在未经人类真实批准及版本摘要核验（`--expected_sha256`）的情况下将待审卡转正。

## 4. 轻量日记与自由工作台豁免

- **格物日记（`05-Journal/` / `doc_type: journal`）**：固定携带 `private: true`，**严禁写入 `status`、`reviewed_by`、`stage` 等知识定性或流程审批字段**。
- **工作台草稿（`30-Workbench/`）**：允许存放无 YAML Frontmatter 的自由格式研究草记；仅在正式采收进入 `21-Library/` 或 `10-Artifacts/` 时补齐合规 YAML 元数据。
