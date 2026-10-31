"""Final-review fix wave F1, the stage (USER_DRIVE_INDEXING_RFC §27.5, §27.10): a submission is on record before it is
made (T-R28), a batch whose outcome is unknown is booked as bought (T-R29), a copy waits for any same-key record still at
the key (T-R30), a long window accepted short is never silent (T-R31), and the minors (a reservation behind a running
batch is kept, one batch's error never stops the others). Synthetic recordings only."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.batch_transcription import BatchRef, BatchStatus
from src.domain.drive_index import LeaseLost
from src.domain.drive_index_params import (
    BATCH_ERROR_BACKOFF_S, BATCH_POLL_FIRST_S, BATCH_SUBMIT_LOOKUP_MIN_AGE_S, BATCH_SUBMIT_LOOKUPS,
)
from src.domain.drive_index_planner import next_month_start
from src.domain.drive_transcript import (
    JobWindow, MediaUnreadable, Segment, TranscriptBatch, TranscriptJob, WindowOutcome, WindowResult, WindowSpec,
    WindowTranscript,
)
from src.domain.drive_transcript_plan import window_estimate_usd
from src.domain.drive_transcript_windows import plan_windows
from src.domain.exceptions import LLMClientError, LLMRateLimitError, LLMUnavailableError
from src.services.drive_transcript_stage import TRANSCRIBER_AGENT, DriveTranscriptStage

from tests.unit.services.drive_transcript_fakes import (
    ckey, HANDLE, SCOPE, NOW, USER, Clock, FakeBatch, FakeDrive, FakeHost, FakeMedia, FakeRunner, Recording, record,
)

MONTH = "2026-10"


class FindingBatch(FakeBatch):
    """The batch port with the T-R28 lookup: `listed` is what the provider lists, by display name."""

    def __init__(self, clock: Clock) -> None:
        super().__init__(clock)
        self.polling = True
        self.listed: Dict[str, str] = {}
        self.finds: List[str] = []
        self.find_error: Optional[BaseException] = None

    async def find_batch(self, display_name: str) -> Optional[BatchRef]:
        self.finds.append(display_name)
        if self.find_error is not None:
            raise self.find_error
        name = self.listed.get(display_name)
        return BatchRef(name=name) if name else None


class CreatingRunner(FakeRunner):
    """`after_create`: the provider created the batch (it is listed), then the call failed (a timeout, a 5xx)."""

    def __init__(self, batch: FindingBatch) -> None:
        super().__init__()
        self.batch = batch
        self.after_create: Optional[BaseException] = None
        self.on_created = None
        self.displays: List[str] = []

    async def submit(self, inputs, *, display_name: str) -> BatchRef:
        self.displays.append(display_name)
        ref = await super().submit(inputs, display_name=display_name)
        self.batch.listed[display_name] = ref.name
        if self.on_created is not None:
            self.on_created()
        if self.after_create is not None:
            exc, self.after_create = self.after_create, None
            raise exc
        return ref


class LeaseHost(FakeHost):
    def __init__(self, repo, clock) -> None:
        super().__init__(repo, clock)
        self.lose_lease = False

    def fence(self) -> None:
        super().fence()
        if self.lose_lease:
            raise LeaseLost("lease ends")


def _env(tmp_path, *, cap=20.0):
    clock = Clock()
    repo = InMemoryDriveIndexRepository()
    media = FakeMedia(clock)
    batch = FindingBatch(clock)
    runner = CreatingRunner(batch)
    host = LeaseHost(repo, clock)
    stage = DriveTranscriptStage(repo, FakeDrive(), media, batch, runner, clock=clock, monotonic=clock.monotonic,
                                 monthly_cap_usd=cap, temp_root=str(tmp_path))
    return SimpleNamespace(clock=clock, repo=repo, media=media, batch=batch, runner=runner, host=host, stage=stage)


async def _add(env, *recs, **recordings):
    await env.host.commit(list(recs), {})
    for item_id, rec in recordings.items():
        env.media.recordings[item_id] = rec


async def _extract(env):
    env.host.backed_off.clear()
    return await env.stage.extract_step(HANDLE, env.host, stop_at=env.clock.mono + 600.0)


async def _cheap(env):
    env.host.backed_off.clear()
    return await env.stage.cheap_steps(HANDLE, env.host, stop_at=env.clock.mono + 600.0)


async def _get(env, item_id):
    return (await env.repo.get_records(SCOPE, [item_id])).get(item_id)


def _job(env, item_id):
    return env.repo.transcript_jobs.get((USER, item_id))


def _batches(env):
    return sorted((b for (u, _), b in env.repo.batches.items() if u == USER), key=lambda b: b.name)


def _reserved(env, month=MONTH):
    usage = env.repo.usage.get((USER, month))
    return usage.transcribe_reserved_usd if usage is not None else 0.0


def _spent(env, month=MONTH):
    return sum(usd for cls, usd, _, m in env.host.spends if m == month)


def _due(env, batch):
    env.clock.now = max(env.clock.now, batch.next_poll_at)


def _transcript(duration, *, short=False):
    end = duration * (0.5 if short else 0.95)
    starts = [float(s) for s in range(0, int(end), 50)] + [end]
    return WindowTranscript(language_codes=("en",), speakers=0, no_speech=False,
                            segments=tuple(Segment(s, None, f"alpha {i} beta") for i, s in enumerate(starts)))


def _answer(env, batch, *, short=False, finish="STOP", usd=0.01):
    out = {}
    for key in batch.request_keys:
        item_id, index = key.rsplit(":", 1)
        spec = next(w.spec for w in _job(env, item_id).windows if w.spec.index == int(index))
        out[key] = WindowResult(key=key, outcome=WindowOutcome.OK, transcript=_transcript(spec.duration_s, short=short),
                                usd=usd, billed=True, finish_reason=finish)
    env.runner.results[batch.name] = out
    env.batch.statuses[batch.name] = BatchStatus.SUCCEEDED


def _resubmit_job(now=NOW):
    """A job whose one window collection sent back (a re-purchase: reserved again when it is submitted, T-R8)."""
    window = JobWindow(spec=WindowSpec(0, 0.0, 600.0), state="uploaded", file_ref="files/x", request_key="a:0",
                       attempts=1, files_expire_at=now + timedelta(hours=40))
    return window, TranscriptJob(user_id=USER, connection_id=USER, item_id="a", content_key=ckey("hash-a"), duration_s=600.0,
                                 windows=(window,), reserved_usd=0.05, reserved_month=MONTH)


async def _with_resubmit(env):
    window, job = _resubmit_job()
    env.repo.transcript_jobs[(USER, "a")] = job
    await env.repo.reserve_model_usd(USER, MONTH, "transcribe", job.reserved_usd)
    await _add(env, replace(record("a"), transcript_state="working", transcript_key=ckey("hash-a")), a=Recording(600))
    return window_estimate_usd(window)


# -- T-R28: the purchase is on record before it is made ---------------------------------------------------------------

async def test_the_batch_document_is_written_before_the_provider_is_asked(tmp_path):
    env = _env(tmp_path)
    estimate = await _with_resubmit(env)
    seen: List = []
    env.runner.on_created = lambda: seen.append((dict(env.repo.batches), _job(env, "a")))
    await _extract(env)
    ((docs, job),) = seen
    ((_, pending),) = docs.items()
    assert pending.state == "submitting" and pending.name.startswith("submitting/")
    assert pending.display_name == env.runner.displays[0] and pending.display_name == f"{USER}:{pending.name}"
    assert pending.request_keys == ("a:0",) and pending.reserved_usd == pytest.approx(estimate)
    assert pending.reserved_month == MONTH and pending.next_poll_at is not None
    assert [(w.state, w.batch_name) for w in job.windows] == [("submitted", pending.name)]
    assert env.host.events[0] == ("reserve", pytest.approx(estimate), MONTH)   # the reservation before the document


async def test_a_submission_moves_to_the_provider_name(tmp_path):
    env = _env(tmp_path)
    estimate = await _with_resubmit(env)
    summary = await _extract(env)
    ((_, batch),) = env.repo.batches.items()
    assert batch.name == "batches/b1" and batch.state == "submitted" and batch.reserved_usd == pytest.approx(estimate)
    assert batch.next_poll_at == batch.submitted_at + timedelta(seconds=BATCH_POLL_FIRST_S)
    assert [(w.state, w.batch_name) for w in _job(env, "a").windows] == [("submitted", "batches/b1")]
    assert (await _get(env, "a")).transcript_state == "waiting"
    assert env.host.releases == [] and summary["transcript_batches"] == 1


async def test_no_fence_between_the_answer_and_its_record(tmp_path):
    """A LeaseLost right after the provider answered (formerly between submit and create_batch) loses nothing: the
    provider name is on the `submitting` document, and the next poll finishes the move without a lookup."""
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(600))
    env.runner.on_created = lambda: setattr(env.host, "lose_lease", True)
    with pytest.raises(LeaseLost):
        await _extract(env)
    ((_, pending),) = env.repo.batches.items()
    assert pending.state == "submitting" and pending.provider_name == "batches/b1"
    env.host.lose_lease = False
    _due(env, pending)
    summary = await _cheap(env)
    assert env.batch.finds == [] and summary["batch_submit_completed"] == 1
    ((_, batch),) = env.repo.batches.items()
    assert batch.name == "batches/b1" and batch.state == "submitted"
    assert [w.batch_name for w in _job(env, "a").windows] == ["batches/b1"]
    assert len(env.runner.submits) == 1


@pytest.mark.parametrize("error", [LLMUnavailableError("timed out"), LLMUnavailableError("503", http_status=503)])
async def test_an_ambiguous_submit_that_was_created_is_found_and_bought_once(tmp_path, error):
    env = _env(tmp_path)
    estimate = await _with_resubmit(env)
    env.runner.after_create = error
    summary = await _extract(env)
    assert env.host.outages == [TRANSCRIBER_AGENT] and summary["batch_submit_unknown"] == 1
    assert env.host.releases == []                              # possibly bought: nothing given back
    ((_, pending),) = env.repo.batches.items()
    assert pending.state == "submitting" and not pending.provider_name
    assert [w.state for w in _job(env, "a").windows] == ["submitted"]
    await _extract(env)                                         # the next step buys nothing again
    assert len(env.runner.submits) == 1 and (await _get(env, "a")).transcript_state == "waiting"
    _due(env, pending)
    summary = await _cheap(env)
    assert env.batch.finds == [pending.display_name] and summary["batch_submit_found"] == 1
    ((_, batch),) = env.repo.batches.items()
    assert batch.name == "batches/b1" and batch.reserved_usd == pytest.approx(estimate)
    assert [w.batch_name for w in _job(env, "a").windows] == ["batches/b1"]
    _answer(env, batch)
    _due(env, batch)
    await _cheap(env)
    assert (await _get(env, "a")).transcript_state == "current"
    assert len(env.runner.submits) == 1 and env.runner.bills == [True]
    assert env.host.releases == [(pytest.approx(estimate), MONTH), (pytest.approx(0.05), MONTH)]
    assert _reserved(env) == pytest.approx(0.0)


async def test_an_ambiguous_submit_never_created_hands_everything_back_once(tmp_path):
    env = _env(tmp_path)
    estimate = await _with_resubmit(env)
    env.runner.raise_next = LLMUnavailableError("connection reset")   # failed before the provider created anything
    await _extract(env)
    ((_, pending),) = env.repo.batches.items()
    assert pending.state == "submitting" and env.runner.submits == []
    lookups = 0
    while (USER, pending.name) in env.repo.batches:
        _due(env, env.repo.batches[(USER, pending.name)])
        await _cheap(env)
        lookups += 1
        if (USER, pending.name) in env.repo.batches:
            assert env.repo.batches[(USER, pending.name)].lookups == lookups and env.host.releases == []
        assert lookups <= BATCH_SUBMIT_LOOKUPS + 2
    assert lookups > BATCH_SUBMIT_LOOKUPS   # 3 misses by 12.5 min: the age bound waits for a fourth
    assert env.clock.now - pending.created_at >= timedelta(seconds=BATCH_SUBMIT_LOOKUP_MIN_AGE_S)
    assert env.repo.batches == {}
    assert env.host.releases == [(pytest.approx(estimate), MONTH)]   # given back exactly once
    window = _job(env, "a").windows[0]
    assert (window.state, window.request_key, window.batch_name, window.attempts) == ("uploaded", "a:0", "", 1)
    assert (await _get(env, "a")).transcript_state == "working"
    await _extract(env)                                              # bought now, once, reserved again
    assert len(env.runner.submits) == 1 and _batches(env)[0].reserved_usd == pytest.approx(estimate)


async def test_a_never_created_first_purchase_is_covered_by_its_file_again(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(600))
    env.runner.raise_next = LLMUnavailableError("connection reset")
    await _extract(env)
    ((_, pending),) = env.repo.batches.items()
    env.clock.now = pending.created_at + timedelta(seconds=BATCH_SUBMIT_LOOKUP_MIN_AGE_S)
    for _ in range(BATCH_SUBMIT_LOOKUPS):
        _due(env, env.repo.batches[(USER, pending.name)])
        await _cheap(env)
    assert env.repo.batches == {} and env.host.releases == []        # it held no re-purchase
    window = _job(env, "a").windows[0]
    assert (window.state, window.request_key) == ("uploaded", "")    # not a re-purchase: the file's reservation
    assert _job(env, "a").reserved_usd > 0


@pytest.mark.parametrize("error", [LLMClientError("bad request", http_status=400),
                                   LLMRateLimitError("429", http_status=429)])
async def test_a_definite_refusal_bought_nothing(tmp_path, error):
    env = _env(tmp_path)
    estimate = await _with_resubmit(env)
    env.runner.raise_next = error
    await _extract(env)
    assert env.repo.batches == {} and env.host.outages == [TRANSCRIBER_AGENT]
    assert env.host.releases == [(pytest.approx(estimate), MONTH)]
    window = _job(env, "a").windows[0]
    assert (window.state, window.request_key, window.batch_name) == ("uploaded", "a:0", "")
    assert (await _get(env, "a")).transcript_state == "working"


async def test_a_store_error_recording_the_answer_is_found_by_its_display_name(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(600))
    real_save = env.repo.save_batch
    calls = {"n": 0}

    async def failing_save(batch):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("store down")
        await real_save(batch)
    env.repo.save_batch = failing_save
    with pytest.raises(RuntimeError):
        await _extract(env)
    ((_, pending),) = env.repo.batches.items()
    assert pending.state == "submitting" and not pending.provider_name
    _due(env, pending)
    await _cheap(env)
    ((_, batch),) = env.repo.batches.items()
    assert batch.name == "batches/b1" and len(env.runner.submits) == 1


async def test_a_lookup_outage_backs_off_and_keeps_the_document(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(600))
    env.runner.raise_next = LLMUnavailableError("reset")
    await _extract(env)
    ((_, pending),) = env.repo.batches.items()
    env.batch.find_error = LLMUnavailableError("down")
    _due(env, pending)
    summary = await _cheap(env)
    assert summary["transcript_outages"] == 1 and env.host.outages[-1] == TRANSCRIBER_AGENT
    assert env.repo.batches[(USER, pending.name)].lookups == 0


# -- T-R29: an unknown outcome is booked as bought --------------------------------------------------------------------

async def test_a_batch_the_provider_forgot_is_booked_then_resubmitted_as_a_repurchase(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(600))
    await _extract(env)
    (b1,) = _batches(env)
    job = _job(env, "a")
    estimate = window_estimate_usd(job.windows[0])
    env.batch.poll_errors[b1.name] = LLMClientError("not found", http_status=404)
    _due(env, b1)
    await _cheap(env)
    assert env.host.spends == [("transcribe", pytest.approx(estimate), 1, MONTH)]   # the first purchase, booked
    assert env.host.releases == []                                                  # never only released
    closed = env.repo.batches[(USER, b1.name)]
    assert closed.state == "failed" and closed.booked_at is not None
    window = _job(env, "a").windows[0]
    assert (window.state, window.request_key, window.lost_purchases) == ("uploaded", "a:0", 1)
    assert _job(env, "a").reserved_usd == pytest.approx(job.reserved_usd)          # released when the job ends
    await _extract(env)
    b2 = next(b for b in _batches(env) if b.name != b1.name)
    assert b2.reserved_usd == pytest.approx(estimate)                               # reserved again at submit


async def test_a_forgotten_batch_books_its_own_reservation_in_its_month(tmp_path):
    env = _env(tmp_path)
    estimate = await _with_resubmit(env)
    await _extract(env)
    (b1,) = _batches(env)
    env.batch.poll_errors[b1.name] = LLMClientError("bad request", http_status=400)
    _due(env, b1)
    await _cheap(env)
    assert env.host.spends == [("transcribe", pytest.approx(estimate), 0, MONTH)]   # the batch's re-purchase
    assert env.host.releases == [(pytest.approx(estimate), MONTH)]                  # its counter, after the spend
    assert [e[0] for e in env.host.events[-2:]] == ["spend", "release"]


async def test_a_forgotten_batch_whose_cost_was_booked_is_not_booked_again(tmp_path):
    env = _env(tmp_path)
    estimate = await _with_resubmit(env)
    await _extract(env)
    (b1,) = _batches(env)
    env.repo.batches[(USER, b1.name)] = replace(b1, booked_at=NOW)
    env.batch.poll_errors[b1.name] = LLMClientError("not found", http_status=404)
    _due(env, b1)
    await _cheap(env)
    assert env.host.spends == [] and env.host.releases == [(pytest.approx(estimate), MONTH)]


async def test_a_provider_reported_failure_is_still_only_released(tmp_path):
    env = _env(tmp_path)
    estimate = await _with_resubmit(env)
    await _extract(env)
    (b1,) = _batches(env)
    env.batch.statuses[b1.name] = BatchStatus.FAILED
    _due(env, b1)
    await _cheap(env)
    assert env.host.spends == [] and env.host.releases == [(pytest.approx(estimate), MONTH)]


# -- T-R30: a copy waits while another record holds the key -----------------------------------------------------------

def _held_primary(state):
    specs = plan_windows(8.0)
    job = TranscriptJob(user_id=USER, connection_id=USER, item_id="a", content_key=ckey("shared"), duration_s=8.0, windows=tuple(
        JobWindow(s, state="uploaded", file_ref="files/a0", files_expire_at=NOW + timedelta(hours=40))
        for s in specs))
    wake = NOW + timedelta(minutes=10) if state == "held" else next_month_start(NOW)
    rec = replace(record("a", content_hash="shared", day="2026-10-02"), transcript_state=state,
                  transcript_hold="retry" if state == "held" else None, transcript_wake_at=wake,
                  transcript_key=ckey("shared"), transcript_attempts=1 if state == "held" else 0,
                  transcript_hold_version=ckey("shared"))
    return rec, job


@pytest.mark.parametrize("state", ["held", "parked"])
async def test_a_copy_waits_for_a_primary_that_holds_the_key(tmp_path, state):
    env = _env(tmp_path)
    rec, job = _held_primary(state)
    env.repo.transcript_jobs[(USER, "a")] = job
    await _add(env, rec, record("b", content_hash="shared"), a=Recording(8), b=Recording(8))
    assert (await _get(env, "a")).transcript_state == state
    summary = await _extract(env)
    assert (await _get(env, "b")).transcript_state == "waiting" and _job(env, "b") is None
    assert env.media.runs == [] and env.runner.submits == [] and summary["transcript_copies_waiting"] == 1
    await _extract(env)                                   # still waiting: the primary still holds the key
    assert (await _get(env, "b")).transcript_state == "waiting" and env.runner.submits == []


async def test_a_copy_and_a_primary_in_a_retry_hold_buy_the_audio_once(tmp_path):
    """The live case: two identical 8 s files were bought separately while one of them backed off."""
    env = _env(tmp_path)
    rec, job = _held_primary("held")
    env.repo.transcript_jobs[(USER, "a")] = job
    await _add(env, rec, record("b", content_hash="shared"), a=Recording(8), b=Recording(8))
    await _extract(env)
    env.clock.advance(timedelta(minutes=11).total_seconds())   # the primary's retry wakes
    await _extract(env)
    assert [[i.key for i in s] for s in env.runner.submits] == [["a:0"]]
    (batch,) = _batches(env)
    _answer(env, batch)
    _due(env, batch)
    await _cheap(env)
    await _extract(env)
    assert (await _get(env, "a")).transcript_state == "current"
    assert (await _get(env, "b")).transcript_state == "current"
    assert len(env.runner.submits) == 1 and {r["item"] for r in env.media.runs} <= {"a"}


# -- T-R31: a long window accepted short is stored truncated ----------------------------------------------------------

async def test_a_long_window_accepted_short_is_counted_and_stored_truncated(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(900))
    await _extract(env)
    (b1,) = _batches(env)
    _answer(env, b1, short=True)
    _due(env, b1)
    first = await _cheap(env)
    assert first.get("windows_coverage") == 1 and "windows_accepted_short" not in first
    await _extract(env)
    b2 = next(b for b in _batches(env) if b.name != b1.name)
    _answer(env, b2, short=True)
    _due(env, b2)
    second = await _cheap(env)
    assert second["windows_accepted_short"] == 1 and second["windows_accepted_truncated"] == 1
    stored = env.repo.transcripts[(USER, ckey("hash-a"))]
    assert stored.transcript is not None and stored.transcript.truncated


# -- minors -----------------------------------------------------------------------------------------------------------

async def test_a_charge_keeps_the_reservation_behind_a_running_batch(tmp_path):
    """T-R20's hole in `_release_job`: a working file charged while one of its windows is in a batch keeps its
    reservation (the batch may still bill against it)."""
    env = _env(tmp_path)
    specs = plan_windows(3_400.0)
    job = TranscriptJob(user_id=USER, connection_id=USER, item_id="a", content_key=ckey("hash-a"), duration_s=3_400.0, reserved_usd=0.4,
                        reserved_month=MONTH, windows=(
                            JobWindow(specs[0], state="submitted", file_ref="files/0", request_key="a:0",
                                      batch_name="batches/b0", files_expire_at=NOW + timedelta(hours=40)),
                            JobWindow(specs[1], state="extracted", request_key="a:1", attempts=1,
                                      reupload_used=True)))
    env.repo.transcript_jobs[(USER, "a")] = job
    await env.repo.create_batch(TranscriptBatch(name="batches/b0", user_id=USER, connection_id=USER, request_keys=("a:0",),
                                                next_poll_at=NOW + timedelta(hours=1)))
    await _add(env, replace(record("a"), transcript_state="working", transcript_key=ckey("hash-a")),
               a=Recording(3_400, extract_error=MediaUnreadable("bad packet")))
    summary = await _extract(env)
    assert summary["transcript_attempts_charged"] == 1
    assert env.host.releases == [] and _job(env, "a").reserved_usd == pytest.approx(0.4)


async def test_one_batch_that_raises_never_stops_the_others(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(600))
    await _extract(env)
    await _add(env, record("b", day="2026-10-02"), b=Recording(600))
    await _extract(env)
    b1, b2 = _batches(env)
    env.batch.poll_errors[b1.name] = TypeError("a bug")
    _answer(env, b2)
    env.clock.now = max(b1.next_poll_at, b2.next_poll_at)
    summary = await _cheap(env)
    assert summary["batch_errors"] == 1
    assert env.repo.batches[(USER, b1.name)].next_poll_at == env.clock.now + timedelta(seconds=BATCH_ERROR_BACKOFF_S)
    assert env.repo.batches[(USER, b2.name)].state == "done"


async def test_an_error_after_booking_keeps_booked_at(tmp_path):
    env = _env(tmp_path)
    await _add(env, record("a"), a=Recording(600))
    await _extract(env)
    (b1,) = _batches(env)
    _answer(env, b1)

    async def broken(*args, **kwargs):
        raise TypeError("a bug")
    env.repo.save_window_result = broken
    _due(env, b1)
    summary = await _cheap(env)
    assert summary["batch_errors"] == 1
    kept = env.repo.batches[(USER, b1.name)]
    assert kept.booked_at is not None and kept.state == "submitted"   # re-collected later without booking again
    assert len(env.host.spends) == 1
