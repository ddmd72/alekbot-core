"""The production graph builds the Slack adapter before WorkerHandler and hands it the queued-event entry at
construction; the Telegram queued-update entry reaches WorkerHandler only through `register_late_platform`, the one
late Telegram binding (owner ruling 2026-11-17). Built with the deployed environment (`tests/support/prod_env.py`)."""
import pytest

from src.composition import application_graph
from src.composition.application_graph import build_application_graph
from src.composition.platform_kit import register_late_platform
from src.handlers.worker_handler import WorkerHandler
from tests.support import prod_env

pytestmark = pytest.mark.slow


def _build(monkeypatch):
    services = prod_env.install_offline_services(monkeypatch)
    from src.config.settings import load_settings
    return build_application_graph(load_settings(), services.db)


def test_the_slack_worker_is_a_constructor_argument_of_the_worker_handler(monkeypatch):
    constructed = []

    class Recording(WorkerHandler):
        def __init__(self, *args, **kwargs):
            constructed.append(dict(kwargs))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(application_graph, "WorkerHandler", Recording)
    graph = _build(monkeypatch)

    [kwargs] = constructed
    assert kwargs["slack_worker"] == graph.slack_adapter.handle_queued_event
    assert graph.worker_handler._slack_worker == graph.slack_adapter.handle_queued_event


def test_the_telegram_worker_arrives_through_the_one_late_platform_binding(monkeypatch):
    bindings = []

    def recording(kit, **kwargs):
        bindings.append((kit.platform, kwargs))
        register_late_platform(kit, **kwargs)

    monkeypatch.setattr(application_graph, "register_late_platform", recording)
    graph = _build(monkeypatch)

    [(platform, kwargs)] = bindings
    assert platform == "telegram"
    assert kwargs["worker"] is graph.worker_handler
    assert kwargs["queued_update"] == graph.telegram_adapter.handle_queued_update
    assert graph.worker_handler._telegram_worker == graph.telegram_adapter.handle_queued_update


def test_without_the_late_binding_no_telegram_worker_is_bound(monkeypatch):
    monkeypatch.setattr(application_graph, "register_late_platform", lambda kit, **kwargs: None)
    graph = _build(monkeypatch)

    assert graph.telegram_adapter is not None
    assert graph.worker_handler._telegram_worker is None
