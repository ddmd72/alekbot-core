"""The integration connect stack of slice 1a, real from the routes down to the stores (MULTI_DRIVE_RFC §5.2):
the oauth blueprint, the real `SessionService` (signed JWTs), `OAuthConnectService` built by composition
(`build_oauth_connect_service`, flows in `FirestoreEphemeralStore` over `EvalDb`), the real Google and Microsoft edges,
the real activations (`build_connect_activations`) with the real `OneDriveAdapter.read_drive_id`, the real
`GcpTaskQueue` over the recording Cloud Tasks client, `FirestoreCredentialStore` over `EvalDb` with the AES-GCM test
cipher. The HTTP boundary is `tests/support/http_boundary_fake.py`; R1's `consented` is a spec'd recorder.

Shared by the route tests and the activation tests; examples from the invented cast only."""
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import Mock, create_autospec
from urllib.parse import parse_qs, urlparse

from quart import Quart

from src.adapters.firestore_credential_store import FirestoreCredentialStore
from src.adapters.google.google_oauth_provider import GoogleOAuthProvider
from src.adapters.microsoft.microsoft_oauth_provider import MicrosoftOAuthProvider
from src.adapters.microsoft.onedrive_adapter import OneDriveAdapter
from src.composition.connector_registry import ConnectorRegistry
from src.composition.oauth_connect_setup import build_connect_activations, build_oauth_connect_service
from src.domain.billing import BillingAccount
from src.domain.user import UserProfile
from src.services.credential_revocation_service import CredentialRevocationService
from src.services.integration_reconnect_service import IntegrationReconnectService
from src.services.session_service import SessionService
from src.web.oauth_app import create_oauth_blueprint
from tests.support import http_boundary_fake
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb
from tests.support.recording_task_queue import make_task_queue

ALEX, CLARA = "user-alex", "user-clara"
SECRET = "session-secret-for-tests-0123456789abcdef"
REDIRECT = "https://service.example/auth/connect/callback"
CREDS, FLOWS = "eval_credentials", "eval_oauth_flows"

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
USERINFO = "https://www.googleapis.com/oauth2/v3/userinfo"
MS_AUTH = "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"
MS_TOKEN = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
GRAPH_ME = "https://graph.microsoft.com/v1.0/me"
APPROOT = "https://graph.microsoft.com/v1.0/me/drive/special/approot"

GRANTED = {
    "gmail": "https://www.googleapis.com/auth/gmail.readonly openid email",
    "google_tasks": "https://www.googleapis.com/auth/tasks openid email",
    "microsoft_todo": "Tasks.ReadWrite offline_access User.Read",
    "microsoft_onedrive": "Files.ReadWrite.AppFolder offline_access User.Read",
}


class ConnectWorld:
    def __init__(self, monkeypatch: Any, *, microsoft: bool = True, wired: bool = True) -> None:
        self.http = http_boundary_fake.install(monkeypatch)
        self.db = EvalDb()
        self.store = FirestoreCredentialStore(self.db, SimpleNamespace(credentials_collection=CREDS),
                                              cipher=AesGcmTestCipher())
        self.sessions = SessionService(secret_key=SECRET, access_token_ttl=3600, refresh_token_ttl=3600)
        edges: Dict[str, Any] = {"google": GoogleOAuthProvider("g-cid", "g-csecret")}
        if microsoft:
            edges["microsoft"] = MicrosoftOAuthProvider("ms-cid", "ms-csecret")
        self.registry = ConnectorRegistry(edges=edges)
        self.events: List[Tuple[str, ...]] = []
        self.reconnect = create_autospec(IntegrationReconnectService, instance=True)

        async def consented(user_id: str, integration: Any) -> None:
            self.events.append(("consented", user_id, integration.value))

        self.reconnect.consented.side_effect = consented
        self.queue, self.tasks_client = make_task_queue()
        drive = OneDriveAdapter(self.store, "ms-cid", "ms-csecret", connector=self.registry.get("microsoft_onedrive"))
        self.revocation = CredentialRevocationService(self.store, self.registry.edges())
        self.connect_service = build_oauth_connect_service(
            self.db, SimpleNamespace(oauth_flows_collection=FLOWS), connectors=self.registry, credentials=self.store,
            cipher=AesGcmTestCipher(), digest_key=b"k" * 32, revocation=self.revocation,
            activations=build_connect_activations(
                credentials=self.store, reconnect=self.reconnect,
                todo_connector=self.registry.get("microsoft_todo"), task_queue=self.queue,
                read_drive_id=drive.read_drive_id),
            redirect_uri=REDIRECT)
        self.app = Quart("connect_world")
        self.app.register_blueprint(create_oauth_blueprint(
            auth_service=Mock(), session_service=self.sessions, auth_registry=Mock(),
            auth_config=SimpleNamespace(oauth_redirect_uri="https://service.example/auth/callback"),
            connect_service=self.connect_service if wired else None, connectors=self.registry if wired else None))
        self.http.route("POST", GOOGLE_TOKEN, handler=lambda r: (200, self._token(r)))
        self.http.route("POST", MS_TOKEN, handler=lambda r: (200, self._token(r)))
        self.http.route("GET", APPROOT, body={"id": "approot-1", "parentReference": {"driveId": "drive-b1"}})
        self.signs_in_as("sub-1")
        self.granted: Optional[str] = None      # None: the kind's full grant (GRANTED)
        self._flow_kind = "gmail"
        self.responses: List[Any] = []

    # ---------------------------------------------------------------- the provider side

    def _token(self, request: Any) -> dict:
        kind = self._flow_kind
        return {"access_token": f"at-{kind}", "refresh_token": f"rt-{kind}", "expires_in": 3600,
                "scope": self.granted if self.granted is not None else GRANTED[kind]}

    def signs_in_as(self, subject: str) -> None:
        """The provider account the next consent signs in with (both realms)."""
        self.http.route("GET", USERINFO, body={"sub": subject, "email": "alex@example.com"})
        self.http.route("GET", GRAPH_ME, body={"id": subject, "mail": "alex@example.com"})

    # ---------------------------------------------------------------- the browser side

    def session(self, user_id: str = ALEX, account_id: str = "acct-1") -> str:
        return self.sessions.create_access_token(UserProfile(user_id=user_id, account_id=account_id),
                                                 BillingAccount(account_id=account_id))

    async def get(self, path: str, *, token: Optional[str] = "default") -> Any:
        async with self.app.test_client() as client:
            token = self.session() if token == "default" else token
            if token is not None:
                client.set_cookie("localhost", "access_token", token)
            resp = await client.get(path)
        self.responses.append(resp)
        return resp

    async def start(self, kind: str, *, query: str = "", token: Optional[str] = "default") -> Tuple[Any, str]:
        """GET /auth/connect/<kind>; returns the response and the state the consent URL carries ("" without one)."""
        self._flow_kind = kind
        resp = await self.get(f"/auth/connect/{kind}{query}", token=token)
        location = resp.headers.get("Location", "")
        state = parse_qs(urlparse(location).query).get("state", [""])[0]
        return resp, state

    async def callback(self, state: str, *, code: str = "code-1", token: Optional[str] = "default") -> Any:
        return await self.get(f"/auth/connect/callback?state={state}&code={code}", token=token)

    async def connect(self, kind: str, *, token: Optional[str] = "default") -> Any:
        _, state = await self.start(kind, token=token)
        return await self.callback(state, token=token)

    # ---------------------------------------------------------------- what is stored

    @property
    def flows(self) -> dict:
        return self.db.collection(FLOWS).docs

    async def held(self, kind: str, user_id: str = ALEX) -> list:
        return await self.store.list_for_user(user_id, kind)

    def worker_tasks(self) -> list:
        return [(task_type, body.get("payload", body)) for task_type, body in self.tasks_client.worker_tasks()]

    def set_cookies(self) -> list:
        return [value for resp in self.responses for value in resp.headers.getlist("Set-Cookie")]
