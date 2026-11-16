"""Email card search is wired (email index v2 §6.5; the EMAIL_SEARCH_V2 switch is gone, slice 10): the user's email
agent holds the container's one card search, built on the facts collection, the container's embedding adapter and its
confirmed-people list."""
from types import SimpleNamespace

import pytest

from src.adapters.firestore_email_card_index import FirestoreEmailCardIndex
from src.config.environment import EnvironmentConfig
from tests.support import prod_env
from tests.support.prod_graph import USER_ID, seed

pytestmark = pytest.mark.slow     # each test builds and starts the production graph


async def _graph(monkeypatch):
    """The production graph over the sealed offline environment, with user u1's eager agents built."""
    from src.composition.application_graph import build_application_graph
    from src.config.settings import load_settings

    offline = prod_env.install_offline_services(monkeypatch)
    app = build_application_graph(load_settings(), offline.db)
    await app.start()
    await seed(app)
    await app.agent_factory.ensure_agents_for_user(USER_ID)
    return SimpleNamespace(app=app, db=offline.db)


async def test_the_agent_holds_the_containers_one_card_search(monkeypatch):
    graph = await _graph(monkeypatch)
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
