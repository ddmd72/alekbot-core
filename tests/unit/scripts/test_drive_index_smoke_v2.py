"""drive_index_smoke.py runs every Search v2 read once (USER_DRIVE_INDEXING_RFC §25.2.5, §25.5.5, §25.8 rollout step 3):
the about, expiring, word-key and hint streams, the hint reads, the enrichment queue reads, the per-state counts, the
usage documents, the most disjunctions one about query may carry, and a kind family's kNN one member kind at a time.
Run on the evaluating Firestore client, never on the network; nothing is written."""
import importlib.util
from pathlib import Path

import pytest

from src.domain.drive_index_policy import SLICE2_CONTENT_KINDS
from src.domain.drive_search import KIND_FAMILIES

from tests.unit.adapters.firestore_drive_eval_fakes import (
    CHUNKS, HINTS, RECS, EvalDb, make_eval_repo,
)
from tests.unit.adapters.firestore_drive_transcript_fakes import transcript_doc_fakes

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure" / "drive_index_smoke.py"

V2_CHECKS = {
    "find_by_about_keys", "find_by_about_keys[kind]", "find_by_about_keys[dates]", "find_by_about_keys[30 keys+kind]",
    "find_expiring", "find_chunks_by_word_keys", "find_chunks_by_word_keys[cursor]", "get_chunk_keys",
    "find_hints_by_keys", "find_hints_by_keys[cursor]", "get_hint_keys", "find_nearest_hints",
    "find_nearest_hints[kind]", "get_hints", "get_hints_with_vectors", "hints_page", "enrich_queue",
    "enrich_wakeups", "keys_pending_page", "get_records_lean", "get_model_usage", "get_initial_build",
}


@pytest.fixture
def smoke():
    spec = importlib.util.spec_from_file_location("drive_index_smoke", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def run(mock_env_config):
    """The whole smoke list runs here, so the transcript reads (§27.3-§27.5, Task 7B) need their collection names and
    the evaluating client's `collection_group` (the two env-prefixed subcollection groups, ruling T-R13)."""
    db = EvalDb()
    mock_env_config.drive_transcript_jobs_collection = "eval_drive_transcript_jobs"
    mock_env_config.drive_transcript_batches_collection = "eval_drive_transcript_batches"
    mock_env_config.drive_transcripts_collection = "eval_drive_transcripts"
    mock_env_config.drive_transcript_windows_subcollection = "eval_drive_transcript_windows"
    mock_env_config.drive_transcript_parts_subcollection = "eval_drive_transcript_parts"
    with transcript_doc_fakes():
        yield db, make_eval_repo(db, mock_env_config)


def family():
    return sorted(max(KIND_FAMILIES.values(), key=len))


async def test_every_v2_read_is_a_named_check_and_runs_clean(smoke, run):
    db, repo = run
    names = [name for name, _ in smoke.checks(repo, "smoke-user")]
    assert V2_CHECKS <= set(names) and len(names) == len(set(names))
    assert sum(name.startswith("count_by_job_state[") for name in names) == 12 * (
        (len(SLICE2_CONTENT_KINDS) + 29) // 30)
    assert f"family kNN[{','.join(family())}]" in names
    assert await smoke.run_smoke(repo, "smoke-user") == []


async def test_the_v2_queries_reach_their_collections_and_nothing_is_written(smoke, run):
    db, repo = run
    await smoke.run_smoke(repo, "smoke-user")
    hint_queries = db.cols[HINTS].executed
    # 2 key pages, 2 nearest, the family's members, 2 generation reads, 1 page
    assert len(hint_queries) == 2 + 2 + len(family()) + 2 + 1
    word = [ops for ops in db.cols[CHUNKS].executed if any(op[1:3] == ("word_keys", "array_contains_any")
                                                           for op in ops if op[0] == "where")]
    assert len(word) == 2 and any(op[0] == "start_after" for op in word[1])
    widest = [op for ops in db.cols[RECS].executed for op in ops
              if op[0] == "where" and op[1:3] == ("about_keys", "array_contains_any")]
    assert max(len(op[3]) for op in widest) == 30
    family_knn = [ops for ops in db.cols[HINTS].executed if any(op[0] == "find_nearest" for op in ops)
                  and any(op[1] == "kind" for op in ops if op[0] == "where")]
    assert sorted(next(op[3] for op in ops if op[0] == "where" and op[1] == "kind") for ops in family_knn) == sorted(
        family() + ["pdf"])
    assert db.commits == [] and db.transactions == []
    assert all(not col.docs for col in db.cols.values())
