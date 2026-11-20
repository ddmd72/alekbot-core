"""PR check: a change to code a living doc covers changes that doc, or says why not.

Usage: PR_BODY=... python -m scripts.ci.living_docs_pr_check --base origin/main
For every living doc whose `covers:` match a changed file, the doc itself is in the change list or the PR body has
the line `Docs not affected: <doc path> — <reason>` (separator `—`, `–` or `-`). Contract:
docs/04_solution_strategy/decisions/living_docs_contract.md.
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

from scripts.ci.living_docs import cover_matches, effective_covers, living_docs, parse_header, tracked_files

_NOT_AFFECTED = re.compile(r"^\s*Docs not affected:\s*(\S+)\s+[—–-]\s*(.*)$")


def missing_doc_lines(changed: list[str], body: str, docs: dict[str, list[str]], tracked: list[str]) -> list[str]:
    """Paste-ready `Docs not affected:` lines for each affected doc that is neither changed nor excused in `body`.

    A changed path matches a cover like `cover_matches` does, against the change list itself, so a deleted file still
    counts. `tracked` is accepted for the caller's symmetry with the other checks and is not needed here.
    """
    excused = {
        m.group(1) for m in (_NOT_AFFECTED.match(line) for line in body.splitlines()) if m and m.group(2).strip()
    }
    missing: list[str] = []
    for doc in sorted(docs):
        if doc in changed or doc in excused:
            continue
        if any(cover_matches(c, changed) for c in effective_covers(docs[doc])):
            missing.append(f"Docs not affected: {doc} — <why>")
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    changed = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", f"{args.base}...HEAD"],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout.split("\n")
    changed = [c for c in changed if c]
    docs: dict[str, list[str]] = {}
    for path in living_docs(root):
        parsed = parse_header(path.read_text(encoding="utf-8"))
        if parsed is not None:
            docs[path.relative_to(root).as_posix()] = parsed[0]
    missing = missing_doc_lines(changed, os.environ.get("PR_BODY", ""), docs, tracked_files(root))
    if not missing:
        return 0
    print("\n".join(missing))
    print(
        "A PR that changes code a living doc covers must change that doc or carry the line above in its description"
        " (docs/04_solution_strategy/decisions/living_docs_contract.md)."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
