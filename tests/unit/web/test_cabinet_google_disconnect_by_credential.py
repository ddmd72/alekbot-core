"""The Cabinet's Gmail and Google Tasks cards on the re-keyed credential port (MULTI_DRIVE_RFC §4.2, slice 1a): the
status reads the user's credential of the card's kind, a disconnect deletes only the user's records of that kind
through the revoke rule (§4.3, `CredentialRevocationService`), which revokes at Google with that credential's token
when it was the last credential of its account. Each seeded credential here is the only one of its Google account, so
every disconnect revokes; the cases where another credential of the account keeps the grant are
`test_cabinet_connectors_and_revoke.py`'s. Real `FirestoreCredentialStore` over `EvalDb`; Google is a recording edge."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from quart import Quart

from src.adapters.firestore_credential_store import FirestoreCredentialStore
from src.composition.connector_registry import ConnectorRegistry
from src.domain.revocation_scope import RevocationScope
from src.ports.oauth_provider_port import OAuthProviderPort
from src.services.credential_revocation_service import CredentialRevocationService
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb
from tests.support.oauth_credentials_store import creds

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
ALEX, CLARA = "user-alex", "user-clara"
CREDS = "eval_credentials"
AUTH = {"Authorization": "Bearer t"}


class GoogleEdge(OAuthProviderPort):
    """Google's revoke boundary, recorded: the token each revoke sends."""

    def __init__(self) -> None:
        self.revoked: list = []

    realm = property(lambda self: "google")
    revocation_scope = property(lambda self: RevocationScope.GRANT_WIDE)

    def authorization_url(self, flow, spec, redirect_uri, *, state):
        raise NotImplementedError

    async def exchange(self, code, flow, spec, redirect_uri):
        raise NotImplementedError

    async def revoke(self, credential):
        self.revoked.append(credential.access_token)


class Users:
    async def get_user(self, user_id):
        return None


class World:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={CREDS: "development_credentials"})
        self.store = FirestoreCredentialStore(self.db, SimpleNamespace(credentials_collection=CREDS),
                                              cipher=AesGcmTestCipher())
        self.google = GoogleEdge()
        self.revocation = CredentialRevocationService(self.store, {"google": self.google})

    def app(self) -> Quart:
        sessions = MagicMock()
        sessions.verify_access_token = MagicMock(return_value={"sub": ALEX, "account_id": "acct-1", "role": "owner"})
        app = Quart("test_cabinet_google_disconnect_by_credential")
        app.register_blueprint(create_user_cabinet_blueprint(
            invite_service=MagicMock(), session_service=sessions, user_repo=Users(), fact_repo=MagicMock(),
            embedding_service=MagicMock(), oauth_credentials_port=self.store, credential_revocation=self.revocation,
            connectors=ConnectorRegistry()))
        return app

    async def seed(self) -> None:
        await self.store.save(creds(ALEX, "gmail", access_token="at-gmail"))
        await self.store.save(creds(ALEX, "google_tasks", access_token="at-tasks",
                                    account=replace(creds(ALEX, "google_tasks").account, label="alex@example.com")))
        await self.store.save(creds(CLARA, "gmail", access_token="at-clara",       # another Google account
                                    account=replace(creds(CLARA, "gmail").account, subject="sub-clara")))

    async def kinds(self, user_id: str) -> list:
        return sorted(c.kind for c in await self.store.list_for_user(user_id))


@pytest.fixture
def world() -> World:
    return World()


async def test_the_gmail_disconnect_revokes_its_token_and_deletes_only_gmail(world):
    await world.seed()
    async with world.app().test_client() as client:
        resp = await client.delete("/api/gmail/disconnect", headers=AUTH)
    assert resp.status_code == 200
    assert world.google.revoked == ["at-gmail"]
    assert await world.kinds(ALEX) == ["google_tasks"]
    assert await world.kinds(CLARA) == ["gmail"]


async def test_the_tasks_disconnect_revokes_its_token_and_deletes_only_tasks(world):
    await world.seed()
    async with world.app().test_client() as client:
        resp = await client.delete("/api/tasks/disconnect", headers=AUTH)
    assert resp.status_code == 200
    assert world.google.revoked == ["at-tasks"]
    assert await world.kinds(ALEX) == ["gmail"]


async def test_the_tasks_status_shows_the_account_label(world):
    await world.seed()
    async with world.app().test_client() as client:
        body = await (await client.get("/api/tasks/status", headers=AUTH)).get_json()
    assert body == {"connected": True, "email_address": "alex@example.com"}


async def test_a_disconnect_of_a_kind_not_connected_is_404_and_deletes_nothing(world):
    await world.store.save(creds(ALEX, "google_tasks"))
    async with world.app().test_client() as client:
        resp = await client.delete("/api/gmail/disconnect", headers=AUTH)
    assert resp.status_code == 404
    assert world.google.revoked == []
    assert await world.kinds(ALEX) == ["google_tasks"]


async def test_without_the_registry_the_cards_read_not_connected(world):
    await world.seed()
    sessions = MagicMock()
    sessions.verify_access_token = MagicMock(return_value={"sub": ALEX, "account_id": "acct-1", "role": "owner"})
    app = Quart("test_cabinet_no_registry")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=sessions, user_repo=Users(), fact_repo=MagicMock(),
        embedding_service=MagicMock(), oauth_credentials_port=world.store, credential_revocation=world.revocation))
    async with app.test_client() as client:
        assert await (await client.get("/api/gmail/status", headers=AUTH)).get_json() == {"connected": False}
        assert (await client.delete("/api/gmail/disconnect", headers=AUTH)).status_code == 501
    assert await world.kinds(ALEX) == ["gmail", "google_tasks"] and world.google.revoked == []
