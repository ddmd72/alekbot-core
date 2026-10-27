"""DedupStore kit (P11 · G1): the docstring of `src/ports/dedup_store.py` checked against `FirestoreDedupStore` on the
evaluating Firestore fake (H1), with the index refusal ON (no query today). The three stores are built as production
builds them (`application_graph.py`): the 48 h daily-review marks and the 72 h OpenAI webhook claims on the shared
`worker_task_dedup` collection, the 1 h Telegram inline marks on `event_dedup`. Assertions are on the raw stored
documents and on what the store answers.

Not covered, with reasons: `mark_processed` (adapter-only, no caller in src/), and the `except NotFound` after the
preconditioned rewrite (`firestore_dedup_store.py:107-108`): a `last_update_time` precondition on a missing document
answers FailedPrecondition on Firestore (P5 scenario `update_precondition_deleted`), so that branch is unreachable and a
mark released between the expired read and the rewrite gives way (False).
"""
import asyncio
import time
from datetime import timedelta
from pathlib import Path

import pytest

import src.adapters.firestore_dedup_store as dedup_module
from src.adapters.firestore_dedup_store import FirestoreDedupStore
from src.ports.dedup_store import DedupStore
from tests.support.clock_instants import UTC_MIDNIGHT, pin_datetime
from tests.support.firestore_eval import EvalDb, EvalDocRef

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
SHARED = "eval_worker_task_dedup"
EVENTS = "eval_event_dedup"
T0 = UTC_MIDNIGHT.utc
DAILY_TTL, WEBHOOK_TTL, TELEGRAM_TTL = 172800, 259200, 3600
DAILY_KEY = "daily_review:u1:2026-10-27"
WEBHOOK_KEY = "openai_deep_research:job1"
TELEGRAM_KEY = "telegram::1001"


class Stores:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={SHARED: "development_worker_task_dedup",
                                                       EVENTS: "development_event_dedup"})
        self.daily: DedupStore = FirestoreDedupStore(self.db, SHARED, ttl_seconds=DAILY_TTL)
        self.webhook: DedupStore = FirestoreDedupStore(self.db, SHARED, ttl_seconds=WEBHOOK_TTL)
        self.telegram: DedupStore = FirestoreDedupStore(self.db, EVENTS, ttl_seconds=TELEGRAM_TTL)

    def docs(self, collection: str = SHARED) -> dict:
        return self.db.collection(collection).docs


@pytest.fixture
def stores(monkeypatch):
    pin_datetime(monkeypatch, dedup_module, T0)
    return Stores()


def at(monkeypatch, delta: timedelta) -> None:
    pin_datetime(monkeypatch, dedup_module, T0 + delta)


WRITERS = [("daily", SHARED, DAILY_KEY, DAILY_TTL), ("webhook", SHARED, WEBHOOK_KEY, WEBHOOK_TTL),
           ("telegram", EVENTS, TELEGRAM_KEY, TELEGRAM_TTL)]


def test_the_adapter_implements_the_port():
    assert issubclass(FirestoreDedupStore, DedupStore)


@pytest.mark.parametrize("name,collection,key,ttl", WRITERS, ids=[w[0] for w in WRITERS])
async def test_a_fresh_claim_writes_a_mark_for_the_writers_ttl(stores, name, collection, key, ttl):
    assert await getattr(stores, name).try_mark_processed(key) is True
    raw = stores.docs(collection)[key]
    assert set(raw) == {"created_at", "expires_at"}
    assert isinstance(raw["created_at"], float)
    assert raw["expires_at"] == T0 + timedelta(seconds=ttl)


async def test_a_live_mark_is_refused_and_left_unchanged(stores):
    assert await stores.daily.try_mark_processed(DAILY_KEY) is True
    before = dict(stores.docs()[DAILY_KEY])
    assert await stores.daily.try_mark_processed(DAILY_KEY) is False
    assert stores.docs()[DAILY_KEY] == before


async def test_liveness_follows_the_docs_own_expires_at_across_writers(stores, monkeypatch):
    """A reader with a shorter TTL than the writer's still sees the mark live until the mark's own expires_at."""
    one_hour_reader = FirestoreDedupStore(stores.db, SHARED, ttl_seconds=TELEGRAM_TTL)
    await stores.daily.try_mark_processed(DAILY_KEY)
    await stores.webhook.try_mark_processed(WEBHOOK_KEY)
    at(monkeypatch, timedelta(hours=2))
    assert await one_hour_reader.is_duplicate(DAILY_KEY) is True
    assert await one_hour_reader.try_mark_processed(DAILY_KEY) is False
    at(monkeypatch, timedelta(hours=49))
    assert await stores.daily.is_duplicate(WEBHOOK_KEY) is True
    assert await stores.daily.try_mark_processed(WEBHOOK_KEY) is False


async def test_an_expired_unpurged_mark_is_reclaimed_once(stores):
    stores.docs()[DAILY_KEY] = {"created_at": time.time() - DAILY_TTL - 60,
                                "expires_at": T0 - timedelta(minutes=1)}
    assert await stores.daily.try_mark_processed(DAILY_KEY) is True
    assert stores.docs()[DAILY_KEY]["expires_at"] == T0 + timedelta(seconds=DAILY_TTL)
    assert await stores.daily.try_mark_processed(DAILY_KEY) is False


async def test_concurrent_claims_of_a_missing_mark_exactly_one_wins(stores):
    results = await asyncio.gather(*(stores.webhook.try_mark_processed(WEBHOOK_KEY) for _ in range(4)))
    assert sorted(results) == [False, False, False, True]
    assert list(stores.docs()) == [WEBHOOK_KEY]


async def test_concurrent_reclaims_of_an_expired_mark_exactly_one_wins(stores):
    stores.docs()[WEBHOOK_KEY] = {"created_at": time.time() - WEBHOOK_TTL - 60,
                                  "expires_at": T0 - timedelta(minutes=1)}
    stores.db.collection(SHARED).yield_on_get = True     # every claimer holds the same expired snapshot
    results = await asyncio.gather(*(stores.webhook.try_mark_processed(WEBHOOK_KEY) for _ in range(4)))
    assert sorted(results) == [False, False, False, True]
    assert stores.docs()[WEBHOOK_KEY]["expires_at"] == T0 + timedelta(seconds=WEBHOOK_TTL)


@pytest.mark.parametrize("age,live", [(60, True), (TELEGRAM_TTL + 60, False)], ids=["fresh", "old"])
async def test_a_legacy_mark_without_expires_at_lives_for_the_readers_ttl_from_created_at(stores, age, live):
    stores.docs(EVENTS)[TELEGRAM_KEY] = {"created_at": time.time() - age}
    assert await stores.telegram.is_duplicate(TELEGRAM_KEY) is live
    assert await stores.telegram.try_mark_processed(TELEGRAM_KEY) is (not live)


async def test_a_legacy_mark_with_neither_field_is_expired(stores):
    stores.docs(EVENTS)[TELEGRAM_KEY] = {}
    assert await stores.telegram.try_mark_processed(TELEGRAM_KEY) is True
    assert stores.docs(EVENTS)[TELEGRAM_KEY]["expires_at"] == T0 + timedelta(seconds=TELEGRAM_TTL)


async def test_a_naive_expires_at_is_utc(stores):
    """Seeded raw: the client never returns a naive value, the store still reads one as UTC."""
    stores.docs()[DAILY_KEY] = {"created_at": 0.0, "expires_at": (T0 + timedelta(minutes=5)).replace(tzinfo=None)}
    assert await stores.daily.is_duplicate(DAILY_KEY) is True


@pytest.mark.parametrize("key", ["", None])
async def test_an_empty_key_is_never_marked(stores, key):
    assert await stores.daily.try_mark_processed(key) is False
    assert await stores.daily.is_duplicate(key) is False
    await stores.daily.release(key)
    assert stores.docs() == {} and stores.db.collection(SHARED).versions == {}


async def test_is_duplicate_of_an_absent_mark_is_false(stores):
    assert await stores.daily.is_duplicate(DAILY_KEY) is False


async def test_release_deletes_and_an_absent_release_is_quiet(stores):
    await stores.webhook.try_mark_processed(WEBHOOK_KEY)
    await stores.webhook.release(WEBHOOK_KEY)
    await stores.webhook.release(WEBHOOK_KEY)
    assert stores.docs() == {}
    assert await stores.webhook.try_mark_processed(WEBHOOK_KEY) is True


@pytest.mark.parametrize("flapping,claimed", [(False, True), (True, False)], ids=["released_once", "released_twice"])
async def test_a_mark_released_between_create_and_read_is_tried_once_more(stores, monkeypatch, flapping, claimed):
    """Another holder releases its mark right after our create met it: one more create, then give way."""
    real_create = EvalDocRef.create
    seen = []

    async def create_meets_a_mark_that_is_then_released(self, data):
        if not seen or flapping:
            self.col.docs[self.id] = {"created_at": time.time(), "expires_at": T0 + timedelta(hours=1)}
        seen.append(self.id)
        try:
            return await real_create(self, data)
        finally:
            if len(seen) == 1 or flapping:
                self.col.docs.pop(self.id, None)        # the other holder's release lands before our read

    monkeypatch.setattr(EvalDocRef, "create", create_meets_a_mark_that_is_then_released)
    assert await stores.daily.try_mark_processed(DAILY_KEY) is claimed
    assert len(seen) == 2
    assert (DAILY_KEY in stores.docs()) is claimed


async def test_a_backend_error_propagates_and_marks_nothing(stores, monkeypatch):
    async def unavailable(self, data):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(EvalDocRef, "create", unavailable)
    with pytest.raises(RuntimeError, match="unavailable"):
        await stores.daily.try_mark_processed(DAILY_KEY)
    assert stores.docs() == {}
