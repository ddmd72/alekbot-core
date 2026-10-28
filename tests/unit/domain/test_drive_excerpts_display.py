"""A9: markdownify's escapes are undone at render, for the kinds markitdown converts only."""
import pytest

from src.adapters.drive_parser.readers import MARKITDOWN_CONVERTERS
from src.domain.drive_excerpts import MARKDOWN_ESCAPED_KINDS, display_text

ESCAPED = r"Tom\_Baker \*draft\* see \[R&D\] \# 3"
PLAIN = "Tom_Baker *draft* see [R&D] # 3"


def test_the_domain_kinds_are_the_adapters_converters():
    assert MARKDOWN_ESCAPED_KINDS == set(MARKITDOWN_CONVERTERS)


@pytest.mark.parametrize("kind", ["docx", "pptx", "epub", "msg"])
def test_escapes_are_undone_for_markitdown_kinds(kind):
    assert display_text(ESCAPED, kind) == PLAIN


@pytest.mark.parametrize("kind", ["pdf", "markdown", "text", "xlsx", "html", ""])
def test_other_kinds_are_verbatim(kind):
    assert display_text(ESCAPED, kind) == ESCAPED


def test_other_backslashes_stay():
    assert display_text(r"C:\Acme\plan and 50\%", "docx") == r"C:\Acme\plan and 50\%"
