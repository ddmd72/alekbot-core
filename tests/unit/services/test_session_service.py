"""
Unit tests for SessionService (OAuth Multi-Tenant Session 4).

RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
"""
import pytest
import jwt
from datetime import datetime, timedelta, timezone

from src.services.session_service import SessionService
from src.domain.user import UserProfile
from src.domain.billing import BillingAccount, AccountTier


# ============================================================================
# Fixtures
# ============================================================================
@pytest.fixture
def session_service():
    """Create SessionService with test secret."""
    return SessionService(
        secret_key="test-secret-key-must-be-32-characters-minimum",
        access_token_ttl=3600,  # 1 hour
        refresh_token_ttl=86400,  # 1 day
    )


@pytest.fixture
def test_user():
    """Create test user."""
    return UserProfile(
        user_id="user-123",
        external_user_id="firebase|abc123",
        email="test@example.com",
        display_name="Test User",
        account_id="account-456",
    )


@pytest.fixture
def test_account():
    """Create test account."""
    return BillingAccount(
        account_id="account-456",
        tier=AccountTier.FREE,
        iam_policy={"user-123": "owner"},
    )


# ============================================================================
# Token Creation Tests
# ============================================================================
def test_create_access_token(session_service, test_user, test_account):
    """Test access token creation."""
    token = session_service.create_access_token(test_user, test_account)

    assert isinstance(token, str)
    assert len(token) > 50

    # Decode and verify payload
    payload = jwt.decode(
        token,
        session_service.secret_key,
        algorithms=[session_service.algorithm],
    )

    assert payload["sub"] == "user-123"
    assert payload["account_id"] == "account-456"
    assert payload["external_user_id"] == "firebase|abc123"
    assert payload["email"] == "test@example.com"
    assert payload["role"] == "owner"
    assert payload["tier"] == "free"
    assert payload["type"] == "access"
    assert "exp" in payload
    assert "iat" in payload


def test_create_refresh_token(session_service, test_user, test_account):
    """Test refresh token creation."""
    token = session_service.create_refresh_token(test_user, test_account)

    assert isinstance(token, str)

    # Decode and verify payload
    payload = jwt.decode(
        token,
        session_service.secret_key,
        algorithms=[session_service.algorithm],
    )

    assert payload["sub"] == "user-123"
    assert payload["account_id"] == "account-456"
    assert payload["type"] == "refresh"
    assert "exp" in payload
    assert "iat" in payload

    # Refresh token should not contain full user data
    assert "email" not in payload
    assert "role" not in payload


# ============================================================================
# Token Verification Tests
# ============================================================================
def test_verify_access_token_success(session_service, test_user, test_account):
    """Test successful access token verification."""
    token = session_service.create_access_token(test_user, test_account)

    payload = session_service.verify_access_token(token)

    assert payload["sub"] == "user-123"
    assert payload["type"] == "access"


def test_verify_refresh_token_success(session_service, test_user, test_account):
    """Test successful refresh token verification."""
    token = session_service.create_refresh_token(test_user, test_account)

    payload = session_service.verify_refresh_token(token)

    assert payload["sub"] == "user-123"
    assert payload["type"] == "refresh"


def test_verify_access_token_wrong_type(session_service, test_user, test_account):
    """Test access token verification rejects refresh tokens."""
    refresh_token = session_service.create_refresh_token(test_user, test_account)

    with pytest.raises(ValueError, match="Expected access token"):
        session_service.verify_access_token(refresh_token)


def test_verify_refresh_token_wrong_type(session_service, test_user, test_account):
    """Test refresh token verification rejects access tokens."""
    access_token = session_service.create_access_token(test_user, test_account)

    with pytest.raises(ValueError, match="Expected refresh token"):
        session_service.verify_refresh_token(access_token)


def test_verify_expired_token(session_service, test_user, test_account):
    """Test expired token verification raises ExpiredSignatureError."""
    # Create token with negative TTL (expired)
    expired_service = SessionService(
        secret_key="test-secret-key-must-be-32-characters-minimum",
        access_token_ttl=-3600,  # Expired 1 hour ago
    )

    token = expired_service.create_access_token(test_user, test_account)

    with pytest.raises(jwt.ExpiredSignatureError):
        session_service.verify_access_token(token)


def test_verify_invalid_signature(session_service, test_user, test_account):
    """Test token verification rejects invalid signature."""
    token = session_service.create_access_token(test_user, test_account)

    # Create different service with different secret
    other_service = SessionService(
        secret_key="different-secret-key-32-characters-min",
    )

    with pytest.raises(jwt.InvalidTokenError):
        other_service.verify_access_token(token)


def test_verify_malformed_token(session_service):
    """Test token verification rejects malformed tokens."""
    with pytest.raises(jwt.InvalidTokenError):
        session_service.verify_access_token("not-a-valid-jwt-token")


# ============================================================================
# Token Decoding Tests
# ============================================================================
def test_decode_token_unsafe(session_service, test_user, test_account):
    """Test unsafe token decoding (no verification)."""
    token = session_service.create_access_token(test_user, test_account)

    payload = session_service.decode_token_unsafe(token)

    assert payload is not None
    assert payload["sub"] == "user-123"


def test_decode_token_unsafe_expired(session_service, test_user, test_account):
    """Test unsafe decoding can read expired tokens."""
    expired_service = SessionService(
        secret_key="test-secret-key-must-be-32-characters-minimum",
        access_token_ttl=-3600,
    )

    token = expired_service.create_access_token(test_user, test_account)

    # Unsafe decode should succeed even for expired tokens
    payload = session_service.decode_token_unsafe(token)

    assert payload is not None
    assert payload["sub"] == "user-123"


def test_decode_token_unsafe_invalid(session_service):
    """Test unsafe decoding returns None for invalid tokens."""
    payload = session_service.decode_token_unsafe("not-a-valid-token")

    assert payload is None


# ============================================================================
# Security Tests
# ============================================================================
def test_secret_key_too_short():
    """Test service initialization rejects short secrets."""
    with pytest.raises(ValueError, match="JWT secret must be at least 32 characters"):
        SessionService(secret_key="short-secret")
