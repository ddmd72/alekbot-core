"""
GmailProviderAdapter.refresh_token — wire test (mock at the aiohttp boundary).

Google answers a revoked/expired refresh token with {"error": "invalid_grant"};
that one error maps to OAuthGrantRevokedError so callers can tell "reconnect
required" from a transient failure. Every other error stays a plain ValueError.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.gmail_provider_adapter import GmailProviderAdapter
from src.domain.email import OAuthCredentials
from src.domain.exceptions import OAuthGrantRevokedError


def _creds() -> OAuthCredentials:
    return OAuthCredentials(
        user_id="user-1",
        provider="gmail",
        access_token="old",
        refresh_token="rtok",
        token_expiry=datetime(2026, 10, 10, tzinfo=timezone.utc),
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        email_address="user@example.com",
        needs_reconnect=True,
    )


def _patch_token_endpoint(body: dict):
    resp = MagicMock()
    resp.json = AsyncMock(return_value=body)
    post_ctx = MagicMock()
    post_ctx.__aenter__ = AsyncMock(return_value=resp)
    post_ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=post_ctx)
    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    return patch(
        "src.adapters.gmail_provider_adapter.aiohttp.ClientSession",
        return_value=session_ctx,
    ), session


async def test_invalid_grant_raises_grant_revoked():
    p, _ = _patch_token_endpoint(
        {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
    )
    adapter = GmailProviderAdapter(client_id="cid", client_secret="secret")
    with p, pytest.raises(OAuthGrantRevokedError) as exc_info:
        await adapter.refresh_token(_creds())
    # Message kept: EmailIndexingService classifies failed_auth by its keywords.
    assert "token refresh failed" in str(exc_info.value)
    assert "invalid_grant" in str(exc_info.value)


async def test_other_error_stays_plain_value_error():
    p, _ = _patch_token_endpoint({"error": "invalid_client"})
    adapter = GmailProviderAdapter(client_id="cid", client_secret="secret")
    with p, pytest.raises(ValueError) as exc_info:
        await adapter.refresh_token(_creds())
    assert not isinstance(exc_info.value, OAuthGrantRevokedError)


async def test_success_clears_needs_reconnect():
    p, session = _patch_token_endpoint({"access_token": "new", "expires_in": 3600})
    adapter = GmailProviderAdapter(client_id="cid", client_secret="secret")
    with p:
        fresh = await adapter.refresh_token(_creds())
    assert fresh.access_token == "new"
    assert fresh.refresh_token == "rtok"
    assert fresh.needs_reconnect is False
    sent = session.post.call_args.kwargs["data"]
    assert sent["grant_type"] == "refresh_token"
    assert sent["refresh_token"] == "rtok"
