"""
`TelegramResponseChannel.send_file` must carry `thread_id` through to Telegram's
`send_document` as `message_thread_id` — the forum-topic routing `send_message` already does,
which `send_file` silently dropped (every file landed in the chat's General topic instead of
the topic the conversation was happening in).

Also proves the MarkdownV2 formatter (`_format_for_platform`, used by `send_message`) leaves a
`skill_preview` save command intact: inside a code span, `$` needs no escaping, and backticks
are not in `_TG_ESCAPE_CHARS`.
"""
import io

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.adapters.telegram.response_channel import TelegramResponseChannel


@pytest.fixture
def mock_bot():
    return AsyncMock()


@pytest.fixture
def response_channel(mock_bot):
    return TelegramResponseChannel(bot=mock_bot, chat_id=123456789)


class TestTelegramSendFileThread:
    async def test_send_file_with_thread_id_passes_message_thread_id(self, response_channel, mock_bot):
        await response_channel.send_file(
            content=b"file bytes",
            filename="my-skill.SKILL.md",
            title="Skill draft: my-skill",
            thread_id="42",
        )

        mock_bot.send_document.assert_awaited_once()
        call_kwargs = mock_bot.send_document.call_args.kwargs
        assert call_kwargs["message_thread_id"] == 42
        assert call_kwargs["chat_id"] == 123456789
        assert call_kwargs["filename"] == "my-skill.SKILL.md"
        assert call_kwargs["caption"] == "Skill draft: my-skill"
        assert isinstance(call_kwargs["document"], io.BytesIO)
        assert call_kwargs["document"].read() == b"file bytes"

    async def test_send_file_without_thread_id_passes_none(self, response_channel, mock_bot):
        await response_channel.send_file(
            content=b"file bytes",
            filename="x.SKILL.md",
            title="Skill draft: x",
            thread_id=None,
        )

        call_kwargs = mock_bot.send_document.call_args.kwargs
        assert call_kwargs["message_thread_id"] is None

    async def test_send_file_empty_string_thread_id_passes_none(self, response_channel, mock_bot):
        """Keeps current DM behaviour: a falsy thread_id (e.g. '') must not crash int()."""
        await response_channel.send_file(
            content=b"file bytes",
            filename="x.SKILL.md",
            title="Skill draft: x",
            thread_id="",
        )

        call_kwargs = mock_bot.send_document.call_args.kwargs
        assert call_kwargs["message_thread_id"] is None


class TestMarkdownV2LeavesSkillCommandIntact:
    """`_format_for_platform` is the formatter `send_message` runs text through."""

    def test_code_span_save_command_untouched(self, response_channel):
        formatted = response_channel._format_for_platform("`$skill save 7f3a`")
        assert formatted == "`$skill save 7f3a`"

    async def test_send_message_delivers_save_command_verbatim(self, response_channel, mock_bot):
        mock_message = MagicMock()
        mock_message.message_id = 1
        mock_bot.send_message.return_value = mock_message

        await response_channel.send_message("`$skill save 7f3a`", thread_id="42")

        call_kwargs = mock_bot.send_message.call_args.kwargs
        assert call_kwargs["text"] == "`$skill save 7f3a`"
        assert call_kwargs["parse_mode"] == "MarkdownV2"
        assert call_kwargs["message_thread_id"] == 42
