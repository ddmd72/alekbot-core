"""FirestoreTaskClaimStore on the evaluating fake: take-over of dead claims, fenced done/release."""
import asyncio
from datetime import datetime, timedelta, timezone

from google.api_core.exceptions import FailedPrecondition

from src.adapters.firestore_task_claim_store import FirestoreTaskClaimStore
from tests.support.firestore_eval import EvalDb, EvalDocRef

COL = "claims"


def _store(db, holder=None):
    s = FirestoreTaskClaimStore(db_client=db, collection_name=COL, ttl_seconds=3600)
    if holder:
        s.holder = holder
    return s


def _doc(db, key="t1"):
    return db.collection(COL).docs.get(key)


async def test_first_claim_creates_running_doc_with_holder_and_attempt():
    db = EvalDb()
    s = _store(db)
    assert await s.try_claim("t1", "a1")
    d = _doc(db)
    assert (d["holder"], d["attempt"], d["state"]) == (s.holder, "a1", "running")
    assert d["expires_at"] - d["created_at"] == timedelta(seconds=3600)


async def test_same_attempt_recreate_is_ours():
    s = _store(EvalDb())
    assert await s.try_claim("t1", "a1")
    assert await s.try_claim("t1", "a1")


def test_process_id_is_shared_by_stores_of_one_process():
    assert _store(EvalDb()).holder == _store(EvalDb()).holder


async def test_running_claim_of_another_process_is_taken_over():
    db = EvalDb()
    await _store(db, "proc-old").try_claim("t1", "a1")
    new = _store(db, "proc-new")
    assert await new.try_claim("t1", "a2")
    assert (_doc(db)["holder"], _doc(db)["attempt"]) == ("proc-new", "a2")


async def test_running_claim_of_this_process_is_taken_over_the_handler_guards_live_ones():
    db = EvalDb()
    s = _store(db)
    await s.try_claim("t1", "a1")
    assert await s.try_claim("t1", "a2")
    assert _doc(db)["attempt"] == "a2"


async def test_done_claim_is_duplicate_even_from_another_process():
    db = EvalDb()
    old = _store(db, "proc-old")
    await old.try_claim("t1", "a1")
    await old.mark_done("t1", "a1")
    assert not await _store(db, "proc-new").try_claim("t1", "a2")


async def test_expired_claim_is_taken_over_even_when_done():
    db = EvalDb()
    s = _store(db)
    await s.try_claim("t1", "a1")
    await s.mark_done("t1", "a1")
    _doc(db)["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert await s.try_claim("t1", "a2")
    assert (_doc(db)["attempt"], _doc(db)["state"]) == ("a2", "running")


async def test_two_concurrent_takeovers_of_a_dead_claim_exactly_one_wins():
    db = EvalDb()
    await _store(db, "proc-old").try_claim("t1", "a1")
    db.collection(COL).__dict__["yield_on_get"] = True  # both hold the same snapshot
    s = _store(db, "proc-new")
    results = await asyncio.gather(s.try_claim("t1", "b1"), s.try_claim("t1", "b2"))
    assert sorted(results) == [False, True]


async def test_release_by_a_non_holder_attempt_keeps_the_newer_claim():
    db = EvalDb()
    s = _store(db)
    await s.try_claim("t1", "a1")
    await s.try_claim("t1", "a2")  # takes over a1
    await s.release("t1", "a1")
    assert _doc(db)["attempt"] == "a2"
    await s.release("t1", "a2")
    assert _doc(db) is None


async def test_mark_done_by_a_non_holder_attempt_changes_nothing():
    db = EvalDb()
    s = _store(db)
    await s.try_claim("t1", "a1")
    await s.try_claim("t1", "a2")
    await s.mark_done("t1", "a1")
    assert _doc(db)["state"] == "running"


async def test_release_and_done_on_missing_claim_are_quiet():
    s = _store(EvalDb())
    await s.release("nope", "a1")
    await s.mark_done("nope", "a1")


def _lose_response_after_landing(monkeypatch, attempt):
    """The SDK retried a take-over that had landed: the write is applied, the caller sees a precondition failure."""
    original = EvalDocRef.update

    async def update(self, data, option=None):
        await original(self, data, option)
        if data.get("attempt") == attempt:
            raise FailedPrecondition("retried after landing")

    monkeypatch.setattr(EvalDocRef, "update", update)


async def test_takeover_that_landed_then_failed_precondition_is_ours(monkeypatch):
    db = EvalDb()
    await _store(db, "proc-old").try_claim("t1", "a1")
    _lose_response_after_landing(monkeypatch, "a2")
    assert await _store(db, "proc-new").try_claim("t1", "a2")
    assert _doc(db)["attempt"] == "a2"


async def test_lost_takeover_to_a_different_attempt_is_a_duplicate(monkeypatch):
    db = EvalDb()
    await _store(db, "proc-old").try_claim("t1", "a1")
    original = EvalDocRef.update

    async def update(self, data, option=None):
        await original(self, {**data, "attempt": "rival"})  # someone else won
        raise FailedPrecondition("changed")

    monkeypatch.setattr(EvalDocRef, "update", update)
    assert not await _store(db, "proc-new").try_claim("t1", "a2")


async def test_takeover_whose_doc_vanished_retries_the_create_once(monkeypatch):
    db = EvalDb()
    await _store(db, "proc-old").try_claim("t1", "a1")

    async def update(self, data, option=None):
        await EvalDocRef.delete(self)  # released by its owner in between
        raise FailedPrecondition("changed")

    monkeypatch.setattr(EvalDocRef, "update", update)
    assert await _store(db, "proc-new").try_claim("t1", "a2")
    assert _doc(db)["attempt"] == "a2"
