"""
Unit tests for MCPAuthorizationService — the OAuth 2.1 business logic
for the remote MCP server exposed to claude.ai.

Tests cover:
- Client save validates redirect-URI host allowlist
- Consent-state JWT roundtrip
- Authorization code mint → consume → one-shot enforcement
- Refresh token rotation (old revoked, new usable)
- Access token JWT verification (audience, type, expiry)
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
import pytest

from src.domain.mcp import MCPAuthCode, MCPClient, MCPRefreshToken
from src.ports.mcp_client_repository import MCPClientRepository
from src.services.mcp_authorization_service import (
    AuthorizationParams,
    MCPAuthError,
    MCPAuthorizationService,
    MCPInvalidGrant,
    MCPInvalidRedirectURI,
)


_TEST_SECRET = "test-secret-that-is-32-characters-long-ok"
_TEST_RESOURCE_URI = "https://dev.alekbot.app/mcp"
_TEST_ALLOWED_HOSTS = ("claude.ai", "claude.com", "localhost", "127.0.0.1")


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class InMemoryMCPRepo(MCPClientRepository):
    def __init__(self):
        self.clients: dict[str, MCPClient] = {}
        self.codes: dict[str, MCPAuthCode] = {}
        self.refresh_tokens: dict[str, MCPRefreshToken] = {}

    async def save_client(self, client: MCPClient) -> None:
        self.clients[client.client_id] = client

    async def get_client(self, client_id: str) -> Optional[MCPClient]:
        return self.clients.get(client_id)

    async def delete_client(self, client_id: str) -> None:
        self.clients.pop(client_id, None)

    async def save_auth_code(self, auth_code: MCPAuthCode) -> None:
        self.codes[auth_code.code] = auth_code

    async def get_auth_code(self, code: str) -> Optional[MCPAuthCode]:
        return self.codes.get(code)

    async def consume_auth_code(self, code: str) -> Optional[MCPAuthCode]:
        return self.codes.pop(code, None)

    async def save_refresh_token(self, token: MCPRefreshToken) -> None:
        self.refresh_tokens[token.token_hash] = token

    async def get_refresh_token(self, token_hash: str) -> Optional[MCPRefreshToken]:
        return self.refresh_tokens.get(token_hash)

    async def revoke_refresh_token(self, token_hash: str) -> None:
        existing = self.refresh_tokens.get(token_hash)
        if existing:
            self.refresh_tokens[token_hash] = existing.model_copy(
                update={"revoked_at": datetime.now(timezone.utc)}
            )


@pytest.fixture
def repo():
    return InMemoryMCPRepo()


@pytest.fixture
def service(repo):
    return MCPAuthorizationService(
        repo=repo,
        jwt_secret=_TEST_SECRET,
        mcp_resource_uri=_TEST_RESOURCE_URI,
        consent_base_url="https://dev.alekbot.app/mcp/consent",
        allowed_redirect_hosts=_TEST_ALLOWED_HOSTS,
        access_token_ttl=3600,
        refresh_token_ttl=2592000,
        auth_code_ttl=600,
        consent_request_ttl=600,
    )


@pytest.fixture
def claude_client():
    return MCPClient(
        client_id="mcp-claude-abc",
        client_secret="plaintext-secret-xyz",
        client_name="Claude",
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
        created_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# save_client — redirect-URI allowlist
# ---------------------------------------------------------------------------


class TestSaveClient:
    async def test_valid_claude_ai_redirect_is_saved(self, service, repo, claude_client):
        await service.save_client(claude_client)
        assert await repo.get_client("mcp-claude-abc") is not None

    async def test_valid_claude_com_redirect_is_saved(self, service):
        c = MCPClient(
            client_id="mcp-x",
            client_secret="s",
            client_name="Claude",
            redirect_uris=["https://claude.com/api/mcp/auth_callback"],
            created_at=datetime.now(timezone.utc),
        )
        await service.save_client(c)

    async def test_subdomain_of_claude_ai_is_allowed(self, service):
        c = MCPClient(
            client_id="mcp-x",
            client_secret="s",
            client_name="Claude",
            redirect_uris=["https://api.claude.ai/api/mcp/auth_callback"],
            created_at=datetime.now(timezone.utc),
        )
        await service.save_client(c)

    async def test_localhost_http_is_allowed(self, service):
        c = MCPClient(
            client_id="mcp-x",
            client_secret="s",
            client_name="Inspector",
            redirect_uris=["http://localhost:8090/cb"],
            created_at=datetime.now(timezone.utc),
        )
        await service.save_client(c)

    async def test_arbitrary_host_is_rejected(self, service):
        c = MCPClient(
            client_id="mcp-x",
            client_secret="s",
            client_name="evil",
            redirect_uris=["https://evil.example.com/cb"],
            created_at=datetime.now(timezone.utc),
        )
        with pytest.raises(MCPInvalidRedirectURI):
            await service.save_client(c)

    async def test_http_on_non_localhost_is_rejected(self, service):
        c = MCPClient(
            client_id="mcp-x",
            client_secret="s",
            client_name="evil",
            redirect_uris=["http://claude.ai/api/mcp/auth_callback"],
            created_at=datetime.now(timezone.utc),
        )
        with pytest.raises(MCPInvalidRedirectURI):
            await service.save_client(c)


# ---------------------------------------------------------------------------
# Consent JWT roundtrip
# ---------------------------------------------------------------------------


class TestConsentRequest:
    async def test_build_and_verify_roundtrip(self, service, claude_client):
        await service.save_client(claude_client)
        url = await service.build_consent_redirect(
            claude_client,
            AuthorizationParams(
                redirect_uri="https://claude.ai/api/mcp/auth_callback",
                code_challenge="abcdef1234567890",
                scopes=["user_context"],
                state="xyz",
                resource="https://dev.alekbot.app/mcp",
            ),
        )
        assert url.startswith("https://dev.alekbot.app/mcp/consent?req=")
        req_jwt = url.split("req=")[1]
        parsed = service.verify_consent_request(req_jwt)
        assert parsed.client_id == "mcp-claude-abc"
        assert parsed.redirect_uri == "https://claude.ai/api/mcp/auth_callback"
        assert parsed.code_challenge == "abcdef1234567890"
        assert parsed.scopes == ["user_context"]
        assert parsed.state == "xyz"
        assert parsed.resource == "https://dev.alekbot.app/mcp"

    async def test_verify_rejects_tampered_jwt(self, service, claude_client):
        await service.save_client(claude_client)
        url = await service.build_consent_redirect(
            claude_client,
            AuthorizationParams(
                redirect_uri="https://claude.ai/api/mcp/auth_callback",
                code_challenge="x" * 32,
                scopes=["user_context"],
            ),
        )
        tampered = url.split("req=")[1][:-5] + "zzzzz"
        with pytest.raises(MCPAuthError):
            service.verify_consent_request(tampered)

    async def test_redirect_uri_must_match_registered(self, service, claude_client):
        await service.save_client(claude_client)
        with pytest.raises(MCPInvalidRedirectURI):
            await service.build_consent_redirect(
                claude_client,
                AuthorizationParams(
                    redirect_uri="https://claude.ai/other-path",
                    code_challenge="x" * 32,
                    scopes=["user_context"],
                ),
            )

    async def test_missing_pkce_is_rejected(self, service, claude_client):
        await service.save_client(claude_client)
        with pytest.raises(MCPAuthError):
            await service.build_consent_redirect(
                claude_client,
                AuthorizationParams(
                    redirect_uri="https://claude.ai/api/mcp/auth_callback",
                    code_challenge="",
                    scopes=["user_context"],
                ),
            )


# ---------------------------------------------------------------------------
# Auth code issuance + finalize
# ---------------------------------------------------------------------------


class TestAuthCodeFlow:
    async def _issue_code(self, service, claude_client) -> str:
        url = await service.build_consent_redirect(
            claude_client,
            AuthorizationParams(
                redirect_uri="https://claude.ai/api/mcp/auth_callback",
                code_challenge="challenge" * 4,
                scopes=["user_context"],
                state="s",
                resource="https://dev.alekbot.app/mcp",
            ),
        )
        req_jwt = url.split("req=")[1]
        code, _ = await service.issue_auth_code_for_consent(
            req_jwt, user_id="user-1", account_id="acc-1"
        )
        return code

    async def test_issue_persists_auth_code_bound_to_user(
        self, service, repo, claude_client
    ):
        await service.save_client(claude_client)
        code = await self._issue_code(service, claude_client)
        stored = await repo.get_auth_code(code)
        assert stored is not None
        assert stored.user_id == "user-1"
        assert stored.account_id == "acc-1"
        assert stored.client_id == "mcp-claude-abc"

    async def test_finalize_mints_access_and_refresh(
        self, service, repo, claude_client
    ):
        await service.save_client(claude_client)
        code = await self._issue_code(service, claude_client)
        issued = await service.finalize_auth_code(code)
        assert issued.access_token
        assert issued.refresh_token
        assert issued.expires_in == 3600
        assert issued.scopes == ["user_context"]
        # Refresh token is persisted by hash
        assert len(repo.refresh_tokens) == 1

    async def test_finalize_is_one_shot(self, service, claude_client):
        await service.save_client(claude_client)
        code = await self._issue_code(service, claude_client)
        await service.finalize_auth_code(code)
        with pytest.raises(MCPInvalidGrant):
            await service.finalize_auth_code(code)


# ---------------------------------------------------------------------------
# Refresh token rotation
# ---------------------------------------------------------------------------


class TestRefreshTokenRotation:
    async def _mint_refresh(self, service, claude_client) -> str:
        await service.save_client(claude_client)
        url = await service.build_consent_redirect(
            claude_client,
            AuthorizationParams(
                redirect_uri="https://claude.ai/api/mcp/auth_callback",
                code_challenge="c" * 32,
                scopes=["user_context"],
            ),
        )
        req_jwt = url.split("req=")[1]
        code, _ = await service.issue_auth_code_for_consent(req_jwt, "user-1", "acc-1")
        issued = await service.finalize_auth_code(code)
        return issued.refresh_token

    async def test_rotation_issues_new_and_revokes_old(self, service, claude_client):
        old = await self._mint_refresh(service, claude_client)
        rotated = await service.rotate_refresh_token(old)
        assert rotated.refresh_token != old
        assert rotated.access_token
        # Old is now unusable
        with pytest.raises(MCPInvalidGrant):
            await service.rotate_refresh_token(old)

    async def test_missing_token_is_invalid_grant(self, service):
        with pytest.raises(MCPInvalidGrant):
            await service.rotate_refresh_token("bogus-token-value")

    async def test_scope_downscoping_preserves_scope_intersection(
        self, service, claude_client
    ):
        old = await self._mint_refresh(service, claude_client)
        rotated = await service.rotate_refresh_token(old, requested_scopes=["user_context"])
        assert rotated.scopes == ["user_context"]

    async def test_revoke_then_load_returns_none(self, service, claude_client):
        old = await self._mint_refresh(service, claude_client)
        await service.revoke_refresh_token_value(old)
        assert await service.load_refresh_token(old) is None


# ---------------------------------------------------------------------------
# Access token verification
# ---------------------------------------------------------------------------


class TestAccessTokenVerification:
    async def test_valid_token_is_decoded(self, service, claude_client):
        await service.save_client(claude_client)
        url = await service.build_consent_redirect(
            claude_client,
            AuthorizationParams(
                redirect_uri="https://claude.ai/api/mcp/auth_callback",
                code_challenge="c" * 32,
                scopes=["user_context"],
                resource="https://dev.alekbot.app/mcp",
            ),
        )
        req_jwt = url.split("req=")[1]
        code, _ = await service.issue_auth_code_for_consent(req_jwt, "user-1", "acc-1")
        issued = await service.finalize_auth_code(code)

        verified = service.verify_access_token(issued.access_token)
        assert verified is not None
        assert verified.user_id == "user-1"
        assert verified.account_id == "acc-1"
        assert verified.client_id == "mcp-claude-abc"
        assert "user_context" in verified.scopes

    async def test_wrong_audience_is_rejected(self, service):
        # Hand-crafted JWT with wrong aud
        bad = jwt.encode(
            {
                "sub": "u",
                "account_id": "a",
                "client_id": "c",
                "scope": "user_context",
                "iat": int(datetime.now(timezone.utc).timestamp()),
                "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
                "type": "mcp_access",
                "aud": "wrong-audience",
            },
            _TEST_SECRET,
            algorithm="HS256",
        )
        assert service.verify_access_token(bad) is None

    async def test_wrong_type_is_rejected(self, service):
        bad = jwt.encode(
            {
                "sub": "u",
                "account_id": "a",
                "client_id": "c",
                "scope": "user_context",
                "iat": int(datetime.now(timezone.utc).timestamp()),
                "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
                "type": "refresh",  # wrong type
                "aud": _TEST_RESOURCE_URI,
            },
            _TEST_SECRET,
            algorithm="HS256",
        )
        assert service.verify_access_token(bad) is None

    async def test_expired_token_is_rejected(self, service):
        bad = jwt.encode(
            {
                "sub": "u",
                "account_id": "a",
                "client_id": "c",
                "scope": "user_context",
                "iat": int((datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()),
                "exp": int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()),
                "type": "mcp_access",
                "aud": _TEST_RESOURCE_URI,
            },
            _TEST_SECRET,
            algorithm="HS256",
        )
        assert service.verify_access_token(bad) is None
