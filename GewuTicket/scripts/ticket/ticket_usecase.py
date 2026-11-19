"""Usecase playbook CRUD engine for GewuTicket (`GewuTicket/usecase-for-agent/`)."""

from __future__ import annotations

from datetime import datetime, timezone
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List


def _now_date() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".uc-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _parse_card(path: Path) -> Dict[str, Any]:
    text = path.read_text("utf-8", errors="replace")
    title = path.stem
    summary = ""
    tags: List[str] = []
    updated = ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# ") and title == path.stem:
            title = s[2:].strip()
        elif s.startswith("- **适用场景**：") or s.startswith("**适用场景**："):
            summary = s.split("：", 1)[1].strip()
        elif s.startswith("scenarios:"):
            summary = s.split(":", 1)[1].strip().strip('"\'')
        elif s.startswith("title:") and title == path.stem:
            title = s.split(":", 1)[1].strip().strip('"\'')
        elif s.startswith("- **标签**：") or s.startswith("**标签**："):
            tags = [t.strip() for t in re.split(r"[,，\s]+", s.split("：", 1)[1]) if t.strip()]
        elif s.startswith("tags:"):
            raw_t = s.split(":", 1)[1].strip().strip("[]")
            tags = [t.strip().strip('"\'') for t in re.split(r"[,，]+", raw_t) if t.strip()]
        elif s.startswith("- **更新日期**：") or s.startswith("**更新日期**："):
            updated = s.split("：", 1)[1].strip()
        elif s.startswith("updated:"):
            updated = s.split(":", 1)[1].strip().strip('"\'')
    return {
        "uc_id": path.stem,
        "file_name": path.name,
        "title": title,
        "scenarios": summary,
        "tags": tags,
        "updated": updated,
        "content": text,
    }


def sync_readme(app_dir: Path) -> None:
    uc_dir = app_dir / "usecase-for-agent"
    cards = [
        _parse_card(p)
        for p in sorted(uc_dir.glob("*.md"))
        if p.name.lower() != "readme.md"
    ]
    rows = [
        f"| `{c['uc_id']}` | [{c['title']}]({c['file_name']}) | {c['scenarios'] or '工单协同实战经验'} | {', '.join(f'`{t}`' for t in c['tags']) or '-'} | {c['updated'] or '-'} |"
        for c in cards
    ]
    readme = "\n".join([
        "# GewuTicket Agent 实战经验路书（`usecase-for-agent`）",
        "",
        "> **给 Agent 的先行指引**：本目录存放历次真实协同任务沉淀下来的**避坑路书与标准流水线**。",
        "> - **开工前（读经验）**：处理 `SYS` 系统变更审批、双维审阅防重放、`ro`/`shadow-rw` 挂载或遇到胶囊熔断前，先检索下方索引（或调 `./gewu ticket --action usecase --mode match --query \"<关键词>\"`）。",
        "> - **复盘时（写心得）**：遇参数返工、SHA 冲突或新避坑配方时，调 `./gewu ticket --action usecase --mode upsert` 沉淀并自动刷新本索引。",
        "",
        "## 实战 Usecase 索引表",
        "",
        "<!-- usecase-index:begin -->",
        "| 经验卡 ID (`uc_id`) | 标题 | 适用场景与触发词 | 标签 | 最近更新 |",
        "| :--- | :--- | :--- | :--- | :--- |",
        *(rows or ["| （暂无） | - | - | - | - |"]),
        "<!-- usecase-index:end -->",
        "",
        "## 标准三段式记录规范",
        "",
        "1. **`## 1. 适用场景与触发信号`**：简明列出何时应查阅本卡。",
        "2. **`## 2. 高频损耗 → 直通对策`**：采用 `损耗点 | 上次表现 | 下次直通做法` 对比表，只写可执行命令与参数边界。",
        "3. **`## 3. 黄金流水线与避坑锚点`**：给出最少步数的工单动作序列与门禁防错检查项。",
        "",
    ])
    _atomic_write(uc_dir / "README.md", readme)


def usecase_list(app_dir: Path) -> Dict[str, Any]:
    uc_dir = app_dir / "usecase-for-agent"
    if not uc_dir.is_dir():
        return {"count": 0, "usecases": []}
    cards = [
        {k: v for k, v in _parse_card(p).items() if k != "content"}
        for p in sorted(uc_dir.glob("*.md"))
        if p.name.lower() != "readme.md"
    ]
    return {"count": len(cards), "usecases": cards}


def usecase_get(app_dir: Path, uc_id: str) -> Dict[str, Any]:
    uc_dir = app_dir / "usecase-for-agent"
    clean = uc_id.strip().removesuffix(".md")
    if not clean or "/" in clean or ".." in clean:
        raise ValueError(f"invalid uc_id: {uc_id!r}")
    p = uc_dir / f"{clean}.md"
    if not p.is_file():
        raise FileNotFoundError(f"usecase not found: {clean}")
    return _parse_card(p)


def usecase_match(app_dir: Path, query: str, *, top_k: int = 3) -> Dict[str, Any]:
    uc_dir = app_dir / "usecase-for-agent"
    terms = [t.lower() for t in re.split(r"[\s,，、/+]+", query.strip()) if t]
    scored = []
    if uc_dir.is_dir():
        for p in sorted(uc_dir.glob("*.md")):
            if p.name.lower() == "readme.md":
                continue
            card = _parse_card(p)
            hay = (card["uc_id"] + " " + card["title"] + " " + card["scenarios"] + " " + " ".join(card["tags"]) + " " + card["content"]).lower()
            score = sum(hay.count(t) for t in terms) if terms else 1
            if score > 0:
                scored.append((score, card))
    scored.sort(key=lambda x: (-x[0], x[1]["uc_id"]))
    hits = [c for _, c in scored[: max(1, top_k)]]
    return {"query": query, "count": len(hits), "matches": hits}


def usecase_upsert(
    app_dir: Path,
    *,
    uc_id: str,
    title: str,
    scenarios: str = "",
    tags: Any = None,
    content: str = "",
    by: str = "Antigravity",
) -> Dict[str, Any]:
    clean = uc_id.strip().removesuffix(".md")
    if not re.fullmatch(r"UC-[0-9]{2,3}-[a-z0-9-]+", clean):
        raise ValueError("uc_id must match UC-<NN>-<slug> (e.g. UC-01-sys-change-and-dual-review)")
    if not title.strip() or not content.strip():
        raise ValueError("title and content are required")
    uc_dir = app_dir / "usecase-for-agent"
    uc_dir.mkdir(parents=True, exist_ok=True)
    path = uc_dir / f"{clean}.md"
    existed = path.is_file()
    tag_list = tags if isinstance(tags, list) else [t.strip() for t in re.split(r"[,，\s]+", str(tags or "")) if t.strip()]
    md = "\n".join([
        f"# {title.strip()}",
        "",
        f"- **适用场景**：{scenarios.strip() or title.strip()}",
        f"- **标签**：{', '.join(tag_list) or 'ticket'}",
        f"- **维护者**：{by.strip() or 'Antigravity'}",
        f"- **更新日期**：{_now_date()}",
        "",
        content.strip(),
        "",
    ])
    _atomic_write(path, md)
    sync_readme(app_dir)
    return {"uc_id": clean, "operation": "updated" if existed else "created", "file_name": path.name}


def usecase_delete(app_dir: Path, uc_id: str) -> Dict[str, Any]:
    clean = uc_id.strip().removesuffix(".md")
    if not clean or "/" in clean or ".." in clean:
        raise ValueError(f"invalid uc_id: {uc_id!r}")
    uc_dir = app_dir / "usecase-for-agent"
    path = uc_dir / f"{clean}.md"
    if not path.is_file():
        raise FileNotFoundError(f"usecase not found: {clean}")
    path.unlink()
    sync_readme(app_dir)
    return {"uc_id": clean, "deleted": True}
