"""
Unit tests for SlackResponseChannel._resolve_links_slack().

Tests cover:
- Reference-style anchors [display text][N] → <url|display text>
- Bare numeric anchors [N] → <url|title>
- Normalization pre-pass: "Title [N]" → "[Title][N]" prevents name duplication
- No-op cases: None / empty link_list, unknown anchor
- Multiple anchors in the same text
"""
import pytest
from unittest.mock import MagicMock

from src.adapters.slack.response_channel import SlackResponseChannel


@pytest.fixture
def channel():
    return SlackResponseChannel(
        app_client=MagicMock(),
        channel_id="C123",
        bot_token="xoxb-test",
    )


LINK_LIST = [
    {"anchor": 1, "title": "Cafe Ristorante", "url": "https://maps.google.com/?q=1"},
    {"anchor": 2, "title": "Trattoria Venezia", "url": "https://maps.google.com/?q=2"},
]


class TestReferenceStyleAnchors:
    """[display text][N] → <url|display text>"""

    def test_single_reference(self, channel):
        text = "Check out [Cafe Ristorante][1] for dinner."
        result = channel._resolve_links_slack(text, LINK_LIST)
        assert result == "Check out <https://maps.google.com/?q=1|Cafe Ristorante> for dinner."

    def test_display_text_differs_from_title(self, channel):
        text = "Go to [the best place][1]."
        result = channel._resolve_links_slack(text, LINK_LIST)
        assert result == "Go to <https://maps.google.com/?q=1|the best place>."

    def test_two_reference_anchors(self, channel):
        text = "[Cafe Ristorante][1] and [Trattoria Venezia][2]."
        result = channel._resolve_links_slack(text, LINK_LIST)
        assert "<https://maps.google.com/?q=1|Cafe Ristorante>" in result
        assert "<https://maps.google.com/?q=2|Trattoria Venezia>" in result


class TestBareNumericAnchors:
    """[N] → <url|title from link_list>"""

    def test_bare_anchor_uses_title(self, channel):
        text = "Best option: [1]."
        result = channel._resolve_links_slack(text, LINK_LIST)
        assert result == "Best option: <https://maps.google.com/?q=1|Cafe Ristorante>."

    def test_two_bare_anchors(self, channel):
        text = "Option A [1] or option B [2]."
        result = channel._resolve_links_slack(text, LINK_LIST)
        assert "<https://maps.google.com/?q=1|Cafe Ristorante>" in result
        assert "<https://maps.google.com/?q=2|Trattoria Venezia>" in result


class TestNormalizationPrePass:
    """'Title [N]' pattern (plain text + bare anchor) must not duplicate the name."""

    def test_title_followed_by_bare_anchor_no_duplication(self, channel):
        # LLM wrote "Cafe Ristorante [1]" — without normalization this would
        # produce "Cafe Ristorante <url|Cafe Ristorante>" (name appears twice).
        text = "Cafe Ristorante [1] is excellent."
        result = channel._resolve_links_slack(text, LINK_LIST)
        assert result == "<https://maps.google.com/?q=1|Cafe Ristorante> is excellent."
        # Name must appear exactly once — as the link label, not as plain text before it.
        assert result.count("Cafe Ristorante") == 1

    def test_normalization_with_whitespace_between_title_and_anchor(self, channel):
        text = "Trattoria Venezia   [2] is nearby."
        result = channel._resolve_links_slack(text, LINK_LIST)
        assert result.count("Trattoria Venezia") == 1
        assert "https://maps.google.com/?q=2" in result


class TestNoOpCases:
    """No link_list or unknown anchor → text returned unchanged."""

    def test_none_link_list(self, channel):
        text = "Some text with [1] anchor."
        assert channel._resolve_links_slack(text, None) == text

    def test_empty_link_list(self, channel):
        text = "Some text with [1] anchor."
        assert channel._resolve_links_slack(text, []) == text

    def test_empty_text(self, channel):
        assert channel._resolve_links_slack("", LINK_LIST) == ""

    def test_unknown_anchor_unchanged(self, channel):
        text = "No match here [99]."
        result = channel._resolve_links_slack(text, LINK_LIST)
        assert result == "No match here [99]."

    def test_unknown_reference_anchor_unchanged(self, channel):
        text = "No match [Label][99]."
        result = channel._resolve_links_slack(text, LINK_LIST)
        assert result == "No match [Label][99]."
