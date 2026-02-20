"""
Unit tests for AuthenticationService (OAuth Multi-Tenant Session 4).

RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
"""
import pytest
from unittest.mock import AsyncMock, Mock, patch
from datetime import datetime

from src.services.authentication_service import AuthenticationService
from src.services.auth_provider_registry import AuthProviderRegistry
from src.ports.auth_port import TokenClaims, OAuthTokens, OAuthUserInfo
from src.domain.user import UserProfile, UserBotConfig
from src.domain.billing import BillingAccount, AccountTier


# ============================================================================
# Fixtures
# ============================================================================
@pytest.fixture
def mock_auth_registry():
    """Mock AuthProviderRegistry."""
    registry = Mock(spec=AuthProviderRegistry)

    mock_provider = Mock()
    mock_provider.get_provider_name.return_value = "firebase"
    registry.get_provider.return_value = mock_provider

    return registry, mock_provider


@pytest.fixture
def mock_user_repo():
    """Mock UserRepository."""
    return Mock()


@pytest.fixture
def mock_account_repo():
    """Mock AccountRepository."""
    return Mock()


@pytest.fixture
def auth_service(mock_auth_registry, mock_user_repo, mock_account_repo):
    """Create AuthenticationService with mocked dependencies."""
    registry, _ = mock_auth_registry
    return AuthenticationService(registry, mock_user_repo, mock_account_repo)


# ============================================================================
# OAuth Callback Tests
# ============================================================================
@pytest.mark.asyncio
async def test_handle_oauth_callback_existing_user(auth_service, mock_auth_registry, mock_user_repo, mock_account_repo):
    """Test OAuth callback with existing user."""
    _, mock_provider = mock_auth_registry

    # Mock OAuth provider responses
    mock_provider.exchange_code_for_tokens = AsyncMock(return_value=OAuthTokens(
        access_token="test-access",
        id_token="test-id",
        expires_in=3600,
    ))

    mock_provider.verify_token = AsyncMock(return_value=TokenClaims(
        sub="firebase-123",
        iss="https://securetoken.google.com/test",
        aud="test",
        exp=datetime.now(),
        iat=datetime.now(),
        email="test@example.com",
    ))

    mock_provider.get_user_info = AsyncMock(return_value=OAuthUserInfo(
        sub="firebase-123",
        email="test@example.com",
        name="Test User",
    ))

    # Mock existing user
    existing_user = UserProfile(
        user_id="user-1",
        external_user_id="firebase|firebase-123",
        email="test@example.com",
        display_name="Test User",
        account_id="account-1",
    )

    mock_user_repo.get_user_by_external_id = AsyncMock(return_value=existing_user)
    mock_user_repo.update_user = AsyncMock(return_value=existing_user)

    # Mock account
    mock_account = BillingAccount(account_id="account-1", tier=AccountTier.FREE, iam_policy={"user-1": "owner"})
    mock_account_repo.get_account = AsyncMock(return_value=mock_account)

    # Call handle_oauth_callback
    user, account, tokens = await auth_service.handle_oauth_callback(
        code="test-code",
        redirect_uri="http://localhost/callback",
    )

    assert user.user_id == "user-1"
    assert account.account_id == "account-1"
    assert tokens.access_token == "test-access"

    mock_user_repo.update_user.assert_called_once()


@pytest.mark.asyncio
async def test_handle_oauth_callback_new_user(auth_service, mock_auth_registry, mock_user_repo, mock_account_repo):
    """Test OAuth callback with new user registration."""
    _, mock_provider = mock_auth_registry

    # Mock OAuth provider responses
    mock_provider.exchange_code_for_tokens = AsyncMock(return_value=OAuthTokens(
        access_token="test-access",
        id_token="test-id",
        expires_in=3600,
    ))

    mock_provider.verify_token = AsyncMock(return_value=TokenClaims(
        sub="firebase-456",
        iss="https://securetoken.google.com/test",
        aud="test",
        exp=datetime.now(),
        iat=datetime.now(),
        email="newuser@example.com",
    ))

    mock_provider.get_user_info = AsyncMock(return_value=OAuthUserInfo(
        sub="firebase-456",
        email="newuser@example.com",
        name="New User",
    ))

    # Mock no existing user
    mock_user_repo.get_user_by_external_id = AsyncMock(return_value=None)
    mock_user_repo.get_user_by_email = AsyncMock(return_value=None)  # no email collision

    # Mock user/account creation
    new_user = UserProfile(user_id="user-2", external_user_id="firebase|firebase-456", email="newuser@example.com", account_id="account-2")
    new_account = BillingAccount(account_id="account-2", tier=AccountTier.FREE, iam_policy={"user-2": "owner"})

    mock_account_repo.create_account = AsyncMock(return_value=new_account)
    mock_user_repo.create_user = AsyncMock(return_value=new_user)
    mock_account_repo.get_account = AsyncMock(return_value=new_account)

    # Call handle_oauth_callback
    user, account, tokens = await auth_service.handle_oauth_callback(
        code="test-code",
        redirect_uri="http://localhost/callback",
    )

    assert user.user_id == "user-2"
    assert account.account_id == "account-2"

    mock_account_repo.create_account.assert_called_once()
    mock_user_repo.create_user.assert_called_once()


# ============================================================================
# User Registration Tests
# ============================================================================
@pytest.mark.asyncio
async def test_register_new_user(auth_service, mock_user_repo, mock_account_repo):
    """Test new user registration creates account and user."""
    user_info = OAuthUserInfo(
        sub="firebase-789",
        email="test@example.com",
        name="Test User",
    )

    claims = TokenClaims(
        sub="firebase-789",
        iss="https://securetoken.google.com/test",
        aud="test",
        exp=datetime.now(),
        iat=datetime.now(),
        email="test@example.com",
    )

    # Mock account/user creation
    mock_account_repo.create_account = AsyncMock(return_value=Mock(account_id="account-3"))
    mock_user_repo.create_user = AsyncMock(return_value=Mock(user_id="user-3"))

    # Call register_new_user
    user = await auth_service.register_new_user(
        external_user_id="firebase|firebase-789",
        user_info=user_info,
        claims=claims,
    )

    assert user.user_id == "user-3"

    mock_account_repo.create_account.assert_called_once()
    mock_user_repo.create_user.assert_called_once()


# ============================================================================
# Platform Linking Tests
# ============================================================================
@pytest.mark.asyncio
async def test_link_platform_identity(auth_service, mock_user_repo):
    """Test linking Slack/Telegram identity to existing user."""
    updated_user = UserProfile(
        user_id="user-1",
        external_user_id="firebase|abc",
        platform_identities={"slack": "U123456"},
    )

    mock_user_repo.link_platform_identity = AsyncMock(return_value=updated_user)

    user = await auth_service.link_platform_identity(
        user_id="user-1",
        platform="slack",
        platform_user_id="U123456",
    )

    assert user.platform_identities["slack"] == "U123456"
    mock_user_repo.link_platform_identity.assert_called_once_with(
        user_id="user-1", platform="slack", platform_user_id="U123456"
    )
