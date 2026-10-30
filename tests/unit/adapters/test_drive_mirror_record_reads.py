"""The record-side reads of the transcript mirror (USER_DRIVE_INDEXING_RFC §27.9.4, Δ187, Δ217), on both adapters:
the queue, the wake-ups, and the seed pass's start-after cursor on `transcript_records`."""
from datetime import datetime, timedelta, timezone

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import DriveIndexRecord

from tests.unit.adapters.firestore_drive_eval_fakes import RECS, eval_transactions, make_eval_repo
from tests.unit.adapters.firestore_drive_mirror_fakes import NameAwareDb
from tests.unit.adapters.test_firestore_drive_index_v2_provisioning import _exempt, _required
from tests.unit.services.drive_index_fakes import U_SCOPE

T = datetime(2026, 10, 21, 9, 30, tzinfo=timezone.utc)


@pytest.fixture(params=["memory", "firestore"])
def adapter(request, mock_env_config):
    if request.param == "memory":
        yield InMemoryDriveIndexRepository(), None
        return
    db = NameAwareDb()
    with eval_transactions():
        yield make_eval_repo(db, mock_env_config), db


def _rec(item_id, user="u", **kw):
    return DriveIndexRecord(user, item_id, **kw)


async def test_mirror_queue_newest_first(adapter):
    repo, _ = adapter
    await repo.upsert_records([
        _rec("old", mirror_due=True, effective_day="2026-01-01"),
        _rec("new-b", mirror_due=True, effective_day="2026-10-01"),
        _rec("new-a", mirror_due=True, effective_day="2026-10-01"),
        _rec("idle", effective_day="2026-12-01"),
        _rec("held", mirror_due=False, mirror_retry_at=T, mirror_held_digest="d", effective_day="2026-12-02"),
        _rec("other", user="v", mirror_due=True, effective_day="2026-12-03"),
    ])
    assert [r.item_id for r in await repo.mirror_queue(U_SCOPE, 10)] == ["new-a", "new-b", "old"]
    assert [r.item_id for r in await repo.mirror_queue(U_SCOPE, 2)] == ["new-a", "new-b"]
    assert await repo.mirror_queue("nobody", 10) == []


async def test_mirror_wakeups(adapter):
    repo, _ = adapter
    await repo.upsert_records([
        _rec("b", mirror_retry_at=T), _rec("a", mirror_retry_at=T),
        _rec("early", mirror_retry_at=T - timedelta(hours=1)), _rec("later", mirror_retry_at=T + timedelta(hours=1)),
        _rec("none"), _rec("other", user="v", mirror_retry_at=T),
    ])
    assert [r.item_id for r in await repo.mirror_wakeups("u", T, 10)] == ["early", "a", "b"]
    assert [r.item_id for r in await repo.mirror_wakeups("u", T, 2)] == ["early", "a"]
    assert [r.item_id for r in await repo.mirror_wakeups("u", T - timedelta(hours=2), 10)] == []


async def test_transcript_records_after_cursor(adapter):
    repo, _ = adapter
    await repo.upsert_records([_rec(f"c{n}", transcript_state="current") for n in range(1, 6)]
                              + [_rec("c3x", user="v", transcript_state="current"), _rec("w", transcript_state="working")])
    assert [r.item_id for r in await repo.transcript_records("u", "current", 10)] == ["c1", "c2", "c3", "c4", "c5"]
    assert [r.item_id for r in await repo.transcript_records("u", "current", 2, after="c2")] == ["c3", "c4"]
    assert [r.item_id for r in await repo.transcript_records("u", "current", 10, after="c5")] == []
    assert [r.item_id for r in await repo.transcript_records("u", "current", 10, after=None)][0] == "c1"


async def test_the_firestore_cursor_read_is_served_by_single_field_indexes(adapter):
    repo, db = adapter
    if db is None:
        pytest.skip("Firestore wire only")
    await repo.upsert_records([_rec("c1", transcript_state="current")])
    db.cols[RECS].executed.clear()
    await repo.transcript_records("u", "current", 5, after="c0")
    (ops,) = db.cols[RECS].executed
    assert [op[1:3] for op in ops if op[0] == "where"] == [("user_id", "=="), ("transcript_state", "=="), ("__name__", ">")]
    assert not [op for op in ops if op[0] == "order_by"]      # the implicit document-id order: no composite
    assert not {"user_id", "transcript_state"} & _exempt("development_drive_index_records")
    assert _required([op for op in ops if op[0] == "where" and op[1] != "__name__"]) is None
