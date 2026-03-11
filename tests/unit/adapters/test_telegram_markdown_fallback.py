"""
Unit tests for Telegram MarkdownV2 fallback logic.

Tests the graceful degradation when markdown parsing fails due to unpaired tags.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from telegram import Bot, Message
from telegram.error import BadRequest

from src.adapters.telegram.response_channel import TelegramResponseChannel


@pytest.fixture
def mock_bot():
    """Create mock Telegram bot."""
    bot = AsyncMock(spec=Bot)
    return bot


@pytest.fixture
def response_channel(mock_bot):
    """Create TelegramResponseChannel with mock bot."""
    return TelegramResponseChannel(bot=mock_bot, chat_id=12345)


class TestMarkdownValidation:
    """Test markdown pair validation logic."""

    def test_validate_paired_asterisks(self, response_channel):
        """Properly paired asterisks should validate."""
        text = "This is *bold* text with *more bold*"
        assert response_channel._validate_markdown_pairs(text) is True

    def test_validate_unpaired_asterisks(self, response_channel):
        """Unpaired asterisks should fail validation."""
        text = "This is *bold text without closing"
        assert response_channel._validate_markdown_pairs(text) is False

    def test_validate_paired_underscores(self, response_channel):
        """Properly paired underscores should validate."""
        text = "This is _italic_ text with _more italic_"
        assert response_channel._validate_markdown_pairs(text) is True

    def test_validate_unpaired_underscores(self, response_channel):
        """Unpaired underscores should fail validation."""
        text = "This is _italic text without closing"
        assert response_channel._validate_markdown_pairs(text) is False

    def test_validate_escaped_underscores(self, response_channel):
        """Escaped underscores should not affect pairing."""
        text = "This has \\_escaped\\_ and _real_ underscores"
        assert response_channel._validate_markdown_pairs(text) is True

    def test_validate_mixed_tags(self, response_channel):
        """Mixed bold and italic should both be paired."""
        text = "This has *bold* and _italic_"
        assert response_channel._validate_markdown_pairs(text) is True

    def test_validate_mixed_tags_one_unpaired(self, response_channel):
        """Mixed with one unpaired should fail."""
        text = "This has *bold and _italic_"
        assert response_channel._validate_markdown_pairs(text) is False


class TestMarkdownSanitization:
    """Test markdown sanitization logic."""

    def test_sanitize_unpaired_asterisk(self, response_channel):
        """Should remove last unpaired asterisk."""
        text = "This is *bold without closing"
        result = response_channel._sanitize_unpaired_tags(text)
        
        # Should remove the unpaired asterisk
        assert result == "This is bold without closing"

    def test_sanitize_unpaired_underscore(self, response_channel):
        """Should remove last unpaired underscore."""
        text = "This is _italic without closing"
        result = response_channel._sanitize_unpaired_tags(text)
        
        # Should remove the unpaired underscore
        assert result == "This is italic without closing"

    def test_sanitize_preserves_paired_tags(self, response_channel):
        """Properly paired tags should not be touched."""
        text = "This has *bold* and _italic_"
        result = response_channel._sanitize_unpaired_tags(text)
        
        assert result == text  # Unchanged

    def test_sanitize_escaped_underscores(self, response_channel):
        """Should preserve escaped underscores."""
        text = "This has \\_escaped and _unpaired"
        result = response_channel._sanitize_unpaired_tags(text)
        
        # Should remove only the real unpaired underscore
        assert "\\_escaped" in result
        assert result.count('_') == 1  # \\_escaped contains one underscore; unpaired removed


class TestSendMessageFallback:
    """Test send_message with markdown fallback."""

    @pytest.mark.asyncio
    async def test_send_message_success_with_valid_markdown(self, response_channel, mock_bot):
        """Valid markdown should send successfully."""
        mock_message = MagicMock(spec=Message)
        mock_message.message_id = 123
        mock_bot.send_message.return_value = mock_message

        text = "This is **bold** text"
        result = await response_channel.send_message(text)

        assert result == "123"
        mock_bot.send_message.assert_called_once()
        call_kwargs = mock_bot.send_message.call_args.kwargs
        assert call_kwargs["parse_mode"] == "MarkdownV2"

    @pytest.mark.asyncio
    async def test_send_message_sanitizes_unpaired_tags(self, response_channel, mock_bot):
        """Unpaired tags should be sanitized before sending."""
        mock_message = MagicMock(spec=Message)
        mock_message.message_id = 123
        mock_bot.send_message.return_value = mock_message

        # Text with unpaired asterisk (simulating truncation)
        text = "This is **bold text without closing"
        result = await response_channel.send_message(text)

        assert result == "123"
        # Should have called send_message (sanitization worked)
        mock_bot.send_message.assert_called_once()
        call_kwargs = mock_bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == 12345
        assert "bold text" in call_kwargs["text"]  # core content preserved after sanitization

    @pytest.mark.asyncio
    async def test_send_message_fallback_to_plain_text(self, response_channel, mock_bot):
        """Should fallback to plain text if markdown parsing fails."""
        # First call fails with parsing error
        mock_bot.send_message.side_effect = [
            BadRequest("can't parse entities: can't find end of bold entity at byte offset 1699"),
            MagicMock(spec=Message, message_id=456)  # Second call succeeds
        ]

        text = "This is **problematic** text"
        result = await response_channel.send_message(text)

        assert result == "456"
        # Should have been called twice (original + fallback)
        assert mock_bot.send_message.call_count == 2
        
        # Second call should have parse_mode=None (plain text)
        second_call_kwargs = mock_bot.send_message.call_args_list[1].kwargs
        assert second_call_kwargs["parse_mode"] is None

    @pytest.mark.asyncio
    async def test_send_message_raises_on_non_markdown_errors(self, response_channel, mock_bot):
        """Non-markdown errors should be raised."""
        mock_bot.send_message.side_effect = BadRequest("Network error")

        text = "Some text"
        with pytest.raises(BadRequest, match="Network error"):
            await response_channel.send_message(text)


class TestUpdateMessageFallback:
    """Test update_message with markdown fallback."""

    @pytest.mark.asyncio
    async def test_update_message_success(self, response_channel, mock_bot):
        """Valid markdown update should succeed."""
        mock_bot.edit_message_text.return_value = None

        text = "Updated **bold** text"
        await response_channel.update_message("123", text)

        mock_bot.edit_message_text.assert_called_once()
        call_kwargs = mock_bot.edit_message_text.call_args.kwargs
        assert call_kwargs["parse_mode"] == "MarkdownV2"

    @pytest.mark.asyncio
    async def test_update_message_fallback_to_plain_text(self, response_channel, mock_bot):
        """Should fallback to plain text if markdown parsing fails."""
        # First call fails, second succeeds
        mock_bot.edit_message_text.side_effect = [
            BadRequest("can't parse entities"),
            None  # Success
        ]

        text = "Updated **problematic** text"
        await response_channel.update_message("123", text)

        # Should have been called twice
        assert mock_bot.edit_message_text.call_count == 2
        
        # Second call should have parse_mode=None
        second_call_kwargs = mock_bot.edit_message_text.call_args_list[1].kwargs
        assert second_call_kwargs["parse_mode"] is None

    @pytest.mark.asyncio
    async def test_update_message_sends_new_if_edit_fails(self, response_channel, mock_bot):
        """Should send new message if edit fails completely."""
        # Both edit attempts fail (markdown + plain text)
        mock_bot.edit_message_text.side_effect = [
            BadRequest("can't parse entities"),
            BadRequest("Message is too old")
        ]
        
        # But send_message should succeed
        mock_message = MagicMock(spec=Message, message_id=456)
        mock_bot.send_message.return_value = mock_message

        text = "Updated text"
        await response_channel.update_message("123", text)

        # Should have tried to edit twice, then sent new message
        assert mock_bot.edit_message_text.call_count == 2
        mock_bot.send_message.assert_called_once()
        send_kwargs = mock_bot.send_message.call_args.kwargs
        assert send_kwargs["chat_id"] == 12345
        assert "Updated text" in send_kwargs["text"]


class TestTruncationScenario:
    """Test the specific scenario that caused production error."""

    @pytest.mark.asyncio
    async def test_truncation_at_bold_tag(self, response_channel, mock_bot):
        """Simulate truncation breaking bold tag at byte offset 1699."""
        mock_message = MagicMock(spec=Message, message_id=789)
        mock_bot.send_message.return_value = mock_message

        # Create text that will be truncated in middle of bold tag
        # 2867 chars (70% of 4096) with bold at the end
        long_text = "A" * 2850 + "**bold text here**"
        
        result = await response_channel.send_message(long_text)

        assert result == "789"
        # Should succeed (either sanitized or fell back to plain text)
        assert mock_bot.send_message.call_count >= 1

    @pytest.mark.asyncio
    async def test_multiple_bold_tags_with_truncation(self, response_channel, mock_bot):
        """Multiple bold tags should be handled correctly even with truncation."""
        mock_message = MagicMock(spec=Message, message_id=999)
        mock_bot.send_message.return_value = mock_message

        # Text with multiple bold sections
        text = "**Section 1** normal text **Section 2** more text **Section 3"
        
        result = await response_channel.send_message(text)

        assert result == "999"
        # Should succeed (sanitization removes unpaired tag)
        mock_bot.send_message.assert_called()


class TestEdgeCases:
    """Test edge cases and corner scenarios."""

    @pytest.mark.asyncio
    async def test_empty_text(self, response_channel, mock_bot):
        """Empty text should be handled gracefully."""
        mock_message = MagicMock(spec=Message, message_id=111)
        mock_bot.send_message.return_value = mock_message

        result = await response_channel.send_message("")

        assert result == "111"

    @pytest.mark.asyncio
    async def test_text_with_only_asterisks(self, response_channel, mock_bot):
        """Text with only formatting characters should work."""
        mock_message = MagicMock(spec=Message, message_id=222)
        mock_bot.send_message.return_value = mock_message

        text = "******"
        result = await response_channel.send_message(text)

        assert result == "222"

    def test_validate_with_emoji_and_special_chars(self, response_channel):
        """Emoji and special characters should not break validation."""
        text = "🔥 *bold* text with 💡 emoji and _italic_ 🎉"
        assert response_channel._validate_markdown_pairs(text) is True
