"""WindowCounter kit: the docstring of `src/ports/window_counter.py` checked against `FirestoreWindowCounter` on the
evaluating Firestore fake, with assertions on what `increment` returns and on the raw stored documents (EXTERNAL_TASK_QUEUE_RFC
§5.11)."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.adapters.firestore_window_counter import FirestoreWindowCounter
from src.ports.window_counter import WindowCounter

COLLECTION = "eval_worker_task_dedup"
WINDOW = 3600
T0 = datetime(2026, 11, 23, 10, 0, 0, tzinfo=timezone.utc)      # a window start (10:00 is a multiple of an hour)


@pytest.fixture
def backend(kit_db):
    db = kit_db(groups={COLLECTION: "development_worker_task_dedup"})
    env = SimpleNamespace(worker_task_dedup_collection=COLLECTION)
    counter: WindowCounter = FirestoreWindowCounter(db, env)
    return SimpleNamespace(db=db, counter=counter)


def stored(backend) -> dict:
    return {doc_id: dict(data) for doc_id, data in backend.db.collection(COLLECTION).docs.items()}


async def test_each_hit_of_a_window_counts_one_more(backend):
    counts = [await backend.counter.increment("calls:usr-1:wrk-1", WINDOW, T0 + timedelta(seconds=s))
              for s in (0, 5, 3599)]
    assert counts == [1, 2, 3]


async def test_the_next_window_starts_again_at_one(backend):
    await backend.counter.increment("calls:usr-1:wrk-1", WINDOW, T0)
    await backend.counter.increment("calls:usr-1:wrk-1", WINDOW, T0)
    assert await backend.counter.increment("calls:usr-1:wrk-1", WINDOW, T0 + timedelta(seconds=WINDOW)) == 1


async def test_keys_do_not_share_a_count(backend):
    await backend.counter.increment("calls:usr-1:wrk-1", WINDOW, T0)
    assert await backend.counter.increment("calls:usr-1:wrk-2", WINDOW, T0) == 1
    assert await backend.counter.increment("claims:usr-1:wrk-1", WINDOW, T0) == 1


async def test_a_hit_is_one_document_named_by_key_and_window_with_its_expiry(backend):
    await backend.counter.increment("401:addr:203.0.113.9", WINDOW, T0 + timedelta(seconds=17))
    docs = stored(backend)
    start = int(T0.timestamp())
    assert list(docs) == [f"xtrate:401:addr:203.0.113.9:{start}"]
    doc = docs[f"xtrate:401:addr:203.0.113.9:{start}"]
    assert doc["count"] == 1
    assert doc["expires_at"] == T0 + timedelta(seconds=2 * WINDOW)


async def test_the_name_always_has_a_colon_so_it_is_never_a_cloud_tasks_claim_key(backend):
    await backend.counter.increment("401:global", WINDOW, T0)
    assert all(":" in doc_id for doc_id in stored(backend))


async def test_concurrent_hits_each_get_a_distinct_count(backend):
    counts = await asyncio.gather(*(backend.counter.increment("calls:usr-1:wrk-1", WINDOW, T0) for _ in range(8)))
    assert sorted(counts) == list(range(1, 9))
