"""Final-review fix wave F1, the repositories (USER_DRIVE_INDEXING_RFC §27.5): the `submitting` batch document of T-R28
and the window counters of T-R29/T-R31 survive both adapters; one unreadable batch document never blocks the poll of the
others."""
from datetime import datetime, timedelta, timezone

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_transcript import JobWindow, TranscriptBatch, TranscriptJob, WindowSpec, batch_doc_id

from tests.unit.adapters.firestore_drive_eval_fakes import EvalDb, eval_transactions, make_eval_repo
from tests.unit.adapters.firestore_drive_transcript_fakes import transcript_doc_fakes
from tests.unit.services.drive_index_fakes import U_SCOPE

T = datetime(2026, 10, 21, 9, 30, tzinfo=timezone.utc)


@pytest.fixture(params=["memory", "firestore"])
def adapter(request, mock_env_config):
    if request.param == "memory":
        yield InMemoryDriveIndexRepository(), None, mock_env_config
        return
    db = EvalDb()
    with eval_transactions(), transcript_doc_fakes():
        yield make_eval_repo(db, mock_env_config), db, mock_env_config


async def test_a_submitting_batch_roundtrips_and_is_polled(adapter):
    repo, _, _ = adapter
    batch = TranscriptBatch(name="submitting/abc", user_id="u", request_keys=("i:0",), created_at=T,
                            reserved_usd=0.25, reserved_month="2026-10", state="submitting",
                            next_poll_at=T + timedelta(seconds=150), display_name="u:submitting/abc",
                            provider_name="batches/p1", lookups=2)
    assert await repo.create_batch(batch)
    (got,) = await repo.get_open_batches(U_SCOPE, T + timedelta(hours=1), 10)
    assert got == batch


async def test_new_window_counters_roundtrip(adapter):
    repo, _, _ = adapter
    job = TranscriptJob(user_id="u", connection_id="u", item_id="i1", content_key="k1", duration_s=600.0, windows=(
        JobWindow(WindowSpec(0, 0.0, 600.0), state="done", lost_purchases=2, accepted_short=True,
                  files_expire_at=T),))
    await repo.save_transcript_job(job)
    window = (await repo.get_transcript_job(U_SCOPE, "i1")).windows[0]
    assert (window.lost_purchases, window.accepted_short) == (2, True)


async def test_a_legacy_batch_document_reads_with_the_defaults(adapter):
    repo, db, env = adapter
    if db is None:
        pytest.skip("Firestore codec only")
    db.collection(env.drive_transcript_batches_collection).docs[batch_doc_id("u", "batches/old")] = {
        "user_id": "u", "name": "batches/old", "request_keys": ["i:0"], "state": "submitted",
        "next_poll_at": T, "reserved_usd": 0.0}
    (got,) = await repo.get_open_batches(U_SCOPE, T, 10)
    assert (got.name, got.display_name, got.provider_name, got.lookups) == ("batches/old", "", "", 0)


async def test_an_unreadable_batch_document_is_dropped_alone(adapter, caplog):
    repo, db, env = adapter
    if db is None:
        pytest.skip("Firestore codec only")
    await repo.create_batch(TranscriptBatch(name="batches/good", user_id="u", request_keys=("i:0",),
                                            next_poll_at=T))
    docs = db.collection(env.drive_transcript_batches_collection).docs
    docs[batch_doc_id("u", "batches/bad")] = {"user_id": "u", "name": "batches/bad", "next_poll_at": T,
                                              "reserved_usd": "not a number"}
    docs[batch_doc_id("u", "batches/bad2")] = {"user_id": "u", "next_poll_at": T}   # no name at all
    with caplog.at_level("ERROR"):
        got = await repo.get_open_batches(U_SCOPE, T, 10)
    assert [b.name for b in got] == ["batches/good"]
    assert sum("unreadable" in r.getMessage() for r in caplog.records if r.levelname == "ERROR") == 2
