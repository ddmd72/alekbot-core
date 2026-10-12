"""Review fixes (rev_40 F1–F3) for the drive eval's recall bars (USER_DRIVE_INDEXING_RFC §17.8)."""
from scripts.validation.drive_eval.metrics import (
    OVERALL, CaseOutcome, bar_mismatch, lost_cases, make_bars, recall, regressions,
)

STAMP = "2026-10-11T00:00:00+00:00"
TREE, GOLD = "t" * 64, "g" * 64


def _bars(outcomes):
    return make_bars(recall(outcomes), outcomes, 8, "fake/8", STAMP, TREE, GOLD)


def test_one_lost_case_of_ten_is_a_regression_no_tolerance():
    """F1: 10 exact hits recorded, one lost (1.0 -> 0.9). Strictly below the bar, so it regresses."""
    before = [CaseOutcome(f"ex{i}", "exact", True) for i in range(10)]
    bars = _bars(before)
    after = [CaseOutcome(o.case_id, o.case, o.case_id != "ex3") for o in before]
    assert {r["scope"] for r in regressions(recall(after), bars)} == {"exact", OVERALL}
    assert lost_cases(after, bars) == ["ex3"]


def test_a_case_that_missed_when_recorded_is_not_lost():
    """F2: a known miss that still misses is not 'lost since the bars were recorded'."""
    bars = _bars([CaseOutcome("1", "exact", False), CaseOutcome("2", "exact", True)])
    assert bars["cases"] == {"1": False, "2": True}
    assert lost_cases([CaseOutcome("1", "exact", False), CaseOutcome("2", "exact", True)], bars) == []
    assert lost_cases([CaseOutcome("1", "exact", False), CaseOutcome("2", "exact", False)], bars) == ["2"]


def test_bars_without_an_overall_recall_cannot_judge_the_run():
    """F3: a hand-edited or badly merged bars file with no recall map must not pass at any recall."""
    bars = _bars([CaseOutcome("1", "exact", True)])
    assert bar_mismatch(bars, 8, TREE, GOLD, ["1"]) == []
    for broken in ({**bars, "recall": {}}, {k: v for k, v in bars.items() if k != "recall"},
                   {**bars, "recall": {"exact": 1.0}}):
        reasons = bar_mismatch(broken, 8, TREE, GOLD, ["1"])
        assert len(reasons) == 1 and OVERALL in reasons[0], reasons
