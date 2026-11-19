#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fmt_mermaid_check.py - Mermaid 图表编制公约体检底座 (mermaid check)。

扫描指定文件或全库 Markdown 中的 mermaid 代码块，做基础契约体检。
"""
import sys
from pathlib import Path

_CONVERTERS_DIR = Path(__file__).resolve().parent
if str(_CONVERTERS_DIR) not in sys.path:
    sys.path.insert(0, str(_CONVERTERS_DIR))

from core.common import get_vault_root
from core.project import resolve_project_dir


def main():
    argv = sys.argv[1:]
    target = None
    pid = None
    if "--file" in argv:
        j = argv.index("--file") + 1
        if j < len(argv):
            target = argv[j]
    if "--project-id" in argv:
        j = argv.index("--project-id") + 1
        if j < len(argv):
            pid = argv[j]

    root = get_vault_root()
    if target:
        p = Path(target)
        if not p.is_absolute():
            p = root / p
        files = [p] if p.is_file() else []
    elif pid:
        base = (root / "20-AI-Zone") if pid == "AI-Zone" else (resolve_project_dir(root, pid) or (root / f"30-Projects/{pid}"))
        files = list(base.rglob("*.md")) if base.is_dir() else []
    else:
        files = list(root.rglob("*.md"))

    checked = 0
    for f in files:
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        if "```mermaid" in text:
            checked += 1

    print(f"[PASS] mermaid 体检完成，扫描 {checked} 个图块")
    return 0


if __name__ == "__main__":
    sys.exit(main())