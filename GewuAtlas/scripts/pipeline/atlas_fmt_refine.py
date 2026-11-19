"""Stateless, handle-scoped primitives for skill-led semantic refinement.

No completion state machine: the agent owns judgment and reports evidence.
"""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import re
from typing import Any, Dict
from notes.atlas_note_engine import _atomic_write_md
from storage.vault_kek_store import VaultStoreError
from converters.chunk_window import split_chunks_by_section, build_sliding_windows


def _note(vault_dir, vault_ref, note_ref, note_reg):
    rec = note_reg.resolve_handle(vault_ref, note_ref)
    rel = str(rec['note_rel_path'])
    path = vault_dir / rel
    if rel.startswith('05-Journal/') or path.is_symlink() or not path.is_file():
        raise VaultStoreError('E_PARAM_INVALID', '精加工仅接受存在的非日记卡片', exit_code=1)
    text = path.read_text(encoding='utf-8')
    match = re.match(r'\A---\r?\n.*?\r?\n---(?:\r?\n|\Z)', text, re.S)
    head = text[:match.end()] if match else ''
    return path, text, head, text[len(head):]


def read_window(*, vault_dir, vault_ref, note_ref, note_reg, offset=0, max_chars=3000):
    _, text, _, body = _note(vault_dir, vault_ref, note_ref, note_reg)
    if offset < 0 or offset > len(body) or not 500 <= max_chars <= 12000:
        raise VaultStoreError('E_PARAM_INVALID', 'offset 越界或 max_chars 不在 500..12000', exit_code=1)
    # R6 section-aware splitting protects tables/fences; cursor is in body characters.
    chunks = split_chunks_by_section(body[offset:], target_size=max_chars,
                                    min_cut=max(250, max_chars*2//3), max_cut=max_chars*4//3)
    windows = build_sliding_windows(chunks)
    target = windows[0]['target_chunk'] if windows else ''
    # The legacy splitter strips whitespace: match to the original bytes before exposing a patch range.
    start = body.find(target, offset) if target else len(body)
    if start < 0:
        raise VaultStoreError('E_FMT_WINDOW', '切片不能精确映射原文；未改写', exit_code=1)
    end = start + len(target)
    return {'vault_ref':vault_ref,'note_ref':note_ref,'expected_sha256':hashlib.sha256(text.encode()).hexdigest(),
            'offset':start,'end_offset':end,'next_offset':end,'total_chars':len(body),
            'remaining_windows':len(windows),'content':target,
            'prev_context':body[max(0,start-400):start],'next_preview':body[end:end+400],
            'done':not body[end:].strip(),'processing_stage':'agent_review_required'}


def apply_window(*, vault_dir, vault_ref, note_ref, note_reg, offset, end_offset, content, expected_sha256):
    path,text,head,body = _note(vault_dir,vault_ref,note_ref,note_reg)
    if not expected_sha256 or hashlib.sha256(text.encode()).hexdigest()!=expected_sha256:
        raise VaultStoreError('E_PARAM_CONFLICT','原文已变化；重新读取当前窗口后再提交',exit_code=1)
    if not 0 <= offset < end_offset <= len(body) or not str(content).strip():
        raise VaultStoreError('E_PARAM_INVALID','窗口范围或精修正文无效',exit_code=1)
    # The gateway serializes each write. Preserve frontmatter and every byte outside the chosen span.
    updated=head+body[:offset]+content+body[end_offset:]
    _atomic_write_md(path,updated)
    return {'vault_ref':vault_ref,'note_ref':note_ref,'next_offset':offset+len(content),
            'expected_sha256':hashlib.sha256(updated.encode()).hexdigest(),'processing_stage':'agent_review_required'}


def note_assets(*,vault_dir,vault_ref,note_ref,note_reg,ws_root,file_name=None):
    _,_,_,body=_note(vault_dir,vault_ref,note_ref,note_reg)
    referenced=[]
    for target in re.findall(r'!\[\[([^\]]+)\]\]',body):
        target=target.split('|',1)[0].split('#',1)[0]
        if Path(target).name==target and target not in referenced:referenced.append(target)
    assets=[]
    base=vault_dir/'99-Assets'/'10-Attachments'
    for name in referenced:
        p=base/name
        if p.is_file() and not p.is_symlink():
            assets.append({'file_name':name,'bytes':p.stat().st_size})
    if not file_name:return {'note_ref':note_ref,'assets':assets,'count':len(assets)}
    if file_name not in [a['file_name'] for a in assets]:
        raise VaultStoreError('E_PARAM_INVALID','只能导出当前卡片实际引用的附件',exit_code=1)
    source=base/file_name
    if source.stat().st_size>50*1024*1024:
        raise VaultStoreError('E_PARAM_INVALID','附件超过50MiB，不导出',exit_code=1)
    data=source.read_bytes();digest=hashlib.sha256(data).hexdigest()
    # Explicit public preview; never return a managed vault path.
    dest_dir=ws_root/'outputs'/'atlas-previews'
    for p in (ws_root/'outputs',dest_dir):
        if p.is_symlink():raise VaultStoreError('E_HALT_SECURITY','预览输出目录不能是链接',exit_code=2)
    dest_dir.mkdir(parents=True,exist_ok=True)
    name=digest+source.suffix.lower();dest=dest_dir/name
    try:
        fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
        if dest.is_symlink() or hashlib.sha256(dest.read_bytes()).hexdigest()!=digest:
            raise VaultStoreError('E_PARAM_CONFLICT','预览文件冲突；未覆盖',exit_code=1)
    else:
        with os.fdopen(fd,'wb') as f:f.write(data)
    return {'note_ref':note_ref,'file_name':file_name,'preview_file':'outputs/atlas-previews/'+name,'sha256':digest,'bytes':len(data)}
