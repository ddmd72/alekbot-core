"""Recorded recall bars are floored to 6 places: a bar never exceeds the run that set it (so the same run passes its
own bars), and the bars file holds no long digit runs."""
import re

from scripts.validation.drive_eval.metrics import make_bars, regressions


def test_bars_are_floored_and_the_recording_run_passes_them():
    table = {"overall": {"cases": 9, "hits": 8, "recall": 8 / 9}, "date": {"cases": 3, "hits": 2, "recall": 2 / 3}}
    bars = make_bars(table, [], 8, "space", "2026-10-19T00:00:00Z", "t", "g")
    assert bars["recall"] == {"overall": 0.888888, "date": 0.666666}
    assert regressions(table, bars) == []
    assert not re.search(r"\d{8,}", repr(bars["recall"]))
