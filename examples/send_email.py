#!/usr/bin/env python3
"""Stand-in email sender for the demo. It never transmits anything.

In a real setup this is where the Gmail or SMTP call lives. It runs the
yes-or-no checks itself before "sending", so even a send the hook allowed
still has to pass them. Two independent gates, two places to fail closed.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from checks.outbound_checks import run_all  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)  # --t must not mean --to
    parser.add_argument("--to", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--body-file", required=True)
    parser.add_argument("--attach", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    draft = {
        "to": [a.strip() for a in args.to.split(",") if a.strip()],
        "subject": args.subject,
        "body": Path(args.body_file).read_text(),
        "attachments": args.attach,
    }
    failures = run_all(draft)
    for reason in failures:
        print(f"BLOCKED: {reason}")
    if failures:
        return 1
    mode = "DRY RUN, would send" if args.dry_run else "[stub] sent"
    print(f"{mode}: {draft['subject']!r} to {', '.join(draft['to'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
