"""Classify a change list as docs-only (CI skips `make ci`) or not.

This module is the one place that defines "documentation". Fail safe: anything not
recognised as docs, and an empty list, means "not docs-only" (run everything).
Usage: git diff --no-renames --name-only A...B | python scripts/ci/docs_only.py   ->  true | false
(--no-renames is required: a rename otherwise lists only the new path, hiding the old code path.)
Known gap: a test that reads a root `*.md` or a `CLAUDE.md` is not detected by the meta-test (none today).
`--doc-reading-tests` prints the unit tests that read documentation, which a docs-only run still executes.
"""
import sys
from pathlib import PurePosixPath
from typing import Iterable

# Markdown that is documentation by name, even inside a code tree.
_DOC_NAMES = {"CLAUDE.md"}
# Top-level trees that are code input even when the file is Markdown
# (system skills are loaded at startup; scripts and tests are executed or asserted on).
_CODE_TREES = ("src", "tests", "scripts", "prompts_snapshot")

# Unit tests that read files this module calls docs. A docs-only run executes exactly these;
# tests/unit/scripts/test_docs_only.py fails when another test starts reading docs.
DOC_READING_TESTS: tuple[str, ...] = (
    "tests/unit/domain/test_drive_search_v2_frozen_core.py",
    "tests/unit/test_facts_have_no_ttl_policy.py",
)


def _is_doc(path: str) -> bool:
    p = PurePosixPath(path.strip().replace("\\", "/"))
    parts = p.parts
    if not parts:
        return False
    if parts[0] == "docs":
        return True
    if p.suffix.lower() != ".md":
        return False
    if len(parts) == 1:  # *.md at the repo root
        return True
    if parts[0] == ".github":
        return True
    if parts[0] in _CODE_TREES:
        return p.name in _DOC_NAMES  # tests/CLAUDE.md, src/**/CLAUDE.md
    return False


def is_docs_only(paths: Iterable[str]) -> bool:
    cleaned = [p for p in (x.strip() for x in paths) if p]
    return bool(cleaned) and all(_is_doc(p) for p in cleaned)


def main(argv: list[str] | None = None) -> None:
    if "--doc-reading-tests" in (sys.argv[1:] if argv is None else argv):
        print(" ".join(DOC_READING_TESTS))
        return
    print("true" if is_docs_only(sys.stdin.read().splitlines()) else "false")


if __name__ == "__main__":
    main()
