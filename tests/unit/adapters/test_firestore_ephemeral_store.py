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
