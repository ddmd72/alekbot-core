"""`chunk_capped` and `count_unread` on Firestore (USER_DRIVE_INDEXING_RFC §5.4, §10.2): the release order of the
chunk cap, and the unread count as a few count aggregations — one per unread class, one `kind in` per query, at most
30 kinds each — never a scan; the in-memory reference semantics, and the SDK's encoding of the filters."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore
from google.cloud.firestore_v1.async_aggregation import AsyncAggregationQuery

from src.adapters.firestore_drive_index_repo import IN_VALUES_MAX, FirestoreDriveIndexRepository
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import DriveIndexRecord, Reason, RecordState
from src.domain.drive_index_policy import SLICE2_CONTENT_KINDS

from tests.unit.adapters.firestore_drive_chunk_fakes import ChunkDb, make_chunk_repo
from tests.unit.adapters.firestore_drive_fakes import RECS, executed, rec_doc, snap
from tests.unit.services.drive_index_fakes import U_SCOPE

T = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.fixture
def db():
    return ChunkDb()


@pytest.fixture
def repo(db, mock_env_config):
    return make_chunk_repo(db, mock_env_config)


class TestChunkCapped:
    async def test_release_order_query(self, repo, db):
        db.cols[RECS].respond = lambda ops: [snap(rec_doc("a", state=RecordState.METADATA_ONLY,
                                                          reason=Reason.USER_CHUNK_CAP))]
        assert [r.item_id for r in await repo.chunk_capped(U_SCOPE, 20)] == ["a"]
        (ops,) = executed(db)
        assert ops[:3] == [("where", "user_id", "==", "u"), ("where", "state", "==", "metadata_only"),
                           ("where", "reason", "==", "user_chunk_cap")]
        assert ops[3:6] == [("order_by", "card_rank", "ASCENDING"), ("order_by", "modified_at", "DESCENDING"),
                            ("order_by", "item_id", "ASCENDING")]
        assert ("limit", 20) in ops

    async def test_in_memory_reference_order(self):
        mem = InMemoryDriveIndexRepository()
        cap = dict(state=RecordState.METADATA_ONLY, reason=Reason.USER_CHUNK_CAP)
        await mem.upsert_records([
            DriveIndexRecord(user_id="u", connection_id="u", item_id="old", card_rank=0, modified_at=T, **cap),
            DriveIndexRecord(user_id="u", connection_id="u", item_id="new", card_rank=0, modified_at=T.replace(day=9), **cap),
            DriveIndexRecord(user_id="u", connection_id="u", item_id="media", card_rank=1, modified_at=T.replace(day=20), **cap),
            DriveIndexRecord(user_id="u", connection_id="u", item_id="undated", card_rank=0, **cap),
            DriveIndexRecord(user_id="u", connection_id="u", item_id="other", state=RecordState.METADATA_ONLY, reason=Reason.TOO_LARGE),
        ])
        assert [r.item_id for r in await mem.chunk_capped(U_SCOPE, 10)] == ["new", "old", "undated", "media"]


class TestCountUnread:
    async def test_one_count_per_unread_class_with_one_kind_filter(self, repo, db):
        db.cols[RECS].respond = lambda ops: [[SimpleNamespace(alias="n", value=2)]]
        assert await repo.count_unread(U_SCOPE, {"pdf", "docx"}) == 12   # 6 classes x 2
        queries = executed(db)
        assert len(queries) == 6 and all(ops[-1] == ("count", "n") for ops in queries)
        assert all(("where", "kind", "in", ["docx", "pdf"]) in ops for ops in queries)
        classes = [tuple(op[1:] for op in ops if op[0] == "where" and op[1] not in ("user_id", "kind"))
                   for ops in queries]
        assert classes == [
            (("state", "==", "failed"),),
            (("state", "==", "pending"), ("indexed_generation", "IS_NULL", None)),
            *[(("state", "==", "metadata_only"), ("reason", "==", r)) for r in
              ("too_large", "no_text", "machine_text", "user_chunk_cap")],
        ]

    async def test_more_than_30_kinds_split_the_in_filter(self, repo, db):
        kinds = {f"k{i:02d}" for i in range(IN_VALUES_MAX + 5)}
        await repo.count_unread(U_SCOPE, kinds)
        queries = executed(db)
        assert len(queries) == 12
        sizes = sorted({len(op[3]) for ops in queries for op in ops if op[:3] == ("where", "kind", "in")})
        assert sizes == [5, IN_VALUES_MAX]

    async def test_no_kinds_no_query(self, repo, db):
        assert await repo.count_unread(U_SCOPE, set()) == 0
        assert executed(db) == []

    async def test_in_memory_reference_semantics(self):
        mem = InMemoryDriveIndexRepository()
        await mem.upsert_records([
            DriveIndexRecord(user_id="u", connection_id="u", item_id="p", kind="pdf", state=RecordState.PENDING),
            DriveIndexRecord(user_id="u", connection_id="u", item_id="p2", kind="pdf", state=RecordState.PENDING, indexed_generation=1),
            DriveIndexRecord(user_id="u", connection_id="u", item_id="f", kind="docx", state=RecordState.FAILED),
            DriveIndexRecord(user_id="u", connection_id="u", item_id="t", kind="pdf", state=RecordState.METADATA_ONLY,
                             reason=Reason.NO_TEXT),
            DriveIndexRecord(user_id="u", connection_id="u", item_id="c", kind="code", state=RecordState.FAILED),
            DriveIndexRecord(user_id="u", connection_id="u", item_id="d", kind="folder", is_folder=True, state=RecordState.PENDING),
        ])
        assert await mem.count_unread(U_SCOPE, SLICE2_CONTENT_KINDS) == 3

    async def test_the_sent_aggregations(self, monkeypatch, mock_env_config):
        sent = []

        async def capture(self, *args, **kwargs):
            sent.append(self._to_protobuf())
            return [[SimpleNamespace(alias="n", value=1)]]

        monkeypatch.setattr(AsyncAggregationQuery, "get", capture)
        client = firestore.AsyncClient(project="p", credentials=AnonymousCredentials(), database="d")
        mock_env_config.drive_index_records_collection = "test_drive_index_records"
        mock_env_config.drive_sync_state_collection = "test_drive_sync_state"
        mock_env_config.drive_index_chunks_collection = "test_drive_index_chunks"
        assert await FirestoreDriveIndexRepository(client, mock_env_config).count_unread(U_SCOPE, {"pdf"}) == 6
        pending = sent[1].structured_query.where.composite_filter.filters
        assert pending[2].unary_filter.op.name == "IS_NULL"
        assert pending[2].unary_filter.field.field_path == "indexed_generation"
        assert pending[3].field_filter.op.name == "IN"
        assert [v.string_value for v in pending[3].field_filter.value.array_value.values] == ["pdf"]
