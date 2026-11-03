"""The connector access in the store (DRIVE_MCP_CONNECTOR_RFC §7.1; slice 3 DELTA 5): every form round trips,
`update_connector_access` writes only it, a settings write keeps it, and the Firestore forms (a `mode` map or null; a
document stored before the field and a damaged value both read as nothing, the latter logged without ids). Run on
`FirestoreDriveConnectionRepository` over `EvalDb` and on the in-memory twin."""
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.adapters.firestore_drive_connection_repo import FirestoreDriveConnectionRepository
from src.domain.drive_connection import DriveConnection
from src.domain.drive_connector_access import ConnectorAccess
from src.domain.drive_mount_name_change import MountNameChange
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
CONNS, SETTINGS = "eval_drive_connections", "eval_drive_user_settings"
T0 = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
CONNECTION_ID = "conn-alexrd01"


def conn(**over):
    base = dict(connection_id=CONNECTION_ID, user_id="usr-alex", account_id="acc-alex", credential_id="cred-1",
                kind="fake_a", drive_identity="fake_a:alex", ref_tag="a1", mount_name="Drive", created_at=T0,
                name_history=(MountNameChange("Drive", T0),), write_folder_id="f-rd", scope_folder_ids=("f-rd",))
    base.update(over)
    return DriveConnection(**base)


def firestore_repo():
    db = EvalDb(indexes=INDEXES_FILE, groups={CONNS: "development_drive_connections",
                                              SETTINGS: "development_drive_user_settings"})
    return FirestoreDriveConnectionRepository(db, CONNS, SETTINGS), db


@pytest.fixture(params=["firestore", "fake"])
def repo(request):
    return firestore_repo()[0] if request.param == "firestore" else FakeDriveConnectionRepository()


async def test_a_new_connection_has_no_connector_access(repo):
    await repo.create(conn())
    assert (await repo.get("usr-alex", CONNECTION_ID)).connector_access is None


@pytest.mark.parametrize("access", [None, ConnectorAccess.whole_drive(), ConnectorAccess.folders(("f-rd",))])
async def test_each_form_round_trips(repo, access):
    stored = conn(connector_access=access)
    await repo.create(stored)
    assert await repo.get("usr-alex", CONNECTION_ID) == stored


async def test_update_connector_access_writes_only_it_and_returns_the_stored_connection(repo):
    before = conn(searched_by_default=False)
    await repo.create(before)
    for access in (ConnectorAccess.folders(("f-rd", "f-oak")), ConnectorAccess.whole_drive(), None):
        returned = await repo.update_connector_access("usr-alex", CONNECTION_ID, access)
        stored = await repo.get("usr-alex", CONNECTION_ID)
        assert returned == stored == replace(before, connector_access=access)


async def test_update_of_an_absent_connection_is_none(repo):
    assert await repo.update_connector_access("usr-alex", "conn-missing", ConnectorAccess.whole_drive()) is None
    assert await repo.get("usr-alex", "conn-missing") is None


async def test_a_settings_write_keeps_the_connector_access(repo):
    await repo.create(conn(connector_access=ConnectorAccess.folders(("f-rd",))))
    stored = await repo.update_settings("usr-alex", CONNECTION_ID, searched_by_default=True, write_folder_id=None,
                                        scope_folder_ids=(), scope_whole_drive=True)
    assert stored.connector_access == ConnectorAccess.folders(("f-rd",))
    assert (await repo.get("usr-alex", CONNECTION_ID)).connector_access == ConnectorAccess.folders(("f-rd",))


class TestFirestoreForms:
    async def test_the_stored_forms(self):
        repo, db = firestore_repo()
        await repo.create(conn())
        doc = lambda: db.collection(CONNS).docs[f"usr-alex:{CONNECTION_ID}"]       # noqa: E731
        assert doc()["connector_access"] is None
        await repo.update_connector_access("usr-alex", CONNECTION_ID, ConnectorAccess.whole_drive())
        assert doc()["connector_access"] == {"mode": "whole"}
        await repo.update_connector_access("usr-alex", CONNECTION_ID, ConnectorAccess.folders(("f-rd", "f-oak")))
        assert doc()["connector_access"] == {"mode": "folders", "folder_ids": ["f-rd", "f-oak"]}
        await repo.update_connector_access("usr-alex", CONNECTION_ID, None)
        assert doc()["connector_access"] is None

    async def test_a_document_stored_before_the_field_reads_nothing(self):
        repo, db = firestore_repo()
        await repo.create(conn(connector_access=ConnectorAccess.whole_drive()))
        docs = db.collection(CONNS).docs
        key = f"usr-alex:{CONNECTION_ID}"
        docs[key] = {k: v for k, v in docs[key].items() if k != "connector_access"}
        assert (await repo.get("usr-alex", CONNECTION_ID)).connector_access is None

    @pytest.mark.parametrize("damaged", [
        "whole", {"mode": "everything"}, {"mode": "folders", "folder_ids": []},
        {"mode": "folders", "folder_ids": ["f-rd", ""]}, {"mode": "folders", "folder_ids": "f-rd"},
        {"mode": "whole", "folder_ids": ["f-rd"]}, ["f-rd"],
    ])
    async def test_a_malformed_value_reads_nothing_and_logs_an_error_without_ids(self, damaged, caplog):
        repo, db = firestore_repo()
        await repo.create(conn())
        db.collection(CONNS).docs[f"usr-alex:{CONNECTION_ID}"]["connector_access"] = damaged
        with caplog.at_level(logging.ERROR):
            read = await repo.get("usr-alex", CONNECTION_ID)
        assert read is not None and read.connector_access is None
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("connector_access" in m for m in errors)
        assert not any("f-rd" in m or "usr-alex" in m or CONNECTION_ID in m for m in errors)
