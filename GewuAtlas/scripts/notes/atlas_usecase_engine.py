#!/usr/bin/env python3
"""Usecase playbook manager for GewuAtlas (`GewuAtlas/usecase-for-agent/`).

Supports CRUD (list / get / match / upsert / delete) on structured Agent experience cards
and automatically synchronizes `usecase-for-agent/README.md` index table.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from notes.atlas_yamlmini import extract_frontmatter, fm_scalar
from storage.vault_kek_store import VaultStoreError

_UC_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$")
_INDEX_BEGIN = "<!-- usecase-index:begin -->"
_INDEX_END = "<!-- usecase-index:end -->"


def _uc_root(app_dir: Path) -> Path:
    d = app_dir / "usecase-for-agent"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _parse_uc_file(path: Path) -> Dict[str, Any]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    fm, body, _ = extract_frontmatter(raw)
    uc_id = str(fm.get("uc_id") or path.stem).strip()
    title = str(fm.get("title") or uc_id).strip()
    scenarios = str(fm.get("scenarios") or "").strip()
    tags_raw = fm.get("tags") or []
    if isinstance(tags_raw, str):
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    elif isinstance(tags_raw, list):
        tags = [str(t).strip() for t in tags_raw if str(t).strip()]
    else:
        tags = []
    updated = str(fm.get("updated") or "").strip()
    updated_by = str(fm.get("updated_by") or "ai").strip()
    return {
        "uc_id": uc_id,
        "file_name": path.name,
        "title": title,
        "scenarios": scenarios,
        "tags": tags,
        "updated": updated,
        "updated_by": updated_by,
        "body": body.strip(),
        "raw": raw,
    }


def sync_usecase_readme(app_dir: Path) -> Dict[str, Any]:
    root = _uc_root(app_dir)
    cards: List[Dict[str, Any]] = []
    for p in sorted(root.glob("*.md")):
        if p.name.lower() == "readme.md":
            continue
        cards.append(_parse_uc_file(p))

    table_lines = [
        _INDEX_BEGIN,
        "| 经验卡 ID (`uc_id`) | 标题 | 适用场景与触发词 | 标签 | 最近更新 |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    for c in cards:
        tags_str = ", ".join(f"`{t}`" for t in c["tags"]) if c["tags"] else "-"
        table_lines.append(
            f"| `{c['uc_id']}` | [{c['title']}]({c['file_name']}) | {c['scenarios'] or '-'} | {tags_str} | {c['updated'] or '-'} |"
        )
    table_lines.append(_INDEX_END)
    table_block = "\n".join(table_lines)

    readme_path = root / "README.md"
    if readme_path.is_file():
        cur = readme_path.read_text(encoding="utf-8", errors="replace")
        if _INDEX_BEGIN in cur and _INDEX_END in cur:
            pre = cur.split(_INDEX_BEGIN, 1)[0]
            post = cur.split(_INDEX_END, 1)[1]
            new_readme = pre + table_block + post
            readme_path.write_text(new_readme, encoding="utf-8")
            return {"count": len(cards), "cards": cards}

    default_readme = (
        "# Agent 实战经验路书（usecase-for-agent）\n\n"
        "> **给 Agent 的先行指引**：本目录存放历次真实任务沉淀下来的**高效率避坑路书与高分交付配方（Usecase Playbooks）**。\n"
        "> - **开工前（读经验）**：处理联网入库、原子萃取、多篇专题研究或遇到异常卡壳时，先阅读下方索引表（或调 `./gewu atlas --action usecase --mode match --query \"<关键词>\"`），直接复用前人验证过的「损耗 → 对策」与「黄金流水线」，可省去 30%~50% 的交互往返。\n"
        "> - **复盘时（写心得）**：当任务出现重试波折、或人类提示「记录本次心得 / 生成 usecase」时，调用 `./gewu atlas --action usecase --mode upsert` 将「高频损耗 → 下次直通对策 + 质量配方」沉淀或合并到对应卡片中。\n\n"
        "## 实战 Usecase 索引表\n\n"
        f"{table_block}\n\n"
        "## 标准三段式记录规范\n\n"
        "1. **`## 1. 适用场景与触发信号`**：简明列出何时应查阅本卡。\n"
        "2. **`## 2. 高频损耗 → 直通对策`**：采用 `损耗点 | 上次表现 | 下次直通做法` 对比表，只写可执行命令与参数边界，不写冗长检讨。\n"
        "3. **`## 3. 黄金流水线与高分交付配方`**：给出最少步数的算子组合序列与冲刺 85+ 分的质量锚点。\n"
    )
    readme_path.write_text(default_readme, encoding="utf-8")
    return {"count": len(cards), "cards": cards}


def usecase_list(app_dir: Path) -> Dict[str, Any]:
    synced = sync_usecase_readme(app_dir)
    items = [
        {
            "uc_id": c["uc_id"],
            "file_name": c["file_name"],
            "title": c["title"],
            "scenarios": c["scenarios"],
            "tags": c["tags"],
            "updated": c["updated"],
            "updated_by": c["updated_by"],
        }
        for c in synced["cards"]
    ]
    return {"count": len(items), "usecases": items, "readme_rel": "GewuAtlas/usecase-for-agent/README.md"}


def usecase_get(app_dir: Path, uc_id: str) -> Dict[str, Any]:
    root = _uc_root(app_dir)
    clean_id = (uc_id or "").strip()
    if clean_id.endswith(".md"):
        clean_id = clean_id[:-3]
    if not clean_id or not _UC_ID_RE.match(clean_id):
        raise VaultStoreError("E_PARAM_INVALID", "usecase get 需要合法的 --uc_id（如 UC-01-web-ingest-and-extract）", exit_code=1)
    target = root / f"{clean_id}.md"
    if not target.is_file():
        # Try case-insensitive or prefix match
        matches = [p for p in root.glob("*.md") if p.name.lower() != "readme.md" and p.stem.lower().startswith(clean_id.lower())]
        if len(matches) == 1:
            target = matches[0]
        else:
            raise VaultStoreError("E_NOT_FOUND", f"未找到 Usecase 经验卡: {clean_id}", exit_code=1)
    parsed = _parse_uc_file(target)
    return {
        "uc_id": parsed["uc_id"],
        "title": parsed["title"],
        "scenarios": parsed["scenarios"],
        "tags": parsed["tags"],
        "updated": parsed["updated"],
        "updated_by": parsed["updated_by"],
        "content": parsed["body"],
    }


def usecase_match(app_dir: Path, query: str, top_k: int = 3) -> Dict[str, Any]:
    synced = sync_usecase_readme(app_dir)
    q_terms = [t.lower() for t in re.split(r"[\s,;，；/|]+", (query or "").strip()) if t.strip()]
    scored: List[tuple[int, Dict[str, Any]]] = []
    for c in synced["cards"]:
        hay_title = (c["uc_id"] + " " + c["title"] + " " + c["scenarios"] + " " + " ".join(c["tags"])).lower()
        hay_body = c["body"].lower()
        score = 0
        if not q_terms:
            score = 1
        else:
            for qt in q_terms:
                if qt in hay_title:
                    score += 5
                if qt in hay_body:
                    score += 2
        if score > 0:
            scored.append((score, c))
    scored.sort(key=lambda x: (-x[0], x[1]["uc_id"]))
    hits = [
        {
            "uc_id": c["uc_id"],
            "title": c["title"],
            "scenarios": c["scenarios"],
            "tags": c["tags"],
            "score": sc,
            "content": c["body"],
        }
        for sc, c in scored[: max(1, int(top_k or 3))]
    ]
    return {"query": query, "count": len(hits), "hits": hits}


def usecase_upsert(
    app_dir: Path,
    *,
    uc_id: str,
    title: str,
    scenarios: str = "",
    tags: Any = None,
    content: str = "",
    by: str = "ai",
) -> Dict[str, Any]:
    root = _uc_root(app_dir)
    clean_id = (uc_id or "").strip()
    if clean_id.endswith(".md"):
        clean_id = clean_id[:-3]
    if not clean_id or not _UC_ID_RE.match(clean_id):
        raise VaultStoreError("E_PARAM_INVALID", "usecase upsert 需要合法的 --uc_id（仅限字母数字与连字符，如 UC-03-topic-name）", exit_code=1)
    target = root / f"{clean_id}.md"
    existed = target.is_file()
    prior = _parse_uc_file(target) if existed else {}

    eff_title = (title or prior.get("title") or clean_id).strip()
    eff_scenarios = (scenarios or prior.get("scenarios") or "通用任务提效与避坑").strip()
    if isinstance(tags, str) and tags.strip():
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    elif isinstance(tags, list) and tags:
        tag_list = [str(t).strip() for t in tags if str(t).strip()]
    else:
        tag_list = prior.get("tags") or ["Usecase", "Playbook"]

    eff_body = (content or prior.get("body") or "").strip().replace("/Volumes/", "/<受控卷>/").replace("Volumes/", "<受控卷>/").replace("GewuAtlas-data", "<数据卷>")
    if not eff_body:
        raise VaultStoreError("E_PARAM_MISSING", "usecase upsert 需要非空 --content（包含损耗对策表与黄金流水线）", exit_code=1)

    today = date.today().isoformat()
    actor = (by or "ai").strip() or "ai"
    tags_yaml = "[" + ", ".join(f'"{fm_scalar(t)}"' for t in tag_list) + "]"
    md_text = (
        "---\n"
        'doc_type: "usecase"\n'
        f'uc_id: "{fm_scalar(clean_id)}"\n'
        f'title: "{fm_scalar(eff_title)}"\n'
        f'scenarios: "{fm_scalar(eff_scenarios)}"\n'
        f"tags: {tags_yaml}\n"
        f'updated: "{today}"\n'
        f'updated_by: "{fm_scalar(actor)}"\n'
        "---\n\n"
        f"{eff_body}\n"
    )
    target.write_text(md_text, encoding="utf-8")
    sync_usecase_readme(app_dir)
    return {
        "uc_id": clean_id,
        "title": eff_title,
        "scenarios": eff_scenarios,
        "tags": tag_list,
        "updated": today,
        "updated_by": actor,
        "operation": "updated" if existed else "created",
    }


def usecase_delete(app_dir: Path, uc_id: str) -> Dict[str, Any]:
    root = _uc_root(app_dir)
    clean_id = (uc_id or "").strip()
    if clean_id.endswith(".md"):
        clean_id = clean_id[:-3]
    if not clean_id or not _UC_ID_RE.match(clean_id):
        raise VaultStoreError("E_PARAM_INVALID", "usecase delete 需要合法的 --uc_id", exit_code=1)
    target = root / f"{clean_id}.md"
    if not target.is_file():
        raise VaultStoreError("E_NOT_FOUND", f"未找到待删除的 Usecase: {clean_id}", exit_code=1)
    target.unlink()
    sync_usecase_readme(app_dir)
    return {"uc_id": clean_id, "deleted": True}
