"""
Translation tests for MCPSdkOAuthProvider — the adapter between the
`mcp` Python SDK's OAuthAuthorizationServerProvider protocol and our
SDK-free MCPAuthorizationService.

Scope of these tests: SDK <-> domain type translation and delegation.
The actual OAuth business logic is tested in
`tests/unit/services/test_mcp_authorization_service.py`.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from mcp.server.auth.provider import (
    AuthorizationParams as SDKAuthorizationParams,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from src.composition.mcp_sdk_oauth_provider import (
    AlekAccessToken,
    AlekAuthorizationCode,
    AlekRefreshToken,
    MCPSdkOAuthProvider,
    _domain_to_sdk_client,
    _sdk_to_domain_client,
)
from src.domain.mcp import MCPAuthCode, MCPClient, MCPRefreshToken
from src.services.mcp_authorization_service import (
    IssuedToken,
    MCPInvalidGrant,
    MCPInvalidRedirectURI,
    VerifiedAccessToken,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_service():
    svc = AsyncMock()
    svc.verify_access_token = lambda t: None  # overridden per test
    return svc


@pytest.fixture
def provider(mock_service):
    return MCPSdkOAuthProvider(mock_service)


@pytest.fixture
def domain_client() -> MCPClient:
    return MCPClient(
        client_id="mcp-test-123",
        client_secret="secret-plaintext",
        client_secret_expires_at=None,
        client_name="Claude",
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="user_context",
        token_endpoint_auth_method="client_secret_post",
        created_at=datetime(2026, 4, 14, 12, 0, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def sdk_client(domain_client) -> OAuthClientInformationFull:
    return _domain_to_sdk_client(domain_client)


# ---------------------------------------------------------------------------
# Translation helpers
# ---------------------------------------------------------------------------


class TestClientTranslation:
    def test_domain_to_sdk_roundtrip(self, domain_client):
        sdk = _domain_to_sdk_client(domain_client)
        back = _sdk_to_domain_client(sdk)
        assert back.client_id == domain_client.client_id
        assert back.client_secret == domain_client.client_secret
        assert back.client_name == domain_client.client_name
        assert [str(u) for u in back.redirect_uris] == domain_client.redirect_uris
        assert back.grant_types == domain_client.grant_types
        assert back.scope == domain_client.scope
        assert back.token_endpoint_auth_method == domain_client.token_endpoint_auth_method

    def test_domain_to_sdk_uses_created_at_as_issued_at(self, domain_client):
        sdk = _domain_to_sdk_client(domain_client)
        assert sdk.client_id_issued_at == int(domain_client.created_at.timestamp())


# ---------------------------------------------------------------------------
# get_client / register_client
# ---------------------------------------------------------------------------


class TestGetClient:
    @pytest.mark.asyncio
    async def test_returns_sdk_shape_on_hit(self, provider, mock_service, domain_client):
        mock_service.get_client = AsyncMock(return_value=domain_client)
        result = await provider.get_client("mcp-test-123")
        assert isinstance(result, OAuthClientInformationFull)
        assert result.client_id == "mcp-test-123"
        assert result.client_secret == "secret-plaintext"

    @pytest.mark.asyncio
    async def test_returns_none_on_miss(self, provider, mock_service):
        mock_service.get_client = AsyncMock(return_value=None)
        assert await provider.get_client("missing") is None


class TestRegisterClient:
    @pytest.mark.asyncio
    async def test_delegates_to_service(self, provider, mock_service, sdk_client):
        mock_service.save_client = AsyncMock()
        await provider.register_client(sdk_client)
        mock_service.save_client.assert_awaited_once()
        (saved,), _ = mock_service.save_client.call_args
        assert saved.client_id == sdk_client.client_id

    @pytest.mark.asyncio
    async def test_maps_invalid_redirect_to_registration_error(
        self, provider, mock_service, sdk_client
    ):
        mock_service.save_client = AsyncMock(
            side_effect=MCPInvalidRedirectURI("bad host")
        )
        with pytest.raises(RegistrationError) as exc:
            await provider.register_client(sdk_client)
        assert exc.value.error == "invalid_redirect_uri"


# ---------------------------------------------------------------------------
# authorize
# ---------------------------------------------------------------------------


class TestAuthorize:
    @pytest.mark.asyncio
    async def test_delegates_and_returns_redirect_url(
        self, provider, mock_service, sdk_client
    ):
        mock_service.build_consent_redirect = AsyncMock(
            return_value="https://dev.alekbot.app/mcp/consent?req=JWT"
        )
        sdk_params = SDKAuthorizationParams(
            state="xyz",
            scopes=["user_context"],
            code_challenge="c" * 43,
            redirect_uri=AnyUrl("https://claude.ai/api/mcp/auth_callback"),
            redirect_uri_provided_explicitly=True,
            resource="https://dev.alekbot.app/mcp",
        )
        url = await provider.authorize(sdk_client, sdk_params)
        assert url == "https://dev.alekbot.app/mcp/consent?req=JWT"
        mock_service.build_consent_redirect.assert_awaited_once()
        (domain_client_arg, domain_params_arg), _ = mock_service.build_consent_redirect.call_args
        assert domain_client_arg.client_id == sdk_client.client_id
        assert domain_params_arg.code_challenge == sdk_params.code_challenge
        assert domain_params_arg.state == "xyz"
        assert domain_params_arg.code_challenge_method == "S256"


# ---------------------------------------------------------------------------
# Authorization codes
# ---------------------------------------------------------------------------


class TestAuthCodeTranslation:
    @pytest.mark.asyncio
    async def test_load_returns_alek_subclass_with_user_id(
        self, provider, mock_service, sdk_client
    ):
        code = MCPAuthCode(
            code="code-abc",
            client_id=sdk_client.client_id,
            user_id="user-1",
            account_id="acc-1",
            redirect_uri="https://claude.ai/api/mcp/auth_callback",
            code_challenge="c" * 43,
            resource="https://dev.alekbot.app/mcp",
            scopes=["user_context"],
            expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        )
        mock_service.load_auth_code = AsyncMock(return_value=code)

        result = await provider.load_authorization_code(sdk_client, "code-abc")
        assert isinstance(result, AlekAuthorizationCode)
        assert result.user_id == "user-1"
        assert result.account_id == "acc-1"
        assert result.code == "code-abc"
        assert result.client_id == sdk_client.client_id

    @pytest.mark.asyncio
    async def test_load_returns_none_if_wrong_client(
        self, provider, mock_service, sdk_client
    ):
        code = MCPAuthCode(
            code="code-abc",
            client_id="some-other-client",
            user_id="u",
            account_id="a",
            redirect_uri="https://claude.ai/api/mcp/auth_callback",
            code_challenge="c" * 43,
            scopes=["user_context"],
            expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        )
        mock_service.load_auth_code = AsyncMock(return_value=code)
        assert await provider.load_authorization_code(sdk_client, "code-abc") is None

    @pytest.mark.asyncio
    async def test_exchange_maps_invalid_grant(
        self, provider, mock_service, sdk_client
    ):
        alek_code = AlekAuthorizationCode(
            code="code-abc",
            scopes=["user_context"],
            expires_at=4102444800,  # 2100-01-01
            client_id=sdk_client.client_id,
            code_challenge="c" * 43,
            redirect_uri=AnyUrl("https://claude.ai/api/mcp/auth_callback"),
            redirect_uri_provided_explicitly=True,
            resource="https://dev.alekbot.app/mcp",
            user_id="u",
            account_id="a",
        )
        mock_service.finalize_auth_code = AsyncMock(
            side_effect=MCPInvalidGrant("code already used")
        )
        with pytest.raises(TokenError) as exc:
            await provider.exchange_authorization_code(sdk_client, alek_code)
        assert exc.value.error == "invalid_grant"

    @pytest.mark.asyncio
    async def test_exchange_returns_oauth_token(
        self, provider, mock_service, sdk_client
    ):
        alek_code = AlekAuthorizationCode(
            code="code-abc",
            scopes=["user_context"],
            expires_at=4102444800,
            client_id=sdk_client.client_id,
            code_challenge="c" * 43,
            redirect_uri=AnyUrl("https://claude.ai/api/mcp/auth_callback"),
            redirect_uri_provided_explicitly=True,
            resource="https://dev.alekbot.app/mcp",
            user_id="u",
            account_id="a",
        )
        mock_service.finalize_auth_code = AsyncMock(
            return_value=IssuedToken(
                access_token="access-jwt",
                refresh_token="refresh-value",
                expires_in=3600,
                scopes=["user_context"],
                resource="https://dev.alekbot.app/mcp",
            )
        )
        token = await provider.exchange_authorization_code(sdk_client, alek_code)
        assert token.access_token == "access-jwt"
        assert token.refresh_token == "refresh-value"
        assert token.expires_in == 3600
        assert token.scope == "user_context"
        assert token.token_type == "Bearer"


# ---------------------------------------------------------------------------
# Refresh tokens
# ---------------------------------------------------------------------------


class TestRefreshTokenTranslation:
    @pytest.mark.asyncio
    async def test_load_returns_alek_subclass(
        self, provider, mock_service, sdk_client
    ):
        domain_token = MCPRefreshToken(
            token_hash="hash",
            client_id=sdk_client.client_id,
            user_id="u",
            account_id="a",
            scopes=["user_context"],
            resource="https://dev.alekbot.app/mcp",
            expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        )
        mock_service.load_refresh_token = AsyncMock(return_value=domain_token)
        result = await provider.load_refresh_token(sdk_client, "refresh-plain")
        assert isinstance(result, AlekRefreshToken)
        assert result.user_id == "u"
        assert result.account_id == "a"
        assert result.token == "refresh-plain"

    @pytest.mark.asyncio
    async def test_exchange_returns_oauth_token(
        self, provider, mock_service, sdk_client
    ):
        refresh = AlekRefreshToken(
            token="refresh-plain",
            client_id=sdk_client.client_id,
            scopes=["user_context"],
            expires_at=4102444800,
            user_id="u",
            account_id="a",
        )
        mock_service.rotate_refresh_token = AsyncMock(
            return_value=IssuedToken(
                access_token="new-access",
                refresh_token="new-refresh",
                expires_in=3600,
                scopes=["user_context"],
                resource="https://dev.alekbot.app/mcp",
            )
        )
        token = await provider.exchange_refresh_token(sdk_client, refresh, ["user_context"])
        assert token.access_token == "new-access"
        assert token.refresh_token == "new-refresh"


# ---------------------------------------------------------------------------
# Access token (bearer middleware)
# ---------------------------------------------------------------------------


class TestAccessTokenLoad:
    @pytest.mark.asyncio
    async def test_load_returns_alek_access_token(self, provider, mock_service):
        mock_service.verify_access_token = lambda t: VerifiedAccessToken(
            token=t,
            client_id="mcp-test",
            user_id="user-42",
            account_id="acc-42",
            scopes=["user_context"],
            resource="https://dev.alekbot.app/mcp",
            expires_at=4102444800,
        )
        result = await provider.load_access_token("jwt-here")
        assert isinstance(result, AlekAccessToken)
        assert result.user_id == "user-42"
        assert result.account_id == "acc-42"
        assert result.client_id == "mcp-test"

    @pytest.mark.asyncio
    async def test_load_returns_none_on_invalid(self, provider, mock_service):
        mock_service.verify_access_token = lambda t: None
        assert await provider.load_access_token("bad-jwt") is None


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------


class TestRevokeToken:
    @pytest.mark.asyncio
    async def test_revokes_refresh_tokens(self, provider, mock_service, sdk_client):
        mock_service.revoke_refresh_token_value = AsyncMock()
        token = AlekRefreshToken(
            token="refresh-plain",
            client_id=sdk_client.client_id,
            scopes=["user_context"],
            expires_at=4102444800,
            user_id="u",
            account_id="a",
        )
        await provider.revoke_token(token)
        mock_service.revoke_refresh_token_value.assert_awaited_once_with("refresh-plain")

    @pytest.mark.asyncio
    async def test_access_token_revocation_is_noop(self, provider, mock_service):
        mock_service.revoke_refresh_token_value = AsyncMock()
        access = AlekAccessToken(
            token="jwt",
            client_id="mcp-test",
            scopes=["user_context"],
            expires_at=4102444800,
            resource="https://dev.alekbot.app/mcp",
            user_id="u",
            account_id="a",
        )
        await provider.revoke_token(access)
        mock_service.revoke_refresh_token_value.assert_not_awaited()
