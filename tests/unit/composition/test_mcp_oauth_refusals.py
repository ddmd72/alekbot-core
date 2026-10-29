"""Refusals of the remote MCP server (P7, inventory §4): the consent page, the SDK app main.py dispatches to
(`/mcp`, `/register`, `/authorize`, `/token`), and the service / provider sites no route can reach.

Everything is real down to the database: `build_mcp_components` over the real Firestore MCP repository on the
evaluating fake, the real consent blueprint, the real session service. The ASGI client talks to the same
`streamable_http_app()` main.py serves. A refused request must leave no client, code or refresh token behind.
"""
import base64
import hashlib
import secrets
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import jwt
import pytest
from mcp.server.auth.provider import RegistrationError, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata
from pydantic import AnyUrl
from quart import Quart

from src.adapters.firestore_mcp_client_repository import FirestoreMCPClientRepository
from src.composition.mcp_sdk_oauth_provider import MCPSdkOAuthProvider
from src.composition.mcp_setup import build_mcp_components
from src.domain.mcp import MCPClient
from src.services.mcp_authorization_service import (
    AuthorizationParams, MCPAuthError, MCPAuthorizationService, MCPInvalidRedirectURI,
)
from src.web.mcp_consent_app import build_mcp_consent_blueprint
from tests.support.firestore_eval import EvalDb
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.unit.web.gate_fakes import access_token, forged_token, session_service

RESOURCE = "https://mcp.example.test/mcp"
BASE = "https://mcp.example.test"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
SECRET = "p7-mcp-jwt-secret-0123456789abcdef-xyz"
CLIENTS, CODES, REFRESH = "mcp_clients", "mcp_codes", "mcp_refresh"
ENV = SimpleNamespace(mcp_oauth_clients_collection=CLIENTS, mcp_auth_codes_collection=CODES,
                      mcp_refresh_tokens_collection=REFRESH)


def _auth_config(resource: str = RESOURCE) -> SimpleNamespace:
    return SimpleNamespace(mcp_resource_uri=resource, oauth_session_secret=SECRET, mcp_access_token_ttl=3600,
                           mcp_refresh_token_ttl=2592000, mcp_auth_code_ttl=600, mcp_consent_request_ttl=600)


class World:
    def __init__(self) -> None:
        self.db = EvalDb()
        self.components = build_mcp_components(db_client=self.db, env_config=ENV, auth_config=_auth_config(),
                                               search_enrichment_service=MagicMock(), cipher=AesGcmTestCipher(), digest_key=bytes(range(32)))
        self.service = self.components.authorization_service
        self.asgi = self.components.fastmcp.streamable_http_app()

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.asgi, raise_app_exceptions=False),
                                 base_url=BASE)

    def docs(self, collection: str) -> dict:
        return self.db.collection(collection).docs

    async def register(self, http: httpx.AsyncClient) -> dict:
        resp = await http.post("/register", json={"redirect_uris": [REDIRECT], "scope": "user_context"})
        assert resp.status_code == 201, resp.text
        return resp.json()

    async def tokens_for(self, http: httpx.AsyncClient, client: dict) -> dict:
        return await _tokens_for(self.service, http, client)


async def _tokens_for(service: MCPAuthorizationService, http: httpx.AsyncClient, client: dict) -> dict:
    """The full authorization-code flow for `client` over the SDK routes, consent approved by user u1."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    auth = await http.get("/authorize", params={
        "client_id": client["client_id"], "redirect_uri": REDIRECT, "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "s", "scope": "user_context"})
    req = auth.headers["location"].split("req=", 1)[1]
    code, _ = await service.issue_auth_code_for_consent(req, user_id="u1", account_id="acc1")
    resp = await http.post("/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": client["client_id"], "client_secret": client["client_secret"], "code_verifier": verifier})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _consent_req(service: MCPAuthorizationService) -> str:
    client = MCPClient(client_id="c1", client_secret="s", redirect_uris=[REDIRECT], created_at=datetime.now(timezone.utc))
    url = await service.build_consent_redirect(client, AuthorizationParams(
        redirect_uri=REDIRECT, code_challenge="c" * 43, scopes=["user_context"], state="xyz", resource=RESOURCE))
    return url.split("req=", 1)[1]


# --------------------------------------------------------------------------- POST /mcp/consent

class _CodeIssueFails(MCPAuthorizationService):
    """The consent JWT expires between the page's check and the code issue: the service refuses the issue."""

    async def issue_auth_code_for_consent(self, consent_jwt, user_id, account_id):
        raise MCPAuthError("invalid consent request: Signature has expired")


def _consent_app(service: MCPAuthorizationService):
    app = Quart("mcp_consent_refusals")
    app.register_blueprint(build_mcp_consent_blueprint(service, session_service()))
    return app.test_client()


class TestConsentPost:
    async def test_invalid_cabinet_cookie_redirects_to_login_and_issues_no_code(self):
        w = World()
        client = _consent_app(w.service)
        client.set_cookie("localhost", "access_token", forged_token())
        resp = await client.post("/mcp/consent", form={"req": await _consent_req(w.service), "action": "approve"})
        assert resp.status_code == 302
        assert resp.headers["Location"].startswith("/auth/login?next=")
        assert w.docs(CODES) == {}

    async def test_bad_consent_jwt_is_400_and_issues_no_code(self):
        w = World()
        client = _consent_app(w.service)
        client.set_cookie("localhost", "access_token", access_token())
        forged_req = jwt.encode({"client_id": "c1", "redirect_uri": "https://evil.example/cb", "code_challenge": "c",
                                 "aud": "mcp-consent", "exp": 4102444800}, "another-secret-0123456789abcdef-xyz",
                                algorithm="HS256")
        resp = await client.post("/mcp/consent", form={"req": forged_req, "action": "approve"})
        assert resp.status_code == 400
        assert "Location" not in resp.headers
        assert w.docs(CODES) == {}

    async def test_approve_when_the_code_issue_fails_is_400_and_redirects_nowhere(self):
        db = EvalDb()
        service = _CodeIssueFails(FirestoreMCPClientRepository(db, ENV, cipher=AesGcmTestCipher(), digest_key=bytes(range(32))), jwt_secret=SECRET, mcp_resource_uri=RESOURCE,
                                  consent_base_url=f"{BASE}/mcp/consent", allowed_redirect_hosts=("claude.ai",))
        client = _consent_app(service)
        client.set_cookie("localhost", "access_token", access_token())
        resp = await client.post("/mcp/consent", form={"req": await _consent_req(service), "action": "approve"})
        assert resp.status_code == 400
        assert "Location" not in resp.headers
        assert db.collection(CODES).docs == {}

    async def test_approve_with_a_valid_session_issues_a_code(self):
        # Positive control.
        w = World()
        client = _consent_app(w.service)
        client.set_cookie("localhost", "access_token", access_token("u1", "acc1"))
        resp = await client.post("/mcp/consent", form={"req": await _consent_req(w.service), "action": "approve"})
        assert resp.status_code == 302 and resp.headers["Location"].startswith(REDIRECT + "?code=")
        assert [d["user_id"] for d in w.docs(CODES).values()] == ["u1"]


# --------------------------------------------------------------------------- /mcp bearer

_TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


class TestMcpBearer:
    async def test_no_bearer_is_401(self):
        async with World().http() as http:
            resp = await http.post("/mcp", json=_TOOLS_LIST)
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"].startswith("Bearer")

    async def test_forged_bearer_is_401(self):
        token = jwt.encode({"type": "mcp_access", "sub": "u1", "account_id": "acc1", "client_id": "c1",
                            "aud": RESOURCE, "scope": "user_context", "exp": 4102444800},
                           "another-secret-0123456789abcdef-xyz", algorithm="HS256")
        async with World().http() as http:
            resp = await http.post("/mcp", json=_TOOLS_LIST, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    async def test_bearer_for_another_resource_is_401(self):
        w = World()
        other = build_mcp_components(db_client=EvalDb(), env_config=ENV,
                                     auth_config=_auth_config("https://other.example.test/mcp"),
                                     search_enrichment_service=MagicMock(), cipher=AesGcmTestCipher(), digest_key=bytes(range(32)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=other.fastmcp.streamable_http_app()),
                                     base_url="https://other.example.test") as other_http:
            # Same signing secret, so only the audience tells the two servers' tokens apart.
            client = await w.register(other_http)
            tokens = await _tokens_for(other.authorization_service, other_http, client)
        async with w.http() as http:
            resp = await http.post("/mcp", json=_TOOLS_LIST, headers={"Authorization": f"Bearer {tokens['access_token']}"})
        assert resp.status_code == 401

    async def test_refresh_token_used_as_access_token_is_401(self):
        w = World()
        async with w.http() as http:
            tokens = await w.tokens_for(http, await w.register(http))
            resp = await http.post("/mcp", json=_TOOLS_LIST,
                                   headers={"Authorization": f"Bearer {tokens['refresh_token']}"})
            own = await http.post("/mcp", json=_TOOLS_LIST,
                                  headers={"Authorization": f"Bearer {tokens['access_token']}"})
        assert resp.status_code == 401
        assert own.status_code != 401   # positive control: the real access token passes the bearer gate


# --------------------------------------------------------------------------- /token

class TestToken:
    async def test_refresh_token_of_another_client_is_invalid_grant_and_stays_usable(self):
        w = World()
        async with w.http() as http:
            owner, other = await w.register(http), await w.register(http)
            tokens = await w.tokens_for(http, owner)
            stolen = await http.post("/token", data={
                "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                "client_id": other["client_id"], "client_secret": other["client_secret"]})
            assert stolen.status_code == 400 and stolen.json()["error"] == "invalid_grant"
            assert all(d["revoked_at"] is None for d in w.docs(REFRESH).values())
            mine = await http.post("/token", data={
                "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                "client_id": owner["client_id"], "client_secret": owner["client_secret"]})
        assert mine.status_code == 200

    async def test_refresh_token_reused_after_rotation_is_invalid_grant(self):
        w = World()
        async with w.http() as http:
            client = await w.register(http)
            tokens = await w.tokens_for(http, client)
            form = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                    "client_id": client["client_id"], "client_secret": client["client_secret"]}
            first = await http.post("/token", data=form)
            again = await http.post("/token", data=form)
        assert first.status_code == 200
        assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
        assert len(w.docs(REFRESH)) == 2   # the original (revoked) and the one rotation minted; no third

    async def test_refresh_raced_by_a_concurrent_rotation_is_invalid_grant(self):
        # The provider's own wrap (mcp_sdk_oauth_provider.py:251-259): the token loaded fine, then another request
        # rotated it before this exchange ran.
        w = World()
        async with w.http() as http:
            client = await w.register(http)
            tokens = await w.tokens_for(http, client)
        provider = MCPSdkOAuthProvider(w.service)
        sdk_client = await provider.get_client(client["client_id"])
        loaded = await provider.load_refresh_token(sdk_client, tokens["refresh_token"])
        await w.service.rotate_refresh_token(tokens["refresh_token"])
        with pytest.raises(TokenError) as raised:
            await provider.exchange_refresh_token(sdk_client, loaded, [])
        assert raised.value.error == "invalid_grant"
        assert len(w.docs(REFRESH)) == 2


# --------------------------------------------------------------------------- /register

class TestRegister:
    @pytest.mark.parametrize("uri", ["javascript:alert(1)", "ftp://claude.ai/cb", "http://claude.ai/cb",
                                     "https://evil.example/cb"])
    async def test_disallowed_redirect_uri_is_400_and_saves_no_client(self, uri):
        w = World()
        async with w.http() as http:
            resp = await http.post("/register", json={"redirect_uris": [uri]})
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_redirect_uri"
        assert w.docs(CLIENTS) == {}

    async def test_empty_redirect_uris_is_400_and_saves_no_client(self):
        # The SDK's own metadata model refuses an empty list (min_length=1) before the service sees it.
        w = World()
        async with w.http() as http:
            resp = await http.post("/register", json={"redirect_uris": []})
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_client_metadata"
        assert w.docs(CLIENTS) == {}

    async def test_null_redirect_uris_saves_no_client(self):
        # Refused, but as a 500, not a 400: the SDK model accepts null (`list[AnyUrl] | None`) and the provider's
        # translation iterates it (mcp_sdk_oauth_provider.py:98, TypeError). P7 report item.
        w = World()
        async with w.http() as http:
            resp = await http.post("/register", json={"redirect_uris": None})
        assert resp.status_code >= 400
        assert w.docs(CLIENTS) == {}

    @pytest.mark.xfail(strict=True, raises=AssertionError,
                       reason="known defect: redirect_uris null is a client error answered 500; the SDK model accepts "
                              "null and mcp_sdk_oauth_provider.py:98 iterates None (TypeError)")
    async def test_null_redirect_uris_is_400_invalid_client_metadata(self):
        w = World()
        async with w.http() as http:
            resp = await http.post("/register", json={"redirect_uris": None})
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_client_metadata"

    async def test_service_refuses_a_client_without_redirect_uris(self):
        # mcp_authorization_service.py:166-167, unreachable through the SDK route (above).
        w = World()
        with pytest.raises(MCPInvalidRedirectURI, match="redirect_uris is required"):
            await w.service.save_client(MCPClient(client_id="c0", redirect_uris=[], created_at=datetime.now(timezone.utc)))
        assert w.docs(CLIENTS) == {}

    async def test_any_other_service_refusal_is_invalid_client_metadata(self):
        # mcp_sdk_oauth_provider.py:153-158: an MCPAuthError that is not a redirect problem.
        class _Refuses(MCPAuthorizationService):
            async def save_client(self, client):
                raise MCPAuthError("client metadata rejected")

        w = World()
        provider = MCPSdkOAuthProvider(_Refuses(w.service._repo, jwt_secret=SECRET, mcp_resource_uri=RESOURCE,
                                                consent_base_url=f"{BASE}/mcp/consent",
                                                allowed_redirect_hosts=("claude.ai",)))
        info = OAuthClientMetadata(redirect_uris=[AnyUrl(REDIRECT)])
        with pytest.raises(RegistrationError) as raised:
            await provider.register_client(OAuthClientInformationFull(client_id="c9", **info.model_dump()))
        assert raised.value.error == "invalid_client_metadata"
        assert w.docs(CLIENTS) == {}


# --------------------------------------------------------------------------- /authorize and the service

class TestAuthorize:
    async def test_plain_pkce_is_refused_and_no_consent_request_is_issued(self):
        w = World()
        async with w.http() as http:
            client = await w.register(http)
            resp = await http.get("/authorize", params={
                "client_id": client["client_id"], "redirect_uri": REDIRECT, "response_type": "code",
                "code_challenge": "c" * 43, "code_challenge_method": "plain", "state": "s"})
        assert resp.status_code == 302
        location = resp.headers["location"]
        assert location.startswith(REDIRECT) and "error=invalid_request" in location and "req=" not in location

    async def test_service_refuses_a_non_s256_challenge_method(self):
        # mcp_authorization_service.py:193-194: the SDK already refuses `plain` (above); this is the service's own check.
        w = World()
        client = MCPClient(client_id="c1", redirect_uris=[REDIRECT], created_at=datetime.now(timezone.utc))
        with pytest.raises(MCPAuthError, match="only S256"):
            await w.service.build_consent_redirect(client, AuthorizationParams(
                redirect_uri=REDIRECT, code_challenge="c" * 43, scopes=[], code_challenge_method="plain"))


def test_service_refuses_a_31_character_secret():
    # mcp_authorization_service.py:151-152.
    with pytest.raises(ValueError, match="at least 32"):
        MCPAuthorizationService(MagicMock(), jwt_secret="x" * 31, mcp_resource_uri=RESOURCE,
                                consent_base_url=f"{BASE}/mcp/consent", allowed_redirect_hosts=("claude.ai",))
