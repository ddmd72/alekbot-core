"""The auth seam of the drive connector: one authorization server (the facts app), two protected resources.

Both ASGI apps run in-process: the facts app serves /register, /authorize, /token and /mcp; the drive app serves
/drive-mcp and its protected-resource metadata.
"""

import base64
import contextlib
import hashlib
import json
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

BASE = "https://mcp.example.test"
FACTS = f"{BASE}/mcp"
DRIVE = f"{BASE}/drive-mcp"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
ENV = SimpleNamespace(mcp_oauth_clients_collection="c", mcp_auth_codes_collection="a", mcp_refresh_tokens_collection="r")
AUTH_CONFIG = SimpleNamespace(
    mcp_resource_uri=FACTS, oauth_session_secret="seam-jwt-secret-0123456789abcdef-xyz", mcp_access_token_ttl=3600,
    mcp_refresh_token_ttl=2592000, mcp_auth_code_ttl=600, mcp_consent_request_ttl=600)
_TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


class World:
    def __init__(self) -> None:
        self.db = EvalDb()
        users, whitelist = allowing_repos("u1")
        self.components = build_mcp_components(
            db_client=self.db, env_config=ENV, auth_config=AUTH_CONFIG, search_enrichment_service=MagicMock(),
            cipher=AesGcmTestCipher(), digest_key=bytes(range(32)), user_repo=users, whitelist_repo=whitelist)
        self.service = self.components.authorization_service
        self.drive = build_drive_mcp_app(authorization_service=self.service, auth_config=AUTH_CONFIG)
        self.facts_http = self._client(self.components.fastmcp)
        self.drive_http = self._client(self.drive)

    @staticmethod
    def _client(app) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app.streamable_http_app(), raise_app_exceptions=False),
                                 base_url=BASE)

    async def register(self, **body) -> httpx.Response:
        return await self.facts_http.post("/register", json={"redirect_uris": [REDIRECT], **body})

    async def authorize(self, client: dict, **params) -> httpx.Response:
        self.verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=").decode()
        return await self.facts_http.get("/authorize", params={
            "client_id": client["client_id"], "redirect_uri": REDIRECT, "response_type": "code",
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "s", **params})

    async def tokens(self, *, scope=None, resource=None) -> dict:
        """The full flow for a fresh client; consent approved by user u1."""
        client = (await self.register()).json()
        params = {k: v for k, v in (("scope", scope), ("resource", resource)) if v}
        auth = await self.authorize(client, **params)
        assert auth.status_code == 302, auth.text
        req = auth.headers["location"].split("req=", 1)[1]
        code, _ = await self.service.issue_auth_code_for_consent(req, user_id="u1", account_id="acc1")
        resp = await self.facts_http.post("/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": client["client_id"],
            "client_secret": client["client_secret"], "code_verifier": self.verifier})
        assert resp.status_code == 200, resp.text
        self.client = client
        return resp.json()

    @contextlib.asynccontextmanager
    async def running(self):
        """Both session managers up, as the dispatcher's lifespan runs them (each can start once per instance)."""
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(self.components.fastmcp.session_manager.run())
            await stack.enter_async_context(self.drive.session_manager.run())
            yield self

    async def list_tools(self, http, path, token=None) -> httpx.Response:
        headers = dict(_MCP_HEADERS)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return await http.post(path, json=_TOOLS_LIST, headers=headers)


@pytest.fixture
async def world():
    w = World()
    yield w
    await w.facts_http.aclose()
    await w.drive_http.aclose()


def _tool_names(resp: httpx.Response) -> list:
    data = next(line[len("data: "):] for line in resp.text.splitlines() if line.startswith("data: ")) \
        if resp.headers["content-type"].startswith("text/event-stream") else resp.text
    return json.loads(data)["result"]["tools"]


async def test_registration_without_a_scope_registers_both_scopes(world):
    resp = await world.register()
    assert resp.status_code == 201
    assert set(resp.json()["scope"].split()) == {"user_context", "drive_read"}


async def test_registration_with_the_drive_scope_is_accepted(world):
    resp = await world.register(scope="drive_read")
    assert resp.status_code == 201 and resp.json()["scope"] == "drive_read"


async def test_registration_with_an_unknown_scope_is_400(world):
    assert (await world.register(scope="profile")).status_code == 400


async def test_the_drive_flow_with_resource_gives_a_token_good_on_drive_mcp_only(world):
    async with world.running():
        tokens = await world.tokens(scope="drive_read", resource=DRIVE)
        assert tokens["scope"] == "drive_read"
        on_drive = await world.list_tools(world.drive_http, "/drive-mcp", tokens["access_token"])
        on_facts = await world.list_tools(world.facts_http, "/mcp", tokens["access_token"])
        assert on_drive.status_code == 200
        assert on_facts.status_code == 401


async def test_the_drive_flow_with_only_the_drive_scope_gives_a_drive_token(world):
    async with world.running():
        tokens = await world.tokens(scope="drive_read")
        assert (await world.list_tools(world.drive_http, "/drive-mcp", tokens["access_token"])).status_code == 200


async def test_a_facts_token_is_401_on_drive_mcp(world):
    async with world.running():
        tokens = await world.tokens(scope="user_context", resource=FACTS)
        assert (await world.list_tools(world.drive_http, "/drive-mcp", tokens["access_token"])).status_code == 401
        assert (await world.list_tools(world.facts_http, "/mcp", tokens["access_token"])).status_code != 401


async def test_an_unknown_resource_at_authorize_redirects_with_an_error_and_leaves_no_consent_or_code(world):
    client = (await world.register()).json()
    auth = await world.authorize(client, scope="drive_read", resource="https://other.example.test/drive-mcp")
    assert auth.status_code == 302
    location = auth.headers["location"]
    assert location.startswith(REDIRECT) and "error=invalid_request" in location and "state=s" in location
    assert "req=" not in location
    assert world.db.collection("a").docs == {}


async def test_the_drive_prm_names_the_drive_resource_its_scope_and_the_one_issuer(world):
    resp = await world.drive_http.get("/.well-known/oauth-protected-resource/drive-mcp")
    assert resp.status_code == 200
    prm = resp.json()
    assert prm["resource"] == DRIVE
    assert prm["scopes_supported"] == ["drive_read"]
    assert [s.rstrip("/") for s in prm["authorization_servers"]] == [BASE]


async def test_the_facts_prm_is_unchanged(world):
    prm = (await world.facts_http.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert prm["resource"] == FACTS
    assert prm["scopes_supported"] == ["user_context"]


async def test_a_drive_401_points_to_the_drive_prm(world):
    async with world.running():
        resp = await world.list_tools(world.drive_http, "/drive-mcp")
        assert resp.status_code == 401
        assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/drive-mcp"' in resp.headers["www-authenticate"]


async def test_a_drive_refresh_keeps_the_drive_audience_and_scope(world):
    async with world.running():
        tokens = await world.tokens(scope="drive_read", resource=DRIVE)
        resp = await world.facts_http.post("/token", data={
            "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": world.client["client_id"],
            "client_secret": world.client["client_secret"]})
        assert resp.status_code == 200, resp.text
        refreshed = resp.json()
        assert refreshed["scope"] == "drive_read"
        assert (await world.list_tools(world.drive_http, "/drive-mcp", refreshed["access_token"])).status_code == 200
        assert (await world.list_tools(world.facts_http, "/mcp", refreshed["access_token"])).status_code == 401


async def test_a_drive_token_sent_to_the_facts_endpoint_is_refused(world):
    tokens = await world.tokens(scope="drive_read", resource=DRIVE)
    async with world.running():
        resp = await world.list_tools(world.facts_http, "/mcp", tokens["access_token"])
    assert resp.status_code in (401, 403)


async def test_a_refresh_asking_for_a_wider_scope_is_refused(world):
    tokens = await world.tokens(scope="drive_read", resource=DRIVE)
    resp = await world.facts_http.post("/token", data={
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": world.client["client_id"],
        "client_secret": world.client["client_secret"], "scope": "drive_read user_context"})
    assert resp.status_code == 400
    resp = await world.facts_http.post("/token", data={
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": world.client["client_id"],
        "client_secret": world.client["client_secret"], "scope": "user_context"})
    assert resp.status_code == 400


async def test_a_user_context_only_client_cannot_get_a_drive_token_by_sending_only_the_resource(world):
    client = (await world.register(scope="user_context")).json()
    auth = await world.authorize(client, resource=DRIVE)
    assert auth.status_code == 302
    assert "error=invalid_scope" in auth.headers["location"] and "req=" not in auth.headers["location"]
    assert world.db.collection("a").docs == {}
    ok = await world.authorize(client, resource=FACTS)
    assert "req=" in ok.headers["location"]


async def test_the_stub_tool_answers_through_the_drive_endpoint(world):
    tokens = await world.tokens(scope="drive_read")
    async with world.running():
        resp = await world.drive_http.post(
            "/drive-mcp", headers={**_MCP_HEADERS, "Authorization": f"Bearer {tokens['access_token']}"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "drive_connector_status", "arguments": {}}})
    assert resp.status_code == 200
    assert "Connected. No drive tools yet." in resp.text
