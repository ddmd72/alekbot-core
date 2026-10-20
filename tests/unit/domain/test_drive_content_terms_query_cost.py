"""Query-term extraction cost (USER_DRIVE_INDEXING_RFC §24.6, arbiter R2 S-m1). `drive_query` has no length cap and the
extraction runs on the event loop: claiming each span must not scan every span claimed before it (it took 42 s on
`'+1 ' * 40000`), and the search feeds it at most QUERY_TERM_CHARS characters, cut at a word boundary."""
import pytest
import time

from src.domain.drive_content_terms import clip_query, query_terms
from src.domain.drive_index_params import QUERY_TERM_CHARS


@pytest.mark.timing  # wall-clock assertion: off the main gate, `make test-timing`
def test_many_spans_are_claimed_in_linear_time():
    started = time.monotonic()
    assert query_terms("+1 " * 40000) == ()
    assert time.monotonic() - started < 5.0   # generous: quadratic took ~42 s, linear well under a second


@pytest.mark.timing  # wall-clock assertion: off the main gate, `make test-timing`
def test_many_overlapping_kinds_still_resolve_as_before():
    text = " ".join(f"INV-{i:04d}/2024 15.03.2024 1 250,00" for i in range(3000))
    started = time.monotonic()
    terms = set(query_terms(text))
    assert time.monotonic() - started < 5.0
    assert {"2024-03-15", "1250.00", "inv00012024"} <= terms and "2024" not in terms


def test_the_cap_is_a_thousand_characters():
    assert QUERY_TERM_CHARS == 1000


def test_clip_keeps_a_short_query_whole():
    assert clip_query("invoice 98765", 1000) == "invoice 98765"


def test_clip_cuts_at_a_word_boundary_never_inside_a_token():
    query = "a" * 990 + " INV-2024/0012 tail"
    clipped = clip_query(query, 1000)
    assert clipped == "a" * 990 and len(clipped) <= 1000
    assert query_terms(clipped) == ()   # no truncated "inv2024" fragment out of the cut token


def test_clip_of_one_long_token_is_its_prefix():
    assert clip_query("9" * 1500, 1000) == "9" * 1000


def test_clip_at_an_exact_boundary_keeps_the_last_word():
    assert clip_query("ab 98765 more", 8) == "ab 98765"
