"""
Unit tests for TelegramWebhookAdapter.

Tests webhook handling, HMAC verification, IAM integration, and file translation.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.adapters.telegram.webhook_adapter import TelegramWebhookAdapter
from src.domain.user import UserProfile
from src.services.iam_service import IAMDecision


class TestTelegramWebhookAdapter:
    """Unit tests for TelegramWebhookAdapter."""

    @pytest.fixture
    def mock_dependencies(self):
        """Create mock dependencies for adapter."""
        return {
            'coordinator': MagicMock(),
            'agent_factory': AsyncMock(),
            'iam_service': AsyncMock(),
            'file_service': MagicMock(),
            'consolidation_queue': MagicMock(),
            'consolidation_config': MagicMock(),
            'dedup_store': AsyncMock(),
            'session_store': AsyncMock()
        }

    @pytest.fixture
    def adapter(self, mock_dependencies):
        """Create TelegramWebhookAdapter with mocked Bot to avoid real API calls."""
        with patch("src.adapters.telegram.webhook_adapter.Bot") as MockBot:
            mock_bot = AsyncMock()
            MockBot.return_value = mock_bot
            instance = TelegramWebhookAdapter(
                token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
                webhook_secret="test_webhook_secret_32chars_min",
                **mock_dependencies
            )
        # adapter.bot is now the AsyncMock (reference persists after patch exits)
        return instance

    @pytest.mark.asyncio
    async def test_get_platform_name(self, adapter):
        """Test get_platform_name returns 'telegram'."""
        assert adapter.get_platform_name() == "telegram"

    @pytest.mark.asyncio
    async def test_get_blueprint(self, adapter):
        """Test get_blueprint returns Quart Blueprint."""
        blueprint = adapter.get_blueprint()
        
        assert blueprint is not None
        assert blueprint.name == 'telegram'

    @pytest.mark.asyncio
    async def test_hmac_verification_valid(self, adapter):
        """Test HMAC verification with valid token."""
        mock_request = MagicMock()
        mock_request.headers.get.return_value = "test_webhook_secret_32chars_min"

        is_valid = await adapter._verify_webhook_signature(mock_request)

        assert is_valid is True

    @pytest.mark.asyncio
    async def test_hmac_verification_invalid(self, adapter):
        """Test HMAC verification with invalid token."""
        mock_request = MagicMock()
        mock_request.headers.get.return_value = "wrong_secret"

        is_valid = await adapter._verify_webhook_signature(mock_request)

        assert is_valid is False

    @pytest.mark.asyncio
    async def test_hmac_verification_missing(self, adapter):
        """Test HMAC verification with missing header."""
        mock_request = MagicMock()
        mock_request.headers.get.return_value = None

        is_valid = await adapter._verify_webhook_signature(mock_request)

        assert is_valid is False

    @pytest.mark.asyncio
    async def test_dedup_with_namespace(self, adapter):
        """Test deduplication uses telegram:: namespace."""
        adapter.dedup_store.try_mark_processed = AsyncMock(return_value=True)
        
        mock_user = MagicMock()
        mock_user.id = 670659908
        mock_user.is_bot = False
        mock_user.first_name = "Test"
        
        mock_chat = MagicMock()
        mock_chat.id = 670659908
        mock_chat.type = "private"
        
        mock_message = MagicMock()
        mock_message.message_id = 1
        mock_message.chat = mock_chat
        mock_message.from_user = mock_user
        mock_message.text = "Hello"
        mock_message.photo = None
        mock_message.document = None
        mock_message.is_topic_message = False
        
        mock_update = MagicMock()
        mock_update.update_id = 12345
        mock_update.message = mock_message
        mock_update.to_dict = MagicMock(return_value={"update_id": 12345})
        
        # IAM rejection so we don't need ConversationHandler
        adapter.iam_service.authorize.return_value = IAMDecision(
            action="reject",
            message="Not authorized"
        )

        mock_request_obj = MagicMock()
        mock_request_obj.headers.get.return_value = adapter.webhook_secret
        mock_request_obj.get_json = AsyncMock(return_value={"update_id": 12345})
        
        # Patch jsonify to avoid Quart app context requirement
        with patch('src.adapters.telegram.webhook_adapter.jsonify', side_effect=lambda x: x):
            with patch('src.adapters.telegram.webhook_adapter.request', new=mock_request_obj):
                with patch('src.adapters.telegram.webhook_adapter.Update') as mock_update_class:
                    mock_update_class.de_json.return_value = mock_update
                    await adapter._handle_telegram_update()
        
        adapter.dedup_store.try_mark_processed.assert_called_once()
        call_args = adapter.dedup_store.try_mark_processed.call_args
        dedup_key = call_args[0][0]
        assert dedup_key == "telegram::12345"

    @pytest.mark.asyncio
    async def test_unauthorized_user_gets_iam_message(self, adapter):
        """Test IAM rejection sends centralized message."""
        adapter.dedup_store.try_mark_processed = AsyncMock(return_value=True)
        
        adapter.iam_service.authorize.return_value = IAMDecision(
            action="reject",
            message="👋 Привіт! Щоб використовувати бота...",
            metadata={"platform_user_id": "670659908"}
        )
        
        mock_user = MagicMock()
        mock_user.id = 670659908
        
        mock_chat = MagicMock()
        mock_chat.id = 670659908
        
        mock_message = MagicMock()
        mock_message.from_user = mock_user
        mock_message.chat = mock_chat
        mock_message.text = "Hello"
        
        mock_update = MagicMock()
        mock_update.update_id = 12346
        mock_update.message = mock_message
        
        mock_request_obj = MagicMock()
        mock_request_obj.headers.get.return_value = adapter.webhook_secret
        mock_request_obj.get_json = AsyncMock(return_value={"update_id": 12346})
        
        with patch('src.adapters.telegram.webhook_adapter.jsonify', side_effect=lambda x: x):
            with patch('src.adapters.telegram.webhook_adapter.request', new=mock_request_obj):
                with patch('src.adapters.telegram.webhook_adapter.Update') as mock_update_class:
                    mock_update_class.de_json.return_value = mock_update
                    await adapter._handle_telegram_update()
        
        adapter.bot.send_message.assert_called_once()
        call_args = adapter.bot.send_message.call_args
        sent_text = call_args.kwargs['text']
        assert "Привіт" in sent_text

    @pytest.mark.asyncio
    async def test_authorized_user_processed(self, adapter):
        """Test authorized user goes to ConversationHandler."""
        adapter.dedup_store.try_mark_processed = AsyncMock(return_value=True)
        
        mock_user_profile = UserProfile(
            user_id="user_123",
            email="test@example.com",
            account_id="account_456"
        )
        adapter.iam_service.authorize.return_value = IAMDecision(
            action="allow",
            user=mock_user_profile
        )
        
        mock_user = MagicMock()
        mock_user.id = 670659908
        
        mock_chat = MagicMock()
        mock_chat.id = 670659908
        
        mock_message = MagicMock()
        mock_message.from_user = mock_user
        mock_message.chat = mock_chat
        mock_message.text = "Hello bot"
        mock_message.photo = None
        mock_message.document = None
        mock_message.is_topic_message = False
        
        mock_update = MagicMock()
        mock_update.update_id = 12347
        mock_update.message = mock_message
        
        # Replace the shared conversation_handler instance with a mock
        mock_handler = AsyncMock()
        adapter.conversation_handler = mock_handler

        mock_request_obj = MagicMock()
        mock_request_obj.headers.get.return_value = adapter.webhook_secret
        mock_request_obj.get_json = AsyncMock(return_value={"update_id": 12347})

        with patch('src.adapters.telegram.webhook_adapter.jsonify', side_effect=lambda x: x):
            with patch('src.adapters.telegram.webhook_adapter.request', new=mock_request_obj):
                with patch('src.adapters.telegram.webhook_adapter.Update') as mock_update_class:
                    mock_update_class.de_json.return_value = mock_update
                    await adapter._handle_telegram_update()

        mock_handler.handle_message.assert_called_once()
        call_args = mock_handler.handle_message.call_args
        context = call_args[0][0]
        assert context.text == "Hello bot"
        assert context.user_id == "user_123"
        assert context.account_id == "account_456"

    @pytest.mark.asyncio
    async def test_file_translation_parallel(self, adapter):
        """Test async parallel file translation."""
        mock_file1 = MagicMock()
        mock_file1.file_id = "file_1"
        mock_file1.file_name = "photo1.jpg"
        mock_file1.mime_type = "image/jpeg"
        mock_file1.file_size = 1024
        
        mock_file2 = MagicMock()
        mock_file2.file_id = "file_2"
        mock_file2.file_name = "photo2.jpg"
        mock_file2.mime_type = "image/jpeg"
        mock_file2.file_size = 2048
        
        # file_path is already a full URL in the real Telegram API
        mock_file_info1 = MagicMock()
        mock_file_info1.file_path = "https://api.telegram.org/file/bot123/photos/file_1.jpg"
        
        mock_file_info2 = MagicMock()
        mock_file_info2.file_path = "https://api.telegram.org/file/bot123/photos/file_2.jpg"
        
        adapter.bot.get_file = AsyncMock(side_effect=[mock_file_info1, mock_file_info2])
        
        attachments = await adapter._translate_platform_files([mock_file1, mock_file2])
        
        assert len(attachments) == 2
        assert adapter.bot.get_file.call_count == 2
        
        assert attachments[0].filename == "photo1.jpg"
        assert attachments[0].mime_type == "image/jpeg"
        assert attachments[0].size_bytes == 1024
        assert "api.telegram.org/file/bot" in attachments[0].url

    @pytest.mark.asyncio
    async def test_file_translation_handles_failures(self, adapter):
        """Test file translation gracefully handles failures — missing file_id."""
        mock_file1 = MagicMock()
        mock_file1.file_id = "file_ok"
        mock_file1.file_name = "good.jpg"
        mock_file1.mime_type = "image/jpeg"
        
        mock_file2 = MagicMock()
        mock_file2.file_id = None  # Explicitly None → will be skipped by adapter
        mock_file2.file_name = "bad.jpg"
        
        mock_file_info = MagicMock()
        mock_file_info.file_path = "https://api.telegram.org/file/bot123/photos/good.jpg"
        adapter.bot.get_file = AsyncMock(return_value=mock_file_info)
        
        attachments = await adapter._translate_platform_files([mock_file1, mock_file2])
        
        assert len(attachments) == 1
        assert attachments[0].filename == "good.jpg"

    @pytest.mark.asyncio
    async def test_file_translation_with_api_error(self, adapter):
        """Test file translation handles API errors gracefully."""
        mock_file = MagicMock()
        mock_file.file_id = "file_error"
        mock_file.file_name = "test.jpg"
        
        adapter.bot.get_file = AsyncMock(side_effect=Exception("API error"))
        
        attachments = await adapter._translate_platform_files([mock_file])
        
        assert len(attachments) == 0

    @pytest.mark.asyncio
    async def test_duplicate_update_skipped(self, adapter):
        """Test duplicate updates are skipped."""
        adapter.dedup_store.try_mark_processed = AsyncMock(return_value=False)
        
        mock_message = MagicMock()
        mock_message.text = "Duplicate"
        
        mock_update = MagicMock()
        mock_update.update_id = 99999
        mock_update.message = mock_message
        
        mock_request_obj = MagicMock()
        mock_request_obj.headers.get.return_value = adapter.webhook_secret
        mock_request_obj.get_json = AsyncMock(return_value={"update_id": 99999})
        
        with patch('src.adapters.telegram.webhook_adapter.jsonify', side_effect=lambda x: x):
            with patch('src.adapters.telegram.webhook_adapter.request', new=mock_request_obj):
                with patch('src.adapters.telegram.webhook_adapter.Update') as mock_update_class:
                    mock_update_class.de_json.return_value = mock_update
                    response = await adapter._handle_telegram_update()
        
        assert response[1] == 200
        adapter.iam_service.authorize.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_and_stop(self, adapter):
        """Test adapter lifecycle methods."""
        await adapter.start()
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_invalid_signature_rejected(self, adapter):
        """Test webhook with invalid signature is rejected."""
        mock_request_obj = MagicMock()
        mock_request_obj.headers.get.return_value = "wrong_secret"
        
        with patch('src.adapters.telegram.webhook_adapter.jsonify', side_effect=lambda x: x):
            with patch('src.adapters.telegram.webhook_adapter.request', new=mock_request_obj):
                response = await adapter._handle_telegram_update()
        
        assert response[1] == 403
        assert "Invalid signature" in str(response[0])
