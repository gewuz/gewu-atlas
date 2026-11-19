# 本地 HTML：DOM 脱水与编码

默认以 article/main 为优先正文容器，必要时 readability 提取；去 script/style/nav/header/footer/aside；HTML 表格先转 GFM 后回填，避免 html2text 破坏管道与换行；提取 Base64 和可访问的本地图片，支持多编码读取。相关本地图片也应显式导入，缺图报告，不扫描宿主目录找图。

deep：先清导航、面包屑、广告、推荐、评论和页脚，再核对正文有无遗漏；恢复标题、列表、引用和表格关系。原作者观点不能因去营销措辞而改变。图片区分承载信息、装饰和失败；对失败只说明来源和缺口，不能凭占位图猜内容。

长文经 fmt-window 逐窗处理，前后文只作接缝参照，正文窗口不重复写入；核对作者、标题、来源和时间，无法核实的保留未知。

静态脚本不执行 JS。空壳/恶性乱码不能作为成功材料；保留原件和失败台账，需要清理派生卡时先 vfs-links，再按 atlas-guard 软删除，不能直接删源或绕过确认。

职责与调用总则以 [atlas-fmt](../SKILL.md) 及 [commands.yaml](../../../dicts/commands.yaml) 与 [rule-core-vfs.md](../../../rules/rule-core-vfs.md) 为准。本参考的语义加工步骤仅在 deep 或明确要求相应加工时执行。
