"""Provisioning of the transcript documents (USER_DRIVE_INDEXING_RFC §27.3-§27.5, §27.8 b): the three collections are
env-prefixed; the subcollections hang off them; every new repository method keeps the `_store_errors` mapping; the one
query 7A sends (open batches) has the shape its composite index (`user_id + next_poll_at`, declared with the queues)
serves (`connection_id + next_poll_at` since MULTI_DRIVE_RFC §8). Index declarations themselves are asserted with the queries that need them."""
import inspect
from datetime import datetime, timezone

import pytest
from google.api_core import exceptions as gexc

from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository
from src.config.environment import EnvironmentConfig
from src.domain.drive_index import DriveIndexStoreUnavailable
from src.domain.drive_transcript import StoredTranscript, TranscriptBatch, TranscriptJob

from tests.unit.adapters.firestore_drive_eval_fakes import EvalDb, eval_transactions, make_eval_repo
from tests.unit.adapters.firestore_drive_transcript_fakes import transcript_doc_fakes
from tests.unit.adapters.test_firestore_drive_index_v2_provisioning import _required
from tests.unit.services.drive_index_fakes import U_SCOPE

NEW_METHODS = ("get_transcript_job", "save_transcript_job", "delete_transcript_job", "save_window_result",
               "get_window_results", "create_batch", "save_batch", "delete_batch", "get_open_batches",
               "get_transcript", "put_transcript", "touch_transcript", "delete_transcript")
NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)


@pytest.mark.parametrize("env,prefix", [("development", "development_"), ("production", ""), ("test", "test_")])
def test_collection_names_are_env_prefixed(env, prefix, monkeypatch):
    monkeypatch.setenv("APP_ENV", env)
    cfg = EnvironmentConfig()
    assert cfg.drive_transcript_jobs_collection == f"{prefix}drive_transcript_jobs"
    assert cfg.drive_transcript_batches_collection == f"{prefix}drive_transcript_batches"
    assert cfg.drive_transcripts_collection == f"{prefix}drive_transcripts"
    assert cfg.drive_transcript_windows_subcollection == f"{prefix}drive_transcript_windows"
    assert cfg.drive_transcript_parts_subcollection == f"{prefix}drive_transcript_parts"


@pytest.fixture
def run(mock_env_config):
    db = EvalDb()
    mock_env_config.drive_transcript_jobs_collection = "t_jobs"
    mock_env_config.drive_transcript_batches_collection = "t_batches"
    mock_env_config.drive_transcripts_collection = "t_store"
    mock_env_config.drive_transcript_windows_subcollection = "t_windows"
    mock_env_config.drive_transcript_parts_subcollection = "t_parts"
    with eval_transactions(), transcript_doc_fakes():
        yield db, make_eval_repo(db, mock_env_config)


async def test_documents_and_subcollections_live_under_the_prefixed_names(run):
    from src.domain.drive_transcript import Segment, StitchedTranscript, WindowTranscript
    db, repo = run
    await repo.save_transcript_job(TranscriptJob(user_id="u", connection_id="u", item_id="i", content_key="k"))
    await repo.save_window_result(U_SCOPE, "i", 3, WindowTranscript((), 1, True, ()))
    await repo.create_batch(TranscriptBatch(name="batches/x", user_id="u", connection_id="u"))
    await repo.put_transcript("u", StoredTranscript("k", StitchedTranscript((Segment(0.0, None, "a"),), (), 1, 1.0)))
    assert set(db.collection("t_jobs").docs) == {"u:i"} and set(db.collection("t_batches").docs) == {"u:batches%2Fx"}
    assert set(db.collection("t_jobs/u:i/t_windows").docs) == {"3"} and set(db.collection("t_store").docs) == {"u:k"}


async def test_open_batches_query_matches_its_index_shape(run):
    db, repo = run
    await repo.get_open_batches(U_SCOPE, NOW, 5)
    ops = db.collection("t_batches").executed[0]
    assert _required(ops) == ({("connection_id", "EQ")}, [("next_poll_at", "ASCENDING")])


def test_every_new_method_is_wrapped_by_store_errors():
    for name in NEW_METHODS:
        method = getattr(FirestoreDriveIndexRepository, name)
        assert inspect.iscoroutinefunction(method) and hasattr(method, "__wrapped__"), name


@pytest.mark.parametrize("exc", [gexc.DeadlineExceeded("d"), gexc.FailedPrecondition("missing index")],
                         ids=lambda e: type(e).__name__)
async def test_store_errors_become_the_domain_outage(run, exc):
    db, repo = run
    db.collection("t_batches").fail = exc
    with pytest.raises(DriveIndexStoreUnavailable):
        await repo.get_open_batches(U_SCOPE, NOW, 5)
