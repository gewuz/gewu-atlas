"""4-channel tolerant Parameter Normalizer with strict semantic boundaries.

Implements the Layer-1 Contract Rail parameter ingestion model:
  precedence: explicit CLI flags > --set k=v > --payload / --json-stdin > defaults.
Rejects unknown/hallucinated fields (E_PARAM_UNKNOWN_FIELD) and path-traversal or
SQL injection fragments (E_PARAM_INVALID). Never leaks argparse usage dumps.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_FORBIDDEN_REF_PATTERNS = (
    re.compile(r"\.\."),
    re.compile(r"[/\\]"),
    re.compile(r"[\x00;\n\r]"),
    re.compile(r"--"),
    re.compile(r"\b(select|insert|update|delete|drop|pragma|attach|union)\b", re.I),
)


class ParamError(ValueError):
    """Structured parameter normalization error without Python tracebacks."""

    def __init__(
        self,
        code: str,
        message: str,
        missing_slots: Optional[List[str]] = None,
        fill_template: Optional[Dict[str, Any]] = None,
        allowed_fields: Optional[List[str]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.missing_slots = missing_slots or []
        self.fill_template = fill_template or {}
        self.allowed_fields = allowed_fields or []


@dataclass(frozen=True)
class SlotSpec:
    """Declarative specification for one CLI / JSON semantic parameter slot."""

    name: str
    kind: str = "str"  # str | ref | enum | bool | list | int
    required: bool = False
    default: Any = None
    choices: Tuple[str, ...] = ()
    aliases: Dict[str, str] = field(default_factory=dict)
    description: str = ""
    example: Any = None
    ref_prefix: Optional[str] = None
    allow_stdin_dash: bool = False


@dataclass(frozen=True)
class ActionSpec:
    """Declarative specification for one --action subcommand."""

    name: str
    description: str = ""
    required_slots: Tuple[str, ...] = ()
    optional_slots: Tuple[str, ...] = ()
    read_only: bool = False
    confirm_required: bool = False


@dataclass(frozen=True)
class CommandSchema:
    """Declarative command schema for 4-channel normalization and --template."""

    command_hint: str
    slots: Tuple[SlotSpec, ...]
    allow_extra_set_keys: bool = False
    actions: Tuple[ActionSpec, ...] = ()

    def slot_map(self) -> Dict[str, SlotSpec]:
        return {s.name: s for s in self.slots}

    def action_map(self) -> Dict[str, ActionSpec]:
        return {a.name: a for a in self.actions}

    def known_slot_types(self) -> Dict[str, str]:
        return {s.name: s.kind for s in self.slots}


def _to_snake(key: str) -> str:
    return key.strip().lstrip("-").replace("-", "_")


def _parse_bool(val: Any, slot: str) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, int) and val in (0, 1):
        return bool(val)
    if isinstance(val, str):
        norm = val.strip().lower()
        if norm in ("true", "1", "yes", "y"):
            return True
        if norm in ("false", "0", "no", "n"):
            return False
    raise ParamError("E_PARAM_INVALID", f"slot '{slot}' expects boolean (true/false/1/0/yes/no)")


def _parse_list(val: Any) -> List[str]:
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str):
        s = val.strip()
        if s.startswith("["):
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        return [item.strip() for item in s.split(",") if item.strip()]
    return [str(val).strip()] if str(val).strip() else []


def _validate_safe_token(val: str, slot: SlotSpec) -> str:
    for pat in _FORBIDDEN_REF_PATTERNS:
        if pat.search(val):
            raise ParamError("E_PARAM_INVALID", f"slot '{slot.name}' contains forbidden path or expression characters")
    if slot.kind == "ref":
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{1,63}", val):
            raise ParamError("E_PARAM_INVALID", f"slot '{slot.name}' is not a valid opaque handle")
        if slot.ref_prefix and not val.startswith(slot.ref_prefix):
            raise ParamError("E_PARAM_INVALID", f"slot '{slot.name}' must start with '{slot.ref_prefix}'")
    return val


def _normalize_value(raw: Any, slot: SlotSpec, stdin_text: Optional[str]) -> Any:
    if slot.allow_stdin_dash and raw == "-":
        if stdin_text is None:
            raise ParamError("E_PARAM_INVALID", f"slot '{slot.name}' received '-' but stdin is empty")
        return stdin_text
    if slot.kind == "bool":
        return _parse_bool(raw, slot.name)
    if slot.kind == "int":
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ParamError("E_PARAM_INVALID", f"slot '{slot.name}' expects integer") from exc
    if slot.kind == "list":
        items = _parse_list(raw)
        return [_validate_safe_token(item, slot) if not slot.allow_stdin_dash else item for item in items]
    if not isinstance(raw, str):
        raw = str(raw)
    if slot.kind == "enum":
        low = raw.strip().lower()
        alias_map = {k.lower(): v for k, v in slot.aliases.items()}
        canonical = alias_map.get(low)
        if canonical is None:
            for choice in slot.choices:
                if choice.lower() == low:
                    canonical = choice
                    break
        if canonical is None or (slot.choices and canonical not in slot.choices):
            raise ParamError(
                "E_PARAM_INVALID",
                f"slot '{slot.name}' must be one of {list(slot.choices)}",
            )
        return canonical
    if slot.kind == "ref":
        return _validate_safe_token(raw.strip(), slot)
    text = raw.strip() if not slot.allow_stdin_dash else raw
    if not slot.allow_stdin_dash:
        for pat in _FORBIDDEN_REF_PATTERNS[:3]:
            if pat.search(text) and ("/" in text or ".." in text):
                # Allow natural prose unless it's trying path traversal or null bytes
                if "\x00" in text or ".." in text:
                    raise ParamError("E_PARAM_INVALID", f"slot '{slot.name}' contains forbidden traversal or null byte")
    return text


def build_template(
    schema: CommandSchema,
    partial: Optional[Dict[str, Any]] = None,
    action_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate minimal fill-in-the-blank JSON template for --template or action-scoped queries."""
    tpl: Dict[str, Any] = {}
    partial = dict(partial or {})
    amap = schema.action_map()
    spec = amap.get(action_name) if action_name else None
    slots_to_show = list(schema.slots)
    cmd_hint = schema.command_hint
    req_override: Optional[set[str]] = None
    if spec is not None:
        partial.setdefault("action", spec.name)
        allowed_order: List[str] = ["action", *spec.required_slots, *spec.optional_slots]
        smap = schema.slot_map()
        slots_to_show = [smap[name] for name in dict.fromkeys(allowed_order) if name in smap]
        req_override = {"action", *spec.required_slots}
        flags = " ".join(
            f"--{s.name} <{s.example or s.name}>"
            if s.name in req_override
            else f"[--{s.name} <{s.example or s.name}>]"
            for s in slots_to_show
            if s.name != "action"
        )
        cmd_hint = f"--action {spec.name}" + (f" {flags}" if flags else "")

    for slot in slots_to_show:
        is_req = slot.name in req_override if req_override is not None else slot.required
        if slot.name in partial and partial[slot.name] not in (None, ""):
            tpl[slot.name] = partial[slot.name]
        elif slot.choices:
            tpl[slot.name] = f"<{'|'.join(slot.choices)}>"
        elif slot.ref_prefix:
            tpl[slot.name] = f"<{slot.ref_prefix}01>"
        elif is_req:
            tpl[slot.name] = f"<必填: {slot.description or slot.name}>"
        else:
            tpl[slot.name] = slot.default if slot.default is not None else f"<可选: {slot.description or slot.name}>"

    out: Dict[str, Any] = {
        "schema": "gewu.fill-template/1",
        "command": cmd_hint,
        "fill_template": tpl,
        "slots": [
            {
                "name": s.name,
                "kind": s.kind,
                "required": s.name in req_override if req_override is not None else s.required,
                "choices": list(s.choices),
                "description": s.description,
                "example": s.example,
            }
            for s in slots_to_show
        ],
    }
    if spec is not None:
        out["action_spec"] = {
            "name": spec.name,
            "description": spec.description,
            "read_only": spec.read_only,
            "confirm_required": spec.confirm_required,
            "required_slots": list(spec.required_slots),
            "optional_slots": list(spec.optional_slots),
        }
    elif amap:
        out["actions"] = {
            a.name: {
                "description": a.description,
                "required_slots": list(a.required_slots),
                "optional_slots": list(a.optional_slots),
                "read_only": a.read_only,
                "confirm_required": a.confirm_required,
            }
            for a in schema.actions
        }
    return out


def _parse_cli_tokens(argv: Sequence[str], valid_slots: Dict[str, SlotSpec]) -> Tuple[Dict[str, Any], Dict[str, Any], Optional[str], bool, bool]:
    cli_vals: Dict[str, Any] = {}
    set_vals: Dict[str, Any] = {}
    payload_raw: Optional[str] = None
    want_template = False
    use_json_stdin = False
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("--template", "--describe"):
            want_template = True
            i += 1
            continue
        if tok == "--json-stdin":
            use_json_stdin = True
            i += 1
            continue
        if tok in ("--help", "-h"):
            want_template = True
            i += 1
            continue
        if not tok.startswith("--"):
            raise ParamError("E_PARAM_INVALID", f"unexpected positional token '{tok}'", allowed_fields=sorted(valid_slots))
        if "=" in tok:
            flag_part, inline_val = tok.split("=", 1)
            has_inline = True
        else:
            flag_part, inline_val = tok, None
            has_inline = False
        key = _to_snake(flag_part)
        if key == "payload":
            if not has_inline:
                if i + 1 >= len(argv):
                    raise ParamError("E_PARAM_MISSING", "missing JSON value after --payload")
                inline_val = argv[i + 1]
                i += 2
            else:
                i += 1
            payload_raw = inline_val
            continue
        if key == "set":
            if not has_inline:
                if i + 1 >= len(argv):
                    raise ParamError("E_PARAM_MISSING", "missing k=v after --set")
                inline_val = argv[i + 1]
                i += 2
            else:
                i += 1
            if "=" not in (inline_val or ""):
                raise ParamError("E_PARAM_INVALID", "--set requires key=value format")
            sk, sv = inline_val.split("=", 1)
            set_vals[_to_snake(sk)] = sv
            continue
        if key not in valid_slots:
            raise ParamError(
                "E_PARAM_UNKNOWN_FIELD",
                f"unknown field '--{flag_part.lstrip('-')}' rejected; allowed fields: {sorted(valid_slots)}",
                allowed_fields=sorted(valid_slots),
            )
        slot = valid_slots[key]
        if slot.kind == "bool" and not has_inline and (i + 1 >= len(argv) or argv[i + 1].startswith("--")):
            val: Any = True
            i += 1
        else:
            if not has_inline:
                if i + 1 >= len(argv):
                    raise ParamError("E_PARAM_MISSING", f"missing value after --{slot.name}")
                val = argv[i + 1]
                i += 2
            else:
                val = inline_val
                i += 1
        if slot.kind == "list":
            existing = cli_vals.get(key)
            if isinstance(existing, list):
                existing.extend(_parse_list(val))
            else:
                cli_vals[key] = _parse_list(val)
        else:
            cli_vals[key] = val
    return cli_vals, set_vals, payload_raw, want_template, use_json_stdin


def normalize_args(
    schema: CommandSchema,
    argv: Sequence[str],
    stdin_data: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Normalize 4-channel inputs into validated slot dict or return --template payload.

    Returns:
      (normalized_dict, None) on normal execution, or
      (None, template_dict) when --template / --describe / -h is requested.
    """
    valid_slots = schema.slot_map()
    cli_vals, set_vals, payload_raw, want_template, use_json_stdin = _parse_cli_tokens(argv, valid_slots)

    if want_template:
        raw_action = cli_vals.get("action") or set_vals.get("action")
        return None, build_template(schema, action_name=str(raw_action).strip() if raw_action else None)

    payload_vals: Dict[str, Any] = {}
    if use_json_stdin:
        if stdin_data is None or not stdin_data.strip():
            raise ParamError("E_PARAM_MISSING", "--json-stdin specified but stdin is empty")
        payload_raw = stdin_data
    if payload_raw is not None:
        try:
            decoded = json.loads(payload_raw)
        except json.JSONDecodeError as exc:
            raise ParamError("E_PARAM_INVALID", "invalid JSON in --payload / --json-stdin") from exc
        if not isinstance(decoded, dict):
            raise ParamError("E_PARAM_INVALID", "JSON payload must be an object")
        for k, v in decoded.items():
            sk = _to_snake(str(k))
            if sk not in valid_slots:
                raise ParamError(
                    "E_PARAM_UNKNOWN_FIELD",
                    f"unknown payload field '{k}' rejected; allowed fields: {sorted(valid_slots)}",
                    allowed_fields=sorted(valid_slots),
                )
            payload_vals[sk] = v

    if not schema.allow_extra_set_keys:
        for k in set_vals:
            if k not in valid_slots:
                raise ParamError(
                    "E_PARAM_UNKNOWN_FIELD",
                    f"unknown --set field '{k}' rejected; allowed fields: {sorted(valid_slots)}",
                    allowed_fields=sorted(valid_slots),
                )

    merged: Dict[str, Any] = {}
    for s in schema.slots:
        if s.default is not None:
            merged[s.name] = s.default
    merged.update(payload_vals)
    for k, v in set_vals.items():
        if k in valid_slots:
            merged[k] = v
    merged.update(cli_vals)

    # Check if stdin is needed for '-' placeholder
    need_stdin = any(
        s.allow_stdin_dash and merged.get(s.name) == "-"
        for s in schema.slots
    )
    if need_stdin and stdin_data is None:
        stdin_data = sys.stdin.read()

    normalized: Dict[str, Any] = {}
    missing: List[str] = []
    for s in schema.slots:
        if s.name not in merged or merged[s.name] is None or merged[s.name] == "":
            if s.required:
                missing.append(s.name)
            else:
                normalized[s.name] = s.default
        else:
            normalized[s.name] = _normalize_value(merged[s.name], s, stdin_data)

    if schema.allow_extra_set_keys:
        extras = {k: v for k, v in set_vals.items() if k not in valid_slots}
        if extras:
            normalized["_extra"] = extras

    chosen_action = str(normalized.get("action") or "").strip()
    aspec = schema.action_map().get(chosen_action) if chosen_action else None
    if aspec is not None:
        for req_slot in aspec.required_slots:
            val = normalized.get(req_slot)
            if (val is None or val == "" or val == []) and req_slot not in missing:
                missing.append(req_slot)

    if missing:
        tpl = build_template(schema, normalized, action_name=chosen_action or None)["fill_template"]
        raise ParamError(
            "E_PARAM_MISSING",
            f"missing required slot(s): {', '.join(missing)}",
            missing_slots=missing,
            fill_template=tpl,
            allowed_fields=sorted(valid_slots),
        )
    return normalized, None


def run_isolated_python(
    script_path: Path,
    args: Sequence[str],
    stdin_payload: Optional[str] = None,
    timeout: int = 25,
) -> subprocess.CompletedProcess:
    """Invoke a target Python module with -I -B and shell=False (immune to PYTHONPATH/cwd hijack)."""
    cmd = [sys.executable, "-I", "-B", str(script_path), *args]
    return subprocess.run(
        cmd,
        input=stdin_payload,
        capture_output=True,
        text=True,
        shell=False,
        timeout=timeout,
    )
