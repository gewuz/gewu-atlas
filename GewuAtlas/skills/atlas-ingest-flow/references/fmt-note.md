# 文本与笔记：六路分流

继承 R6 note_refiner 的识别与整理能力：完整 Library、Atomic、MOC 元数据直通；TXT 结构化转换并归档；语音转写/碎片备忘整理；普通 Markdown 补齐元数据。识别基于文本和元数据，语义判断由 Agent 在需要时复核，不把关键词判断当真相。

默认遵守原有六路分流与域归属；收件箱的 Atomic/MOC 声明不能直通正式区，仍作为 Library 原料进入后续萃取/织网。受管工作台中合规的既有卡片才可按其类型直通；显式 target_zone 指定落点。Atomic 的审核状态不能因导入被自动伪造为人工批准。日记被保护，不自动搬移或采收；需要提炼时由用户挑选内容另作素材。

使用 `--encoding cp1252` 等明确来源编码。默认优先 BOM/UTF，非UTF可能有歧义，核对中文与西文，不因能解码就声称正确。RTF 使用 striprtf 解码控制字与 Unicode 转义并提取纯文本；不承诺恢复 RTF 图片、字体和复杂表格版式。

deep：检查分流/元数据、说话人、口头重复、停顿和专有名词。允许整理冗词，保留立场、数字、不确定性；待办建议不得冒充用户命令。表格结构交给 normalize，无法确定的列关系标注。

DropZone 中独占行 URL 清单走 url-ingest；普通内联链接或日记不自动下载；失败保留原清单与台账。

职责与调用总则以 [atlas-fmt](../SKILL.md) 及 [commands.yaml](../../../dicts/commands.yaml) 与 [rule-core-vfs.md](../../../rules/rule-core-vfs.md) 为准。本参考的语义加工步骤仅在 deep 或明确要求相应加工时执行。
