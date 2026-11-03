"""The stable grant id (DRIVE_MCP_CONNECTOR_RFC §3.3.8, E13): minted at code exchange, kept through every refresh
rotation, carried in the access token as `gid`, and checked by `grant_allowed` for the one-time drive link."""

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt

from src.adapters.firestore_mcp_client_repository import FirestoreMCPClientRepository
from src.domain.mcp import MCPClient, MCPRefreshToken
from src.services.mcp_authorization_service import AuthorizationParams, MCPAuthorizationService
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb
from tests.support.mcp_gate_fakes import REMOVED_EMAIL, SCOPE_OF, FakeUserRepo, FakeWhitelistRepo, allowing_repos

FACTS = "https://mcp.example.test/mcp"
DRIVE = "https://mcp.example.test/drive-mcp"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
SECRET = "grant-id-jwt-secret-0123456789abcdef-xyz"
ENV = SimpleNamespace(mcp_oauth_clients_collection="c", mcp_auth_codes_collection="a", mcp_refresh_tokens_collection="r")
FAR = datetime(2099, 1, 1, tzinfo=timezone.utc)


class World:
    def __init__(self, users=None, whitelist=None) -> None:
        self.db = EvalDb()
        self.repo = FirestoreMCPClientRepository(self.db, ENV, cipher=AesGcmTestCipher(), digest_key=bytes(range(32)))
        default_users, default_whitelist = allowing_repos("u1")
        self.users = users or default_users
        self.whitelist = whitelist or default_whitelist
        self.service = MCPAuthorizationService(
            self.repo, jwt_secret=SECRET, mcp_resource_uri=FACTS, drive_resource_uri=DRIVE, scope_of=SCOPE_OF,
            consent_base_url="https://mcp.example.test/mcp/consent", allowed_redirect_hosts=("claude.ai",),
            user_repo=self.users, whitelist_repo=self.whitelist)
        self.client = MCPClient(client_id="c1", client_secret="s", redirect_uris=[REDIRECT],
                                scope="user_context drive_read", created_at=datetime.now(timezone.utc))

    async def exchange(self, resource=DRIVE, scopes=("drive_read",)):
        url = await self.service.build_consent_redirect(self.client, AuthorizationParams(
            redirect_uri=REDIRECT, code_challenge="c" * 43, scopes=list(scopes), state="x", resource=resource))
        code, _ = await self.service.issue_auth_code_for_consent(url.split("req=", 1)[1], user_id="u1",
                                                                 account_id="acc1")
        return await self.service.finalize_auth_code(code)

    def gid(self, access_token: str, audience: str = DRIVE):
        return jwt.decode(access_token, SECRET, algorithms=["HS256"], audience=audience).get("gid")

    async def stored(self, refresh_value: str) -> MCPRefreshToken:
        token = await self.repo.get_refresh_token(self.service._hash_token(refresh_value))
        assert token is not None
        return token


async def test_a_code_exchange_mints_a_grant_id_into_the_refresh_token_and_the_access_token():
    w = World()
    issued = await w.exchange()
    stored = await w.stored(issued.refresh_token)
    assert stored.grant_id and len(stored.grant_id) == 22
    assert w.gid(issued.access_token) == stored.grant_id
    assert w.service.verify_access_token(issued.access_token, resource=DRIVE).grant_id == stored.grant_id


async def test_every_rotation_keeps_the_grant_id():
    w = World()
    issued = await w.exchange()
    grant_id = (await w.stored(issued.refresh_token)).grant_id
    for _ in range(3):
        issued = await w.service.rotate_refresh_token(issued.refresh_token)
        assert (await w.stored(issued.refresh_token)).grant_id == grant_id
        assert w.gid(issued.access_token) == grant_id


async def test_a_refresh_token_stored_without_one_gets_one_at_its_first_rotation_and_keeps_it():
    w = World()
    await w.repo.save_refresh_token(MCPRefreshToken(
        token_hash=w.service._hash_token("legacy"), client_id="c1", user_id="u1", account_id="acc1",
        scopes=["drive_read"], resource=DRIVE, expires_at=FAR))
    first = await w.service.rotate_refresh_token("legacy")
    grant_id = (await w.stored(first.refresh_token)).grant_id
    assert grant_id and w.gid(first.access_token) == grant_id
    second = await w.service.rotate_refresh_token(first.refresh_token)
    assert (await w.stored(second.refresh_token)).grant_id == grant_id
    assert w.gid(second.access_token) == grant_id


async def test_two_grants_of_one_user_have_two_ids():
    w = World()
    one, two = await w.exchange(), await w.exchange()
    assert w.gid(one.access_token) != w.gid(two.access_token)


async def test_grant_allowed_is_true_for_an_active_grant_of_a_whitelisted_user():
    w = World()
    issued = await w.exchange()
    issued = await w.service.rotate_refresh_token(issued.refresh_token)
    assert await w.service.grant_allowed("u1", w.gid(issued.access_token)) is True


async def test_grant_allowed_is_false_after_revoke_after_expiry_and_after_whitelist_removal():
    revoked = World()
    issued = await revoked.exchange()
    await revoked.service.revoke_refresh_token_value(issued.refresh_token)
    assert await revoked.service.grant_allowed("u1", revoked.gid(issued.access_token)) is False

    expired = World()
    issued = await expired.exchange()
    stored = await expired.stored(issued.refresh_token)
    await expired.repo.save_refresh_token(stored.model_copy(
        update={"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}))
    assert await expired.service.grant_allowed("u1", expired.gid(issued.access_token)) is False

    removed = World(users=FakeUserRepo({"u1": REMOVED_EMAIL}), whitelist=FakeWhitelistRepo(emails=(REMOVED_EMAIL,)))
    issued = await removed.exchange()
    grant_id = removed.gid(issued.access_token)
    assert await removed.service.grant_allowed("u1", grant_id) is True
    removed.whitelist.entry.allowed_emails.discard(REMOVED_EMAIL)
    assert await removed.service.grant_allowed("u1", grant_id) is False


async def test_grant_allowed_is_false_without_a_grant_id():
    w = World()
    await w.exchange()
    assert await w.service.grant_allowed("u1", None) is False
    assert await w.service.grant_allowed("u1", "") is False
    assert await w.service.grant_allowed("u1", "unknown-grant-id-000000") is False


async def test_the_refusal_log_carries_no_email(caplog):
    w = World(users=FakeUserRepo({"u1": REMOVED_EMAIL}), whitelist=FakeWhitelistRepo(emails=()))
    await w.repo.save_refresh_token(MCPRefreshToken(
        token_hash="h", client_id="c1", user_id="u1", account_id="acc1", scopes=["drive_read"], resource=DRIVE,
        expires_at=FAR, grant_id="g" * 22))
    with caplog.at_level(logging.DEBUG):
        assert await w.service.grant_allowed("u1", "g" * 22) is False
    assert "MCP link grant refused: user=u1 reason=not_whitelisted" in caplog.text
    assert REMOVED_EMAIL not in caplog.text


async def test_the_facts_token_verifies_as_before_and_carries_its_gid():
    w = World()
    issued = await w.exchange(resource=None, scopes=())
    verified = w.service.verify_access_token(issued.access_token)
    assert verified is not None
    assert (verified.user_id, verified.account_id, verified.scopes) == ("u1", "acc1", ["user_context"])
    assert verified.grant_id == w.gid(issued.access_token, FACTS) == (await w.stored(issued.refresh_token)).grant_id
