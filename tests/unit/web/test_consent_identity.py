"""Identity at every consent edge (SECRETS_AT_REST_RFC §2.3, §8 I9): each of the four connect callbacks stores the
account subject and label read at consent, and a failed or incomplete identity read stores nothing.

Real: the oauth blueprint, `GoogleOAuthService`, `FirestoreCredentialStore` over `EvalDb` with the AES-GCM test cipher.
The HTTP boundary is `tests/support/http_boundary_fake.py` (token endpoints, Google userinfo, Graph `/me` and approot).
"""
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import pytest
from quart import Quart

from src.adapters.firestore_credential_store import FirestoreCredentialStore
from src.services.google_oauth_service import GMAIL_SCOPES, GOOGLE_TASKS_SCOPES, GoogleOAuthService
from src.web.oauth_app import create_oauth_blueprint
from tests.support import http_boundary_fake
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb
from tests.support.oauth_credentials_store import one
from src.composition.connector_registry import ConnectorRegistry

GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
USERINFO = "https://www.googleapis.com/oauth2/v3/userinfo"
MS_TOKEN = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
GRAPH_ME = "https://graph.microsoft.com/v1.0/me"
APPROOT = "https://graph.microsoft.com/v1.0/me/drive/special/approot"
USER = "user-a"

GOOGLE_TOKEN_BODY = {"access_token": "at-google", "refresh_token": "rt-google", "expires_in": 3600}
# Google's token endpoint always lists the GRANTED scopes (space-delimited; `email` comes back as userinfo.email);
# each consent is granted its own service scope, told apart by the posted redirect_uri.
GOOGLE_GRANTED = {
    "https://example.test/auth/connect-gmail/callback":
        "https://www.googleapis.com/auth/gmail.readonly openid https://www.googleapis.com/auth/userinfo.email",
    "https://example.test/auth/connect-google-tasks/callback":
        "https://www.googleapis.com/auth/tasks openid https://www.googleapis.com/auth/userinfo.email",
}
MS_TOKEN_BODY = {"access_token": "at-ms", "refresh_token": "rt-ms", "expires_in": 3600, "scope": "x offline_access"}

# (callback path, state cookie, user cookie, kind, success redirect, error prefix)
CALLBACKS = {
    "gmail": ("/auth/connect-gmail/callback", "gmail_oauth_state", "gmail_connect_user_id", "gmail",
              "/cabinet?gmail_connected=1", "/cabinet?gmail_error="),
    "google_tasks": ("/auth/connect-google-tasks/callback", "tasks_oauth_state", "tasks_connect_user_id",
                     "google_tasks", "/cabinet?tasks_connected=1", "/cabinet?tasks_error="),
    "todo": ("/auth/connect-microsoft-todo/callback", "microsoft_todo_oauth_state", "microsoft_todo_connect_user_id",
             "microsoft_todo", "/cabinet?microsoft_todo_connected=1", "/cabinet?microsoft_todo_error="),
    "onedrive": ("/auth/connect-onedrive/callback", "drive_oauth_state", "drive_connect_user_id",
                 "microsoft_onedrive", "/cabinet?drive_connected=1", "/cabinet?drive_error="),
}
GOOGLE = ("gmail", "google_tasks")
MICROSOFT = ("todo", "onedrive")


class World:
    def __init__(self, monkeypatch) -> None:
        self.http = http_boundary_fake.install(monkeypatch)
        self.db = EvalDb()
        self.store = FirestoreCredentialStore(self.db, SimpleNamespace(credentials_collection="eval_credentials"),
                                              cipher=AesGcmTestCipher())
        session_service = Mock()
        session_service.verify_access_token = Mock(return_value={"sub": USER})
        auth_config = SimpleNamespace(
            oauth_redirect_uri="https://example.test/auth/callback",
            gmail_oauth_redirect_uri="https://example.test/auth/connect-gmail/callback",
            google_tasks_oauth_redirect_uri="https://example.test/auth/connect-google-tasks/callback")
        self.app = Quart("test_consent_identity")
        self.app.register_blueprint(create_oauth_blueprint(
            auth_service=Mock(), session_service=session_service, auth_registry=Mock(), auth_config=auth_config,
            gmail_oauth_service=GoogleOAuthService("cid", "csecret", scopes=GMAIL_SCOPES),
            google_tasks_oauth_service=GoogleOAuthService("cid", "csecret", scopes=GOOGLE_TASKS_SCOPES),
            oauth_credentials_port=self.store,
            ms_todo_client_id="ms-cid", ms_todo_client_secret="ms-csecret",
            ms_todo_redirect_uri="https://example.test/auth/connect-microsoft-todo/callback",
            onedrive_redirect_uri="https://example.test/auth/connect-onedrive/callback", connectors=ConnectorRegistry(),
        ))
        # an unknown redirect_uri gets the bare GOOGLE_TOKEN_BODY (no `scope`), which test_consent_scope_check relies on
        self.http.route("POST", GOOGLE_TOKEN, handler=lambda r: (200, {**GOOGLE_TOKEN_BODY, **(
            {"scope": GOOGLE_GRANTED[r.data["redirect_uri"]]} if r.data["redirect_uri"] in GOOGLE_GRANTED else {})}))
        self.http.route("POST", MS_TOKEN, body=MS_TOKEN_BODY)
        self.http.route("GET", USERINFO, body={"sub": "sub-g", "email": "user@example.com"})
        self.http.route("GET", GRAPH_ME, body={"id": "sub-m", "mail": "user@example.test"})
        self.http.route("GET", APPROOT, body={"id": "R!1", "parentReference": {"driveId": "d1"}})

    @property
    def docs(self) -> dict:
        return self.db.collection("eval_credentials").docs

    async def callback(self, which: str):
        path, state_cookie, user_cookie = CALLBACKS[which][:3]
        async with self.app.test_client() as client:
            client.set_cookie("localhost", state_cookie, "st-1")
            client.set_cookie("localhost", user_cookie, USER)
            return await client.get(f"{path}?code=c-1&state=st-1")

    async def authorize(self, path: str) -> dict:
        async with self.app.test_client() as client:
            client.set_cookie("localhost", "access_token", "session-cookie")
            resp = await client.get(path)
        return parse_qs(urlparse(resp.headers["Location"]).query)


@pytest.fixture
def world(monkeypatch) -> World:
    return World(monkeypatch)


@pytest.mark.parametrize("which,realm,subject,label", [
    ("gmail", "google", "sub-g", "user@example.com"),
    ("google_tasks", "google", "sub-g", "user@example.com"),
    ("todo", "microsoft", "sub-m", "user@example.test"),
    ("onedrive", "microsoft", "sub-m", "user@example.test"),
])
async def test_each_consent_stores_the_account_subject_and_label(world, which, realm, subject, label):
    resp = await world.callback(which)
    assert resp.headers["Location"] == CALLBACKS[which][4]
    got = await one(world.store, USER, CALLBACKS[which][3])
    assert (got.account.realm, got.account.subject, got.account.label) == (realm, subject, label)
    (doc,) = world.docs.values()
    assert doc["account"] == {"realm": realm, "subject": subject}
    assert world.http.unrouted == []


async def test_the_onedrive_consent_still_stores_the_drive_id(world):
    await world.callback("onedrive")
    assert (await one(world.store, USER, "microsoft_onedrive")).drive_id == "d1"


async def test_a_microsoft_account_without_mail_takes_the_principal_name(world):
    world.http.route("GET", GRAPH_ME, body={"id": "sub-m", "mail": None, "userPrincipalName": "user@example.net"})
    await world.callback("todo")
    assert (await one(world.store, USER, "microsoft_todo")).account.label == "user@example.net"


@pytest.mark.parametrize("which", GOOGLE)
@pytest.mark.parametrize("status,body", [
    (401, {"error": "invalid_token"}),
    (200, {"email": "user@example.com"}),
    (200, {"sub": "sub-g"}),
    (200, {"sub": "", "email": "user@example.com"}),
])
async def test_a_failed_google_identity_read_stores_nothing(world, which, status, body):
    world.http.route("GET", USERINFO, status=status, body=body)
    resp = await world.callback(which)
    assert resp.headers["Location"].startswith(CALLBACKS[which][5])
    assert world.docs == {}


@pytest.mark.parametrize("which", GOOGLE)
async def test_an_unreachable_userinfo_stores_nothing(world, which):
    world.http._routes = [r for r in world.http._routes if r.prefix != USERINFO]
    resp = await world.callback(which)
    assert resp.headers["Location"].startswith(CALLBACKS[which][5])
    assert world.docs == {}


@pytest.mark.parametrize("which", MICROSOFT)
@pytest.mark.parametrize("status,body", [
    (403, {"error": {"code": "Authorization_RequestDenied"}}),
    (200, {"mail": "user@example.test"}),
    (200, {"id": "", "mail": "user@example.test"}),
])
async def test_a_failed_microsoft_identity_read_stores_nothing(world, which, status, body):
    world.http.route("GET", GRAPH_ME, status=status, body=body)
    resp = await world.callback(which)
    assert resp.headers["Location"].startswith(CALLBACKS[which][5])
    assert world.docs == {}


async def test_a_failed_identity_read_keeps_an_existing_record(world):
    await world.callback("todo")
    before = {k: dict(v) for k, v in world.docs.items()}
    world.http.route("GET", GRAPH_ME, status=401, body={})
    await world.callback("todo")
    assert world.docs == before


async def test_a_failed_identity_read_does_not_enqueue_the_to_do_setup(world, monkeypatch):
    """The To Do callback enqueues its setup only after a stored credential (a blueprint with a queue)."""
    queue = Mock()
    enqueued = []

    async def enqueue(task_type, payload):
        enqueued.append(task_type)
    queue.enqueue_worker_task = enqueue
    app = Quart("test_consent_identity_queue")
    session_service = Mock()
    session_service.verify_access_token = Mock(return_value={"sub": USER})
    app.register_blueprint(create_oauth_blueprint(
        auth_service=Mock(), session_service=session_service, auth_registry=Mock(),
        auth_config=SimpleNamespace(oauth_redirect_uri="https://example.test/auth/callback"),
        oauth_credentials_port=world.store, ms_todo_client_id="ms-cid", ms_todo_client_secret="ms-csecret",
        ms_todo_redirect_uri="https://example.test/auth/connect-microsoft-todo/callback", task_queue=queue, connectors=ConnectorRegistry()))
    world.http.route("GET", GRAPH_ME, status=401, body={})
    async with app.test_client() as client:
        client.set_cookie("localhost", "microsoft_todo_oauth_state", "st-1")
        client.set_cookie("localhost", "microsoft_todo_connect_user_id", USER)
        await client.get("/auth/connect-microsoft-todo/callback?code=c-1&state=st-1")
    assert enqueued == [] and world.docs == {}


@pytest.mark.parametrize("path", ["/auth/connect-microsoft-todo", "/auth/connect-onedrive"])
async def test_both_microsoft_authorize_urls_request_user_read(world, path):
    scopes = (await world.authorize(path))["scope"][0].split()
    assert "User.Read" in scopes and "offline_access" in scopes


@pytest.mark.parametrize("path", ["/auth/connect-gmail", "/auth/connect-google-tasks"])
async def test_both_google_authorize_urls_request_openid_and_email(world, path):
    scopes = (await world.authorize(path))["scope"][0].split()
    assert {"openid", "email"} <= set(scopes)


async def test_the_onedrive_token_exchange_asks_the_same_scope(world):
    await world.callback("onedrive")
    (post,) = world.http.requests_to("POST", MS_TOKEN)
    assert "User.Read" in post.data["scope"].split()
