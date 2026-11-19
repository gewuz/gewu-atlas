#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""archive_month.py - 98-Archives 按月分目录共享工具。

身魂分离冷归档统一落点约定：所有 98-Archives 舱（全局 / 各域内）按月
分子目录，一个月一个目录，月标签 YYMM（2026-10 -> 2610, 2027-01 -> 2701）。
原件冷归档不参与双链短链接寻址（original_file 为纯文本契约），按月
分层不影响检索与引用，仅便于人工翻阅与按月打包移出库区。
"""
from datetime import datetime
from pathlib import Path


def month_tag() -> str:
    """返回当月标签 (YYMM)。"""
    return datetime.now().strftime("%y%m")


def monthly_archive_dir(base) -> Path:
    """98-Archives 基座目录追加当月子目录 (base + YYMM)。"""
    return Path(base) / month_tag()
