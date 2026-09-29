"""
Refresh tokens are age-capped by the CURRENT ttl, not only by their own exp.

Cabinet sessions are limited to 24h. Refresh tokens minted while the ttl was 30 days carry a
30-day exp; lowering the ttl must invalidate them immediately, not a month later.
"""
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from src.config.auth import AuthConfig
from src.services.session_service import SessionService

_SECRET = "test-secret-key-must-be-32-characters-minimum"


def _refresh_token(issued_ago: timedelta, lifetime: timedelta) -> str:
    iat = datetime.now(timezone.utc) - issued_ago
    return jwt.encode(
        {"sub": "user-123", "account_id": "account-456", "type": "refresh",
         "iat": int(iat.timestamp()), "exp": int((iat + lifetime).timestamp())},
        _SECRET, algorithm="HS256",
    )


def test_refresh_token_older_than_current_ttl_is_rejected_despite_valid_exp():
    service = SessionService(secret_key=_SECRET, refresh_token_ttl=86400)
    legacy = _refresh_token(issued_ago=timedelta(days=3), lifetime=timedelta(days=30))

    with pytest.raises(jwt.ExpiredSignatureError):
        service.verify_refresh_token(legacy)


def test_refresh_token_within_current_ttl_is_accepted():
    service = SessionService(secret_key=_SECRET, refresh_token_ttl=86400)
    fresh = _refresh_token(issued_ago=timedelta(hours=2), lifetime=timedelta(days=30))

    assert service.verify_refresh_token(fresh)["sub"] == "user-123"


def test_session_ttls_default_to_24_hours(monkeypatch):
    monkeypatch.delenv("ACCESS_TOKEN_TTL", raising=False)
    monkeypatch.delenv("REFRESH_TOKEN_TTL", raising=False)
    config = AuthConfig({})

    assert config.access_token_ttl == 86400
    assert config.refresh_token_ttl == 86400
