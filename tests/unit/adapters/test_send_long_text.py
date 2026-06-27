"""
send_long_text() — adapter-owned single-vs-thread delivery decision.

Regression for the 2026-06-25 reminder truncation: the body was 1968 chars raw
(under Slack's 2500 limit) but expanded to 2678 chars once its 8 [N] anchors were
resolved into <url|title> markup, so the old service-level gate — which measured
the RAW length — picked the single-message path and SlackResponseChannel.send_message
hard-truncated it instead of threading.

send_long_text moves the decision into the adapter, which measures the RENDERED
length (the same string send_message would truncate on), so a body that fits raw
but overflows once links expand is threaded, not cut.
"""
import pytest
from unittest.mock import AsyncMock

from src.adapters.slack.response_channel import (
    SLACK_MAX_MESSAGE_LENGTH,
    SlackResponseChannel,
)
from src.adapters.telegram.response_channel import (
    TELEGRAM_MAX_MESSAGE_LENGTH,
    TelegramResponseChannel,
)
from src.domain.language import LanguageCode


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

class TestSlackSendLongText:
    def _channel(self) -> SlackResponseChannel:
        ch = SlackResponseChannel(AsyncMock(), "C1", "token", language=LanguageCode.EN)
        # Spy on the two delivery primitives; keep _resolve_links_slack /
        # _format_for_platform real — they ARE the logic under test.
        ch.send_message = AsyncMock(return_value={"ts": "ph-ts", "channel": "D9"})
        ch.send_chunked_message = AsyncMock()
        return ch

    @pytest.mark.asyncio
    async def test_raw_under_limit_but_rendered_over_threads(self):
        """The incident: raw < 2500 but link resolution pushes rendered > 2500 → thread."""
        ch = self._channel()
        # raw 2454 chars (< 2500); one anchor whose URL resolution adds ~120 chars,
        # pushing the rendered string past 2500.
        text = "A" * 2450 + " [1]"
        link_list = [{"anchor": "1", "title": "Report", "url": "https://example.com/" + "p" * 110}]

        # Precondition: the raw text really is under the limit (old gate would have
        # chosen the truncating single-message path) while the rendered text is over it.
        assert len(text) <= SLACK_MAX_MESSAGE_LENGTH
        rendered = ch._format_for_platform(ch._resolve_links_slack(text, link_list))
        assert len(rendered) > SLACK_MAX_MESSAGE_LENGTH

        await ch.send_long_text(text, link_list=link_list)

        # Placeholder posted, then full body + link_list handed to the threaded path.
        ch.send_message.assert_awaited_once_with("📩", None)
        ch.send_chunked_message.assert_awaited_once()
        args, kwargs = ch.send_chunked_message.call_args
        assert args[0] == text
        assert args[1] == "ph-ts"
        assert kwargs["link_list"] == link_list

    @pytest.mark.asyncio
    async def test_short_text_single_message_no_placeholder(self):
        """Rendered fits → one message, no 📩 placeholder, no chunking."""
        ch = self._channel()
        text = "See [Report][1]."
        link_list = [{"anchor": "1", "title": "Report", "url": "https://example.com/r"}]

        await ch.send_long_text(text, link_list=link_list)

        ch.send_chunked_message.assert_not_awaited()
        ch.send_message.assert_awaited_once_with(text, thread_id=None, link_list=link_list)

    @pytest.mark.asyncio
    async def test_plain_long_text_without_links_threads(self):
        """No link_list, but raw body already over the limit → thread."""
        ch = self._channel()
        text = "B" * (SLACK_MAX_MESSAGE_LENGTH + 200)

        await ch.send_long_text(text)

        ch.send_message.assert_awaited_once_with("📩", None)
        ch.send_chunked_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

class TestTelegramSendLongText:
    def _channel(self) -> TelegramResponseChannel:
        ch = TelegramResponseChannel(AsyncMock(), 42, language=LanguageCode.EN)
        ch.send_message = AsyncMock(return_value="ph-id")
        ch.send_chunked_message = AsyncMock()
        return ch

    @pytest.mark.asyncio
    async def test_raw_over_safe_length_sends_sequential_messages(self):
        """Telegram truncates raw at 0.7×max (2867); a 3000-char body must split.

        Telegram DMs have no threads, so overflow is sent as SEQUENTIAL messages —
        no contentless "📩" placeholder, no send_chunked indirection. The first
        message carries real content.
        """
        ch = self._channel()
        safe_length = int(TELEGRAM_MAX_MESSAGE_LENGTH * 0.7)
        text = "C" * 3000
        assert len(text) > safe_length
        assert len(text) <= TELEGRAM_MAX_MESSAGE_LENGTH  # under the OLD (wrong) gate

        await ch.send_long_text(text)

        ch.send_chunked_message.assert_not_awaited()
        # Split into >= 2 real messages; first one is content, not a marker.
        assert ch.send_message.await_count >= 2
        first_arg = ch.send_message.await_args_list[0].args[0]
        assert first_arg.startswith("C") and "📩" not in first_arg

    @pytest.mark.asyncio
    async def test_short_text_single_message(self):
        ch = self._channel()
        text = "Short reply."

        await ch.send_long_text(text)

        ch.send_chunked_message.assert_not_awaited()
        ch.send_message.assert_awaited_once_with(text, thread_id=None, link_list=None)
