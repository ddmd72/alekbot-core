"""The job document's window bookkeeping (USER_DRIVE_INDEXING_RFC §27.4-§27.5; plan Task 12, G35/T-R15, T-R16)."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.drive_index_params import MEDIA_PASS_MAX_WINDOWS, TRANSCRIBE_MAX_SECONDS
from src.domain.drive_transcript import JobWindow, TranscriptJob, WindowSpec
from src.domain.drive_transcript_job import (
    job_plan, ready_to_submit, request_key, resettled, resubmits, submitted, windows_to_extract, with_uploaded,
)
from src.domain.drive_transcript_plan import file_estimate_usd
from src.domain.drive_transcript_windows import plan_windows

NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
LIVE = NOW + timedelta(hours=47)


def _job(duration=5000.0, windows=(), reserved=1.0):
    return TranscriptJob(user_id="u", connection_id="u", item_id="i", content_key="k", duration_s=duration, windows=tuple(windows),
                         reserved_usd=reserved, reserved_month="2026-10")


def _w(index, state="uploaded", expires=LIVE, **kw):
    spec = plan_windows(5000.0)[index] if index < 3 else WindowSpec(index, index * 1740.0, 1800.0)
    return JobWindow(spec=spec, state=state, file_ref=f"files/{index}", files_expire_at=expires, **kw)


def test_unknown_duration_plans_the_worst_case():
    assert job_plan(_job(duration=0.0)) == plan_windows(float(TRANSCRIBE_MAX_SECONDS))


def test_known_mode_asks_for_every_missing_window():
    job = _job(windows=[_w(1)])
    assert [s.index for s in windows_to_extract(job, NOW, pass_mode=False)] == [0, 2]


def test_pass_mode_asks_for_one_contiguous_slice():
    job = _job(duration=0.0, windows=[_w(1)])
    assert [s.index for s in windows_to_extract(job, NOW, pass_mode=True)] == [0]
    job = _job(duration=0.0, windows=[_w(0)])
    assert [s.index for s in windows_to_extract(job, NOW, pass_mode=True)] == list(range(1, 1 + MEDIA_PASS_MAX_WINDOWS))


def test_a_window_about_to_expire_or_sent_back_is_extracted_again():
    soon = _w(0, expires=NOW + timedelta(hours=2))
    back = _w(1, state="extracted")
    failed = _w(2, state="failed")
    assert [s.index for s in windows_to_extract(_job(windows=[soon, back, failed]), NOW, pass_mode=False)] == [0, 1, 2]
    done = [_w(0, state="done"), _w(1, state="submitted"), _w(2)]
    assert windows_to_extract(_job(windows=done), NOW, pass_mode=False) == ()


def test_with_uploaded_keeps_the_counters_of_a_window_sent_back():
    old = _w(1, state="extracted", attempts=2, request_key="i:1", reupload_used=True, batch_name="batches/b")
    job = with_uploaded(_job(windows=[old, _w(0)]), WindowSpec(1, 1740.0, 1800.0), "files/new", LIVE)
    one = job.windows[1]
    assert [w.spec.index for w in job.windows] == [0, 1]
    assert (one.state, one.file_ref, one.attempts, one.request_key, one.reupload_used, one.batch_name) == (
        "uploaded", "files/new", 2, "i:1", True, "")


def test_ready_to_submit_needs_every_window_and_a_known_duration():
    assert not ready_to_submit(_job(windows=[_w(0), _w(1)]), NOW)
    assert ready_to_submit(_job(windows=[_w(0), _w(1), _w(2)]), NOW)
    assert not ready_to_submit(_job(duration=0.0, windows=[_w(0), _w(1), _w(2)]), NOW)
    assert not ready_to_submit(_job(windows=[_w(0, "submitted"), _w(1, "submitted"), _w(2, "done")]), NOW)


def test_resubmits_are_uploaded_windows_that_were_in_a_batch():
    job = _job(windows=[_w(0, request_key="i:0"), _w(1), _w(2, "submitted", request_key="i:2")])
    assert [w.spec.index for w in resubmits(job)] == [0]


def test_submitted_names_the_batch_and_the_keys():
    job = submitted(_job(windows=[_w(0), _w(1, "done", request_key="i:1")]), "batches/b1")
    assert [(w.state, w.request_key, w.batch_name) for w in job.windows] == [
        ("submitted", request_key("i", 0), "batches/b1"), ("done", "i:1", "")]
    assert job.batch_name == "batches/b1"


def test_resettled_follows_the_real_duration_and_never_raises_the_reservation():
    worst = file_estimate_usd(float(TRANSCRIBE_MAX_SECONDS))
    job = _job(duration=0.0, windows=[_w(0), _w(1), _w(3)], reserved=worst)
    out = resettled(job, 2500.0)
    assert out.job.duration_s == 2500.0 and [w.spec.index for w in out.job.windows] == [0, 1]
    assert [w.spec.index for w in out.dropped] == [3]
    assert out.job.reserved_usd == pytest.approx(file_estimate_usd(2500.0))
    assert out.release_usd == pytest.approx(worst - file_estimate_usd(2500.0))
    low = resettled(replace(job, reserved_usd=0.01), 2500.0)
    assert low.release_usd == 0.0 and low.job.reserved_usd == 0.01
