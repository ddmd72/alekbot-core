"""scripts/ci/docs_only.py: a change list is docs-only only when every path is documentation."""
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci.docs_only import is_docs_only

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
