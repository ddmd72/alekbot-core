"""FirestoreDriveConnectionRepository over the evaluating Firestore fake (`EvalDb`, index refusal on), and the port's
rules run on both it and the in-memory twin (`tests/support/drive_connection_repo.py`) so the two cannot drift
(MULTI_DRIVE_RFC §4.4): create never overwrites, update is partial and never moves the state, transition follows
`can_transition`, the orders the port promises, settings read back whole, and structural tenancy. Firestore-only cases
assert the raw stored documents and that no query needs a composite index."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.adapters.firestore_drive_connection_repo import FirestoreDriveConnectionRepository
from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_user_settings import DriveUserSettings
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
CONNS, SETTINGS = "eval_drive_connections", "eval_drive_user_settings"
T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
ACTIVE, NEEDS, GONE = ConnectionState.ACTIVE, ConnectionState.NEEDS_RECONNECT, ConnectionState.DISCONNECTED


def conn(user_id, connection_id, minutes=0, **over):
    base = dict(connection_id=connection_id, user_id=user_id, account_id="acc-1", credential_id=f"cred-{connection_id}",
                kind="fake_a", drive_identity=f"fake_a:{connection_id}", ref_tag="a1",
                created_at=T0 + timedelta(minutes=minutes))
    base.update(over)
    return DriveConnection(**base)


def firestore_repo():
    db = EvalDb(indexes=INDEXES_FILE, groups={CONNS: "development_drive_connections",
                                              SETTINGS: "development_drive_user_settings"})
    return FirestoreDriveConnectionRepository(db, CONNS, SETTINGS), db


@pytest.fixture(params=["firestore", "fake"])
def repo(request):
    return firestore_repo()[0] if request.param == "firestore" else FakeDriveConnectionRepository()


# ---------------------------------------------------------------------------------------------- the port's rules

class TestPortRules:
    async def test_a_connection_round_trips_with_every_field(self, repo):
        stored = conn("usr-1", "conn-1", ref_tag="b2", mount_name="Drive 2", indexing_enabled=True,
                      transcribe_enabled=True, state=GONE, disconnected_at=T0 + timedelta(hours=1))
        assert await repo.create(stored) is True
        assert await repo.get("usr-1", "conn-1") == stored

    async def test_create_never_overwrites(self, repo):
        assert await repo.create(conn("usr-1", "conn-1")) is True
        assert await repo.create(conn("usr-1", "conn-1", mount_name="Other")) is False
        assert (await repo.get("usr-1", "conn-1")).mount_name == "Drive"

    async def test_a_read_is_addressed_by_its_user(self, repo):
        await repo.create(conn("usr-1", "conn-1"))
        assert await repo.get("usr-2", "conn-1") is None
        assert await repo.list_for_user("usr-2") == []

    async def test_list_for_user_is_oldest_first_and_holds_every_state(self, repo):
        await repo.create(conn("usr-1", "conn-2", minutes=5))
        await repo.create(conn("usr-1", "conn-1", minutes=1, state=GONE, disconnected_at=T0))
        await repo.create(conn("usr-1", "conn-0", minutes=1))
        await repo.create(conn("usr-2", "conn-3"))
        assert [c.connection_id for c in await repo.list_for_user("usr-1")] == ["conn-0", "conn-1", "conn-2"]

    async def test_list_by_state_across_users_with_the_indexing_filter(self, repo):
        await repo.create(conn("usr-2", "conn-a", indexing_enabled=True))
        await repo.create(conn("usr-1", "conn-b", minutes=3, indexing_enabled=True))
        await repo.create(conn("usr-1", "conn-c", minutes=1))
        await repo.create(conn("usr-1", "conn-d", minutes=2, state=NEEDS, indexing_enabled=True))
        await repo.create(conn("usr-1", "conn-e", state=GONE, disconnected_at=T0, indexing_enabled=True))
        assert [c.connection_id for c in await repo.list_by_state([ACTIVE])] == ["conn-c", "conn-b", "conn-a"]
        assert [c.connection_id for c in await repo.list_by_state([ACTIVE], indexing_enabled=True)] == [
            "conn-b", "conn-a"]
        assert [c.connection_id for c in await repo.list_by_state([ACTIVE], indexing_enabled=False)] == ["conn-c"]
        assert [c.connection_id for c in await repo.list_by_state([ACTIVE, NEEDS])] == [
            "conn-c", "conn-d", "conn-b", "conn-a"]
        assert [c.connection_id for c in await repo.list_by_state([GONE])] == ["conn-e"]
        assert await repo.list_by_state([]) == []

    async def test_update_sets_only_the_fields_given_and_returns_the_stored_connection(self, repo):
        await repo.create(conn("usr-1", "conn-1", state=NEEDS))
        updated = await repo.update("usr-1", "conn-1", indexing_enabled=True, credential_id="cred-9")
        assert updated == await repo.get("usr-1", "conn-1")
        assert (updated.indexing_enabled, updated.credential_id, updated.transcribe_enabled) == (True, "cred-9", False)
        assert (updated.mount_name, updated.drive_identity, updated.state) == ("Drive", "fake_a:conn-1", NEEDS)
        again = await repo.update("usr-1", "conn-1", indexing_enabled=False, transcribe_enabled=True,
                                  drive_identity="fake_a:other", mount_name="Work")
        assert (again.indexing_enabled, again.transcribe_enabled, again.drive_identity, again.mount_name) == (
            False, True, "fake_a:other", "Work")

    async def test_update_of_an_absent_connection_writes_nothing(self, repo):
        assert await repo.update("usr-1", "conn-x", indexing_enabled=True) is None
        assert await repo.get("usr-1", "conn-x") is None

    async def test_transition_follows_the_rule(self, repo):
        await repo.create(conn("usr-1", "conn-1"))
        assert await repo.transition("usr-1", "conn-1", ACTIVE, T0) is False
        assert await repo.transition("usr-1", "conn-1", NEEDS, T0) is True
        assert await repo.transition("usr-1", "conn-1", NEEDS, T0) is False
        assert await repo.transition("usr-1", "conn-1", ACTIVE, T0) is True
        assert (await repo.get("usr-1", "conn-1")).disconnected_at is None
        later = T0 + timedelta(hours=2)
        assert await repo.transition("usr-1", "conn-1", GONE, later) is True
        gone = await repo.get("usr-1", "conn-1")
        assert (gone.state, gone.disconnected_at) == (GONE, later)
        for to in (ACTIVE, NEEDS, GONE):
            assert await repo.transition("usr-1", "conn-1", to, T0) is False
        assert await repo.transition("usr-1", "conn-x", NEEDS, T0) is False
        assert await repo.transition("usr-2", "conn-1", NEEDS, T0) is False

    async def test_update_never_touches_the_state(self, repo):
        await repo.create(conn("usr-1", "conn-1"))
        await repo.transition("usr-1", "conn-1", GONE, T0)
        updated = await repo.update("usr-1", "conn-1", mount_name="Old")
        assert (updated.state, updated.disconnected_at) == (GONE, T0)

    async def test_settings_read_back_whole(self, repo):
        assert await repo.get_settings("usr-1") is None
        await repo.save_settings(DriveUserSettings(user_id="usr-1", primary_connection_id="conn-1"))
        assert await repo.get_settings("usr-1") == DriveUserSettings(user_id="usr-1", primary_connection_id="conn-1")
        await repo.save_settings(DriveUserSettings(user_id="usr-1"))
        assert await repo.get_settings("usr-1") == DriveUserSettings(user_id="usr-1", primary_connection_id=None)
        assert await repo.get_settings("usr-2") is None


# ---------------------------------------------------------------------------------------------- Firestore only

class TestFirestoreDocuments:
    async def test_the_stored_document_and_its_id(self):
        repo, db = firestore_repo()
        await repo.create(conn("usr-1", "conn-1", ref_tag="a1"))
        doc = db.collection(CONNS).docs["usr-1:conn-1"]
        assert {k: doc[k] for k in ("connection_id", "user_id", "account_id", "credential_id", "kind",
                                    "drive_identity", "ref_tag", "mount_name", "indexing_enabled",
                                    "transcribe_enabled", "state", "created_at", "disconnected_at")} == {
            "connection_id": "conn-1", "user_id": "usr-1", "account_id": "acc-1", "credential_id": "cred-conn-1",
            "kind": "fake_a", "drive_identity": "fake_a:conn-1", "ref_tag": "a1", "mount_name": "Drive",
            "indexing_enabled": False, "transcribe_enabled": False, "state": "active", "created_at": T0,
            "disconnected_at": None}
        assert doc["updated_at"] is not None

    async def test_the_settings_document_and_its_id(self):
        repo, db = firestore_repo()
        await repo.save_settings(DriveUserSettings(user_id="usr-1", primary_connection_id="conn-1"))
        doc = db.collection(SETTINGS).docs["usr-1"]
        assert (doc["user_id"], doc["primary_connection_id"]) == ("usr-1", "conn-1")

    async def test_a_transition_writes_the_state_and_the_disconnect_time(self):
        repo, db = firestore_repo()
        await repo.create(conn("usr-1", "conn-1"))
        await repo.transition("usr-1", "conn-1", GONE, T0)
        doc = db.collection(CONNS).docs["usr-1:conn-1"]
        assert (doc["state"], doc["disconnected_at"]) == ("disconnected", T0)

    @pytest.mark.parametrize("field, value", [("user_id", "usr-2"), ("connection_id", "conn-9")])
    async def test_a_document_that_contradicts_its_address_is_absent(self, field, value, caplog):
        repo, db = firestore_repo()
        await repo.create(conn("usr-1", "conn-1", indexing_enabled=True))
        db.collection(CONNS).docs["usr-1:conn-1"][field] = value
        assert await repo.get("usr-1", "conn-1") is None
        assert await repo.list_for_user("usr-1") == []
        assert await repo.list_by_state([ACTIVE]) == []
        assert await repo.update("usr-1", "conn-1", mount_name="X") is None
        assert await repo.transition("usr-1", "conn-1", NEEDS, T0) is False
        assert "does not match its address" in caplog.text

    async def test_settings_stored_under_another_user_are_absent(self, caplog):
        repo, db = firestore_repo()
        await repo.save_settings(DriveUserSettings(user_id="usr-1", primary_connection_id="conn-1"))
        db.collection(SETTINGS).docs["usr-1"]["user_id"] = "usr-2"
        assert await repo.get_settings("usr-1") is None
        assert "another user" in caplog.text

    @pytest.mark.parametrize("field", ["credential_id", "drive_identity", "mount_name"])
    async def test_an_identity_field_is_never_emptied(self, field):
        repo, _ = firestore_repo()
        await repo.create(conn("usr-1", "conn-1"))
        with pytest.raises(ValueError):
            await repo.update("usr-1", "conn-1", **{field: ""})
        assert (await repo.get("usr-1", "conn-1")) == conn("usr-1", "conn-1")

    async def test_a_concurrent_state_change_is_not_lost_by_a_settings_write(self):
        repo, db = firestore_repo()
        await repo.create(conn("usr-1", "conn-1"))
        db.interfere_on_txn_read(CONNS, "usr-1:conn-1", {"state": "needs_reconnect"}, after_read=True)
        await repo.update("usr-1", "conn-1", indexing_enabled=True)
        stored = await repo.get("usr-1", "conn-1")
        assert (stored.state, stored.indexing_enabled) == (NEEDS, True)
