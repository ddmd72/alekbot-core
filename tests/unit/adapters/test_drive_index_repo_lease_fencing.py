"""Sync-state writes are fenced by the lease (USER_DRIVE_INDEXING_RFC §7.1): `save_sync_state` and `delete_sync_state`
apply only while `owner` holds the lease (owner None: only while nobody does), atomically, else raise LeaseLost. A
tick that outlived its lease can neither purge the new holder's state nor write its own over it."""
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from src.adapters.firestore_drive_index_repo import STATE_FIELDS, state_to_doc
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import DriveSyncState, LeaseLost

from tests.unit.adapters.firestore_drive_fakes import NOW, STATES, Db, make_repo, snap
from tests.unit.services.drive_index_fakes import U_SCOPE
from src.domain.connection_scope import ConnectionScope

SPAN = timedelta(minutes=31)
LATER = NOW + SPAN + timedelta(minutes=1)


class TestInMemory:
    async def test_a_stale_purge_cannot_delete_the_new_holders_state(self):
        repo = InMemoryDriveIndexRepository()
        await repo.acquire_lease(U_SCOPE, "A", NOW, NOW + SPAN, create=True)
        assert await repo.acquire_lease(U_SCOPE, "B", LATER, LATER + SPAN, create=True) is not None
        with pytest.raises(LeaseLost):
            await repo.delete_sync_state(U_SCOPE, "A")
        assert (await repo.get_sync_state(U_SCOPE)).lease_owner == "B"
        assert await repo.acquire_lease(U_SCOPE, "C", LATER, LATER + SPAN, create=True) is None

    async def test_a_stale_save_is_refused_and_the_holders_save_applies(self):
        repo = InMemoryDriveIndexRepository()
        a = await repo.acquire_lease(U_SCOPE, "A", NOW, NOW + SPAN, create=True)
        b = await repo.acquire_lease(U_SCOPE, "B", LATER, LATER + SPAN, create=True)
        with pytest.raises(LeaseLost):
            await repo.save_sync_state(replace(a, cursor="A-progress"), "A")
        await repo.save_sync_state(replace(b, cursor="B-progress"), "B")
        st = await repo.get_sync_state(U_SCOPE)
        assert (st.cursor, st.lease_owner) == ("B-progress", "B")

    async def test_a_save_after_the_state_was_purged_does_not_recreate_it(self):
        repo = InMemoryDriveIndexRepository()
        a = await repo.acquire_lease(U_SCOPE, "A", NOW, NOW + SPAN, create=True)
        await repo.delete_sync_state(U_SCOPE, "A")
        with pytest.raises(LeaseLost):
            await repo.save_sync_state(a, "A")
        assert await repo.get_sync_state(U_SCOPE) is None

    async def test_without_an_owner_a_write_applies_only_while_nobody_holds_the_lease(self):
        repo = InMemoryDriveIndexRepository()
        await repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", epoch=1))
        await repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", epoch=2))
        assert (await repo.get_sync_state(U_SCOPE)).epoch == 2
        await repo.acquire_lease(U_SCOPE, "A", NOW, NOW + SPAN, create=True)
        with pytest.raises(LeaseLost):
            await repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", epoch=3))
        with pytest.raises(LeaseLost):
            await repo.delete_sync_state(U_SCOPE)
        await repo.delete_sync_state(ConnectionScope(user_id="u", connection_id="x"))   # nothing stored: nothing to fence


@pytest.fixture
def passthrough_transactions():
    with patch("src.adapters.firestore_drive_index_repo.firestore.async_transactional", side_effect=lambda fn: fn):
        yield


def _stored(db, owner):
    doc = db.cols[STATES].document("u")
    doc.get = AsyncMock(return_value=snap(state_to_doc(DriveSyncState(user_id="u", connection_id="u", lease_owner=owner,
                                                                      lease_until=LATER + SPAN)), "u"))
    return doc


class TestFirestore:
    async def test_save_is_a_transactional_merge_by_the_holder(self, mock_env_config, passthrough_transactions):
        db = Db()
        repo = make_repo(db, mock_env_config)
        doc = _stored(db, "B")
        await repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", epoch=3, lease_owner="B"), "B")
        assert doc.get.call_args.kwargs["transaction"] is db.tx
        ref, data = db.tx.set.call_args.args
        assert ref is doc and db.tx.set.call_args.kwargs["merge"] == list(STATE_FIELDS)
        assert set(data) == set(STATE_FIELDS) and data["epoch"] == 3
        doc.set.assert_not_awaited()

    async def test_a_stale_save_writes_nothing(self, mock_env_config, passthrough_transactions):
        db = Db()
        repo = make_repo(db, mock_env_config)
        _stored(db, "B")
        with pytest.raises(LeaseLost):
            await repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", epoch=3), "A")
        db.tx.set.assert_not_called()

    async def test_a_save_onto_a_missing_state_needs_no_owner(self, mock_env_config, passthrough_transactions):
        db = Db()
        repo = make_repo(db, mock_env_config)
        with pytest.raises(LeaseLost):
            await repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u"), "A")
        db.tx.set.assert_not_called()
        await repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u"))
        db.tx.set.assert_called_once()

    async def test_delete_by_the_holder_only(self, mock_env_config, passthrough_transactions):
        db = Db()
        repo = make_repo(db, mock_env_config)
        doc = _stored(db, "B")
        with pytest.raises(LeaseLost):
            await repo.delete_sync_state(U_SCOPE, "A")
        db.tx.delete.assert_not_called()
        await repo.delete_sync_state(U_SCOPE, "B")
        db.tx.delete.assert_called_once_with(doc)
        doc.delete.assert_not_awaited()

    async def test_deleting_a_missing_state_is_a_no_op(self, mock_env_config, passthrough_transactions):
        db = Db()
        repo = make_repo(db, mock_env_config)
        await repo.delete_sync_state(U_SCOPE, "A")
        db.tx.delete.assert_not_called()
