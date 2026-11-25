import pytest

from src.domain.link_previews import LINK_PREVIEW_MAX_LINKS, distinct_link_count, link_previews_allowed

A, B, C = "https://example.com/a", "https://example.org/b?x=1&y=2", "http://docs.example.net/c"


def test_threshold_is_two():
    assert LINK_PREVIEW_MAX_LINKS == 2


@pytest.mark.parametrize("text,allowed", [
    ("", True),
    ("no links here", True),
    (f"one {A}", True),
    (f"two {A} and {B}", True),
    (f"three {A} {B} {C}", False),
])
def test_threshold(text, allowed):
    assert link_previews_allowed(text) is allowed


def test_duplicates_count_once():
    assert distinct_link_count(f"{A} again {A} and {A}") == 1
    assert link_previews_allowed(f"{A} {A} {B} {B} {A}")


def test_amp_and_plain_ampersand_forms_are_one_url():
    plain = "https://example.org/b?x=1&y=2"
    escaped = "https://example.org/b?x=1&amp;y=2"
    assert distinct_link_count(f"{plain} <{escaped}|t>") == 1


def test_slack_mrkdwn_forms():
    assert distinct_link_count(f"<{A}|first> <{B}> <{C}|third>") == 3
    assert distinct_link_count(f"<{A}|first> <{A}>") == 1


def test_telegram_markdownv2_inline_links():
    assert distinct_link_count(f"[one]({A}) and [two]({B}) and [three]({C})") == 3


def test_url_in_parentheses_and_trailing_punctuation():
    assert distinct_link_count(f"(see {A}). Also {A}, {A}; {A}! {A}?") == 1
    assert distinct_link_count(f"*{A}* and _{B}_") == 2


def test_non_http_schemes_are_not_links():
    assert distinct_link_count("ftp://example.com/a mailto:x@example.com example.com/path") == 0
