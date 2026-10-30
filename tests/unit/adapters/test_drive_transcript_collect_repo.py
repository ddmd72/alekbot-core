"""Repository additions for transcript collection (plan Task 13), on both adapters: `transcript_key_in_use` (the orphan
sweep keeps a stored transcript a record still carries, §27.8 b, Δ122) and the window's `batch_resubmits` counter (the
one resubmit after a failed batch, §27.5) surviving the job document."""
from datetime import datetime, timezone

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import DriveIndexRecord
from src.domain.drive_transcript import JobWindow, TranscriptJob, WindowSpec

from tests.unit.adapters.firestore_drive_eval_fakes import RECS, EvalDb, eval_transactions, make_eval_repo
from tests.unit.adapters.firestore_drive_transcript_fakes import transcript_doc_fakes
from tests.unit.adapters.test_firestore_drive_index_v2_provisioning import _exempt, _required
from tests.unit.services.drive_index_fakes import U_SCOPE

LIVE_RECS = "development_drive_index_records"
T = datetime(2026, 10, 21, 9, 30, tzinfo=timezone.utc)


@pytest.fixture(params=["memory", "firestore"])
def adapter(request, mock_env_config):
    if request.param == "memory":
        yield InMemoryDriveIndexRepository(), None
        return
    db = EvalDb()
    with eval_transactions(), transcript_doc_fakes():
        yield make_eval_repo(db, mock_env_config), db


async def test_transcript_key_in_use_in_any_state_per_user(adapter):
    repo, _ = adapter
    await repo.upsert_records([
        DriveIndexRecord("u", "a", connection_id="u", transcript_state="current", transcript_key="k1"),
        DriveIndexRecord("u", "b", connection_id="u", transcript_state="ready", transcript_key="k2"),
        DriveIndexRecord("v", "c", connection_id="v", transcript_state="current", transcript_key="k3"),
    ])
    assert await repo.transcript_key_in_use("u", "k1") and await repo.transcript_key_in_use("u", "k2")
    assert not await repo.transcript_key_in_use("u", "k3") and not await repo.transcript_key_in_use("u", "nope")


async def test_the_firestore_read_is_equality_only(adapter):
    repo, db = adapter
    if db is None:
        pytest.skip("Firestore wire only")
    await repo.upsert_records([DriveIndexRecord("u", "a", connection_id="u", transcript_key="k1")])
    db.cols[RECS].executed.clear()
    await repo.transcript_key_in_use("u", "k1")
    (ops,) = db.cols[RECS].executed
    assert _required(ops) is None   # two equality filters, no ordering: single-field indexes serve it
    assert not {"user_id", "transcript_key"} & _exempt(LIVE_RECS)


async def test_batch_resubmits_roundtrips_on_the_job(adapter, mock_env_config):
    repo, _ = adapter
    job = TranscriptJob(user_id="u", connection_id="u", item_id="i1", content_key="k1", duration_s=600.0, windows=(
        JobWindow(WindowSpec(0, 0.0, 600.0), state="uploaded", file_ref="files/1", batch_resubmits=1,
                  files_expire_at=T),))
    await repo.save_transcript_job(job)
    assert (await repo.get_transcript_job(U_SCOPE, "i1")).windows[0].batch_resubmits == 1


async def test_refused_roundtrips_on_the_job(adapter):
    repo, _ = adapter
    job = TranscriptJob(user_id="u", connection_id="u", item_id="i2", content_key="k2", duration_s=600.0, windows=(
        JobWindow(WindowSpec(0, 0.0, 600.0), state="done", gap=True, refused=True),))
    await repo.save_transcript_job(job)
    assert (await repo.get_transcript_job(U_SCOPE, "i2")).windows[0].refused


async def test_batch_booked_at_roundtrips(adapter):
    from src.domain.drive_transcript import TranscriptBatch
    repo, _ = adapter
    batch = TranscriptBatch(name="batches/b9", user_id="u", connection_id="u", request_keys=("i:0",), submitted_at=T, created_at=T,
                            next_poll_at=T)
    await repo.create_batch(batch)
    assert (await repo.get_open_batches(U_SCOPE, T, 5))[0].booked_at is None
    await repo.save_batch(TranscriptBatch(**{**batch.__dict__, "booked_at": T}))
    assert (await repo.get_open_batches(U_SCOPE, T, 5))[0].booked_at == T
