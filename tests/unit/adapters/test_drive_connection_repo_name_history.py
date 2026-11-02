"""FirestoreDriveConnectionRepository stores and reads `name_history` (MULTI_DRIVE_RFC §11.8; slice 2a core), over the
evaluating Firestore fake: the round trip with tz-aware instants, the raw stored list of `{name, at}`, a document
stored before the field (the slice-1b shape) reading as no history, and the in-memory twin agreeing. Technical ids
only."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.adapters.firestore_drive_connection_repo import FirestoreDriveConnectionRepository
from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_mount_name_change import MountNameChange
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
CONNS, SETTINGS = "eval_drive_connections", "eval_drive_user_settings"
T0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 1, 9, 0, 0, 123456, tzinfo=timezone.utc)
HISTORY = (MountNameChange("Acme", T0), MountNameChange("Acme Hamburg", T1))


def conn(**over):
    base = dict(connection_id="conn-1", user_id="usr-1", account_id="acc-1", credential_id="cred-1", kind="fake_a",
                drive_identity="fake_a:conn-1", ref_tag="a1", mount_name="Acme Hamburg", created_at=T0,
                name_history=HISTORY)
    base.update(over)
    return DriveConnection(**base)


def firestore_repo():
    db = EvalDb(indexes=INDEXES_FILE, groups={CONNS: "development_drive_connections",
                                              SETTINGS: "development_drive_user_settings"})
    return FirestoreDriveConnectionRepository(db, CONNS, SETTINGS), db


@pytest.fixture(params=["firestore", "fake"])
def repo(request):
    return firestore_repo()[0] if request.param == "firestore" else FakeDriveConnectionRepository()


async def test_the_history_round_trips(repo):
    assert await repo.create(conn()) is True
    stored = await repo.get("usr-1", "conn-1")
    assert stored == conn()
    assert [c.at.tzinfo is not None for c in stored.name_history] == [True, True]
    assert (await repo.list_for_user("usr-1"))[0].name_history == HISTORY


async def test_an_empty_history_round_trips(repo):
    await repo.create(conn(mount_name="Drive", name_history=()))
    assert (await repo.get("usr-1", "conn-1")).name_history == ()


async def test_update_and_transition_keep_the_history(repo):
    await repo.create(conn())
    await repo.update("usr-1", "conn-1", indexing_enabled=True)
    await repo.transition("usr-1", "conn-1", ConnectionState.NEEDS_RECONNECT, T1 + timedelta(days=1))
    assert (await repo.get("usr-1", "conn-1")).name_history == HISTORY


async def test_the_raw_document_holds_a_list_of_name_and_instant():
    repo, db = firestore_repo()
    await repo.create(conn())
    data = (await db.collection(CONNS).document("usr-1:conn-1").get()).to_dict()
    assert data["name_history"] == [{"name": "Acme", "at": T0}, {"name": "Acme Hamburg", "at": T1}]


async def test_a_document_stored_before_the_field_reads_as_no_history():
    repo, db = firestore_repo()
    await repo.create(conn(mount_name="Drive", name_history=()))
    ref = db.collection(CONNS).document("usr-1:conn-1")
    data = {k: v for k, v in (await ref.get()).to_dict().items() if k != "name_history"}
    await ref.set(data)
    stored = await repo.get("usr-1", "conn-1")
    assert (stored.mount_name, stored.name_history) == ("Drive", ())


async def test_a_stored_instant_in_another_zone_reads_as_utc():
    repo, db = firestore_repo()
    await repo.create(conn())
    ref = db.collection(CONNS).document("usr-1:conn-1")
    plus_two = timezone(timedelta(hours=2))
    await ref.update({"name_history": [{"name": "Acme", "at": T0.astimezone(plus_two)},
                                       {"name": "Acme Hamburg", "at": T1.astimezone(plus_two)}]})
    stored = await repo.get("usr-1", "conn-1")
    assert stored.name_history == HISTORY
    assert all(c.at.tzinfo is timezone.utc for c in stored.name_history)


async def test_a_stored_entry_without_an_instant_is_refused():
    repo, db = firestore_repo()
    await repo.create(conn())
    await db.collection(CONNS).document("usr-1:conn-1").update(
        {"name_history": [{"name": "Acme Hamburg", "at": None}]})
    with pytest.raises(ValueError):
        await repo.get("usr-1", "conn-1")
