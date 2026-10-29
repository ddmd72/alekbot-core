"""The RichContentService registered on the notification service is the one the chat handler uses, per platform."""
import pytest

from tests.support import prod_env

pytestmark = pytest.mark.slow


def test_notification_and_chat_handler_share_one_rich_content_service_per_platform(monkeypatch):
    services = prod_env.install_offline_services(monkeypatch)
    from src.composition.application_graph import build_application_graph
    from src.config.settings import load_settings
    graph = build_application_graph(load_settings(), services.db)

    registered = graph.notification_service._platform_rich_content
    assert set(registered) == {"slack", "telegram"}
    for platform, adapter in (("slack", graph.slack_adapter), ("telegram", graph.telegram_adapter)):
        handler = getattr(adapter, "conversation_handler", None) or getattr(adapter, "_conversation_handler")
        assert handler._rich_content_service is registered[platform]
