"""`save_sync_state` never writes the lease.

A tick that outlived its lease (USER_DRIVE_INDEXING_RFC §7.1) still holds a state naming itself as the lease owner.
If its final `save_sync_state` writes that lease back, its `finally: release_lease` clears the lease of the tick that
took over, and a third tick runs alongside it. Only `acquire_lease` and `release_lease` write the lease fields, in
both implementations of the port, as only `update_sync_flags` writes the fan-out flags."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore
from google.cloud.firestore_v1.async_batch import AsyncWriteBatch
from google.cloud.firestore_v1.async_document import AsyncDocumentReference
from google.cloud.firestore_v1.async_transaction import AsyncTransaction

from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository, state_to_doc
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import DriveSyncState, LeaseLost

from tests.unit.adapters.firestore_drive_fakes import NOW, STATES, Db, make_repo, snap
from tests.unit.services.drive_index_fakes import U_SCOPE

LEASE = ("lease_owner", "lease_until")
SPAN = timedelta(minutes=31)


# -- in-memory ---------------------------------------------------------------------------------------

class TestInMemoryLeaseOwnership:
    async def test_save_keeps_the_stored_lease(self):
        repo = InMemoryDriveIndexRepository()
        await repo.acquire_lease(U_SCOPE, "b", NOW, NOW + SPAN, create=True)
        await repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", epoch=3, lease_owner="a", lease_until=NOW), "b")
        st = await repo.get_sync_state(U_SCOPE)
        assert st.epoch == 3
        assert (st.lease_owner, st.lease_until) == ("b", NOW + SPAN)

    async def test_save_without_a_stored_state_writes_no_lease(self):
        repo = InMemoryDriveIndexRepository()
        await repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", lease_owner="a", lease_until=NOW + SPAN))
        st = await repo.get_sync_state(U_SCOPE)
        assert (st.lease_owner, st.lease_until) == (None, None)

    async def test_zombie_tick_cannot_release_the_new_holders_lease(self):
        repo = InMemoryDriveIndexRepository()
        zombie = await repo.acquire_lease(U_SCOPE, "a", NOW, NOW + SPAN, create=True)
        later = NOW + SPAN + timedelta(minutes=1)
        assert await repo.acquire_lease(U_SCOPE, "b", later, later + SPAN, create=True) is not None
        with pytest.raises(LeaseLost):                # the stalled tick resumes; its write is refused (§7.1)
            await repo.save_sync_state(replace(zombie, epoch=9), "a")
        await repo.release_lease(U_SCOPE, "a")            # ... then its finally runs
        assert (await repo.get_sync_state(U_SCOPE)).lease_owner == "b"
        assert (await repo.get_sync_state(U_SCOPE)).epoch == 0
        assert await repo.acquire_lease(U_SCOPE, "c", later, later + SPAN, create=True) is None


# -- Firestore through the recording fakes ---------------------------------------------------------

@pytest.fixture
def passthrough_transactions():
    with patch("src.adapters.firestore_drive_index_repo.firestore.async_transactional", side_effect=lambda fn: fn):
        yield


class TestFirestoreLeaseOwnership:
    async def test_save_neither_merges_nor_sends_the_lease(self, mock_env_config, passthrough_transactions):
        db = Db()
        repo = make_repo(db, mock_env_config)
        doc = db.cols[STATES].document("u")
        doc.get = AsyncMock(return_value=snap(state_to_doc(DriveSyncState(user_id="u", connection_id="u", lease_owner="a")), "u"))
        await repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", epoch=3, lease_owner="a", lease_until=NOW), "a")
        ref, data = db.tx.set.call_args.args
        merge = db.tx.set.call_args.kwargs["merge"]
        assert ref is doc
        assert not set(LEASE) & set(merge)
        assert not set(LEASE) & set(data)
        assert data["epoch"] == 3

    async def test_create_path_still_writes_the_lease(self, mock_env_config, passthrough_transactions):
        db = Db()
        repo = make_repo(db, mock_env_config)
        await repo.acquire_lease(U_SCOPE, "a", NOW, NOW + SPAN, create=True)
        _, data = db.tx.set.call_args.args
        assert (data["lease_owner"], data["lease_until"]) == ("a", NOW + SPAN)

    async def test_zombie_tick_cannot_release_the_new_holders_lease(self, mock_env_config, passthrough_transactions):
        """The stored document follows the writes: a merge set and the transaction's updates are applied to it."""
        db = Db()
        repo = make_repo(db, mock_env_config)
        later = NOW + SPAN + timedelta(minutes=1)
        stored = {**state_to_doc(DriveSyncState(user_id="u", connection_id="u", lease_owner="b", lease_until=later + SPAN))}
        doc = db.cols[STATES].document("u")
        doc.get = AsyncMock(side_effect=lambda **kw: snap(stored, "u"))

        async def merge_set(data, merge):
            stored.update({k: v for k, v in data.items() if k in merge})

        doc.set = AsyncMock(side_effect=merge_set)
        db.tx.set.side_effect = lambda ref, data, merge: stored.update({k: v for k, v in data.items() if k in merge})
        db.tx.update.side_effect = lambda ref, changes: stored.update(changes)

        zombie = DriveSyncState(user_id="u", connection_id="u", epoch=2, cursor="c2", lease_owner="a", lease_until=NOW + SPAN)
        with pytest.raises(LeaseLost):                # refused in the transaction: "b" holds the lease (§7.1)
            await repo.save_sync_state(zombie, "a")
        await repo.release_lease(U_SCOPE, "a")
        assert (stored["lease_owner"], stored["lease_until"]) == ("b", later + SPAN)
        assert stored["epoch"] == 0 and stored["cursor"] is None
        assert await repo.acquire_lease(U_SCOPE, "c", later, later + SPAN, create=True) is None


# -- Firestore through the SDK's own encoding ------------------------------------------------------

@pytest.fixture
def sdk(monkeypatch, mock_env_config):
    """A real AsyncClient that never reaches the network: batch and transaction commits capture the Write protos
    they would send; a transaction's begin and its document read are stubbed (the stored holder is "a")."""
    sent = []

    async def capture(self, *args, **kwargs):
        sent.extend(self._write_pbs)
        return [object() for _ in self._write_pbs]

    async def begin(self, retry_id=None):
        self._id = b"tx"

    async def commit(self):
        sent.extend(self._write_pbs)
        self._clean_up()
        return []

    async def get(self, field_paths=None, transaction=None, **kwargs):
        assert transaction is not None, "a sync-state read outside the fencing transaction"
        doc = state_to_doc(DriveSyncState(user_id=self.id, lease_owner="a"))
        return SimpleNamespace(exists=True, to_dict=lambda: dict(doc))

    monkeypatch.setattr(AsyncWriteBatch, "commit", capture)
    monkeypatch.setattr(AsyncTransaction, "_begin", begin)
    monkeypatch.setattr(AsyncTransaction, "_commit", commit)
    monkeypatch.setattr(AsyncDocumentReference, "get", get)
    client = firestore.AsyncClient(project="p", credentials=AnonymousCredentials(), database="d")
    mock_env_config.drive_index_records_collection = "test_drive_index_records"
    mock_env_config.drive_sync_state_collection = "test_drive_sync_state"
    return FirestoreDriveIndexRepository(client, mock_env_config), sent


class TestWriteEncoding:
    async def test_save_sync_state_never_writes_the_lease(self, sdk):
        repo, sent = sdk
        until = datetime(2026, 10, 11, 13, tzinfo=timezone.utc)
        await repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", epoch=3, lease_owner="a", lease_until=until), "a")
        (write,) = sent
        assert write.update.name.endswith("/documents/test_drive_sync_state/u")
        mask = set(write.update_mask.field_paths)
        assert not set(LEASE) & mask                      # not masked: the stored lease stays as it is
        assert not set(LEASE) & set(write.update.fields)
        assert "epoch" in mask and write.update.fields["epoch"].integer_value == 3
