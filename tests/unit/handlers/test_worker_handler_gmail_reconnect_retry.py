"""The Gmail reconnect notice is retried when nothing reached the user (decisions/gmail_grant_revoked_reconnect.md;
same rule as the drive notice, USER_DRIVE_INDEXING_RFC §7.7). `needs_reconnect` doubles as "user told": it goes
up before `notify` and comes down when `notify` attempted no send or raised, so the next failing run notifies
again. The rollback re-reads the credentials, so a reconnect saved meanwhile is never overwritten."""
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


def _creds(needs_reconnect=False, refresh_token="rtok"):
    return OAuthCredentials(user_id="u", provider="gmail", access_token="tok", refresh_token=refresh_token,
                            token_expiry=_NOW, scopes=[], email_address="user@example.com",
                            needs_reconnect=needs_reconnect)


class _Store:
    """Credentials store that hands out copies, like Firestore."""

    def __init__(self, creds):
        self.creds = creds
        self.saves = []

    async def get_credentials(self, user_id, provider):
        return replace(self.creds) if self.creds else None

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


async def _review(worker):
    return await worker._handle_daily_email_review({"user_id": "u", "account_id": "a"})


@pytest.mark.parametrize("outcome", [
    NotifyResult(delivered=False, agent_status=AgentStatus.TIMEOUT, error="timeout"),
    NotifyResult(delivered=False, agent_status=AgentStatus.SUCCESS, error="no_channel"),
    RuntimeError("channel lookup failed"),
])
async def test_nothing_sent_rolls_the_flag_back_and_the_next_run_notifies_again(outcome):
    store = _Store(_creds())
    notify = AsyncMock(side_effect=[outcome, NotifyResult(delivered=True, agent_status=AgentStatus.SUCCESS,
                                                          sent=True)])
    worker = _worker(store, notify)
    _, status = await _review(worker)
    assert status == 200
    assert store.saves == [True, False] and store.creds.needs_reconnect is False
    await _review(worker)
    assert notify.await_count == 2 and store.creds.needs_reconnect is True


async def test_an_attempted_send_keeps_the_flag():
    store = _Store(_creds())
    notify = AsyncMock(return_value=NotifyResult(delivered=False, agent_status=AgentStatus.FAILED,
                                                 error="slack down", sent=True))
    worker = _worker(store, notify)
    await _review(worker)
    await _review(worker)
    assert store.saves == [True] and notify.await_count == 1


async def test_a_reconnect_saved_during_the_notice_is_not_overwritten():
    store = _Store(_creds())

    async def reconnect_then_fail(**_):
        store.creds = _creds(refresh_token="fresh")   # the user reconnected while the formatter ran
        return NotifyResult(delivered=False, agent_status=AgentStatus.TIMEOUT)

    worker = _worker(store, AsyncMock(side_effect=reconnect_then_fail))
    await _review(worker)
    assert store.saves == [True]
    assert store.creds.refresh_token == "fresh" and store.creds.needs_reconnect is False
