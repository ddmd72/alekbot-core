"""The transcript stage, part A: extraction, upload and submission (USER_DRIVE_INDEXING_RFC §27.3-§27.6, §27.10; plan
Task 12; G12, G18, G20, G31, G35, G39; rulings T-R8, T-R11, T-R15, T-R16). Synthetic recordings only."""
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_media_facts import DriveMediaFacts
from src.domain.drive_index import EXTRACTION_VERSION, Reason, RecordState
from src.domain.drive_index_params import (
    BATCH_POLL_FIRST_S, MAX_ATTEMPTS, RETRY_BASE, TRANSCRIBE_EXTRACT_WALL_S, TRANSCRIBE_MAX_SECONDS,
    TRANSCRIBE_REREAD_COOLDOWN,
)
from src.domain.drive_transcript import (
    JobWindow, MediaStatus, MediaUnreadable, Segment, StitchedTranscript, StoredTranscript, TranscriptJob, WindowSpec,
)
from src.domain.drive_transcript_plan import file_estimate_usd, mark_current, window_estimate_usd
from src.domain.drive_transcript_render import TRANSCRIPT_BODY_VERSION, render_body
from src.domain.exceptions import LLMClientError, LLMUnavailableError
from src.services.drive_transcript_stage import EXTRACTED_BY, TRANSCRIBER_AGENT, DriveTranscriptStage

from tests.unit.services.drive_transcript_fakes import (
    cid, HANDLE, SCOPE, NOW, OLD, URL_PREFIX, USER, Clock, FakeBatch, FakeDrive, FakeHost, FakeMedia, FakeRunner, Recording, record,
)

MONTH = "2026-10"


def _env(tmp_path, *, cap=20.0, seconds_per_window=30.0):
    clock = Clock()
    repo = InMemoryDriveIndexRepository()
    media = FakeMedia(clock, seconds_per_window=seconds_per_window)
    batch = FakeBatch(clock)
    runner = FakeRunner()
    drive = FakeDrive()
    host = FakeHost(repo, clock)
    stage = DriveTranscriptStage(repo, drive, media, batch, runner, clock=clock, monotonic=clock.monotonic,
                                 monthly_cap_usd=cap, temp_root=str(tmp_path))
    return SimpleNamespace(clock=clock, repo=repo, media=media, batch=batch, runner=runner, drive=drive, host=host,
                           stage=stage)


async def _add(env, *recs, **recordings):
    await env.host.commit(list(recs), {})
    for item_id, rec in recordings.items():
        env.media.recordings[item_id] = rec


async def _step(env, stop_in=600.0):
    return await env.stage.extract_step(HANDLE, env.host, stop_at=env.clock.mono + stop_in)


async def _get(env, item_id):
    return (await env.repo.get_records(SCOPE, [item_id]))[item_id]


def _job(env, item_id):
    return env.repo.transcript_jobs.get((USER, item_id))


def _reserved(env):
    return max(0.0, env.repo.usage.get((USER, MONTH)).transcribe_reserved_usd) if (USER, MONTH) in env.repo.usage else 0.0


def _past_wake(env, rec):
    """T-R21: a charged attempt is held for retry until its wake-up; move the clock just past it."""
    env.clock.advance((rec.transcript_wake_at - env.clock.now).total_seconds() + 1)


def _stitched(*texts, duration=120.0, speakers=1, languages=("uk",)):
    return StitchedTranscript(tuple(Segment(60.0 * i, None, t) for i, t in enumerate(texts)), languages, speakers,
                              duration)


# -- the store path (Δ112, G18, G31) ----------------------------------------------------------------------------------

async def test_cache_hit_finalizes_with_no_ffmpeg_and_no_model(tmp_path):
    env = _env(tmp_path)
    transcript = _stitched("hello there", "and again")
    await env.repo.put_transcript(USER, StoredTranscript("hash-a", transcript, last_used_at=OLD))
    await _add(env, record("a"))
    summary = await _step(env)
    assert env.drive.calls == [] and env.media.probes == [] and env.media.runs == []
    assert env.batch.uploads == [] and env.runner.submits == [] and env.host.reserves == []
    assert env.host.indexed == [("a", render_body(transcript), EXTRACTED_BY)]
    rec = await _get(env, "a")
    assert rec.transcript_state == "current" and rec.transcript_stub == "" and rec.transcript_seconds == 120
    assert rec.transcript_languages == ("uk",) and rec.transcript_body_version == TRANSCRIPT_BODY_VERSION
    assert env.repo.transcripts[(USER, "hash-a")].last_used_at == NOW
    assert summary["transcript_store_hits"] == 1


@pytest.mark.parametrize("stale", ["body", "extraction"])
async def test_body_or_extraction_version_mismatch_refinalizes_without_model(tmp_path, stale):
    env = _env(tmp_path)
    transcript = _stitched("one", "two")
    await env.repo.put_transcript(USER, StoredTranscript("hash-a", transcript, last_used_at=OLD))
    done = replace(record("a"), state=RecordState.INDEXED, indexed_generation=1, index_generation=1,
                   indexed_version="c1", transcript_state="current", transcript_key="hash-a", transcript_version=1,
                   transcript_body_version=0 if stale == "body" else TRANSCRIPT_BODY_VERSION,
                   extraction_version=EXTRACTION_VERSION - 1 if stale == "extraction" else EXTRACTION_VERSION,
                   transcript_seconds=120)
    await _add(env, done)
    assert (await _get(env, "a")).transcript_state == "ready"   # the plan re-queues it (G18, G31)
    await _step(env)
    assert env.media.probes == [] and env.media.runs == [] and env.runner.submits == []
    assert [i[0] for i in env.host.indexed] == ["a"]
    rec = await _get(env, "a")
    assert rec.transcript_state == "current" and rec.transcript_body_version == TRANSCRIPT_BODY_VERSION
    assert rec.extraction_version == EXTRACTION_VERSION and rec.indexed_generation == 2


async def test_an_empty_stored_transcript_is_a_no_speech_stub(tmp_path):
    env = _env(tmp_path)
    await env.repo.put_transcript(USER, StoredTranscript("hash-a", _stitched("  ", ""), last_used_at=OLD))
    await _add(env, record("a"))
    await _step(env)
    rec = await _get(env, "a")
    assert env.host.indexed == []   # G20: never a plain no_text demotion
    assert rec.transcript_state == "current" and rec.transcript_stub == "no_speech"
    assert (rec.state, rec.reason) == (RecordState.METADATA_ONLY, Reason.NO_TEXT)


async def test_a_dropped_index_run_leaves_the_record_for_the_next_step(tmp_path):
    env = _env(tmp_path)
    await env.repo.put_transcript(USER, StoredTranscript("hash-a", _stitched("x"), last_used_at=OLD))
    await _add(env, record("a"))
    env.host.drop_index_result = True
    summary = await _step(env)
    assert summary["transcript_finalize_dropped"] == 1 and (await _get(env, "a")).transcript_state == "ready"


# -- copies (G12) -----------------------------------------------------------------------------------------------------

async def test_copy_shares_one_transcription(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a", content_hash="shared", day="2026-10-02"), record("b", content_hash="shared"),
               a=Recording(600), b=Recording(600))
    summary = await _step(env)
    assert [r["item"] for r in env.media.runs] == ["a"]
    assert [[i.key for i in s] for s in env.runner.submits] == [["a:0"]]
    assert (await _get(env, "a")).transcript_state == "waiting"
    assert (await _get(env, "b")).transcript_state == "waiting" and _job(env, "b") is None
    assert summary["transcript_copies_waiting"] == 1
    # collection (Task 13) lands the transcript of "a"
    await env.repo.put_transcript(USER, StoredTranscript("shared", _stitched("words"), last_used_at=env.clock.now))
    a = await _get(env, "a")
    await env.host.commit([mark_current(a, env.clock.now, seconds=600, speakers=1, languages=("uk",))], {"a": a})
    await _step(env)
    assert [r["item"] for r in env.media.runs] == ["a"] and len(env.runner.submits) == 1
    assert (await _get(env, "a")).transcript_state == "current"
    assert (await _get(env, "b")).transcript_state == "current"


async def test_a_copy_whose_other_file_is_gone_starts_on_its_own(tmp_path):
    env = _env(tmp_path)
    b = replace(record("b", content_hash="shared"), transcript_state="waiting", transcript_key="shared")
    await _add(env, b, b=Recording(600))
    assert (await _get(env, "b")).transcript_state == "waiting"
    summary = await _step(env)
    assert summary["transcript_copies_released"] == 1
    assert [r["item"] for r in env.media.runs] == ["b"] and (await _get(env, "b")).transcript_state == "waiting"
    assert _job(env, "b") is not None   # its own job now, in a batch


# -- URL and probe ------------------------------------------------------------------------------------------------------

async def test_fresh_download_url_per_step(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(5000, cut_after=1))
    await _step(env)
    await _step(env)
    assert env.drive.calls == ["a", "a"]
    urls = [r["url"] for r in env.media.runs]
    assert len(urls) == 2 and urls[0] != urls[1]
    assert env.media.probes[0] == urls[0] and env.media.probes[1] == urls[1]


async def test_url_never_persisted(tmp_path, caplog):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(5000))
    with caplog.at_level(logging.DEBUG):
        await _step(env)
    assert env.runner.submits
    stored = repr(env.repo.records) + repr(env.repo.transcript_jobs) + repr(env.repo.batches) + repr(env.repo.transcripts)
    for secret in ("graph.invalid", "files.invalid", "token=secret", "https://"):
        assert secret not in stored
        assert secret not in caplog.text
    assert all(not i.file_name.startswith("http") for i in env.runner.submits[0])


async def test_probe_duration_beats_facet(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a", kind="video", media=DriveMediaFacts(video_duration_ms=10_000)), a=Recording(3600))
    await _step(env)
    job = _job(env, "a")
    assert job.duration_s == 3600 and [w.spec.index for w in job.windows] == [0, 1, 2]
    assert env.host.reserves == [(pytest.approx(file_estimate_usd(3600)), MONTH)]
    assert (await _get(env, "a")).transcript_seconds == 3600


async def test_too_long_stub_no_model(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(TRANSCRIBE_MAX_SECONDS + 600))
    await _step(env)
    rec = await _get(env, "a")
    assert env.media.runs == [] and env.batch.uploads == [] and env.runner.submits == [] and env.host.reserves == []
    assert rec.transcript_state == "current" and rec.transcript_stub == "too_long"
    assert (rec.state, rec.reason) == (RecordState.METADATA_ONLY, Reason.TOO_LARGE)
    stored = env.repo.transcripts[(USER, "hash-a")]
    assert stored.stub == "too_long" and stored.transcript is None and stored.last_used_at == NOW


async def test_no_audio_stub_no_model(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(600, has_audio=False))
    await _step(env)
    rec = await _get(env, "a")
    assert env.media.runs == [] and env.runner.submits == [] and env.host.reserves == []
    assert rec.transcript_stub == "no_audio" and (rec.state, rec.reason) == (RecordState.METADATA_ONLY, Reason.NO_TEXT)
    assert env.repo.transcripts[(USER, "hash-a")].last_used_at is not None


async def test_a_probe_stub_collects_the_previous_readable_generation(tmp_path):
    env = _env(tmp_path)
    old = replace(record("a"), state=RecordState.INDEXED, indexed_generation=1, index_generation=1,
                  indexed_version="c0", content_version="c1")
    await _add(env, old, a=Recording(600, has_audio=False))
    await _step(env)
    assert "a" in env.host.collected and (await _get(env, "a")).indexed_generation is None


async def test_unreadable_file_fails_at_max_attempts_without_stub(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(600, probe_error=MediaUnreadable("bad header")))
    for attempt in range(1, MAX_ATTEMPTS + 1):
        await _step(env)
        rec = await _get(env, "a")
        assert rec.transcript_attempts == attempt and rec.transcript_stub == ""
        if attempt < MAX_ATTEMPTS:   # T-R21: held for retry, the backoff doubling, until its wake-up
            assert (rec.transcript_state, rec.transcript_hold) == ("held", "retry")
            assert rec.transcript_wake_at == env.clock.now + RETRY_BASE * 2 ** (attempt - 1)
            _past_wake(env, rec)
    assert rec.transcript_state == "failed" and rec.transcript_wake_at is None
    assert env.media.runs == [] and env.host.reserves == []


async def test_an_unreadable_extraction_releases_the_reservation_and_keeps_counting(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(600, extract_error=MediaUnreadable("corrupt")))
    await _step(env)
    rec = await _get(env, "a")
    assert rec.transcript_attempts == 1 and (rec.transcript_state, rec.transcript_hold) == ("held", "retry")
    assert rec.transcript_wake_at == env.clock.now + RETRY_BASE   # T-R21: held for retry, not ready at once
    assert sum(u for u, _ in env.host.releases) == pytest.approx(sum(u for u, _ in env.host.reserves)) and _reserved(env) == 0
    for _ in range(MAX_ATTEMPTS - 1):
        _past_wake(env, rec)
        await _step(env)
        rec = await _get(env, "a")
    assert rec.transcript_state == "failed" and rec.transcript_attempts == MAX_ATTEMPTS and _job(env, "a") is None
    assert sum(u for u, _ in env.host.releases) == pytest.approx(sum(u for u, _ in env.host.reserves)) and _reserved(env) == 0


# -- money (Δ106, G39) ----------------------------------------------------------------------------------------------

async def test_cap_parks_whole_file(tmp_path):
    env = _env(tmp_path, cap=file_estimate_usd(3600) * 0.9)
    await _add(env, record("a"), a=Recording(3600))
    await _step(env)
    rec = await _get(env, "a")
    assert rec.transcript_state == "parked" and rec.transcript_wake_at == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert env.media.runs == [] and env.host.reserves == [] and _job(env, "a") is None


async def test_reservation_written_before_first_extraction(tmp_path):
    env = _env(tmp_path)
    seen = []

    def check(item_id):
        seen.append((_reserved(env), _job(env, item_id).reserved_usd, env.repo.records[(USER, item_id)].transcript_state))

    env.media.on_extract = check
    await _add(env, record("a"), a=Recording(600))
    await _step(env)
    estimate = file_estimate_usd(600)
    assert seen == [(pytest.approx(estimate), pytest.approx(estimate), "working")]


async def test_two_files_never_both_pass_one_cap(tmp_path):
    one = file_estimate_usd(3600)
    env = _env(tmp_path, cap=one * 1.5)
    await _add(env, record("a", day="2026-10-02"), record("b"), a=Recording(3600, cut_after=1),
               b=Recording(3600, cut_after=1))
    await _step(env)
    assert (await _get(env, "a")).transcript_state == "working"
    assert (await _get(env, "b")).transcript_state == "parked"
    await _add(env, record("c", day="2026-10-03"), c=Recording(3600))   # a later step reads the stored reservation
    await _step(env)
    assert (await _get(env, "c")).transcript_state == "parked"
    assert [r["item"] for r in env.media.runs] == ["a", "a"]


async def test_one_batch_per_user_per_submission(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a", day="2026-10-02"), record("b"), a=Recording(600), b=Recording(3000))
    summary = await _step(env)
    assert len(env.runner.submits) == 1
    assert sorted(i.key for i in env.runner.submits[0]) == ["a:0", "b:0", "b:1"]
    ((_, batch),) = env.repo.batches.items()
    assert batch.user_id == USER and sorted(batch.request_keys) == ["a:0", "b:0", "b:1"]
    assert batch.next_poll_at == batch.submitted_at + timedelta(seconds=BATCH_POLL_FIRST_S)
    assert batch.reserved_usd == 0.0 and batch.state == "submitted"
    for item_id in ("a", "b"):
        assert (await _get(env, item_id)).transcript_state == "waiting"
        job = _job(env, item_id)
        assert all(w.state == "submitted" and w.batch_name == batch.name for w in job.windows)
        assert [w.request_key for w in job.windows] == [f"{item_id}:{w.spec.index}" for w in job.windows]
    assert summary["windows_submitted"] == 3 and summary["transcript_batches"] == 1


async def test_a_woken_mid_job_park_resubmits_through_its_job(tmp_path):
    env = _env(tmp_path)
    window = JobWindow(spec=WindowSpec(0, 0.0, 600.0), state="uploaded", file_ref="files/x", request_key="a:0",
                       attempts=1, files_expire_at=NOW + timedelta(hours=40))
    env.repo.transcript_jobs[(USER, "a")] = TranscriptJob(user_id=USER, connection_id=USER, item_id="a", content_key="hash-a",
                                                          duration_s=600.0, windows=(window,), reserved_usd=0.05,
                                                          reserved_month="2026-09")
    parked = replace(record("a"), transcript_state="parked", transcript_hold="resubmit", transcript_key="hash-a",
                     transcript_hold_version="hash-a", transcript_wake_at=NOW - timedelta(minutes=1))
    await _add(env, parked)
    assert (await _get(env, "a")).transcript_state == "working"   # the plan wakes it as working, never ready
    await _step(env)
    assert env.media.runs == [] and [[i.key for i in s] for s in env.runner.submits] == [["a:0"]]
    ((_, batch),) = env.repo.batches.items()
    assert batch.reserved_usd == pytest.approx(window_estimate_usd(window)) and batch.reserved_month == MONTH
    assert env.host.reserves == [(pytest.approx(window_estimate_usd(window)), MONTH)]


async def test_a_resubmit_that_does_not_fit_parks_the_file_mid_job(tmp_path):
    env = _env(tmp_path, cap=0.0001)
    window = JobWindow(spec=WindowSpec(0, 0.0, 600.0), state="uploaded", file_ref="files/x", request_key="a:0",
                       attempts=1, files_expire_at=NOW + timedelta(hours=40))
    env.repo.transcript_jobs[(USER, "a")] = TranscriptJob(user_id=USER, connection_id=USER, item_id="a", content_key="hash-a",
                                                          duration_s=600.0, windows=(window,), reserved_usd=0.05,
                                                          reserved_month=MONTH)
    await _add(env, replace(record("a"), transcript_state="working", transcript_key="hash-a"))
    await _step(env)
    rec = await _get(env, "a")
    assert env.runner.submits == [] and env.host.reserves == []
    assert rec.transcript_state == "parked" and rec.transcript_hold == "resubmit"
    assert _job(env, "a").reserved_usd == 0.05   # the file's own reservation stays held


# -- extraction: walls, passes, attempts (T-R15, T-R16) ----------------------------------------------------------------

async def test_webm_without_a_duration_reserves_the_worst_case_and_resettles_at_eof(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(2500, known=False))
    summary = await _step(env)
    rec = await _get(env, "a")
    assert rec.transcript_attempts == 0 and rec.transcript_state == "waiting" and rec.transcript_seconds == 2500
    assert env.host.reserves[0] == (pytest.approx(file_estimate_usd(TRANSCRIBE_MAX_SECONDS)), MONTH)
    assert env.media.runs[0]["duration_known"] is False and env.media.runs[0]["indexes"] == [0, 1, 2]
    job = _job(env, "a")
    assert job.duration_s == 2500 and job.reserved_usd == pytest.approx(file_estimate_usd(2500))
    assert _reserved(env) == pytest.approx(file_estimate_usd(2500))
    assert [i.key for i in env.runner.submits[0]] == ["a:0", "a:1"]
    assert summary["transcript_resettled"] == 1


async def test_a_long_webm_is_cut_in_passes_and_resumed(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(12_000, known=False))
    await _step(env)
    runs = env.media.runs
    assert [r["indexes"] for r in runs] == [[0, 1, 2], [3, 4, 5]]
    assert runs[0]["wall_s"] == TRANSCRIBE_EXTRACT_WALL_S and runs[1]["wall_s"] == TRANSCRIBE_EXTRACT_WALL_S - 90
    assert env.runner.submits == [] and (await _get(env, "a")).transcript_state == "working"
    await _step(env)
    assert runs[2]["indexes"] == [6, 7, 8] and len(env.runner.submits) == 1
    assert _job(env, "a").duration_s == 12_000


async def test_a_webm_past_the_bound_is_too_long_and_gives_everything_back(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(TRANSCRIBE_MAX_SECONDS + 3000, known=False))
    await _step(env)
    await _step(env)
    rec = await _get(env, "a")
    assert rec.transcript_stub == "too_long" and rec.transcript_state == "current"
    assert _job(env, "a") is None and _reserved(env) == pytest.approx(0.0)
    assert sorted(env.batch.deleted) == sorted(f"files/f{n}" for n in range(1, 7))
    assert env.runner.submits == []


async def test_known_duration_eof_resettles_the_plan_and_the_reservation(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(2500, header_duration=4000))
    await _step(env)
    job = _job(env, "a")
    assert env.media.runs[0]["indexes"] == [0, 1, 2]
    assert job.duration_s == 2500 and [w.spec.index for w in job.windows] == [0, 1]
    assert job.windows[1].spec.duration_s == pytest.approx(760)
    assert _reserved(env) == pytest.approx(file_estimate_usd(2500))
    assert (await _get(env, "a")).transcript_seconds == 2500


@pytest.mark.parametrize("status", [MediaStatus.TIMEOUT, MediaStatus.MEMORY])
async def test_a_run_killed_before_its_first_window_is_charged(tmp_path, status):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(600, status=status))
    await _step(env)
    rec = await _get(env, "a")
    assert rec.transcript_attempts == 1 and env.batch.uploads == []
    assert (rec.transcript_state, rec.transcript_hold) == ("held", "retry")   # T-R21: backs off until its wake-up
    assert rec.transcript_wake_at == env.clock.now + RETRY_BASE
    assert _reserved(env) == pytest.approx(0.0)


async def test_a_wall_cut_after_a_window_advances_without_a_charge(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(5000, cut_after=1))
    await _step(env)
    rec = await _get(env, "a")
    assert rec.transcript_attempts == 0 and rec.transcript_state == "working"
    await _step(env)
    assert [r["indexes"] for r in env.media.runs] == [[0, 1, 2], [1, 2]]


async def test_resume_after_killed_tick_skips_uploaded_windows(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(5000))
    env.batch.fail_upload = [None, RuntimeError("the instance died")]
    with pytest.raises(RuntimeError):
        await _step(env)
    assert [w.spec.index for w in _job(env, "a").windows] == [0]
    await _step(env)
    assert [r["indexes"] for r in env.media.runs] == [[0, 1, 2], [1, 2]]
    assert sorted(i.key for i in env.runner.submits[0]) == ["a:0", "a:1", "a:2"]


async def test_wall_bounds_one_step(tmp_path):
    env = _env(tmp_path, seconds_per_window=100.0)
    await _add(env, record("a", day="2026-10-02"), record("b"), a=Recording(TRANSCRIBE_MAX_SECONDS),
               b=Recording(600))
    await _step(env, stop_in=10.0)
    (run,) = env.media.runs
    assert run["item"] == "a" and run["wall_s"] == TRANSCRIBE_EXTRACT_WALL_S
    assert len(_job(env, "a").windows) == 2 and (await _get(env, "a")).transcript_attempts == 0
    assert not any(p.startswith(f"{URL_PREFIX}b/") for p in env.media.probes)   # b waits for the next step
    assert (await _get(env, "b")).transcript_state == "ready"


# -- crash exits and job documents -------------------------------------------------------------------------------------

async def test_a_working_record_without_a_job_starts_again(tmp_path):
    env = _env(tmp_path)
    await _add(env, replace(record("a"), transcript_state="working", transcript_key="hash-a"), a=Recording(600))
    summary = await _step(env)
    assert summary["transcript_restarted"] == 1 and summary["transcript_started"] == 1
    assert (await _get(env, "a")).transcript_state == "waiting"


async def test_an_unreadable_job_document_is_dropped_and_charged(tmp_path, monkeypatch):
    env = _env(tmp_path)
    await _add(env, replace(record("a"), transcript_state="working", transcript_key="hash-a"), a=Recording(600))
    env.repo.transcript_jobs[(USER, "a")] = TranscriptJob(user_id=USER, connection_id=USER, item_id="a", content_key="hash-a")
    real = env.repo.get_transcript_job

    async def corrupt(scope, item_id):
        if item_id == "a" and (scope.connection_id, item_id) in env.repo.transcript_jobs:
            raise ValueError("unknown window state 'planned'")
        return await real(scope, item_id)

    monkeypatch.setattr(env.repo, "get_transcript_job", corrupt)
    summary = await _step(env)   # never fails the tick
    assert summary["transcript_jobs_dropped"] == 1
    assert (await _get(env, "a")).transcript_attempts == 1


async def test_a_new_content_version_drops_the_old_job_and_releases_it_once(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(5000, cut_after=1))
    await _step(env)
    held = _job(env, "a").reserved_usd
    edited = replace(await _get(env, "a"), content_version="c2", content_identity=cid("hash-new"), modified_at=OLD)
    env.clock.advance(TRANSCRIBE_REREAD_COOLDOWN.total_seconds() + 60)   # past the edit cooldown (T-R9)
    await _add(env, edited, a=Recording(600))
    await _step(env)
    assert env.host.releases[0] == (pytest.approx(held), MONTH)
    assert env.batch.deleted == ["files/f1"]
    assert _job(env, "a").content_key == "hash-new"


async def test_drop_jobs_releases_and_deletes(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(5000, cut_after=1))
    await _step(env)
    held = _job(env, "a").reserved_usd
    assert await env.stage.drop_jobs(HANDLE, env.host, ["a", "missing"]) == 1
    assert _job(env, "a") is None and env.host.releases == [(pytest.approx(held), MONTH)]
    assert env.batch.deleted == ["files/f1"] and _reserved(env) == pytest.approx(0.0)


# -- outages (§25.4.6) ------------------------------------------------------------------------------------------------

async def test_outage_stops_extraction_only(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(600))
    env.host.backed_off.add(TRANSCRIBER_AGENT)
    commits = len(env.host.commits)
    assert await _step(env) == {"transcript_backed_off": 1}
    assert env.drive.calls == [] and env.media.probes == [] and len(env.host.commits) == commits
    env.host.backed_off.clear()
    env.batch.fail_upload = [LLMUnavailableError("down")]
    summary = await _step(env)
    rec = await _get(env, "a")
    assert env.host.outages == [TRANSCRIBER_AGENT] and summary["transcript_outages"] == 1
    assert rec.transcript_state == "working" and rec.transcript_attempts == 0 and env.runner.submits == []
    env.host.backed_off.clear()
    await _step(env)
    assert len(env.runner.submits) == 1


async def test_a_submit_outage_keeps_the_windows_in_a_submitting_batch(tmp_path):
    # T-R28: an outage on the submit call (503-class, outcome unknown) may have bought the batch, so the windows stay
    # marked in the `submitting` document written before the call and the poll looks it up; never `uploaded` again
    # (that would buy them twice). The record is still `working`: the move to `waiting` waits for the provider name.
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(600))
    env.runner.raise_next = LLMUnavailableError("down")
    await _step(env)
    ((_, pending),) = env.repo.batches.items()
    assert env.host.outages == [TRANSCRIBER_AGENT] and pending.state == "submitting" and not pending.provider_name
    assert pending.request_keys == ("a:0",) and pending.reserved_usd == 0.0    # a first purchase: the file's money
    assert [(w.state, w.batch_name) for w in _job(env, "a").windows] == [("submitted", pending.name)]
    assert env.host.releases == []
    assert (await _get(env, "a")).transcript_state == "working"


async def test_an_upload_the_provider_refuses_is_charged(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(600))
    env.batch.fail_upload = [LLMClientError("bad request", http_status=400)]
    await _step(env)
    assert (await _get(env, "a")).transcript_attempts == 1 and env.host.outages == []


# -- the plan's holds (Task 4) -----------------------------------------------------------------------------------------

async def test_edit_cooldown_holds(tmp_path):
    env = _env(tmp_path)
    bought = replace(record("a", content_hash="hash-old"), transcript_state="current", transcript_key="hash-old",
                     transcript_version=1, transcript_body_version=TRANSCRIPT_BODY_VERSION,
                     model_read_at=NOW - timedelta(hours=1))
    edited = replace(bought, content_version="c2", content_identity=cid("hash-new"))
    await _add(env, edited, a=Recording(600))
    rec = await _get(env, "a")
    assert rec.transcript_state == "held" and rec.transcript_hold == "cooldown"
    await _step(env)
    assert env.drive.calls == [] and env.media.probes == []


async def test_a_working_record_whose_windows_are_all_submitted_is_repaired_to_waiting(tmp_path):
    env = _env(tmp_path)
    window = JobWindow(spec=WindowSpec(0, 0.0, 600.0), state="submitted", file_ref="files/x", request_key="a:0",
                       batch_name="batches/b0", files_expire_at=NOW + timedelta(hours=40))
    env.repo.transcript_jobs[(USER, "a")] = TranscriptJob(user_id=USER, connection_id=USER, item_id="a", content_key="hash-a",
                                                          duration_s=600.0, windows=(window,), reserved_usd=0.05,
                                                          reserved_month=MONTH)
    await _add(env, replace(record("a"), transcript_state="working", transcript_key="hash-a"), a=Recording(600))
    await _step(env)
    assert env.drive.calls == [] and env.media.probes == [] and env.runner.submits == []
    assert (await _get(env, "a")).transcript_state == "waiting"
