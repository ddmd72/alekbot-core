"""R1 in the Cabinet (SECRETS_AT_REST_RFC §6.2, F1; plan W2.4): the three status routes answer `reconnect_needed` when an
integration reads "not connected" but its feature state shows a past connection; the Gmail and Drive disconnects mark
the feature state before the credential goes; a consent through the generic connect routes (MULTI_DRIVE_RFC §5.2: the
start, then the one callback, over the real `OAuthConnectService`, both edges and the activations) clears the mark and
the notice flag.

The real `IntegrationReconnectService` runs over the real credential, indexing-state and To Do config stores on one
evaluating Firestore fake, and the in-memory drive index repository; the drive is a fake with a settable connection."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List
from unittest.mock import AsyncMock, MagicMock, Mock
from urllib.parse import parse_qs, urlparse

import pytest
from quart import Quart

from src.adapters.firestore_indexed_email_repo import FirestoreIndexedEmailRepository
from src.adapters.firestore_credential_store import FirestoreCredentialStore
from src.adapters.firestore_task_config_repository import FirestoreTaskConfigRepository
from src.adapters.google.google_oauth_provider import GoogleOAuthProvider
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.adapters.microsoft import OneDriveAdapter
from src.adapters.microsoft.microsoft_oauth_provider import MicrosoftOAuthProvider
from src.composition.connector_registry import ConnectorRegistry
from src.composition.oauth_connect_setup import build_connect_activations, build_oauth_connect_service
from src.domain.billing import BillingAccount
from src.domain.drive_index import DriveSyncState
from src.domain.email import IndexingState
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from src.domain.task import TaskUserConfig
from src.domain.user import UserProfile
from src.domain.user_drive import DriveConnection
from src.services.credential_revocation_service import CredentialRevocationService
from src.services.integration_reconnect_service import IntegrationReconnectService
from src.services.session_service import SessionService
from src.services.task_setup_service import TaskSetupService
from src.web.oauth_app import create_oauth_blueprint
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support import http_boundary_fake
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb
from tests.support.oauth_credentials_store import has_kind

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
USER = "user-a"
NOW = datetime(2026, 10, 29, 9, 30, tzinfo=timezone.utc)
CREDS, FACTS, STATE, TASKS = "eval_credentials", "eval_email_facts", "eval_email_state", "eval_task_config"
AUTH = {"Authorization": "Bearer t"}
SECRET = "session-secret-for-tests-0123456789abcdef"
GRANTED = {
    "gmail": "https://www.googleapis.com/auth/gmail.readonly openid email",
    "microsoft_todo": "Tasks.ReadWrite offline_access User.Read",
    "microsoft_onedrive": "Files.ReadWrite.AppFolder offline_access User.Read",
}


class Drive:
    display_name = "Drive"

    def __init__(self) -> None:
        self.state = DriveConnection(connected=False)
        self.disconnected: List[str] = []

    async def connection(self, user_id):
        return self.state

    async def disconnect(self, user_id):
        self.disconnected.append(user_id)
        self.state = DriveConnection(connected=False)


class Users:
    async def get_user(self, user_id):
        return None


class World:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={CREDS: "development_credentials",
                                                        FACTS: "development_domain_email_facts_v1",
                                                        STATE: "development_email_indexing_state"})
        env = SimpleNamespace(credentials_collection=CREDS, domain_email_facts_collection=FACTS,
                              email_indexing_state_collection=STATE, task_config_collection=TASKS)
        self.creds = FirestoreCredentialStore(self.db, env, cipher=AesGcmTestCipher())
        self.email = FirestoreIndexedEmailRepository(self.db, env)
        self.tasks = FirestoreTaskConfigRepository(self.db, env)
        self.drive_states = InMemoryDriveIndexRepository()
        self.drive = Drive()
        self.reconnect = IntegrationReconnectService(
            credentials=self.creds, users=Users(), notifier=None, email_states=self.email, task_config=self.tasks,
            drive_states=self.drive_states, drive=self.drive, clock=lambda: NOW, mail_connector=ConnectorRegistry().get("gmail"), todo_connector=ConnectorRegistry().get("microsoft_todo"))
        # get_status reads only the credential port and the config
        self.task_setup = TaskSetupService(lifecycle=None, task_config=self.tasks, tasks_provider=None,
                                           oauth_credentials=self.creds, task_search_index=None, task_queue=None,
                                           notification_url_base="", connector=ConnectorRegistry().get("microsoft_todo"))

    def cabinet(self) -> Quart:
        session_service = MagicMock()
        session_service.verify_access_token = MagicMock(return_value={"sub": USER, "account_id": "acc-a",
                                                                       "role": "owner"})
        bp = create_user_cabinet_blueprint(
            invite_service=MagicMock(), session_service=session_service, user_repo=Users(), fact_repo=MagicMock(),
            embedding_service=MagicMock(), oauth_credentials_port=self.creds, indexed_email_repo=self.email,
            task_setup=self.task_setup, user_drive=self.drive, reconnect=self.reconnect, connectors=ConnectorRegistry(),
            credential_revocation=CredentialRevocationService(self.creds, {}))
        app = Quart("test_cabinet_reconnect")
        app.register_blueprint(bp)
        return app

    async def consent(self, monkeypatch, kind: str):
        """The user's consent of `kind` through the generic connect routes, over this world's credential store and its
        real `IntegrationReconnectService` (the activations call its `consented`). The provider side is the HTTP
        boundary fake; the drive-id read after the token is refused (best effort)."""
        http = http_boundary_fake.install(monkeypatch)
        http.route("POST", "https://oauth2.googleapis.com/token", body={
            "access_token": "at", "refresh_token": "rt", "expires_in": 3600, "scope": GRANTED[kind]})
        http.route("POST", "https://login.microsoftonline.com/consumers/oauth2/v2.0/token", body={
            "access_token": "a", "refresh_token": "r", "expires_in": 3600, "scope": GRANTED[kind]})
        http.route("GET", "https://www.googleapis.com/oauth2/v3/userinfo", body={"sub": "sub-a",
                                                                                "email": "user@example.com"})
        http.route("GET", "https://graph.microsoft.com/v1.0/me", body={"id": "ms-subject-a",
                                                                       "mail": "user@example.com"})
        http.route("GET", "https://graph.microsoft.com/v1.0/me/drive/special/approot", 403, {})
        registry = ConnectorRegistry(edges={"google": GoogleOAuthProvider("cid", "csecret"),
                                            "microsoft": MicrosoftOAuthProvider("cid", "csecret")})
        drive = OneDriveAdapter(self.creds, "cid", "csecret", connector=registry.get("microsoft_onedrive"))
        connect_service = build_oauth_connect_service(
            self.db, SimpleNamespace(oauth_flows_collection="eval_oauth_flows"), connectors=registry,
            credentials=self.creds, cipher=AesGcmTestCipher(), digest_key=b"k" * 32,
            revocation=CredentialRevocationService(self.creds, registry.edges()),
            activations=build_connect_activations(credentials=self.creds, reconnect=self.reconnect,
                                                  todo_connector=registry.get("microsoft_todo"), task_queue=None,
                                                  read_drive_id=drive.read_drive_id),
            redirect_uri="https://service.example/auth/connect/callback")
        sessions = SessionService(secret_key=SECRET, access_token_ttl=3600, refresh_token_ttl=3600)
        app = Quart("test_oauth_reconnect")
        app.register_blueprint(create_oauth_blueprint(
            auth_service=Mock(), session_service=sessions, auth_registry=Mock(),
            auth_config=SimpleNamespace(oauth_redirect_uri="https://service.example/auth/callback"),
            connect_service=connect_service, connectors=registry))
        token = sessions.create_access_token(UserProfile(user_id=USER, account_id="acct-1"),
                                             BillingAccount(account_id="acct-1"))
        async with app.test_client() as client:
            client.set_cookie("localhost", "access_token", token)
            start = await client.get(f"/auth/connect/{kind}")
            state = parse_qs(urlparse(start.headers["Location"]).query)["state"][0]
            return await client.get(f"/auth/connect/callback?state={state}&code=c")

    async def connect(self, provider: str) -> None:
        await self.creds.save(OAuthCredential(
            user_id=USER, account_id="acct-1", kind=provider, integration=ConnectorRegistry().get(provider).integration,
            account=ProviderAccount(realm="microsoft" if provider.startswith("microsoft") else "google",
                                    subject="sub-a", label="user@example.com"),
            access_token="at", refresh_token="rt", token_expiry=NOW, scopes=[]))

    async def gmail_state(self) -> None:
        await self.email.update_indexing_state(IndexingState(user_id=USER, provider="gmail"))


@pytest.fixture
def world():
    return World()


async def _get(app: Quart, path: str) -> Dict:
    async with app.test_client() as client:
        resp = await client.get(path, headers=AUTH)
    assert resp.status_code == 200
    return await resp.get_json()


# ----------------------------------------------------------------------------- status

async def test_gmail_with_a_past_connection_and_no_credential_answers_reconnect(world):
    await world.gmail_state()
    assert await _get(world.cabinet(), "/api/gmail/status") == {"connected": False, "reconnect_needed": True}


async def test_gmail_never_connected_answers_no_reconnect(world):
    assert await _get(world.cabinet(), "/api/gmail/status") == {"connected": False, "reconnect_needed": False}


async def test_drive_with_a_sync_state_and_no_grant_answers_reconnect(world):
    await world.drive_states.save_sync_state(DriveSyncState(user_id=USER))
    body = await _get(world.cabinet(), "/api/drive/status")
    assert (body["connected"], body["reconnect_needed"]) == (False, True)


async def test_a_connected_drive_carries_no_reconnect_needed(world):
    await world.drive_states.save_sync_state(DriveSyncState(user_id=USER))
    world.drive.state = DriveConnection(connected=True, drive_id="d1")
    body = await _get(world.cabinet(), "/api/drive/status")
    assert body["connected"] is True and "reconnect_needed" not in body


async def test_to_do_with_a_primary_list_and_no_credential_answers_reconnect(world):
    await world.tasks.save_config(USER, TaskUserConfig(primary_list_id="list-1"))
    body = await _get(world.cabinet(), "/api/tasks/microsoft/status")
    assert (body["connected"], body["reconnect_needed"]) == (False, True)


async def test_to_do_with_a_reset_config_answers_no_reconnect(world):
    await world.tasks.save_config(USER, TaskUserConfig())
    body = await _get(world.cabinet(), "/api/tasks/microsoft/status")
    assert (body["connected"], body["reconnect_needed"]) == (False, False)


async def test_a_failing_reconnect_read_never_fails_the_status_card(world):
    world.reconnect.status = AsyncMock(side_effect=RuntimeError("firestore down"))
    assert await _get(world.cabinet(), "/api/gmail/status") == {"connected": False, "reconnect_needed": False}


# ----------------------------------------------------------------------------- mark on disconnect

async def test_the_gmail_disconnect_marks_the_state_and_then_shows_no_reconnect(world):
    await world.gmail_state()
    await world.connect("gmail")
    async with world.cabinet().test_client() as client:
        resp = await client.delete("/api/gmail/disconnect", headers=AUTH)
    assert resp.status_code == 200
    assert world.db.collection(CREDS).docs == {}
    assert (await world.email.get_indexing_state(USER, "gmail")).disconnected_by_user_at is not None
    assert await _get(world.cabinet(), "/api/gmail/status") == {"connected": False, "reconnect_needed": False}


async def test_a_failed_mark_deletes_nothing(world):
    await world.gmail_state()
    await world.connect("gmail")
    world.reconnect.mark_disconnected = AsyncMock(side_effect=RuntimeError("firestore down"))
    async with world.cabinet().test_client() as client:
        resp = await client.delete("/api/gmail/disconnect", headers=AUTH)
    assert resp.status_code == 500
    assert await has_kind(world.creds, USER, "gmail") is True


async def test_the_drive_disconnect_marks_the_sync_state(world):
    await world.drive_states.save_sync_state(DriveSyncState(user_id=USER))
    world.drive.state = DriveConnection(connected=True, drive_id="d1")
    async with world.cabinet().test_client() as client:
        resp = await client.delete("/api/drive/disconnect", headers=AUTH)
    assert resp.status_code == 200 and world.drive.disconnected == [USER]
    assert (await world.drive_states.get_sync_state(USER)).disconnected_by_user_at == NOW
    body = await _get(world.cabinet(), "/api/drive/status")
    assert (body["connected"], body["reconnect_needed"]) == (False, False)


# ----------------------------------------------------------------------------- clear on consent

async def test_the_gmail_consent_clears_the_mark_and_the_flag(world, monkeypatch):
    await world.gmail_state()
    await world.email.set_reconnect_flags(USER, "gmail", reconnect_notified=True, disconnected_by_user_at=NOW)
    resp = await world.consent(monkeypatch, "gmail")
    assert resp.headers["Location"] == "/cabinet?connected=gmail"
    state = await world.email.get_indexing_state(USER, "gmail")
    assert (state.reconnect_notified, state.disconnected_by_user_at) == (False, None)


async def test_a_failed_clear_never_fails_the_connect(world, monkeypatch):
    world.reconnect.consented = AsyncMock(side_effect=RuntimeError("firestore down"))
    resp = await world.consent(monkeypatch, "gmail")
    assert resp.headers["Location"] == "/cabinet?connected=gmail"
    assert await has_kind(world.creds, USER, "gmail") is True


async def test_the_drive_consent_clears_the_mark(world, monkeypatch):
    await world.drive_states.save_sync_state(DriveSyncState(user_id=USER))
    await world.drive_states.update_sync_flags(USER, disconnected_by_user_at=NOW)
    resp = await world.consent(monkeypatch, "microsoft_onedrive")
    assert resp.headers["Location"] == "/cabinet?connected=microsoft_onedrive"
    assert (await world.drive_states.get_sync_state(USER)).disconnected_by_user_at is None


async def test_the_to_do_consent_clears_the_flag(world, monkeypatch):
    await world.tasks.save_config(USER, TaskUserConfig(primary_list_id="list-1", reconnect_notified=True))
    resp = await world.consent(monkeypatch, "microsoft_todo")
    assert resp.headers["Location"] == "/cabinet?connected=microsoft_todo"
    got = await world.tasks.get_config(USER)
    assert (got.primary_list_id, got.reconnect_notified) == ("list-1", False)
