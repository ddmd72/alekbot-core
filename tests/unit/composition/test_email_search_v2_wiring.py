"""Email search v2 is wired behind EMAIL_SEARCH_V2 (code default off; email index v2 §6.5, slice 8): on, the user's email
agent holds the container's one card search, built on the facts collection, the container's embedding adapter and its
confirmed-people list; unset or off, the agent keeps v1 (no card search anywhere)."""
from types import SimpleNamespace

import pytest

from src.adapters.firestore_email_card_index import FirestoreEmailCardIndex
from src.composition.email_search_setup import build_email_card_search, email_search_v2_enabled
from src.config.environment import EnvironmentConfig
from tests.support import prod_env
from tests.support.prod_graph import USER_ID, seed

pytestmark = pytest.mark.slow     # each test builds and starts the production graph


async def _graph(monkeypatch, value):
    """The production graph over the sealed offline environment, with user u1's eager agents built; the switch is set
    after the seal (it clears the env)."""
    from src.composition.application_graph import build_application_graph
    from src.config.settings import load_settings

    offline = prod_env.install_offline_services(monkeypatch)
    if value is None:
        monkeypatch.delenv("EMAIL_SEARCH_V2", raising=False)
    else:
        monkeypatch.setenv("EMAIL_SEARCH_V2", value)
    app = build_application_graph(load_settings(), offline.db)
    await app.start()
    await seed(app)
    await app.agent_factory.ensure_agents_for_user(USER_ID)
    return SimpleNamespace(app=app, db=offline.db)


async def test_on_the_agent_holds_the_containers_one_card_search(monkeypatch):
    graph = await _graph(monkeypatch, "1")
    try:
        app = graph.app
        container = app.container
        search = container.email_card_search
        assert search is not None
        assert app.agent_factory.email_card_search is search
        agent = app.coordinator.agents[f"email_search_agent_{USER_ID}"]
        assert agent._card_search is search
        assert search._embeddings is container.embedding_service
        assert search._people is container.confirmed_people
        assert isinstance(search._index, FirestoreEmailCardIndex)
        assert search._index._col.name == EnvironmentConfig().domain_email_facts_collection
    finally:
        await graph.app.shutdown()


@pytest.mark.parametrize("value", [None, "0", "off", ""])
async def test_off_or_unset_keeps_v1(monkeypatch, value):
    graph = await _graph(monkeypatch, value)
    try:
        assert graph.app.container.email_card_search is None
        assert graph.app.coordinator.agents[f"email_search_agent_{USER_ID}"]._card_search is None
    finally:
        await graph.app.shutdown()


def test_the_switch_reads_the_environment_at_call_time(monkeypatch):
    monkeypatch.delenv("EMAIL_SEARCH_V2", raising=False)
    assert email_search_v2_enabled() is False
    assert build_email_card_search(None, None, None, None) is None
    monkeypatch.setenv("EMAIL_SEARCH_V2", "1")
    assert email_search_v2_enabled() is True
