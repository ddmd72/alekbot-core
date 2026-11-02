"""`unreadable_records` (USER_DRIVE_INDEXING_RFC §6): the Cabinet's list of files that could not be read. On Firestore:
the filters of `count_unread(pending=False)` exactly (so no new composite index), a limit and a lean projection on each
query, merged by item id and cut to the limit, I3 per record; the in-memory reference agrees with the count."""
from src.adapters.firestore_drive_index_repo import UNREADABLE_FIELDS
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import DriveIndexRecord, Reason, RecordState
from src.domain.drive_index_params import CABINET_UNREADABLE_MAX

from tests.unit.adapters.firestore_drive_chunk_fakes import ChunkDb, make_chunk_repo
from tests.unit.adapters.firestore_drive_fakes import RECS, executed, rec_doc, snap
from tests.unit.services.drive_index_fakes import U_SCOPE

DOCS = frozenset({"pdf"})


def _filters(ops):
    return [op for op in ops if op[0] == "where"]


class TestFirestore:
    async def test_same_filters_as_the_unreadable_counts_and_no_order(self, mock_env_config):
        db = ChunkDb()
        repo = make_chunk_repo(db, mock_env_config)
        await repo.count_unread(U_SCOPE, DOCS, pending=False)
        counted = [_filters(ops) for ops in executed(db)]
        db2 = ChunkDb()
        await make_chunk_repo(db2, mock_env_config).unreadable_records(U_SCOPE, DOCS, limit=CABINET_UNREADABLE_MAX)
        listed = executed(db2)
        assert [_filters(ops) for ops in listed] == counted and len(listed) == 5
        assert not any(op[0] == "order_by" for ops in listed for op in ops)
        assert not any(("where", "state", "==", "pending") in ops for ops in listed)

    async def test_each_query_has_the_limit_and_the_lean_projection(self, mock_env_config):
        db = ChunkDb()
        await make_chunk_repo(db, mock_env_config).unreadable_records(U_SCOPE, DOCS, limit=3)
        for ops in executed(db):
            assert ("limit", 3) in ops and ("select", UNREADABLE_FIELDS) in ops
        assert {"user_id", "name", "state", "reason", "web_url"} <= set(UNREADABLE_FIELDS)

    async def test_images_and_transcribed_kinds_get_their_own_queries(self, mock_env_config):
        db = ChunkDb()
        await make_chunk_repo(db, mock_env_config).unreadable_records(
            U_SCOPE, DOCS, frozenset({"image"}), transcribed=frozenset({"audio"}), limit=2)
        ops_all = executed(db)
        assert len(ops_all) == 5 + 5 + 4            # documents, text images, recordings (no `no_text` class)
        assert any(("where", "triage_verdict", "==", "text") in ops for ops in ops_all)
        assert any(("where", "kind", "in", ["audio"]) in ops for ops in ops_all)

    async def test_merged_by_item_id_and_cut_to_the_limit(self, mock_env_config):
        db = ChunkDb()
        repo = make_chunk_repo(db, mock_env_config)
        docs = {i: rec_doc(i, name=f"{i}.pdf", state=RecordState.FAILED) for i in "abcdef"}

        def respond(ops):
            return [snap(docs[i]) for i in "abcde"] if ("where", "state", "==", "failed") in ops \
                else [snap(docs["a"]), snap(docs["f"])]
        db.cols[RECS].respond = respond
        found = await repo.unreadable_records(U_SCOPE, DOCS, limit=4)
        assert [r.item_id for r in found] == ["a", "b", "c", "d"]
        assert len({r.item_id for r in found}) == len(found)

    async def test_a_record_of_another_user_is_dropped(self, mock_env_config):
        db = ChunkDb()
        repo = make_chunk_repo(db, mock_env_config)
        mine, theirs = rec_doc("a", state=RecordState.FAILED), rec_doc("b", state=RecordState.FAILED)
        theirs["user_id"] = "someone-else"
        db.cols[RECS].respond = lambda ops: [snap(mine), snap(theirs)]
        assert [r.item_id for r in await repo.unreadable_records(U_SCOPE, DOCS, limit=5)] == ["a"]

    async def test_a_zero_limit_reads_nothing(self, mock_env_config):
        db = ChunkDb()
        assert await make_chunk_repo(db, mock_env_config).unreadable_records(U_SCOPE, DOCS, limit=0) == []
        assert executed(db) == []


def rec(item_id, **kw):
    return DriveIndexRecord(**{"user_id": "u", "connection_id": "u", "item_id": item_id, "kind": "pdf",
                               "state": RecordState.PENDING, **kw})


class TestInMemory:
    async def _repo(self):
        repo = InMemoryDriveIndexRepository()
        await repo.upsert_records([
            rec("p1"), rec("p2"), rec("capped", card_capped=True), rec("ok", state=RecordState.INDEXED),
            rec("f", state=RecordState.FAILED),
            rec("t", state=RecordState.METADATA_ONLY, reason=Reason.TOO_LARGE),
            rec("n", state=RecordState.METADATA_ONLY, reason=Reason.NO_TEXT),
            rec("photo", kind="jpg", state=RecordState.FAILED),
            rec("folder", is_folder=True, state=RecordState.FAILED),
            rec("rec", kind="audio", state=RecordState.METADATA_ONLY, reason=Reason.NO_TEXT),
        ])
        return repo

    async def test_the_could_not_be_read_classes_only(self):
        repo = await self._repo()
        found = await repo.unreadable_records(U_SCOPE, DOCS, limit=10)
        assert [r.item_id for r in found] == ["f", "n", "t"]

    async def test_agrees_with_the_count_and_honours_the_limit(self):
        repo = await self._repo()
        assert len(await repo.unreadable_records(U_SCOPE, DOCS, limit=10)) == await repo.count_unread(
            U_SCOPE, DOCS, pending=False)
        assert len(await repo.unreadable_records(U_SCOPE, DOCS, limit=2)) == 2

    async def test_a_silent_recording_is_not_unreadable_but_a_failed_one_is(self):
        repo = await self._repo()
        await repo.upsert_records([rec("bad", kind="audio", state=RecordState.FAILED)])
        found = await repo.unreadable_records(U_SCOPE, DOCS, transcribed=frozenset({"audio"}), limit=10)
        assert "bad" in {r.item_id for r in found} and "rec" not in {r.item_id for r in found}
