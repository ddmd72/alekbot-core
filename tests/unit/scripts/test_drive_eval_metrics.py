"""Recall@K and recorded bars of the drive eval (USER_DRIVE_INDEXING_RFC §17.8; plan Task 0 hit rule)."""
from scripts.validation.drive_eval.metrics import (
    OVERALL, CaseOutcome, bar_mismatch, case_hit, lost_cases, make_bars, recall, regressions,
)

STAMP = "2026-10-11T00:00:00+00:00"


def _bars(outcomes, k=8, tree="t" * 64, gold="g" * 64):
    return make_bars(recall(outcomes), outcomes, k, "fake/8", STAMP, tree, gold)


def test_case_hit_needs_every_expected_path_or_k_of_them():
    assert case_hit(["a", "b"], {"a", "b", "c"}, 8)
    assert not case_hit(["a", "b"], {"a"}, 8)
    many = [f"p{i}" for i in range(10)]
    assert case_hit(many, set(many[:8]), 8)
    assert not case_hit(many, set(many[:7]), 8)


def test_recall_per_case_type_and_overall():
    table = recall([CaseOutcome("1", "exact", True), CaseOutcome("2", "exact", False),
                    CaseOutcome("3", "date", True)])
    assert table["exact"] == {"cases": 2, "hits": 1, "recall": 0.5}
    assert table["date"]["recall"] == 1.0
    assert table[OVERALL] == {"cases": 3, "hits": 2, "recall": 2 / 3}


def test_bars_pin_k_tree_gold_and_every_case():
    outcomes = [CaseOutcome("1", "exact", True), CaseOutcome("2", "date", False)]
    assert _bars(outcomes) == {
        "k": 8, "embedding_space": "fake/8", "recorded_at": STAMP, "tree_sha256": "t" * 64, "gold_sha256": "g" * 64,
        "recall": {"date": 0.0, "exact": 1.0, OVERALL: 0.5}, "cases": {"1": True, "2": False}}


def test_regressions_compare_against_bars_and_name_lost_cases():
    before = [CaseOutcome("1", "exact", True), CaseOutcome("2", "date", True)]
    bars = _bars(before)
    after = [CaseOutcome("1", "exact", False), CaseOutcome("2", "date", True)]
    assert {r["scope"] for r in regressions(recall(after), bars)} == {"exact", OVERALL}
    assert lost_cases(after, bars) == ["1"]
    assert regressions(recall(before), bars) == []


def test_bars_judge_only_the_run_they_were_recorded_on():
    bars = _bars([CaseOutcome("1", "exact", True), CaseOutcome("2", "date", True)])
    assert bar_mismatch(bars, 8, "t" * 64, "g" * 64, ["1", "2"]) == []
    assert bar_mismatch(bars, 10, "t" * 64, "g" * 64, ["1", "2"]) == ["K changed: bars 8, now 10"]
    assert bar_mismatch(bars, 8, "x" * 64, "g" * 64, ["1", "2"]) == ["tree.json changed since the bars were recorded"]
    (reason,) = bar_mismatch(bars, 8, "t" * 64, "h" * 64, ["1", "3"])
    assert "added ['3']" in reason and "removed ['2']" in reason
