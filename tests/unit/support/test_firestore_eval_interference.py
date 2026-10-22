"""The fake's test hooks for transactional interference, the order of `get_all` and the session-wide transaction decorator
(tests/support/firestore_eval.py, Facts S2 T0). The conftest fixture installs the dispatching decorator, so a body here is
decorated with the plain `firestore.async_transactional`."""
import pytest
from google.api_core.exceptions import Aborted
from google.cloud import firestore

from tests.support.firestore_eval import EvalDb, dispatching_transactions, eval_transactions


@pytest.fixture
def db():
    return EvalDb()


@pytest.fixture
def col(db):
    c = db.collection("c")
    c.docs["a"] = {"v": 1}
    c.versions["a"] = 1
    return c


def _reader(col, seen, write=True):
    @firestore.async_transactional
    async def body(tx):
        snap = await col.document("a").get(transaction=tx)
        seen.append((snap.to_dict()["v"], snap.update_time))
        if write:
            tx.update(col.document("a"), {"v": snap.to_dict()["v"] + 10})

    return body


async def test_interference_before_read_changes_update_time_seen_by_the_transaction(db, col):
    token_before = (await col.document("a").get()).update_time
    db.interfere_on_txn_read("c", "a", {"v": 2})
    seen = []
    await _reader(col, seen)(db.transaction())
    assert len(seen) == 1                                   # the body did not re-run: the snapshot already held the change
    assert seen[0][0] == 2 and seen[0][1] != token_before   # the new data and a new token
    assert col.docs["a"] == {"v": 12}


async def test_interference_after_read_aborts_and_reruns_the_body(db, col):
    token_before = (await col.document("a").get()).update_time
    db.interfere_on_txn_read("c", "a", {"v": 2}, after_read=True)
    seen = []
    await _reader(col, seen)(db.transaction())
    assert [v for v, _ in seen] == [1, 2]                   # first attempt held the old snapshot, the re-run the new one
    assert seen[0][1] == token_before and seen[1][1] != token_before
    assert col.docs["a"] == {"v": 12}


async def test_two_interferences_fire_on_two_reads(db, col):
    db.interfere_on_txn_read("c", "a", {"v": 2}, after_read=True)
    db.interfere_on_txn_read("c", "a", {"v": 3}, after_read=True)
    seen = []
    await _reader(col, seen)(db.transaction())
    assert [v for v, _ in seen] == [1, 2, 3]                # one conflict per read, then a clean third attempt
    assert col.docs["a"] == {"v": 13}

    db.interfere_on_txn_read("c", "a", {"v": 20})
    db.interfere_on_txn_read("c", "a", {"v": 30})
    first, second, third = [], [], []
    await _reader(col, first, write=False)(db.transaction())
    await _reader(col, second, write=False)(db.transaction())
    await _reader(col, third, write=False)(db.transaction())
    assert [first[0][0], second[0][0], third[0][0]] == [20, 30, 30]


async def test_a_plain_read_does_not_consume_an_interference(db, col):
    db.interfere_on_txn_read("c", "a", {"v": 2})
    assert (await col.document("a").get()).to_dict() == {"v": 1}
    assert col.docs["a"] == {"v": 1}
    seen = []
    await _reader(col, seen, write=False)(db.transaction())
    assert seen[0][0] == 2


async def test_raise_on_txn_read_raises_once(db, col):
    db.raise_on_txn_read("c", "a", Aborted("read aborted"))
    seen = []
    with pytest.raises(Aborted):
        await _reader(col, seen)(db.transaction())
    assert seen == [] and col.docs["a"] == {"v": 1}         # not retried, nothing written
    await _reader(col, seen)(db.transaction())              # one-shot: the next transaction reads normally
    assert [v for v, _ in seen] == [1] and col.docs["a"] == {"v": 11}


async def test_get_all_does_not_keep_input_order(db):
    c = db.collection("c")
    for doc_id in ("a", "b", "c"):
        c.docs[doc_id] = {"v": doc_id}
    refs = [c.document(i) for i in ("a", "b", "c")]
    got = [snap async for snap in db.get_all(refs)]
    assert [s.id for s in got] == ["c", "b", "a"]
    assert {s.id: s.to_dict() for s in got} == {"a": {"v": "a"}, "b": {"v": "b"}, "c": {"v": "c"}}


async def test_dispatching_decorator_runs_the_original_for_a_non_eval_transaction(db, col):
    calls = []

    def original(fn):
        async def run(transaction, *args, **kwargs):
            calls.append(transaction)
            return ("original", await fn(transaction, *args, **kwargs))
        return run

    class Plain:
        pass

    with eval_transactions():       # stands for whatever was installed before the dispatcher
        pass
    from unittest.mock import patch
    with patch("google.cloud.firestore.async_transactional", new=original), dispatching_transactions():
        @firestore.async_transactional
        async def body(tx, arg):
            return (type(tx).__name__, arg)

        plain = Plain()
        assert await body(plain, 7) == ("original", ("Plain", 7))
        assert calls == [plain]
        eval_tx = db.transaction()
        assert await body(eval_tx, 8) == ("EvalTransaction", 8)   # the fake's loop, not the original
        assert calls == [plain]


async def test_eval_transactions_installs_a_distinct_object():
    before = firestore.async_transactional
    with eval_transactions():
        first = firestore.async_transactional
    with eval_transactions():
        second = firestore.async_transactional
    assert first is not before and second is not before and first is not second
    assert firestore.async_transactional is before
