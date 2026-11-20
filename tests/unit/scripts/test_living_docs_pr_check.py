"""scripts/ci/living_docs_pr_check.py: a PR touching covered code changes the living doc or says why not."""
import pytest

from scripts.ci.living_docs_pr_check import missing_doc_lines

DOCS = {"docs/a.md": ["src/a/", "src/b/*.py", "src/c.py"], "docs/b.md": ["src/c.py"]}
TRACKED = ["src/a/x.py", "src/b/y.py", "src/c.py", "src/d.py", "docs/a.md", "docs/b.md"]


def _missing(changed, body="", docs=DOCS):
    return missing_doc_lines(changed, body, docs, TRACKED)


def test_a_change_outside_every_cover_requires_nothing():
    assert _missing(["src/d.py", "README.md"]) == []


def test_an_affected_doc_without_a_line_is_missing_with_a_paste_ready_line():
    assert _missing(["src/a/x.py"]) == ["Docs not affected: docs/a.md — <why>"]


def test_a_doc_changed_in_the_pr_is_ok():
    assert _missing(["src/a/x.py", "docs/a.md"]) == []


@pytest.mark.parametrize("sep", ["—", "–", "-"])
def test_the_line_in_the_body_excuses_the_doc_with_any_separator(sep):
    body = f"Summary\n\nDocs not affected: docs/a.md {sep} only a log line changed\n"
    assert _missing(["src/a/x.py"], body) == []


@pytest.mark.parametrize("body", [
    "Docs not affected: docs/a.md — ",
    "Docs not affected: docs/a.md —   ",
    "Docs not affected: docs/a.md",
    "Docs not affected: docs/other.md — wrong doc",
    "Docs not affected: docs/a.md.bak — wrong doc",
])
def test_an_empty_reason_or_another_doc_path_does_not_excuse(body):
    assert _missing(["src/a/x.py"], body) == ["Docs not affected: docs/a.md — <why>"]


def test_a_glob_cover_matches():
    assert _missing(["src/b/y.py"]) == ["Docs not affected: docs/a.md — <why>"]
    assert _missing(["src/b/y.txt"]) == []


def test_a_directory_cover_matches_nested_files():
    assert _missing(["src/a/deep/z.py"]) == ["Docs not affected: docs/a.md — <why>"]


def test_two_affected_docs_give_two_lines():
    assert _missing(["src/c.py"]) == [
        "Docs not affected: docs/a.md — <why>",
        "Docs not affected: docs/b.md — <why>",
    ]


def test_one_line_excuses_only_its_own_doc():
    body = "Docs not affected: docs/a.md — same as before"
    assert _missing(["src/c.py"], body) == ["Docs not affected: docs/b.md — <why>"]


def test_a_deleted_covered_file_still_requires_the_line():
    changed = ["src/a/gone.py"]  # absent from TRACKED: the file was deleted by the PR
    assert "src/a/gone.py" not in TRACKED
    assert _missing(changed) == ["Docs not affected: docs/a.md — <why>"]


def test_a_doc_that_covers_none_is_never_affected():
    assert _missing(["src/a/x.py"], docs={"docs/index.md": ["none"]}) == []
