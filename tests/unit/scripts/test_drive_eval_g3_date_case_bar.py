"""The G3 date bar covers the date cases only (USER_DRIVE_INDEXING_RFC §17.8, §24.11, arbiter R2 S-m3).

Pooled over every case, the date-expectation rate was carried by the `none` cases (most of the gold, the negatives
among them meet it by not searching), so a full regression of the only `date_filter` case still passed. The bar is now
the Wilson lower bound of the rate over the `date_in_query` and `date_filter` cases together; bars recorded before it
give no verdict."""
import json
from pathlib import Path

import pytest

from scripts.validation.drive_eval.smart_dry_run import (
    DATED_EXPECTATIONS, G3Error, judge, make_g3_bars, score, validate_g3, wilson_lower_bound,
)

_GOLD = Path(__file__).resolve().parents[2] / "fixtures" / "drive_eval" / "gold_g3.json"


def _row(expect, hit, kind="positive"):
    return {"set": kind, "category": "find" if kind == "positive" else "web", "searched": kind == "positive",
            "expect": expect, "expect_hit": hit}


def _gold_shaped(date_filter_hit=True, date_in_query_hits=(True, True)):
    """The committed gold's shape before the fix: 25 `none` cases, one date_filter, two date_in_query."""
    rows = [_row("none", True) for _ in range(13)] + [_row("none", True, "negative") for _ in range(12)]
    rows += [_row("date_filter", date_filter_hit)] + [_row("date_in_query", h) for h in date_in_query_hits]
    return rows


def test_dated_expectations_are_the_two_date_kinds():
    assert DATED_EXPECTATIONS == ("date_in_query", "date_filter")


def test_the_date_scope_holds_only_the_date_cases():
    table = score(_gold_shaped(date_filter_hit=False))
    assert table["dated"] == {"runs": 3, "searched": 2, "rate": 2 / 3}


def test_the_bar_is_the_lower_bound_over_the_date_cases():
    bars = make_g3_bars(score(_gold_shaped()), "g", "m", 3, "now")
    assert bars["date_rate"] == 1.0
    assert bars["date_bar"] == pytest.approx(wilson_lower_bound(3, 3))


def test_a_full_regression_of_the_date_filter_case_fails_the_run():
    bars = make_g3_bars(score(_gold_shaped() * 3), "g", "m", 3, "now")
    regressed = score(_gold_shaped(date_filter_hit=False) * 3)
    assert regressed["expect"]["rate"] > 0.95   # what the pooled rate saw: nearly perfect
    assert judge(regressed, bars, "g", "m", repeats=3) == "regression"


def test_a_run_meeting_the_date_bar_passes_whatever_the_none_cases_do():
    bars = make_g3_bars(score(_gold_shaped() * 3), "g", "m", 3, "now")
    none_slip = [{**r, "expect_hit": False} if r["expect"] == "none" and r["set"] == "positive" and i < 3 else r
                 for i, r in enumerate(_gold_shaped() * 3)]
    assert judge(score(none_slip), bars, "g", "m", repeats=3) == "pass"


def test_bars_recorded_before_the_date_bar_give_no_verdict():
    old = {"gold_sha256": "g", "smart_model": "m", "repeats": 3, "positive_rate": 1.0, "positive_bar": 0.7,
           "expect_rate": 1.0, "expect_bar": 0.8}
    with pytest.raises(G3Error, match="no date_bar.*--record"):
        judge(score(_gold_shaped()), old, "g", "m", repeats=3)


def test_the_committed_gold_has_at_least_six_date_cases_over_both_kinds():
    gold = json.loads(_GOLD.read_text(encoding="utf-8"))
    validate_g3(gold)
    dated = [c for c in gold if c["expect"] in DATED_EXPECTATIONS]
    assert len(dated) >= 6 and all(c["set"] == "positive" for c in dated)
    assert sum(1 for c in dated if c["expect"] == "date_filter") >= 3
    assert sum(1 for c in dated if c["expect"] == "date_in_query") >= 3
    assert all(c.get("note") for c in dated if c["id"] not in {"p-electricity"})   # the owner reviews each answer
