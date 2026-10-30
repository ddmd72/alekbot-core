"""R1 in the Cabinet (SECRETS_AT_REST_RFC §6.2, F1; plan W2.4): the three status routes answer `reconnect_needed` when an
integration reads "not connected" but its feature state shows a past connection; the Gmail and Drive disconnects mark
the feature state before the credential goes; a consent callback clears the mark and the notice flag.

The real `IntegrationReconnectService` runs over the real credential, indexing-state and To Do config stores on one
evaluating Firestore fake, and the in-memory drive index repository; the drive is a fake with a settable connection."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from quart import Quart

from src.adapters.firestore_indexed_email_repo import FirestoreIndexedEmailRepository
from src.adapters.firestore_credential_store import FirestoreCredentialStore
from src.adapters.firestore_task_config_repository import FirestoreTaskConfigRepository
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.composition.connector_registry import ConnectorRegistry
from src.domain.drive_index import DriveSyncState
from src.domain.email import IndexingState
from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from src.domain.task import TaskUserConfig
from src.domain.user_drive import DriveConnection
from src.services.integration_reconnect_service import IntegrationReconnectService
from src.services.task_setup_service import TaskSetupService
from src.web.oauth_app import create_oauth_blueprint
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb
from tests.support.oauth_credentials_store import has_kind

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
USER = "user-a"
NOW = datetime(2026, 10, 29, 9, 30, tzinfo=timezone.utc)
CREDS, FACTS, STATE, TASKS = "eval_credentials", "eval_email_facts", "eval_email_state", "eval_task_config"
AUTH = {"Authorization": "Bearer t"}


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
            task_setup=self.task_setup, user_drive=self.drive, reconnect=self.reconnect, connectors=ConnectorRegistry())
        app = Quart("test_cabinet_reconnect")
        app.register_blueprint(bp)
        return app

    def oauth(self, gmail_oauth_service=None) -> Quart:
        auth_config = Mock()
        auth_config.gmail_oauth_redirect_uri = "https://example.test/auth/connect-gmail/callback"
        app = Quart("test_oauth_reconnect")
        app.register_blueprint(create_oauth_blueprint(
            auth_service=Mock(), session_service=Mock(), auth_registry=Mock(), auth_config=auth_config,
            gmail_oauth_service=gmail_oauth_service, oauth_credentials_port=self.creds, ms_todo_client_id="cid",
            ms_todo_client_secret="csecret", onedrive_redirect_uri="https://example.test/auth/connect-onedrive/callback",
            ms_todo_redirect_uri="https://example.test/auth/connect-microsoft-todo/callback",
            reconnect=self.reconnect, connectors=ConnectorRegistry()))
        return app

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

async def test_the_gmail_consent_clears_the_mark_and_the_flag(world):
    await world.gmail_state()
    await world.email.set_reconnect_flags(USER, "gmail", reconnect_notified=True, disconnected_by_user_at=NOW)
    gmail = Mock()
    gmail.exchange_code = AsyncMock(return_value=OAuthCredential(
        user_id=USER,
        account_id="acct-1",
        kind="gmail",
        integration=Integration.MAIL,
        account=ProviderAccount(realm="google", subject="sub-a", label="user@example.com"),
        access_token="at",
        refresh_token="rt",
        token_expiry=NOW,
        scopes=[],
    ))
    async with world.oauth(gmail).test_client() as client:
        client.set_cookie("localhost", "gmail_oauth_state", "s1")
        client.set_cookie("localhost", "gmail_connect_user_id", USER)
        resp = await client.get("/auth/connect-gmail/callback?code=c&state=s1")
    assert resp.headers["Location"] == "/cabinet?gmail_connected=1"
    state = await world.email.get_indexing_state(USER, "gmail")
    assert (state.reconnect_notified, state.disconnected_by_user_at) == (False, None)


async def test_a_failed_clear_never_fails_the_connect(world):
    gmail = Mock()
    gmail.exchange_code = AsyncMock(return_value=OAuthCredential(
        user_id=USER,
        account_id="acct-1",
        kind="gmail",
        integration=Integration.MAIL,
        account=ProviderAccount(realm="google", subject="sub-a", label="user@example.com"),
        access_token="at",
        refresh_token="rt",
        token_expiry=NOW,
        scopes=[],
    ))
    world.reconnect.consented = AsyncMock(side_effect=RuntimeError("firestore down"))
    async with world.oauth(gmail).test_client() as client:
        client.set_cookie("localhost", "gmail_oauth_state", "s1")
        client.set_cookie("localhost", "gmail_connect_user_id", USER)
        resp = await client.get("/auth/connect-gmail/callback?code=c&state=s1")
    assert resp.headers["Location"] == "/cabinet?gmail_connected=1"
    assert await has_kind(world.creds, USER, "gmail") is True


def _aiohttp_session(token: Dict):
    resp = MagicMock()
    resp.status, resp.ok = 200, True
    resp.json = AsyncMock(return_value=token)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.post.return_value = resp
    me = MagicMock()        # the account identity read (Graph /me), required since SECRETS_AT_REST_RFC I9
    me.status, me.ok = 200, True
    me.json = AsyncMock(return_value={"id": "ms-subject-a", "mail": "user@example.com"})
    me.__aenter__ = AsyncMock(return_value=me)
    me.__aexit__ = AsyncMock(return_value=False)
    refused = MagicMock()   # the drive-id read after the token: refused, it is best effort
    refused.status, refused.ok = 403, False
    refused.__aenter__ = AsyncMock(return_value=refused)
    refused.__aexit__ = AsyncMock(return_value=False)
    session.get.side_effect = lambda url, **kw: me if url == "https://graph.microsoft.com/v1.0/me" else refused
    return session


async def test_the_drive_consent_clears_the_mark(world):
    await world.drive_states.save_sync_state(DriveSyncState(user_id=USER))
    await world.drive_states.update_sync_flags(USER, disconnected_by_user_at=NOW)
    token = {"access_token": "a", "refresh_token": "r", "expires_in": 3600, "scope": "Files.ReadWrite.AppFolder"}
    async with world.oauth().test_client() as client:
        client.set_cookie("localhost", "drive_oauth_state", "s1")
        client.set_cookie("localhost", "drive_connect_user_id", USER)
        with patch("aiohttp.ClientSession", return_value=_aiohttp_session(token)):
            resp = await client.get("/auth/connect-onedrive/callback?code=c&state=s1")
    assert resp.headers["Location"] == "/cabinet?drive_connected=1"
    assert (await world.drive_states.get_sync_state(USER)).disconnected_by_user_at is None


async def test_the_to_do_consent_clears_the_flag(world):
    await world.tasks.save_config(USER, TaskUserConfig(primary_list_id="list-1", reconnect_notified=True))
    token = {"access_token": "a", "refresh_token": "r", "expires_in": 3600, "scope": "Tasks.ReadWrite"}
    async with world.oauth().test_client() as client:
        client.set_cookie("localhost", "microsoft_todo_oauth_state", "s1")
        client.set_cookie("localhost", "microsoft_todo_connect_user_id", USER)
        with patch("aiohttp.ClientSession", return_value=_aiohttp_session(token)):
            resp = await client.get("/auth/connect-microsoft-todo/callback?code=c&state=s1")
    assert resp.headers["Location"] == "/cabinet?microsoft_todo_connected=1"
    got = await world.tasks.get_config(USER)
    assert (got.primary_list_id, got.reconnect_notified) == ("list-1", False)
