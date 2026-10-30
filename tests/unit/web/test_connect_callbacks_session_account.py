"""The four connect callbacks on the re-keyed credential port (MULTI_DRIVE_RFC §4.2, slice 1a):

- the saved credential's `kind` and `integration` come from the registry spec, its `account_id` from the signed-in
  session (the verified `access_token` cookie of the same user); without that session nothing is exchanged or stored;
- every kind is single-credential in slice 1a (`multiple` False): another account's consent replaces the user's record
  of that kind, the same account's keeps its credential_id, and the user's other kinds and other users stay.

Real: the oauth blueprint, `GoogleOAuthService`, the real `SessionService` (signed JWTs), `FirestoreCredentialStore`
over `EvalDb` with the AES-GCM test cipher. The HTTP boundary is `tests/support/http_boundary_fake.py`.
"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from quart import Quart

from src.adapters.firestore_credential_store import FirestoreCredentialStore
from src.composition.connector_registry import ConnectorRegistry
from src.domain.billing import BillingAccount
from src.domain.integration import Integration
from src.domain.user import UserProfile
from src.services.google_oauth_service import GMAIL_SCOPES, GOOGLE_TASKS_SCOPES, GoogleOAuthService
from src.services.session_service import SessionService
from src.web.oauth_app import create_oauth_blueprint
from tests.support import http_boundary_fake
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb
from tests.unit.web.test_consent_identity import (
    APPROOT, CALLBACKS, GOOGLE_GRANTED, GOOGLE_TOKEN, GOOGLE_TOKEN_BODY, GRAPH_ME, MS_TOKEN, MS_TOKEN_BODY, USERINFO,
)

ALEX, CLARA = "user-alex", "user-clara"
SECRET = "session-secret-for-tests-0123456789abcdef"
INTEGRATION = {"gmail": Integration.MAIL, "google_tasks": Integration.TASKS, "todo": Integration.TASKS,
               "onedrive": Integration.DRIVE}


class World:
    def __init__(self, monkeypatch) -> None:
        self.http = http_boundary_fake.install(monkeypatch)
        self.db = EvalDb()
        self.store = FirestoreCredentialStore(self.db, SimpleNamespace(credentials_collection="eval_credentials"),
                                              cipher=AesGcmTestCipher())
        self.sessions = SessionService(secret_key=SECRET, access_token_ttl=3600, refresh_token_ttl=3600)
        auth_config = SimpleNamespace(
            oauth_redirect_uri="https://example.test/auth/callback",
            gmail_oauth_redirect_uri="https://example.test/auth/connect-gmail/callback",
            google_tasks_oauth_redirect_uri="https://example.test/auth/connect-google-tasks/callback")
        self.app = Quart("test_connect_callbacks_session_account")
        self.app.register_blueprint(create_oauth_blueprint(
            auth_service=Mock(), session_service=self.sessions, auth_registry=Mock(), auth_config=auth_config,
            gmail_oauth_service=GoogleOAuthService("cid", "csecret", scopes=GMAIL_SCOPES),
            google_tasks_oauth_service=GoogleOAuthService("cid", "csecret", scopes=GOOGLE_TASKS_SCOPES),
            oauth_credentials_port=self.store,
            ms_todo_client_id="ms-cid", ms_todo_client_secret="ms-csecret",
            ms_todo_redirect_uri="https://example.test/auth/connect-microsoft-todo/callback",
            onedrive_redirect_uri="https://example.test/auth/connect-onedrive/callback",
            connectors=ConnectorRegistry(),
        ))
        self.http.route("POST", GOOGLE_TOKEN, handler=lambda r: (200, {**GOOGLE_TOKEN_BODY, **(
            {"scope": GOOGLE_GRANTED[r.data["redirect_uri"]]} if r.data["redirect_uri"] in GOOGLE_GRANTED else {})}))
        self.http.route("POST", MS_TOKEN, body=MS_TOKEN_BODY)
        self.account("sub-1")
        self.http.route("GET", APPROOT, body={"id": "R!1", "parentReference": {"driveId": "d1"}})

    def account(self, subject: str) -> None:
        """The provider account the next consent signs in with (both realms)."""
        self.http.route("GET", USERINFO, body={"sub": subject, "email": "alex@example.com"})
        self.http.route("GET", GRAPH_ME, body={"id": subject, "mail": "alex@example.com"})

    def session(self, user_id: str, account_id: str) -> str:
        return self.sessions.create_access_token(UserProfile(user_id=user_id, account_id=account_id),
                                                 BillingAccount(account_id=account_id))

    @property
    def docs(self) -> dict:
        return self.db.collection("eval_credentials").docs

    def posts(self) -> int:
        return len(self.http.requests_to("POST", GOOGLE_TOKEN)) + len(self.http.requests_to("POST", MS_TOKEN))

    async def callback(self, which: str, *, user: str = ALEX, session: object = "default"):
        path, state_cookie, user_cookie = CALLBACKS[which][:3]
        async with self.app.test_client() as client:
            client.set_cookie("localhost", state_cookie, "st-1")
            client.set_cookie("localhost", user_cookie, user)
            token = self.session(user, "acct-1") if session == "default" else session
            if token is not None:
                client.set_cookie("localhost", "access_token", token)
            return await client.get(f"{path}?code=c-1&state=st-1")


@pytest.fixture
def world(monkeypatch) -> World:
    return World(monkeypatch)


@pytest.mark.parametrize("which", list(CALLBACKS))
async def test_the_credential_carries_the_spec_and_the_sessions_account(world, which):
    resp = await world.callback(which)
    assert resp.headers["Location"] == CALLBACKS[which][4]
    (cred,) = await world.store.list_for_user(ALEX)
    assert (cred.kind, cred.integration, cred.account_id) == (CALLBACKS[which][3], INTEGRATION[which], "acct-1")
    (doc,) = world.docs.values()
    assert (doc["kind"], doc["integration"], doc["account_id"]) == (
        CALLBACKS[which][3], INTEGRATION[which].value, "acct-1")


@pytest.mark.parametrize("which", list(CALLBACKS))
@pytest.mark.parametrize("session", [None, "not-a-jwt"], ids=["no-session", "invalid-session"])
async def test_no_signed_in_session_stores_nothing_and_exchanges_nothing(world, which, session):
    resp = await world.callback(which, session=session)
    assert resp.headers["Location"] == CALLBACKS[which][5] + "state"
    assert world.docs == {} and world.posts() == 0


@pytest.mark.parametrize("which", list(CALLBACKS))
async def test_another_users_session_stores_nothing(world, which):
    resp = await world.callback(which, user=ALEX, session=world.session(CLARA, "acct-2"))
    assert resp.headers["Location"] == CALLBACKS[which][5] + "state"
    assert world.docs == {} and world.posts() == 0


@pytest.mark.parametrize("which", list(CALLBACKS))
async def test_another_accounts_consent_replaces_the_record_of_that_kind(world, which):
    kind = CALLBACKS[which][3]
    other = "microsoft_todo" if kind != "microsoft_todo" else "gmail"
    await world.callback("todo" if other == "microsoft_todo" else "gmail")      # another kind of the user
    await world.callback(which, user=CLARA, session=world.session(CLARA, "acct-2"))   # another user, same kind
    await world.callback(which)
    (first,) = await world.store.list_for_user(ALEX, kind)
    world.account("sub-2")
    resp = await world.callback(which)
    assert resp.headers["Location"] == CALLBACKS[which][4]
    (now,) = await world.store.list_for_user(ALEX, kind)
    assert now.account.subject == "sub-2" and now.credential_id != first.credential_id
    assert len(await world.store.list_for_user(ALEX, other)) == 1
    assert len(await world.store.list_for_user(CLARA, kind)) == 1


@pytest.mark.parametrize("which", list(CALLBACKS))
async def test_the_same_accounts_consent_keeps_the_credential_id(world, which):
    await world.callback(which)
    (first,) = await world.store.list_for_user(ALEX)
    await world.callback(which)
    (again,) = await world.store.list_for_user(ALEX)
    assert again.credential_id == first.credential_id
