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


SECRET = "test-secret-key-must-be-32-characters-minimum"


def _signed(user, account, *, token_type="access", ttl=timedelta(minutes=15), sid=None):
    """A JWT with the claims of a session token, signed with the test secret: the legacy access and refresh token
    shapes no longer have a minting method, and these tests only need their bytes."""
    now = datetime.now(timezone.utc)
    claims = {"sub": user.user_id, "iat": int(now.timestamp()), "exp": int((now + ttl).timestamp()),
              "account_id": account.account_id, "type": token_type}
    if sid is not None:
        claims["sid"] = sid
    return jwt.encode(claims, SECRET, algorithm="HS256")


# ============================================================================
# Fixtures
# ============================================================================
@pytest.fixture
def session_service():
    """Create SessionService with test secret."""
    return SessionService(
        secret_key="test-secret-key-must-be-32-characters-minimum",
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
# Token Decoding Tests
# ============================================================================
def test_decode_token_unsafe(session_service, test_user, test_account):
    """Test unsafe token decoding (no verification)."""
    token = _signed(test_user, test_account)

    payload = session_service.decode_token_unsafe(token)

    assert payload is not None
    assert payload["sub"] == "user-123"


def test_decode_token_unsafe_expired(session_service, test_user, test_account):
    """Test unsafe decoding can read expired tokens."""
    token = _signed(test_user, test_account, ttl=timedelta(seconds=-3600))

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


# ============================================================================
# Device-session access tokens (CABINET_DEVICE_SESSIONS_RFC §7.1)
# ============================================================================
class TestDeviceAccessToken:
    def test_the_token_carries_the_usual_claims_and_the_sid_for_the_access_ttl(self, session_service, test_user,
                                                                               test_account):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        token = session_service.create_device_access_token(test_user, test_account, "sid-1", now)
        payload = session_service.verify_device_access_token(token)
        assert {k: payload[k] for k in ("sub", "account_id", "external_user_id", "role", "tier", "email", "type",
                                        "sid")} == {
            "sub": "user-123", "account_id": "account-456", "external_user_id": "firebase|abc123", "role": "owner",
            "tier": "free", "email": "test@example.com", "type": "access", "sid": "sid-1"}
        assert (payload["iat"], payload["exp"]) == (int(now.timestamp()), int((now + timedelta(minutes=15)).timestamp()))

    def test_an_expired_token_gives_its_sid_only_when_asked(self, session_service, test_user, test_account):
        minted = datetime.now(timezone.utc) - timedelta(minutes=16)
        token = session_service.create_device_access_token(test_user, test_account, "sid-1", minted)
        with pytest.raises(jwt.ExpiredSignatureError):
            session_service.verify_device_access_token(token)
        assert session_service.verify_device_access_token(token, allow_expired=True)["sid"] == "sid-1"

    def test_an_expired_token_with_a_wrong_signature_is_refused_even_when_asked(self, test_user, test_account):
        other = SessionService(secret_key="another-secret-key-that-is-32-characters-long")
        minted = datetime.now(timezone.utc) - timedelta(hours=1)
        token = other.create_device_access_token(test_user, test_account, "sid-1", minted)
        ours = SessionService(secret_key="test-secret-key-must-be-32-characters-minimum")
        with pytest.raises(jwt.InvalidSignatureError):
            ours.verify_device_access_token(token, allow_expired=True)

    def test_a_token_without_a_sid_or_of_another_type_is_refused(self, session_service, test_user, test_account):
        with pytest.raises(ValueError, match="no session id"):
            session_service.verify_device_access_token(_signed(test_user, test_account))
        with pytest.raises(ValueError, match="Expected access token"):
            session_service.verify_device_access_token(
                _signed(test_user, test_account, token_type="refresh"))
