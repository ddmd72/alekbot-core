"""The wrapper-tag escape stays linear on hostile spacing (USER_DRIVE_INDEXING_RFC §10.4, final review F3-3).

`escape_text` runs on the main event loop over every opened file (up to 12k-char windows and whole files). Two
adjacent unbounded gap groups around the optional slash backtracked quadratically on '<' + a long whitespace run:
32k characters took seconds. The gaps are now unambiguous and bounded (WRAPPER_TAG_GAP_MAX)."""
import pytest
import time

from src.domain.drive_index_params import WRAPPER_TAG_GAP_MAX
from src.domain.drive_search import escape_text


def _timed(text: str) -> float:
    started = time.perf_counter()
    escape_text(text)
    return time.perf_counter() - started


@pytest.mark.timing  # wall-clock assertion: off the main gate, `make test-timing`
def test_a_long_whitespace_run_after_an_opener_is_fast():
    assert _timed("<" + " " * 32_000) < 0.5          # was seconds (quadratic backtracking)


@pytest.mark.timing  # wall-clock assertion: off the main gate, `make test-timing`
def test_many_openers_each_with_whitespace_are_fast():
    assert _timed(("<" + " ​" * 40) * 400) < 0.5   # ~32k characters, invisible characters in the gaps


def test_look_alikes_within_the_bound_are_still_escaped():
    gap = " " * WRAPPER_TAG_GAP_MAX
    assert escape_text(f"<{gap}/{gap}drive_document").startswith("&lt;")
    assert escape_text("< / ​DRIVE_DOCUMENT").startswith("&lt;")
    assert escape_text("＜／ｄｒｉｖｅ＿ｄｏｃｕｍｅｎｔ").startswith("&lt;")
