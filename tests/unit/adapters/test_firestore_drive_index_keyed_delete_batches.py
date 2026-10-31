"""Chunk and hint deletes commit at most KEYED_DELETE_BATCH documents: a delete's index entries count toward
Firestore's transaction size, and RECORD_WRITE_BATCH chunks of a v2 index failed a purge with "Transaction too big"
(2026-10-19)."""
from types import SimpleNamespace

import pytest

from src.domain.drive_index_params import KEYED_DELETE_BATCH, RECORD_WRITE_BATCH

from tests.unit.adapters.firestore_drive_chunk_fakes import CHUNKS, HINTS, ChunkDb, chunk, chunk_snap, make_chunk_repo
from tests.unit.services.drive_index_fakes import U_SCOPE

N = 2 * KEYED_DELETE_BATCH + 7


@pytest.fixture
def db():
    return ChunkDb()


@pytest.fixture
def repo(db, mock_env_config):
    return make_chunk_repo(db, mock_env_config)


def batch_sizes(db):
    return [len(b.delete.call_args_list) for b in db.batches]


def test_the_keyed_batch_is_smaller_than_the_record_batch():
    assert KEYED_DELETE_BATCH < RECORD_WRITE_BATCH


async def test_a_purge_deletes_chunks_in_keyed_batches(repo, db):
    db.cols[CHUNKS].respond = lambda ops: [chunk_snap(chunk(index=i)) for i in range(N)]
    assert await repo.delete_user_chunks(U_SCOPE, RECORD_WRITE_BATCH) == N
    assert batch_sizes(db) == [KEYED_DELETE_BATCH, KEYED_DELETE_BATCH, 7]


async def test_collecting_a_generation_deletes_in_keyed_batches(repo, db):
    db.cols[CHUNKS].respond = lambda ops: [chunk_snap(chunk(gen=1, index=i)) for i in range(N)]
    assert await repo.delete_chunks_except(U_SCOPE, "a", 2) == N
    assert batch_sizes(db) == [KEYED_DELETE_BATCH, KEYED_DELETE_BATCH, 7]


async def test_a_purge_deletes_hints_in_keyed_batches(repo, db):
    db.cols[HINTS].respond = lambda ops: [SimpleNamespace(reference=SimpleNamespace(doc_id=f"h{i}"),
                                                          to_dict=lambda: {"generation": 1})
                                          for i in range(N)]
    assert await repo.delete_user_hints(U_SCOPE, RECORD_WRITE_BATCH) == N
    assert batch_sizes(db) == [KEYED_DELETE_BATCH, KEYED_DELETE_BATCH, 7]
