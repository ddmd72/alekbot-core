"""The `document` family of Search v2 (USER_DRIVE_INDEXING_RFC §25.5.5, D23; §25.13.6 drive_kind text): Markdown and
plain text are documents; HTML, e-mail and e-books are not (OQ-V8)."""
import re

import pytest

from src.domain.drive_search import KIND_FAMILIES, kinds_for
from src.domain.drive_tool_texts import KIND

_DOCUMENT = frozenset({"docx", "doc", "odt", "rtf", "pdf", "markdown", "text"})


def test_document_family_includes_markdown_and_text():
    assert KIND_FAMILIES["document"] == _DOCUMENT


@pytest.mark.parametrize("kind", ["html", "mhtml", "eml", "msg", "epub"])
def test_document_family_leaves_out_web_pages_mail_and_books(kind):
    assert kind not in KIND_FAMILIES["document"]


@pytest.mark.parametrize("word", ["document", "documents", "docs"])
def test_a_document_request_covers_markdown_and_text(word):
    assert kinds_for(word) == _DOCUMENT


def test_the_manifest_text_lists_the_family_as_built():
    listed = re.search(r"document \(([^)]*)\)", KIND)
    assert listed is not None
    assert frozenset(k.strip() for k in listed.group(1).split(",")) == KIND_FAMILIES["document"]


def test_other_families_are_unchanged():
    assert KIND_FAMILIES["spreadsheet"] == {"xlsx", "xls", "ods", "csv"}
    assert KIND_FAMILIES["word"] == {"docx", "doc"}
    assert KIND_FAMILIES["presentation"] == {"pptx", "ppt", "odp"}
    assert KIND_FAMILIES["photo"] == {"image"}
