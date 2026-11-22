#!/usr/bin/env python3
"""Zero-dependency YAML Frontmatter parser, syntax guard, and schema validator for GewuAtlas."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


class FrontmatterError(ValueError):
    """Raised when YAML frontmatter cannot be parsed safely as a mapping."""


def _strip_comment(line: str) -> str:
    in_s = in_d = False
    escaped = False
    for i, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_d:
            escaped = True
            continue
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif ch == "#" and not in_s and not in_d and (i == 0 or line[i - 1] in (" ", "\t")):
            return line[:i].rstrip()
    return line.rstrip()


def _split_kv(txt: str) -> Tuple[str, str]:
    in_s = in_d = False
    for i, ch in enumerate(txt):
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif ch == ":" and not in_s and not in_d:
            return txt[:i].strip(), txt[i + 1 :].strip()
    return txt.strip(), ""


def _is_kv(txt: str) -> bool:
    in_s = in_d = False
    for ch in txt:
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif ch == ":" and not in_s and not in_d:
            return True
    return False


def _split_flow(inner: str) -> List[str]:
    items: List[str] = []
    buf = ""
    depth = 0
    in_s = in_d = False
    for ch in inner:
        if ch == "'" and not in_d:
            in_s = not in_s
            buf += ch
            continue
        if ch == '"' and not in_s:
            in_d = not in_d
            buf += ch
            continue
        if not in_s and not in_d:
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
            elif ch == "," and depth == 0:
                items.append(buf.strip())
                buf = ""
                continue
        buf += ch
    if buf.strip() != "":
        items.append(buf.strip())
    return items


def _parse_scalar(s: str) -> Any:
    s = s.strip()
    if s == "":
        return None
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        if s[0] == '"':
            try:
                return json.loads(s)
            except Exception:
                return s[1:-1]
        return s[1:-1].replace("''", "'")
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(x) for x in _split_flow(inner)]
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip()
        res: Dict[str, Any] = {}
        if not inner:
            return res
        for item in _split_flow(inner):
            k, v = _split_kv(item)
            res[k] = _parse_scalar(v) if v != "" else None
        return res
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~"):
        return None
    if re.fullmatch(r"[-+]?\d+", s):
        return int(s)
    if re.fullmatch(r"[-+]?\d+\.\d+", s):
        return float(s)
    return s


def _load_lines(text: str) -> List[Tuple[int, str]]:
    lines: List[Tuple[int, str]] = []
    for raw in text.splitlines():
        s = _strip_comment(raw)
        if not s.strip():
            continue
        indent = len(s) - len(s.lstrip(" "))
        lines.append((indent, s.strip()))
    return lines


def _parse(lines: List[Tuple[int, str]], idx: int, indent: int) -> Tuple[Any, int]:
    if idx >= len(lines):
        return None, idx
    txt = lines[idx][1]
    if txt == "-" or txt.startswith("- "):
        return _seq(lines, idx, indent)
    return _map(lines, idx, indent)


def _map(lines: List[Tuple[int, str]], idx: int, indent: int) -> Tuple[Dict[str, Any], int]:
    m: Dict[str, Any] = {}
    n = len(lines)
    while idx < n:
        il, txt = lines[idx]
        if il != indent or txt == "-" or txt.startswith("- "):
            break
        key, val = _split_kv(txt)
        if key in m:
            raise FrontmatterError(f"重复的 YAML 键: '{key}'")
        if re.match(r"^[|>][+-]?$", val.strip()):
            scalar_lines: List[str] = []
            base_child: Optional[int] = None
            idx += 1
            while idx < n and lines[idx][0] > indent:
                if base_child is None:
                    base_child = lines[idx][0]
                rel = max(0, lines[idx][0] - base_child)
                scalar_lines.append(" " * rel + lines[idx][1])
                idx += 1
            m[key] = "\n".join(scalar_lines) if val.startswith("|") else " ".join(scalar_lines)
        elif val == "":
            if idx + 1 < n and (
                lines[idx + 1][0] > indent
                or (lines[idx + 1][0] == indent and (lines[idx + 1][1] == "-" or lines[idx + 1][1].startswith("- ")))
            ):
                child, idx = _parse(lines, idx + 1, lines[idx + 1][0])
                m[key] = child
            else:
                m[key] = None
                idx += 1
        else:
            m[key] = _parse_scalar(val)
            idx += 1
    return m, idx


def _seq(lines: List[Tuple[int, str]], idx: int, indent: int) -> Tuple[List[Any], int]:
    arr: List[Any] = []
    n = len(lines)
    while idx < n:
        il, txt = lines[idx]
        if il != indent or (txt != "-" and not txt.startswith("- ")):
            break
        rest = txt[1:].strip()
        if rest == "":
            if idx + 1 < n and lines[idx + 1][0] > indent:
                child, idx = _parse(lines, idx + 1, lines[idx + 1][0])
                arr.append(child)
            else:
                arr.append(None)
                idx += 1
        elif _is_kv(rest):
            key_indent = indent + len(txt) - len(txt[1:].lstrip())
            key, val = _split_kv(rest)
            item: Dict[str, Any] = {}
            if val == "":
                if idx + 1 < n and lines[idx + 1][0] > key_indent:
                    child, idx = _parse(lines, idx + 1, lines[idx + 1][0])
                    item[key] = child
                else:
                    item[key] = None
                    idx += 1
            else:
                item[key] = _parse_scalar(val)
                idx += 1
            while idx < n and lines[idx][0] == key_indent and not (lines[idx][1] == "-" or lines[idx][1].startswith("- ")):
                ck, cv = _split_kv(lines[idx][1])
                if cv == "":
                    if idx + 1 < n and (
                        lines[idx + 1][0] > key_indent
                        or (lines[idx + 1][0] == key_indent and (lines[idx + 1][1] == "-" or lines[idx + 1][1].startswith("- ")))
                    ):
                        child, idx = _parse(lines, idx + 1, lines[idx + 1][0])
                        item[ck] = child
                    else:
                        item[ck] = None
                        idx += 1
                else:
                    item[ck] = _parse_scalar(cv)
                    idx += 1
            arr.append(item)
        else:
            arr.append(_parse_scalar(rest))
            idx += 1
    return arr, idx


def yaml_load(text: Optional[str]) -> Any:
    if text is None:
        return None
    lines = _load_lines(text)
    if not lines:
        return None
    val, idx = _parse(lines, 0, lines[0][0])
    if idx < len(lines):
        raise FrontmatterError(f"未消费的 YAML 内容 (第 {idx + 1} 行)")
    return val


def extract_frontmatter(content: str) -> Tuple[Dict[str, Any], str, bool]:
    if not content.startswith("---"):
        return {}, content, False
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, content, False
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, content, False
    meta = yaml_load("\n".join(lines[1:end]))
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise FrontmatterError("Frontmatter 顶层必须为映射")
    body = "\n".join(lines[end + 1 :])
    return meta, body, True


def fm_scalar(s: Any) -> str:
    flat = re.sub(r"[\r\n]+", " ", str(s)).strip()
    return flat.replace("\\", "\\\\").replace('"', '\\"')


def replace_frontmatter_field(content: str, key: str, raw_value: str) -> str:
    match = re.match(r"\A---[^\S\n]*(\r?\n)(.*?)(\r?\n)---[^\S\n]*(?=\r?\n|$)", content, re.S)
    if not match:
        return content
    nl = match.group(1)
    header = match.group(2)
    sep = ":" if raw_value.startswith(("\n", "\r\n")) else ": "
    replacement = f"{key}{sep}{raw_value}"
    pattern = re.compile(r"(?m)^" + re.escape(key) + r"[ \t]*:[^\n]*(?:\n[ \t]+[^\n]*)*")
    if pattern.search(header):
        header = pattern.sub(lambda _m: replacement, header, count=1)
    else:
        header = (header + nl + replacement) if header else replacement
    return f"---{nl}{header}{match.group(3)}---" + content[match.end() :]


def drop_frontmatter_field(content: str, key: str) -> str:
    """Remove a top-level YAML frontmatter key (including indented continuation lines) safely."""
    match = re.match(r"\A---[^\S\n]*(\r?\n)(.*?)(\r?\n)---[^\S\n]*(?=\r?\n|$)", content, re.S)
    if not match:
        return content
    nl = match.group(1)
    header = match.group(2)
    pattern = re.compile(r"(?m)^" + re.escape(key) + r"[ \t]*:[^\n]*(?:\n[ \t]+[^\n]*)*(?:\r?\n)?")
    new_header = pattern.sub("", header).strip("\r\n")
    return f"---{nl}{new_header}{match.group(3)}---" + content[match.end() :]


def allowed_fields_for_doc_type(doc_type: str) -> Set[str]:
    """Return allowed frontmatter property keys for `doc_type` from metadata.yaml."""
    schemas = load_metadata_dict().get("schemas") or {}
    schema_key = "toc" if doc_type == "slice_toc" and doc_type not in schemas else doc_type
    schema_def = schemas.get(schema_key) if isinstance(schemas, dict) else None
    if not isinstance(schema_def, dict):
        return set()
    return set((schema_def.get("required_properties") or {}).keys()) | set((schema_def.get("optional_properties") or {}).keys())


def prune_frontmatter_for_doc_type(content: str, doc_type: str) -> str:
    """Drop frontmatter fields not declared in metadata.yaml for `doc_type` to prevent unknown_field pollution."""
    allowed = allowed_fields_for_doc_type(doc_type)
    if not allowed:
        return content
    try:
        meta, _, has_fm = extract_frontmatter(content)
    except FrontmatterError:
        return content
    if not has_fm:
        return content
    out = content
    for key in list(meta.keys()):
        if key not in allowed:
            out = drop_frontmatter_field(out, str(key))
    return out


DICTS_DIR = Path(__file__).resolve().parents[2] / "dicts"
_DICT_CACHE: Dict[str, Tuple[int, int, Dict[str, Any]]] = {}


def load_dict_yaml(filename: str) -> Dict[str, Any]:
    """Load and parse a YAML dictionary file from GewuAtlas/dicts/ with stat-based cache."""
    path = DICTS_DIR / filename
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise FrontmatterError(f"字典路径不允许符号链接: {filename}")
    if not path.is_file():
        raise FrontmatterError(f"缺失权威字典文件: dicts/{filename}")
    st = path.stat()
    cached = _DICT_CACHE.get(filename)
    if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]
    parsed = yaml_load(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise FrontmatterError(f"字典顶层必须为映射: dicts/{filename}")
    _DICT_CACHE[filename] = (st.st_mtime_ns, st.st_size, parsed)
    return parsed


def load_metadata_dict() -> Dict[str, Any]:
    return load_dict_yaml("metadata.yaml")


def load_layout_dict() -> Dict[str, Any]:
    return load_dict_yaml("layout.yaml")


def load_methodology_dict() -> Dict[str, Any]:
    return load_dict_yaml("methodology.yaml")


_LAYOUT_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _validate_layout_rel_dir(raw: Any, *, allow_trash_dot: bool = False) -> str:
    if not isinstance(raw, str):
        raise FrontmatterError("layout.yaml 目录项必须为字符串")
    val = raw.strip()
    if (
        not val
        or val != raw
        or val.startswith("/")
        or val.endswith("/")
        or "\\" in val
        or "//" in val
        or _LAYOUT_CONTROL_RE.search(val)
    ):
        raise FrontmatterError(f"layout.yaml 包含非法目录路径: {raw!r}")
    parts = val.split("/")
    for idx, seg in enumerate(parts):
        if not seg or seg in (".", ".."):
            raise FrontmatterError(f"layout.yaml 包含危险路径段: {raw!r}")
        if seg.startswith(".") and not (allow_trash_dot and idx == 0 and len(parts) == 1 and seg == ".trash"):
            raise FrontmatterError(f"layout.yaml 不允许未受控隐藏目录段: {raw!r}")
    return val


def _validate_layout_dir_list(
    raw: Any,
    label: str,
    *,
    allow_trash_dot: bool = False,
) -> Tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise FrontmatterError(f"layout.yaml {label} 必须为非空列表")
    out: List[str] = []
    seen = set()
    for item in raw:
        clean = _validate_layout_rel_dir(item, allow_trash_dot=allow_trash_dot)
        if clean not in seen:
            seen.add(clean)
            out.append(clean)
    return tuple(out)


def get_vault_layout_spec(layout_dict: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Dynamically resolve and validate Vault & project fractal directory skeletons from dicts/layout.yaml."""
    layout = layout_dict if layout_dict is not None else load_layout_dict()
    vs = layout.get("vault_skeleton")
    if not isinstance(vs, dict):
        raise FrontmatterError("layout.yaml 缺失 vault_skeleton 配置")
    dirs = _validate_layout_dir_list(vs.get("dirs"), "vault_skeleton.dirs", allow_trash_dot=True)
    gitkeep_dirs = _validate_layout_dir_list(vs.get("gitkeep_dirs"), "vault_skeleton.gitkeep_dirs", allow_trash_dot=True)
    required_dirs = _validate_layout_dir_list(vs.get("required_dirs"), "vault_skeleton.required_dirs")
    skip_tops = _validate_layout_dir_list(vs.get("skip_note_scan_tops"), "vault_skeleton.skip_note_scan_tops")
    init_sk = layout.get("init_skeleton") or {}
    project_fractal_dirs = _validate_layout_dir_list(
        init_sk.get("dirs"),
        "init_skeleton.dirs",
    )
    top_map = layout.get("top_level") or {}
    tops = {
        _validate_layout_rel_dir(v, allow_trash_dot=True).split("/")[0]
        for v in top_map.values()
        if isinstance(v, str) and v.strip()
    }
    tops.update(d.split("/")[0] for d in dirs)
    tops.discard(".trash")
    return {
        "dirs": dirs,
        "gitkeep_dirs": gitkeep_dirs,
        "required_dirs": required_dirs,
        "skip_note_scan_tops": skip_tops,
        "project_fractal_dirs": project_fractal_dirs,
        "top_level_names": frozenset(tops),
    }


def _vocab_choices(vocab_name: str, fallback: Tuple[str, ...] = ()) -> Tuple[str, ...]:
    meta_dict = load_metadata_dict()
    vocab = (meta_dict.get("vocabularies") or {}).get(vocab_name) or {}
    allowed = vocab.get("allowed_values")
    if isinstance(allowed, dict) and allowed:
        return tuple(str(k) for k in allowed.keys())
    return fallback


ALLOWED_DOC_TYPES = _vocab_choices(
    "doc_type",
    ("atomic", "library", "moc", "slice", "toc", "slice_toc", "slip", "artifact", "workbench", "project", "journal", "manifest", "memo", "ticket"),
)
ALLOWED_STATUSES = _vocab_choices("status", ("active", "pending", "deprecated", "archived"))
ALLOWED_TYPES = _vocab_choices("type", ("OBJ", "SUB", "SYN", "HYP"))
ALLOWED_STAGES = _vocab_choices("stage", ("draft", "final"))
ALLOWED_READ_STATUSES = _vocab_choices("read_status", ("read", "unread"))
ALLOWED_SLIP_KINDS = _vocab_choices("slip_kind", ("task", "ingest", "search"))
ALLOWED_DEFAULT_MODES = _vocab_choices("default_mode", ("auto", "supervised"))
ALLOWED_REVIEWED_BY = _vocab_choices("reviewed_by", ("ai", "human"))
ALLOWED_SOURCE_TYPES = _vocab_choices("source_type")


def normalize_provenance_refs(value: Any) -> List[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise ValueError("证据引用必须为字符串或字符串列表")
    out: List[str] = []
    for item in value:
        item = item.strip()
        if item.startswith("[[") and item.endswith("]]"):
            item = item[2:-2]
        item = item.split("|")[0].split("#")[0].strip()
        if item.endswith(".md"):
            item = item[:-3]
        if not item or any(c in item for c in ('"', "\\", "\n", "\r", "\x00")):
            raise ValueError("非法证据引用: " + item)
        link = f"[[{item}]]"
        if link not in out:
            out.append(link)
    return out


def _parse_provenance_group_item(group: Any) -> Dict[str, Any]:
    """Accept both legacy dict groups and Obsidian-compatible flat string groups."""
    if isinstance(group, dict):
        if set(group) - {"source", "toc", "slices"}:
            raise ValueError("证据组仅允许 source、toc、slices")
        if not isinstance(group.get("source"), str) or not str(group["source"]).strip():
            raise ValueError("每个证据组必须有一个 source 字符串")
        if "toc" in group and group["toc"] is not None and not isinstance(group["toc"], str):
            raise ValueError("每个证据组 toc 必须为单值字符串")
        if "slices" in group and group["slices"] is not None and not isinstance(group["slices"], list):
            raise ValueError("证据组 slices 必须为列表")
        return {
            "source": group["source"],
            "toc": group.get("toc"),
            "slices": group.get("slices", []),
        }
    if isinstance(group, str):
        raw = group.strip()
        if not raw:
            raise ValueError("每个证据组必须有一个 source 字符串")
        if "source=" not in raw and "|" not in raw:
            return {"source": raw, "toc": "", "slices": []}
        parsed: Dict[str, Any] = {"source": "", "toc": "", "slices": []}
        for segment in raw.split("|"):
            part = segment.strip()
            if not part or "=" not in part:
                raise ValueError(f"证据组字符串格式无效: {group}")
            k, v = part.split("=", 1)
            key = k.strip().lower()
            val = v.strip()
            if key not in {"source", "toc", "slices"}:
                raise ValueError(f"证据组仅允许 source、toc、slices: {key}")
            if key == "source":
                parsed["source"] = val
            elif key == "toc":
                parsed["toc"] = val
            elif key == "slices":
                parsed["slices"] = [x.strip() for x in val.split(",") if x.strip()] if val else []
        if not parsed["source"]:
            raise ValueError("每个证据组必须有一个 source 字符串")
        return parsed
    raise ValueError("provenance 证据组必须为字符串或对象")


def normalize_provenance_groups(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("provenance 必须为列表")
    out: List[Dict[str, Any]] = []
    for raw_group in value:
        group = _parse_provenance_group_item(raw_group)
        source = normalize_provenance_refs(group["source"])[0]
        toc_list = normalize_provenance_refs(group.get("toc"))
        toc = toc_list[0] if toc_list else ""
        target = next((g for g in out if g["source"] == source and g["toc"] == toc), None)
        if target is None:
            target = {"source": source, "toc": toc, "slices": []}
            out.append(target)
        for ref in normalize_provenance_refs(group.get("slices", [])):
            if ref not in target["slices"]:
                target["slices"].append(ref)
    return out


def format_provenance_group_str(group: Dict[str, Any]) -> str:
    """Serialize a normalized provenance group to Obsidian-compatible list[string] item."""
    parts = [f"source={group['source']}"]
    if group.get("toc"):
        parts.append(f"toc={group['toc']}")
    if group.get("slices"):
        parts.append(f"slices={','.join(group['slices'])}")
    return " | ".join(parts)


def derive_provenance_fields(groups: Any) -> Dict[str, Any]:
    norm = normalize_provenance_groups(groups)
    res: Dict[str, Any] = {
        "source": norm[0]["source"] if norm else "",
        "sources": [],
        "toc": [],
        "slices": [],
        "provenance": norm,
    }
    for g in norm:
        for field, values in (("sources", [g["source"]]), ("toc", normalize_provenance_refs(g["toc"])), ("slices", g["slices"])):
            for val in values:
                if val not in res[field]:
                    res[field].append(val)
    return res


_LEGACY_LINK_COMPAT_SPECS: Dict[str, Dict[str, Dict[str, str]]] = {
    "atomic": {
        "sources": {"type": "list[string]"},
        "slices": {"type": "list[string]"},
        "source": {"type": "string"},
        "toc": {"type": "list[string]"},
        "provenance": {"type": "list[string]"},
        "map": {"type": "list[string]"},
        "moc": {"type": "list[string]"},
    },
    "artifact": {
        "sources": {"type": "list[string]"},
        "source": {"type": "string"},
        "toc": {"type": "list[string]"},
        "slices": {"type": "list[string]"},
        "provenance": {"type": "list[string]"},
        "map": {"type": "list[string]"},
        "moc": {"type": "list[string]"},
        "extracted_to": {"type": "list[string]"},
    },
    "library": {
        "map": {"type": "list[string]"},
        "moc": {"type": "list[string]"},
        "extracted_to": {"type": "list[string]"},
    },
    "toc": {
        "extracted_to": {"type": "list[string]"},
    },
}


def apply_provenance_frontmatter(text: str, groups: Any) -> str:
    """Validate provenance groups and strip legacy/frontmatter provenance keys (`sources`, `slices`, etc.) from atomic cards."""
    _ = derive_provenance_fields(groups)
    out = text
    for legacy_key in ("sources", "slices", "source", "toc", "provenance", "moc", "map"):
        out = drop_frontmatter_field(out, legacy_key)
    return out


def validate_provenance_meta(meta: Dict[str, Any]) -> List[str]:
    """Validate link-field value types while staying lenient toward legacy cards containing deprecated provenance keys."""
    errors: List[str] = []
    for key in ("sources", "toc", "slices"):
        if key in meta and meta[key] is not None:
            if not isinstance(meta[key], list) or any(not isinstance(x, str) for x in meta[key]):
                errors.append(f"{key} 必须为字符串列表")
    if "source" in meta and meta["source"] is not None and not isinstance(meta["source"], str):
        errors.append("source 必须为字符串")
    return errors


def is_exempt_rel_path(rel_path: str) -> bool:
    norm = rel_path.replace("\\", "/").strip("/")
    if not norm:
        return False
    parts = norm.split("/")
    layout = load_layout_dict()
    exempt_cfg = layout.get("exempt_zones") or ["91-Temp", "97-Attic", "30-Workbench"]
    for item in exempt_cfg:
        tok = str(item).strip("/")
        if not tok:
            continue
        if "/" in tok:
            if tok in norm:
                return True
        elif tok in parts:
            return True
    return False


def _matches_declared_type(val: Any, declared_type: str, key: str) -> Optional[str]:
    if not declared_type:
        return None
    if declared_type == "string":
        if not isinstance(val, str):
            return f"字段 {key} 必须为字符串"
        return None
    if declared_type == "boolean":
        if not isinstance(val, bool):
            return f"字段 {key} 必须为布尔值"
        return None
    if declared_type == "integer":
        if not isinstance(val, int) or isinstance(val, bool):
            return f"字段 {key} 必须为整数"
        return None
    if declared_type == "list[string]":
        if not isinstance(val, list) or any(
            not (isinstance(item, str) or (key == "provenance" and isinstance(item, dict)))
            for item in val
        ):
            if key == "tags" and isinstance(val, list) and any(isinstance(item, int) and not isinstance(item, bool) for item in val):
                bad = next(item for item in val if isinstance(item, int) and not isinstance(item, bool))
                return f"标签严禁纯数字: {bad}"
            return f"{key} 必须为字符串数组"
        if key == "tags":
            for item in val:
                if item.isdigit():
                    return f"标签严禁纯数字: {item}"
        return None
    if declared_type == "list[object]":
        if not isinstance(val, list) or any(not isinstance(item, dict) for item in val):
            return f"{key} 必须为对象列表"
        return None
    return None


def inspect_note_against_dict(meta: Dict[str, Any], rel_path: Optional[str] = None) -> Dict[str, List[str]]:
    """Validate note Frontmatter and optional Vault relative path against GewuAtlas/dicts/."""
    meta_dict = load_metadata_dict()
    vocabularies = meta_dict.get("vocabularies") or {}
    schemas = meta_dict.get("schemas") or {}
    allowed_doc_types = set(_vocab_choices("doc_type", ALLOWED_DOC_TYPES))

    errors: List[str] = []
    unknown_fields: List[str] = []
    zone_errors: List[str] = []

    doc_type = str(meta.get("doc_type") or "").strip()
    if not doc_type:
        return {"errors": ["缺失必需属性 doc_type"], "unknown_fields": [], "zone_errors": []}
    if doc_type not in allowed_doc_types:
        errors.append(f"未知 doc_type: {doc_type}")
        return {"errors": errors, "unknown_fields": [], "zone_errors": []}

    schema_key = "toc" if doc_type == "slice_toc" and doc_type not in schemas else doc_type
    schema_def = schemas.get(schema_key) if isinstance(schemas, dict) else None

    if not str(meta.get("title") or "").strip():
        errors.append("缺失必需属性 title")
    if doc_type in ("atomic", "library", "moc", "artifact") and not str(meta.get("summary") or "").strip():
        errors.append(f"{doc_type} 卡片缺少 summary")

    if doc_type == "journal":
        for forbidden in ("status", "type", "reviewed_by", "stage"):
            if forbidden in meta:
                errors.append(f"journal 日记不携带知识定性字段: {forbidden}")

    if isinstance(schema_def, dict):
        req_props = schema_def.get("required_properties") or {}
        opt_props = schema_def.get("optional_properties") or {}
        all_specs: Dict[str, Any] = {}
        all_specs.update(_LEGACY_LINK_COMPAT_SPECS.get(schema_key, {}))
        all_specs.update(opt_props)
        all_specs.update(req_props)

        for req_key in req_props:
            if req_key in ("doc_type", "title", "summary"):
                continue
            if req_key not in meta or meta[req_key] is None:
                errors.append(f"{doc_type} 缺少必需字段 {req_key}")
            elif isinstance(meta[req_key], str) and not meta[req_key].strip():
                errors.append(f"{doc_type} 必需字段 {req_key} 不能为空")

        for field_key, val in meta.items():
            if field_key not in all_specs:
                unknown_fields.append(str(field_key))
                continue
            if val is None:
                continue
            spec = all_specs[field_key] if isinstance(all_specs[field_key], dict) else {}
            type_err = _matches_declared_type(val, str(spec.get("type") or ""), field_key)
            if type_err:
                errors.append(type_err)
                continue
            if "fixed_value" in spec and field_key != "doc_type":
                if val != spec["fixed_value"]:
                    errors.append(f"字段 {field_key} 必须为固定值 {spec['fixed_value']}")
            vocab_ref = str(spec.get("ref_vocab") or "").strip()
            if vocab_ref and isinstance(val, str):
                sval = val.strip()
                if sval:
                    vocab_obj = vocabularies.get(vocab_ref) or {}
                    allowed_vals = vocab_obj.get("allowed_values") or {}
                    if sval not in allowed_vals:
                        errors.append(f"非法 {field_key}: {sval}")
            pattern = str(spec.get("pattern") or "").strip()
            if pattern and isinstance(val, str) and val.strip():
                if not re.search(pattern, val.strip()):
                    errors.append(f"字段 {field_key} 格式不符合字典规范: {val}")
            max_len = spec.get("max_length")
            if isinstance(max_len, int) and isinstance(val, str) and len(val) > max_len:
                errors.append(f"字段 {field_key} 超出最大长度 {max_len} ({len(val)})")

        if rel_path is not None and not is_exempt_rel_path(rel_path):
            fpat = str(schema_def.get("folder_pattern") or "").strip()
            rel_norm = rel_path.replace("\\", "/")
            if fpat and not re.search(fpat, rel_norm):
                zone_errors.append(f"doc_type '{doc_type}' 位置与字典路径规则不符 (期望匹配 {fpat})")

    # Keep explicit fallback checks for core vocabulary fields even if omitted from a subtype schema
    if doc_type != "journal":
        for key, vocab_name, label in (
            ("status", "status", "status"),
            ("type", "type", "认识论 type"),
            ("stage", "stage", "stage"),
            ("read_status", "read_status", "read_status"),
        ):
            raw = str(meta.get(key) or "").strip()
            allowed_vals = _vocab_choices(vocab_name)
            if raw and allowed_vals and raw not in allowed_vals:
                msg = f"非法{label}: {raw}"
                if msg not in errors:
                    errors.append(msg)

    errors.extend(validate_provenance_meta(meta))
    return {
        "errors": list(dict.fromkeys(errors)),
        "unknown_fields": sorted(set(unknown_fields)),
        "zone_errors": zone_errors,
    }


def validate_dicts_integrity() -> Dict[str, Any]:
    """Validate that GewuAtlas/dicts/{layout,metadata,methodology}.yaml parse cleanly and match core expectations."""
    errors: List[str] = []
    versions: Dict[str, str] = {}
    for fname in ("layout.yaml", "metadata.yaml", "methodology.yaml"):
        try:
            obj = load_dict_yaml(fname)
            versions[fname] = str(obj.get("version") or "unknown")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"dicts/{fname}: {exc}")

    if not errors:
        meta = load_metadata_dict()
        layout = load_layout_dict()
        schemas = meta.get("schemas") or {}
        doc_types = set((meta.get("vocabularies") or {}).get("doc_type", {}).get("allowed_values", {}).keys())
        required_doc_types = {"journal", "manifest", "memo", "artifact", "library", "atomic", "moc", "slice", "toc", "slip", "workbench", "project", "index"}
        missing_dt = sorted(required_doc_types - doc_types)
        if missing_dt:
            errors.append(f"metadata.yaml missing core doc_types: {', '.join(missing_dt)}")
        missing_schemas = sorted((required_doc_types) - set(schemas.keys()))
        if missing_schemas:
            errors.append(f"metadata.yaml missing schemas: {', '.join(missing_schemas)}")
        if "slip" not in (layout.get("domain_skeleton") or {}):
            errors.append("layout.yaml missing domain_skeleton.slip")
        if "formal_cabins" not in layout or "25-Slip" not in (layout.get("formal_cabins") or []):
            errors.append("layout.yaml formal_cabins missing 25-Slip")
        try:
            get_vault_layout_spec(layout)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"layout.yaml vault_skeleton invalid: {exc}")

    return {
        "ok": not errors,
        "versions": versions,
        "errors": errors,
    }


def validate_note_meta(meta: Dict[str, Any], *, strict: bool = False, rel_path: Optional[str] = None) -> List[str]:
    res = inspect_note_against_dict(meta, rel_path=rel_path)
    out = list(res["errors"])
    if strict:
        if res["unknown_fields"]:
            out.append(f"未在 metadata.yaml 登记的污染字段: {', '.join(res['unknown_fields'])}")
        out.extend(res["zone_errors"])
    return out


def _strip_code_regions(text: str) -> str:
    """Exclude block code and inline backticks from semantic link discovery."""
    from notes.atlas_markdown_blocks import code_line_mask
    out: List[str] = []
    prose: List[str] = []
    inline_re = re.compile(r"(?<!`)(?P<tick>`+)(?!`)(?P<code>.*?)(?<!`)(?P=tick)(?!`)", re.S)
    header = re.match(r"\A---[^\S\n]*\r?\n.*?\r?\n---[^\S\n]*(?:\r?\n|$)", text or "", re.S)
    if header:
        out.append(inline_re.sub(" ", header.group(0)))
        text = text[header.end():]

    def flush() -> None:
        if prose:
            out.append(inline_re.sub(" ", "".join(prose)))
            prose.clear()

    for line, is_code in zip((text or "").splitlines(keepends=True), code_line_mask(text or "")):
        if is_code:
            flush()
            out.append("\n" if line.endswith("\n") else " ")
        else:
            prose.append(line)
    flush()
    return "".join(out)


def extract_wikilinks(text: str) -> List[str]:
    seen: List[str] = []
    cleaned = _strip_code_regions(text)
    for raw in re.findall(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]", cleaned):
        stem = raw.rstrip("\\").strip()
        if stem and stem not in seen:
            seen.append(stem)
    return seen


def resolve_wikilink(target: str, source_rel: str, paths) -> List[str]:
    """Resolve identity without picking an arbitrary same-name note."""
    import posixpath
    from urllib.parse import unquote
    target = unquote(target.strip())
    known = set(paths)
    if target.startswith(("./", "../")):
        candidate = posixpath.normpath(posixpath.join(posixpath.dirname(source_rel), target))
        candidate = candidate if candidate.endswith(".md") else candidate + ".md"
        return [candidate] if candidate in known else []
    if "/" in target:
        candidate = target if target.endswith(".md") else target + ".md"
        return [candidate] if candidate in known else []
    stem = target[:-3] if target.endswith(".md") else target
    return sorted(p for p in known if posixpath.basename(p) == stem + ".md")
