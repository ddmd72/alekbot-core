"""T6: verify_oidc_identity (sandbox RFC §4.4). Mocked at google.oauth2.id_token.verify_oauth2_token."""
import logging
from unittest.mock import patch

from google.auth.exceptions import MalformedError

from src.web.worker_oidc_verifier import verify_oidc_identity

AUD = ["https://sandbox.example.com"]
TARGET = "src.web.worker_oidc_verifier.google_id_token.verify_oauth2_token"
HDR = "Bearer tok"


def claims(**kw):
    base = {"email": "a@example.com", "email_verified": True, "aud": AUD[0]}
    base.update(kw)
    return base


def test_valid_token_returns_email():
    with patch(TARGET, return_value=claims()):
        assert verify_oidc_identity(HDR, AUD) == "a@example.com"


def test_bad_signature_none():
    with patch(TARGET, side_effect=ValueError("Could not verify token signature")):
        assert verify_oidc_identity(HDR, AUD) is None


def test_expired_none():
    with patch(TARGET, side_effect=ValueError("Token expired")):
        assert verify_oidc_identity(HDR, AUD) is None


def test_unverified_email_none():
    with patch(TARGET, return_value=claims(email_verified=False)):
        assert verify_oidc_identity(HDR, AUD) is None


def test_wrong_audience_none():
    with patch(TARGET, return_value=claims(aud="https://other.example.com")):
        assert verify_oidc_identity(HDR, AUD) is None


def test_missing_bearer_none():
    with patch(TARGET, return_value=claims()) as m:
        assert verify_oidc_identity(None, AUD) is None
        assert verify_oidc_identity("", AUD) is None
        assert verify_oidc_identity("Basic abc", AUD) is None
        assert verify_oidc_identity("Bearer   ", AUD) is None
        m.assert_not_called()


def test_missing_email_claim_none():
    with patch(TARGET, return_value=claims(email=None)):
        assert verify_oidc_identity(HDR, AUD) is None


def test_never_raises():
    with patch(TARGET, side_effect=RuntimeError("boom")):
        assert verify_oidc_identity(HDR, AUD) is None
    with patch(TARGET, return_value=None):  # claims.get on None -> AttributeError
        assert verify_oidc_identity(HDR, AUD) is None


def test_exception_text_with_token_is_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    sentinel = "SENTINEL-TOKEN-abc.def.ghi"
    with patch(TARGET, side_effect=MalformedError(f"Wrong number of segments in token: {sentinel}")):
        assert verify_oidc_identity(HDR, AUD) is None
    assert "verification failed" in caplog.text
    assert sentinel not in caplog.text


def test_bare_string_audience_is_not_a_substring_test():
    with patch(TARGET, return_value=claims(aud="https://sandbox")):
        assert verify_oidc_identity(HDR, AUD[0]) is None
    with patch(TARGET, return_value=claims()):
        assert verify_oidc_identity(HDR, AUD[0]) == "a@example.com"


def test_non_string_audience_claim_none():
    with patch(TARGET, return_value=claims(aud=["https://sandbox.example.com"])):
        assert verify_oidc_identity(HDR, AUD) is None


def test_key_fetch_uses_a_short_timeout():
    from src.web import worker_oidc_verifier as v

    seen = {}

    class Base:
        def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kw):
            seen["timeout"] = timeout

    with patch.object(v.google_requests, "Request", Base):
        v._short_timeout_request()("https://x.example.com")
    assert seen["timeout"] == v._CERT_FETCH_TIMEOUT_S <= 10
