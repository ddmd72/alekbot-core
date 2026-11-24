"""Registering for the task queue's worker resource over the SDK routes main.py serves (EXTERNAL_TASK_QUEUE_RFC §5.4
"OAuth", §10 Q1): `external_tasks` is registrable but never a default scope, so a registration that names no scope and
an existing client never gain it; a client registered with it authorizes the tasks resource by its scope alone or by
the resource, and gets an access token for `/tasks-mcp` bound to the picked worker (`wid`)."""
import base64
import hashlib
import secrets
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest

from src.composition.mcp_setup import build_mcp_components
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb
from tests.support.mcp_gate_fakes import ALLOWED_EMAIL, FakeUserRepo, FakeWhitelistRepo

RESOURCE = "https://mcp.example.test/mcp"
TASKS = "https://mcp.example.test/tasks-mcp"
BASE = "https://mcp.example.test"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
SECRET = "tasks-registration-jwt-secret-0123456789abcdef"
ENV = SimpleNamespace(mcp_oauth_clients_collection="c", mcp_auth_codes_collection="a", mcp_refresh_tokens_collection="r")
AUTH = SimpleNamespace(mcp_resource_uri=RESOURCE, oauth_session_secret=SECRET, mcp_access_token_ttl=3600,
                       mcp_refresh_token_ttl=2592000, mcp_auth_code_ttl=600, mcp_consent_request_ttl=600)


class World:
    def __init__(self) -> None:
        users = FakeUserRepo({"usr-1": ALLOWED_EMAIL})
        users.users["usr-1"].config.external_tasks_enabled = True
        self.components = build_mcp_components(db_client=EvalDb(), env_config=ENV, auth_config=AUTH,
                                               memory_search=MagicMock(), cipher=AesGcmTestCipher(),
                                               digest_key=bytes(range(32)), user_repo=users,
                                               whitelist_repo=FakeWhitelistRepo())
        self.service = self.components.authorization_service
        self.asgi = self.components.fastmcp.streamable_http_app()

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.asgi, raise_app_exceptions=False),
                                 base_url=BASE)

    async def register(self, http, **scope) -> dict:
        resp = await http.post("/register", json={"redirect_uris": [REDIRECT], **scope})
        assert resp.status_code == 201, resp.text
        return resp.json()


async def _authorize(http, client: dict, **params):
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    resp = await http.get("/authorize", params={
        "client_id": client["client_id"], "redirect_uri": REDIRECT, "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "s", **params})
    return resp, verifier


async def test_a_registration_naming_no_scope_does_not_get_the_tasks_scope():
    w = World()
    async with w.http() as http:
        client = await w.register(http)
    assert client["scope"].split() == ["user_context", "drive_read"]


async def test_a_registration_may_name_the_tasks_scope():
    w = World()
    async with w.http() as http:
        client = await w.register(http, scope="external_tasks")
    assert client["scope"] == "external_tasks"


@pytest.mark.parametrize("params", [{"scope": "external_tasks"}, {"resource": TASKS}])
async def test_a_client_without_the_tasks_scope_cannot_authorize_the_tasks_resource(params):
    w = World()
    async with w.http() as http:
        client = await w.register(http, scope="user_context drive_read")
        resp, _ = await _authorize(http, client, **params)
    assert resp.status_code == 302
    query = parse_qs(urlsplit(resp.headers["location"]).query)
    assert query["error"] == ["invalid_scope"] and "/mcp/consent" not in resp.headers["location"]


@pytest.mark.parametrize("params", [{"scope": "external_tasks"}, {"resource": TASKS},
                                    {"scope": "external_tasks", "resource": TASKS}])
async def test_a_tasks_client_gets_a_tasks_token_bound_to_the_picked_worker(params):
    w = World()
    async with w.http() as http:
        client = await w.register(http, scope="external_tasks")
        resp, verifier = await _authorize(http, client, **params)
        assert resp.status_code == 302 and "/mcp/consent?req=" in resp.headers["location"]
        req = resp.headers["location"].split("req=", 1)[1]
        assert w.service.verify_consent_request(req).resource == TASKS
        code, _ = await w.service.issue_auth_code_for_consent(req, user_id="usr-1", account_id="acc-1",
                                                              worker_id="w1")
        token = await http.post("/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
            "client_id": client["client_id"], "client_secret": client["client_secret"], "code_verifier": verifier})
        assert token.status_code == 200, token.text
        body = token.json()
        assert body["scope"] == "external_tasks"
        claims = jwt.decode(body["access_token"], SECRET, algorithms=["HS256"], audience=TASKS)
        assert (claims["wid"], claims["sub"], claims["account_id"]) == ("w1", "usr-1", "acc-1")
        # the facts app refuses it as a bearer
        assert (await http.post("/mcp", headers={"Authorization": f"Bearer {body['access_token']}"},
                                json={})).status_code == 401
