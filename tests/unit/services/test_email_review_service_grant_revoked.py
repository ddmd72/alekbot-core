"""
EmailReviewService — a revoked Gmail grant must surface as OAuthGrantRevokedError
(the worker turns it into a reconnect notice); other refresh failures keep the
quiet None return.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.email import OAuthCredentials
from src.domain.exceptions import OAuthGrantRevokedError
from src.services.email_review_service import EmailReviewService


def _service(refresh_error: Exception):
    provider = MagicMock()
    provider.refresh_token = AsyncMock(side_effect=refresh_error)
    provider.list_emails = AsyncMock()
    oauth = MagicMock()
    oauth.get_credentials = AsyncMock(return_value=OAuthCredentials(
        user_id="user-1",
        provider="gmail",
        access_token="tok",
        refresh_token="rtok",
        token_expiry=datetime.now(timezone.utc) - timedelta(minutes=1),
        scopes=[],
        email_address="user@example.com",
    ))
    oauth.save_credentials = AsyncMock()
    return EmailReviewService(email_provider=provider, oauth_credentials=oauth), provider, oauth


async def test_revoked_grant_raises():
    svc, provider, oauth = _service(OAuthGrantRevokedError("invalid_grant"))

    with pytest.raises(OAuthGrantRevokedError):
        await svc.fetch_review_payload("user-1")

    oauth.save_credentials.assert_not_awaited()
    provider.list_emails.assert_not_awaited()


async def test_transient_refresh_failure_still_returns_none():
    svc, provider, _ = _service(ValueError("Gmail token refresh failed: server_error — "))

    assert await svc.fetch_review_payload("user-1") is None
    provider.list_emails.assert_not_awaited()
