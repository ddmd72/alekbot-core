"""The search streams' chunk reads project neither `text` nor `content_terms` (USER_DRIVE_INDEXING_RFC §24.6, review
D-M3): a chunk may hold up to CONTENT_TERMS_PER_CHUNK terms, and a widened stream decodes up to FETCH_MAX rows per
kind. The term stream knows which term found a chunk from its own query; the window reads its chunks with
`get_chunks`."""
import pytest

from src.adapters.firestore_drive_index_repo import STREAM_FIELDS

from tests.unit.adapters.firestore_drive_chunk_fakes import CHUNKS, ChunkDb, make_chunk_repo
from tests.unit.adapters.firestore_drive_fakes import executed
from tests.unit.services.drive_index_fakes import U_SCOPE


@pytest.fixture
def db():
    return ChunkDb()


@pytest.fixture
def repo(db, mock_env_config):
    return make_chunk_repo(db, mock_env_config)


def test_the_stream_fields_hold_neither_text_nor_terms():
    assert "text" not in STREAM_FIELDS and "content_terms" not in STREAM_FIELDS
    assert {"user_id", "item_id", "generation", "index", "kind"} <= set(STREAM_FIELDS)


async def test_the_chunk_knn_does_not_read_the_terms(repo, db):
    await repo.find_nearest_chunks(U_SCOPE, [1.0, 0.0], 300)
    (ops,) = executed(db, CHUNKS)
    (select,) = [op for op in ops if op[0] == "select"]
    assert "content_terms" not in select[1]


async def test_the_term_stream_does_not_read_the_terms(repo, db):
    await repo.find_chunks_by_term(U_SCOPE, "20240012", 100)
    (ops,) = executed(db, CHUNKS)
    (select,) = [op for op in ops if op[0] == "select"]
    assert "content_terms" not in select[1]
