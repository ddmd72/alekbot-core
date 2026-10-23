"""Telegram in the application graph is all or nothing (P9 design §7 delta f).

The graph is built with the deployed environment (`tests/support/prod_env.py`) over fakes at the SDK boundaries. The
adapter and its media adapter are built first; the worker, channel and media registrations and the platform factory
entry follow only when both exist. A malformed secret still raises out of the build, so the deploy fails.
"""
import pytest

from src.adapters.platform.factory import PlatformAdapterFactory
from src.composition.application_graph import build_application_graph
from src.composition.telegram_adapter_factory import TelegramAdapterFactory
from tests.support import prod_env

pytestmark = pytest.mark.slow


def _build(monkeypatch):
    services = prod_env.install_offline_services(monkeypatch)
    from src.config.settings import load_settings
    return build_application_graph(load_settings(), services.db)


def test_a_configured_telegram_is_built_attached_and_registered(monkeypatch):
    graph = _build(monkeypatch)

    assert graph.telegram_adapter is not None
    assert graph.worker_handler._telegram_worker == graph.telegram_adapter.handle_queued_update
    assert PlatformAdapterFactory._adapters.get("telegram") is graph.telegram_adapter
    assert "telegram" in graph.notification_service._platform_media
    assert graph.notification_service._channel_factory.create("telegram", "1") is not None


def test_a_failure_while_building_registers_nothing(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("telegram sdk down")

    monkeypatch.setattr(TelegramAdapterFactory, "create_adapter", staticmethod(broken))
    graph = _build(monkeypatch)

    assert graph.telegram_adapter is None
    assert graph.worker_handler._telegram_worker is None
    assert "telegram" not in PlatformAdapterFactory._adapters
    assert "telegram" not in graph.notification_service._platform_media
    assert graph.notification_service._channel_factory.create("telegram", "1") is None
    assert graph.slack_adapter is not None


def test_a_failure_in_the_media_adapter_registers_nothing(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("media adapter down")

    monkeypatch.setattr("src.composition.application_graph.TelegramMediaAdapter", broken)
    graph = _build(monkeypatch)

    assert graph.telegram_adapter is None
    assert graph.worker_handler._telegram_worker is None
    assert "telegram" not in PlatformAdapterFactory._adapters
    assert "telegram" not in graph.notification_service._platform_media


def test_an_unset_token_builds_no_telegram(monkeypatch):
    services = prod_env.install_offline_services(monkeypatch)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    from src.config.settings import load_settings
    graph = build_application_graph(load_settings(), services.db)

    assert graph.telegram_adapter is None
    assert graph.worker_handler._telegram_worker is None
    assert "telegram" not in PlatformAdapterFactory._adapters


def test_a_malformed_secret_raises_out_of_the_build(monkeypatch):
    services = prod_env.install_offline_services(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "not-a-token")
    from src.config.settings import load_settings
    config = load_settings()

    with pytest.raises(ValueError, match="Invalid TELEGRAM_BOT_TOKEN format"):
        build_application_graph(config, services.db)
