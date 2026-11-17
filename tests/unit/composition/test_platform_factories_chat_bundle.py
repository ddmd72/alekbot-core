"""Both platform factories build their ConversationHandler from the one `ChatServices` through
`build_conversation_handler`; only the SDK-facing neighbours (the platform adapter, the task queue, Slack's history
source) are replaced. Slack's handler gets the channel binding and the history source, Telegram's has neither (P9-D9;
the Bot API has no history), and each platform adapter gets the bundle's audio service and localization."""
import dataclasses
from src.composition.deploy_topology import build_deploy_topology
from unittest.mock import MagicMock, patch

from src.composition.chat_services import ChatServices
from src.composition.slack_adapter_factory import SlackAdapterFactory
from src.composition.telegram_adapter_factory import TelegramAdapterFactory

_SLACK = "src.composition.slack_adapter_factory."
_TG = "src.composition.telegram_adapter_factory."


def _chat() -> ChatServices:
    return ChatServices(**{f.name: MagicMock(name=f.name) for f in dataclasses.fields(ChatServices)})


def test_slack_builds_its_handler_from_the_bundle_with_its_binding_and_history():
    chat, rich, binding = _chat(), MagicMock(name="rich"), MagicMock(name="binding")
    config = {"SLACK_BOT_TOKEN": "x", "GOOGLE_CLOUD_PROJECT": "p", "CLOUD_RUN_SERVICE_URL": "http://localhost:8080"}
    with patch(_SLACK + "SlackChannelHistorySource") as history, patch(_SLACK + "GcpTaskQueue"), \
            patch(_SLACK + "HTTPModeAdapter") as adapter:
        SlackAdapterFactory.create_adapter(
            app=MagicMock(), config=config, deploy_topology=build_deploy_topology(config, MagicMock(is_development=True)), chat=chat,
            iam_service=MagicMock(), session_store=MagicMock(), db_client=MagicMock(),
            channel_binding_service=binding, rich_content_service=rich)

    kwargs = adapter.call_args.kwargs
    handler = kwargs["conversation_handler"]
    assert handler.coordinator is chat.coordinator and handler._skill_service is chat.skill_service
    assert handler._rich_content_service is rich
    assert handler._channel_binding is binding
    assert handler._channel_history is history.return_value
    assert kwargs["audio_service"] is chat.audio_service and kwargs["localization"] is chat.localization


def test_telegram_builds_its_handler_from_the_bundle_without_binding_or_history():
    chat, rich = _chat(), MagicMock(name="rich")
    with patch(_TG + "TelegramWebhookAdapter") as adapter:
        TelegramAdapterFactory.create_adapter(
            token="123:abc", webhook_secret="secret", dedup_store=MagicMock(), session_store=MagicMock(), chat=chat,
            iam_service=MagicMock(), rich_content_service=rich)

    kwargs = adapter.call_args.kwargs
    handler = kwargs["conversation_handler"]
    assert handler.agent_factory is chat.agent_factory and handler._doc_delivery_service is chat.doc_delivery_service
    assert handler._rich_content_service is rich
    assert handler._channel_binding is None and handler._channel_history is None
    assert kwargs["audio_service"] is chat.audio_service and kwargs["localization"] is chat.localization
