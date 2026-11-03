"""Wiring of `build_application_graph` that no other test observes (mutation survivors, wire-drop).

The graph is built with the deployed environment over fakes at the SDK boundaries (`tests/support/prod_env.py`). The
collaborators handed to `build_drive_activation` and the HTML renderer handed to the Slack and Telegram adapter
factories are recorded while the real callee still runs, then compared by identity with the objects the graph built.
"""
import pytest

from src.composition import application_graph
from src.composition.application_graph import build_application_graph
from src.composition.slack_adapter_factory import SlackAdapterFactory
from src.composition.telegram_adapter_factory import TelegramAdapterFactory
from tests.support import prod_env

pytestmark = pytest.mark.slow


class _Renderer:
    """Stands in for the Playwright renderer: the graph only hands it on."""

    async def stop(self) -> None:
        return None


def _record(monkeypatch, owner, name, calls):
    real = getattr(owner, name)

    def recording(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, staticmethod(recording) if isinstance(owner, type) else recording)


def _build(monkeypatch):
    services = prod_env.install_offline_services(monkeypatch)
    monkeypatch.setenv("ENABLE_HTML_RENDERER", "true")
    renderer = _Renderer()
    monkeypatch.setattr(application_graph, "PlaywrightHtmlRenderer", lambda *a, **k: renderer)
    activation, slack, telegram = [], [], []
    _record(monkeypatch, application_graph, "build_drive_activation", activation)
    _record(monkeypatch, SlackAdapterFactory, "create_adapter", slack)
    _record(monkeypatch, TelegramAdapterFactory, "create_adapter", telegram)
    from src.config.settings import load_settings
    graph = build_application_graph(load_settings(), services.db)
    return graph, renderer, activation, slack, telegram


def test_the_drive_activation_gets_the_drive_services_of_the_container(monkeypatch):
    graph, _renderer, activation, _slack, _telegram = _build(monkeypatch)
    container = graph.container

    assert len(activation) == 1
    kwargs = activation[0]
    assert container.drive_connection_service is not None
    assert kwargs["connections"] is container.drive_connection_service
    assert kwargs["drive"] is container.user_drive
    assert kwargs["task_queue"] is container.drive_task_queue
    assert kwargs["task_queue"] is not None


def test_the_html_renderer_reaches_the_slack_and_telegram_adapters(monkeypatch):
    _graph, renderer, _activation, slack, telegram = _build(monkeypatch)

    assert len(slack) == 1 and slack[0]["html_renderer"] is renderer
    assert len(telegram) == 1 and telegram[0]["html_renderer"] is renderer
