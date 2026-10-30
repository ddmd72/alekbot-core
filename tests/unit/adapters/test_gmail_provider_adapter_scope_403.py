"""GmailProviderAdapter - non-2xx answers (wire test at the aiohttp boundary, tests/support/http_boundary_fake).

A 403 for an insufficient scope is OAuthScopeInsufficientError (an OAuthGrantRevokedError); any other 403 is not.
Every non-2xx logs status + reasons, never the token."""
import logging

import aiohttp
import pytest

from src.adapters.gmail_provider_adapter import GmailProviderAdapter
from src.domain.exceptions import OAuthGrantRevokedError, OAuthScopeInsufficientError
from tests.support import http_boundary_fake
from tests.support.oauth_credentials_store import creds

LIST = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
INSUFFICIENT = {"error": {"code": 403, "status": "PERMISSION_DENIED",
                          "errors": [{"reason": "insufficientPermissions", "message": "Insufficient Permission"}]}}
SCOPE_DETAIL = {"error": {"code": 403, "status": "PERMISSION_DENIED",
                          "details": [{"@type": "x", "reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}
OTHER = {"error": {"code": 403, "status": "PERMISSION_DENIED", "errors": [{"reason": "rateLimitExceeded"}]}}


async def _list(monkeypatch, status, body):
    http = http_boundary_fake.install(monkeypatch)
    http.route("GET", LIST, status=status, body=body)
    return await GmailProviderAdapter("cid", "csecret").list_emails(creds("user-1", "gmail", access_token="SECRET-TOKEN"))


@pytest.mark.parametrize("body", [INSUFFICIENT, SCOPE_DETAIL])
async def test_insufficient_scope_403_is_a_revoked_grant_subclass(monkeypatch, body):
    with pytest.raises(OAuthScopeInsufficientError) as exc:
        await _list(monkeypatch, 403, body)
    assert isinstance(exc.value, OAuthGrantRevokedError)
    assert "auth" in str(exc.value).lower()  # EmailIndexingService classifies failed_auth by keyword


async def test_other_403_is_not_the_subclass(monkeypatch):
    with pytest.raises(aiohttp.ClientResponseError) as exc:
        await _list(monkeypatch, 403, OTHER)
    assert exc.value.status == 403


async def test_a_non_json_body_keeps_the_plain_error(monkeypatch):
    with pytest.raises(aiohttp.ClientResponseError):
        await _list(monkeypatch, 500, "upstream down")


async def test_warning_carries_the_reason_and_no_token(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    with pytest.raises(OAuthScopeInsufficientError):
        await _list(monkeypatch, 403, INSUFFICIENT)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "403" in text and "insufficientPermissions" in text and "PERMISSION_DENIED" in text
    assert "SECRET-TOKEN" not in text and "Insufficient Permission" not in text
