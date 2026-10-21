"""Collection helpers of the job bookkeeping (USER_DRIVE_INDEXING_RFC §27.5-§27.6; plan Task 13; G37, T-R11, T-R18): poll
cadence, the window checks in order, re-purchase classification, failed-batch and gone-file rules, and stitching."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.drive_index_params import (
    BATCH_POLL_EVERY_S, BATCH_POLL_SLOW_S, BATCH_SLOW_AFTER_S, TRANSCRIBE_FILE_LIFE_AT_SUBMIT,
)
from src.domain.drive_transcript import JobWindow, Segment, TranscriptBatch, TranscriptJob, WindowSpec, WindowTranscript
from src.domain.drive_transcript_job import (
    after_failed_batch, all_done, check_window, file_gone, in_batch, interpolated_share, is_repurchase, poll_after,
    split_request_key, stitch_job,
)
from src.domain.drive_transcript_plan import WindowCheck
from src.domain.drive_transcript_windows import plan_windows

T = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
SPEC = WindowSpec(0, 0.0, 600.0)


def _wt(*segs, no_speech=False, langs=("uk",)):
    return WindowTranscript(langs, 0, no_speech, tuple(segs))


def _even(duration, word="w"):
    starts = [float(s) for s in range(0, int(duration * 0.95), 50)] + [duration * 0.95]
    return _wt(*(Segment(s, None, f"{word} {i} x") for i, s in enumerate(starts)))


def test_split_request_key_is_the_inverse_of_request_key():
    assert split_request_key("ABC!12:3") == ("ABC!12", 3)
    assert split_request_key("a:b:10") == ("a:b", 10)
    for bad in ("nokey", ":3", "a:", "a:x"):
        with pytest.raises(ValueError):
            split_request_key(bad)


def test_poll_after_is_every_five_minutes_then_slow():
    b = TranscriptBatch(name="batches/1", user_id="u", submitted_at=T)
    assert poll_after(b, T + timedelta(minutes=10)) == T + timedelta(minutes=10, seconds=BATCH_POLL_EVERY_S)
    late = T + timedelta(seconds=BATCH_SLOW_AFTER_S)
    assert poll_after(b, late) == late + timedelta(seconds=BATCH_POLL_SLOW_S)
    assert poll_after(replace(b, submitted_at=None), T) is not None


def test_checks_in_order_time_then_coverage_then_interpolation():
    assert check_window(SPEC, _even(600)) is None
    assert check_window(SPEC, _wt(no_speech=True)) is None
    past = _wt(Segment(0.0, None, "a"), Segment(300.0, None, "b"), Segment(700.0, None, "c"))
    assert check_window(SPEC, past) is WindowCheck.TIME
    assert check_window(SPEC, _wt(Segment(0.0, None, "a"), Segment(100.0, None, "b"))) is WindowCheck.COVERAGE
    assert check_window(SPEC, _wt()) is WindowCheck.COVERAGE   # no segments and not no_speech: a truncated answer
    sparse = _wt(Segment(0.0, None, "One a. Two b. Three c. Four d. Five e. Six f. Seven g."),
                 Segment(560.0, None, "end"))
    assert interpolated_share(SPEC, sparse) > 0.3
    assert check_window(SPEC, sparse) is WindowCheck.INTERPOLATION


def test_interpolated_share_uses_the_window_end():
    """With the spec, the last segment's span runs to the window's end: a long last turn is split too."""
    last = _wt(Segment(0.0, None, "a"), Segment(540.0, None, "One a. Two b. Three c."))
    assert interpolated_share(WindowSpec(0, 0.0, 1_800.0), last) > 0


def test_is_repurchase_counts_only_collection_sendbacks():
    w = JobWindow(SPEC, state="submitted", request_key="i:0")
    assert not is_repurchase(w) and not is_repurchase(replace(w, batch_resubmits=1, reupload_used=True))
    assert is_repurchase(replace(w, attempts=1))
    assert is_repurchase(replace(w, time_resubmits=1)) and is_repurchase(replace(w, interp_resubmits=1))


def test_after_failed_batch_once_and_file_life():
    fresh = JobWindow(SPEC, state="submitted", file_ref="files/1", request_key="i:0", batch_name="batches/b",
                      files_expire_at=T + timedelta(hours=40))
    back = after_failed_batch(fresh, T)
    assert (back.state, back.request_key, back.batch_resubmits, back.file_ref, back.batch_name) == (
        "uploaded", "", 1, "files/1", "")
    assert after_failed_batch(replace(fresh, attempts=1), T).request_key == "i:0"   # a re-purchase stays one
    assert (after_failed_batch(replace(fresh, batch_resubmits=1), T).state,
            after_failed_batch(replace(fresh, batch_resubmits=1), T).gap) == ("done", True)
    short = replace(fresh, files_expire_at=T + TRANSCRIBE_FILE_LIFE_AT_SUBMIT - timedelta(minutes=1))
    again = after_failed_batch(short, T)
    assert (again.state, again.file_ref, again.reupload_used) == ("extracted", "", True)
    spent = after_failed_batch(replace(short, reupload_used=True), T)
    assert (spent.state, spent.gap) == ("done", True)


def test_file_gone_reextracts_once():
    w = file_gone(JobWindow(SPEC, state="uploaded", file_ref="files/1", attempts=1, request_key="i:0"))
    assert (w.state, w.file_ref, w.reupload_used, w.attempts, w.request_key) == ("extracted", "", True, 1, "i:0")


def test_in_batch_and_all_done():
    job = TranscriptJob("u", "i", "k", duration_s=3_600.0, windows=(
        JobWindow(plan_windows(3_600)[0], state="done"),
        JobWindow(plan_windows(3_600)[1], state="submitted", batch_name="batches/b"),
        JobWindow(plan_windows(3_600)[2], state="submitted", batch_name="batches/c")))
    assert [w.spec.index for w in in_batch(job, "batches/b")] == [1]
    assert not all_done(job)
    done = replace(job, windows=tuple(replace(w, state="done") for w in job.windows))
    assert all_done(done)
    assert not all_done(replace(done, windows=done.windows[:2]))   # a planned window missing
    assert not all_done(replace(done, duration_s=0.0))


def test_stitch_job_gaps_speakers_seams_languages():
    specs = plan_windows(3_600)
    job = TranscriptJob("u", "i", "k", duration_s=3_600.0, windows=(
        JobWindow(specs[0], state="done"), JobWindow(specs[1], state="done", gap=True),
        JobWindow(specs[2], state="done")))
    results = {0: _wt(Segment(0.0, 1, "hello there friend"), Segment(30.0, 2, "yes indeed"), langs=("uk",)),
               1: _wt(Segment(0.0, 1, "ignored, the window is a gap")),
               2: _wt(Segment(10.0, 1, "last words now"), langs=("en", "uk"))}
    st = stitch_job(job, results)
    gaps = [s for s in st.segments if s.gap]
    assert len(gaps) == 1 and st.truncated
    cut = (specs[0].start_s + specs[0].duration_s + specs[1].start_s) / 2
    assert gaps[0].start_s == pytest.approx(cut)   # placed at its seam cut, so the cut keeps it
    assert st.language_codes == ("uk", "en") and st.duration_s == 3_600.0
    assert "ignored" not in " ".join(s.text for s in st.segments)
    assert st.speakers >= 2 and st.uncertain_seam   # nothing matched across the gap's seams


def test_stitch_job_interpolates_with_specs_and_marks_a_missing_answer_as_gap():
    spec = WindowSpec(0, 0.0, 600.0)
    job = TranscriptJob("u", "i", "k", duration_s=600.0, windows=(JobWindow(spec, state="done"),))
    long_turn = _wt(Segment(0.0, None, "One a. Two b. Three c. Four d."), Segment(500.0, None, "end"))
    st = stitch_job(job, {0: long_turn})
    assert any(s.interpolated for s in st.segments) and not st.truncated and st.speakers == 1
    missing = stitch_job(job, {})
    assert missing.truncated and [s.gap for s in missing.segments] == [True]
