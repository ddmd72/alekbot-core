"""
Unit tests for FileAccessTokenService — capability tokens gating private files.
"""
import jwt
import pytest
from datetime import datetime, timedelta, timezone

from src.services.file_access_token_service import (
    FileAccessTokenService,
    FileAccessToken,
    FileAccessTokenInvalid,
    FileAccessTokenExpired,
)


_SECRET = "x" * 40  # >= 32 chars
_OTHER_SECRET = "y" * 40


@pytest.fixture
def svc():
    return FileAccessTokenService(secret_key=_SECRET)


class TestConstruction:
    def test_rejects_short_secret(self):
        with pytest.raises(ValueError, match="at least 32"):
            FileAccessTokenService(secret_key="too-short")

    def test_rejects_empty_secret(self):
        with pytest.raises(ValueError):
            FileAccessTokenService(secret_key="")


class TestMintVerifyRoundTrip:
    def test_round_trip_returns_payload(self, svc):
        token = svc.mint(key="docs/abc-report.pdf", user_id="user-1")
        decoded = svc.verify(token)
        assert isinstance(decoded, FileAccessToken)
        assert decoded.key == "docs/abc-report.pdf"
        assert decoded.user_id == "user-1"
        assert decoded.gated is False

    def test_gated_flag_round_trips(self, svc):
        token = svc.mint(key="email_review/r.html", user_id="u", gated=True)
        assert svc.verify(token).gated is True

    def test_default_ttl_not_expired(self, svc):
        token = svc.mint(key="k", user_id="u")  # default 30d
        svc.verify(token)  # must not raise


class TestExpiry:
    def test_expired_token_rejected(self, svc):
        token = svc.mint(key="k", user_id="u", ttl_seconds=-1)
        with pytest.raises(FileAccessTokenExpired):
            svc.verify(token)


class TestTampering:
    def test_wrong_secret_rejected(self, svc):
        token = svc.mint(key="k", user_id="u")
        other = FileAccessTokenService(secret_key=_OTHER_SECRET)
        with pytest.raises(FileAccessTokenInvalid):
            other.verify(token)

    def test_malformed_token_rejected(self, svc):
        with pytest.raises(FileAccessTokenInvalid):
            svc.verify("not-a-jwt")

    def test_wrong_type_rejected(self, svc):
        # A validly-signed JWT that is not a file_access token must be refused.
        now = datetime.now(timezone.utc)
        foreign = jwt.encode(
            {
                "key": "k", "uid": "u", "type": "access",
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(hours=1)).timestamp()),
            },
            _SECRET, algorithm="HS256",
        )
        with pytest.raises(FileAccessTokenInvalid, match="wrong token type"):
            svc.verify(foreign)

    def test_missing_key_claim_rejected(self, svc):
        now = datetime.now(timezone.utc)
        bad = jwt.encode(
            {
                "uid": "u", "type": "file_access",
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(hours=1)).timestamp()),
            },
            _SECRET, algorithm="HS256",
        )
        with pytest.raises(FileAccessTokenInvalid, match="missing key/uid"):
            svc.verify(bad)

    def test_missing_uid_claim_rejected(self, svc):
        now = datetime.now(timezone.utc)
        bad = jwt.encode(
            {
                "key": "k", "type": "file_access",
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(hours=1)).timestamp()),
            },
            _SECRET, algorithm="HS256",
        )
        with pytest.raises(FileAccessTokenInvalid, match="missing key/uid"):
            svc.verify(bad)


class TestTtlConstants:
    def test_email_review_ttl_shorter_than_default(self):
        assert FileAccessTokenService.EMAIL_REVIEW_TTL < FileAccessTokenService.DEFAULT_TTL
        assert FileAccessTokenService.EMAIL_REVIEW_TTL == 5 * 24 * 3600
        assert FileAccessTokenService.DEFAULT_TTL == 30 * 24 * 3600
