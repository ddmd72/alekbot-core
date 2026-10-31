"""scripts/infrastructure/drive_index_smoke.py: runs every repository read once, writes nothing, and reports each
failing query (USER_DRIVE_INDEXING_RFC §12.5). Run here on the recording Firestore client, never on the network."""
import importlib.util
from pathlib import Path

import pytest
from google.api_core.exceptions import FailedPrecondition

from src.domain.drive_index_policy import SLICE2_CONTENT_KINDS
from src.domain.drive_search import KIND_FAMILIES

from tests.unit.adapters.firestore_drive_chunk_fakes import (
    BATCHES, CHUNKS, JOBS, PARTS, TRANSCRIPTS, WINDOWS, ChunkDb, make_chunk_repo,
)
from tests.unit.adapters.firestore_drive_fakes import RECS, STATES

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure" / "drive_index_smoke.py"
# Search v2's checks (§25.2.5, pinned on the evaluating client in test_drive_index_smoke_v2.py) add to the records: 4
# about pages, expiring, the enrichment queue (whole, then per job: profile and passages, P5 review MINOR 6) and
# wake-ups, keys pending, the per-job-state counts (2 jobs x 6 states
# per 30 kinds) and the largest kind family's card kNN, one per member kind; to the chunks: 2 word-key pages and the
# family's chunk kNN.
_FAMILY = len(max(KIND_FAMILIES.values(), key=len))
_V2_RECORD_QUERIES = 4 + 1 + 1 + 2 + 1 + 1 + 12 * ((len(SLICE2_CONTENT_KINDS) + 29) // 30) + _FAMILY
_V2_CHUNK_QUERIES = 2 + _FAMILY
# §26.2 (§26.9 step 3): the records the bot cannot open, one read (the tick's counters and the fan-out's notice)
_ACCESS_RECORD_QUERIES = 1
# §27.3 (Task 7B): the transcript queue and its wake-ups read the records; the copy check, the open batches, the
# store's orphans and the two collection-group sweeps read their own collections (pinned below). Task 14B adds the
# two later record reads: the in-flight records by state (Task 12's `transcript_records`, the stage's and the purge's
# job discovery) and the key-in-use check (Task 13's `transcript_key_in_use`, the orphan sweep's guard)
_TRANSCRIPT_RECORD_QUERIES = 4
TRANSCRIPT_CHECKS = ["transcript_queue", "transcript_wakeups", "transcript_records", "jobs_by_content_key",
                     "get_open_batches", "orphan_transcripts", "transcript_key_in_use",
                     "orphaned windows (collection group)", "orphaned parts (collection group)"]


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
    assert len(queries) == 45 + _V2_RECORD_QUERIES + _ACCESS_RECORD_QUERIES + _TRANSCRIPT_RECORD_QUERIES
    assert any(("where", "modified_at", "IS_NULL", None) in ops for ops in queries)
    assert sum(1 for ops in queries if any(op[:2] == ("order_by", "inaccessible_since") for op in ops)) == 1
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
    assert len(db.cols[RECS].executed) == (45 + _V2_RECORD_QUERIES + _ACCESS_RECORD_QUERIES
                                           + _TRANSCRIPT_RECORD_QUERIES - (_FAMILY - 1))
    assert len(db.cols[CHUNKS].executed) == 5 + 2   # including the chunk reads after them (no family chunk kNN)


async def test_every_transcript_read_is_a_named_check_and_runs_once(smoke, mock_env_config):
    """Task 7B (§27.3-§27.5, §27.8 b; Task 14B adds the in-flight and key-in-use record reads): the nine transcript
    reads close the list, each sent once, the sweeps on the env-prefixed collection groups (ruling T-R13); the purge
    deletes themselves are not run. MULTI_DRIVE_RFC §8, §10: the connection-scoped stores (records by state and wake,
    jobs, batches, window results) filter the connection; the user-scoped transcript store (heads, parts) and the
    key-in-use record read behind its sweep filter the user."""
    db = ChunkDb()
    repo = make_chunk_repo(db, mock_env_config)
    names = [name for name, _ in smoke.checks(repo, "smoke-user")]
    assert names[-len(TRANSCRIPT_CHECKS) - 1:] == ["inaccessible_records", *TRANSCRIPT_CHECKS]
    assert await smoke.run_smoke(repo, "smoke-user") == []
    record_reads = [ops for ops in db.cols[RECS].executed
                    if any(op[0] == "where" and op[1] in ("transcript_state", "transcript_wake_at", "transcript_key")
                           for op in ops)]
    assert len(record_reads) == _TRANSCRIPT_RECORD_QUERIES
    on_user, on_connection = ("where", "user_id", "==", "smoke-user"), ("where", "connection_id", "==", "smoke-conn")
    for ops in record_reads:
        key_in_use = any(op[:2] == ("where", "transcript_key") for op in ops)
        assert (on_user if key_in_use else on_connection) in ops
    assert sum(any(op[:2] == ("where", "transcript_key") for op in ops) for ops in record_reads) == 1
    for runs, scoped in ((db.cols[JOBS].executed, on_connection), (db.cols[BATCHES].executed, on_connection),
                         (db.cols[TRANSCRIPTS].executed, on_user), (db.groups[WINDOWS].executed, on_connection),
                         (db.groups[PARTS].executed, on_user)):
        (ops,) = runs
        assert scoped in ops
    for col in (*db.cols.values(), *db.groups.values()):
        for doc in col.docs.values():
            doc.set.assert_not_called()
            doc.update.assert_not_called()
            doc.delete.assert_not_called()
    assert db.batches == [] and not db.tx.method_calls
