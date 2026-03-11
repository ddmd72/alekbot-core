"""
Unit tests for auth domain models — TokenClaims, OAuthTokens, OAuthUserInfo, IAMDecision.

Verifies Pydantic validation, field defaults, and IAMDecision dataclass behaviour.
"""
from datetime import datetime, timezone

import pytest

from src.domain.auth import TokenClaims, OAuthTokens, OAuthUserInfo, IAMDecision


class TestTokenClaims:
    def test_required_fields(self):
        now = datetime.now(timezone.utc)
        claims = TokenClaims(sub="uid", iss="https://iss", aud="aud", exp=now, iat=now)
        assert claims.sub == "uid"
        assert claims.iss == "https://iss"

    def test_optional_fields_default_none(self):
        now = datetime.now(timezone.utc)
        claims = TokenClaims(sub="u", iss="i", aud="a", exp=now, iat=now)
        assert claims.email is None
        assert claims.email_verified is None
        assert claims.name is None
        assert claims.custom_claims == {}

    def test_missing_required_field_raises(self):
        with pytest.raises(Exception):
            TokenClaims(iss="i", aud="a", exp=datetime.now(timezone.utc), iat=datetime.now(timezone.utc))


class TestOAuthTokens:
    def test_required_fields(self):
        t = OAuthTokens(access_token="at", id_token="it", expires_in=3600)
        assert t.access_token == "at"
        assert t.token_type == "Bearer"
        assert t.refresh_token is None

    def test_custom_token_type(self):
        t = OAuthTokens(access_token="at", id_token="it", expires_in=100, token_type="MAC")
        assert t.token_type == "MAC"


class TestOAuthUserInfo:
    def test_required_sub(self):
        u = OAuthUserInfo(sub="user123")
        assert u.sub == "user123"
        assert u.email is None
        assert u.provider_metadata == {}

    def test_all_fields(self):
        u = OAuthUserInfo(
            sub="s",
            email="a@b.com",
            email_verified=True,
            name="Alice",
            given_name="Alice",
            family_name="Smith",
            locale="en",
            provider_metadata={"key": "val"},
        )
        assert u.email == "a@b.com"
        assert u.provider_metadata == {"key": "val"}


class TestIAMDecision:
    def test_allow_action(self):
        d = IAMDecision(action="allow")
        assert d.action == "allow"
        assert d.user is None
        assert d.message is None
        assert d.metadata == {}

    def test_reject_with_message(self):
        d = IAMDecision(action="reject", message="Not authorized")
        assert d.action == "reject"
        assert d.message == "Not authorized"

    def test_metadata_default_is_empty_dict(self):
        d1 = IAMDecision(action="allow")
        d2 = IAMDecision(action="reject")
        # Each instance gets its own dict (dataclass field default_factory)
        d1.metadata["key"] = "val"
        assert d2.metadata == {}
