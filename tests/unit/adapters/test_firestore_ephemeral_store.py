import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.adapters.firestore_ephemeral_store import FirestoreEphemeralStore


def _doc_mock(exists: bool, data: dict | None = None):
    snap = MagicMock()
    snap.exists = exists
    snap.to_dict.return_value = data
    doc_ref = MagicMock()
    doc_ref.get = AsyncMock(return_value=snap)
    doc_ref.set = AsyncMock()
    doc_ref.delete = AsyncMock()
    return doc_ref


@pytest.mark.asyncio
async def test_get_returns_none_when_missing():
    col = MagicMock()
    col.document.return_value = _doc_mock(exists=False)
    db = MagicMock()
    db.collection.return_value = col

    store = FirestoreEphemeralStore(db_client=db, collection="voice_tickets")
    result = await store.get("ticket-1")

    assert result is None


@pytest.mark.asyncio
async def test_get_returns_none_when_expired():
    expired_at = time.time() - 5
    doc = _doc_mock(exists=True, data={"value": {"user_id": "u1"}, "expires_at": expired_at})
    col = MagicMock()
    col.document.return_value = doc
    db = MagicMock()
    db.collection.return_value = col

    store = FirestoreEphemeralStore(db_client=db, collection="voice_tickets")
    result = await store.get("ticket-1")

    assert result is None
    doc.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_returns_value_when_not_expired():
    doc = _doc_mock(exists=True, data={"value": {"user_id": "u1"}, "expires_at": time.time() + 60})
    col = MagicMock()
    col.document.return_value = doc
    db = MagicMock()
    db.collection.return_value = col

    store = FirestoreEphemeralStore(db_client=db, collection="voice_tickets")
    result = await store.get("ticket-1")

    assert result == {"user_id": "u1"}


@pytest.mark.asyncio
async def test_set_writes_value_and_expires_at():
    doc = _doc_mock(exists=False)
    col = MagicMock()
    col.document.return_value = doc
    db = MagicMock()
    db.collection.return_value = col

    store = FirestoreEphemeralStore(db_client=db, collection="voice_tickets")
    before = time.time()
    await store.set("ticket-1", {"user_id": "u1"}, ttl_s=30)

    written = doc.set.call_args.args[0]
    assert written["value"] == {"user_id": "u1"}
    assert before + 30 <= written["expires_at"] <= before + 31


# =============================================================================
# get_and_delete — atomic single-use consumption
#
# Mirrors the passthrough-patch technique already used for the other
# transactional adapter in this repo (tests/unit/adapters/
# test_firestore_user_repo_oauth.py::_passthrough_transactional, itself
# mirroring test_firestore_account_repo.py): patch firestore.async_transactional
# to a bare passthrough so the inner function runs directly against a plain
# MagicMock() transaction, with no need to stub the real SDK's transaction
# lifecycle.
# =============================================================================


def _passthrough_transactional(fn):
    return fn


def _txn_doc_mock(exists: bool, data: dict | None = None):
    """A doc ref whose .get() honours the `transaction=` kwarg, as
    AsyncDocumentReference.get does."""
    snap = MagicMock()
    snap.exists = exists
    snap.to_dict.return_value = data
    doc_ref = MagicMock()
    doc_ref.get = AsyncMock(return_value=snap)
    return doc_ref


def _txn_store(doc_ref):
    col = MagicMock()
    col.document.return_value = doc_ref
    db = MagicMock()
    db.collection.return_value = col
    db.transaction.return_value = MagicMock()
    return FirestoreEphemeralStore(db_client=db, collection="voice_tickets"), db


@pytest.mark.asyncio
async def test_get_and_delete_returns_value_and_deletes_inside_the_transaction(monkeypatch):
    doc_ref = _txn_doc_mock(exists=True, data={"value": {"user_id": "u1"}, "expires_at": time.time() + 60})
    store, db = _txn_store(doc_ref)
    monkeypatch.setattr(
        "src.adapters.firestore_ephemeral_store.firestore.async_transactional",
        _passthrough_transactional,
    )

    result = await store.get_and_delete("ticket-1")

    assert result == {"user_id": "u1"}
    transaction = db.transaction.return_value
    # The read must ENLIST in the transaction — a plain read outside it would
    # reintroduce exactly the TOCTOU window this method exists to close.
    doc_ref.get.assert_awaited_once_with(transaction=transaction)
    transaction.delete.assert_called_once_with(doc_ref)


@pytest.mark.asyncio
async def test_get_and_delete_returns_none_when_missing_and_deletes_nothing(monkeypatch):
    doc_ref = _txn_doc_mock(exists=False)
    store, db = _txn_store(doc_ref)
    monkeypatch.setattr(
        "src.adapters.firestore_ephemeral_store.firestore.async_transactional",
        _passthrough_transactional,
    )

    assert await store.get_and_delete("ticket-1") is None
    db.transaction.return_value.delete.assert_not_called()


@pytest.mark.asyncio
async def test_get_and_delete_returns_none_when_expired_but_still_consumes(monkeypatch):
    """An expired ticket is reported absent AND cleaned up — same housekeeping
    as get(), so a stale document does not linger past its TTL."""
    doc_ref = _txn_doc_mock(exists=True, data={"value": {"user_id": "u1"}, "expires_at": time.time() - 5})
    store, db = _txn_store(doc_ref)
    monkeypatch.setattr(
        "src.adapters.firestore_ephemeral_store.firestore.async_transactional",
        _passthrough_transactional,
    )

    assert await store.get_and_delete("ticket-1") is None
    db.transaction.return_value.delete.assert_called_once_with(doc_ref)


@pytest.mark.asyncio
async def test_two_concurrent_get_and_delete_calls_yield_exactly_one_winner(monkeypatch):
    """The regression this method exists for.

    Simulates Firestore's same-document contention semantics: the transaction
    reads, and the FIRST transaction to reach its write consumes the document,
    so any transaction that re-reads afterwards sees it gone. With the old
    get()-then-delete() pair both callers would have been served the config;
    here exactly one non-None result comes back.

    The two calls are driven concurrently via asyncio.gather, and the read is
    deliberately made to yield control (await asyncio.sleep(0)) so both
    coroutines are genuinely interleaved rather than running to completion one
    after the other.
    """
    state = {"present": True, "deleted_by": []}

    doc_ref = MagicMock()

    async def _get(transaction=None):
        await asyncio.sleep(0)  # force interleaving
        snap = MagicMock()
        snap.exists = state["present"]
        snap.to_dict.return_value = {"value": {"user_id": "u1"}, "expires_at": time.time() + 60}
        return snap

    doc_ref.get = AsyncMock(side_effect=_get)

    col = MagicMock()
    col.document.return_value = doc_ref
    db = MagicMock()
    db.collection.return_value = col

    def _new_transaction():
        transaction = MagicMock()

        def _delete(ref):
            # Firestore aborts+retries a transaction whose document was
            # written by another transaction after this one read it; the
            # retry re-reads and finds nothing. Modelled here as: the first
            # writer wins, later writers see an already-consumed document.
            if not state["present"]:
                raise AssertionError("second transaction wrote an already-consumed ticket")
            state["present"] = False
            state["deleted_by"].append(ref)

        transaction.delete.side_effect = _delete
        return transaction

    db.transaction.side_effect = _new_transaction

    store = FirestoreEphemeralStore(db_client=db, collection="voice_tickets")
    monkeypatch.setattr(
        "src.adapters.firestore_ephemeral_store.firestore.async_transactional",
        _passthrough_transactional,
    )

    results = await asyncio.gather(
        store.get_and_delete("ticket-1"),
        store.get_and_delete("ticket-1"),
    )

    non_none = [r for r in results if r is not None]
    assert len(non_none) == 1, f"expected exactly one winner, got {results}"
    assert non_none[0] == {"user_id": "u1"}
    assert len(state["deleted_by"]) == 1
