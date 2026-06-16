"""Unit tests for FirestoreAccountRepository.increment_account_usage.

Focus: the daily rotation stamps prev_daily_date with the calendar date of the day
that just ended, so a clock-driven report can resolve "yesterday" correctly
(see AccountUsageStats.usage_for_date). Mocks at the Firestore SDK boundary:
async_transactional is patched to a passthrough, and the transaction's update()
call is captured.
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from src.adapters.firestore_account_repo import FirestoreAccountRepository


def _make_repo_and_capture(existing_usage: dict):
    """Build a repo whose transaction captures the updates dict passed to update()."""
    captured = {}

    snapshot = MagicMock()
    snapshot.exists = True
    snapshot.to_dict.return_value = {"usage": existing_usage}

    doc_ref = MagicMock()
    doc_ref.get = AsyncMock(return_value=snapshot)

    transaction = MagicMock()
    transaction.update = MagicMock(side_effect=lambda ref, updates: captured.update(updates))

    db_client = MagicMock()
    collection = MagicMock()
    collection.document.return_value = doc_ref
    db_client.collection.return_value = collection
    db_client.transaction.return_value = transaction

    repo = FirestoreAccountRepository(db_client=db_client, collection_name="accounts")
    return repo, captured


def _passthrough_transactional(fn):
    return fn


class TestIncrementAccountUsageRotation:

    async def test_rotation_stamps_prev_daily_date_with_ended_day(self):
        # Last activity was yesterday; today's first request triggers a daily reset.
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1))
        existing = {
            "daily_tokens": 1234,
            "daily_cost": 0.07,
            "daily_reset_at": yesterday,
            "monthly_reset_at": yesterday,
        }
        repo, captured = _make_repo_and_capture(existing)

        with patch(
            "src.adapters.firestore_account_repo.firestore.async_transactional",
            _passthrough_transactional,
        ):
            await repo.increment_account_usage("acct-1", tokens=10, cost=0.001)

        # The snapshot moved into prev_daily, stamped with the day it belonged to.
        assert captured["usage.prev_daily_tokens"] == 1234
        assert captured["usage.prev_daily_cost"] == 0.07
        assert captured["usage.prev_daily_date"] == yesterday.date().isoformat()
        # Live counter resets to this request's usage.
        assert captured["usage.daily_tokens"] == 10

    async def test_no_rotation_same_day_does_not_touch_prev_daily_date(self):
        # Activity already happened today → increment in place, no rotation.
        now = datetime.now(timezone.utc)
        existing = {
            "daily_tokens": 500,
            "daily_cost": 0.02,
            "daily_reset_at": now,
            "monthly_reset_at": now,
        }
        repo, captured = _make_repo_and_capture(existing)

        with patch(
            "src.adapters.firestore_account_repo.firestore.async_transactional",
            _passthrough_transactional,
        ):
            await repo.increment_account_usage("acct-1", tokens=10, cost=0.001)

        assert "usage.prev_daily_date" not in captured
