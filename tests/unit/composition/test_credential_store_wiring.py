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
    """The Google consents are the graph's connect service over its Google edge (MULTI_DRIVE_RFC §5.1, §5.2): the
    consent URL of each Google kind asks for the identity scopes next to the kind's own."""
    from datetime import datetime, timedelta, timezone
    from urllib.parse import parse_qs, urlparse
    from src.domain.flow_intent import FlowIntent
    from src.domain.oauth_flow import OAuthFlow

    app = prod_graph.app
    flow = OAuthFlow(user_id="user-alex", kind="gmail", intent=FlowIntent.CONNECT, target_id="", expected_subject="",
                     hint_label="", pkce_verifier="v" * 43, return_path="/cabinet",
                     expires_at=datetime.now(timezone.utc) + timedelta(minutes=10))
    edge = app.connect_service._edges["google"]
    for kind in ("gmail", "google_tasks"):
        spec = app.connect_service._specs[kind]
        url = edge.authorization_url(flow, spec, app.connect_service._redirect_uri, state="s")
        scopes = set(parse_qs(urlparse(url).query)["scope"][0].split())
        assert {"openid", "email"} | set(spec.required_scopes) <= scopes
