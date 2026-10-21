"""Final-review fix wave F1, the pure side (USER_DRIVE_INDEXING_RFC §27.5): the coverage tolerance and the long-window
short acceptance (T-R31, narrowing T-R27), a batch whose outcome is unknown booked as bought (T-R29), and a batch that
was never created handing its windows back (T-R28)."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.drive_index_params import (
    TRANSCRIBE_COVERAGE_MIN, TRANSCRIBE_COVERAGE_TAIL_S, TRANSCRIBE_SHORT_ACCEPT_MAX_S,
)
from src.domain.drive_transcript import (
    JobWindow, Segment, TranscriptBatch, TranscriptJob, WindowSpec, WindowTranscript, batch_url_fields,
)
from src.domain.drive_transcript_job import (
    after_failed_batch, in_batch, is_repurchase, repointed, stitch_job, unsubmitted,
)
from src.domain.drive_transcript_plan import TranscriptContext, WindowAction, WindowCheck, window_failed
from src.domain.drive_transcript_windows import coverage_ok, plan_windows

T = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
CTX = TranscriptContext(now=T, enabled=True, month_usd=0.0, reserved_usd=0.0, cap_usd=20.0)


def _segs(*starts):
    return tuple(Segment(float(s), None, f"word {i}") for i, s in enumerate(starts))


# -- T-R31: coverage tolerance = max(0.1 x d, 60 s) --------------------------------------------------------------------

def test_tail_constant_is_sixty_seconds_and_short_accept_bound_five_minutes():
    assert TRANSCRIBE_COVERAGE_TAIL_S == 60 and TRANSCRIBE_SHORT_ACCEPT_MAX_S == 300


@pytest.mark.parametrize("duration,last_ok,last_short", [
    (137.0, 77.0, 76.0),          # 137 - 60: the live recording that ends in silence passes at once
    (8.0, 0.0, None),             # an 8 s window: any first segment covers it
    (300.0, 240.0, 239.0),
    (600.0, 540.0, 539.0),        # 0.1 x 600 = 60: both rules meet here
    (1_800.0, 1_620.0, 1_619.0),  # a long window keeps the 0.9 rule
])
def test_coverage_tolerance_is_the_larger_of_a_tenth_and_sixty_seconds(duration, last_ok, last_short):
    spec = WindowSpec(0, 0.0, duration)
    assert coverage_ok(spec, _segs(0, last_ok), False)
    if last_short is not None:
        assert not coverage_ok(spec, _segs(0, last_short), False)
    tolerance = max((1 - TRANSCRIBE_COVERAGE_MIN) * duration, TRANSCRIBE_COVERAGE_TAIL_S)
    assert coverage_ok(spec, _segs(max(0.0, duration - tolerance)), False)


def test_coverage_still_needs_a_segment_unless_no_speech():
    spec = WindowSpec(0, 0.0, 8.0)
    assert not coverage_ok(spec, (), False)
    assert coverage_ok(spec, (), True)


# -- T-R31: the second clean short answer, by window length ------------------------------------------------------------

def _win(duration, **kw):
    return JobWindow(spec=WindowSpec(index=0, start_s=0.0, duration_s=duration), state="submitted", **kw)


@pytest.mark.parametrize("duration", [137.0, float(TRANSCRIBE_SHORT_ACCEPT_MAX_S)])
def test_a_short_window_accepted_short_is_not_marked(duration):
    r = window_failed(_win(duration, coverage_resubmits=1), WindowCheck.COVERAGE, CTX, "STOP")
    assert r.action is WindowAction.ACCEPT
    assert r.window.state == "done" and not r.window.gap and not r.window.accepted_short


@pytest.mark.parametrize("duration", [TRANSCRIBE_SHORT_ACCEPT_MAX_S + 1.0, 1_800.0])
def test_a_longer_window_accepted_short_is_marked_never_silent(duration):
    first = window_failed(_win(duration), WindowCheck.COVERAGE, CTX, "STOP")
    assert first.action is WindowAction.RESUBMIT and not first.window.accepted_short
    r = window_failed(first.window, WindowCheck.COVERAGE, CTX, "STOP")
    assert r.action is WindowAction.ACCEPT
    assert r.window.state == "done" and not r.window.gap and r.window.accepted_short


def test_stitch_marks_a_transcript_with_a_long_window_accepted_short_truncated():
    specs = plan_windows(3_400.0)
    answer = WindowTranscript(language_codes=("en",), speakers=0, no_speech=False, segments=_segs(0, 50, 100))
    job = TranscriptJob("u", "i", "k", duration_s=3_400.0,
                        windows=tuple(JobWindow(s, state="done") for s in specs))
    results = {s.index: answer for s in specs}
    assert not stitch_job(job, results).truncated
    marked = replace(job, windows=(replace(job.windows[0], accepted_short=True),) + job.windows[1:])
    assert stitch_job(marked, results).truncated


# -- T-R29: a batch whose outcome is unknown is booked as bought --------------------------------------------------------

def _fresh(**kw):
    return JobWindow(WindowSpec(0, 0.0, 600.0), state="submitted", file_ref="files/1", request_key="i:0",
                     batch_name="batches/b", files_expire_at=T + timedelta(hours=40), **kw)


def test_a_window_booked_as_bought_comes_back_as_a_repurchase():
    back = after_failed_batch(_fresh(), T, bought=True)
    assert (back.state, back.request_key, back.batch_resubmits, back.lost_purchases) == ("uploaded", "i:0", 1, 1)
    assert is_repurchase(back)


def test_a_plain_failed_batch_is_still_no_purchase():
    back = after_failed_batch(_fresh(), T)
    assert (back.request_key, back.lost_purchases) == ("", 0) and not is_repurchase(back)


def test_lost_purchases_alone_make_a_repurchase():
    assert is_repurchase(JobWindow(WindowSpec(0, 0.0, 60.0), state="uploaded", lost_purchases=1))


# -- T-R28: a submission's windows, by the batch document that carries them ---------------------------------------------

def _job(*windows):
    return TranscriptJob("u", "i", "k", duration_s=3_400.0, windows=windows, batch_name="submitting/x")


def test_unsubmitted_hands_the_windows_of_one_batch_back_as_they_were():
    first = JobWindow(WindowSpec(0, 0.0, 1_800.0), state="submitted", file_ref="f0", request_key="i:0",
                      batch_name="submitting/x")
    again = JobWindow(WindowSpec(1, 1_740.0, 1_660.0), state="submitted", file_ref="f1", request_key="i:1",
                      batch_name="submitting/x", attempts=1)
    other = JobWindow(WindowSpec(2, 3_480.0, 10.0), state="submitted", file_ref="f2", request_key="i:2",
                      batch_name="batches/other")
    out = unsubmitted(_job(first, again, other), "submitting/x")
    assert [(w.state, w.request_key, w.batch_name, w.file_ref) for w in out.windows] == [
        ("uploaded", "", "", "f0"),           # a first purchase: covered by the file's reservation again
        ("uploaded", "i:1", "", "f1"),        # a re-purchase: reserved again at the next submit
        ("submitted", "i:2", "batches/other", "f2")]
    assert out.batch_name == "" and in_batch(out, "submitting/x") == ()
    assert (out.windows[0].batch_resubmits, out.windows[0].attempts) == (0, 0)   # nothing happened to them


def test_repointed_moves_the_windows_to_the_provider_batch():
    w = JobWindow(WindowSpec(0, 0.0, 600.0), state="submitted", request_key="i:0", batch_name="submitting/x")
    done = JobWindow(WindowSpec(1, 540.0, 60.0), state="done", batch_name="")
    out = repointed(_job(w, done), "submitting/x", "batches/b7")
    assert [w.batch_name for w in out.windows] == ["batches/b7", ""] and out.batch_name == "batches/b7"
    assert repointed(out, "submitting/x", "batches/b7") == out


def test_batch_url_guard_covers_the_new_fields():
    batch = TranscriptBatch(name="submitting/x", user_id="u", display_name="https://x.invalid/a",
                            provider_name="https://x.invalid/b")
    assert set(batch_url_fields(batch)) >= {"display_name", "provider_name"}
