"""scripts/infrastructure/drive_index_smoke.py: runs every repository read once, writes nothing, and reports each
failing query (USER_DRIVE_INDEXING_RFC §12.5). Run here on the recording Firestore client, never on the network."""
import importlib.util
from pathlib import Path

import pytest
from google.api_core.exceptions import FailedPrecondition

from src.domain.drive_index_policy import SLICE2_CONTENT_KINDS
from src.domain.drive_search import KIND_FAMILIES

from tests.unit.adapters.firestore_drive_chunk_fakes import CHUNKS, ChunkDb, make_chunk_repo
from tests.unit.adapters.firestore_drive_fakes import RECS, STATES

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure" / "drive_index_smoke.py"
# Search v2's checks (§25.2.5, pinned on the evaluating client in test_drive_index_smoke_v2.py) add to the records: 4
# about pages, expiring, the enrichment queue and wake-ups, keys pending, the per-job-state counts (2 jobs x 6 states
# per 30 kinds) and the largest kind family's card kNN, one per member kind; to the chunks: 2 word-key pages and the
# family's chunk kNN.
_FAMILY = len(max(KIND_FAMILIES.values(), key=len))
_V2_RECORD_QUERIES = 4 + 1 + 1 + 1 + 1 + 12 * ((len(SLICE2_CONTENT_KINDS) + 29) // 30) + _FAMILY
_V2_CHUNK_QUERIES = 2 + _FAMILY


@pytest.fixture
def smoke():
    spec = importlib.util.spec_from_file_location("drive_index_smoke", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_every_read_runs_once_and_nothing_is_written(smoke, mock_env_config):
    db = ChunkDb()   # the chunk collection exists (§24.6); its reads are pinned in test_drive_index_smoke_chunks.py
    repo = make_chunk_repo(db, mock_env_config)
    assert await smoke.run_smoke(repo, "smoke-user") == []
    queries = db.cols[RECS].executed
    # due 5 + oldest_due_at 5 + count_carded 1 + count_by_state 7 + lowest_ranked_carded 1 (empty) + 2 direct
    # eviction queries + children, records_page, records_seen_before, card_capped, 2 nearest, 2 name, 2 filter
    # + readable_chunk_total's sum (§24.6) + chunk_capped 1 + count_unread's 6 counts (one kind batch x 6 classes)
    # + 6 more over the images triaged as text (review D-M4) = 45; + the Search v2 record reads
    assert len(queries) == 45 + _V2_RECORD_QUERIES
    assert any(("where", "modified_at", "IS_NULL", None) in ops for ops in queries)
    assert sum(1 for ops in queries if any(op[0] == "sum" for op in ops)) == 1
    assert len(db.cols[CHUNKS].executed) == 5 + _V2_CHUNK_QUERIES  # 2 nearest, 2 term, 1 page; + v2
    assert len(db.cols[STATES].executed) == 1                      # list_sync_user_ids
    assert db.batches == [] and not db.tx.method_calls             # no batch, no transaction
    for col in db.cols.values():
        for doc in col.docs.values():
            doc.set.assert_not_called()
            doc.update.assert_not_called()
            doc.delete.assert_not_called()


async def test_a_missing_index_is_reported_and_the_rest_still_runs(smoke, mock_env_config):
    db = ChunkDb()

    def respond(ops):
        if any(op[0] == "find_nearest" for op in ops):
            raise FailedPrecondition("The query requires an index.")
        return [[type("Agg", (), {"value": 0})()]] if ops[-1][0] in ("count", "sum") else []

    db.cols[RECS].respond = respond
    failures = await smoke.run_smoke(make_chunk_repo(db, mock_env_config), "smoke-user")
    family = ",".join(sorted(max(KIND_FAMILIES.values(), key=len)))
    # the v2 family check runs card kNN first, so it fails on its first member kind and its other reads do not run
    assert [name for name, _ in failures] == ["find_nearest_cards", "find_nearest_cards[kind]", f"family kNN[{family}]"]
    assert "requires an index" in failures[0][1] and "requires an index" in failures[2][1]
    # the failing ones were sent, and every other query still ran (the family check stops after its first card kNN)
    assert len(db.cols[RECS].executed) == 45 + _V2_RECORD_QUERIES - (_FAMILY - 1)
    assert len(db.cols[CHUNKS].executed) == 5 + 2   # including the chunk reads after them (no family chunk kNN)
