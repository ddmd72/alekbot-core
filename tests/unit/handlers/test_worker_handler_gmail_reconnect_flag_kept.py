"""The Gmail reconnect flag stays up whatever the notice does (decisions/gmail_grant_revoked_reconnect.md).
`needs_reconnect` drives the Cabinet's "Reconnect required": taking it down after a notice that reached nobody
would show "Connected" for a revoked grant. So it is saved once, before the notice, and a later failing run
sends no second notice, even when the first one never left (no channel, a formatter failure, `notify` raising)."""
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.agent import AgentStatus
from src.domain.email import OAuthCredentials
from src.domain.exceptions import OAuthGrantRevokedError
from src.domain.notify_result import NotifyResult
from src.handlers.worker_handler import WorkerHandler
from src.services.email_review_service import EmailReviewService

_NOW = datetime(2026, 10, 10, 11, 12, tzinfo=timezone.utc)


class _Store:
    """Credentials store that hands out copies, like Firestore."""

    def __init__(self):
        self.creds = OAuthCredentials(user_id="u", provider="gmail", access_token="tok", refresh_token="rtok",
                                      token_expiry=_NOW, scopes=[], email_address="user@example.com")
        self.saves = []

    async def get_credentials(self, user_id, provider):
        return replace(self.creds)

    async def save_credentials(self, creds):
        self.saves.append(creds.needs_reconnect)
        self.creds = replace(creds)


def _worker(store, notify):
    review = MagicMock(spec=EmailReviewService)
    review.fetch_review_payload = AsyncMock(side_effect=OAuthGrantRevokedError("invalid_grant"))
    notification = AsyncMock()
    notification.notify = notify
    return WorkerHandler(
        agent_worker_handler=MagicMock(), email_indexing_service=MagicMock(), notification_service=notification,
        consolidation_service=None, coordinator=MagicMock(), agent_factory=MagicMock(), indexed_email_repo=None,
        user_repo=MagicMock(), task_dispatch=AsyncMock(), email_review=review, oauth_credentials=store,
    )


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
        _, status = await worker._handle_daily_email_review({"user_id": "u", "account_id": "a"})
        assert status == 200
    assert store.creds.needs_reconnect is True
    assert store.saves == [True]
    assert notify.await_count == 1
