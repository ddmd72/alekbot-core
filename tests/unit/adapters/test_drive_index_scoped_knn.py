"""The folder-scoped kNN of both drive index adapters (USER_DRIVE_INDEXING_RFC Δ246): `find_nearest_chunks_in` and
`find_nearest_hints_in` pre-filter `user_id ==` and `item_id in [...]` before `find_nearest`, at most KNN_ITEM_IDS_MAX
ids a query (Firestore's `in` limit), more refused before any query. Firestore: the query the real SDK builds, read
from its protobuf at the SDK boundary (`AsyncVectorQuery.get`). In memory: filter, then nearest. The composite vector
indexes the pre-filter needs are declared."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore
from google.cloud.firestore_v1.async_vector_query import AsyncVectorQuery

from src.adapters.firestore_drive_index_repo import (
    CHUNK_DISTANCE, CHUNK_VECTOR, HINT_DISTANCE, HINT_VECTOR, FirestoreDriveIndexRepository, chunk_to_doc,
)
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_enrichment import DriveHintDoc
from src.domain.drive_index import DriveChunk
from src.domain.drive_index_params import KNN_ITEM_IDS_MAX
from tests.unit.services.drive_index_fakes import U_SCOPE

_ROOT = Path(__file__).resolve().parents[3]
CHUNKS = "test_drive_index_chunks"
HINTS = "test_drive_index_hints"


def _chunk(item: str, index: int = 0) -> DriveChunk:
    return DriveChunk(user_id="u", connection_id="u", item_id=item, generation=1, index=index, text="", char_start=0, char_end=10,
                      kind="epub")


def _hint(item: str, index: int = 0) -> DriveHintDoc:
    return DriveHintDoc(user_id="u", connection_id="u", item_id=item, generation=1, index=index, kind="epub", chunk_text_hash="t",
                        items=(), hint_keys=("w:chapter",), passages_version=1)


@pytest.fixture
def sent(monkeypatch):
    """Every vector query the real SDK would send: (collection id, StructuredQuery protobuf); answers with `rows`."""
    log = {"queries": [], "rows": []}

    async def capture(self, *args, **kwargs):
        log["queries"].append((self._nested_query._parent.id, self._to_protobuf()))
        return [SimpleNamespace(to_dict=lambda row=row: dict(row)) for row in log["rows"]]

    monkeypatch.setattr(AsyncVectorQuery, "get", capture)
    return log


@pytest.fixture
def repo(mock_env_config):
    client = firestore.AsyncClient(project="p", credentials=AnonymousCredentials(), database="d")
    mock_env_config.drive_index_records_collection = "test_drive_index_records"
    mock_env_config.drive_sync_state_collection = "test_drive_sync_state"
    mock_env_config.drive_index_chunks_collection = CHUNKS
    mock_env_config.drive_index_hints_collection = HINTS
    return FirestoreDriveIndexRepository(client, mock_env_config)


def _filters(query):
    out = []
    for f in query.where.composite_filter.filters:
        ff = f.field_filter
        if ff.op.name == "IN":
            out.append((ff.field.field_path, "IN", [v.string_value for v in ff.value.array_value.values]))
        else:
            out.append((ff.field.field_path, ff.op.name, ff.value.string_value))
    return out


class TestFirestoreChunks:
    async def test_user_and_item_ids_filter_before_the_nearest(self, repo, sent):
        sent["rows"] = [{**chunk_to_doc(_chunk("book-2")), CHUNK_DISTANCE: 0.25}]
        hits = await repo.find_nearest_chunks_in(U_SCOPE, [1.0, 0.0], 300, ["book-1", "book-2", "book-1"])
        assert [(c.item_id, d) for c, d in hits] == [("book-2", 0.25)]
        ((collection, query),) = sent["queries"]
        assert collection == CHUNKS
        assert _filters(query) == [("connection_id", "EQUAL", "u"), ("item_id", "IN", ["book-1", "book-2"])]   # distinct
        assert query.find_nearest.vector_field.field_path == CHUNK_VECTOR
        assert query.find_nearest.limit == 300
        assert query.find_nearest.distance_result_field == CHUNK_DISTANCE
        assert query.find_nearest.distance_measure.name == "COSINE"
        selected = {f.field_path for f in query.select.fields}
        assert CHUNK_DISTANCE in selected and CHUNK_VECTOR not in selected and "text" not in selected

    async def test_thirty_ids_go_out_in_one_query(self, repo, sent):
        ids = [f"book-{n:02d}" for n in range(KNN_ITEM_IDS_MAX)]
        await repo.find_nearest_chunks_in(U_SCOPE, [1.0], 5, ids)
        ((_, query),) = sent["queries"]
        assert _filters(query)[1] == ("item_id", "IN", ids)

    async def test_more_than_thirty_distinct_ids_are_refused_before_any_query(self, repo, sent):
        ids = [f"book-{n:02d}" for n in range(KNN_ITEM_IDS_MAX + 1)]
        with pytest.raises(ValueError, match="at most 30"):
            await repo.find_nearest_chunks_in(U_SCOPE, [1.0], 5, ids)
        assert sent["queries"] == []

    async def test_no_ids_is_no_query(self, repo, sent):
        assert await repo.find_nearest_chunks_in(U_SCOPE, [1.0], 5, []) == []
        assert sent["queries"] == []


class TestFirestoreHints:
    async def test_user_and_item_ids_filter_before_the_nearest(self, repo, sent):
        sent["rows"] = [{"user_id": "u", "item_id": "book-1", "generation": 1, "index": 0, "kind": "epub",
                         "hint_keys": ["w:chapter"], HINT_DISTANCE: 0.5}]
        hits = await repo.find_nearest_hints_in(U_SCOPE, [1.0, 0.0], 300, ["book-1"])
        assert [(h.item_id, h.hint_keys, d) for h, d in hits] == [("book-1", ("w:chapter",), 0.5)]
        ((collection, query),) = sent["queries"]
        assert collection == HINTS
        assert _filters(query) == [("connection_id", "EQUAL", "u"), ("item_id", "IN", ["book-1"])]
        assert query.find_nearest.vector_field.field_path == HINT_VECTOR
        assert query.find_nearest.distance_result_field == HINT_DISTANCE
        selected = {f.field_path for f in query.select.fields}
        assert HINT_DISTANCE in selected and HINT_VECTOR not in selected and "items" not in selected

    async def test_more_than_thirty_distinct_ids_are_refused_before_any_query(self, repo, sent):
        with pytest.raises(ValueError, match="at most 30"):
            await repo.find_nearest_hints_in(U_SCOPE, [1.0], 5, [f"book-{n:02d}" for n in range(KNN_ITEM_IDS_MAX + 1)])
        assert sent["queries"] == []


class TestInMemory:
    async def test_chunks_of_other_files_never_answer_even_when_nearer(self):
        repo = InMemoryDriveIndexRepository()
        await repo.write_chunks([_chunk("notes", 0), _chunk("book-1", 0), _chunk("book-1", 1), _chunk("book-2", 0)],
                                [[1.0, 0.0], [1.0, 0.5], [1.0, 1.0], [1.0, 2.0]])
        hits = await repo.find_nearest_chunks_in(U_SCOPE, [1.0, 0.0], 2, ["book-1", "book-2"])
        assert [(c.item_id, c.index) for c, _ in hits] == [("book-1", 0), ("book-1", 1)]
        assert await repo.find_nearest_chunks_in(U_SCOPE, [1.0, 0.0], 2, []) == []

    async def test_hints_of_other_files_never_answer(self):
        repo = InMemoryDriveIndexRepository()
        await repo.write_hints([_hint("notes"), _hint("book-1")], [[1.0, 0.0], [1.0, 1.0]])
        hits = await repo.find_nearest_hints_in(U_SCOPE, [1.0, 0.0], 5, ["book-1"])
        assert [h.item_id for h, _ in hits] == ["book-1"]

    async def test_more_than_thirty_distinct_ids_are_refused(self):
        repo = InMemoryDriveIndexRepository()
        ids = [f"book-{n:02d}" for n in range(KNN_ITEM_IDS_MAX + 1)]
        with pytest.raises(ValueError):
            await repo.find_nearest_chunks_in(U_SCOPE, [1.0], 5, ids)
        with pytest.raises(ValueError):
            await repo.find_nearest_hints_in(U_SCOPE, [1.0], 5, ids)


@pytest.mark.parametrize("collection, vector", [("development_drive_index_chunks", CHUNK_VECTOR),
                                                ("development_drive_index_hints", HINT_VECTOR)])
def test_the_scoped_vector_index_is_declared(collection, vector):
    raw = json.loads((_ROOT / "config" / "firestore.indexes.json").read_text())
    wanted = [{"fieldPath": "connection_id", "order": "ASCENDING"}, {"fieldPath": "item_id", "order": "ASCENDING"},
              {"fieldPath": vector, "vectorConfig": {"dimension": 768, "flat": {}}}]
    assert sum(idx["collectionGroup"] == collection and idx.get("fields") == wanted for idx in raw) == 1
