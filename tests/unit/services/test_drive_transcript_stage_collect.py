"""The transcript stage, part B: poll, collect, settle, resubmit, finalize and the orphan sweep (USER_DRIVE_INDEXING_RFC
§27.4-§27.8, §27.10; plan Task 13; G9, G20, G32, G33, G37; rulings T-R11, T-R18, T-R20). Synthetic recordings only."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.batch_transcription import BatchStatus
from src.domain.drive_index import Reason, RecordState
from src.domain.drive_index_params import (
    BATCH_POLL_EVERY_S, BATCH_POLL_FIRST_S, BATCH_POLL_SLOW_S, BATCH_POLLS_PER_TICK, MAX_ATTEMPTS,
    TRANSCRIPT_ORPHAN_TTL,
)
from src.domain.drive_transcript import (
    Segment, StitchedTranscript, StoredTranscript, TranscriptBatch, WindowOutcome, WindowResult, WindowTranscript,
)
from src.domain.drive_transcript_plan import file_estimate_usd, window_estimate_usd
from src.domain.drive_transcript_render import GAP_TEXT
from src.domain.drive_transcript_windows import plan_windows
from src.domain.exceptions import LLMClientError, LLMUnavailableError
from src.services.drive_transcript_stage import EXTRACTED_BY, TRANSCRIBER_AGENT, DriveTranscriptStage

from tests.unit.services.drive_transcript_fakes import (
    ckey, cid, HANDLE, SCOPE, NOW, OLD, USER, Clock, FakeBatch, FakeDrive, FakeHost, FakeMedia, FakeRunner, Recording, record,
)

MONTH = "2026-10"


def _env(tmp_path, *, cap=20.0):
    clock = Clock()
    repo = InMemoryDriveIndexRepository()
    media = FakeMedia(clock)
    batch = FakeBatch(clock)
    batch.polling = True
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


async def _extract(env, stop_in=600.0):
    return await env.stage.extract_step(HANDLE, env.host, stop_at=env.clock.mono + stop_in)


async def _cheap(env, stop_in=600.0):
    return await env.stage.cheap_steps(HANDLE, env.host, stop_at=env.clock.mono + stop_in)


async def _get(env, item_id):
    return (await env.repo.get_records(SCOPE, [item_id])).get(item_id)


def _job(env, item_id):
    return env.repo.transcript_jobs.get((USER, item_id))


def _batches(env):
    return sorted((b for (u, _), b in env.repo.batches.items() if u == USER), key=lambda b: b.name)


def _last_batch(env):
    return _batches(env)[-1]


def _reserved(env):
    usage = env.repo.usage.get((USER, MONTH))
    return max(0.0, usage.transcribe_reserved_usd) if usage is not None else 0.0


def _due(env, batch):
    """Move the clock to the batch's next poll."""
    env.clock.now = max(env.clock.now, batch.next_poll_at)


def _transcript(duration, *, speaker=None, lang="uk", word="alpha", short=False, past_end=False):
    """A window answer that passes every check: a stamp at least every 50 s and one at 95 % of the window (single
    sentences, so nothing is interpolated). `short` stops at half the window (coverage, Δ129); `past_end` adds a stamp
    far past the window (the time validator, §27.6)."""
    end = duration * (0.5 if short else 0.95)
    starts = [float(s) for s in range(0, int(end), 50)] + [end]
    if past_end:
        starts.append(duration + 60.0)
    segs = tuple(Segment(s, speaker, f"{word} {i} beta gamma") for i, s in enumerate(starts))
    return WindowTranscript(language_codes=(lang,), speakers=1 if speaker else 0, no_speech=False, segments=segs)


def _ok(key, transcript, usd=0.01):
    return WindowResult(key=key, outcome=WindowOutcome.OK, transcript=transcript, usd=usd, billed=True)


def _answer_all(env, batch, *, usd=0.01, **by_key):
    """Every window of `batch` answered well, unless `by_key` gives a key its own result."""
    out = {}
    for key in batch.request_keys:
        item_id, index = key.rsplit(":", 1)
        spec = next(w.spec for w in _job(env, item_id).windows if w.spec.index == int(index))
        out[key] = by_key.get(key.replace(":", "_"), _ok(key, _transcript(spec.duration_s), usd))
    env.runner.results[batch.name] = out
    env.batch.statuses[batch.name] = BatchStatus.SUCCEEDED


def _assert_all_gaps_charged(env, item_id):
    """T-R23: every window a gap marker with no refusal is no transcript: the job is dropped (reservation released),
    nothing stored or indexed, and the file charged one attempt with the T-R21 retry hold."""
    assert _job(env, item_id) is None and (USER, ckey(f"hash-{item_id}")) not in env.repo.transcripts
    assert env.host.indexed == [] and _reserved(env) == pytest.approx(0.0)
    rec = env.repo.records[(USER, item_id)]
    assert (rec.transcript_state, rec.transcript_hold, rec.transcript_attempts) == ("held", "retry", 1)
    assert rec.transcript_wake_at is not None and rec.transcript_wake_at > env.clock.now


async def _submitted(env, *recs, **recordings):
    await _add(env, *recs, **recordings)
    await _extract(env)
    return _last_batch(env)


# -- polling (Δ104, Δ133) ---------------------------------------------------------------------------------------------

async def test_first_poll_after_150s_then_300s(tmp_path):
    env = _env(tmp_path)
    b = await _submitted(env, record("a"), a=Recording(600))
    assert b.next_poll_at == b.submitted_at + timedelta(seconds=BATCH_POLL_FIRST_S)
    env.clock.now = b.submitted_at + timedelta(seconds=BATCH_POLL_FIRST_S - 1)
    await _cheap(env)
    assert env.batch.polls == []
    env.clock.now = b.submitted_at + timedelta(seconds=BATCH_POLL_FIRST_S)
    summary = await _cheap(env)
    assert env.batch.polls == [b.name] and summary["batch_polls"] == 1
    after = _last_batch(env)
    assert after.next_poll_at == env.clock.now + timedelta(seconds=BATCH_POLL_EVERY_S)
    assert after.last_polled_at == env.clock.now and after.state == "submitted"


async def test_slow_cadence_after_one_hour(tmp_path):
    env = _env(tmp_path)
    b = await _submitted(env, record("a"), a=Recording(600))
    env.clock.now = b.submitted_at + timedelta(seconds=3_590)
    await _cheap(env)
    assert _last_batch(env).next_poll_at == env.clock.now + timedelta(seconds=BATCH_POLL_EVERY_S)
    _due(env, _last_batch(env))
    await _cheap(env)
    assert _last_batch(env).next_poll_at == env.clock.now + timedelta(seconds=BATCH_POLL_SLOW_S)
    assert len(env.batch.polls) == 2


async def test_at_most_10_polls_per_tick(tmp_path):
    env = _env(tmp_path)
    for i in range(BATCH_POLLS_PER_TICK + 2):
        await env.repo.create_batch(TranscriptBatch(name=f"batches/x{i:02d}", user_id=USER, connection_id=USER, submitted_at=NOW,
                                                    request_keys=(f"x{i}:0",), created_at=NOW, next_poll_at=NOW))
    await _cheap(env)
    assert len(env.batch.polls) == BATCH_POLLS_PER_TICK == len(set(env.batch.polls))
    await _cheap(env)   # the rest, once each; the polled ones are not due again yet
    assert len(env.batch.polls) == BATCH_POLLS_PER_TICK + 2 == len(set(env.batch.polls))
    assert all(b.next_poll_at is not None for b in _batches(env))


async def test_batch_waiting_for_hours_resumes_across_ticks(tmp_path):
    env = _env(tmp_path)
    b = await _submitted(env, record("a"), a=Recording(3_600))
    file_reserved = _reserved(env)
    ticks = 0
    while env.clock.now < b.submitted_at + timedelta(hours=6):
        env.clock.advance(600)   # the 10-minute fan-out
        ticks += 1
        await _extract(env)
        await _cheap(env)
        assert (await _get(env, "a")).transcript_state == "waiting"
    polls = len(env.batch.polls)
    assert 12 <= polls < ticks   # every 5 min in the first hour (each tick), then every 30 min
    assert all(b2.next_poll_at is not None for b2 in _batches(env))
    _answer_all(env, _last_batch(env))
    env.clock.advance(1_800)
    await _extract(env)
    await _cheap(env)
    rec = await _get(env, "a")
    assert rec.transcript_state == "current" and rec.transcript_seconds == 3_600
    assert len(env.runner.submits) == 1 and _job(env, "a") is None
    assert file_reserved > 0 and _reserved(env) == pytest.approx(0.0)
    assert [i[0] for i in env.host.indexed] == ["a"] and env.host.indexed[0][2] == EXTRACTED_BY


async def test_a_poll_outage_backs_off_and_leaves_the_batch(tmp_path):
    env = _env(tmp_path)
    b = await _submitted(env, record("a"), a=Recording(600))
    _due(env, b)
    env.batch.poll_errors[b.name] = LLMUnavailableError("down")
    summary = await _cheap(env)
    assert env.host.outages == [TRANSCRIBER_AGENT] and summary["transcript_outages"] == 1
    assert _last_batch(env) == b   # untouched: polled again next step


async def test_a_batch_the_provider_no_longer_knows_is_settled_as_failed(tmp_path):
    env = _env(tmp_path)
    b = await _submitted(env, record("a"), a=Recording(600))
    _due(env, b)
    env.batch.poll_errors[b.name] = LLMClientError("not found", http_status=404)
    await _cheap(env)
    assert _last_batch(env).state == "failed" and _last_batch(env).next_poll_at is None
    assert _job(env, "a").windows[0].state == "uploaded"


# -- failed, expired, cancelled batches (§27.5, Δ104) ----------------------------------------------------------------

@pytest.mark.parametrize("status", [BatchStatus.EXPIRED, BatchStatus.FAILED, BatchStatus.CANCELLED])
async def test_job_expiry_resubmits_open_windows_once(tmp_path, status):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(600))
    reserves = list(env.host.reserves)
    env.batch.statuses[b1.name] = status
    _due(env, b1)
    await _cheap(env)
    closed = env.repo.batches[(USER, b1.name)]
    assert closed.state == "failed" and closed.next_poll_at is None
    window = _job(env, "a").windows[0]
    assert (window.state, window.batch_resubmits, window.request_key) == ("uploaded", 1, "")
    assert (await _get(env, "a")).transcript_state == "working"
    await _extract(env)   # resubmitted: a first purchase that bought nothing stays on the file's reservation
    b2 = _last_batch(env)
    assert b2.name != b1.name and b2.reserved_usd == 0.0 and env.host.reserves == reserves
    env.batch.statuses[b2.name] = status
    _due(env, b2)
    await _cheap(env)   # the second failure: a gap marker, never a third batch
    await _extract(env)
    assert len(env.runner.submits) == 2
    _assert_all_gaps_charged(env, "a")


async def test_expired_file_reuploaded_once(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(600))
    assert env.batch.uploads and len(env.batch.uploads) == 1
    env.clock.advance(49 * 3_600)   # the window's provider file is older than its 48 h
    env.batch.statuses[b1.name] = BatchStatus.EXPIRED
    await _cheap(env)
    window = _job(env, "a").windows[0]
    assert (window.state, window.file_ref, window.reupload_used) == ("extracted", "", True)
    await _extract(env)   # cut and uploaded again, then submitted
    assert len(env.batch.uploads) == 2 and len(env.runner.submits) == 2
    assert _job(env, "a").windows[0].state == "submitted" and _job(env, "a").windows[0].reupload_used
    b2 = _last_batch(env)
    env.clock.advance(49 * 3_600)
    env.batch.statuses[b2.name] = BatchStatus.EXPIRED
    await _cheap(env)   # its one re-upload and its one batch resubmit are spent: a gap marker
    assert len(env.batch.uploads) == 2
    _assert_all_gaps_charged(env, "a")


async def test_failed_batch_releases_reservation(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(600))
    key = b1.request_keys[0]
    _answer_all(env, b1, a_0=WindowResult(key=key, outcome=WindowOutcome.ERROR, code=3))
    _due(env, b1)
    await _cheap(env)
    await _extract(env)   # the re-purchase is reserved at submit and carried by its batch (T-R8, T-R11)
    b2 = _last_batch(env)
    est = window_estimate_usd(_job(env, "a").windows[0])
    assert b2.reserved_usd == pytest.approx(est) and b2.reserved_month == MONTH
    before = _reserved(env)
    env.batch.statuses[b2.name] = BatchStatus.FAILED
    _due(env, b2)
    await _cheap(env)
    assert env.host.releases.count((pytest.approx(est), MONTH)) == 1
    assert _reserved(env) == pytest.approx(before - est)
    w = _job(env, "a").windows[0]
    assert w.state == "uploaded" and w.request_key   # a re-purchase stays one: reserved again at the next submit


# -- collection, per check (G37, T-R18) ------------------------------------------------------------------------------

async def test_succeeded_job_with_error_response_is_failed_attempt(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(600))
    key = b1.request_keys[0]
    _answer_all(env, b1, a_0=WindowResult(key=key, outcome=WindowOutcome.ERROR, code=3, usd=0.002))
    _due(env, b1)
    summary = await _cheap(env)
    w = _job(env, "a").windows[0]
    assert (w.state, w.attempts, w.file_ref != "") == ("uploaded", 1, True)
    assert (await _get(env, "a")).transcript_state == "working" and summary["windows_invalid"] == 1
    assert env.host.spends == [("transcribe", 0.002, 1, MONTH)]   # an error answer is billed as it came
    assert env.host.indexed == [] and env.batch.deleted == []
    await _extract(env)
    assert len(env.runner.submits) == 2 and env.runner.submits[1][0].key == key


async def test_invalid_output_is_a_failed_attempt(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(600))
    _answer_all(env, b1, a_0=WindowResult(key=b1.request_keys[0], outcome=WindowOutcome.INVALID))
    _due(env, b1)
    await _cheap(env)
    assert _job(env, "a").windows[0].attempts == 1 and _job(env, "a").windows[0].state == "uploaded"


@pytest.mark.parametrize("code", [5, 7])
async def test_a_gone_input_file_is_uploaded_again_once(tmp_path, code):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(600))
    gone = WindowResult(key=b1.request_keys[0], outcome=WindowOutcome.ERROR, code=code)
    _answer_all(env, b1, a_0=gone)
    _due(env, b1)
    await _cheap(env)
    w = _job(env, "a").windows[0]
    assert (w.state, w.file_ref, w.reupload_used, w.attempts) == ("extracted", "", True, 1)
    await _extract(env)
    assert len(env.batch.uploads) == 2
    b2 = _last_batch(env)
    _answer_all(env, b2, a_0=replace(gone, key=b2.request_keys[0]))
    _due(env, b2)
    await _cheap(env)   # gone again with its re-upload spent: the same request cannot succeed
    assert len(env.batch.uploads) == 2
    _assert_all_gaps_charged(env, "a")


async def test_short_window_is_resubmitted_not_stitched(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(600))
    key = b1.request_keys[0]
    _answer_all(env, b1, a_0=_ok(key, _transcript(600, short=True)))
    _due(env, b1)
    summary = await _cheap(env)
    assert summary["windows_coverage"] == 1
    assert _job(env, "a").windows[0].state == "uploaded" and _job(env, "a").windows[0].attempts == 1
    assert env.repo.window_results == {} and env.host.indexed == []
    assert (USER, ckey("hash-a")) not in env.repo.transcripts


async def test_gap_marker_after_max_attempts(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(600))
    for attempt in range(1, MAX_ATTEMPTS + 1):
        await _extract(env)
        b = _last_batch(env)
        _answer_all(env, b, a_0=_ok(b.request_keys[0], _transcript(600, short=True)))
        _due(env, b)
        await _cheap(env)
    assert len(env.runner.submits) == MAX_ATTEMPTS
    _assert_all_gaps_charged(env, "a")


async def test_refused_window_gap_marker_truncated(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(3_600))
    assert len(b1.request_keys) == len(plan_windows(3_600)) == 3
    _answer_all(env, b1, a_1=WindowResult(key="a:1", outcome=WindowOutcome.REFUSED))
    _due(env, b1)
    await _cheap(env)
    assert len(env.runner.submits) == 1   # a refusal is never resubmitted
    stored = env.repo.transcripts[(USER, ckey("hash-a"))]
    body = env.host.indexed[0][1]
    assert stored.transcript.truncated and body.count(GAP_TEXT) == 1 and "alpha" in body
    gap = next(s for s in stored.transcript.segments if s.gap)
    assert plan_windows(3_600)[1].start_s < gap.start_s < plan_windows(3_600)[1].start_s + 60   # at its seam
    assert (await _get(env, "a")).transcript_state == "current"


async def test_time_validator_resubmits_once_then_gap(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(600))
    for _ in range(2):
        await _extract(env)
        b = _last_batch(env)
        _answer_all(env, b, a_0=_ok(b.request_keys[0], _transcript(600, past_end=True)))
        _due(env, b)
        await _cheap(env)
    assert len(env.runner.submits) == 2
    _assert_all_gaps_charged(env, "a")


async def test_interpolation_share_resubmits_once_then_accepts(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(600))
    sparse = WindowTranscript(("uk",), 0, False, (
        Segment(0.0, None, "One here. Two here. Three here. Four here. Five here. Six here. Seven here. Eight here."),
        Segment(560.0, None, "The end")))
    for _ in range(2):
        await _extract(env)
        b = _last_batch(env)
        _answer_all(env, b, a_0=_ok(b.request_keys[0], sparse))
        _due(env, b)
        await _cheap(env)
    assert len(env.runner.submits) == 2
    stored = env.repo.transcripts[(USER, ckey("hash-a"))]
    assert not stored.transcript.truncated and any(s.interpolated for s in stored.transcript.segments)
    assert "[~" in env.host.indexed[0][1]


# -- settlement (Δ106, G9, T-R11, G33) --------------------------------------------------------------------------------

async def test_settle_releases_and_books_real_cost(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(600))
    file_reserved = env.host.reserves[0][0]
    assert file_reserved == pytest.approx(file_estimate_usd(600))
    _answer_all(env, b1, a_0=WindowResult(key="a:0", outcome=WindowOutcome.ERROR, code=13, transient=True,
                                          usd=0.003))
    _due(env, b1)
    await _cheap(env)
    await _extract(env)
    b2 = _last_batch(env)
    env.host.events.clear()
    _answer_all(env, b2, usd=0.0125)
    _due(env, b2)
    await _cheap(env)
    spend = ("spend", 0.0125, MONTH)
    release_batch = ("release", pytest.approx(b2.reserved_usd), MONTH)
    release_file = ("release", pytest.approx(file_reserved), MONTH)
    assert env.host.events == [spend, release_batch, release_file]   # booked first, each reservation released once
    assert env.repo.batches[(USER, b2.name)].state == "done"
    assert _reserved(env) == pytest.approx(0.0)


async def test_cost_is_booked_in_the_month_of_the_reservation(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(600))
    env.clock.now = env.clock.now.replace(month=11, day=2)   # collected after the month turned (G9)
    _answer_all(env, b1, usd=0.02)
    await _cheap(env)
    assert env.host.spends == [("transcribe", 0.02, 1, MONTH)]
    assert env.host.releases == [(pytest.approx(file_estimate_usd(600)), MONTH)]


async def test_result_for_deleted_record_settled_and_dropped(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(600))
    file_ref = _job(env, "a").windows[0].file_ref
    assert await env.stage.drop_jobs(HANDLE, env.host, ["a"]) == 0   # T-R20: left for collection
    await env.repo.delete_records(SCOPE, ["a"])
    _answer_all(env, b1, usd=0.01)
    _due(env, b1)
    env.host.events.clear()
    summary = await _cheap(env)
    assert env.host.events == [("spend", 0.01, MONTH), ("release", pytest.approx(file_estimate_usd(600)), MONTH)]
    assert _job(env, "a") is None and env.repo.window_results == {}
    assert env.batch.deleted == [file_ref] and env.host.indexed == []
    assert summary["transcript_results_dropped"] == 1 and _reserved(env) == pytest.approx(0.0)


async def test_result_for_another_content_version_settled_and_dropped(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(600))
    await env.host.commit([replace(await _get(env, "a"), content_version="c2", content_identity=cid("hash-a2"))],
                          {"a": await _get(env, "a")})
    await _extract(env)   # stage A leaves the job alone while its window is in a batch (T-R20)
    assert len(env.runner.submits) == 1 and _job(env, "a").content_key == ckey("hash-a")
    _answer_all(env, b1, usd=0.01)
    _due(env, b1)
    await _cheap(env)
    assert _job(env, "a") is None and (USER, ckey("hash-a")) not in env.repo.transcripts
    assert _reserved(env) == pytest.approx(0.0)
    assert (await _get(env, "a")).transcript_state in ("held", "ready")   # free to proceed at its new version


async def test_files_deleted_on_success_and_abandon(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a", day="2026-10-02"), record("b"), a=Recording(600), b=Recording(600))
    refs = {i: _job(env, i).windows[0].file_ref for i in ("a", "b")}
    await env.repo.delete_records(SCOPE, ["b"])
    _answer_all(env, b1)
    _due(env, b1)
    await _cheap(env)
    assert sorted(env.batch.deleted) == sorted(refs.values())
    assert (await _get(env, "a")).transcript_state == "current"


async def test_a_resubmitted_window_keeps_its_file(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(600))
    _answer_all(env, b1, a_0=_ok("a:0", _transcript(600, short=True)))
    _due(env, b1)
    await _cheap(env)
    assert env.batch.deleted == [] and len(env.batch.uploads) == 1


# -- finalize (§27.8, G20, G32, §7.4) ----------------------------------------------------------------------------------

async def test_no_speech_has_no_chunks(tmp_path):
    env = _env(tmp_path)
    await _add(env, replace(record("a"), state=RecordState.INDEXED, indexed_generation=1, index_generation=1,
                            indexed_version="c1"), a=Recording(600))
    await _extract(env)
    b1 = _last_batch(env)
    silent = WindowTranscript((), 0, True, ())
    _answer_all(env, b1, a_0=_ok("a:0", silent))
    _due(env, b1)
    summary = await _cheap(env)
    rec = await _get(env, "a")
    assert env.host.indexed == [] and summary["transcript_stub_no_speech"] == 1
    assert rec.transcript_state == "current" and rec.transcript_stub == "no_speech"
    assert (rec.state, rec.reason) == (RecordState.METADATA_ONLY, Reason.NO_TEXT)
    assert rec.transcript_seconds == 600 and "a" in env.host.collected   # the old chunks go (G20 stub, never plain)
    stored = env.repo.transcripts[(USER, ckey("hash-a"))]
    assert stored.transcript is None and stored.stub == "no_speech" and stored.last_used_at == env.clock.now


async def test_finalize_writes_store_record_and_releases(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(600))
    _answer_all(env, b1)
    _due(env, b1)
    await _cheap(env)
    rec = await _get(env, "a")
    stored = env.repo.transcripts[(USER, ckey("hash-a"))]
    assert rec.transcript_state == "current" and rec.transcript_seconds == 600
    assert rec.transcript_languages == ("uk",) and rec.transcript_speakers == stored.transcript.speakers
    assert rec.extracted_by == EXTRACTED_BY and stored.last_used_at == env.clock.now
    assert _job(env, "a") is None and env.repo.window_results == {} and _reserved(env) == pytest.approx(0.0)


async def test_finalize_through_host_fence(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(600))
    _answer_all(env, b1)

    async def edited_under_the_run(rec):
        await env.host.commit([replace(rec, content_version="c2", content_identity=cid("hash-a2"))], {"a": rec})
        return False   # the §7.4 fence drops the run's result

    env.host.on_index = edited_under_the_run
    _due(env, b1)
    summary = await _cheap(env)
    assert summary["transcript_finalize_fenced"] == 1
    assert (await _get(env, "a")).transcript_state != "current" and (USER, ckey("hash-a")) not in env.repo.transcripts
    # the job belongs to another version now: dropped at once, its reservation released once
    assert _job(env, "a") is None and _reserved(env) == pytest.approx(0.0)
    assert env.host.releases.count((pytest.approx(file_estimate_usd(600)), MONTH)) == 1
    env.host.on_index = None
    await _cheap(env)
    assert env.host.releases.count((pytest.approx(file_estimate_usd(600)), MONTH)) == 1


async def test_crash_during_finalize_refinalizes(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(600))
    _answer_all(env, b1)

    async def crash(rec):
        raise RuntimeError("instance killed")

    env.host.on_index = crash
    _due(env, b1)
    with pytest.raises(RuntimeError):
        await _cheap(env)
    assert (await _get(env, "a")).transcript_state == "waiting" and _job(env, "a") is not None
    assert env.repo.batches[(USER, b1.name)].state == "done"   # collection is settled; only finalize repeats
    env.host.on_index = None
    await _cheap(env)
    rec = await _get(env, "a")
    assert rec.transcript_state == "current" and _job(env, "a") is None
    assert len(env.runner.collects) == 1 and _reserved(env) == pytest.approx(0.0)


async def test_crash_after_the_job_is_dropped_finishes_from_the_store(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(600))
    _answer_all(env, b1)
    real_commit = env.host.commit

    async def crash_on_current(records, before):
        if any(r.transcript_state == "current" for r in records):
            raise RuntimeError("instance killed")
        await real_commit(records, before)

    env.host.commit = crash_on_current
    _due(env, b1)
    with pytest.raises(RuntimeError):
        await _cheap(env)
    assert _job(env, "a") is None and (USER, ckey("hash-a")) in env.repo.transcripts
    assert (await _get(env, "a")).transcript_state == "waiting"
    env.host.commit = real_commit
    await _extract(env)   # stage A's store path (Δ112): no model, no ffmpeg
    assert (await _get(env, "a")).transcript_state == "current" and len(env.runner.submits) == 1


async def test_finalize_takes_a_file_budget_slot(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a", day="2026-10-02"), record("b"), a=Recording(600), b=Recording(600))
    _answer_all(env, b1)
    env.host.file_slots = 1
    _due(env, b1)
    summary = await _cheap(env)
    states = sorted([(await _get(env, i)).transcript_state for i in ("a", "b")])
    assert states == ["current", "waiting"] and summary["transcript_finalize_no_slot"] == 1
    env.host.file_slots = 1   # the next tick's slot
    await _cheap(env)
    assert [(await _get(env, i)).transcript_state for i in ("a", "b")] == ["current", "current"]


async def test_finalize_starts_only_before_the_deadline(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(600))
    _answer_all(env, b1)
    _due(env, b1)
    summary = await _cheap(env, stop_in=0.0)
    assert summary["transcript_finalize_deferred"] == 1 and env.host.indexed == []
    assert env.repo.batches[(USER, b1.name)].state == "done"   # collection takes no share
    await _cheap(env)
    assert (await _get(env, "a")).transcript_state == "current"


async def test_a_parked_record_with_a_job_in_a_batch_is_collected_and_finalized(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(600))
    rec = await _get(env, "a")
    await env.repo.upsert_records([replace(rec, transcript_state="parked",
                                           transcript_wake_at=NOW + timedelta(days=20))])
    _answer_all(env, b1)
    _due(env, b1)
    await _cheap(env)
    assert (await _get(env, "a")).transcript_state == "current" and _job(env, "a") is None


async def test_multi_window_speakers_are_aligned(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(3_600))
    specs = plan_windows(3_600)
    line = "we talked about the budget for the next quarter today"

    def window(spec, labels):
        """Two real speakers alternating every 20 s of the file's clock; each window numbers them its own way."""
        starts = [float(t) for t in range(0, int(spec.duration_s), 20)] + [spec.duration_s - 5]
        segs = tuple(Segment(t, labels[int((spec.start_s + t) // 20) % 2], f"{line} {int(spec.start_s + t)}")
                     for t in starts)
        return WindowTranscript(("uk",), 2, False, segs)

    results = {"a_0": _ok("a:0", window(specs[0], (1, 2))), "a_1": _ok("a:1", window(specs[1], (2, 1))),
               "a_2": _ok("a:2", window(specs[2], (1, 2)))}
    _answer_all(env, b1, **results)
    _due(env, b1)
    await _cheap(env)
    stored = env.repo.transcripts[(USER, ckey("hash-a"))]
    rec = await _get(env, "a")
    assert stored.transcript.speakers == rec.transcript_speakers == 2   # the aligned count, not the windows' labels
    assert not stored.transcript.uncertain_seam
    assert "Speaker 2:" in env.host.indexed[0][1] and "Speaker 3:" not in env.host.indexed[0][1]


# -- the orphan sweep (§27.8 b) ----------------------------------------------------------------------------------------

async def test_orphan_sweep_after_ttl(tmp_path):
    env = _env(tmp_path)
    long_ago = NOW - TRANSCRIPT_ORPHAN_TTL - timedelta(days=1)
    t = StitchedTranscript((Segment(0.0, None, "x"),), ("uk",), 1, 60.0)
    await env.repo.put_transcript(USER, StoredTranscript(ckey("orphan"), t, last_used_at=long_ago))
    await env.repo.put_transcript(USER, StoredTranscript(ckey("in-use"), t, last_used_at=long_ago))
    await env.repo.put_transcript(USER, StoredTranscript(ckey("young"), t, last_used_at=NOW - timedelta(days=1)))
    await env.repo.upsert_records([replace(record("c", content_hash="in-use"), transcript_state="current",
                                           transcript_key=ckey("in-use"))])
    summary = await _cheap(env)
    assert (USER, ckey("orphan")) not in env.repo.transcripts
    assert env.repo.transcripts[(USER, ckey("in-use"))].last_used_at == NOW   # referenced: kept, and out of the next page
    assert (USER, ckey("young")) in env.repo.transcripts
    assert summary["transcript_orphans_deleted"] == 1 and summary["transcript_orphans_kept"] == 1
    await env.repo.put_transcript(USER, StoredTranscript(ckey("orphan2"), t, last_used_at=long_ago))
    await _cheap(env)   # once a day
    assert (USER, ckey("orphan2")) in env.repo.transcripts
    env.clock.advance(86_400)
    await _cheap(env)
    assert (USER, ckey("orphan2")) not in env.repo.transcripts


async def test_nothing_from_a_transcript_is_logged(tmp_path, caplog):
    import logging
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(3_600))
    _answer_all(env, b1, a_1=WindowResult(key="a:1", outcome=WindowOutcome.REFUSED))
    _due(env, b1)
    with caplog.at_level(logging.DEBUG):
        await _cheap(env)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "alpha" not in text and "files/" not in text and "https://" not in text


# -- fix round 1 (T-R22, T-R23, M1-M5) ---------------------------------------------------------------------------------

async def test_a_collection_that_fails_after_booking_neither_books_nor_bills_again(tmp_path):
    """T-R22: anything raising between the booking and the batch's close re-collects with no second booking and no
    second account bill."""
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(600))
    _answer_all(env, b1, usd=0.01)
    real = env.repo.save_window_result
    calls = {"n": 0}

    async def flaky(*args, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("store write failed")
        await real(*args, **kw)

    env.repo.save_window_result = flaky
    _due(env, b1)
    with pytest.raises(RuntimeError):
        await _cheap(env)
    assert env.repo.batches[(USER, b1.name)].booked_at is not None
    assert env.repo.batches[(USER, b1.name)].state == "submitted"
    await _cheap(env)   # polled again (still due), collected again: answers applied once
    assert env.host.spends == [("transcribe", 0.01, 1, MONTH)]
    assert env.runner.bills == [True, False]   # the account billed once
    assert env.repo.batches[(USER, b1.name)].state == "done"
    assert (await _get(env, "a")).transcript_state == "current"


async def test_all_gaps_without_a_refusal_fail_at_max_attempts_with_no_stub(tmp_path):
    env = _env(tmp_path)
    await _add(env, replace(record("a"), transcript_attempts=MAX_ATTEMPTS - 1, transcript_hold_version=ckey("hash-a")),
               a=Recording(600))
    for _ in range(2):   # the batch fails, its one resubmit fails too: a gap marker, nothing refused
        await _extract(env)
        b = _last_batch(env)
        env.batch.statuses[b.name] = BatchStatus.FAILED
        _due(env, b)
        summary = await _cheap(env)
    assert summary["transcript_all_gaps_charged"] == 1 and summary["transcript_failed"] == 1
    rec = await _get(env, "a")
    assert rec.transcript_state == "failed" and rec.transcript_stub == ""
    assert _job(env, "a") is None and env.host.indexed == [] and _reserved(env) == pytest.approx(0.0)
    assert (USER, ckey("hash-a")) not in env.repo.transcripts


async def test_all_gaps_with_a_refusal_finalize_truncated(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(600))
    _answer_all(env, b1, a_0=WindowResult(key="a:0", outcome=WindowOutcome.REFUSED))
    _due(env, b1)
    await _cheap(env)
    rec = await _get(env, "a")
    stored = env.repo.transcripts[(USER, ckey("hash-a"))]
    assert rec.transcript_state == "current" and stored.transcript.truncated
    assert env.host.indexed[0][1].count(GAP_TEXT) == 1 and _reserved(env) == pytest.approx(0.0)


async def test_partial_gaps_without_a_refusal_still_finalize(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(3_600))
    for _ in range(MAX_ATTEMPTS):   # window 1 stops short every time; windows 0 and 2 answer well the first time
        await _extract(env)
        b = _last_batch(env)
        _answer_all(env, b, a_1=_ok("a:1", _transcript(1_800, short=True)))
        _due(env, b)
        await _cheap(env)
    rec = await _get(env, "a")
    stored = env.repo.transcripts[(USER, ckey("hash-a"))]
    assert rec.transcript_state == "current" and stored.transcript.truncated
    body = env.host.indexed[0][1]
    assert body.count(GAP_TEXT) == 1 and "alpha" in body


async def test_a_refused_window_is_marked_on_the_job(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(3_600))
    _answer_all(env, b1, a_1=WindowResult(key="a:1", outcome=WindowOutcome.REFUSED))
    env.host.file_slots = 0   # keep the job: finalize has no slot
    _due(env, b1)
    await _cheap(env)
    windows = {w.spec.index: w for w in _job(env, "a").windows}
    assert windows[1].refused and windows[1].gap and not windows[0].refused


async def test_a_poll_client_error_is_logged_as_an_error(tmp_path, caplog):
    import logging
    env = _env(tmp_path)
    b = await _submitted(env, record("a"), a=Recording(600))
    _due(env, b)
    env.batch.poll_errors[b.name] = LLMClientError("bad request", http_status=400)
    with caplog.at_level(logging.WARNING):
        await _cheap(env)
    assert any(r.levelno == logging.ERROR and "gone at the provider" in r.getMessage() for r in caplog.records)


async def test_a_batch_without_request_keys_is_closed_and_released_once(tmp_path):
    env = _env(tmp_path)
    await env.host.reserve(0.05, MONTH)
    await env.repo.create_batch(TranscriptBatch(name="batches/empty", user_id=USER, connection_id=USER, submitted_at=NOW, created_at=NOW,
                                                next_poll_at=NOW, reserved_usd=0.05, reserved_month=MONTH))
    await _cheap(env)
    closed = env.repo.batches[(USER, "batches/empty")]
    assert closed.state == "failed" and closed.next_poll_at is None and env.batch.polls == []
    assert env.host.releases == [(0.05, MONTH)] and _reserved(env) == pytest.approx(0.0)
    env.clock.advance(3_600)
    await _cheap(env)
    assert env.host.releases == [(0.05, MONTH)]


async def test_an_answer_with_text_but_no_priced_cost_books_the_window_estimate(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(600))
    estimate = window_estimate_usd(_job(env, "a").windows[0])
    unpriced = replace(_ok("a:0", _transcript(600), usd=0.0), billed=False)   # usage missing: priced 0, not billed
    _answer_all(env, b1, a_0=unpriced)
    _due(env, b1)
    await _cheap(env)
    assert env.host.spends == [("transcribe", pytest.approx(estimate), 1, MONTH)]


async def test_an_error_answer_without_cost_books_nothing(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(600))
    _answer_all(env, b1, a_0=WindowResult(key="a:0", outcome=WindowOutcome.ERROR, code=13, transient=True))
    _due(env, b1)
    await _cheap(env)
    assert env.host.spends == [("transcribe", 0.0, 1, MONTH)]


async def test_dropped_windows_are_gap_markers_while_another_batch_carries_the_rest(tmp_path):
    from src.domain.drive_transcript import JobWindow, TranscriptJob
    env = _env(tmp_path)
    specs = plan_windows(3_600)
    job = TranscriptJob(USER, "a", ckey("hash-a"), connection_id=USER, content_version=1, duration_s=3_600.0, reserved_usd=0.1,
                        reserved_month=MONTH, windows=(
                            JobWindow(specs[0], state="submitted", file_ref="files/0", request_key="a:0",
                                      batch_name="batches/b1", files_expire_at=NOW + timedelta(hours=40)),
                            JobWindow(specs[1], state="submitted", file_ref="files/1", request_key="a:1",
                                      batch_name="batches/b2", files_expire_at=NOW + timedelta(hours=40)),
                            JobWindow(specs[2], state="done")))
    await env.repo.save_transcript_job(job)
    await env.repo.create_batch(TranscriptBatch(name="batches/b1", user_id=USER, connection_id=USER, request_keys=("a:0",),
                                                submitted_at=NOW, created_at=NOW, next_poll_at=NOW))
    env.runner.results["batches/b1"] = {"a:0": _ok("a:0", _transcript(1_800))}
    env.batch.statuses["batches/b1"] = BatchStatus.SUCCEEDED
    await _cheap(env)   # no record "a": G33, but b2 still carries window 1
    windows = {w.spec.index: w for w in _job(env, "a").windows}
    assert (windows[0].state, windows[0].gap, windows[0].file_ref) == ("done", True, "")
    assert windows[1].state == "submitted" and env.batch.deleted == ["files/0"]


async def test_a_fenced_finalize_goes_on_to_the_next_file(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a", day="2026-10-02"), record("b"), a=Recording(600), b=Recording(600))
    _answer_all(env, b1)

    async def rename_a(rec):
        if rec.item_id != "a":
            return None
        await env.host.commit([replace(rec, name="renamed.webm")], {"a": rec})
        return False   # the §7.4 fence drops a's run; its content version stands

    env.host.on_index = rename_a
    _due(env, b1)
    summary = await _cheap(env)
    assert summary["transcript_finalize_fenced"] == 1 and "transcript_finalize_no_slot" not in summary
    assert (await _get(env, "b")).transcript_state == "current"
    assert (await _get(env, "a")).transcript_state == "waiting" and _job(env, "a") is not None
    env.host.on_index = None
    await _cheap(env)
    assert (await _get(env, "a")).transcript_state == "current"


async def test_short_stop_answer_is_resubmitted_once_then_accepted_t_r27(tmp_path):
    env = _env(tmp_path)
    b1 = await _submitted(env, record("a"), a=Recording(600))
    key = b1.request_keys[0]
    _answer_all(env, b1, a_0=replace(_ok(key, _transcript(600, short=True)), finish_reason="STOP"))
    _due(env, b1)
    summary = await _cheap(env)
    w = _job(env, "a").windows[0]
    assert (w.state, w.coverage_resubmits, w.attempts) == ("uploaded", 1, 0)
    assert summary.get("windows_accepted_short", 0) == 0 and env.repo.window_results == {}
    await _extract(env)
    b2 = _last_batch(env)
    _answer_all(env, b2, a_0=replace(_ok(b2.request_keys[0], _transcript(600, short=True)), finish_reason="STOP"))
    _due(env, b2)
    summary = await _cheap(env)
    assert summary["windows_accepted_short"] == 1 and summary.get("windows_gap", 0) == 0
