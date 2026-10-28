"""G3 after review E (USER_DRIVE_INDEXING_RFC §17.8, §24.11; E-M3 and its minors).

Every case says where its date belongs (`expect`: date_in_query | date_filter | none); the harness scores that from
the fields of Smart's actual `search_files_in_drive` calls as its own metric. Bars are Wilson lower confidence bounds
of the recorded rates (one LLM sample is noisy), and a run is judged only against bars recorded with the same
number of repeats."""
import json
import math
from pathlib import Path

import pytest

from scripts.validation.drive_eval.smart_dry_run import (
    EXPECTATIONS, WILSON_Z, G3Error, expectation_met, judge, make_g3_bars, score, validate_g3, wilson_lower_bound,
)

_GOLD = Path(__file__).resolve().parents[2] / "fixtures" / "drive_eval" / "gold_g3.json"


def _case(i, kind="positive", category="find", expect="none"):
    return {"id": f"c{i}", "set": kind, "category": category, "lang": "en", "text": "question", "expect": expect}


def _valid():
    return [_case(i) for i in range(10)] + [_case(i + 10, "negative", "web") for i in range(10)]


def test_every_committed_case_says_where_its_date_belongs():
    gold = {c["id"]: c for c in json.loads(_GOLD.read_text(encoding="utf-8"))}
    validate_g3(list(gold.values()))
    assert {c["expect"] for c in gold.values()} == set(EXPECTATIONS)
    assert gold["p-invoice-due-date"]["expect"] == "date_in_query"
    assert gold["p-contracts-january"]["expect"] == "date_filter"
    assert {c["expect"] for c in gold.values() if c["set"] == "negative"} == {"none"}


@pytest.mark.parametrize("mutate, message", [
    (lambda g: [{k: v for k, v in g[0].items() if k != "expect"}] + g[1:], "c0: expect must be one of"),
    (lambda g: [{**g[0], "expect": "maybe"}] + g[1:], "c0: expect must be one of"),
])
def test_a_case_without_a_known_expectation_is_refused(mutate, message):
    validate_g3(_valid())
    with pytest.raises(G3Error, match=message):
        validate_g3(mutate(_valid()))


def _search(**args):
    """A drive search in the drive operation log (dry_run_drive.DriveOpLog): the specialist's `search_drive` call."""
    return {"op": "search", "args": args, "outcome": "ok", "stubbed": False}


_OTHER = {"op": "list_folder", "args": {"folder": "Inbox"}, "outcome": "ok", "stubbed": False}   # a drive read, not a search


@pytest.mark.parametrize("expect, calls, met", [
    ("date_filter", [_search(query="contract", date_from="2026-01-01")], True),
    ("date_filter", [_search(date_to="2026-01-31")], True),
    ("date_filter", [_search(query="contract January 2026")], False),
    ("date_filter", [_OTHER], False),
    ("date_in_query", [_search(query="счёт 15.02.2026")], True),
    ("date_in_query", [_search(query="счёт", date_from="2026-02-15", date_to="2026-02-15")], False),
    ("date_in_query", [_search(query="счёт 15.02.2026"), _search(query="счёт", date_to="2026-02-15")],
     False),
    ("date_in_query", [_search(query=" ")], False),
    ("date_in_query", [_OTHER], False),
    ("none", [_OTHER], True),
    ("none", [], True),
    ("none", [_search(query="Porto", date_from="")], True),
    ("none", [_search(query="Porto", date_to="2026-03-01")], False),
])
def test_expectations_are_read_from_the_search_calls_fields(expect, calls, met):
    assert expectation_met(expect, calls) is met


def test_the_expectation_rate_is_its_own_metric():
    rows = [{"set": "positive", "category": "content", "searched": True, "expect": "date_in_query", "expect_hit": True},
            {"set": "positive", "category": "find", "searched": True, "expect": "date_filter", "expect_hit": False},
            {"set": "negative", "category": "web", "searched": False, "expect": "none", "expect_hit": True}]
    table = score(rows)
    assert table["expect"] == {"runs": 3, "searched": 2, "rate": 2 / 3}
    assert table["expect:date_filter"]["rate"] == 0.0 and table["expect:date_in_query"]["rate"] == 1.0


@pytest.mark.parametrize("hits, runs, expected", [(0, 0, 0.0), (10, 10, 0.7225), (5, 10, 0.2366), (0, 10, 0.0)])
def test_wilson_lower_bound(hits, runs, expected):
    assert WILSON_Z == 1.96
    assert math.isclose(wilson_lower_bound(hits, runs), expected, abs_tol=1e-4)


def _table(pos_hits, pos_runs, exp_hits, exp_runs, dated=None):
    """`dated`: (hits, runs) over the date cases only, the scope the date bar is over (R2 S-m3); `expect` is pooled."""
    table = {"positive": {"runs": pos_runs, "searched": pos_hits, "rate": pos_hits / pos_runs},
             "negative": {"runs": 10, "searched": 1, "rate": 0.1},
             "expect": {"runs": exp_runs, "searched": exp_hits, "rate": exp_hits / exp_runs}}
    if dated is not None:
        table["dated"] = {"runs": dated[1], "searched": dated[0], "rate": dated[0] / dated[1]}
    return table


def test_the_bars_are_lower_confidence_bounds_of_the_recorded_rates():
    bars = make_g3_bars(_table(10, 10, 5, 10), "g", "m", 3, "now")
    assert bars["positive_rate"] == 1.0 and math.isclose(bars["positive_bar"], 0.7225, abs_tol=1e-4)
    assert bars["expect_rate"] == 0.5 and math.isclose(bars["expect_bar"], 0.2366, abs_tol=1e-4)
    assert bars["wilson_z"] == WILSON_Z and bars["repeats"] == 3


def test_a_run_below_the_recorded_rate_but_within_the_bound_passes():
    # the date bar is over the date cases (R2 S-m3): the pooled `expect` rate is held perfect and decides nothing
    bars = make_g3_bars(_table(10, 10, 10, 10, dated=(5, 10)), "g", "m", 3, "now")
    assert judge(_table(8, 10, 10, 10, dated=(3, 10)), bars, "g", "m", repeats=3) == "pass"
    assert judge(_table(7, 10, 10, 10, dated=(5, 10)), bars, "g", "m", repeats=3) == "regression"
    assert judge(_table(10, 10, 10, 10, dated=(2, 10)), bars, "g", "m", repeats=3) == "regression"


def test_bars_from_another_number_of_repeats_give_no_verdict():
    bars = make_g3_bars(_table(10, 10, 5, 10), "g", "m", 3, "now")
    with pytest.raises(G3Error, match="recorded with --repeats 3, this run has 1"):
        judge(_table(10, 10, 5, 10), bars, "g", "m", repeats=1)


def test_bars_without_a_lower_bound_give_no_verdict():
    bars = {"gold_sha256": "g", "smart_model": "m", "repeats": 3, "positive_rate": 1.0}
    with pytest.raises(G3Error, match="no positive_bar"):
        judge(_table(10, 10, 5, 10), bars, "g", "m", repeats=3)
