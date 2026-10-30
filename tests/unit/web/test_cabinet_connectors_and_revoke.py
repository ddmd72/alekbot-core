"""The Cabinet side of slice 1a (MULTI_DRIVE_RFC §4.3, §14 row 1a):

- `GET /api/connectors` lists the kinds the Cabinet offers (frozen Google Tasks left out, and a kind whose realm has
  no edge on this deployment), with integration and label; anonymous is refused;
- the Gmail and Google Tasks disconnects go through `CredentialRevocationService`: our record always goes, Google is
  revoked only when no stored credential of that account remains; Gmail marks the deliberate disconnect first;
- the Gmail and drive status carry the credential id the Reconnect link names.

Real: the cabinet blueprint, `FirestoreCredentialStore` over `EvalDb`, `CredentialRevocationService`, the real Google
edge (its revoke POST is seen at the HTTP boundary fake), `OneDriveAdapter` for the drive card."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, create_autospec

import pytest
from quart import Quart

from src.adapters.firestore_credential_store import FirestoreCredentialStore
from src.adapters.google.google_oauth_provider import GoogleOAuthProvider
from src.adapters.microsoft.microsoft_oauth_provider import MicrosoftOAuthProvider
from src.adapters.microsoft.onedrive_adapter import OneDriveAdapter
from src.composition.connector_registry import ConnectorRegistry
from src.services.credential_revocation_service import CredentialRevocationService
from src.services.integration_reconnect_service import IntegrationReconnectService
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support import http_boundary_fake
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb
from tests.support.oauth_credentials_store import creds

ALEX, OTHER_USER = "user-alex", "user-2"
AUTH = {"Authorization": "Bearer t"}
REVOKE = "https://oauth2.googleapis.com/revoke"


class Users:
    async def get_user(self, user_id):
        return None


class World:
    def __init__(self, monkeypatch, *, microsoft: bool = True, revocation: bool = True) -> None:
        self.http = http_boundary_fake.install(monkeypatch)
        self.http.route("POST", REVOKE, 200, {})
        self.store = FirestoreCredentialStore(EvalDb(), SimpleNamespace(credentials_collection="eval_credentials"),
                                              cipher=AesGcmTestCipher())
        edges = {"google": GoogleOAuthProvider("g-cid", "g-csecret")}
        if microsoft:
            edges["microsoft"] = MicrosoftOAuthProvider("ms-cid", "ms-csecret")
        self.registry = ConnectorRegistry(edges=edges)
        self.events = []
        self.reconnect = create_autospec(IntegrationReconnectService, instance=True)

        async def mark(user_id, integration):
            held = [k.credential_id for k in await self.store.list_keys(user_id, "gmail")]
            self.events.append(("mark", integration.value, held))

        async def status(user_id, integrations):
            return {i.value: False for i in integrations}

        self.reconnect.mark_disconnected.side_effect = mark
        self.reconnect.status.side_effect = status
        self.revocation = CredentialRevocationService(self.store, self.registry.edges()) if revocation else None
        self.drive = OneDriveAdapter(self.store, "ms-cid", "ms-csecret",
                                     connector=self.registry.get("microsoft_onedrive"))

    def app(self, *, registry=True) -> Quart:
        sessions = MagicMock()
        sessions.verify_access_token = MagicMock(return_value={"sub": ALEX, "account_id": "acct-1", "role": "owner"})
        app = Quart("test_cabinet_connectors_and_revoke")
        app.register_blueprint(create_user_cabinet_blueprint(
            invite_service=MagicMock(), session_service=sessions, user_repo=Users(), fact_repo=MagicMock(),
            embedding_service=MagicMock(), oauth_credentials_port=self.store, user_drive=self.drive,
            credential_revocation=self.revocation, reconnect=self.reconnect,
            connectors=self.registry if registry else None))
        return app

    async def save(self, user_id, kind, subject="sub-1", **over):
        cred = creds(user_id, kind, **over)
        await self.store.save(replace(cred, account=replace(cred.account, subject=subject)))

    async def kinds(self, user_id):
        return sorted(c.kind for c in await self.store.list_for_user(user_id))

    def revoked(self):
        return [r.params["token"] for r in self.http.requests_to("POST", REVOKE)]


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


# --------------------------------------------------------------------------------------------- /api/connectors

async def test_the_connectors_are_the_offered_kinds_with_integration_and_label(world):
    async with world.app().test_client() as client:
        resp = await client.get("/api/connectors", headers=AUTH)
    assert resp.status_code == 200
    assert (await resp.get_json())["connectors"] == [
        {"kind": "gmail", "integration": "mail", "label": "Gmail"},
        {"kind": "microsoft_todo", "integration": "tasks", "label": "Microsoft To Do"},
        {"kind": "microsoft_onedrive", "integration": "drive", "label": "OneDrive"},
    ]


async def test_a_realm_without_an_edge_offers_no_connector(monkeypatch):
    world = World(monkeypatch, microsoft=False)
    async with world.app().test_client() as client:
        body = await (await client.get("/api/connectors", headers=AUTH)).get_json()
    assert [c["kind"] for c in body["connectors"]] == ["gmail"]


async def test_without_the_registry_there_are_no_connectors(world):
    async with world.app(registry=False).test_client() as client:
        assert await (await client.get("/api/connectors", headers=AUTH)).get_json() == {"connectors": []}


async def test_an_anonymous_connectors_read_is_refused(world):
    async with world.app().test_client() as client:
        resp = await client.get("/api/connectors")
    assert resp.status_code == 401


# ----------------------------------------------------------------------------------------------- disconnects

async def test_a_gmail_disconnect_marks_first_then_removes_and_keeps_the_grant_another_service_uses(world):
    await world.save(ALEX, "gmail", access_token="at-gmail")
    await world.save(ALEX, "google_tasks", access_token="at-tasks")       # the same Google account
    async with world.app().test_client() as client:
        resp = await client.delete("/api/gmail/disconnect", headers=AUTH)
    assert resp.status_code == 200
    [(event, integration, held)] = world.events
    assert (event, integration, len(held)) == ("mark", "mail", 1)       # marked while the credential was still there
    assert await world.kinds(ALEX) == ["google_tasks"]
    assert world.revoked() == []                                         # Google Tasks still holds the grant


async def test_the_last_credential_of_an_account_is_revoked_at_google(world):
    await world.save(ALEX, "gmail", access_token="at-gmail")
    await world.save(ALEX, "google_tasks", access_token="at-tasks")
    async with world.app().test_client() as client:
        await client.delete("/api/gmail/disconnect", headers=AUTH)
        resp = await client.delete("/api/tasks/disconnect", headers=AUTH)
    assert resp.status_code == 200
    assert await world.kinds(ALEX) == []
    assert world.revoked() == ["at-tasks"]


async def test_another_users_credential_of_the_same_account_keeps_the_grant(world):
    await world.save(ALEX, "gmail", access_token="at-gmail")
    await world.save(OTHER_USER, "gmail", access_token="at-other")       # the same provider account held by two users
    async with world.app().test_client() as client:
        await client.delete("/api/gmail/disconnect", headers=AUTH)
    assert await world.kinds(ALEX) == [] and await world.kinds(OTHER_USER) == ["gmail"]
    assert world.revoked() == []


async def test_a_lone_gmail_credential_is_revoked_on_disconnect(world):
    await world.save(ALEX, "gmail", access_token="at-gmail")
    async with world.app().test_client() as client:
        await client.delete("/api/gmail/disconnect", headers=AUTH)
    assert world.revoked() == ["at-gmail"]


async def test_without_the_revocation_service_a_disconnect_is_501_and_removes_nothing(monkeypatch):
    world = World(monkeypatch, revocation=False)
    await world.save(ALEX, "gmail")
    await world.save(ALEX, "google_tasks")
    async with world.app().test_client() as client:
        assert (await client.delete("/api/gmail/disconnect", headers=AUTH)).status_code == 501
        assert (await client.delete("/api/tasks/disconnect", headers=AUTH)).status_code == 501
    assert await world.kinds(ALEX) == ["gmail", "google_tasks"]
    assert world.events == [] and world.revoked() == []


# --------------------------------------------------------------------------------------- the reconnect target

async def test_the_gmail_status_names_the_credential(world):
    await world.save(ALEX, "gmail")
    (held,) = await world.store.list_for_user(ALEX, "gmail")
    async with world.app().test_client() as client:
        body = await (await client.get("/api/gmail/status", headers=AUTH)).get_json()
    assert body["connected"] is True and body["credential_id"] == held.credential_id


async def test_the_drive_status_names_the_credential_only_while_connected(world):
    async with world.app().test_client() as client:
        assert "credential_id" not in await (await client.get("/api/drive/status", headers=AUTH)).get_json()
        await world.save(ALEX, "microsoft_onedrive")
        (held,) = await world.store.list_for_user(ALEX, "microsoft_onedrive")
        body = await (await client.get("/api/drive/status", headers=AUTH)).get_json()
    assert body["connected"] is True and body["credential_id"] == held.credential_id
