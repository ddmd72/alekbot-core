"""The Cabinet's Gmail and Google Tasks cards on the re-keyed credential port (MULTI_DRIVE_RFC §4.2, slice 1a): the
status reads the user's credential of the card's kind, a disconnect revokes at Google with that credential's token as
today and deletes only the user's records of that kind. Real `FirestoreCredentialStore` over `EvalDb`; Google's revoke
is the recorded `GoogleOAuthService.revoke_token` boundary."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.adapters.firestore_credential_store import FirestoreCredentialStore
from src.composition.connector_registry import ConnectorRegistry
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb
from tests.support.oauth_credentials_store import creds

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
ALEX, CLARA = "user-alex", "user-clara"
CREDS = "eval_credentials"
AUTH = {"Authorization": "Bearer t"}


class Users:
    async def get_user(self, user_id):
        return None


class World:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={CREDS: "development_credentials"})
        self.store = FirestoreCredentialStore(self.db, SimpleNamespace(credentials_collection=CREDS),
                                              cipher=AesGcmTestCipher())
        self.gmail = MagicMock()
        self.gmail.revoke_token = AsyncMock()
        self.tasks = MagicMock()
        self.tasks.revoke_token = AsyncMock()

    def app(self) -> Quart:
        sessions = MagicMock()
        sessions.verify_access_token = MagicMock(return_value={"sub": ALEX, "account_id": "acct-1", "role": "owner"})
        app = Quart("test_cabinet_google_disconnect_by_credential")
        app.register_blueprint(create_user_cabinet_blueprint(
            invite_service=MagicMock(), session_service=sessions, user_repo=Users(), fact_repo=MagicMock(),
            embedding_service=MagicMock(), oauth_credentials_port=self.store, gmail_oauth_service=self.gmail,
            google_tasks_oauth_service=self.tasks, connectors=ConnectorRegistry()))
        return app

    async def seed(self) -> None:
        await self.store.save(creds(ALEX, "gmail", access_token="at-gmail"))
        await self.store.save(creds(ALEX, "google_tasks", access_token="at-tasks",
                                    account=replace(creds(ALEX, "google_tasks").account, label="alex@example.com")))
        await self.store.save(creds(CLARA, "gmail", access_token="at-clara"))

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
    world.gmail.revoke_token.assert_awaited_once_with("at-gmail")
    assert await world.kinds(ALEX) == ["google_tasks"]
    assert await world.kinds(CLARA) == ["gmail"]


async def test_the_tasks_disconnect_revokes_its_token_and_deletes_only_tasks(world):
    await world.seed()
    async with world.app().test_client() as client:
        resp = await client.delete("/api/tasks/disconnect", headers=AUTH)
    assert resp.status_code == 200
    world.tasks.revoke_token.assert_awaited_once_with("at-tasks")
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
    world.gmail.revoke_token.assert_not_awaited()
    assert await world.kinds(ALEX) == ["google_tasks"]


async def test_without_the_registry_the_cards_read_not_connected(world):
    await world.seed()
    sessions = MagicMock()
    sessions.verify_access_token = MagicMock(return_value={"sub": ALEX, "account_id": "acct-1", "role": "owner"})
    app = Quart("test_cabinet_no_registry")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=sessions, user_repo=Users(), fact_repo=MagicMock(),
        embedding_service=MagicMock(), oauth_credentials_port=world.store, gmail_oauth_service=world.gmail))
    async with app.test_client() as client:
        assert await (await client.get("/api/gmail/status", headers=AUTH)).get_json() == {"connected": False}
        assert (await client.delete("/api/gmail/disconnect", headers=AUTH)).status_code == 501
    assert await world.kinds(ALEX) == ["gmail", "google_tasks"]
