"""
Unit tests for TelegramResponseChannel.

Tests ResponseChannel protocol implementation for Telegram.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.adapters.telegram.response_channel import TelegramResponseChannel
from src.domain.ui_messages import StatusType
from src.domain.messaging import RichContent


class TestTelegramResponseChannel:
    """Unit tests for TelegramResponseChannel."""

    @pytest.fixture
    def mock_bot(self):
        """Create mock Telegram Bot."""
        bot = AsyncMock()
        return bot

    @pytest.fixture
    def response_channel(self, mock_bot):
        """Create TelegramResponseChannel with mock bot."""
        return TelegramResponseChannel(bot=mock_bot, chat_id=123456789)

    @pytest.mark.asyncio
    async def test_properties(self, response_channel):
        """Test max_message_length and supports_message_editing properties."""
        assert response_channel.max_message_length == 4096
        assert response_channel.supports_message_editing is True

    @pytest.mark.asyncio
    async def test_send_message_basic(self, response_channel, mock_bot):
        """Test basic message sending."""
        # Setup mock
        mock_message = MagicMock()
        mock_message.message_id = 42
        mock_bot.send_message.return_value = mock_message

        # Send message
        result = await response_channel.send_message("Test message")

        # Verify
        assert result == "42"
        mock_bot.send_message.assert_called_once()
        call_args = mock_bot.send_message.call_args
        assert call_args.kwargs['chat_id'] == 123456789
        assert call_args.kwargs['parse_mode'] == "MarkdownV2"
        assert call_args.kwargs['message_thread_id'] is None

    @pytest.mark.asyncio
    async def test_send_message_with_thread(self, response_channel, mock_bot):
        """Test message sending with thread_id."""
        mock_message = MagicMock()
        mock_message.message_id = 43
        mock_bot.send_message.return_value = mock_message

        result = await response_channel.send_message("Test", thread_id="999")

        assert result == "43"
        call_args = mock_bot.send_message.call_args
        assert call_args.kwargs['message_thread_id'] == 999

    @pytest.mark.asyncio
    async def test_send_message_truncation(self, response_channel, mock_bot):
        """Test message truncation at 4096 chars."""
        mock_message = MagicMock()
        mock_message.message_id = 44
        mock_bot.send_message.return_value = mock_message

        # Create message > 4096 chars
        long_message = "A" * 5000

        await response_channel.send_message(long_message)

        # Verify truncation happened
        call_args = mock_bot.send_message.call_args
        sent_text = call_args.kwargs['text']
        assert len(sent_text) <= 4096
        assert "занадто довга відповідь" in sent_text

    @pytest.mark.asyncio
    async def test_update_message(self, response_channel, mock_bot):
        """Test message editing."""
        await response_channel.update_message("100", "Updated text")

        mock_bot.edit_message_text.assert_called_once()
        call_args = mock_bot.edit_message_text.call_args
        assert call_args.kwargs['chat_id'] == 123456789
        assert call_args.kwargs['message_id'] == 100
        assert call_args.kwargs['parse_mode'] == "MarkdownV2"

    @pytest.mark.asyncio
    async def test_update_message_48h_fallback(self, response_channel, mock_bot):
        """Test fallback when message can't be edited (48h limit)."""
        # Mock edit failure
        mock_bot.edit_message_text.side_effect = Exception("Message can't be edited")
        
        # Mock successful send
        mock_message = MagicMock()
        mock_message.message_id = 200
        mock_bot.send_message.return_value = mock_message

        # Should fallback to send_message
        await response_channel.update_message("100", "New text")

        # Verify fallback happened
        mock_bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_markdown_formatting_bold(self, response_channel):
        """Test MarkdownV2 formatting for bold text."""
        text = "This is **bold** text"
        formatted = response_channel._format_for_platform(text)

        # ** should be converted to * (Telegram MarkdownV2)
        assert "This is *bold* text" in formatted
        # Note: "This is *bold* text" has no special MarkdownV2 chars, so no escaping needed

    @pytest.mark.asyncio
    async def test_markdown_formatting_italic(self, response_channel):
        """Test MarkdownV2 formatting for italic text."""
        text = "This is __italic__ text"
        formatted = response_channel._format_for_platform(text)

        # __ should be converted to _ (Telegram MarkdownV2)
        assert "This is _italic_ text" in formatted

    @pytest.mark.asyncio
    async def test_markdown_escaping_special_chars(self, response_channel):
        """Test escaping of special characters."""
        text = "Test with special chars: . ! - +"
        formatted = response_channel._format_for_platform(text)

        # Special chars should be escaped
        assert "\\." in formatted
        assert "\\!" in formatted
        assert "\\-" in formatted
        assert "\\+" in formatted

    @pytest.mark.asyncio
    async def test_send_chunked_message_single_chunk(self, response_channel, mock_bot):
        """Test send_chunked_message with text that fits in one chunk."""
        short_text = "Short message"

        await response_channel.send_chunked_message(
            text=short_text,
            message_id="100",
            thread_id=None
        )

        # Should just update message (no additional sends)
        mock_bot.edit_message_text.assert_called_once()
        mock_bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_chunked_message_multiple_chunks(self, response_channel, mock_bot):
        """Test send_chunked_message with text requiring multiple chunks."""
        # Create text larger than TELEGRAM_CHUNK_SIZE (4000)
        long_text = "Paragraph.\n\n" * 500  # ~5500 chars

        mock_message = MagicMock()
        mock_message.message_id = 101
        mock_bot.send_message.return_value = mock_message

        await response_channel.send_chunked_message(
            text=long_text,
            message_id="100",
            thread_id=None
        )

        # Should update first message to "готова"
        mock_bot.edit_message_text.assert_called_once()
        edit_call = mock_bot.edit_message_text.call_args
        assert "Відповідь готова" in edit_call.kwargs['text']

        # Should send remaining chunks
        assert mock_bot.send_message.call_count >= 1

    @pytest.mark.asyncio
    async def test_send_rich_content_fallback(self, response_channel, mock_bot):
        """Test rich content uses fallback text for MVP."""
        mock_message = MagicMock()
        mock_message.message_id = 102
        mock_bot.send_message.return_value = mock_message

        rich_content = RichContent(
            content_type="table",
            data={"rows": []},
            fallback_text="Fallback table text"
        )

        await response_channel.send_rich_content(rich_content)

        # Should send fallback text
        call_args = mock_bot.send_message.call_args
        assert "Fallback table text" in call_args.kwargs['text']

    @pytest.mark.asyncio
    async def test_send_status(self, response_channel, mock_bot):
        """Test sending status message."""
        mock_message = MagicMock()
        mock_message.message_id = 103
        mock_bot.send_message.return_value = mock_message

        message_id = await response_channel.send_status(StatusType.THINKING)

        assert message_id == "103"
        mock_bot.send_message.assert_called_once()
        call_args = mock_bot.send_message.call_args
        text = call_args.kwargs['text']
        assert "⏳" in text  # Hourglass emoji
        assert text.endswith("\\.")  # Ends with escaped dot

    @pytest.mark.asyncio
    async def test_send_status_with_phrase(self, response_channel, mock_bot):
        """Test sending status and returning phrase."""
        mock_message = MagicMock()
        mock_message.message_id = 104
        mock_bot.send_message.return_value = mock_message

        message_id, phrase = await response_channel.send_status_with_phrase(
            StatusType.SEARCHING_MEMORY
        )

        assert message_id == "104"
        assert isinstance(phrase, str)
        assert len(phrase) > 0  # Non-empty phrase

    @pytest.mark.asyncio
    async def test_get_status_phrase(self, response_channel):
        """Test getting status phrase without sending."""
        phrase = await response_channel.get_status_phrase(StatusType.THINKING)

        assert isinstance(phrase, str)
        assert len(phrase) > 0
        # Should NOT call bot.send_message
        response_channel.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_status(self, response_channel, mock_bot):
        """Test updating status message."""
        await response_channel.update_status("105", StatusType.PROCESSING_FILE)

        mock_bot.edit_message_text.assert_called_once()
        call_args = mock_bot.edit_message_text.call_args
        assert call_args.kwargs['message_id'] == 105
        text = call_args.kwargs['text']
        assert "⏳" in text

    @pytest.mark.asyncio
    async def test_update_status_with_dots(self, response_channel, mock_bot):
        """Test status animation with dots."""
        await response_channel.update_status_with_dots(
            message_id="106",
            status_type=StatusType.THINKING,
            dots_count=3
        )

        call_args = mock_bot.edit_message_text.call_args
        text = call_args.kwargs['text']
        # Should have at least dots_count escaped dots (status phrase may contain additional dots)
        assert text.count('\\.') >= 3

    @pytest.mark.asyncio
    async def test_update_status_with_phrase_and_dots(self, response_channel, mock_bot):
        """Test status animation with fixed phrase."""
        await response_channel.update_status_with_phrase_and_dots(
            message_id="107",
            phrase="Думаю",
            dots_count=2
        )

        call_args = mock_bot.edit_message_text.call_args
        text = call_args.kwargs['text']
        assert "⏳ Думаю" in text
        assert text.count('\\.') == 2

    @pytest.mark.asyncio
    async def test_send_entertainment_message(self, response_channel, mock_bot):
        """Test entertainment message with emoji."""
        mock_message = MagicMock()
        mock_message.message_id = 108
        mock_bot.send_message.return_value = mock_message

        await response_channel.send_entertainment_message("Fun fact!")

        call_args = mock_bot.send_message.call_args
        text = call_args.kwargs['text']
        assert "💡" in text
        assert "Fun fact" in text  # '!' is escaped to '\!' in MarkdownV2

    @pytest.mark.asyncio
    async def test_get_entertainment_intro(self, response_channel):
        """Test getting entertainment intro phrase."""
        intro = await response_channel.get_entertainment_intro()

        assert isinstance(intro, str)
        assert len(intro) > 0

    @pytest.mark.asyncio
    @patch('aiohttp.ClientSession')
    async def test_download_file_success(self, mock_session_class, response_channel):
        """Test successful file download."""
        # Mock aiohttp response as async context manager
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.content.read = AsyncMock(side_effect=[b"chunk1", b"chunk2", b""])
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        # session.get() must be a sync call returning async context manager (not a coroutine)
        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session_class.return_value = mock_session

        url = "https://api.telegram.org/file/bot123/photo.jpg"
        result = await response_channel.download_file(url, "image/jpeg")

        # Should return temp file path
        assert result is not None
        assert isinstance(result, str)
        assert "photo.jpg" in result

    @pytest.mark.asyncio
    @patch('aiohttp.ClientSession')
    async def test_download_file_404(self, mock_session_class, response_channel):
        """Test file download with 404 error."""
        # Mock 404 response
        mock_response = AsyncMock()
        mock_response.status = 404
        
        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session.get.return_value.__aenter__.return_value = mock_response
        mock_session_class.return_value = mock_session

        url = "https://api.telegram.org/file/bot123/missing.jpg"
        result = await response_channel.download_file(url, "image/jpeg")

        # Should return None on failure
        assert result is None

    @pytest.mark.asyncio
    @patch('aiohttp.ClientSession')
    async def test_download_file_exception(self, mock_session_class, response_channel):
        """Test file download with network exception."""
        # Mock exception
        mock_session_class.side_effect = Exception("Network error")

        url = "https://api.telegram.org/file/bot123/photo.jpg"
        result = await response_channel.download_file(url, "image/jpeg")

        # Should return None and not crash
        assert result is None
