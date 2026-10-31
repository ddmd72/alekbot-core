"""I3 on the Firestore drive index adapter, over the evaluating fake: a stored document of another user under the same
connection id is never deleted by a delete issued under the caller's scope, and each refusal is logged at ERROR.
Technical ids only."""
import logging
from datetime import datetime, timezone

import pytest

from src.domain.connection_scope import ConnectionScope
from src.domain.drive_index import DriveSyncState
from src.domain.drive_transcript import TranscriptJob

from tests.unit.adapters.firestore_drive_eval_fakes import STATES, EvalDb, eval_transactions, make_eval_repo

A = ConnectionScope(user_id="usr-1", connection_id="conn-1")
OTHER = ConnectionScope(user_id="usr-2", connection_id="conn-1")   # the same connection id under another user
JOBS = "eval_drive_transcript_jobs"


@pytest.fixture
def db():
    return EvalDb()


@pytest.fixture
def repo(db, mock_env_config):
    mock_env_config.drive_transcript_jobs_collection = JOBS
    mock_env_config.drive_transcript_batches_collection = "eval_drive_transcript_batches"
    mock_env_config.drive_transcripts_collection = "eval_drive_transcripts"
    mock_env_config.drive_transcript_windows_subcollection = "windows"
    mock_env_config.drive_transcript_parts_subcollection = "parts"
    with eval_transactions():
        yield make_eval_repo(db, mock_env_config)


async def test_delete_sync_state_leaves_another_users_state_and_logs_error(repo, db, caplog):
    await repo.save_sync_state(DriveSyncState(user_id="usr-2", connection_id="conn-1", epoch=3))
    with caplog.at_level(logging.ERROR):
        await repo.delete_sync_state(A)
    assert set(db.collection(STATES).docs) == {"conn-1"}
    assert (await repo.get_sync_state(OTHER)).epoch == 3
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_delete_transcript_job_leaves_another_users_job_and_logs_error(repo, db, caplog):
    await repo.save_transcript_job(TranscriptJob(user_id="usr-2", connection_id="conn-1", item_id="item-1",
                                                 content_key="key-1", duration_s=600.0, windows=()))
    stored = dict(db.collection(JOBS).docs)
    assert len(stored) == 1
    with caplog.at_level(logging.ERROR):
        await repo.delete_transcript_job(A, "item-1")
    assert dict(db.collection(JOBS).docs) == stored
    assert (await repo.get_transcript_job(OTHER, "item-1")).content_key == "key-1"
    assert any(r.levelno == logging.ERROR for r in caplog.records)
