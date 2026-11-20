"""AccountRepository kit (P11 · G1): the docstring of `src/ports/account_repository.py` checked against
`FirestoreAccountRepository` on the evaluating Firestore fake (H1), with the index refusal ON (`list_all_accounts` is
one equality). Assertions are on the raw stored documents and on what the repository returns.

Already pinned elsewhere and not repeated: rotation at month ends and at the DST instants
(`test_billing_rotation_instants.py`), and the concurrent first increments of a day
(`test_firestore_account_repo_mutants.py::test_two_concurrent_first_increments_of_a_day_both_count`).
`check_quota` is dead by decision (no caller; the hard cap was dropped 2026-07-26) and is not tested. Documents no
writer produces (an unknown tier, no `is_active`) are not pinned: their behaviour waits for the data audit.

Every seeded account sets its usage reset stamps to T0 explicitly: `AccountUsageStats` defaults them to the unpinned
`datetime.now` of `src.domain.billing`.

Known unmodelled behaviour (`firestore_eval.py`): real Firestore blocks a non-transactional `update` while a
transaction holds the document; nothing here asserts the lock-free/rotation interleaving beyond the decision record.
"""
import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from google.api_core.exceptions import NotFound
from google.cloud.firestore_v1.types.write import WriteResult

import src.adapters.firestore_account_repo as account_module
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.domain.billing import AccountTier, AccountUsageStats, BillingAccount
from src.ports.account_repository import AccountRepository
from tests.support.clock_instants import UTC_MIDNIGHT, pin_datetime
from tests.support.firestore_eval import EvalDocRef
from tests.support.redelivery_world import require

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
ACCOUNTS = "eval_domain_accounts_v2"
T0 = UTC_MIDNIGHT.utc + timedelta(hours=10)      # 2026-10-27 10:00Z, mid-day: the lock-free path
COUNTERS = ("total_tokens", "total_cost", "total_requests", "daily_tokens", "daily_cost", "monthly_tokens",
            "monthly_cost")


class Accounts:
    def __init__(self, kit_db) -> None:
        self.db = kit_db(indexes=INDEXES_FILE, groups={ACCOUNTS: "development_domain_accounts_v2"})
        self.repo: AccountRepository = FirestoreAccountRepository(self.db, ACCOUNTS)

    @property
    def docs(self) -> dict:
        return self.db.collection(ACCOUNTS).docs

    def usage(self, account_id: str = "a1") -> dict:
        return self.docs[account_id]["usage"]

    async def seed(self, account_id: str = "a1", daily_cost: float = 1.0, reset_at=T0, **fields) -> BillingAccount:
        # every cost counter distinct, so a misread transform result cannot pass for the daily one
        usage = AccountUsageStats(total_tokens=0, total_cost=40.0, total_requests=3, daily_tokens=0,
                                  daily_cost=daily_cost, daily_reset_at=reset_at, monthly_tokens=0,
                                  monthly_cost=12.0, monthly_reset_at=reset_at)
        account = BillingAccount(account_id=account_id, usage=usage, iam_policy={"u1": "owner"},
                                 created_at=T0, updated_at=T0, **fields)
        return await self.repo.create_account(account)


@pytest.fixture
def accounts(monkeypatch, kit_db):
    pin_datetime(monkeypatch, account_module, T0)
    return Accounts(kit_db)


def io_trace(monkeypatch) -> list:
    """Every document read and write attempt, in order, as ("read" | "write", doc id): the race tests require from it
    that the interleaving they force actually happened (a serial run fails that require). Transaction commits are not
    writes through a reference, so a transactional race shows as the loser's extra read after its abort."""
    events: list = []

    def wrap(name, kind):
        real = getattr(EvalDocRef, name)

        async def traced(self, *args, **kwargs):
            events.append((kind, self.id))
            return await real(self, *args, **kwargs)

        monkeypatch.setattr(EvalDocRef, name, traced)

    wrap("get", "read")
    for name in ("set", "create", "update", "delete"):
        wrap(name, "write")
    return events


def test_the_adapter_implements_the_port():
    assert issubclass(FirestoreAccountRepository, AccountRepository)


# ------------------------------------------------------------------------------------------------------ CRUD

async def test_create_then_get_round_trips_the_account(accounts):
    account = await accounts.seed(tier=AccountTier.FAMILY)
    assert await accounts.repo.get_account("a1") == account
    assert accounts.docs["a1"] == account.model_dump()


async def test_get_of_an_absent_account_is_none(accounts):
    assert await accounts.repo.get_account("a1") is None


async def test_create_replaces_a_document_with_the_same_id(accounts):
    await accounts.seed(daily_cost=3.0, tier=AccountTier.PRO)
    replacement = BillingAccount(account_id="a1", iam_policy={"u2": "owner"}, created_at=T0, updated_at=T0,
                                 usage=AccountUsageStats(daily_reset_at=T0, monthly_reset_at=T0))
    await accounts.repo.create_account(replacement)
    assert accounts.docs["a1"] == replacement.model_dump()
    assert await accounts.repo.get_account("a1") == replacement


async def test_get_reads_a_legacy_account_with_missing_fields_as_defaults(accounts):
    accounts.docs["a1"] = {"account_id": "a1", "retired_field": "dropped"}
    account = await accounts.repo.get_account("a1")
    assert account.is_active is True and account.tier is AccountTier.FREE
    assert account.daily_cost_limit == 5.0 and account.iam_policy == {}
    assert account.usage.daily_cost == 0.0
    assert not hasattr(account, "retired_field")


# ------------------------------------------------------------------------------------------------- increment

async def test_lock_free_increment_adds_to_every_counter(accounts):
    await accounts.seed(daily_cost=1.0)
    before = dict(accounts.usage())
    await accounts.repo.increment_account_usage("a1", tokens=100, cost=0.5)
    after = accounts.usage()
    for field in COUNTERS:
        added = 1 if field == "total_requests" else (100 if field.endswith("tokens") else 0.5)
        assert after[field] == pytest.approx(before[field] + added), field
    assert after["daily_reset_at"] == T0 and after["monthly_reset_at"] == T0      # no window rotated
    assert after["prev_daily_date"] is None and after["prev_daily_cost"] == 0.0


async def test_increment_returns_the_servers_before_and_after(accounts):
    await accounts.seed(daily_cost=1.0)
    result = await accounts.repo.increment_account_usage("a1", tokens=100, cost=0.5)
    assert result.daily_cost_before == pytest.approx(1.0)
    assert result.daily_cost_after == pytest.approx(accounts.usage()["daily_cost"]) == pytest.approx(1.5)
    assert result.daily_cost_limit == 5.0


@pytest.mark.h1_only("patches the fake's EvalDocRef.update; on the real client the patch changes nothing")
async def test_an_increment_whose_write_reports_no_transforms_estimates_from_the_pre_read(accounts, monkeypatch):
    """The write landed; only the returned position is estimated (pre-read + cost) when the server's transform
    results are missing or do not match the transformed paths."""
    await accounts.seed(daily_cost=1.0)
    real_update = EvalDocRef.update

    async def update_without_transforms(self, data, option=None):
        await real_update(self, data, option=option)
        return WriteResult(update_time=T0, transform_results=[])

    monkeypatch.setattr(EvalDocRef, "update", update_without_transforms)
    result = await accounts.repo.increment_account_usage("a1", tokens=100, cost=0.5)
    assert (result.daily_cost_before, result.daily_cost_after) == (pytest.approx(1.0), pytest.approx(1.5))
    assert accounts.usage()["daily_cost"] == pytest.approx(1.5)


@pytest.mark.h1_only("traces the fake's EvalDocRef reads and writes and sets yield_on_get")
async def test_concurrent_increments_get_disjoint_slices_and_cross_once(accounts, monkeypatch):
    await accounts.seed(daily_cost=4.0)
    accounts.db.collection(ACCOUNTS).yield_on_get = True     # every caller pre-reads 4.0 before any update lands
    events = io_trace(monkeypatch)
    results = await asyncio.gather(*(accounts.repo.increment_account_usage("a1", tokens=10, cost=0.4)
                                     for _ in range(5)))
    require([kind for kind, _ in events][:5] == ["read"] * 5, "every caller read before any increment landed")
    slices = sorted((r.daily_cost_before, r.daily_cost_after) for r in results)
    assert slices[0][0] == pytest.approx(4.0) and slices[-1][1] == pytest.approx(6.0)
    assert all(a[1] == pytest.approx(b[0]) for a, b in zip(slices, slices[1:]))
    assert sum(r.crossed_daily_limit for r in results) == 1
    assert accounts.usage()["daily_cost"] == pytest.approx(6.0)


async def test_an_account_without_usage_starts_its_windows(accounts):
    accounts.docs["a1"] = {"account_id": "a1", "is_active": True}
    result = await accounts.repo.increment_account_usage("a1", tokens=100, cost=0.5)
    usage = accounts.usage()
    assert {f: usage[f] for f in COUNTERS} == {"total_tokens": 100, "total_cost": 0.5, "total_requests": 1,
                                                "daily_tokens": 100, "daily_cost": 0.5, "monthly_tokens": 100,
                                                "monthly_cost": 0.5}
    assert usage["daily_reset_at"] == T0 and usage["monthly_reset_at"] == T0
    assert usage["prev_daily_date"] is None
    assert (result.daily_cost_before, result.daily_cost_after) == (0.0, 0.5)


async def test_a_missing_daily_cost_limit_is_the_default(accounts):
    await accounts.seed(daily_cost=1.0)
    del accounts.docs["a1"]["daily_cost_limit"]
    result = await accounts.repo.increment_account_usage("a1", tokens=10, cost=0.1)
    assert result.daily_cost_limit == 5.0


async def test_increment_of_an_absent_account_raises_and_writes_nothing(accounts):
    with pytest.raises(ValueError):
        await accounts.repo.increment_account_usage("a1", tokens=10, cost=0.1)
    assert accounts.docs == {}


@pytest.mark.h1_only("traces the fake's EvalDocRef reads and writes and sets yield_on_get")
async def test_an_account_deleted_before_the_rotation_transaction_raises(accounts, monkeypatch):
    await accounts.seed(daily_cost=1.0, reset_at=T0 - timedelta(days=1))
    accounts.db.collection(ACCOUNTS).yield_on_get = True
    events = io_trace(monkeypatch)

    async def delete_meanwhile():
        await accounts.db.collection(ACCOUNTS).document("a1").delete()

    increment, _ = await asyncio.gather(accounts.repo.increment_account_usage("a1", tokens=10, cost=0.1),
                                        delete_meanwhile(), return_exceptions=True)
    require([kind for kind, _ in events][:3] == ["read", "write", "read"],
            "the delete landed between the pre-read and the rotation transaction's read")
    assert isinstance(increment, ValueError)
    assert accounts.docs == {}


# ------------------------------------------------------------------------------------------------------- list

async def test_list_returns_active_accounts_only(accounts):
    await accounts.seed("a1")
    await accounts.seed("a2", is_active=False)
    await accounts.seed("a3")
    listed = await accounts.repo.list_all_accounts()
    assert {a.account_id for a in listed} == {"a1", "a3"}


# ----------------------------------------------------------------------------------------------------- update

async def test_a_member_set_by_set_member_role_is_stored_and_read_back(accounts):
    # Members change only through set_member_role (update_account keeps iam_policy as stored).
    await accounts.seed()
    await accounts.repo.set_member_role("a1", "u2", "member")
    assert accounts.docs["a1"]["iam_policy"] == {"u1": "owner", "u2": "member"}
    assert (await accounts.repo.get_account("a1")).iam_policy == {"u1": "owner", "u2": "member"}


async def test_update_account_keeps_usage_counted_since_the_callers_read(accounts):
    """Flips when update_account writes the usage it was given (the caller's read of daily_cost 1.0)."""
    await accounts.seed(daily_cost=1.0)
    account = await accounts.repo.get_account("a1")                # a caller's read
    require(account.usage.daily_cost == pytest.approx(1.0), "the read predates the increment")
    await accounts.repo.increment_account_usage("a1", tokens=100, cost=0.5)
    require(accounts.usage()["daily_cost"] == pytest.approx(1.5), "the increment landed after the read")
    account.daily_cost_limit = 9.0
    await accounts.repo.update_account(account)
    require(accounts.docs["a1"]["daily_cost_limit"] == 9.0, "the update was stored")
    assert (accounts.usage()["daily_cost"], accounts.usage()["total_tokens"]) == (pytest.approx(1.5), 100)


async def test_update_account_of_an_absent_account_raises_and_writes_nothing(accounts):
    account = await accounts.seed()
    del accounts.docs["a1"]
    with pytest.raises(NotFound):
        await accounts.repo.update_account(account)
    assert accounts.docs == {}


# ----------------------------------------------------------------------------------------------------- errors

@pytest.mark.h1_only("patches the fake's EvalDocRef.get")
@pytest.mark.parametrize("method", ["get_account", "increment_account_usage"])
async def test_a_backend_error_propagates(accounts, monkeypatch, method):
    await accounts.seed()
    before = accounts.docs["a1"]

    async def unavailable(self, *args, **kwargs):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(EvalDocRef, "get", unavailable)
    args = ("a1",) if method == "get_account" else ("a1", 10, 0.1)
    with pytest.raises(RuntimeError, match="unavailable"):
        await getattr(accounts.repo, method)(*args)
    assert accounts.docs["a1"] == before


# ------------------------------------------------------------------------------------- increment: moved cases

async def test_an_accounts_own_daily_cost_limit_is_reported_and_crossed(accounts):
    await accounts.seed(daily_cost=20.0, daily_cost_limit=25.0)
    result = await accounts.repo.increment_account_usage("a1", tokens=10, cost=6.0)
    assert result.daily_cost_limit == 25.0
    assert result.crossed_daily_limit


async def test_an_integer_daily_cost_is_read_from_the_servers_integer_result(accounts):
    await accounts.seed()
    await accounts.db.collection(ACCOUNTS).document("a1").update({"usage.daily_cost": 5})
    result = await accounts.repo.increment_account_usage("a1", tokens=1, cost=2)
    assert (result.daily_cost_before, result.daily_cost_after) == (5.0, 7.0)


async def test_a_day_rotation_snapshots_the_day_that_ended_and_restarts_the_day(accounts):
    await accounts.seed()
    yesterday = T0 - timedelta(days=1)
    await accounts.db.collection(ACCOUNTS).document("a1").update(
        {"usage.daily_tokens": 1234, "usage.daily_cost": 0.07, "usage.daily_reset_at": yesterday})
    await accounts.repo.increment_account_usage("a1", tokens=10, cost=0.001)
    usage = accounts.usage()
    assert (usage["prev_daily_tokens"], usage["prev_daily_cost"], usage["prev_daily_date"]) == (
        1234, pytest.approx(0.07), yesterday.date().isoformat())
    assert (usage["daily_tokens"], usage["daily_cost"]) == (10, pytest.approx(0.001))


async def test_a_month_end_rotation_snapshots_the_month_that_ended(accounts):
    await accounts.seed(reset_at=T0.replace(day=1) - timedelta(days=1))       # stamped on the last day of September
    await accounts.repo.increment_account_usage("a1", tokens=10, cost=0.5)
    usage = accounts.usage()
    assert (usage["prev_monthly_month"], usage["prev_monthly_cost"]) == ("2026-09", pytest.approx(12.0))
    assert usage["daily_reset_at"] == T0 and usage["monthly_reset_at"] == T0


ROTATIONS = {"same_window": {}, "daily": {"usage.daily_reset_at": T0 - timedelta(days=1)},
             "monthly": {"usage.monthly_reset_at": T0.replace(day=1) - timedelta(days=1)},
             "no_stamps": {"usage.daily_reset_at": None, "usage.monthly_reset_at": None}}


@pytest.mark.h1_only("counts the fake's transactions and traces its EvalDocRef writes")
@pytest.mark.parametrize("window", list(ROTATIONS))
async def test_only_an_increment_that_rotates_a_window_opens_a_transaction(accounts, monkeypatch, window):
    """The same-window hot path is one lock-free update; a due rotation is one transaction and no direct write."""
    await accounts.seed(daily_cost=3.0)
    if ROTATIONS[window]:
        await accounts.db.collection(ACCOUNTS).document("a1").update(ROTATIONS[window])
    opened = []
    make_transaction = accounts.db.transaction
    monkeypatch.setattr(accounts.db, "transaction", lambda: opened.append(1) or make_transaction())
    events = io_trace(monkeypatch)
    result = await accounts.repo.increment_account_usage("a1", tokens=10, cost=0.4)
    writes = [e for e in events if e[0] == "write"]
    if window == "same_window":
        assert (opened, writes) == ([], [("write", "a1")])
    else:
        assert (len(opened), writes) == (1, [])
    if window in ("daily", "no_stamps"):
        assert result.daily_cost_before == 0.0
