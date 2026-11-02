"""The connection settings in the store (MULTI_DRIVE_RFC §11.3, §11.8; design §1.5): `_to_doc` / `_from_doc` round trip,
a document stored before 2b reads as the migrated connection (root write folder, searched, default scope), a stored null
write folder is read-only, `update_settings` writes the four and never the state, and `rename` is one transaction (ok:
name + one history entry; error: nothing; DISCONNECTED or absent: None). Run on `FirestoreDriveConnectionRepository` over
`EvalDb` and on the in-memory twin."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.adapters.firestore_drive_connection_repo import FirestoreDriveConnectionRepository
from src.domain.drive_connection import ROOT_FOLDER, DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_mount_name_change import MountNameChange
from src.domain.drive_mount_name_check import MountNameCheck
from src.domain.drive_mounts import NAME_TAKEN, validate_mount_name
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
CONNS, SETTINGS = "eval_drive_connections", "eval_drive_user_settings"
T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=1)
SETTING_KEYS = ("searched_by_default", "write_folder_id", "scope_folder_ids", "scope_whole_drive")


def conn(connection_id, name="Drive", minutes=0, **over):
    base = dict(connection_id=connection_id, user_id="usr-1", account_id="acc-1", credential_id=f"cred-{connection_id}",
                kind="fake_a", drive_identity=f"fake_a:{connection_id}", ref_tag=f"a{minutes}", mount_name=name,
                created_at=T0 + timedelta(minutes=minutes), name_history=(MountNameChange(name, T0),))
    base.update(over)
    return DriveConnection(**base)


def firestore_repo():
    db = EvalDb(indexes=INDEXES_FILE, groups={CONNS: "development_drive_connections",
                                              SETTINGS: "development_drive_user_settings"})
    return FirestoreDriveConnectionRepository(db, CONNS, SETTINGS), db


@pytest.fixture(params=["firestore", "fake"])
def repo(request):
    return firestore_repo()[0] if request.param == "firestore" else FakeDriveConnectionRepository()


def checker(name, connection_id):
    def check(every):
        return validate_mount_name(name, connection_id=connection_id, connections=every, primary_connection_id=None,
                                   primary_top_folders=(), now=T1)
    return check


class TestRoundTrip:
    async def test_every_setting_round_trips(self, repo):
        stored = conn("conn-2", "Acme", searched_by_default=False, write_folder_id="f-rd",
                      scope_folder_ids=("f-rd", "f-coimbra"))
        await repo.create(stored)
        assert await repo.get("usr-1", "conn-2") == stored

    async def test_read_only_and_whole_drive_round_trip(self, repo):
        stored = conn("conn-2", "Acme", write_folder_id=None, scope_whole_drive=True)
        await repo.create(stored)
        assert await repo.get("usr-1", "conn-2") == stored


class TestStoredDocuments:
    async def test_the_four_keys_are_written(self):
        repo, db = firestore_repo()
        await repo.create(conn("conn-2", "Acme", write_folder_id=None, scope_folder_ids=("f-rd",)))
        doc = db.collection(CONNS).docs["usr-1:conn-2"]
        assert {k: doc[k] for k in SETTING_KEYS} == {"searched_by_default": True, "write_folder_id": None,
                                                     "scope_folder_ids": ["f-rd"], "scope_whole_drive": False}

    async def test_a_document_stored_before_2b_is_the_migrated_connection(self):
        repo, db = firestore_repo()
        await repo.create(conn("conn-1", write_folder_id=None, searched_by_default=False, scope_whole_drive=True))
        docs = db.collection(CONNS).docs
        docs["usr-1:conn-1"] = {k: v for k, v in docs["usr-1:conn-1"].items() if k not in SETTING_KEYS}
        read = await repo.get("usr-1", "conn-1")
        assert read.write_folder_id == ROOT_FOLDER and read.searched_by_default is True
        assert read.scope_folder_ids == () and read.scope_whole_drive is False

    async def test_a_stored_null_write_folder_is_read_only(self):
        repo, db = firestore_repo()
        await repo.create(conn("conn-1"))
        db.collection(CONNS).docs["usr-1:conn-1"]["write_folder_id"] = None
        assert (await repo.get("usr-1", "conn-1")).write_folder_id is None


class TestUpdateSettings:
    async def test_writes_the_four_and_returns_the_stored_connection(self, repo):
        await repo.create(conn("conn-2", "Acme"))
        stored = await repo.update_settings("usr-1", "conn-2", searched_by_default=False, write_folder_id="f-rd",
                                            scope_folder_ids=["f-rd"], scope_whole_drive=False)
        assert stored == await repo.get("usr-1", "conn-2")
        assert (stored.searched_by_default, stored.write_folder_id, stored.scope_folder_ids) == (False, "f-rd",
                                                                                                ("f-rd",))

    async def test_never_touches_the_state(self, repo):
        await repo.create(conn("conn-2", "Acme"))
        await repo.transition("usr-1", "conn-2", ConnectionState.NEEDS_RECONNECT, T1)
        stored = await repo.update_settings("usr-1", "conn-2", searched_by_default=True, write_folder_id=None,
                                            scope_folder_ids=(), scope_whole_drive=True)
        assert stored.state is ConnectionState.NEEDS_RECONNECT and stored.write_folder_id is None

    async def test_absent_is_none(self, repo):
        assert await repo.update_settings("usr-1", "conn-9", searched_by_default=True, write_folder_id=None,
                                          scope_folder_ids=(), scope_whole_drive=False) is None

    async def test_folders_and_whole_together_write_nothing(self, repo):
        await repo.create(conn("conn-2", "Acme"))
        with pytest.raises(ValueError):
            await repo.update_settings("usr-1", "conn-2", searched_by_default=True, write_folder_id=None,
                                       scope_folder_ids=["f-rd"], scope_whole_drive=True)
        assert (await repo.get("usr-1", "conn-2")).write_folder_id == ROOT_FOLDER


class TestRename:
    async def test_ok_writes_the_name_and_one_history_entry(self, repo):
        await repo.create(conn("conn-2", "Acme"))
        result = await repo.rename("usr-1", "conn-2", T1, checker("Acme Hamburg", "conn-2"))
        assert result == MountNameCheck(name="Acme Hamburg")
        stored = await repo.get("usr-1", "conn-2")
        assert stored.mount_name == "Acme Hamburg"
        assert stored.name_history == (MountNameChange("Acme", T0), MountNameChange("Acme Hamburg", T1))

    async def test_an_error_writes_nothing(self, repo):
        await repo.create(conn("conn-1", "Drive"))
        await repo.create(conn("conn-2", "Acme", minutes=1))
        result = await repo.rename("usr-1", "conn-2", T1, checker("drive", "conn-2"))
        assert result.error == NAME_TAKEN.format(name="drive")
        assert (await repo.get("usr-1", "conn-2")) == conn("conn-2", "Acme", minutes=1)

    async def test_the_same_name_writes_nothing(self, repo):
        await repo.create(conn("conn-2", "Acme"))
        assert (await repo.rename("usr-1", "conn-2", T1, checker("  Acme ", "conn-2"))).ok
        assert (await repo.get("usr-1", "conn-2")).name_history == (MountNameChange("Acme", T0),)

    async def test_disconnected_or_absent_is_none(self, repo):
        await repo.create(conn("conn-2", "Acme", state=ConnectionState.DISCONNECTED, disconnected_at=T0))
        calls = []

        def check(every):
            calls.append(every)
            return MountNameCheck(name="Acme Hamburg")

        assert await repo.rename("usr-1", "conn-2", T1, check) is None
        assert await repo.rename("usr-1", "conn-9", T1, check) is None
        assert calls == [] and (await repo.get("usr-1", "conn-2")).mount_name == "Acme"

    async def test_the_check_sees_every_connection_of_the_user(self, repo):
        await repo.create(conn("conn-1", "Drive"))
        await repo.create(conn("conn-2", "Acme", minutes=1))
        await repo.create(conn("conn-3", "Drive", user_id="usr-2"))
        seen = []

        def check(every):
            seen.extend(c.connection_id for c in every)
            return MountNameCheck(name="Acme")

        await repo.rename("usr-1", "conn-2", T1, check)
        assert seen == ["conn-1", "conn-2"]
