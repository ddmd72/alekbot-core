"""Transcript stage A, review fix round 1 (plan Task 12): a job with a window in a batch is collection's to settle (T-R20,
G33); a charged attempt backs off and is never charged twice in one step (T-R21); the month of a step's reservations, a
held reservation replaced on restart, a stub's duration on re-finalize (M1, M3, M4); and the M6 cases."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.drive_index import EXTRACTION_VERSION, RecordState
from src.domain.drive_index_params import MAX_ATTEMPTS, RETRY_BASE, TRANSCRIBE_REREAD_COOLDOWN
from src.domain.drive_transcript import JobWindow, MediaStatus, StoredTranscript, TranscriptJob, WindowSpec
from src.domain.drive_transcript_plan import window_estimate_usd
from src.domain.drive_transcript_render import TRANSCRIPT_BODY_VERSION
from src.domain.exceptions import LLMRateLimitError
from src.services.drive_transcript_stage import TRANSCRIBER_AGENT

from tests.unit.services.drive_transcript_fakes import cid, HANDLE, NOW, OLD, USER, Recording, record
from tests.unit.services.test_drive_transcript_stage_extract import MONTH, _add, _env, _get, _job, _step


def _resubmit_job(reserved_month=MONTH, now=NOW):
    window = JobWindow(spec=WindowSpec(0, 0.0, 600.0), state="uploaded", file_ref="files/x", request_key="a:0",
                       attempts=1, files_expire_at=now + timedelta(hours=40))
    return window, TranscriptJob(user_id=USER, connection_id=USER, item_id="a", content_key="hash-a", duration_s=600.0, windows=(window,),
                                 reserved_usd=0.05, reserved_month=reserved_month)


# -- T-R20: a job with a window in a batch is left for collection (G33) -------------------------------------------------

async def test_an_edited_waiting_file_leaves_its_running_batch_alone(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(600))
    await _step(env)
    ((_, b1),) = env.repo.batches.items()
    old_job = _job(env, "a")
    assert (await _get(env, "a")).transcript_state == "waiting"
    edited = replace(await _get(env, "a"), content_version="c2", content_identity=cid("hash-new"), modified_at=OLD)
    env.clock.advance(TRANSCRIBE_REREAD_COOLDOWN.total_seconds() + 60)
    await _add(env, edited, a=Recording(600))
    assert (await _get(env, "a")).transcript_state == "ready"
    summary = await _step(env)
    assert summary["transcript_waiting_for_settle"] == 1
    assert env.host.releases == [] and env.batch.deleted == []
    assert _job(env, "a") == old_job and list(env.repo.batches.values()) == [b1]
    assert len(env.runner.submits) == 1 and len(env.media.runs) == 1   # no b2, no second extraction


async def test_drop_jobs_leaves_a_job_with_a_window_in_a_batch(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(600))
    await _step(env)
    job = _job(env, "a")
    assert await env.stage.drop_jobs(HANDLE, env.host, ["a"]) == 0
    assert _job(env, "a") == job and env.host.releases == [] and env.batch.deleted == []


# -- T-R21: charged attempts back off ---------------------------------------------------------------------------------

async def test_a_charged_attempt_is_held_for_retry_and_never_charged_twice_in_a_step(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(5000, cut_after=1))
    await _step(env)
    assert (await _get(env, "a")).transcript_state == "working"
    env.drive.raise_for["a"] = asyncio.TimeoutError()
    summary = await _step(env)
    rec = await _get(env, "a")
    assert summary["transcript_attempts_charged"] == 1 and rec.transcript_attempts == 1
    assert (rec.transcript_state, rec.transcript_hold) == ("held", "retry")
    assert rec.transcript_wake_at == env.clock.now + RETRY_BASE
    del env.drive.raise_for["a"]
    await _step(env)
    assert (await _get(env, "a")).transcript_attempts == 1 and len(env.media.runs) == 1   # still backing off
    env.clock.advance(RETRY_BASE.total_seconds() + 1)
    summary = await _step(env)
    assert summary["transcript_woken"] == 1 and summary["transcript_started"] == 1
    assert [r["indexes"] for r in env.media.runs][-1] == [1, 2]   # the uploaded window survived the charge


async def test_the_retry_backoff_doubles_and_the_last_attempt_fails(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(600))
    env.drive.raise_for["a"] = asyncio.TimeoutError()
    await _step(env)
    first = await _get(env, "a")
    assert first.transcript_wake_at == env.clock.now + RETRY_BASE
    env.clock.advance(RETRY_BASE.total_seconds() + 1)
    await _step(env)
    second = await _get(env, "a")
    assert second.transcript_attempts == 2 and second.transcript_wake_at == env.clock.now + 2 * RETRY_BASE
    env.clock.advance(2 * RETRY_BASE.total_seconds() + 1)
    await _step(env)
    last = await _get(env, "a")
    assert last.transcript_attempts == MAX_ATTEMPTS and last.transcript_state == "failed"
    assert last.transcript_wake_at is None and last.transcript_stub == ""


# -- M1, M3, M4 -------------------------------------------------------------------------------------------------------

async def test_a_step_straddling_midnight_books_its_reservations_in_its_own_month(tmp_path):
    env = _env(tmp_path)
    env.clock.now = datetime(2026, 10, 31, 23, 59, 45, tzinfo=timezone.utc)
    window, job = _resubmit_job(now=env.clock.now)
    env.repo.transcript_jobs[(USER, "a")] = job
    await _add(env, replace(record("a"), transcript_state="working", transcript_key="hash-a"),
               record("b"), b=Recording(600))
    await _step(env)
    assert env.clock.now.month == 11   # the extraction of "b" passed midnight before the submission
    batch = next(iter(env.repo.batches.values()))
    assert batch.reserved_month == "2026-10" and env.host.reserves[-1] == (pytest.approx(window_estimate_usd(window)),
                                                                           "2026-10")


async def test_a_restart_gives_back_the_reservation_the_old_job_held(tmp_path):
    env = _env(tmp_path)
    env.repo.transcript_jobs[(USER, "a")] = TranscriptJob(user_id=USER, connection_id=USER, item_id="a", content_key="hash-a",
                                                          reserved_usd=0.05, reserved_month=MONTH)
    await _add(env, record("a"), a=Recording(600))
    await _step(env)
    assert env.host.releases == [(0.05, MONTH)]
    assert _job(env, "a").reserved_usd == pytest.approx(env.host.reserves[0][0])


async def test_a_refinalized_too_long_stub_keeps_its_duration(tmp_path):
    env = _env(tmp_path)
    await env.repo.put_transcript(USER, StoredTranscript("hash-a", None, stub="too_long", last_used_at=OLD))
    stubbed = replace(record("a"), state=RecordState.METADATA_ONLY, transcript_state="current", transcript_key="hash-a",
                      transcript_stub="too_long", transcript_seconds=20_000, transcript_version=1,
                      transcript_body_version=TRANSCRIPT_BODY_VERSION, extraction_version=EXTRACTION_VERSION - 1)
    await _add(env, stubbed)
    await _step(env)
    rec = await _get(env, "a")
    assert rec.transcript_state == "current" and rec.transcript_stub == "too_long" and rec.transcript_seconds == 20_000


# -- M6 ---------------------------------------------------------------------------------------------------------------

async def test_a_continuation_run_cut_empty_is_not_charged(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(12_000, known=False))

    def second_run_cut(item_id):
        if len(env.media.runs) == 2:
            env.media.recordings[item_id].status = MediaStatus.TIMEOUT

    env.media.on_extract = second_run_cut
    await _step(env)
    rec = await _get(env, "a")
    assert len(env.media.runs) == 2 and env.media.runs[1]["wall_s"] < 240
    assert rec.transcript_attempts == 0 and rec.transcript_state == "working"
    assert [w.spec.index for w in _job(env, "a").windows] == [0, 1, 2]


async def test_a_submit_outage_gives_the_resubmit_reservation_back_once(tmp_path):
    env = _env(tmp_path)
    window, job = _resubmit_job()
    env.repo.transcript_jobs[(USER, "a")] = job
    await _add(env, replace(record("a"), transcript_state="working", transcript_key="hash-a"))
    # T-R28: only a definite refusal (here a 429) proves nothing was bought and gives the money back; an ambiguous
    # error keeps it on the `submitting` document (test_drive_transcript_stage_final_f1.py, the ambiguous cases).
    env.runner.raise_next = LLMRateLimitError("429", http_status=429)
    await _step(env)
    estimate = window_estimate_usd(window)
    assert env.host.reserves == [(pytest.approx(estimate), MONTH)]
    assert env.host.releases == [(pytest.approx(estimate), MONTH)]
    assert env.repo.batches == {} and env.host.outages == [TRANSCRIBER_AGENT]
    assert _job(env, "a").reserved_usd == 0.05 and [w.state for w in _job(env, "a").windows] == ["uploaded"]
