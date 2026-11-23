"""A Settings row on a phone: a long value must not squeeze the title to a letter per line. The title keeps a minimum
share of the row and breaks only between words; the value is cut with an ellipsis."""
import re

from tests.unit.web.cabinet_source import STATIC

_CSS = (STATIC / "cabinet.css").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    return re.search(r"\n\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", _CSS).group(1)


def test_the_row_title_has_a_minimum_width_and_never_breaks_inside_a_word_by_choice():
    rule = _rule(".c-row-title")
    assert re.search(r"min-width:\s*[1-9]\d*%", rule)
    assert "overflow-wrap: anywhere" not in rule and "word-break: break-all" not in rule


def test_the_row_value_may_shrink_and_ends_with_an_ellipsis_on_one_line():
    rule = _rule(".c-row-value")
    assert "min-width: 0" in rule
    assert "text-overflow: ellipsis" in rule and "white-space: nowrap" in rule and "overflow: hidden" in rule
    assert re.search(r"max-width:\s*\d+%", rule)
