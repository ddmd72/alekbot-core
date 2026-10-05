"""TelegramAdapterFactory wiring — skill_service passthrough.

No test harness existed for this factory before Task 8 (Agent Skills delivery B,
composition wiring). ConversationHandler and TelegramWebhookAdapter are patched so
the test exercises only the factory's own dependency wiring, not the collaborators.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.composition.telegram_adapter_factory import TelegramAdapterFactory


@pytest.mark.requirement("REQ-CORE-09")
def test_factory_forwards_skill_service_to_conversation_handler():
    """TelegramAdapterFactory must thread its skill_service kwarg into
    ConversationHandler unchanged — composition wiring for Agent Skills delivery
    (Task 8), mirroring SlackAdapterFactory's wiring."""
    sentinel_skill_service = MagicMock(name="skill_service")

    with patch("src.composition.telegram_adapter_factory.Bot"), \
         patch("src.composition.telegram_adapter_factory.TelegramMediaAdapter"), \
         patch("src.composition.telegram_adapter_factory.TelegramWebhookAdapter"), \
         patch("src.composition.telegram_adapter_factory.ConversationHandler") as mock_handler:
        TelegramAdapterFactory.create_adapter(
            token="123:abc",
            webhook_secret="secret",
            dedup_store=MagicMock(),
            session_store=MagicMock(),
            coordinator=AsyncMock(),
            agent_factory=AsyncMock(),
            iam_service=AsyncMock(),
            file_service=AsyncMock(),
            skill_service=sentinel_skill_service,
        )
        assert mock_handler.call_args.kwargs["skill_service"] is sentinel_skill_service


@pytest.mark.requirement("REQ-CORE-09")
def test_factory_defaults_skill_service_to_none():
    """Omitting skill_service must not break construction — default is None,
    matching ConversationHandler's own default."""
    with patch("src.composition.telegram_adapter_factory.Bot"), \
         patch("src.composition.telegram_adapter_factory.TelegramMediaAdapter"), \
         patch("src.composition.telegram_adapter_factory.TelegramWebhookAdapter"), \
         patch("src.composition.telegram_adapter_factory.ConversationHandler") as mock_handler:
        TelegramAdapterFactory.create_adapter(
            token="123:abc",
            webhook_secret="secret",
            dedup_store=MagicMock(),
            session_store=MagicMock(),
            coordinator=AsyncMock(),
            agent_factory=AsyncMock(),
            iam_service=AsyncMock(),
            file_service=AsyncMock(),
        )
        assert mock_handler.call_args.kwargs["skill_service"] is None
