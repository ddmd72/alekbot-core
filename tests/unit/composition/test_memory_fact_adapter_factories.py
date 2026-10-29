"""Both adapter factories hand memory_fact_service to the ConversationHandler they build
(MEMORY_FACT_CONFIRMATION_RFC, wiring). Without it `$memory save` / `$memory cancel` are dead on that platform."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.composition.slack_adapter_factory import SlackAdapterFactory
from src.composition.telegram_adapter_factory import TelegramAdapterFactory

_SLACK = "src.composition.slack_adapter_factory."
_TG = "src.composition.telegram_adapter_factory."


def _slack(**extra):
    env_config = MagicMock()
    env_config.is_development = True
    config = {"SLACK_BOT_TOKEN": "x", "GOOGLE_CLOUD_PROJECT": "p", "CLOUD_RUN_SERVICE_URL": "http://localhost:8080"}
    with patch(_SLACK + "SlackChannelHistorySource"), patch(_SLACK + "RichContentService"), \
            patch(_SLACK + "GcpTaskQueue"), \
            patch(_SLACK + "HTTPModeAdapter"), patch(_SLACK + "ConversationHandler") as handler:
        SlackAdapterFactory.create_adapter(
            rich_content_service=MagicMock(),
            app=MagicMock(), config=config, env_config=env_config, coordinator=AsyncMock(),
            agent_factory=AsyncMock(), iam_service=AsyncMock(), file_service=AsyncMock(),
            session_store=MagicMock(), db_client=MagicMock(), **extra)
    return handler.call_args.kwargs


def _telegram(**extra):
    with patch(_TG + "TelegramWebhookAdapter"), \
            patch(_TG + "ConversationHandler") as handler:
        TelegramAdapterFactory.create_adapter(
            rich_content_service=MagicMock(),
            token="123:abc", webhook_secret="secret", dedup_store=MagicMock(), session_store=MagicMock(),
            coordinator=AsyncMock(), agent_factory=AsyncMock(), iam_service=AsyncMock(),
            file_service=AsyncMock(), **extra)
    return handler.call_args.kwargs


@pytest.mark.requirement("REQ-CORE-09")
@pytest.mark.parametrize("build", [_slack, _telegram])
def test_factory_forwards_memory_fact_service(build):
    sentinel = MagicMock(name="memory_fact_service")
    assert build(memory_fact_service=sentinel)["memory_fact_service"] is sentinel


@pytest.mark.requirement("REQ-CORE-09")
@pytest.mark.parametrize("build", [_slack, _telegram])
def test_factory_defaults_memory_fact_service_to_none(build):
    assert build()["memory_fact_service"] is None
