"""Prints the skip report (tests/plugins/skip_report.py) as a short Markdown summary for the CI step summary.

Usage: python scripts/ci/skip_report_summary.py skip-report.json >> "$GITHUB_STEP_SUMMARY"
A missing or unreadable file prints a one-line note and exits 0: the summary never decides the build.
"""
import json
import sys
from pathlib import Path
from typing import Dict


def render(report: Dict) -> str:
    outcomes = report.get("outcomes", {})
    lines = ["### Tests that ran and tests that did not", "",
             ", ".join(f"{name}: {count}" for name, count in sorted(outcomes.items())) or "no tests reported", ""]
    skipped = report.get("skipped_by_reason", {})
    if skipped:
        lines += ["| Skipped | Reason |", "|---|---|"]
        lines += [f"| {info['count']} | {reason.replace('|', '/')} |"
                  for reason, info in sorted(skipped.items(), key=lambda kv: (-kv[1]["count"], kv[0]))]
    return "\n".join(lines) + "\n"


def main(argv) -> int:
    try:
        report = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    except (IndexError, OSError, ValueError):
        print("skip report: no readable report file")
        return 0
    print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
