"""TaskClaimStore kit, gap file (P11 · G1): the docstring of `src/ports/task_claim_store.py` checked against
`FirestoreTaskClaimStore` on the evaluating Firestore fake (H1), index refusal ON, for what
`test_firestore_task_claim_store.py` (H1, S1b), the handler tests and the P15 redelivery tests do not already pin:
legacy expiry and state shapes, the done window, mark_done and release lost to a takeover, and the collection shared
with the DedupStore marks. That file is not repeated here.

One `EvalDb` holds the claim store (1 h) and the two mark stores of the shared collection (48 h, 72 h), as
`application_graph.py` builds them.
"""
import asyncio
import re
from datetime import timedelta
from pathlib import Path

import pytest

import src.adapters.firestore_dedup_store as dedup_module
import src.adapters.firestore_task_claim_store as claim_module
from src.adapters.firestore_dedup_store import FirestoreDedupStore
from src.adapters.firestore_task_claim_store import FirestoreTaskClaimStore
from src.ports.task_claim_store import TaskClaimStore
from tests.support.clock_instants import UTC_MIDNIGHT, pin_datetime
from tests.support.firestore_eval import EvalDb, EvalDocRef

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
SHARED = "eval_worker_task_dedup"
T0 = UTC_MIDNIGHT.utc
TASK = "execute_reminder-0f3a"


class Shared:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={SHARED: "development_worker_task_dedup"})
        self.claims: TaskClaimStore = FirestoreTaskClaimStore(self.db, SHARED, ttl_seconds=3600)
        self.daily = FirestoreDedupStore(self.db, SHARED, ttl_seconds=172800)
        self.webhook = FirestoreDedupStore(self.db, SHARED, ttl_seconds=259200)

    @property
    def col(self):
        return self.db.collection(SHARED)

    @property
    def docs(self) -> dict:
        return self.col.docs


def at(monkeypatch, delta: timedelta = timedelta(0)) -> None:
    for module in (claim_module, dedup_module):
        pin_datetime(monkeypatch, module, T0 + delta)


@pytest.fixture
def shared(monkeypatch):
    at(monkeypatch)
    return Shared()


async def test_a_claim_without_expires_at_is_expired_and_taken_over_even_when_done(shared):
    shared.docs[TASK] = {"holder": "p-old", "attempt": "a1", "state": "done"}
    assert await shared.claims.try_claim(TASK, "a2") is True
    assert shared.docs[TASK]["attempt"] == "a2" and shared.docs[TASK]["state"] == "running"


async def test_a_non_datetime_expires_at_counts_as_expired(shared):
    shared.docs[TASK] = {"holder": "p-old", "attempt": "a1", "state": "done", "expires_at": "2099-01-01T00:00:00Z"}
    assert await shared.claims.try_claim(TASK, "a2") is True
    assert shared.docs[TASK]["attempt"] == "a2"


async def test_a_naive_expires_at_is_read_as_utc_defensive_branch(shared):
    """A defensive branch, not a contract: the client always returns an aware Timestamp; a naive value exists only
    when a test seeds one into the fake."""
    shared.docs[TASK] = {"holder": "p-old", "attempt": "a1", "state": "done",
                         "expires_at": (T0 + timedelta(minutes=5)).replace(tzinfo=None)}
    assert await shared.claims.try_claim(TASK, "a2") is False
    assert shared.docs[TASK]["attempt"] == "a1"


@pytest.mark.parametrize("state", [None, "paused"], ids=["no_state", "unknown_state"])
async def test_a_claim_without_state_or_with_an_unknown_state_is_taken_over(shared, state):
    raw = {"holder": "p-old", "attempt": "a1", "created_at": T0, "expires_at": T0 + timedelta(minutes=30)}
    if state is not None:
        raw["state"] = state
    shared.docs[TASK] = raw
    assert await shared.claims.try_claim(TASK, "a2") is True
    assert shared.docs[TASK]["attempt"] == "a2" and shared.docs[TASK]["state"] == "running"


async def test_mark_done_restarts_the_window_from_the_finish(shared, monkeypatch):
    assert await shared.claims.try_claim(TASK, "a1") is True
    at(monkeypatch, timedelta(minutes=50))
    await shared.claims.mark_done(TASK, "a1")
    assert shared.docs[TASK]["state"] == "done"
    assert shared.docs[TASK]["expires_at"] == T0 + timedelta(minutes=110)
    at(monkeypatch, timedelta(minutes=100))
    assert await shared.claims.try_claim(TASK, "a2") is False


async def test_mark_done_lost_to_a_takeover_changes_nothing(shared):
    assert await shared.claims.try_claim(TASK, "a1") is True
    shared.col.yield_on_get = True      # both read the a1 claim; the takeover writes first, mark_done's precondition fails
    await asyncio.gather(shared.claims.try_claim(TASK, "a2"), shared.claims.mark_done(TASK, "a1"))
    assert shared.docs[TASK]["attempt"] == "a2" and shared.docs[TASK]["state"] == "running"


async def test_release_lost_to_a_takeover_keeps_the_new_claim(shared):
    assert await shared.claims.try_claim(TASK, "a1") is True
    shared.col.yield_on_get = True      # both read the a1 claim; the takeover writes first, release's precondition fails
    await asyncio.gather(shared.claims.try_claim(TASK, "a2"), shared.claims.release(TASK, "a1"))
    assert shared.docs[TASK]["attempt"] == "a2" and shared.docs[TASK]["state"] == "running"


async def test_claims_and_marks_share_the_collection_each_on_its_own_expiry(shared, monkeypatch):
    daily, webhook = "daily_review:u1:2026-10-27", "openai_deep_research:job1"
    assert await shared.claims.try_claim(TASK, "a1") is True
    assert await shared.daily.try_mark_processed(daily) is True
    assert await shared.webhook.try_mark_processed(webhook) is True
    claim_keys = [k for k, v in shared.docs.items() if "attempt" in v]
    mark_keys = [k for k, v in shared.docs.items() if "attempt" not in v]
    assert claim_keys == [TASK] and all(re.fullmatch(r"[A-Za-z0-9_-]+", k) for k in claim_keys)
    assert sorted(mark_keys) == sorted([daily, webhook]) and all(":" in k for k in mark_keys)

    at(monkeypatch, timedelta(hours=2))
    assert await shared.daily.is_duplicate(daily) is True
    assert await shared.webhook.is_duplicate(webhook) is True
    assert await shared.claims.try_claim(TASK, "a2") is True        # the 1 h claim has expired

    at(monkeypatch, timedelta(hours=49))
    assert await shared.daily.try_mark_processed(daily) is True      # the 48 h mark has expired
    assert await shared.webhook.is_duplicate(webhook) is True        # the 72 h claim is still live


async def test_a_backend_error_in_try_claim_propagates(shared, monkeypatch):
    async def unavailable(self, data):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(EvalDocRef, "create", unavailable)
    with pytest.raises(RuntimeError, match="unavailable"):
        await shared.claims.try_claim(TASK, "a1")
    assert shared.docs == {}
