"""Living docs: the shared parser of the `<!-- living-doc` header (stdlib only).

A living doc is a `docs/**/*.md` whose text starts with the marker; its header lists, one per line under `covers:`,
the repository paths, directories (trailing `/`) and fnmatch globs it owns. An index doc that owns no code writes
exactly `covers: none`. The contract: docs/04_solution_strategy/decisions/living_docs_contract.md.
Used by tests/unit/test_living_doc_references.py and scripts/ci/living_docs_pr_check.py.
"""
import fnmatch
import re
import subprocess
from datetime import date
from pathlib import Path

HEADER = "<!-- living-doc"
NO_COVERS = "none"


def tracked_files(root: Path) -> list[str]:
    """Repository files as posix paths: `git ls-files`, or a filesystem walk when git is unavailable."""
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True, timeout=30
        ).stdout
        files = [line for line in out.splitlines() if line]
        if files:
            return files
    except (OSError, subprocess.SubprocessError):
        pass
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", ".codegraph"}
    return [
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and not skip.intersection(p.relative_to(root).parts)
    ]


def parse_header(text: str) -> tuple[list[str], str | None, str] | None:
    """(covers lines, audited value or None, body without the header) for a doc with the header; else None.

    The optional `audited: YYYY-MM-DD` line ends the covers list wherever it sits and is never a covers path.
    A doc that owns no code has the single covers line `none`; `effective_covers` turns it into an empty list.
    """
    if not text.startswith(HEADER):
        return None
    end = text.find("-->")
    if end == -1:
        return [], None, text
    header, body = text[:end], text[end + 3:]
    covers: list[str] = []
    audited: str | None = None
    in_covers = False
    for line in header.splitlines():
        stripped = line.strip()
        if stripped.startswith("audited:"):
            audited = stripped[len("audited:"):].strip()
            in_covers = False
            continue
        if stripped.startswith("covers:"):
            in_covers = True
            stripped = stripped[len("covers:"):].strip()
        if in_covers and stripped:
            covers.append(stripped)
    return covers, audited, body


def effective_covers(covers: list[str]) -> list[str]:
    """The cover lines that match files: none for the `covers: none` form."""
    return [] if covers == [NO_COVERS] else covers


def valid_iso_date(value: str) -> bool:
    try:
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)) and bool(date.fromisoformat(value))
    except ValueError:
        return False


def cover_matches(line: str, files: list[str]) -> bool:
    if line.endswith("/"):
        return any(f.startswith(line) for f in files)
    return line in files or any(fnmatch.fnmatchcase(f, line) for f in files)


def living_docs(root: Path) -> list[Path]:
    """Every `docs/**/*.md` whose text starts with the living-doc marker, sorted."""
    return [
        p for p in sorted((root / "docs").rglob("*.md"))
        if p.is_file() and p.read_text(encoding="utf-8").startswith(HEADER)
    ]


# Living docs by location; decision records and RFCs are never living docs.
_REQUIRED_GLOBS = (
    "docs/05_building_blocks/**/*.md",
    "docs/06_runtime/*.md",
    "docs/07_deployment/*.md",
    "docs/11_quality/*.md",
    "docs/12_risks/*.md",
    "docs/how_to/*.md",
)
_REQUIRED_FILES = (
    "docs/README.md",
    "docs/04_solution_strategy/README.md",
    "docs/08_concepts/README.md",
    "docs/08_concepts/DATABASE_SCHEMA.md",
    "docs/08_concepts/multi_vector_rrf_search.md",
    "docs/08_concepts/groovy_prompt_pattern.md",
)


def required_living_docs(root: Path) -> list[Path]:
    """The docs that must start with the living-doc header, by location (docs/README.md names the same set)."""
    found = {p for pattern in _REQUIRED_GLOBS for p in root.glob(pattern) if p.is_file()}
    found.update(root / f for f in _REQUIRED_FILES if (root / f).is_file())
    return sorted(found)


def covers_conflicts(docs: dict[str, list[str]], files: list[str]) -> list[str]:
    """One owner per file: a message for every file that the covers of two or more docs match."""
    problems: list[str] = []
    for f in sorted(files):
        owners = sorted(d for d, covers in docs.items() if any(cover_matches(c, [f]) for c in effective_covers(covers)))
        if len(owners) > 1:
            problems.append(f"{f} is covered by {' and '.join(owners)}")
    return problems
