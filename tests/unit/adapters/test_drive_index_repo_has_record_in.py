"""`has_record_in(user_id, state, priority)`: is any record of the user in this state at this priority class.
One limit-1 equality query, served by Firestore's single-field indexes (USER_DRIVE_INDEXING_RFC §10.2 `building`)."""
from datetime import datetime, timezone

import pytest
from google.cloud.firestore_v1.field_path import FieldPath

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_index import DriveIndexRecord, Priority, RecordState
from tests.unit.adapters.firestore_drive_fakes import RECS, Db, executed, make_repo, rec_doc, snap
from tests.unit.services.drive_index_fakes import U_SCOPE

NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


async def test_in_memory_matches_state_and_priority_of_the_user_only():
    repo = InMemoryDriveIndexRepository()
    await repo.upsert_records([
        DriveIndexRecord(user_id="u", connection_id="u", item_id="a", state=RecordState.UNRESOLVED, due_at=NOW, priority=Priority.RETRY),
        DriveIndexRecord(user_id="u", connection_id="u", item_id="b", state=RecordState.PENDING, due_at=NOW, priority=Priority.CASCADE),
        DriveIndexRecord(user_id="x", connection_id="x", item_id="c", state=RecordState.UNRESOLVED, due_at=NOW,
                         priority=Priority.CASCADE),
        DriveIndexRecord(user_id="u", connection_id="u", item_id="d", state=RecordState.UNRESOLVED),   # parked: no priority
    ])
    assert not await repo.has_record_in(U_SCOPE, RecordState.UNRESOLVED, Priority.CASCADE)
    assert await repo.has_record_in(U_SCOPE, RecordState.UNRESOLVED, Priority.RETRY)
    assert await repo.has_record_in(ConnectionScope(user_id="x", connection_id="x"), RecordState.UNRESOLVED, Priority.CASCADE)


@pytest.fixture
def db():
    return Db()


@pytest.fixture
def repo(db, mock_env_config):
    return make_repo(db, mock_env_config)


async def test_firestore_is_one_limit_one_equality_query(repo, db):
    db.cols[RECS].respond = lambda ops: [snap(rec_doc("a"))]
    assert await repo.has_record_in(U_SCOPE, RecordState.UNRESOLVED, Priority.CASCADE)
    assert executed(db) == [[("where", "connection_id", "==", "u"), ("where", "state", "==", "unresolved"),
                             ("where", "priority", "==", 1), ("select", (FieldPath.document_id(),)),
                             ("limit", 1)]]


async def test_firestore_none_found(repo, db):
    assert not await repo.has_record_in(U_SCOPE, RecordState.UNRESOLVED, Priority.CASCADE)


async def test_equality_only_needs_no_composite_index(repo, db):
    """Equality filters with no ordering are served by the automatic single-field indexes."""
    await repo.has_record_in(U_SCOPE, RecordState.UNRESOLVED, Priority.CASCADE)
    ops = executed(db)[0]
    assert not [op for op in ops if op[0] == "order_by" or (op[0] == "where" and op[2] != "==")]
