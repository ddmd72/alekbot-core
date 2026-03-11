"""
Unit tests for FirebaseAuthAdapter (OAuth Multi-Tenant Session 3).

Tests Firebase OAuth adapter implementation without network calls.
Mocks Firebase Admin SDK and aiohttp for isolated testing.

RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
"""
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch, MagicMock

from src.adapters.firebase_auth_adapter import FirebaseAuthAdapter
from src.ports.auth_port import TokenClaims, OAuthTokens, OAuthUserInfo


# ============================================================================
# OAuth Multi-Tenant Session 3: FirebaseAuthAdapter Basic Tests
# ============================================================================
def test_firebase_adapter_provider_name():
    """Test provider name is 'firebase'."""
    adapter = FirebaseAuthAdapter(
        project_id="test-project",
        web_api_key="test-key"
    )
    assert adapter.get_provider_name() == "firebase"


def test_firebase_adapter_authorization_url():
    """Test OAuth authorization URL generation."""
    adapter = FirebaseAuthAdapter(
        project_id="test-project",
        web_api_key="test-key"
    )

    url = adapter.get_authorization_url(
        state="test-state-token",
        redirect_uri="http://localhost:8080/auth/callback"
    )

    # Verify URL structure
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=test-project.apps.googleusercontent.com" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8080%2Fauth%2Fcallback" in url
    assert "response_type=code" in url
    assert "scope=openid+email+profile" in url
    assert "state=test-state-token" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url


# ============================================================================
# OAuth Multi-Tenant Session 3: Token Exchange Tests (Mocked)
# ============================================================================
@pytest.mark.asyncio
async def test_firebase_exchange_code_for_tokens_success():
    """Test successful authorization code exchange."""
    adapter = FirebaseAuthAdapter(
        project_id="test-project",
        web_api_key="test-key"
    )

    # Mock successful token response — must be sync context manager (not AsyncMock)
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={
        "access_token": "test-access-token",
        "id_token": "test-id-token",
        "refresh_token": "test-refresh-token",
        "expires_in": 3600,
        "token_type": "Bearer"
    })
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.post = MagicMock(return_value=mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        tokens = await adapter.exchange_code_for_tokens(
            code="test-auth-code",
            redirect_uri="http://localhost:8080/auth/callback"
        )

        assert tokens.access_token == "test-access-token"
        assert tokens.id_token == "test-id-token"
        assert tokens.refresh_token == "test-refresh-token"
        assert tokens.expires_in == 3600
        assert tokens.token_type == "Bearer"


@pytest.mark.asyncio
async def test_firebase_exchange_code_for_tokens_failure():
    """Test failed authorization code exchange."""
    adapter = FirebaseAuthAdapter(
        project_id="test-project",
        web_api_key="test-key"
    )

    # Mock failed token response
    mock_response = MagicMock()
    mock_response.status = 400
    mock_response.text = AsyncMock(return_value="Invalid authorization code")
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.post = MagicMock(return_value=mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(ValueError, match="Token exchange failed"):
            await adapter.exchange_code_for_tokens(
                code="invalid-code",
                redirect_uri="http://localhost:8080/auth/callback"
            )


# ============================================================================
# OAuth Multi-Tenant Session 3: Token Verification Tests (Mocked)
# ============================================================================
@pytest.mark.asyncio
async def test_firebase_verify_token_success():
    """Test successful ID token verification."""
    adapter = FirebaseAuthAdapter(
        project_id="test-project",
        web_api_key="test-key"
    )

    # Mock Firebase Admin SDK verify_id_token
    exp_time = datetime.now() + timedelta(hours=1)
    iat_time = datetime.now()

    mock_decoded_token = {
        "sub": "firebase-user-123",
        "iss": "https://securetoken.google.com/test-project",
        "aud": "test-project",
        "exp": int(exp_time.timestamp()),
        "iat": int(iat_time.timestamp()),
        "email": "test@example.com",
        "email_verified": True,
        "name": "Test User",
        "picture": "https://example.com/photo.jpg",
    }

    with patch("firebase_admin.auth.verify_id_token", return_value=mock_decoded_token):
        claims = await adapter.verify_token("test-id-token")

        assert claims.sub == "firebase-user-123"
        assert claims.iss == "https://securetoken.google.com/test-project"
        assert claims.aud == "test-project"
        assert claims.email == "test@example.com"
        assert claims.email_verified is True
        assert claims.name == "Test User"
        assert claims.picture == "https://example.com/photo.jpg"


@pytest.mark.asyncio
async def test_firebase_verify_token_failure():
    """Test failed ID token verification (expired, invalid signature)."""
    adapter = FirebaseAuthAdapter(
        project_id="test-project",
        web_api_key="test-key"
    )

    # Mock Firebase Admin SDK raising FirebaseError
    from firebase_admin.exceptions import FirebaseError

    with patch("firebase_admin.auth.verify_id_token", side_effect=FirebaseError("INVALID_TOKEN", "Token expired")):
        with pytest.raises(ValueError, match="Invalid ID token"):
            await adapter.verify_token("expired-token")


# ============================================================================
# OAuth Multi-Tenant Session 3: UserInfo Endpoint Tests (Mocked)
# ============================================================================
@pytest.mark.asyncio
async def test_firebase_get_user_info_success():
    """Test successful UserInfo retrieval from Google."""
    adapter = FirebaseAuthAdapter(
        project_id="test-project",
        web_api_key="test-key"
    )

    # Mock successful UserInfo response
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={
        "sub": "google-user-123",
        "email": "test@example.com",
        "email_verified": True,
        "name": "Test User",
        "given_name": "Test",
        "family_name": "User",
        "picture": "https://example.com/photo.jpg",
        "locale": "en"
    })
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = MagicMock(return_value=mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        user_info = await adapter.get_user_info("test-access-token")

        assert user_info.sub == "google-user-123"
        assert user_info.email == "test@example.com"
        assert user_info.email_verified is True
        assert user_info.name == "Test User"
        assert user_info.given_name == "Test"
        assert user_info.family_name == "User"
        assert user_info.picture == "https://example.com/photo.jpg"
        assert user_info.locale == "en"


@pytest.mark.asyncio
async def test_firebase_get_user_info_failure():
    """Test failed UserInfo retrieval (invalid token)."""
    adapter = FirebaseAuthAdapter(
        project_id="test-project",
        web_api_key="test-key"
    )

    # Mock failed UserInfo response
    mock_response = MagicMock()
    mock_response.status = 401
    mock_response.text = AsyncMock(return_value="Invalid access token")
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = MagicMock(return_value=mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(ValueError, match="UserInfo request failed"):
            await adapter.get_user_info("invalid-token")


# ============================================================================
# OAuth Multi-Tenant Session 3: Token Refresh Tests (Mocked)
# ============================================================================
@pytest.mark.asyncio
async def test_firebase_refresh_access_token_success():
    """Test successful access token refresh."""
    adapter = FirebaseAuthAdapter(
        project_id="test-project",
        web_api_key="test-key"
    )

    # Mock successful token refresh response
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={
        "access_token": "new-access-token",
        "id_token": "new-id-token",
        "expires_in": "3600",
        "token_type": "Bearer"
    })
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.post = MagicMock(return_value=mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        tokens = await adapter.refresh_access_token("test-refresh-token")

        assert tokens.access_token == "new-access-token"
        assert tokens.id_token == "new-id-token"
        assert tokens.expires_in == 3600
        assert tokens.token_type == "Bearer"


@pytest.mark.asyncio
async def test_firebase_refresh_access_token_failure():
    """Test failed token refresh (invalid refresh token)."""
    adapter = FirebaseAuthAdapter(
        project_id="test-project",
        web_api_key="test-key"
    )

    # Mock failed token refresh response
    mock_response = MagicMock()
    mock_response.status = 400
    mock_response.text = AsyncMock(return_value="Invalid refresh token")
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.post = MagicMock(return_value=mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(ValueError, match="Token refresh failed"):
            await adapter.refresh_access_token("invalid-refresh-token")
