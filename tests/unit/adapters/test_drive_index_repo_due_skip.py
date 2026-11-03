"""`due_records(..., skip_classes=…)` (MULTI_DRIVE_RFC E87): a skipped priority class is left out and the order of the
rest is unchanged. Firestore: the skipped class is not queried; the other classes keep their one equality query each
(the existing `priority, due_at` index), so no new composite index is needed."""
from datetime import timedelta

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import DriveIndexRecord, Priority
from tests.unit.adapters.firestore_drive_fakes import NOW, PROJECTION, RECS, Db, executed, make_repo, rec_doc, snap
from tests.unit.services.drive_index_fakes import U_SCOPE


def _rec(item_id: str, priority: Priority, minutes: int = 0) -> DriveIndexRecord:
    return DriveIndexRecord(user_id="u", connection_id="u", item_id=item_id, due_at=NOW - timedelta(minutes=minutes),
                            priority=int(priority))


async def test_in_memory_leaves_out_the_skipped_class_and_keeps_the_order():
    repo = InMemoryDriveIndexRepository()
    await repo.upsert_records([_rec("a", Priority.CASCADE, 9), _rec("b", Priority.FIRST_SEEN, 1),
                               _rec("c", Priority.EDIT, 2), _rec("d", Priority.FIRST_SEEN, 5),
                               _rec("e", Priority.MAINTENANCE, 7)])
    assert [r.item_id for r in await repo.due_records(U_SCOPE, NOW, 10)] == ["c", "a", "d", "b", "e"]
    out = await repo.due_records(U_SCOPE, NOW, 10, skip_classes=frozenset({int(Priority.CASCADE)}))
    assert [r.item_id for r in out] == ["c", "d", "b", "e"]
    assert [r.item_id for r in await repo.due_records(U_SCOPE, NOW, 2, skip_classes=frozenset({1}))] == ["c", "d"]


@pytest.fixture
def db():
    return Db()


@pytest.fixture
def repo(db, mock_env_config):
    return make_repo(db, mock_env_config)


async def test_firestore_does_not_query_a_skipped_class(repo, db):
    by_class = {int(Priority.EDIT): [rec_doc("c", due_at=NOW, priority=0)],
                int(Priority.CASCADE): [rec_doc("a", due_at=NOW, priority=1)],
                int(Priority.FIRST_SEEN): [rec_doc("d", due_at=NOW, priority=2)]}
    db.cols[RECS].respond = lambda ops: [snap(d) for d in by_class.get(ops[1][3], [])]
    out = await repo.due_records(U_SCOPE, NOW, 10, skip_classes=frozenset({int(Priority.CASCADE)}))
    assert [r.item_id for r in out] == ["c", "d"]
    queries = executed(db)
    assert [q[1][3] for q in queries] == [0, 2, 3, 4]
    assert queries[1] == [("where", "connection_id", "==", "u"), ("where", "priority", "==", 2),
                          ("where", "due_at", "<=", NOW), ("order_by", "due_at", "ASCENDING"),
                          ("order_by", "item_id", "ASCENDING"), ("limit", 9), PROJECTION]


async def test_firestore_without_a_skip_queries_every_class(repo, db):
    await repo.due_records(U_SCOPE, NOW, 10)
    assert [q[1][3] for q in executed(db)] == [0, 1, 2, 3, 4]
