"""
Unit tests for the "thinking" status phrase rotation.

The animation loop rotates the status bubble to a fresh phrase of the active
status type every ~5s so a long wait stays lively. The rotation decision lives
in ConversationHandler._pick_next_status_phrase (extracted from the timing loop
so it is testable without real sleeps).
"""
import pytest
from unittest.mock import AsyncMock

from src.handlers.conversation_handler import ConversationHandler
from src.domain.ui_messages import StatusType
from src.locales import uk, en, fr, es


def _handler() -> ConversationHandler:
    # _pick_next_status_phrase touches no instance state — bypass the constructor.
    return ConversationHandler.__new__(ConversationHandler)


class TestPickNextStatusPhrase:
    async def test_returns_fresh_phrase(self):
        handler = _handler()
        channel = AsyncMock()
        channel.get_status_phrase = AsyncMock(return_value="phrase-B")

        result = await handler._pick_next_status_phrase(channel, StatusType.THINKING, "phrase-A")

        assert result == "phrase-B"
        channel.get_status_phrase.assert_awaited_with(StatusType.THINKING)

    async def test_avoids_immediate_repeat(self):
        """First two draws collide with current → retries until a different one."""
        handler = _handler()
        channel = AsyncMock()
        channel.get_status_phrase = AsyncMock(side_effect=["same", "same", "different"])

        result = await handler._pick_next_status_phrase(channel, StatusType.THINKING, "same")

        assert result == "different"
        assert channel.get_status_phrase.await_count == 3

    async def test_single_phrase_pool_does_not_loop_forever(self):
        """A pool that always yields the current phrase returns it after bounded tries."""
        handler = _handler()
        channel = AsyncMock()
        channel.get_status_phrase = AsyncMock(return_value="only")

        result = await handler._pick_next_status_phrase(channel, StatusType.THINKING, "only")

        assert result == "only"
        assert channel.get_status_phrase.await_count == 4  # bounded retry budget

    async def test_uses_active_status_type(self):
        handler = _handler()
        channel = AsyncMock()
        channel.get_status_phrase = AsyncMock(return_value="file-phrase")

        await handler._pick_next_status_phrase(channel, StatusType.PROCESSING_FILE, "x")

        channel.get_status_phrase.assert_awaited_with(StatusType.PROCESSING_FILE)


class TestThinkingPoolExpanded:
    """The rotation is only fun with a substantial, duplicate-free pool."""

    @pytest.mark.parametrize("mod", [uk, en, fr, es], ids=["uk", "en", "fr", "es"])
    def test_pool_is_large_and_unique(self, mod):
        messages = getattr(mod, [k for k in dir(mod) if k.endswith("_MESSAGES")][0])
        phrases = messages[StatusType.THINKING.value]
        assert len(phrases) >= 20
        assert len(phrases) == len(set(phrases))
