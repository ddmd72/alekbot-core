"""The authorization service binds `resource` and scope at /authorize and verifies tokens per resource."""

from datetime import datetime, timezone
from types import SimpleNamespace

import jwt
import pytest

from src.adapters.firestore_mcp_client_repository import FirestoreMCPClientRepository
from src.domain.mcp import MCPClient, MCPRefreshToken
from src.services.mcp_authorization_service import (
    AuthorizationParams,
    MCPAuthorizationService,
    MCPInvalidGrant,
    MCPInvalidResource,
    MCPInvalidScope,
)
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb
from tests.support.mcp_gate_fakes import allowing_repos

FACTS = "https://mcp.example.test/mcp"
DRIVE = "https://mcp.example.test/drive-mcp"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
SECRET = "binding-jwt-secret-0123456789abcdef-xyz"
ENV = SimpleNamespace(mcp_oauth_clients_collection="c", mcp_auth_codes_collection="a", mcp_refresh_tokens_collection="r")


class World:
    def __init__(self) -> None:
        self.db = EvalDb()
        self.repo = FirestoreMCPClientRepository(self.db, ENV, cipher=AesGcmTestCipher(), digest_key=bytes(range(32)))
        users, whitelist = allowing_repos("u1")
        self.service = MCPAuthorizationService(
            self.repo, jwt_secret=SECRET, mcp_resource_uri=FACTS, drive_resource_uri=DRIVE,
            consent_base_url="https://mcp.example.test/mcp/consent", allowed_redirect_hosts=("claude.ai",),
            user_repo=users, whitelist_repo=whitelist)
        self.client = MCPClient(client_id="c1", client_secret="s", redirect_uris=[REDIRECT],
                                scope="user_context drive_read", created_at=datetime.now(timezone.utc))

    async def consent(self, resource, scopes) -> str:
        url = await self.service.build_consent_redirect(self.client, AuthorizationParams(
            redirect_uri=REDIRECT, code_challenge="c" * 43, scopes=scopes, state="x", resource=resource))
        return url.split("req=", 1)[1]

    async def token_for(self, resource, scopes) -> str:
        req = await self.consent(resource, scopes)
        code, _ = await self.service.issue_auth_code_for_consent(req, user_id="u1", account_id="acc1")
        return (await self.service.finalize_auth_code(code)).access_token


async def test_a_drive_consent_request_carries_the_drive_resource_and_scope():
    w = World()
    req = w.service.verify_consent_request(await w.consent(DRIVE + "/", []))
    assert (req.resource, req.scopes) == (DRIVE, ["drive_read"])


async def test_an_unknown_resource_raises_invalid_resource_and_mints_no_consent_request():
    w = World()
    with pytest.raises(MCPInvalidResource):
        await w.consent("https://other.example.test/mcp", ["user_context"])
    assert w.db.collection("a").docs == {}


async def test_the_facts_resource_with_the_drive_scope_raises_invalid_scope():
    with pytest.raises(MCPInvalidScope):
        await World().consent(FACTS, ["drive_read"])


async def test_a_drive_access_token_has_the_drive_audience():
    w = World()
    token = await w.token_for(DRIVE, ["drive_read"])
    payload = jwt.decode(token, SECRET, algorithms=["HS256"], audience=DRIVE)
    assert (payload["aud"], payload["scope"]) == (DRIVE, "drive_read")


async def test_a_drive_token_verifies_only_against_the_drive_resource():
    w = World()
    token = await w.token_for(DRIVE, ["drive_read"])
    assert w.service.verify_access_token(token, resource=DRIVE) is not None
    assert w.service.verify_access_token(token) is None
    assert w.service.verify_access_token(token, resource=FACTS) is None


async def test_a_facts_token_verifies_only_against_the_facts_resource():
    w = World()
    token = await w.token_for(None, [])
    assert w.service.verify_access_token(token) is not None
    assert w.service.verify_access_token(token, resource=DRIVE) is None


async def test_a_refresh_token_stored_without_a_resource_still_mints_the_facts_audience():
    w = World()
    await w.repo.save_refresh_token(MCPRefreshToken(
        token_hash=w.service._hash_token("legacy"), client_id="c1", user_id="u1", account_id="acc1",
        scopes=["user_context"], resource=None, expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc)))
    issued = await w.service.rotate_refresh_token("legacy")
    assert jwt.decode(issued.access_token, SECRET, algorithms=["HS256"], audience=FACTS)["aud"] == FACTS


async def test_a_user_context_only_client_asking_for_the_drive_resource_with_no_scope_is_invalid_scope():
    w = World()
    w.client = w.client.model_copy(update={"scope": "user_context"})
    with pytest.raises(MCPInvalidScope):
        await w.consent(DRIVE, [])
    assert w.db.collection("a").docs == {}
    assert w.service.verify_consent_request(await w.consent(FACTS, [])).scopes == ["user_context"]


async def test_a_client_stored_without_a_scope_keeps_working_for_facts_only():
    w = World()
    w.client = w.client.model_copy(update={"scope": ""})
    assert w.service.verify_consent_request(await w.consent(FACTS, [])).scopes == ["user_context"]
    with pytest.raises(MCPInvalidScope):
        await w.consent(DRIVE, ["drive_read"])


async def test_a_service_built_with_a_non_canonical_facts_uri_mints_and_verifies_facts_tokens():
    w = World()
    users, whitelist = allowing_repos("u1")
    w.service = MCPAuthorizationService(
        w.repo, jwt_secret=SECRET, mcp_resource_uri="https://MCP.Example.test/mcp/", drive_resource_uri=DRIVE,
        consent_base_url="https://mcp.example.test/mcp/consent", allowed_redirect_hosts=("claude.ai",),
        user_repo=users, whitelist_repo=whitelist)
    token = await w.token_for(None, [])
    assert w.service.verify_access_token(token) is not None
    assert w.service.verify_access_token(token, resource=DRIVE) is None


async def test_a_legacy_token_with_the_raw_audience_still_verifies():
    w = World()
    legacy = jwt.encode({"type": "mcp_access", "sub": "u1", "account_id": "acc1", "client_id": "c1",
                         "scope": "user_context", "aud": FACTS, "exp": 4102444800}, SECRET, algorithm="HS256")
    assert w.service.verify_access_token(legacy) is not None
    raw = jwt.encode({"type": "mcp_access", "sub": "u1", "account_id": "acc1", "client_id": "c1",
                      "scope": "user_context", "aud": "https://MCP.Example.test/mcp/", "exp": 4102444800},
                     SECRET, algorithm="HS256")
    assert w.service.verify_access_token(raw) is not None


async def test_a_drive_refresh_asking_for_the_facts_scope_is_refused_by_the_service():
    w = World()
    await w.repo.save_refresh_token(MCPRefreshToken(
        token_hash=w.service._hash_token("rt"), client_id="c1", user_id="u1", account_id="acc1",
        scopes=["drive_read"], resource=DRIVE, expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc)))
    with pytest.raises(MCPInvalidGrant):
        await w.service.rotate_refresh_token("rt", requested_scopes=["user_context"])
    assert (await w.service.rotate_refresh_token("rt", requested_scopes=["drive_read"])).scopes == ["drive_read"]
