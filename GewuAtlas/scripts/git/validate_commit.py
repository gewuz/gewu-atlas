"""Validate Agent-authored commit text against gewu.commit-message/1 without running Git or altering files."""
import argparse
import json
import re
import sys
from pathlib import Path

FORBIDDEN_COMMIT_LEAKS = (
    "SQLite format 3",
    "enc:v1:",
    "PRAGMA key",
    ".vault_kek",
    "-----BEGIN ",
)

RECOMMENDED_TYPES = ("feat", "fix", "refactor", "docs", "test", "chore", "security", "perf", "build", "revert")


def validate(text, agent):
    if not agent.strip() or "\n" in agent or "\r" in agent:
        raise ValueError("invalid self-reported agent")
    if "\x00" in text or not text.strip():
        raise ValueError("empty message or NUL")
    for leak in FORBIDDEN_COMMIT_LEAKS:
        if leak in text:
            raise ValueError(f"commit message leaks forbidden internal/secret pattern: {leak}")
    lines = text.splitlines()
    match = re.fullmatch(r"([a-z][a-z0-9-]*)\(([a-z][a-z0-9-]*)\): \[([^\]\r\n]+)\] (\S.*)", lines[0])
    if not match:
        raise ValueError("expected type(scope): [Agent] description")
    kind, scope, author, summary = match.groups()
    if author != agent:
        raise ValueError("self-reported identity mismatch")
    if len(lines) > 1 and lines[1].strip():
        raise ValueError("blank line required before body")
    body_lines = max(0, len(lines) - 2) if len(lines) > 1 else 0
    return {
        "contract": "gewu.commit-message/1",
        "format": "valid",
        "type": kind,
        "scope": scope,
        "agent": author,
        "subject": summary,
        "body_lines": body_lines,
        "recommended_types": list(RECOMMENDED_TYPES),
        "agent_discretionary_fields": ["type", "scope", "subject", "body"],
        "semantic_review": "required",
        "commit_executed": False,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--message-file", type=Path, required=True)
    p.add_argument("--agent", required=True)
    a = p.parse_args()
    try:
        if a.message_file.stat().st_size > 65536:
            raise ValueError("message exceeds 64KiB")
        result = validate(a.message_file.read_text(encoding="utf-8"), a.agent)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError) as e:
        print(json.dumps({"contract": "gewu.commit-message/1", "format": "invalid", "reason": str(e), "commit_executed": False}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
