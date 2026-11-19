# PPT：版面与叙事

默认脚本支持 PPTX 文本框与 Group Shape 聚类、标题/转场识别、指标列表、表格 GFM、内嵌图片和演讲者备注。旧 PPT 需另存 PPTX/PDF；SmartArt、图表、母版及复杂效果不能保证完整还原。

deep：逐页核对标题和章节顺序、散点指标与单位、备注是否为正文补充或排练提示、表格列对应、图片遗漏。将散点整理成连贯叙述时只用源内容，不补造因果。全图进行四轮检查和伴生卡；不适合 Mermaid 的图明确不适用。与源演示对照后才报告完成。

职责与调用总则以 [atlas-fmt](../SKILL.md) 及 [commands.yaml](../../../dicts/commands.yaml) 与 [rule-core-vfs.md](../../../rules/rule-core-vfs.md) 为准。本参考的语义加工步骤仅在 deep 或明确要求相应加工时执行。
