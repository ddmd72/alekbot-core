"""T-R27: a short answer that finished cleanly (STOP) is resubmitted once, then accepted (the audio ends in silence);
MAX_TOKENS, an unknown finish and invalid output keep the MAX_ATTEMPTS rule."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.domain.drive_index_params import MAX_ATTEMPTS
from src.domain.drive_transcript import JobWindow, WindowSpec
from src.domain.drive_transcript_job import is_repurchase
from src.domain.drive_transcript_plan import TranscriptContext, WindowAction, WindowCheck, window_failed

CTX = TranscriptContext(now=datetime(2026, 10, 21, tzinfo=timezone.utc), enabled=True, month_usd=0.0,
                        reserved_usd=0.0, cap_usd=20.0)


def _win(**kw):
    return JobWindow(spec=WindowSpec(index=0, start_s=0.0, duration_s=137.0), state="submitted", **kw)


def test_first_short_stop_answer_is_resubmitted_uncharged():
    r = window_failed(_win(), WindowCheck.COVERAGE, CTX, "STOP")
    assert r.action is WindowAction.RESUBMIT
    assert (r.window.state, r.window.coverage_resubmits, r.window.attempts) == ("uploaded", 1, 0)
    assert is_repurchase(r.window)


def test_second_short_stop_answer_is_accepted_as_the_transcript():
    r = window_failed(_win(coverage_resubmits=1), WindowCheck.COVERAGE, CTX, "STOP")
    assert r.action is WindowAction.ACCEPT
    assert r.window.state == "done" and not r.window.gap


@pytest.mark.parametrize("finish", ["MAX_TOKENS", "", "OTHER"])
def test_short_answer_without_a_clean_stop_is_charged_as_before(finish):
    w = _win()
    for n in range(1, MAX_ATTEMPTS):
        r = window_failed(w, WindowCheck.COVERAGE, CTX, finish)
        assert r.action is WindowAction.RESUBMIT and r.window.attempts == n and r.window.coverage_resubmits == 0
        w = r.window
    r = window_failed(w, WindowCheck.COVERAGE, CTX, finish)
    assert r.action is WindowAction.GAP and r.window.gap


def test_second_short_answer_that_hit_max_tokens_is_not_accepted():
    r = window_failed(_win(coverage_resubmits=1), WindowCheck.COVERAGE, CTX, "MAX_TOKENS")
    assert r.action is WindowAction.RESUBMIT and r.window.attempts == 1


@pytest.mark.parametrize("finish", ["STOP", "MAX_TOKENS"])
def test_invalid_output_keeps_the_attempts_rule_whatever_the_finish(finish):
    w = _win()
    for _ in range(MAX_ATTEMPTS - 1):
        w = window_failed(w, WindowCheck.INVALID, CTX, finish).window
    r = window_failed(w, WindowCheck.INVALID, CTX, finish)
    assert r.action is WindowAction.GAP and r.window.coverage_resubmits == 0


def test_coverage_resubmits_survive_the_job_document():
    from src.adapters.firestore_drive_index_repo import _window_from_doc, _window_to_doc
    w = _win(coverage_resubmits=1)
    assert _window_from_doc(_window_to_doc(w)).coverage_resubmits == 1
    assert _window_from_doc({**_window_to_doc(w), "coverage_resubmits": None or 0}).coverage_resubmits == 0
