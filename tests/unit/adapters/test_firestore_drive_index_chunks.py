"""FirestoreDriveIndexRepository chunk methods (USER_DRIVE_INDEXING_RFC §3.3, §5.4, §7.4, §7.5, §24.6): codec, write
batches, deletes by query, the kNN projection, one array_contains per term, reads by doc id, the readable-chunk sum
and the chunk page. On the recording client; the SDK's own encoding is checked where a field name could slip."""
from types import SimpleNamespace

import pytest
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore
from google.cloud.firestore_v1.async_aggregation import AsyncAggregationQuery
from google.cloud.firestore_v1.async_query import AsyncQuery
from google.cloud.firestore_v1.async_vector_query import AsyncVectorQuery
from google.cloud.firestore_v1.vector import Vector

from src.adapters.firestore_drive_index_repo import (
    CHUNK_DISTANCE, CHUNK_FIELDS, CHUNK_LEAN_FIELDS, CHUNK_VECTOR, STREAM_FIELDS, FirestoreDriveIndexRepository,
    chunk_from_doc, chunk_to_doc,
)
from src.domain.drive_index import DriveChunk, chunk_doc_id
from src.domain.drive_index_params import RECORD_WRITE_BATCH

from tests.support.g1a3_drive_chunks import chunk
from tests.unit.adapters import firestore_drive_chunk_fakes
from tests.unit.adapters.firestore_drive_chunk_fakes import CHUNKS, ChunkDb, chunk_snap, make_chunk_repo
from tests.unit.adapters.firestore_drive_fakes import RECS, executed, snap
from tests.unit.services.drive_index_fakes import U_SCOPE


@pytest.fixture
def db():
    return ChunkDb()


@pytest.fixture
def repo(db, mock_env_config):
    return make_chunk_repo(db, mock_env_config)


def sent_sets(batch):
    return [c.args for c in batch.set.call_args_list]


class TestCodec:
    def test_round_trip(self):
        c = DriveChunk("u", "i", 3, 7, "Рахунок № INV-2024/0012 на 1 250,00 €", 100, 140, "pdf",
                       ("1250", "1250.00", "inv20240012"))
        doc = chunk_to_doc(c)
        assert doc["content_terms"] == ["1250", "1250.00", "inv20240012"]
        assert set(doc) == set(CHUNK_FIELDS)
        assert chunk_from_doc({**doc, CHUNK_VECTOR: Vector([1.0]), CHUNK_DISTANCE: 0.1}) == c

    def test_a_projected_stream_row_reads_with_empty_text(self):
        # review D-M3: a stream row projects neither `text` nor `content_terms`; both read back empty
        doc = {k: v for k, v in chunk_to_doc(firestore_drive_chunk_fakes.chunk(terms=("1250",))).items()
               if k in STREAM_FIELDS}
        assert "text" not in doc and "content_terms" not in doc
        assert chunk_from_doc(doc) == DriveChunk("u", "a", 1, 0, "", 0, 10, "pdf", ())

    def test_missing_terms_read_as_empty(self):
        doc = {k: v for k, v in chunk_to_doc(chunk()).items() if k != "content_terms"}
        assert chunk_from_doc(doc).content_terms == ()

    def test_key(self):
        assert chunk_key("u", "i", 3, 7) == "u:i:3:7"

    def test_the_sdk_encodes_a_chunk(self):
        from google.cloud.firestore_v1 import _helpers
        encoded = _helpers.encode_dict({**chunk_to_doc(chunk(terms=("1250",))), CHUNK_VECTOR: Vector([0.5, 0.25])})
        assert encoded["content_terms"].array_value.values[0].string_value == "1250"
        assert encoded["generation"].integer_value == 1


class TestWrite:
    @pytest.mark.slow
    async def test_writes_in_batches_with_vectors(self, repo, db):
        chunks = [chunk(index=i, terms=("1250",)) for i in range(RECORD_WRITE_BATCH + 1)]
        await repo.write_chunks(chunks, [[float(i), 1.0] for i in range(len(chunks))])
        assert len(db.batches) == 2
        first = sent_sets(db.batches[0])
        assert len(first) == RECORD_WRITE_BATCH and len(sent_sets(db.batches[1])) == 1
        ref, data = first[1]
        assert ref.doc_id == "u:a:1:1"
        assert data[CHUNK_VECTOR] == Vector([1.0, 1.0]) and data["content_terms"] == ["1250"]
        assert {k: v for k, v in data.items() if k != CHUNK_VECTOR} == chunk_to_doc(chunks[1])
        assert all(b.commit.await_count == 1 for b in db.batches)

    async def test_vectors_must_match_chunks(self, repo, db):
        with pytest.raises(ValueError):
            await repo.write_chunks([chunk(), chunk(index=1)], [[1.0]])
        assert db.batches == []


class TestDeleteChunksExcept:
    async def test_deletes_every_other_generation(self, repo, db):
        db.cols[CHUNKS].respond = lambda ops: [chunk_snap(chunk(gen=g, index=i)) for g, i in ((1, 0), (2, 0), (2, 1),
                                                                                               (3, 0))]
        assert await repo.delete_chunks_except(U_SCOPE, "a", 2) == 2
        (ops,) = executed(db, CHUNKS)
        assert ops == [("where", "user_id", "==", "u"), ("where", "item_id", "==", "a"), ("select", ("generation",))]
        deleted = [c.args[0].doc_id for c in db.batches[0].delete.call_args_list]
        assert deleted == ["u:a:1:0", "u:a:3:0"]

    async def test_none_deletes_all(self, repo, db):
        db.cols[CHUNKS].respond = lambda ops: [chunk_snap(chunk(gen=g)) for g in (1, 2)]
        assert await repo.delete_chunks_except(U_SCOPE, "a", None) == 2

    async def test_nothing_to_delete_commits_nothing(self, repo, db):
        assert await repo.delete_chunks_except(U_SCOPE, "a", 1) == 0
        assert db.batches == []


class TestFindNearest:
    async def test_projects_the_chunk_fields_and_the_distance(self, repo, db):
        db.cols[CHUNKS].respond = lambda ops: [chunk_snap(chunk(item="b"), **{CHUNK_DISTANCE: 0.25})]
        hits = await repo.find_nearest_chunks(U_SCOPE, [1.0, 0.0], 300, kind="pdf")
        assert [(c.item_id, d) for c, d in hits] == [("b", 0.25)]
        (ops,) = executed(db, CHUNKS)
        assert [op[0] for op in ops] == ["where", "where", "select", "find_nearest"]
        assert ops[1] == ("where", "kind", "==", "pdf")
        assert set(ops[2][1]) == set(STREAM_FIELDS) | {CHUNK_DISTANCE}
        assert CHUNK_VECTOR not in ops[2][1] and "text" not in ops[2][1]   # windows come from get_chunks
        knn = ops[3][1]
        assert (knn["vector_field"], knn["limit"], knn["distance_result_field"]) == (CHUNK_VECTOR, 300,
                                                                                    CHUNK_DISTANCE)

    async def test_the_sent_query_selects_the_distance_field(self, monkeypatch, mock_env_config):
        sent = []

        async def capture(self, *args, **kwargs):
            sent.append(self._to_protobuf())
            return []

        monkeypatch.setattr(AsyncVectorQuery, "get", capture)
        repo = _real_repo(mock_env_config)
        assert await repo.find_nearest_chunks(U_SCOPE, [1.0], 5) == []
        (query,) = sent
        selected = {f.field_path for f in query.select.fields}
        assert CHUNK_DISTANCE in selected and CHUNK_VECTOR not in selected
        assert query.find_nearest.vector_field.field_path == CHUNK_VECTOR
        assert query.find_nearest.distance_result_field == CHUNK_DISTANCE


class TestFindByTerm:
    async def test_one_array_contains_term_ordered_by_key(self, repo, db):
        db.cols[CHUNKS].respond = lambda ops: [chunk_snap(chunk(terms=("20240012",)))]
        found = await repo.find_chunks_by_term(U_SCOPE, "20240012", 100)
        assert [c.content_terms for c in found] == [("20240012",)]
        (ops,) = executed(db, CHUNKS)
        assert ops == [("where", "user_id", "==", "u"), ("where", "content_terms", "array_contains", "20240012"),
                       ("order_by", "item_id", "ASCENDING"), ("order_by", "generation", "ASCENDING"),
                       ("order_by", "index", "ASCENDING"), ("limit", 100), ("select", STREAM_FIELDS)]

    async def test_kind(self, repo, db):
        await repo.find_chunks_by_term(U_SCOPE, "1250", 5, kind="xlsx")
        (ops,) = executed(db, CHUNKS)
        assert ops[1] == ("where", "kind", "==", "xlsx")


class TestGetChunks:
    async def test_by_doc_id_in_index_order_without_vectors(self, repo, db):
        for c in (chunk(index=2), chunk(index=0)):
            db.stored[chunk_doc_id(U_SCOPE, "a", 1, c.index)] = chunk_to_doc(c)
        found = await repo.get_chunks(U_SCOPE, "a", 1, [2, 1, 0, 2])
        assert [c.index for c in found] == [0, 2]
        ((ids, paths),) = db.get_all_calls
        assert ids == ["u:a:1:0", "u:a:1:1", "u:a:1:2"] and CHUNK_VECTOR not in paths

    async def test_with_vectors(self, repo, db):
        db.stored["u:a:1:0"] = {**chunk_to_doc(chunk()), CHUNK_VECTOR: Vector([0.5, 0.5])}
        rows = await repo.get_chunks_with_vectors(U_SCOPE, "a", 1, [0, 1])
        assert [(c.index, v) for c, v in rows] == [(0, [0.5, 0.5])]
        ((_, paths),) = db.get_all_calls
        assert CHUNK_VECTOR in paths


class TestReadableTotal:
    async def test_sum_over_records_with_a_readable_generation(self, repo, db):
        db.cols[RECS].respond = lambda ops: [[SimpleNamespace(alias="n", value=1234)]]
        assert await repo.readable_chunk_total(U_SCOPE) == 1234
        (ops,) = executed(db, RECS)
        assert ops == [("where", "user_id", "==", "u"), ("where", "indexed_generation", ">=", 0),
                       ("sum", "chunk_count", "n")]

    async def test_no_match_is_zero(self, repo, db):
        db.cols[RECS].respond = lambda ops: [[SimpleNamespace(alias="n", value=None)]]
        assert await repo.readable_chunk_total(U_SCOPE) == 0

    async def test_the_sent_aggregation(self, monkeypatch, mock_env_config):
        sent = []

        async def capture(self, *args, **kwargs):
            sent.append(self._to_protobuf())
            return [[SimpleNamespace(alias="n", value=7)]]

        monkeypatch.setattr(AsyncAggregationQuery, "get", capture)
        assert await _real_repo(mock_env_config).readable_chunk_total(U_SCOPE) == 7
        (proto,) = sent
        (agg,) = proto.aggregations
        assert agg.sum.field.field_path == "chunk_count" and agg.alias == "n"
        filters = proto.structured_query.where.composite_filter.filters
        assert [(f.field_filter.field.field_path, f.field_filter.op.name) for f in filters] == [
            ("user_id", "EQUAL"), ("indexed_generation", "GREATER_THAN_OR_EQUAL")]
        assert filters[1].field_filter.value.integer_value == 0


class TestChunksPage:
    async def test_first_page(self, repo, db):
        db.cols[CHUNKS].respond = lambda ops: [chunk_snap(chunk())]
        assert [c.item_id for c in await repo.chunks_page(U_SCOPE, None, 50)] == ["a"]
        (ops,) = executed(db, CHUNKS)
        assert ops == [("where", "user_id", "==", "u"), ("order_by", "item_id", "ASCENDING"),
                       ("order_by", "generation", "ASCENDING"), ("order_by", "index", "ASCENDING"),
                       ("select", CHUNK_LEAN_FIELDS), ("limit", 50)]   # ids only (review m3)

    async def test_starts_after_the_key(self, repo, db):
        await repo.chunks_page(U_SCOPE, ("a", 2, 9), 50)
        (ops,) = executed(db, CHUNKS)
        assert ("start_after", {"item_id": "a", "generation": 2, "index": 9}) in ops

    async def test_the_sent_cursor(self, monkeypatch, mock_env_config):
        sent = []

        async def capture(self, *args, **kwargs):
            sent.append(self._to_protobuf())
            return []

        monkeypatch.setattr(AsyncQuery, "get", capture)
        await _real_repo(mock_env_config).chunks_page(U_SCOPE, ("a", 2, 9), 50)
        (query,) = sent
        assert [o.field.field_path for o in query.order_by] == ["item_id", "generation", "index"]
        assert [v.string_value or v.integer_value for v in query.start_at.values] == ["a", 2, 9]
        assert not query.start_at.before   # start_after: the cursor row itself is excluded


class TestDeleteUserChunks:
    async def test_deletes_up_to_limit(self, repo, db):
        db.cols[CHUNKS].respond = lambda ops: [snap({}, f"u:a:1:{i}") for i in range(3)]
        assert await repo.delete_user_chunks(U_SCOPE, 3) == 3
        (ops,) = executed(db, CHUNKS)
        assert ops[0] == ("where", "user_id", "==", "u") and ops[-1] == ("limit", 3)
        assert db.batches[0].delete.call_count == 3


def _real_repo(env_config):
    client = firestore.AsyncClient(project="p", credentials=AnonymousCredentials(), database="d")
    env_config.drive_index_records_collection = "test_drive_index_records"
    env_config.drive_sync_state_collection = "test_drive_sync_state"
    env_config.drive_index_chunks_collection = "test_drive_index_chunks"
    return FirestoreDriveIndexRepository(client, env_config)
