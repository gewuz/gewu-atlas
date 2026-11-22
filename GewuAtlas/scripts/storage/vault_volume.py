"""Separated Data-Volume and Vault directory manager for GewuAtlas."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Tuple

from notes.atlas_yamlmini import FrontmatterError, extract_frontmatter, get_vault_layout_spec
from storage.vault_kek_store import (
    RESERVED_VAULT_IDS,
    VAULT_ID_RE,
    VaultStoreError,
    validate_rel_vault_path,
)

VOLUME_REL_ROOT = "Volumes/GewuAtlas-data"


def _load_layout_spec_safe() -> Dict[str, Any]:
    try:
        return get_vault_layout_spec()
    except FrontmatterError as exc:
        raise VaultStoreError("E_DICT_INVALID", str(exc), exit_code=1) from exc


def get_vault_subdirs() -> Tuple[str, ...]:
    return tuple(_load_layout_spec_safe()["dirs"])


def get_vault_gitkeep_dirs() -> Tuple[str, ...]:
    return tuple(_load_layout_spec_safe()["gitkeep_dirs"])


VAULT_SUBDIRS = get_vault_subdirs()

BASE_TEMPLATES: Dict[str, str] = {
    "01-Atomics-常青原子卡.base": """filters:
  and:
    - doc_type == "atomic"
    - file.ext == "md"
    - status == "active"
    - not:
        - file.folder.startsWith("90-System")
        - file.folder.contains("91-Temp")
        - file.folder.contains("97-Attic")
formulas:
  type_label: if(type == "OBJ", "📘 客观事实", if(type == "SUB", "💡 主观洞察", if(type == "SYN", "⚡ 人机推导", if(type == "HYP", "❓ 待证假说", "📝 其他"))))
properties:
  file.name:
    displayName: 原子卡片 (Atomic)
  summary:
    displayName: 核心命题与定义
  formula.type_label:
    displayName: 认识论定性
  updated:
    displayName: 更新日期
  tags:
    displayName: 领域标签
views:
  - type: table
    name: "🧠 常青原子卡全览 (Status: active)"
    groupBy:
      property: type
      direction: ASC
    order:
      - file.name
      - formula.type_label
      - summary
      - tags
      - updated
    sort:
      - property: updated
        direction: DESC
  - type: cards
    name: "📇 原子卡片画廊"
    order:
      - file.name
      - summary
      - formula.type_label
      - tags
""",
    "02-Pending-待审卡片盒.base": """filters:
  and:
    - file.ext == "md"
    - status == "pending"
    - not:
        - file.folder.startsWith("90-System")
        - file.folder.contains("91-Temp")
        - file.folder.contains("97-Attic")
properties:
  file.name:
    displayName: 待审知识卡片
  status:
    displayName: 审核状态
  summary:
    displayName: 核心命题 / 摘要
  project_id:
    displayName: 所属课题
  updated:
    displayName: 萃取日期
views:
  - type: table
    name: "⏳ 待审知识队列 (Pending Queue)"
    order:
      - file.name
      - status
      - summary
      - project_id
      - updated
    sort:
      - property: updated
        direction: DESC
""",
    "03-MOC-主题内容地图.base": """filters:
  and:
    - doc_type == "moc"
    - file.ext == "md"
    - status != "deprecated"
formulas:
  read_label: if(read_status == "unread", "🆕 unread 待阅", "🟢 read 已阅")
properties:
  file.name:
    displayName: 内容地图 (MOC)
  formula.read_label:
    displayName: 导读阅览状态
  summary:
    displayName: 全景导读摘要
  updated:
    displayName: 织网日期
views:
  - type: table
    name: "🗺️ 全景 MOC 导读网络"
    order:
      - file.name
      - formula.read_label
      - summary
      - updated
    sort:
      - property: updated
        direction: DESC
""",
    "04-Library-客观文献卷宗.base": """filters:
  and:
    - doc_type == "library"
    - file.ext == "md"
properties:
  file.name:
    displayName: 文献标题
  summary:
    displayName: 导读摘要
  source_type:
    displayName: 原料格式
  channel:
    displayName: 采集渠道
  updated:
    displayName: 入库日期
views:
  - type: table
    name: "📚 全部客观文献原料"
    order:
      - file.name
      - summary
      - source_type
      - channel
      - updated
    sort:
      - property: updated
        direction: DESC
""",
    "05-Artifacts-高维成果研报.base": """filters:
  and:
    - doc_type == "artifact"
    - file.ext == "md"
formulas:
  stage_label: if(stage == "final", "🏆 定稿研报", "📝 草案进行中")
properties:
  file.name:
    displayName: 交付研报文档
  formula.stage_label:
    displayName: 交付阶段
  summary:
    displayName: 研报导读摘要
  project_id:
    displayName: 课题编号
  updated:
    displayName: 交付日期
views:
  - type: table
    name: "🚀 高维成果交付全览"
    order:
      - file.name
      - formula.stage_label
      - project_id
      - summary
      - updated
    sort:
      - property: updated
        direction: DESC
""",
    "06-Projects-课题域全景.base": """filters:
  and:
    - doc_type == "manifest"
    - file.ext == "md"
formulas:
  mode_label: if(default_mode == "auto", "🤖 自主自治", "👁️ 人机监督")
  privacy_label: if(private == true, "🔒 私有软墙", "🌐 全库公开")
properties:
  file.name:
    displayName: 课题域清单 (Manifest)
  project_id:
    displayName: 课题标识
  status:
    displayName: 生命状态
  formula.mode_label:
    displayName: 自治级别
  formula.privacy_label:
    displayName: 可见性
  updated:
    displayName: 更新日期
views:
  - type: table
    name: "📁 全部课题域全景"
    order:
      - file.name
      - project_id
      - status
      - formula.mode_label
      - formula.privacy_label
      - updated
""",
    "07-Tickets-工单协作台账.base": """filters:
  and:
    - doc_type == "ticket"
    - file.ext == "md"
properties:
  file.name:
    displayName: 工单单号
  track:
    displayName: 轨道
  status:
    displayName: 流程状态
  title:
    displayName: 任务标题
  updated:
    displayName: 日期
views:
  - type: table
    name: "📋 全部协同工单流水"
    order:
      - file.name
      - track
      - status
      - title
      - updated
""",
}

INDEX_README = """---
doc_type: index
title: "格物志多维看板导航控制台"
summary: "10-Index 导航总控台，汇聚常青原子卡、待审队列、MOC 地图、文献卷宗、成果研报、课题域与工单 Bases 视图"
status: active
tags: [Index, Dashboard, Bases, Console]
updated: {today}
---

# 格物志多维看板导航控制台 (10-Index)

本控制台为 Obsidian 宿主提供多维 `.base` 交互看板，由 `./gewu atlas --action index-sync --vault <vault>` 维护。

- [[01-Atomics-常青原子卡.base]]
- [[02-Pending-待审卡片盒.base]]
- [[03-MOC-主题内容地图.base]]
- [[04-Library-客观文献卷宗.base]]
- [[05-Artifacts-高维成果研报.base]]
- [[06-Projects-课题域全景.base]]
- [[07-Tickets-工单协作台账.base]]
"""

NOTE_TEMPLATES: Dict[str, str] = {
    "01-Atomic-原子知识卡.md": """---
doc_type: atomic
title: "{{title}}"
summary: "一句话核心命题或断言（≤ 200 字）"
status: active
type: SYN
project_id: "ai-zone"
tags: [Atom]
aliases: []
supersedes: ""
updated: {{date:YYYY-MM-DD}}
created_by: "human"
updated_by: "human"
reviewed_by: human
---

# {{title}}

> 核心命题：一句话提炼本卡唯一核心断言。

## 1. 核心定义与断言 (Claim)

用简洁、可验证的同行专家语态陈述核心命题。

## 2. 依据与机理 (Evidence)

- **关键机理**：说明因果链条、架构机制或定量数据。
- **支撑事实**：引用原始文献、实验或生产实践证据。

## 3. 边界与权衡 (Boundary)

- **适用前提**：明确该结论成立的条件与范围。
- **失效场景与反例**：列出退化边界或替代路径。

## 参考来源与知识库索引

- **一手文献与切片来源**：
  - [[来源文献文件名|文献可读标题]]（`nte_XX`，关联切片：[[切片文件名|切片标题]] `nte_YY`）
""",
    "02-Artifact-成果研究报告.md": """---
doc_type: artifact
title: "{{title}}"
summary: "一句话概括本篇研究报告的核心结论与交付价值（≤ 200 字）"
status: active
stage: draft
type: SYN
project_id: "ai-zone"
tags: [Artifact, 研究报告]
aliases: []
processed: false
updated: {{date:YYYY-MM-DD}}
created_by: "human"
updated_by: "human"
---

# {{title}}

> 核心结论：先给出可落地的顶层判断与关键数字，再展开机理与选型论证。

## 1. 问题定义与背景边界

明确研究范围、核心痛点与本文不讨论的边界。

## 2. 底层机理与架构剖析

拆解关键机制，必要时附带合规 Mermaid 结构图：

```mermaid
flowchart LR
    A["输入条件"] --> B["核心机制"]
    B --> C["输出与边界"]
```

## 3. 横向对比与可复算工程案例

| 评估维度 | 方案 A | 方案 B |
| :--- | :--- | :--- |
| 适用场景 | ... | ... |
| 关键代价 | ... | ... |

### 可复算工程案例

给出明确的假设口径与可复算的量化结果。

## 4. 边界、风险与落地建议

1. **红线与失效场景**：...
2. **分阶段落地路径**：...

## 参考来源与知识库索引

- **课题总纲与主题地图**：
  - [[Manifest-课题名|课题立项总纲]]（`nte_XX`）
  - [[MOC-主题地图|主题内容地图]]（`nte_YY`）
- **核心原子卡片（`22-Atomics`）**：
  - [[Atom-卡片文件名|原子卡片可读标题]]（`nte_ZZ`）
- **一手文献与切片来源（`21-Library` / `24-Slices`）**：
  - [[文献文件名|文献可读标题]]（`nte_AA`，关联切片：[[切片文件名|切片标题]] `nte_BB`）
""",
    "03-MOC-主题内容地图.md": """---
doc_type: moc
title: "{{title}}"
summary: "一句话概括本主题知识网络的全景脉络与核心议题（≤ 200 字）"
status: active
type: SYN
read_status: unread
project_id: "ai-zone"
tags: [MOC]
aliases: []
updated: {{date:YYYY-MM-DD}}
created_by: "human"
updated_by: "human"
---

# {{title}}

## 宏观综述

概述本主题域的核心问题、关键概念之间的拓扑关系及推荐阅读顺序。

<!-- atlas:moc-index:start -->
### Layer 1 核心拓扑与理论基石

- [[Atom-卡片文件名|原子卡片标题]]：一句话核心命题。

### Layer 2 工程机制与方法实践

- [[Atom-卡片文件名|原子卡片标题]]：一句话核心命题。
<!-- atlas:moc-index:end -->

## 逻辑闭环评估与扩展研究建议

- **当前闭环度**：说明现有卡片已覆盖哪些关键环节，尚缺哪些反例或一手数据。
- **建议扩展方向**：
  1. 方向一：...
  2. 方向二：...
""",
    "04-Library-客观文献卷宗.md": """---
doc_type: library
title: "{{title}}"
summary: "一句话概括原始文献的核心内容与权威出处（≤ 200 字）"
status: active
source_type: web
channel: "manual"
source_url: "https://example.com"
processed: false
project_id: "ai-zone"
tags: [Library]
aliases: []
updated: {{date:YYYY-MM-DD}}
created_by: "human"
updated_by: "human"
---

# {{title}}

> 注意：`21-Library/` 属只读一手原料区，保持客观原文完整，不在此处混写主观二次结论。

正文内容...
""",
    "05-Manifest-课题立项总纲.md": """---
doc_type: manifest
title: "{{title}}"
summary: "一句话说明本课题域的研究目标与产出边界（≤ 200 字）"
status: active
default_mode: auto
project_id: "prj-slug"
private: false
tags: [Manifest, Project]
aliases: []
updated: {{date:YYYY-MM-DD}}
created_by: "human"
updated_by: "human"
---

# {{title}}

## 1. 课题目标与核心问题

- **核心问题**：...
- **目标交付物**：...

## 2. 研究范围与排除边界

- **纳入范围**：...
- **暂不纳入**：...

## 3. 阶段里程碑与验收标准

- [ ] 一手文献采集与切片入库
- [ ] 核心原子知识卡蒸馏与 MOC 织网
- [ ] 成果研报交付与验货闭环
""",
    "06-Journal-每日格物日记.md": """---
doc_type: journal
tags: [Journal]
---

# 人类记录

在此记录当日个人随想、人工决策与待办事项（AI 严禁覆盖或改写本区）。

# AI 编年记录

- 系统自动追加当日知识库操作与关键变更摘要。
""",
    "07-Slip-书签批注卡.md": """---
doc_type: slip
title: "{{title}}"
summary: "一句话概括书签批注或轻量跟进任务（≤ 200 字）"
status: active
slip_kind: note
source_note: "[[来源笔记]]"
project_id: "ai-zone"
tags: [Slip]
aliases: []
updated: {{date:YYYY-MM-DD}}
created_by: "human"
updated_by: "human"
---

# {{title}}

## 原文摘录与批注

> 引用片段...

批注思考与后续动作：...
""",
    "08-Workbench-工作台草稿.md": """---
doc_type: workbench
tags: [Workbench, Draft]
---

# {{title}}

自由探索笔记、实验对比与报告初稿（成熟后经 `artifact-adopt` 采收入正式区）。
""",
}

TEMPLATES_README = """# 格物志文档模板库 (`90-System/01-Templates`)

本目录存放与 `GewuAtlas/dicts/metadata.yaml` 及 `atlas-style` 对齐的标准 Obsidian 文档模板，由 `./gewu atlas --action index-sync --ref <vlt_XX>` 维护（默认保护人工修改，仅 `--force true` 强制刷新）。

## Obsidian 配置方法

在 Obsidian 中打开 **设置 → 核心插件 → 模板（Templates）**，将「模板文件夹位置」设置为：

```text
90-System/01-Templates
```

## 模板清单

| 模板文件 | 对应 `doc_type` | 适用分区 | 核心约束 |
| :--- | :--- | :--- | :--- |
| `01-Atomic-原子知识卡.md` | `atomic` | `22-Atomics/` | YAML 无 `sources` / `slices`，末章用写法 A 双链 + 句柄溯源 |
| `02-Artifact-成果研究报告.md` | `artifact` | `10-Artifacts/` | YAML 无 `sources`，末章用写法 A 双链 + 句柄溯源 |
| `03-MOC-主题内容地图.md` | `moc` | `23-MOC/` | 保留 `<!-- atlas:moc-index:start/end -->` 受控块 |
| `04-Library-客观文献卷宗.md` | `library` | `21-Library/` | 只读原料，不回写 `extracted_to` |
| `05-Manifest-课题立项总纲.md` | `manifest` | `30-Projects/<id>/` | 定义 `project_id`、`default_mode` 与 `private` 边界 |
| `06-Journal-每日格物日记.md` | `journal` | `05-Journal/` | 极简 YAML（仅 `doc_type` 与 `tags`），严禁写入 `status` / `reviewed_by` |
| `07-Slip-书签批注卡.md` | `slip` | `25-Slip/` | 轻量书签批注与随记卡片 |
| `08-Workbench-工作台草稿.md` | `workbench` | `30-Workbench/` | 极简 YAML（仅 `doc_type` 与 `tags`），成熟后可采收入正式区 |
"""


def _today_iso() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _now_stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")


def normalize_vault_id(vault_id: str) -> str:
    if not isinstance(vault_id, str):
        raise VaultStoreError("E_PARAM_INVALID", "vault 参数必须为字符串", exit_code=1)
    vid = vault_id.strip()
    if not VAULT_ID_RE.fullmatch(vid) or vid in RESERVED_VAULT_IDS:
        raise VaultStoreError(
            "E_PARAM_INVALID",
            "非法 Vault 标识（仅允许小写字母、数字、-_，长度 1~48，且不可为保留字）",
            exit_code=1,
        )
    return vid


def make_vault_rel_path(vault_id: str) -> str:
    vid = normalize_vault_id(vault_id)
    return f"{VOLUME_REL_ROOT}/{vid}"


def _write_noexec_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".atlas-", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def resolve_safe_vault_dir(workspace_root: Path, rel_path: str) -> Path:
    validate_rel_vault_path(rel_path)
    vol_root = workspace_root / "Volumes" / "GewuAtlas-data"
    vol_root.mkdir(parents=True, exist_ok=True)
    vol_resolved = vol_root.resolve()
    target = workspace_root / PurePosixPath(rel_path)
    cur = workspace_root
    for part in PurePosixPath(rel_path).parts:
        cur = cur / part
        if cur.exists() and cur.is_symlink():
            raise VaultStoreError("E_HALT_SECURITY", "拒绝访问包含符号链接的数据卷路径", exit_code=2)
    resolved = target.resolve()
    if vol_resolved not in resolved.parents:
        raise VaultStoreError("E_PARAM_INVALID", "Vault 路径越界出数据卷根", exit_code=2)
    return resolved


def sync_obsidian_bases_detailed(vault_dir: Path, overwrite: bool = False) -> Tuple[List[str], List[str]]:
    """Sync default 10-Index/*.base & README.md without clobbering existing human edits unless overwrite=True."""
    index_dir = vault_dir / "10-Index"
    index_dir.mkdir(parents=True, exist_ok=True)
    synced: List[str] = []
    preserved: List[str] = []
    items = list(BASE_TEMPLATES.items()) + [("README.md", INDEX_README.format(today=_today_iso()))]
    for fname, body in items:
        target = index_dir / fname
        if target.exists() and not target.is_symlink() and not overwrite:
            preserved.append(fname)
            continue
        _write_noexec_text(target, body)
        synced.append(fname)
    return synced, preserved


def sync_obsidian_bases(vault_dir: Path, overwrite: bool = False) -> List[str]:
    synced, _ = sync_obsidian_bases_detailed(vault_dir, overwrite=overwrite)
    return synced


def sync_obsidian_templates_detailed(vault_dir: Path, overwrite: bool = False) -> Tuple[List[str], List[str]]:
    """Sync default 90-System/01-Templates/*.md & README.md without clobbering existing human edits unless overwrite=True."""
    tpl_dir = vault_dir / "90-System" / "01-Templates"
    for cur in (vault_dir / "90-System", tpl_dir):
        if cur.exists() and cur.is_symlink():
            raise VaultStoreError("E_HALT_SECURITY", "拒绝在包含符号链接的模板目录中同步模板", exit_code=2)
    tpl_dir.mkdir(parents=True, exist_ok=True)
    synced: List[str] = []
    preserved: List[str] = []
    items = list(NOTE_TEMPLATES.items()) + [("README.md", TEMPLATES_README)]
    for fname, body in items:
        target = tpl_dir / fname
        if target.is_symlink():
            raise VaultStoreError("E_HALT_SECURITY", f"拒绝覆盖符号链接模板文件: {fname}", exit_code=2)
        if target.exists() and not overwrite:
            preserved.append(fname)
            continue
        _write_noexec_text(target, body)
        synced.append(fname)
    return synced, preserved


def sync_obsidian_templates(vault_dir: Path, overwrite: bool = False) -> List[str]:
    synced, _ = sync_obsidian_templates_detailed(vault_dir, overwrite=overwrite)
    return synced


def initial_workbench_readme(domain_label: str) -> str:
    return (f"# 工作台 · {domain_label}\n\n"
            "当前为自由探索工作台（允许人类与智能体存放未加 YAML 的原始 `note` 笔记与探索草案）。\n")


def init_empty_workbench(path: Path, domain_label: str = "ai-zone") -> None:
    """Initialize only the public README; tasks allocate their own folders later."""
    if path.is_symlink():
        raise VaultStoreError("E_HALT_SECURITY", "工作台不能是符号链接", exit_code=2)
    path.mkdir(parents=True, exist_ok=True)
    readme = path / "README.md"
    if readme.is_symlink():
        raise VaultStoreError("E_HALT_SECURITY", "工作台说明不能是符号链接", exit_code=2)
    if not readme.exists():
        _write_noexec_text(readme, initial_workbench_readme(domain_label))


def sync_vault_layout_dirs(vault_dir: Path) -> int:
    """Incrementally create any newly declared layout.yaml dirs inside an existing Vault (non-destructive)."""
    spec = _load_layout_spec_safe()
    gitkeeps = set(spec["gitkeep_dirs"])
    v_res = vault_dir.resolve()
    added = 0
    for sub in spec["dirs"]:
        cur = vault_dir
        for part in PurePosixPath(sub).parts:
            cur = cur / part
            if cur.exists() and cur.is_symlink():
                raise VaultStoreError("E_HALT_SECURITY", "拒绝在包含符号链接的目录中同步骨架", exit_code=2)
        target = vault_dir / PurePosixPath(sub)
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
            if v_res not in target.resolve().parents:
                raise VaultStoreError("E_PARAM_INVALID", "目录同步越界出 Vault 根目录", exit_code=2)
            added += 1
        if sub in gitkeeps and target.is_dir() and not (target / ".gitkeep").exists():
            _write_noexec_text(target / ".gitkeep", "")
    return added


def init_vault_tree(
    workspace_root: Path,
    rel_path: str,
    *,
    title: str,
    obsidian_base: bool = True,
    default_mode: str = "auto",
) -> Dict[str, Any]:
    vault_dir = resolve_safe_vault_dir(workspace_root, rel_path)
    if vault_dir.exists() and any(vault_dir.iterdir()):
        raise VaultStoreError("E_PARAM_CONFLICT", f"目录 {rel_path} 已存在且非空", exit_code=1)
    spec = _load_layout_spec_safe()
    subdirs = spec["dirs"]
    gitkeeps = set(spec["gitkeep_dirs"])
    for sub in subdirs:
        sub_dir = vault_dir / sub
        sub_dir.mkdir(parents=True, exist_ok=True)
        if sub in gitkeeps:
            _write_noexec_text(sub_dir / ".gitkeep", "")

    init_empty_workbench(vault_dir / "20-AI-Zone" / "30-Workbench")

    manifest_body = (
        f"---\n"
        f"doc_type: manifest\n"
        f'title: "{title} · AI-Zone 总控台账"\n'
        f'summary: "公共母港自治域总控台账"\n'
        f"status: active\n"
        f"tags: [Manifest, AI-Zone]\n"
        f"updated: {_today_iso()}\n"
        f"default_mode: {default_mode}\n"
        f"project_id: ai-zone\n"
        f"private: false\n"
        f"---\n\n"
        f"# {title} · AI-Zone 总控台账\n"
    )
    _write_noexec_text(vault_dir / "20-AI-Zone" / "Manifest-AI-Zone.md", manifest_body)

    base_files: List[str] = []
    if obsidian_base:
        base_files = sync_obsidian_bases(vault_dir)
    template_files = sync_obsidian_templates(vault_dir)
    return {
        "rel_path": rel_path,
        "subdirs_created": len(subdirs),
        "obsidian_base_files": base_files,
        "template_files": template_files,
    }


_FORBIDDEN_IMPORT_NAMES = frozenset({".vault_kek", ".local", "app_core", "kek.key", ".env"})
_ISOMORPHIC_MARKERS = ("10-Index", "05-Journal", "30-Projects", "90-System", "99-Assets", "00-DropZone")


def _safe_child_vault_dir(workspace_root: Path, folder_name: str) -> Path:
    if (
        not isinstance(folder_name, str)
        or not folder_name
        or "/" in folder_name
        or "\\" in folder_name
        or folder_name in (".", "..")
        or folder_name.startswith(".")
        or "\x00" in folder_name
    ):
        raise VaultStoreError("E_PARAM_INVALID", "非法待导入 Vault 目录名", exit_code=1)
    vol_root = (workspace_root / "Volumes" / "GewuAtlas-data").resolve()
    cand = workspace_root / "Volumes" / "GewuAtlas-data" / folder_name
    if cand.is_symlink():
        raise VaultStoreError("E_HALT_SECURITY", "拒绝导入符号链接 Vault 目录", exit_code=2)
    resolved = cand.resolve()
    if resolved.parent != vol_root or not resolved.is_dir():
        raise VaultStoreError("E_NOT_FOUND", "Volumes/GewuAtlas-data 下未找到目标 Vault 目录", exit_code=1)
    return resolved


def is_isomorphic_vault_dir(vault_dir: Path) -> bool:
    if not vault_dir.is_dir() or vault_dir.is_symlink():
        return False
    try:
        has_content = any(True for _ in vault_dir.iterdir())
    except OSError:
        return False
    if not has_content:
        return False
    ai_zone = vault_dir / "20-AI-Zone"
    if not ai_zone.is_dir() or ai_zone.is_symlink():
        return False
    if any((vault_dir / m).is_dir() and not (vault_dir / m).is_symlink() for m in _ISOMORPHIC_MARKERS):
        return True
    return any((ai_zone / sub).is_dir() and not (ai_zone / sub).is_symlink() for sub in ("20-Memory", "30-Workbench", "10-Artifacts"))


def validate_import_vault_safety(vault_dir: Path) -> None:
    if not vault_dir.is_dir() or vault_dir.is_symlink():
        raise VaultStoreError("E_HALT_SECURITY", "拒绝导入不存在或符号链接目录", exit_code=2)
    for cur_root, dirs, files in os.walk(vault_dir, followlinks=False):
        cur = Path(cur_root)
        if cur.is_symlink():
            raise VaultStoreError("E_HALT_SECURITY", "拒绝导入包含符号链接的 Vault 目录", exit_code=2)
        for d in dirs:
            dp = cur / d
            if dp.is_symlink():
                raise VaultStoreError("E_HALT_SECURITY", "拒绝导入包含符号链接的 Vault 目录", exit_code=2)
            if d in _FORBIDDEN_IMPORT_NAMES:
                raise VaultStoreError("E_HALT_SECURITY", f"待导入目录包含禁止带入的运行态/密钥项: {d}", exit_code=2)
        for f in files:
            fp = cur / f
            if fp.is_symlink():
                raise VaultStoreError("E_HALT_SECURITY", "拒绝导入包含符号链接文件的 Vault 目录", exit_code=2)
            if f in _FORBIDDEN_IMPORT_NAMES:
                raise VaultStoreError("E_HALT_SECURITY", f"待导入目录包含禁止带入的运行态/密钥项: {f}", exit_code=2)


def discover_unregistered_isomorphic_vaults(
    workspace_root: Path,
    registered_vault_ids: Iterable[str],
    registered_rel_paths: Iterable[str] = (),
) -> List[Path]:
    vol_root = workspace_root / "Volumes" / "GewuAtlas-data"
    if not vol_root.is_dir() or vol_root.is_symlink():
        return []
    reg_ids = {str(x).strip() for x in registered_vault_ids if str(x).strip()}
    reg_names = {PurePosixPath(str(p)).name for p in registered_rel_paths if str(p).strip()}
    candidates: List[Path] = []
    for child in sorted(vol_root.iterdir(), key=lambda p: p.name):
        if child.name.startswith(".") or child.is_symlink() or not child.is_dir():
            continue
        if child.name in reg_ids or child.name in reg_names:
            continue
        if is_isomorphic_vault_dir(child):
            candidates.append(child)
    return candidates


def _read_existing_vault_title(vault_dir: Path) -> Optional[str]:
    mf = vault_dir / "20-AI-Zone" / "Manifest-AI-Zone.md"
    if not mf.is_file() or mf.is_symlink():
        return None
    try:
        meta, _, has_fm = extract_frontmatter(mf.read_text(encoding="utf-8", errors="replace"))
        if has_fm and meta.get("title"):
            raw = str(meta["title"]).strip()
            cleaned = re.sub(r"\s*·\s*AI-Zone\s*总控台账$", "", raw).strip()
            if cleaned:
                return cleaned
    except Exception:
        return None
    return None


def adopt_vault_tree(
    workspace_root: Path,
    candidate_name: str,
    *,
    title: Optional[str] = None,
    obsidian_base: bool = True,
    default_mode: str = "auto",
    seal_rename: bool = False,
    taken_vault_ids: Iterable[str] = (),
) -> Dict[str, Any]:
    cand_dir = _safe_child_vault_dir(workspace_root, candidate_name)
    if not is_isomorphic_vault_dir(cand_dir):
        raise VaultStoreError(
            "E_PARAM_INVALID",
            "目标目录不符合 GewuAtlas 同构骨架特征（缺少 20-AI-Zone 及核心分区），拒绝就地接管",
            exit_code=1,
        )
    validate_import_vault_safety(cand_dir)

    taken = {str(x).strip() for x in taken_vault_ids if str(x).strip()}
    if seal_rename:
        vid = f"vlt-{secrets.token_hex(4)}"
        while vid in taken or (cand_dir.parent / vid).exists():
            vid = f"vlt-{secrets.token_hex(4)}"
    else:
        if not VAULT_ID_RE.fullmatch(candidate_name) or candidate_name in RESERVED_VAULT_IDS:
            raise VaultStoreError(
                "E_PARAM_INVALID",
                "待导入目录名含非 [a-z0-9_-] 字符或为保留字；请改用合法目录名，或传入 --seal_rename true 自动盲化重命名后接管",
                exit_code=1,
            )
        vid = candidate_name

    if vid in taken:
        raise VaultStoreError("E_PARAM_CONFLICT", "目标 Vault 已在 KEK 注册库中存在", exit_code=1)

    rel_path = make_vault_rel_path(vid)
    sealed_old_rel = f"{VOLUME_REL_ROOT}/{candidate_name}"
    if seal_rename and cand_dir.name != vid:
        dst_dir = cand_dir.parent / vid
        if dst_dir.exists() or dst_dir.is_symlink():
            raise VaultStoreError("E_PARAM_CONFLICT", "盲化目标槽位已存在", exit_code=1)
        os.replace(cand_dir, dst_dir)
        vault_dir = dst_dir
    else:
        vault_dir = resolve_safe_vault_dir(workspace_root, rel_path)

    resolved_title = (title or "").strip() or _read_existing_vault_title(vault_dir) or "已导入知识库"
    added_layout_dirs = sync_vault_layout_dirs(vault_dir)
    init_empty_workbench(vault_dir / "20-AI-Zone" / "30-Workbench")

    mf = vault_dir / "20-AI-Zone" / "Manifest-AI-Zone.md"
    if not mf.exists():
        manifest_body = (
            f"---\n"
            f"doc_type: manifest\n"
            f'title: "{resolved_title} · AI-Zone 总控台账"\n'
            f'summary: "公共母港自治域总控台账"\n'
            f"status: active\n"
            f"tags: [Manifest, AI-Zone]\n"
            f"updated: {_today_iso()}\n"
            f"default_mode: {default_mode}\n"
            f"project_id: ai-zone\n"
            f"private: false\n"
            f"---\n\n"
            f"# {resolved_title} · AI-Zone 总控台账\n"
        )
        _write_noexec_text(mf, manifest_body)

    synced_bases: List[str] = []
    preserved_bases: List[str] = []
    if obsidian_base:
        synced_bases, preserved_bases = sync_obsidian_bases_detailed(vault_dir, overwrite=False)

    return {
        "vault_id": vid,
        "rel_path": rel_path,
        "old_rel_path": sealed_old_rel,
        "seal_renamed": bool(seal_rename and candidate_name != vid),
        "title": resolved_title,
        "vault_dir": vault_dir,
        "added_layout_dirs_count": added_layout_dirs,
        "synced_base_files": synced_bases,
        "preserved_base_files_count": len(preserved_bases),
    }


def rename_vault_tree(workspace_root: Path, old_rel_path: str, new_rel_path: str) -> None:
    old_dir = resolve_safe_vault_dir(workspace_root, old_rel_path)
    new_dir = resolve_safe_vault_dir(workspace_root, new_rel_path)
    if not old_dir.is_dir():
        raise VaultStoreError("E_NOT_FOUND", f"原 Vault 目录不存在: {old_rel_path}", exit_code=1)
    if new_dir.exists():
        raise VaultStoreError("E_PARAM_CONFLICT", f"目标 Vault 目录已存在: {new_rel_path}", exit_code=1)
    os.replace(old_dir, new_dir)


def trash_vault_tree(workspace_root: Path, rel_path: str, vault_id: str) -> str:
    src_dir = resolve_safe_vault_dir(workspace_root, rel_path)
    trash_root = workspace_root / "Volumes" / "GewuAtlas-data" / ".trash"
    trash_root.mkdir(parents=True, exist_ok=True)
    stamp = _now_stamp()
    name = f"{vault_id}.{stamp}"
    dest_dir = trash_root / name
    if dest_dir.exists() or dest_dir.is_symlink():
        name = f"{vault_id}.{stamp}.{secrets.token_hex(3)}"
        dest_dir = trash_root / name
    if src_dir.is_dir():
        shutil.move(str(src_dir), str(dest_dir))
    return f"{VOLUME_REL_ROOT}/.trash/{name}"


def restore_vault_tree(workspace_root: Path, rel_path: str, trash_rel_path: str | None) -> bool:
    if not trash_rel_path:
        raise VaultStoreError("E_NOT_FOUND", "缺少回收槽记录，无法从隔离区恢复 Vault", exit_code=1)
    p = PurePosixPath(trash_rel_path)
    if p.is_absolute() or ".." in p.parts or len(p.parts) != 4 or p.parts[:3] != ("Volumes", "GewuAtlas-data", ".trash"):
        raise VaultStoreError("E_SECURITY_VIOLATION", "非法 Vault 回收槽相对路径", exit_code=2)
    vol_root = (workspace_root / "Volumes" / "GewuAtlas-data").resolve()
    td = (workspace_root / p).resolve()
    if vol_root not in td.parents or td.is_symlink() or not td.is_dir():
        raise VaultStoreError("E_NOT_FOUND", "回收槽中的 Vault 数据目录不存在，无法恢复", exit_code=1)
    dst_dir = resolve_safe_vault_dir(workspace_root, rel_path)
    if dst_dir.exists() or dst_dir.is_symlink():
        raise VaultStoreError("E_PARAM_CONFLICT", "目标 Vault 槽位已被占用，无法恢复覆盖", exit_code=1)
    dst_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(td), str(dst_dir))
    return True


def purge_vault_tree(workspace_root: Path, rel_path: str, trash_rel_path: str | None = None) -> None:
    # Whole-vault purge has no retention-safe recovery contract. Keep the
    # compatibility function fail-closed, including direct internal callers.
    raise VaultStoreError(
        "E_HALT_VAULT_PURGE_DISABLED",
        "整库硬清除已停用；仅支持保留内容的软删除与恢复，禁止重试。",
        exit_code=2,
    )


def inspect_vault_tree(workspace_root: Path, rel_path: str) -> Dict[str, Any]:
    vault_dir = resolve_safe_vault_dir(workspace_root, rel_path)
    subdirs = get_vault_subdirs()
    exists = vault_dir.is_dir()
    present_subdirs = 0
    base_files: List[str] = []
    if exists:
        for sub in subdirs:
            if (vault_dir / sub).is_dir():
                present_subdirs += 1
        idx = vault_dir / "10-Index"
        if idx.is_dir():
            base_files = sorted(p.name for p in idx.glob("*.base") if p.is_file())
    return {
        "rel_path": rel_path,
        "exists_on_disk": exists,
        "present_subdirs": present_subdirs,
        "expected_subdirs": len(subdirs),
        "obsidian_base_count": len(base_files),
        "obsidian_base_files": base_files,
    }
