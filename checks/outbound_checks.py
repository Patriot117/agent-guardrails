#!/usr/bin/env python3
"""Yes-or-no checks on a draft before it can go out.

Each check answers one question with a pass or a reason it failed. No scores, no
judgment calls: if a rule can be written as code, it shouldn't be left to a model.
The judgment calls go to the reviewer agent (.claude/agents/reviewer.md).

Usage: python3 checks/outbound_checks.py draft.json   (exit 1 = blocked)
Draft format: {"to": [...], "subject": "...", "body": "...", "attachments": [...]}
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PLACEHOLDER = re.compile(r"\{\{.*?\}\}|\[(TODO|TBD|INSERT[^\]]*)\]|lorem ipsum", re.I)


def check_has_recipient(draft: dict) -> str | None:
    return None if draft.get("to") else "no recipient"


def check_has_subject(draft: dict) -> str | None:
    return None if str(draft.get("subject", "")).strip() else "empty subject"


def check_no_placeholders(draft: dict) -> str | None:
    text = f"{draft.get('subject', '')}\n{draft.get('body', '')}"
    match = PLACEHOLDER.search(text)
    return f"unfilled placeholder: {match.group(0)}" if match else None


def check_no_em_dashes(draft: dict) -> str | None:
    # House style for anything a buyer reads. Trivial, which is the point:
    # a rule this simple should never depend on a model remembering it.
    text = f"{draft.get('subject', '')}\n{draft.get('body', '')}"
    return "em dash in buyer-facing text" if "\u2014" in text else None


def check_attachments_exist(draft: dict) -> str | None:
    missing = [a for a in draft.get("attachments", []) if not Path(a).is_file()]
    return f"attachment not found: {', '.join(missing)}" if missing else None


def check_attachments_mentioned(draft: dict) -> str | None:
    body = str(draft.get("body", "")).lower()
    says_attached = re.search(r"\battach(ed|ment)\b", body)
    if says_attached and not draft.get("attachments"):
        return "body mentions an attachment but none is attached"
    return None


CHECKS = [
    check_has_recipient,
    check_has_subject,
    check_no_placeholders,
    check_no_em_dashes,
    check_attachments_exist,
    check_attachments_mentioned,
]


def run_all(draft: dict) -> list[str]:
    return [reason for check in CHECKS if (reason := check(draft))]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    failures = run_all(json.loads(Path(argv[1]).read_text()))
    for reason in failures:
        print(f"BLOCKED: {reason}")
    if not failures:
        print("PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
