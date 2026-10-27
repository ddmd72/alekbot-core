"""FirestoreAccountRepository on the evaluating fake: what the rotating increment writes when only one window turns, the
rotating increment under a concurrent one, and the active-account listing (the mutation check's survivors).

Assertions are on the stored account document and on what the repository returns."""
import asyncio
from datetime import datetime, timezone

import pytest

from src.adapters import firestore_account_repo as repo_module
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.domain.billing import AccountUsageStats, BillingAccount
from tests.support.clock_instants import pin_datetime
from tests.support.firestore_eval import EvalDb

UTC = timezone.utc
ACCOUNTS = "accounts"
NOW = datetime(2026, 10, 27, 8, 0, tzinfo=UTC)
YESTERDAY = datetime(2026, 10, 26, 18, 0, tzinfo=UTC)
EARLIER_TODAY = datetime(2026, 10, 27, 6, 0, tzinfo=UTC)
MONTH_START = datetime(2026, 10, 1, 0, 30, tzinfo=UTC)


@pytest.fixture
def db():
    return EvalDb()


@pytest.fixture
def repo(db, monkeypatch):
    pin_datetime(monkeypatch, repo_module, NOW)
    return FirestoreAccountRepository(db, ACCOUNTS)


def _usage(db) -> dict:
    return db.collection(ACCOUNTS).docs["a1"]["usage"]


async def _seed(repo, daily_reset_at, monthly_reset_at):
    await repo.create_account(BillingAccount(account_id="a1", usage=AccountUsageStats(
        total_tokens=5000, total_cost=50.0, total_requests=9,
        daily_tokens=100, daily_cost=1.0, daily_reset_at=daily_reset_at,
        monthly_tokens=1000, monthly_cost=10.0, monthly_reset_at=monthly_reset_at,
    )))


async def test_a_new_day_inside_the_month_adds_to_the_totals_and_the_month(repo, db):
    await _seed(repo, daily_reset_at=YESTERDAY, monthly_reset_at=MONTH_START)

    result = await repo.increment_account_usage("a1", tokens=10, cost=0.5)

    usage = _usage(db)
    assert (usage["total_tokens"], usage["total_cost"], usage["total_requests"]) == (5010, pytest.approx(50.5), 10)
    assert (usage["monthly_tokens"], usage["monthly_cost"]) == (1010, pytest.approx(10.5))
    assert usage["monthly_reset_at"] == MONTH_START
    assert (usage["daily_tokens"], usage["daily_cost"]) == (10, pytest.approx(0.5))
    assert (result.daily_cost_before, result.daily_cost_after) == (0.0, pytest.approx(0.5))


async def test_a_turning_month_with_the_day_already_open_adds_to_the_day(repo, db):
    """An account document with no `monthly_reset_at` (written before the field existed) and a day opened earlier today:
    the month starts over, the day keeps counting."""
    await _seed(repo, daily_reset_at=EARLIER_TODAY, monthly_reset_at=MONTH_START)
    del db.collection(ACCOUNTS).docs["a1"]["usage"]["monthly_reset_at"]

    result = await repo.increment_account_usage("a1", tokens=10, cost=0.5)

    usage = _usage(db)
    assert (usage["daily_tokens"], usage["daily_cost"]) == (110, pytest.approx(1.5))
    assert usage["daily_reset_at"] == EARLIER_TODAY
    assert (usage["monthly_tokens"], usage["monthly_cost"], usage["monthly_reset_at"]) == (10, pytest.approx(0.5), NOW)
    assert (result.daily_cost_before, result.daily_cost_after) == (pytest.approx(1.0), pytest.approx(1.5))


async def test_two_concurrent_first_increments_of_a_day_both_count(repo, db):
    """Both increments read the account before either writes, so both take the rotating path. The day's counters
    end with both increments and exactly one of them opened the day (`daily_cost_before == 0`)."""
    await _seed(repo, daily_reset_at=YESTERDAY, monthly_reset_at=MONTH_START)
    db.collection(ACCOUNTS).yield_on_get = True
    transactions = []
    make_transaction = db.transaction

    def counting_transaction():
        transactions.append(1)
        return make_transaction()

    db.transaction = counting_transaction

    first, second = await asyncio.gather(
        repo.increment_account_usage("a1", tokens=10, cost=0.5),
        repo.increment_account_usage("a1", tokens=20, cost=0.25),
    )

    assert len(transactions) == 2, "both increments saw yesterday's window, so both rotated (the interleaving)"
    usage = _usage(db)
    assert (usage["daily_tokens"], usage["daily_cost"]) == (30, pytest.approx(0.75))
    assert (usage["prev_daily_tokens"], usage["prev_daily_cost"], usage["prev_daily_date"]) == (
        100, pytest.approx(1.0), "2026-10-26")
    assert (usage["total_tokens"], usage["total_requests"]) == (5030, 11)
    assert sorted([first.daily_cost_before, second.daily_cost_before]) == [0.0, pytest.approx(0.5)]


async def test_listing_accounts_leaves_out_an_inactive_one(repo):
    await repo.create_account(BillingAccount(account_id="a1"))
    await repo.create_account(BillingAccount(account_id="a2", is_active=False))

    listed = await repo.list_all_accounts()

    assert [a.account_id for a in listed] == ["a1"]
