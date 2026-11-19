"""increment_account_usage — the lock-free hot path (prod log audit C-04).

Concurrent specialist executions increment the SAME account document. A read-modify-write
transaction aborted under that contention and exhausted Firestore's 5 attempts, so usage was
silently lost. The hot path is now one server-side `Increment` update; only a daily/monthly
window rotation still needs a transaction.

The adapter reads its own clock to decide whether a window rotates, so that clock is pinned (P16) to the reference
instant the stamps are written with: a "same window" case is the same day and month whatever the wall clock reads.
"""
import asyncio
import logging
from datetime import timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.adapters.firestore_account_repo as account_repo_module
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.domain.billing import DEFAULT_DAILY_COST_LIMIT
from tests.support.clock_instants import OCT_END, OCT_NEXT, UTC_MIDNIGHT, pin_datetime

NOW = UTC_MIDNIGHT.utc + timedelta(hours=12)  # midday: the stamps and the adapter's now sit away from either boundary
HOT_PATHS = [
    "usage.daily_cost", "usage.daily_tokens", "usage.monthly_cost", "usage.monthly_tokens",
    "usage.total_cost", "usage.total_requests", "usage.total_tokens",
]  # Firestore returns transform results in this (alphabetical) order


class _Value:
    def __init__(self, double_value=0.0, integer_value=0):
        self.double_value = double_value
        self.integer_value = integer_value


class _WriteResult:
    def __init__(self, transform_results):
        self.transform_results = transform_results


def _write_result(daily_cost_after, **by_path):
    values = {p: _Value(integer_value=1) for p in HOT_PATHS}
    values.update({p: _Value(**v) if isinstance(v, dict) else _Value(double_value=v) for p, v in by_path.items()})
    values["usage.daily_cost"] = _Value(double_value=daily_cost_after)
    return _WriteResult([values[p] for p in HOT_PATHS])


def _data(daily_cost=1.0, daily_reset_at=NOW, monthly_reset_at=NOW, **extra):
    usage = {"daily_cost": daily_cost, "daily_tokens": 100}
    if daily_reset_at is not None:
        usage["daily_reset_at"] = daily_reset_at
    if monthly_reset_at is not None:
        usage["monthly_reset_at"] = monthly_reset_at
    return {"usage": usage, **extra}


def _repo(data, write_result=None):
    snapshot = MagicMock()
    snapshot.exists = data is not None
    snapshot.to_dict.return_value = data
    doc_ref = MagicMock()
    doc_ref.get = AsyncMock(return_value=snapshot)
    doc_ref.update = AsyncMock(return_value=write_result or _write_result(1.4))
    collection = MagicMock()
    collection.document.return_value = doc_ref
    db_client = MagicMock()
    db_client.collection.return_value = collection
    transaction = MagicMock()
    db_client.transaction.return_value = transaction
    return FirestoreAccountRepository(db_client, "accounts"), db_client, doc_ref, transaction


@pytest.fixture(autouse=True)
def _adapter_clock(monkeypatch):
    pin_datetime(monkeypatch, account_repo_module, NOW)


class TestLockFreeHotPath:

    def test_the_same_window_stamps_are_same_window_on_the_adapters_clock(self):
        # The precondition of every test below: a reference instant taken from the wall clock at import made them
        # take the rotation path when the run crossed 00:00 UTC after the import.
        now = account_repo_module.datetime.now(timezone.utc)
        assert FirestoreAccountRepository._rotations_due(_data()["usage"], now) == (False, False)

    async def test_same_window_increment_uses_no_transaction(self):
        repo, db_client, doc_ref, _ = _repo(_data())

        await repo.increment_account_usage("acct-1", tokens=10, cost=0.4)

        db_client.transaction.assert_not_called()
        doc_ref.update.assert_awaited_once()
        assert sorted(doc_ref.update.await_args.args[0]) == HOT_PATHS

    async def test_position_comes_from_the_servers_post_increment_value(self):
        # the pre-read is stale (1.0); the server says the day is now at 5.2
        repo, _, _, _ = _repo(
            _data(daily_cost=1.0),
            _write_result(5.2, **{"usage.daily_tokens": 999, "usage.monthly_cost": 77.0}),
        )

        result = await repo.increment_account_usage("acct-1", tokens=10, cost=0.4)

        assert result.daily_cost_after == pytest.approx(5.2)
        assert result.daily_cost_before == pytest.approx(4.8)
        assert result.crossed_daily_limit  # default limit 5.0 sits between 4.8 and 5.2

    async def test_integer_valued_server_result_is_read(self):
        repo, _, _, _ = _repo(_data(), _WriteResult([_Value(integer_value=7)] + [_Value()] * 6))

        result = await repo.increment_account_usage("acct-1", tokens=1, cost=2.0)

        assert result.daily_cost_after == 7.0
        assert result.daily_cost_before == 5.0

    async def test_limit_is_the_accounts_own_or_the_default(self):
        repo, _, _, _ = _repo(_data(daily_cost_limit=12.5))
        assert (await repo.increment_account_usage("a", 1, 0.1)).daily_cost_limit == 12.5

        repo, _, _, _ = _repo(_data())
        assert (await repo.increment_account_usage("a", 1, 0.1)).daily_cost_limit == DEFAULT_DAILY_COST_LIMIT

    async def test_unusable_transform_results_degrade_to_an_estimate_not_a_failure(self, caplog):
        repo, _, doc_ref, _ = _repo(_data(daily_cost=2.0), _WriteResult([]))

        with caplog.at_level(logging.WARNING):
            result = await repo.increment_account_usage("acct-1", tokens=10, cost=0.5)

        doc_ref.update.assert_awaited_once()  # the usage write itself happened
        assert result.daily_cost_after == pytest.approx(2.5)
        assert "no usable transform results" in caplog.text

    async def test_concurrent_increments_cross_the_limit_exactly_once(self):
        """Eight parallel executions: each sees its own before/after slice of the day."""
        repo, _, doc_ref, _ = _repo(_data(daily_cost=3.0))
        server = {"daily_cost": 3.0}

        async def server_side_increment(updates):
            await asyncio.sleep(0)  # let the other callers interleave
            server["daily_cost"] += 0.7
            return _write_result(server["daily_cost"])

        doc_ref.update = AsyncMock(side_effect=server_side_increment)

        results = await asyncio.gather(*[repo.increment_account_usage("acct-1", 10, 0.7) for _ in range(8)])

        assert sum(r.crossed_daily_limit for r in results) == 1
        assert max(r.daily_cost_after for r in results) == pytest.approx(3.0 + 8 * 0.7)


class TestRotationStillTransactional:

    @pytest.mark.parametrize("stale", ["daily", "monthly", "no_stamps"])
    async def test_a_due_rotation_goes_through_the_transaction(self, stale):
        last_month = (NOW.replace(day=1) - timedelta(days=1))
        data = {
            "daily": _data(daily_reset_at=NOW - timedelta(days=1)),
            "monthly": _data(monthly_reset_at=last_month),
            "no_stamps": _data(daily_reset_at=None, monthly_reset_at=None),
        }[stale]
        repo, db_client, doc_ref, transaction = _repo(data)

        with patch("src.adapters.firestore_account_repo.firestore.async_transactional", lambda fn: fn):
            result = await repo.increment_account_usage("acct-1", tokens=10, cost=0.4)

        db_client.transaction.assert_called_once()
        transaction.update.assert_called_once()
        doc_ref.update.assert_not_awaited()
        if stale in ("daily", "no_stamps"):
            assert result.daily_cost_before == 0.0  # a new day starts from zero


class TestWindowBoundaryFollowsTheAdaptersClock:

    async def test_last_second_of_a_day_and_first_second_of_the_next_rotate(self, monkeypatch):
        stamp = UTC_MIDNIGHT.utc - timedelta(seconds=1)
        pin_datetime(monkeypatch, account_repo_module, UTC_MIDNIGHT)
        repo, _, doc_ref, transaction = _repo(_data(daily_cost=3.0, daily_reset_at=stamp, monthly_reset_at=stamp))

        with patch("src.adapters.firestore_account_repo.firestore.async_transactional", lambda fn: fn):
            result = await repo.increment_account_usage("acct-1", tokens=10, cost=0.4)

        doc_ref.update.assert_not_awaited()
        updates = transaction.update.call_args.args[1]
        assert updates["usage.daily_reset_at"] == UTC_MIDNIGHT.utc
        assert "usage.monthly_reset_at" not in updates  # the same month
        assert result.daily_cost_before == 0.0

    async def test_first_and_last_second_of_one_day_are_one_window(self, monkeypatch):
        pin_datetime(monkeypatch, account_repo_module, UTC_MIDNIGHT.utc + timedelta(days=1, seconds=-1))
        repo, db_client, doc_ref, _ = _repo(_data(daily_reset_at=UTC_MIDNIGHT.utc, monthly_reset_at=UTC_MIDNIGHT.utc))

        await repo.increment_account_usage("acct-1", tokens=10, cost=0.4)

        db_client.transaction.assert_not_called()
        doc_ref.update.assert_awaited_once()

    async def test_a_month_end_rotates_the_day_and_the_month(self, monkeypatch):
        pin_datetime(monkeypatch, account_repo_module, OCT_NEXT)
        repo, _, doc_ref, transaction = _repo(_data(daily_reset_at=OCT_END.utc, monthly_reset_at=OCT_END.utc))

        with patch("src.adapters.firestore_account_repo.firestore.async_transactional", lambda fn: fn):
            await repo.increment_account_usage("acct-1", tokens=10, cost=0.4)

        doc_ref.update.assert_not_awaited()
        updates = transaction.update.call_args.args[1]
        assert updates["usage.daily_reset_at"] == OCT_NEXT.utc
        assert updates["usage.monthly_reset_at"] == OCT_NEXT.utc
        assert updates["usage.prev_monthly_month"] == "2026-10"


async def test_missing_account_raises_and_writes_nothing():
    repo, db_client, doc_ref, _ = _repo(None)

    with pytest.raises(ValueError, match="not found"):
        await repo.increment_account_usage("ghost", tokens=1, cost=0.1)

    doc_ref.update.assert_not_awaited()
    db_client.transaction.assert_not_called()
