"""verify_worker_oidc's optional audience pin, used by the drive indexer (plan Δ21). The default stays
unpinned for the main service (tests/unit/web/test_worker_oidc_verifier.py)."""
from unittest.mock import patch

from src.web.worker_oidc_verifier import verify_worker_oidc

_SA = "indexer@example.invalid"
_URL = "https://indexer.example.invalid"
_AUDIENCES = (_URL, f"{_URL}/worker")


def _verify(claims, audiences):
    with patch("src.web.worker_oidc_verifier.google_id_token.verify_oauth2_token", return_value=claims):
        return verify_worker_oidc("Bearer t", _SA, audiences)


def _claims(**overrides):
    return {"email": _SA, "email_verified": True, **overrides}


def test_cloud_tasks_default_audience_accepted():
    assert _verify(_claims(aud=f"{_URL}/worker"), _AUDIENCES) is True


def test_bare_service_url_accepted():
    assert _verify(_claims(aud=_URL), _AUDIENCES) is True


def test_other_audience_rejected():
    assert _verify(_claims(aud="https://main.example.invalid/worker"), _AUDIENCES) is False


def test_missing_audience_rejected_when_pinned():
    assert _verify(_claims(), _AUDIENCES) is False


def test_identity_still_checked_when_pinned():
    assert _verify(_claims(aud=_URL, email="other@example.invalid"), _AUDIENCES) is False


def test_unpinned_by_default():
    assert _verify(_claims(aud="anything"), None) is True
