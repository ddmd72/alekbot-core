"""Every refresh rotation re-checks that the grant's user still exists, has an email and is whitelisted."""

import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.adapters.firestore_mcp_client_repository import FirestoreMCPClientRepository
from src.domain.mcp import MCPRefreshToken
from src.services.mcp_authorization_service import MCPAuthorizationService, MCPInvalidGrant
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb
from tests.support.mcp_gate_fakes import ALLOWED_EMAIL, REMOVED_EMAIL, FakeUserRepo, FakeWhitelistRepo, SCOPE_OF

FACTS = "https://mcp.example.test/mcp"
DRIVE = "https://mcp.example.test/drive-mcp"
ENV = SimpleNamespace(mcp_oauth_clients_collection="c", mcp_auth_codes_collection="a", mcp_refresh_tokens_collection="r")
USER_ID = "user-0001-abcdef"


class World:
    def __init__(self, users=None, whitelist=None) -> None:
        self.db = EvalDb()
        self.repo = FirestoreMCPClientRepository(self.db, ENV, cipher=AesGcmTestCipher(), digest_key=bytes(range(32)))
        self.users = users if users is not None else FakeUserRepo({USER_ID: ALLOWED_EMAIL})
        self.whitelist = whitelist or FakeWhitelistRepo()
        self.service = MCPAuthorizationService(
            self.repo, jwt_secret="whitelist-jwt-secret-0123456789abcdef-xyz", mcp_resource_uri=FACTS,
            drive_resource_uri=DRIVE, scope_of=SCOPE_OF, consent_base_url="https://mcp.example.test/mcp/consent",
            allowed_redirect_hosts=("claude.ai",), user_repo=self.users, whitelist_repo=self.whitelist)

    async def stored_token(self, value="refresh-1") -> str:
        await self.repo.save_refresh_token(MCPRefreshToken(
            token_hash=self.service._hash_token(value), client_id="c1", user_id=USER_ID, account_id="acc1",
            scopes=["drive_read"], resource=DRIVE, expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc)))
        return value

    def refresh_docs(self) -> dict:
        return self.db.collection("r").docs


async def test_a_whitelisted_user_rotates():
    w = World()
    issued = await w.service.rotate_refresh_token(await w.stored_token())
    assert issued.refresh_token and issued.scopes == ["drive_read"]
    assert len(w.refresh_docs()) == 2


async def test_an_email_removed_from_the_whitelist_is_invalid_grant_and_revokes_the_presented_token():
    w = World(users=FakeUserRepo({USER_ID: REMOVED_EMAIL}))
    value = await w.stored_token()
    with pytest.raises(MCPInvalidGrant, match="no longer allowed"):
        await w.service.rotate_refresh_token(value)
    [doc] = w.refresh_docs().values()
    assert doc.get("revoked_at") is not None
    assert await w.service.load_refresh_token(value) is None


async def test_a_user_that_no_longer_exists_is_invalid_grant():
    w = World(users=FakeUserRepo({}))
    with pytest.raises(MCPInvalidGrant):
        await w.service.rotate_refresh_token(await w.stored_token())
    assert len(w.refresh_docs()) == 1


async def test_a_user_without_an_email_is_invalid_grant():
    w = World(users=FakeUserRepo({USER_ID: None}))
    with pytest.raises(MCPInvalidGrant):
        await w.service.rotate_refresh_token(await w.stored_token())
    assert len(w.refresh_docs()) == 1


async def test_a_domain_entry_on_the_whitelist_keeps_the_grant():
    w = World(users=FakeUserRepo({USER_ID: REMOVED_EMAIL}), whitelist=FakeWhitelistRepo(emails=(), domains=("acme.example",)))
    issued = await w.service.rotate_refresh_token(await w.stored_token())
    assert issued.refresh_token


async def test_the_refusal_log_line_carries_no_email(caplog):
    w = World(users=FakeUserRepo({USER_ID: REMOVED_EMAIL}))
    with caplog.at_level(logging.INFO), pytest.raises(MCPInvalidGrant):
        await w.service.rotate_refresh_token(await w.stored_token())
    lines = [r.getMessage() for r in caplog.records if "MCP refresh refused" in r.getMessage()]
    assert lines == [f"MCP refresh refused: user={USER_ID[:8]} reason=not_whitelisted"]
    assert REMOVED_EMAIL not in caplog.text and "acme.example" not in caplog.text


async def test_a_whitelist_read_error_mints_nothing():
    w = World()
    w.whitelist.error = RuntimeError("whitelist backend down")
    with pytest.raises(RuntimeError):
        await w.service.rotate_refresh_token(await w.stored_token())
    assert len(w.refresh_docs()) == 1
    assert all(d.get("revoked_at") is None for d in w.refresh_docs().values())
