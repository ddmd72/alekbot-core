"""scripts/ci/docs_only.py: a change list is docs-only only when every path is documentation."""
import re
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci.docs_only import DOC_READING_TESTS, is_docs_only

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("paths", [
    ["docs/a.md"],
    ["docs/diagram.png", "docs/10_rfcs/X.txt"],
    ["CLAUDE.md", "README.md", "BOOTSTRAP.md"],
    ["tests/CLAUDE.md"],
    ["src/agents/CLAUDE.md", "src/adapters/CLAUDE.md"],
    [".github/pull_request_template.md"],
    ["docs/a.md", "CLAUDE.md", "tests/CLAUDE.md"],
])
def test_docs_only_sets(paths):
    assert is_docs_only(paths) is True


@pytest.mark.parametrize("path", [
    "src/skills/smart/x/SKILL.md",
    "src/domain/notes.md",
    "src/x.py",
    "tests/unit/test_a.py",
    "tests/fixtures/sample.md",
    "scripts/ci/README.md",
    "scripts/x.py",
    "prompts_snapshot/a.md",
    "notes.txt",
    "config.json",
    "Makefile",
    ".github/workflows/ci.yml",
    "requirements.txt",
    "somedir/readme.md",
])
def test_code_input_is_not_docs(path):
    assert is_docs_only([path]) is False


def test_mixed_set_is_not_docs_only():
    assert is_docs_only(["docs/a.md", "src/x.py"]) is False


def test_empty_set_is_not_docs_only():
    assert is_docs_only([]) is False
    assert is_docs_only(["", "  "]) is False


@pytest.mark.parametrize("stdin,expected", [
    ("docs/a.md\nCLAUDE.md\n", "true"),
    ("docs/a.md\nsrc/x.py\n", "false"),
    ("src/skills/smart/x/SKILL.md\n", "false"),
    ("", "false"),
])
def test_cli_prints_verdict(stdin, expected):
    out = subprocess.run(
        [sys.executable, "scripts/ci/docs_only.py"], input=stdin, capture_output=True, text=True, cwd=REPO_ROOT, check=True
    ).stdout.strip()
    assert out == expected


def test_a_rename_out_of_code_is_not_docs_only():
    # The CI diff runs with --no-renames, so the deleted old path is listed beside the new one.
    assert is_docs_only(["src/skills/smart/x/SKILL.md", "docs/SKILL.md"]) is False


def test_cli_lists_the_doc_reading_tests():
    out = subprocess.run(
        [sys.executable, "scripts/ci/docs_only.py", "--doc-reading-tests"], capture_output=True, text=True, cwd=REPO_ROOT, check=True
    ).stdout.split()
    assert out == list(DOC_READING_TESTS)
    assert all((REPO_ROOT / p).is_file() for p in out)


# What "reads documentation": a path joined with the docs tree, a markdown glob, or a tree walk that names ".md".
_JOINS_DOCS = re.compile(r"""/\s*["']docs[/"']""")
_GLOBS_MD = re.compile(r"""glob\(\s*["'][^"']*\*\.md""")
_WALKS_MD = re.compile(r"""os\.walk""")
_NAMES_MD = re.compile(r"""["']\.md["']""")


def _reads_docs(text: str) -> bool:
    return bool(_JOINS_DOCS.search(text) or _GLOBS_MD.search(text) or (_WALKS_MD.search(text) and _NAMES_MD.search(text)))


def test_every_test_that_reads_docs_is_in_doc_reading_tests():
    found = {
        p.relative_to(REPO_ROOT).as_posix()
        for p in (REPO_ROOT / "tests" / "unit").rglob("*.py")
        if p.name != Path(__file__).name and _reads_docs(p.read_text(encoding="utf-8"))
    }
    assert found == set(DOC_READING_TESTS), f"tests reading docs: {sorted(found)}; add them to DOC_READING_TESTS"
