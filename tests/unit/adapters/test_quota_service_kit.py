"""QuotaService kit (P11 · G1): the docstring of `src/ports/quota_service.py` checked against `FirestoreQuotaService`
over the real `FirestoreAccountRepository` on the evaluating Firestore fake (H1, index refusal ON), with a recording
alert sink. The existing quota tests run on an `AsyncMock` repository; here the crossing comes from the stored counters.
"""
import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

import src.adapters.firestore_account_repo as account_module
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_quota_service import FirestoreQuotaService
from src.domain.billing import AccountUsageStats, BillingAccount
from src.ports.alert_sink import AlertSinkPort
from src.ports.quota_service import QuotaService
from tests.support.clock_instants import UTC_MIDNIGHT, pin_datetime
from tests.support.redelivery_world import require
from tests.support.firestore_eval import EvalDb, EvalDocRef

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
ACCOUNTS = "eval_domain_accounts_v2"
T0 = UTC_MIDNIGHT.utc + timedelta(hours=10)
MODEL = "gemini-flash-latest"


class RecordingSink(AlertSinkPort):
    def __init__(self, fails: bool = False) -> None:
        self.posts: list = []
        self.fails = fails

    async def post(self, text: str) -> None:
        if self.fails:
            raise RuntimeError("webhook unavailable")
        self.posts.append(text)


class Billing:
    def __init__(self, sink=None) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={ACCOUNTS: "development_domain_accounts_v2"})
        self.repo = FirestoreAccountRepository(self.db, ACCOUNTS)
        self.sink = sink
        self.quota: QuotaService = FirestoreQuotaService(self.repo, sink)

    def usage(self) -> dict:
        return self.db.collection(ACCOUNTS).docs["a1"]["usage"]

    async def seed(self, daily_cost: float, reset_at=T0) -> None:
        # every cost counter distinct, so a misread transform result cannot pass for the daily one
        usage = AccountUsageStats(daily_cost=daily_cost, daily_reset_at=reset_at, monthly_cost=12.0,
                                  monthly_reset_at=reset_at, total_cost=40.0)
        await self.repo.create_account(BillingAccount(account_id="a1", usage=usage, created_at=T0, updated_at=T0))


@pytest.fixture
def billing(monkeypatch):
    pin_datetime(monkeypatch, account_module, T0)
    return Billing(RecordingSink())


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
    assert issubclass(FirestoreQuotaService, QuotaService)


async def test_record_usage_is_durable_before_it_returns(billing):
    await billing.seed(daily_cost=1.0)
    assert await billing.quota.record_usage("a1", MODEL, 300, 0.25) is None
    usage = billing.usage()
    assert usage["daily_cost"] == pytest.approx(1.25) and usage["daily_tokens"] == 300
    assert usage["total_requests"] == 1


async def test_the_crossing_increment_posts_once(billing):
    await billing.seed(daily_cost=4.6)
    await billing.quota.record_usage("a1", MODEL, 300, 0.5)
    assert len(billing.sink.posts) == 1
    assert "a1" in billing.sink.posts[0] and "$5.1000" in billing.sink.posts[0] and "$5.00" in billing.sink.posts[0]


@pytest.mark.parametrize("start", [1.0, 5.5], ids=["below", "already_over"])
async def test_below_the_limit_and_already_over_post_nothing(billing, start):
    await billing.seed(daily_cost=start)
    await billing.quota.record_usage("a1", MODEL, 300, 0.5)
    assert billing.sink.posts == []
    assert billing.usage()["daily_cost"] == pytest.approx(start + 0.5)


async def test_concurrent_calls_cross_exactly_once(billing, monkeypatch):
    await billing.seed(daily_cost=4.0)
    billing.db.collection(ACCOUNTS).yield_on_get = True      # every call pre-reads 4.0 before any increment lands
    events = io_trace(monkeypatch)
    await asyncio.gather(*(billing.quota.record_usage("a1", MODEL, 10, 0.4) for _ in range(5)))
    require([kind for kind, _ in events][:5] == ["read"] * 5, "every call read before any increment landed")
    assert len(billing.sink.posts) == 1
    assert billing.usage()["daily_cost"] == pytest.approx(6.0)


async def test_the_days_first_call_that_alone_crosses_posts_once(billing):
    await billing.seed(daily_cost=4.9, reset_at=T0 - timedelta(days=1))
    await billing.quota.record_usage("a1", MODEL, 300, 6.0)
    assert len(billing.sink.posts) == 1
    assert billing.usage()["daily_cost"] == pytest.approx(6.0)
    assert billing.usage()["prev_daily_cost"] == pytest.approx(4.9)


async def test_a_new_utc_day_can_cross_again(billing, monkeypatch):
    await billing.seed(daily_cost=4.6)
    await billing.quota.record_usage("a1", MODEL, 300, 0.5)
    pin_datetime(monkeypatch, account_module, T0 + timedelta(days=1))
    await billing.quota.record_usage("a1", MODEL, 300, 4.6)
    await billing.quota.record_usage("a1", MODEL, 300, 0.5)
    assert len(billing.sink.posts) == 2


async def test_a_repository_error_is_swallowed_and_posts_nothing(billing):
    assert await billing.quota.record_usage("a1", MODEL, 300, 6.0) is None     # absent account: ValueError inside
    assert billing.sink.posts == []
    assert billing.db.collection(ACCOUNTS).docs == {}


async def test_a_failing_sink_is_swallowed(monkeypatch):
    pin_datetime(monkeypatch, account_module, T0)
    billing = Billing(RecordingSink(fails=True))
    await billing.seed(daily_cost=4.6)
    assert await billing.quota.record_usage("a1", MODEL, 300, 0.5) is None
    assert billing.usage()["daily_cost"] == pytest.approx(5.1)


async def test_no_sink_is_a_no_op(monkeypatch):
    pin_datetime(monkeypatch, account_module, T0)
    billing = Billing(None)
    await billing.seed(daily_cost=4.6)
    assert await billing.quota.record_usage("a1", MODEL, 300, 0.5) is None
    assert billing.usage()["daily_cost"] == pytest.approx(5.1)
