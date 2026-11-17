"""The Gmail reconnect flag stays up whatever the notice does (decisions/gmail_grant_revoked_reconnect.md).
`needs_reconnect` drives the Cabinet's "Reconnect required": taking it down after a notice that reached nobody
would show "Connected" for a revoked grant. So it is saved once, before the notice, and a later failing run
sends no second notice, even when the first one never left (no channel, a formatter failure, `notify` raising)."""
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.agent import AgentStatus
from src.domain.integration import Integration
from src.domain.credential_key import CredentialKey
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from src.domain.exceptions import OAuthGrantRevokedError
from src.domain.notify_result import NotifyResult
from src.services.daily_email_review_task import DailyEmailReviewTask
from src.services.email_review_service import EmailReviewService
from src.services.gmail_reconnect_flag import GmailReconnectFlag
from src.composition.connector_registry import ConnectorRegistry

_NOW = datetime(2026, 10, 10, 11, 12, tzinfo=timezone.utc)


class _Store:
    """Credentials store that hands out copies, like Firestore. A flag write goes through update, which
    writes only while the record exists (OAuthCredentialsPort); there is no consent path here."""

    def __init__(self):
        self.creds = OAuthCredential(
            user_id="u",
            account_id="acct-1",
            kind="gmail",
            integration=Integration.MAIL,
            account=ProviderAccount(realm="google", subject="", label="user@example.com"),
            access_token="tok",
            refresh_token="rtok",
            token_expiry=_NOW,
            scopes=[],
        )
        self.saves = []

    async def list_keys(self, user_id, kind=None):
        return [CredentialKey(user_id=user_id, credential_id=self.creds.credential_id)] if self.creds is not None else []

    async def get(self, user_id, credential_id):
        return replace(self.creds) if self.creds is not None else None

    async def update(self, creds):
        if self.creds is None:
            return False
        self.saves.append(creds.needs_reconnect)
        self.creds = replace(creds)
        return True


def _worker(store, notify):
    review = MagicMock(spec=EmailReviewService)
    review.fetch_review_payload = AsyncMock(side_effect=OAuthGrantRevokedError("invalid_grant"))
    notification = AsyncMock()
    notification.notify = notify
    ensure_agents = AsyncMock()
    return DailyEmailReviewTask(
        email_review=review, ensure_agents=ensure_agents, notification_service=notification,
        gmail_reconnect_flag=GmailReconnectFlag(
            oauth_credentials=store, mail_connector=ConnectorRegistry().get("gmail"), ensure_agents=ensure_agents,
            notification_service=notification),
        alert_sink=None)


@pytest.mark.parametrize("outcome", [
    NotifyResult(delivered=False, agent_status=AgentStatus.SUCCESS, error="no_channel"),
    NotifyResult(delivered=False, agent_status=AgentStatus.TIMEOUT, error="timeout"),
    RuntimeError("channel lookup failed"),
])
async def test_the_flag_stays_up_and_no_second_notice_follows(outcome):
    store = _Store()
    notify = AsyncMock(side_effect=[outcome, NotifyResult(delivered=True, agent_status=AgentStatus.SUCCESS,
                                                          sent=True)])
    worker = _worker(store, notify)
    for _ in range(2):
        _, status = await worker({"user_id": "u", "account_id": "a"}, {})
        assert status == 200
    assert store.creds.needs_reconnect is True
    assert store.saves == [True]
    assert notify.await_count == 1
