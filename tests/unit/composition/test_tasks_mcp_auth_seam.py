"""The tasks resource beside the facts and drive resources (EXTERNAL_TASK_QUEUE_RFC §5.4): a facts or a drive access
token is `401` at `/tasks-mcp`, and a worker key is `401` at `/mcp` and `/drive-mcp`; each verifier knows only its own
credentials.

The facts app (the authorization server) and the drive app are built as `tests/unit/composition/test_drive_mcp_auth_seam.py`
builds them; the tasks app is `tests/support/task_mcp_world.py`. Real tokens come from the full OAuth flow.
"""
import base64
import contextlib
import hashlib
import secrets
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from src.composition.drive_mcp_setup import build_drive_mcp_app
from src.composition.mcp_setup import build_mcp_components
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb
from tests.support.mcp_gate_fakes import allowing_repos
from tests.support.task_mcp_world import World as TasksWorld

BASE = "https://mcp.example.test"
FACTS = f"{BASE}/mcp"
DRIVE = f"{BASE}/drive-mcp"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
ENV = SimpleNamespace(mcp_oauth_clients_collection="c", mcp_auth_codes_collection="a", mcp_refresh_tokens_collection="r")
AUTH_CONFIG = SimpleNamespace(
    mcp_resource_uri=FACTS, oauth_session_secret="seam-jwt-secret-0123456789abcdef-xyz", mcp_access_token_ttl=3600,
    mcp_refresh_token_ttl=2592000, mcp_auth_code_ttl=600, mcp_consent_request_ttl=600)
TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
ACCEPT = {"Accept": "application/json, text/event-stream"}


class Seam:
    def __init__(self) -> None:
        users, whitelist = allowing_repos("u1")
        self.components = build_mcp_components(
            db_client=EvalDb(), env_config=ENV, auth_config=AUTH_CONFIG, memory_search=MagicMock(),
            cipher=AesGcmTestCipher(), digest_key=bytes(range(32)), user_repo=users, whitelist_repo=whitelist)
        self.service = self.components.authorization_service
        self.drive = build_drive_mcp_app(authorization_service=self.service, auth_config=AUTH_CONFIG)
        self.facts_http = self._client(self.components.fastmcp)
        self.drive_http = self._client(self.drive)

    @staticmethod
    def _client(app) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app.streamable_http_app(), raise_app_exceptions=False),
                                 base_url=BASE)

    async def access_token(self, scope: str, resource: str) -> str:
        client = (await self.facts_http.post("/register", json={"redirect_uris": [REDIRECT]})).json()
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        auth = await self.facts_http.get("/authorize", params={
            "client_id": client["client_id"], "redirect_uri": REDIRECT, "response_type": "code", "scope": scope,
            "resource": resource, "code_challenge": challenge, "code_challenge_method": "S256", "state": "s"})
        req = auth.headers["location"].split("req=", 1)[1]
        code, _ = await self.service.issue_auth_code_for_consent(req, user_id="u1", account_id="acc1")
        resp = await self.facts_http.post("/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": client["client_id"],
            "client_secret": client["client_secret"], "code_verifier": verifier})
        assert resp.status_code == 200, resp.text
        return resp.json()["access_token"]

    @contextlib.asynccontextmanager
    async def running(self):
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(self.components.fastmcp.session_manager.run())
            await stack.enter_async_context(self.drive.session_manager.run())
            yield self


@pytest.fixture
async def seam():
    s = Seam()
    yield s
    await s.facts_http.aclose()
    await s.drive_http.aclose()


@pytest.fixture
async def tasks():
    w = await TasksWorld().seed()
    w.initial = await w.texts.texts()
    return w


@pytest.mark.slow
async def test_a_facts_or_a_drive_access_token_is_401_at_the_tasks_resource(seam, tasks):
    async with seam.running():
        facts = await seam.access_token("user_context", FACTS)
        drive = await seam.access_token("drive_read", DRIVE)
        assert (await seam.facts_http.post("/mcp", json=TOOLS_LIST,
                                           headers={**ACCEPT, "Authorization": f"Bearer {facts}"})).status_code == 200
    for token in (facts, drive):
        assert (await tasks.post(TOOLS_LIST, token)).status_code == 401
    assert (await tasks.post(TOOLS_LIST, tasks.key)).status_code == 200


async def test_a_worker_key_is_401_at_the_facts_and_drive_resources(seam, tasks):
    async with seam.running():
        for http, path in ((seam.facts_http, "/mcp"), (seam.drive_http, "/drive-mcp")):
            resp = await http.post(path, json=TOOLS_LIST, headers={**ACCEPT, "Authorization": f"Bearer {tasks.key}"})
            assert resp.status_code == 401, path
