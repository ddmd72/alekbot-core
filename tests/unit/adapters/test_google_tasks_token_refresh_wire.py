"""GoogleTasksAdapter token refresh at the HTTP boundary (Google Tasks is frozen; slice 1a only moved the identity
onto `replace`): the refresh request carries the stored refresh token, and the refreshed credential keeps its identity,
clears the reconnect flag and is what the store then holds."""
from datetime import datetime, timedelta, timezone

import pytest

from src.adapters.google_tasks_adapter import GoogleTasksAdapter
from src.composition.connector_registry import ConnectorRegistry
from tests.support import http_boundary_fake
from tests.support.oauth_credentials_store import CredentialsStore, creds, one

USER = "user-alex"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SOON = datetime.now(timezone.utc) + timedelta(minutes=1)


@pytest.fixture
def world(monkeypatch):
    return CredentialsStore(), http_boundary_fake.install(monkeypatch)


def _adapter(store: CredentialsStore) -> GoogleTasksAdapter:
    """The frozen adapter no longer implements the whole port; the auth path under test needs none of the rest."""
    adapter_cls = type("GoogleTasksAuthOnly", (GoogleTasksAdapter,), {})
    adapter_cls.__abstractmethods__ = frozenset()
    return adapter_cls(store.repo, "cid", "csecret", connector=ConnectorRegistry().get("google_tasks"))


async def test_a_refresh_stores_the_new_token_and_keeps_identity_and_refresh_token(world):
    store, http = world
    await store.repo.save(creds(USER, "google_tasks", token_expiry=SOON, needs_reconnect=True))
    http.route("POST", TOKEN_URL, body={"access_token": "at-new", "expires_in": 3600})
    got = await _adapter(store)._get_fresh_credentials(USER)
    stored = await one(store.repo, USER, "google_tasks")
    assert got.access_token == stored.access_token == "at-new"
    assert stored.refresh_token == "rt-old" and stored.needs_reconnect is False
    assert stored.account == creds(USER, "google_tasks").account and stored.integration == got.integration
    assert stored.token_expiry > datetime.now(timezone.utc) + timedelta(minutes=30)
    [sent] = http.requests_to("POST", TOKEN_URL)
    assert sent.data["refresh_token"] == "rt-old" and sent.data["grant_type"] == "refresh_token"


async def test_a_rotated_refresh_token_replaces_the_stored_one(world):
    store, http = world
    await store.repo.save(creds(USER, "google_tasks", token_expiry=SOON))
    http.route("POST", TOKEN_URL, body={"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 3600})
    await _adapter(store)._get_fresh_credentials(USER)
    assert (await one(store.repo, USER, "google_tasks")).refresh_token == "rt-new"
