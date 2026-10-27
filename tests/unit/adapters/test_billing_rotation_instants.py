"""Usage counters rotate on the UTC calendar: month ends, and the DST instants of the clock table (P16-D).

``increment_account_usage`` is driven on ``EvalDb`` (``eval_transactions()`` for the rotating path) with the module's
``datetime`` pinned and re-pinned per step. The collection that yields is not relevant: no gather here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.adapters import firestore_account_repo as repo_module
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.domain.billing import AccountUsageStats, BillingAccount
from tests.support import clock_instants as ci
from tests.support.firestore_eval import EvalDb, eval_transactions

UTC = timezone.utc
ACCOUNTS = "accounts"
DST_INSTANTS = [
    *ci.TRANSITIONS.values(),
    *(i for r in ci.REPEATED_HOUR.values() for i in (r.fold0, r.fold1)),
    ci.UTC_MIDNIGHT,
    ci.SUMMER_MIDNIGHT,
]


def _repo() -> tuple[FirestoreAccountRepository, EvalDb]:
    db = EvalDb()
    return FirestoreAccountRepository(db, ACCOUNTS), db


async def _seed(repo: FirestoreAccountRepository, *, daily_reset_at: datetime, monthly_reset_at: datetime,
                daily_tokens: int = 100, daily_cost: float = 1.0) -> None:
    usage = AccountUsageStats(
        daily_tokens=daily_tokens, daily_cost=daily_cost, daily_reset_at=daily_reset_at,
        monthly_tokens=1000, monthly_cost=10.0, monthly_reset_at=monthly_reset_at,
        total_tokens=5000, total_cost=50.0, total_requests=9,
    )
    await repo.create_account(BillingAccount(account_id="a1", usage=usage))


def _usage(db: EvalDb) -> dict:
    return db.collection(ACCOUNTS).docs["a1"]["usage"]


async def test_month_end_rotates_daily_and_monthly_once_at_utc_midnight(monkeypatch):
    repo, db = _repo()
    early = datetime(2026, 10, 31, 6, 0, tzinfo=UTC)
    await _seed(repo, daily_reset_at=early, monthly_reset_at=early)

    with eval_transactions():
        ci.pin_datetime(monkeypatch, repo_module, ci.OCT_END)
        await repo.increment_account_usage("a1", tokens=10, cost=0.5)
        # the last second of October is still October 31: no rotation, the day total grows
        assert _usage(db)["daily_tokens"] == 110
        assert _usage(db)["prev_daily_date"] is None

        ci.pin_datetime(monkeypatch, repo_module, ci.OCT_NEXT)
        result = await repo.increment_account_usage("a1", tokens=7, cost=0.25)
        usage = _usage(db)
        assert usage["prev_daily_date"] == "2026-10-31"
        assert usage["prev_daily_tokens"] == 110
        assert usage["prev_daily_cost"] == pytest.approx(1.5)
        assert usage["daily_tokens"] == 7
        assert usage["monthly_tokens"] == 7  # the monthly window rotated at the same instant
        assert usage["monthly_cost"] == pytest.approx(0.25)
        assert usage["monthly_reset_at"] == ci.OCT_NEXT.utc
        assert usage["total_tokens"] == 5000 + 10 + 7  # totals never rotate
        assert result.daily_cost_before == 0.0

        # a second increment on November 1 rotates nothing more
        ci.pin_datetime(monkeypatch, repo_module, datetime(2026, 11, 1, 9, 0, tzinfo=UTC))
        await repo.increment_account_usage("a1", tokens=3, cost=0.1)
        usage = _usage(db)
        assert usage["prev_daily_date"] == "2026-10-31"
        assert usage["daily_tokens"] == 10
        assert usage["monthly_tokens"] == 10


async def test_usage_for_date_answers_each_day_across_the_rotation(monkeypatch):
    repo, _ = _repo()
    early = datetime(2026, 10, 31, 6, 0, tzinfo=UTC)
    await _seed(repo, daily_reset_at=early, monthly_reset_at=early)

    with eval_transactions():
        ci.pin_datetime(monkeypatch, repo_module, ci.OCT_END)
        await repo.increment_account_usage("a1", tokens=10, cost=0.5)
        ci.pin_datetime(monkeypatch, repo_module, ci.OCT_NEXT)
        await repo.increment_account_usage("a1", tokens=7, cost=0.25)

    usage = (await repo.get_account("a1")).usage
    assert usage.usage_for_date(datetime(2026, 10, 31).date()) == (110, pytest.approx(1.5))
    assert usage.usage_for_date(datetime(2026, 11, 1).date()) == (7, pytest.approx(0.25))
    assert usage.usage_for_date(datetime(2026, 10, 30).date()) == (0, 0.0)


@pytest.mark.parametrize("instant", DST_INSTANTS, ids=[i.name for i in DST_INSTANTS])
async def test_dst_instant_is_invisible_to_utc_counters(monkeypatch, instant):
    repo, db = _repo()
    midnight = instant.utc.replace(hour=0, minute=0, second=0)  # same UTC date and month as the pinned instant
    await _seed(repo, daily_reset_at=midnight, monthly_reset_at=midnight)

    with eval_transactions():
        ci.pin_datetime(monkeypatch, repo_module, instant)
        await repo.increment_account_usage("a1", tokens=10, cost=0.5)

    usage = _usage(db)
    assert usage["daily_tokens"] == 110
    assert usage["monthly_tokens"] == 1010
    assert usage["prev_daily_date"] is None
    assert usage["daily_reset_at"] == midnight
    assert usage["monthly_reset_at"] == midnight


@pytest.mark.parametrize("instant", DST_INSTANTS, ids=[i.name for i in DST_INSTANTS])
def test_rotations_due_is_false_on_the_same_utc_date(instant):
    midnight = instant.utc.replace(hour=0, minute=0, second=0)
    assert FirestoreAccountRepository._rotations_due(
        {"daily_reset_at": midnight, "monthly_reset_at": midnight}, instant.utc) == (False, False)


@pytest.mark.parametrize("instant", DST_INSTANTS, ids=[i.name for i in DST_INSTANTS])
def test_rotations_due_fires_the_next_utc_day_not_the_next_local_day(instant):
    next_day = instant.utc.replace(hour=0, minute=0, second=0) + timedelta(days=1)
    daily, monthly = FirestoreAccountRepository._rotations_due(
        {"daily_reset_at": instant.utc, "monthly_reset_at": instant.utc}, next_day)
    assert daily is True
    assert monthly is (next_day.month != instant.utc.month)


@pytest.mark.parametrize("last,first", [(ci.FEB_END, ci.FEB_NEXT), (ci.OCT_END, ci.OCT_NEXT), (ci.DEC_END, ci.DEC_NEXT)],
                         ids=["feb", "oct", "dec-year"])
def test_rotations_due_at_each_month_end(last, first):
    usage = {"daily_reset_at": last.utc, "monthly_reset_at": last.utc}
    assert FirestoreAccountRepository._rotations_due(usage, last.utc) == (False, False)
    assert FirestoreAccountRepository._rotations_due(usage, first.utc) == (True, True)


def test_rotations_due_when_a_window_was_never_stamped():
    assert FirestoreAccountRepository._rotations_due({}, ci.OCT_END.utc) == (True, True)
