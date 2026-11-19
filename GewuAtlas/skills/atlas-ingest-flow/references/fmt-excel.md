# Excel 与 CSV：结构与数据保真

默认读取 XLSX、XLS、CSV。多 Sheet 逐页生成导航；XLSX 支持合并单元格重构、隐藏行列提示、日期、公式缓存；无缓存保留公式文本，不执行公式、不重算。XLS 经 xlrd 读取单元格；合并样式等高级布局不等同 XLSX。CSV 可用 encoding 指定编码。

threshold 默认50行：小表完整 GFM；大表为表头/样本预览加 UTF-8 CSV 底表，不把预览当全表。单元格换行压平、管道转义；数值合并单元格不重复填充求和。

深加工：核对每个 Sheet、合并表头层级、单位币种、百分比口径、合计/汇总，检查 CSV 与预览同源。对前10行的代表性明确限制。洞察需数据支持，不能从相关性编造归因；公式无缓存应说明需源应用重算。含宏不执行，损坏/加密文件报告失败。

职责与调用总则以 [atlas-fmt](../SKILL.md) 及 [commands.yaml](../../../dicts/commands.yaml) 与 [rule-core-vfs.md](../../../rules/rule-core-vfs.md) 为准。本参考的语义加工步骤仅在 deep 或明确要求相应加工时执行。
