"""The main graph's credential store (SECRETS_AT_REST_RFC §2.3, §2.6; plan D-P1, I9): the one encrypted store over the
graph's one cipher, and both Google consents ask for the identity scopes. Built over the sealed deploy environment."""
import pytest

from tests.support.prod_graph import prod_graph  # noqa: F401  (the fixture)


@pytest.mark.slow
async def test_the_main_graph_store_uses_the_graphs_one_cipher(prod_graph):
    from src.adapters.firestore_credential_store import FirestoreCredentialStore
    app = prod_graph.app
    store = app.container.oauth_credentials
    assert isinstance(store, FirestoreCredentialStore) and store._cipher is app.cipher


@pytest.mark.slow
async def test_both_google_consents_ask_for_the_identity(prod_graph):
    for service in (prod_graph.app.gmail_oauth_service, prod_graph.app.google_tasks_oauth_service):
        assert {"openid", "email"} <= set(service._scopes)
