"""The production graph's two chat handlers are built from one `ChatServices`: every shared dependency is the same
object on the Slack and the Telegram `ConversationHandler`, and the per-platform inputs differ as declared (Slack alone
has the channel binding and the history source; each has its own platform's RichContentService, the one the
notification service delivers through)."""
import pytest

from src.composition.chat_services import ChatServices
from tests.support.prod_graph import ProdGraph, prod_graph  # noqa: F401  (the fixture)

pytestmark = pytest.mark.slow

# ChatServices field -> the ConversationHandler attribute that holds it (the overflow callback is checked apart)
SHARED = (
    "coordinator", "agent_factory", "consolidation_queue", "global_config", "audio_service", "_notification_service",
    "_localization", "_file_conversion_service", "_fallback_service", "_short_link_service", "_skill_service",
    "_memory_facts", "_fact_answers", "_drive_notes", "_long_turns", "_doc_delivery_service",
    "_geo_marks", "_device_sessions",
)


async def test_both_handlers_share_every_bundle_dependency(prod_graph: ProdGraph):
    app = prod_graph.app
    slack = app.slack_adapter.conversation_handler
    telegram = app.telegram_adapter.conversation_handler

    assert len(SHARED) + 1 == len(ChatServices.__dataclass_fields__)
    for attr in SHARED:
        assert getattr(slack, attr) is getattr(telegram, attr), attr
    assert slack._overflow_callback == telegram._overflow_callback
    assert slack._notification_service is app.notification_service
    assert slack.agent_factory is app.agent_factory and slack.coordinator is app.coordinator


async def test_the_platform_inputs_differ_as_declared(prod_graph: ProdGraph):
    app = prod_graph.app
    slack = app.slack_adapter.conversation_handler
    telegram = app.telegram_adapter.conversation_handler
    notifier = app.notification_service

    assert slack._channel_binding is not None and slack._channel_history is not None
    assert telegram._channel_binding is None and telegram._channel_history is None
    assert slack._rich_content_service is notifier._platform_rich_content["slack"]
    assert telegram._rich_content_service is notifier._platform_rich_content["telegram"]
    assert slack._rich_content_service is not telegram._rich_content_service
    assert notifier._platform_media["slack"] is slack._rich_content_service._media_port
    assert notifier._platform_media["telegram"] is telegram._rich_content_service._media_port
