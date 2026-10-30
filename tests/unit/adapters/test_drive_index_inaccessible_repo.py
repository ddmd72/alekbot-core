"""The inaccessible fields in the repositories (USER_DRIVE_INDEXING_RFC §26.2, Δ47, Δ54).

`inaccessible_notified_since` is the fan-out's own record field: the tick's record writes leave it out (its upserts
merge whole records, so a stale snapshot would otherwise clear it and repeat the notice), as `state_to_doc` leaves out
the sync state's fan-out flags; only `set_inaccessible_notified` writes it, with a field update. Both repositories,
the Firestore wire at the client boundary and the in-memory reference."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from google.api_core.exceptions import NotFound
from google.cloud.firestore_v1._helpers import DatetimeWithNanoseconds

from src.adapters.firestore_drive_index_repo import RECORD_FIELDS, record_from_doc, record_to_doc
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import DriveIndexRecord

from tests.unit.adapters.firestore_drive_fakes import RECS, TS_MIN, Db, executed, make_repo, rec_doc, snap
from tests.unit.services.drive_index_fakes import U_SCOPE

T = datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)
NOTIFIED = "inaccessible_notified_since"


def marked(item_id="a", since=T, notified=None, **kw):
    return DriveIndexRecord(user_id="u", connection_id="u", item_id=item_id, inaccessible_since=since, inaccessible_checked_at=since,
                            inaccessible_reobserved=True, inaccessible_notified_since=notified, **kw)


class TestCodec:
    def test_record_to_doc_never_carries_the_fan_out_field(self):
        doc = record_to_doc(marked(notified=T))
        assert NOTIFIED not in doc and NOTIFIED not in RECORD_FIELDS
        assert doc["inaccessible_since"] == T and doc["inaccessible_checked_at"] == T
        assert doc["inaccessible_reobserved"] is True

    def test_every_inaccessible_field_reads_back_as_plain_utc(self):
        stored = {**record_to_doc(marked()), NOTIFIED: DatetimeWithNanoseconds(2026, 10, 14, 9, 30, tzinfo=timezone.utc)}
        stored["inaccessible_since"] = DatetimeWithNanoseconds(2026, 10, 14, 9, 30, tzinfo=timezone.utc)
        back = record_from_doc(stored)
        assert back.inaccessible_notified_since == T and type(back.inaccessible_notified_since) is datetime
        assert back.inaccessible_since == T and type(back.inaccessible_since) is datetime

    def test_an_old_record_reads_as_accessible(self):
        rec = record_from_doc({"user_id": "u", "item_id": "a"})
        assert (rec.inaccessible_since, rec.inaccessible_checked_at, rec.inaccessible_reobserved,
                rec.inaccessible_notified_since) == (None, None, False, None)


@pytest.fixture
def db():
    return Db()


@pytest.fixture
def repo(db, mock_env_config):
    return make_repo(db, mock_env_config)


class TestFirestore:
    async def test_upsert_never_merges_the_fan_out_field(self, repo, db):
        await repo.upsert_records([marked(notified=T)])
        _, data = db.batches[0].set.call_args.args
        assert NOTIFIED not in data and NOTIFIED not in db.batches[0].set.call_args.kwargs["merge"]
        assert data["inaccessible_since"] == T

    async def test_inaccessible_records_query(self, repo, db):
        db.cols[RECS].respond = lambda ops: [snap({**rec_doc("a", inaccessible_since=T), NOTIFIED: T})]
        recs = await repo.inaccessible_records(U_SCOPE, 50)
        assert [r.item_id for r in recs] == ["a"] and recs[0].inaccessible_notified_since == T
        ops = executed(db)[-1]
        assert ("where", "user_id", "==", "u") in ops and ("where", "inaccessible_since", ">=", TS_MIN) in ops
        assert ("order_by", "inaccessible_since", "ASCENDING") in ops and ("limit", 50) in ops
        projection = next(op for op in ops if op[0] == "select")[1]
        assert NOTIFIED in projection and set(RECORD_FIELDS) <= set(projection)

    async def test_set_notified_is_a_field_update_per_record_and_skips_a_deleted_one(self, repo, db):
        col = db.cols[RECS]
        col.document("u:gone").update = AsyncMock(side_effect=NotFound("deleted"))
        await repo.set_inaccessible_notified("u", {"a": T, "gone": T, "b": None})
        col.docs["u:a"].update.assert_awaited_once_with({NOTIFIED: T})
        col.docs["u:b"].update.assert_awaited_once_with({NOTIFIED: None})
        col.docs["u:a"].set.assert_not_awaited()


class TestInMemory:
    async def test_upsert_keeps_the_stored_fan_out_field(self):
        repo = InMemoryDriveIndexRepository()
        await repo.upsert_records([marked()])
        await repo.set_inaccessible_notified("u", {"a": T})
        await repo.upsert_records([marked(notified=None, name="renamed.pdf")])   # a stale tick snapshot
        rec = (await repo.get_records(U_SCOPE, ["a"]))["a"]
        assert rec.name == "renamed.pdf" and rec.inaccessible_notified_since == T

    async def test_inaccessible_records_oldest_first_marked_only(self):
        repo = InMemoryDriveIndexRepository()
        await repo.upsert_records([marked("b", since=T + timedelta(hours=1)), marked("a", since=T),
                                   DriveIndexRecord(user_id="u", connection_id="u", item_id="open"),
                                   DriveIndexRecord(user_id="v", item_id="x", inaccessible_since=T)])
        assert [r.item_id for r in await repo.inaccessible_records(U_SCOPE, 10)] == ["a", "b"]
        assert [r.item_id for r in await repo.inaccessible_records(U_SCOPE, 1)] == ["a"]

    async def test_set_notified_on_a_missing_record_writes_nothing(self):
        repo = InMemoryDriveIndexRepository()
        await repo.set_inaccessible_notified("u", {"nope": T})
        assert repo.records == {}
