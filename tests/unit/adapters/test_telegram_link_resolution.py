"""
Unit tests for TelegramResponseChannel._resolve_links_telegram().

IMPORTANT: _resolve_links_telegram() is called AFTER _format_for_platform().
_format_for_platform escapes '[' → '\\[' and ']' → '\\]', so anchor patterns
in the input are already escaped. Tests use pre-formatted text accordingly.

Tests cover:
- Reference-style \\[display\\]\\[N\\] → [display](url)
- Bare \\[N\\] → [escaped_title](url)
- URL escaping: ')' → '\\)' in links
- Normalization pre-pass: "escaped_title \\[N\\]" → "\\[escaped_title\\]\\[N\\]"
- No-op cases: None / empty link_list, unknown anchor
"""
import pytest
from unittest.mock import MagicMock, AsyncMock
from telegram import Bot

from src.adapters.telegram.response_channel import TelegramResponseChannel


@pytest.fixture
def channel():
    return TelegramResponseChannel(bot=AsyncMock(spec=Bot), chat_id=12345)


LINK_LIST = [
    {"anchor": 1, "title": "Cafe Ristorante", "url": "https://maps.google.com/?q=1"},
    {"anchor": 2, "title": "Trattoria Venezia", "url": "https://maps.google.com/?q=2"},
]


def _fmt(channel: TelegramResponseChannel, text: str) -> str:
    """Apply _format_for_platform then _resolve_links_telegram — mirrors send_message flow."""
    return channel._resolve_links_telegram(channel._format_for_platform(text), LINK_LIST)


class TestReferenceStyleAnchors:
    """\\[display\\]\\[N\\] → [display](url)"""

    def test_single_reference_anchor(self, channel):
        # Original text: "[Cafe Ristorante][1] for dinner."
        # After _format_for_platform: "\\[Cafe Ristorante\\]\\[1\\] for dinner\\."
        formatted = channel._format_for_platform("[Cafe Ristorante][1] for dinner.")
        result = channel._resolve_links_telegram(formatted, LINK_LIST)
        assert "[Cafe Ristorante](https://maps.google.com/?q=1)" in result
        assert "for dinner" in result

    def test_display_text_differs_from_title(self, channel):
        formatted = channel._format_for_platform("Visit [the best spot][1] tonight.")
        result = channel._resolve_links_telegram(formatted, LINK_LIST)
        assert "[the best spot](https://maps.google.com/?q=1)" in result

    def test_two_reference_anchors(self, channel):
        formatted = channel._format_for_platform("[Cafe Ristorante][1] and [Trattoria Venezia][2].")
        result = channel._resolve_links_telegram(formatted, LINK_LIST)
        assert "[Cafe Ristorante](https://maps.google.com/?q=1)" in result
        assert "[Trattoria Venezia](https://maps.google.com/?q=2)" in result


class TestBareNumericAnchors:
    """\\[N\\] → [escaped_title](url)"""

    def test_bare_anchor_uses_title(self, channel):
        formatted = channel._format_for_platform("Top pick: [1].")
        result = channel._resolve_links_telegram(formatted, LINK_LIST)
        assert "[Cafe Ristorante](https://maps.google.com/?q=1)" in result

    def test_two_bare_anchors(self, channel):
        formatted = channel._format_for_platform("Options: [1] or [2].")
        result = channel._resolve_links_telegram(formatted, LINK_LIST)
        assert "[Cafe Ristorante](https://maps.google.com/?q=1)" in result
        assert "[Trattoria Venezia](https://maps.google.com/?q=2)" in result


class TestUrlEscaping:
    """URLs containing ')' must be escaped as '\\)' for Telegram MarkdownV2."""

    def test_url_with_closing_paren(self, channel):
        link_list = [{"anchor": 1, "title": "Place", "url": "https://example.com/path(arg)"}]
        formatted = channel._format_for_platform("See [Place][1].")
        result = channel._resolve_links_telegram(formatted, link_list)
        assert "https://example.com/path(arg\\)" in result

    def test_url_without_special_chars_unchanged(self, channel):
        link_list = [{"anchor": 1, "title": "Place", "url": "https://maps.google.com/?q=1"}]
        formatted = channel._format_for_platform("[Place][1]")
        result = channel._resolve_links_telegram(formatted, link_list)
        assert "https://maps.google.com/?q=1" in result


class TestNormalizationPrePass:
    """'Title \\[N\\]' (plain text + escaped bare anchor) must not duplicate the name."""

    def test_title_followed_by_bare_anchor_no_duplication(self, channel):
        # LLM wrote "Cafe Ristorante [1]" — after formatting → "Cafe Ristorante \\[1\\]"
        # normalization → "\\[Cafe Ristorante\\]\\[1\\]" → "[Cafe Ristorante](url)"
        result = _fmt(channel, "Cafe Ristorante [1] is excellent.")
        assert result.count("Cafe Ristorante") == 1
        assert "https://maps.google.com/?q=1" in result

    def test_normalization_with_whitespace(self, channel):
        result = _fmt(channel, "Trattoria Venezia   [2] nearby.")
        assert result.count("Trattoria Venezia") == 1
        assert "https://maps.google.com/?q=2" in result


class TestNoOpCases:
    """No link_list or unknown anchor → formatted text returned unchanged."""

    def test_none_link_list(self, channel):
        formatted = channel._format_for_platform("Text with [1].")
        result = channel._resolve_links_telegram(formatted, None)
        assert result == formatted

    def test_empty_link_list(self, channel):
        formatted = channel._format_for_platform("Text with [1].")
        result = channel._resolve_links_telegram(formatted, [])
        assert result == formatted

    def test_empty_text(self, channel):
        result = channel._resolve_links_telegram("", LINK_LIST)
        assert result == ""

    def test_unknown_bare_anchor_unchanged(self, channel):
        formatted = channel._format_for_platform("No match [99].")
        result = channel._resolve_links_telegram(formatted, LINK_LIST)
        # \\[99\\] should remain unresolved (escaped form)
        assert "99" in result
        assert "https://" not in result

    def test_unknown_reference_anchor_unchanged(self, channel):
        formatted = channel._format_for_platform("[Label][99].")
        result = channel._resolve_links_telegram(formatted, LINK_LIST)
        assert "https://" not in result
