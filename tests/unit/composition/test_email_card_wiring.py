"""The online card step is wired behind EMAIL_CARD_ONLINE (default off) onto the graph's one document service, mailbox
provider and embedding adapter (email index v2 §6.5, slice 6)."""
from types import SimpleNamespace

import pytest

from src.composition.email_card_setup import attach_email_card_step, email_card_online_enabled
from tests.support import prod_env


async def _graph(monkeypatch, value):
    """The production graph over the sealed offline environment; the switch is set after the seal (it clears the env)."""
    from src.composition.application_graph import build_application_graph
    from src.config.settings import load_settings

    offline = prod_env.install_offline_services(monkeypatch)
    if value is None:
        monkeypatch.delenv("EMAIL_CARD_ONLINE", raising=False)
    else:
        monkeypatch.setenv("EMAIL_CARD_ONLINE", value)
    app = build_application_graph(load_settings(), offline.db)
    await app.start()
    return SimpleNamespace(app=app)


async def test_on_the_graph_attaches_one_step_on_the_graphs_own_objects(monkeypatch):
    graph = await _graph(monkeypatch, "1")
    try:
        app = graph.app
        step = app.worker_handler._email_indexing._card_step
        assert step is not None
        service = step._cards
        assert service._provider is app.worker_handler._email_indexing._gmail
        assert service._attachments._provider is app.worker_handler._email_indexing._gmail
        assert service._attachments._documents is app.agent_factory.file_conversion_service._documents
        assert service._embeddings is app.worker_handler._email_indexing._embedding
    finally:
        await graph.app.shutdown()


@pytest.mark.parametrize("value", [None, "0", "off", ""])
async def test_off_or_unset_attaches_no_step(monkeypatch, value):
    graph = await _graph(monkeypatch, value)
    try:
        assert graph.app.worker_handler._email_indexing._card_step is None
    finally:
        await graph.app.shutdown()


def test_without_a_document_service_there_is_no_step(monkeypatch):
    monkeypatch.setenv("EMAIL_CARD_ONLINE", "1")
    service = SimpleNamespace(attached=None, attach_card_step=lambda step: setattr(service, "attached", step))
    assert attach_email_card_step(service, SimpleNamespace(), None, None, None, None, None) is False
    assert service.attached is None


def test_the_switch_reads_the_environment_at_call_time(monkeypatch):
    monkeypatch.delenv("EMAIL_CARD_ONLINE", raising=False)
    assert email_card_online_enabled() is False
    monkeypatch.setenv("EMAIL_CARD_ONLINE", "1")
    assert email_card_online_enabled() is True
