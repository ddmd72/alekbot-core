"""Both adapter factories hand `doc_delivery_service` to the ConversationHandler they build (P9 T3, D1).

Without it a sync `document` rich-content item is dropped with a warning on that platform. The built handler is the real
`ConversationHandler`; only its neighbours (SDK clients, adapters, the task queue) are replaced. The sync delivery itself
is proved in `tests/unit/handlers/test_conversation_handler_delivery.py::TestDeliverItem::test_document_success_sends_link`.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.composition.slack_adapter_factory import SlackAdapterFactory
from src.composition.telegram_adapter_factory import TelegramAdapterFactory
from src.handlers.conversation_handler import ConversationHandler

_SLACK = "src.composition.slack_adapter_factory."
_TG = "src.composition.telegram_adapter_factory."


def _slack_handler(**extra) -> ConversationHandler:
    env_config = MagicMock()
    env_config.is_development = True
    config = {"SLACK_BOT_TOKEN": "x", "GOOGLE_CLOUD_PROJECT": "p", "CLOUD_RUN_SERVICE_URL": "http://localhost:8080"}
    with patch(_SLACK + "SlackChannelHistorySource"), patch(_SLACK + "RichContentService"), \
            patch(_SLACK + "GcpTaskQueue"), patch(_SLACK + "HTTPModeAdapter") as adapter:
        SlackAdapterFactory.create_adapter(
            rich_content_service=MagicMock(),
            app=MagicMock(), config=config, env_config=env_config, coordinator=AsyncMock(),
            agent_factory=AsyncMock(), iam_service=AsyncMock(), file_service=AsyncMock(),
            session_store=MagicMock(), db_client=MagicMock(), **extra)
    return adapter.call_args.kwargs["conversation_handler"]


def _telegram_handler(**extra) -> ConversationHandler:
    with patch(_TG + "TelegramWebhookAdapter") as adapter:
        TelegramAdapterFactory.create_adapter(
            rich_content_service=MagicMock(),
            token="123:abc", webhook_secret="secret", dedup_store=MagicMock(), session_store=MagicMock(),
            coordinator=AsyncMock(), agent_factory=AsyncMock(), iam_service=AsyncMock(),
            file_service=AsyncMock(), **extra)
    return adapter.call_args.kwargs["conversation_handler"]


@pytest.mark.parametrize("build", [_slack_handler, _telegram_handler])
def test_factory_forwards_doc_delivery_service(build):
    service = MagicMock(name="doc_delivery_service")
    handler = build(doc_delivery_service=service)
    assert isinstance(handler, ConversationHandler)
    assert handler._doc_delivery_service is service


@pytest.mark.parametrize("build", [_slack_handler, _telegram_handler])
def test_factory_defaults_doc_delivery_service_to_none(build):
    assert build()._doc_delivery_service is None
