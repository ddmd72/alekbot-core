"""drive_index_smoke.py covers the chunk reads too (USER_DRIVE_INDEXING_RFC §12.5, §24.6): each runs once against the
chunk collection (or the records' sum), and nothing is written or deleted."""
import importlib.util
from pathlib import Path

import pytest
from google.api_core.exceptions import FailedPrecondition

from tests.unit.adapters.firestore_drive_chunk_fakes import CHUNKS, ChunkDb, make_chunk_repo
from tests.unit.adapters.firestore_drive_fakes import RECS

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure" / "drive_index_smoke.py"


@pytest.fixture
def smoke():
    spec = importlib.util.spec_from_file_location("drive_index_smoke", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_every_chunk_read_runs_once_and_nothing_is_written(smoke, mock_env_config):
    db = ChunkDb()
    assert await smoke.run_smoke(make_chunk_repo(db, mock_env_config), "smoke-user") == []
    chunk_queries = db.cols[CHUNKS].executed
    assert len(chunk_queries) == 5   # 2 nearest, 2 term, 1 page
    assert sum(1 for ops in chunk_queries if any(op[0] == "find_nearest" for op in ops)) == 2
    assert sum(1 for ops in chunk_queries if ("where", "content_terms", "array_contains", "20240012") in ops) == 2
    assert any(op[0] == "start_after" for ops in chunk_queries for op in ops)
    assert any(op[0] == "sum" for ops in db.cols[RECS].executed for op in ops)
    chunk_reads = [ids for ids, _ in db.get_all_calls if ids and ids[0].count(":") == 3]
    assert chunk_reads == [["smoke-user:smoke-item:1:0", "smoke-user:smoke-item:1:1"]] * 2
    assert db.batches == [] and not db.tx.method_calls


async def test_a_missing_chunk_index_is_reported_by_name(smoke, mock_env_config):
    db = ChunkDb()

    def respond(ops):
        if ("where", "content_terms", "array_contains", "20240012") in ops:
            raise FailedPrecondition("The query requires an index.")
        return []

    db.cols[CHUNKS].respond = respond
    failures = await smoke.run_smoke(make_chunk_repo(db, mock_env_config), "smoke-user")
    assert [name for name, _ in failures] == ["find_chunks_by_term", "find_chunks_by_term[kind]"]


async def test_the_chunk_cap_and_every_unread_count_run_once(smoke, mock_env_config):
    db = ChunkDb()
    assert await smoke.run_smoke(make_chunk_repo(db, mock_env_config), "smoke-user") == []
    records = db.cols[RECS].executed
    assert sum(1 for ops in records if ("where", "reason", "==", "user_chunk_cap") in ops
               and any(op[0] == "order_by" for op in ops)) == 1
    unread = [ops for ops in records if any(op[:3] == ("where", "kind", "in") for op in ops)]
    assert len(unread) == 6 and all(ops[-1][0] == "count" for ops in unread)
