"""Unit tests for FirestoreAccountRepository.increment_account_usage.

Focus: the daily rotation stamps prev_daily_date with the calendar date of the day
that just ended, so a clock-driven report can resolve "yesterday" correctly
(see AccountUsageStats.usage_for_date). Mocks at the Firestore SDK boundary:
async_transactional is patched to a passthrough, and the transaction's update()
call is captured.
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.domain.billing import DEFAULT_DAILY_COST_LIMIT


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


def _make_repo_returning(existing_usage: dict, **account_fields):
    """Like _make_repo_and_capture, but exposes top-level account fields.

    Needed for daily_cost_limit, which lives on the account document rather than under
    ``usage`` — and for asserting the UsageIncrement the transaction returns.
    """
    snapshot = MagicMock()
    snapshot.exists = True
    snapshot.to_dict.return_value = {"usage": existing_usage, **account_fields}

    doc_ref = MagicMock()
    doc_ref.get = AsyncMock(return_value=snapshot)

    db_client = MagicMock()
    collection = MagicMock()
    collection.document.return_value = doc_ref
    db_client.collection.return_value = collection
    db_client.transaction.return_value = MagicMock()

    return FirestoreAccountRepository(db_client=db_client, collection_name="accounts")


class TestIncrementAccountUsageReturnsDailyPosition:
    """The increment reports where the day's spend landed, so the caller can alert
    without a second read (added 2026-07-28 with the daily budget alert)."""

    async def test_same_day_increment_reports_before_and_after(self):
        now = datetime.now(timezone.utc)
        repo = _make_repo_returning(
            {"daily_cost": 4.80, "daily_reset_at": now, "monthly_reset_at": now}
        )

        with patch(
            "src.adapters.firestore_account_repo.firestore.async_transactional",
            _passthrough_transactional,
        ):
            result = await repo.increment_account_usage("acct-1", tokens=10, cost=0.40)

        assert result.daily_cost_before == 4.80
        assert result.daily_cost_after == pytest.approx(5.20)
        assert result.crossed_daily_limit

    async def test_rotation_reports_zero_before(self):
        """A new day starts the comparison at 0 even if yesterday ended over budget."""
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        repo = _make_repo_returning(
            {"daily_cost": 9.99, "daily_reset_at": yesterday, "monthly_reset_at": yesterday}
        )

        with patch(
            "src.adapters.firestore_account_repo.firestore.async_transactional",
            _passthrough_transactional,
        ):
            result = await repo.increment_account_usage("acct-1", tokens=10, cost=0.40)

        assert result.daily_cost_before == 0.0
        assert result.daily_cost_after == pytest.approx(0.40)
        assert not result.crossed_daily_limit

    async def test_limit_defaults_when_absent_from_document(self):
        """Existing account documents predate the field."""
        now = datetime.now(timezone.utc)
        repo = _make_repo_returning(
            {"daily_cost": 0.0, "daily_reset_at": now, "monthly_reset_at": now}
        )

        with patch(
            "src.adapters.firestore_account_repo.firestore.async_transactional",
            _passthrough_transactional,
        ):
            result = await repo.increment_account_usage("acct-1", tokens=10, cost=0.01)

        assert result.daily_cost_limit == DEFAULT_DAILY_COST_LIMIT

    async def test_per_account_limit_is_honoured(self):
        now = datetime.now(timezone.utc)
        repo = _make_repo_returning(
            {"daily_cost": 20.0, "daily_reset_at": now, "monthly_reset_at": now},
            daily_cost_limit=25.0,
        )

        with patch(
            "src.adapters.firestore_account_repo.firestore.async_transactional",
            _passthrough_transactional,
        ):
            result = await repo.increment_account_usage("acct-1", tokens=10, cost=6.0)

        assert result.daily_cost_limit == 25.0
        assert result.crossed_daily_limit
