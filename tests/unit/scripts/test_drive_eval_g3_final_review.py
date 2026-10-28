"""G3 after the final review (USER_DRIVE_INDEXING_RFC §17.8, §24.11; final review F5-6).

- only a successful search counts as searched: a refused call (no words, a bad filter) is not a search;
- `date_in_query` holds only when the case's ISO date is one of drive_query's exact terms: a dropped date is a miss;
- the negatives have a bar too (the Wilson upper bound of the recorded negative rate), and bars without it are refused;
- a case run over SEARCH_CALL_CEILING search calls is reported in search_discipline, never a hard failure (owner,
  2026-10-28, specialist era);
- a case run as a user whose drive search is off searches at most once (no retry after the off result)."""
import json
from pathlib import Path

import pytest

from scripts.validation.drive_eval.smart_dry_run import (
    SEARCH_CALL_CEILING, AsEvalUser, G3Error, expectation_met, hard_failures, judge, make_g3_bars, score, search_discipline,
    searched, validate_g3, wilson_upper_bound,
)
from src.domain.drive_search import STATUS_DISABLED, DriveSearchRequest

_GOLD = Path(__file__).resolve().parents[2] / "fixtures" / "drive_eval" / "gold_g3.json"


def _search(failed=False, **args):
    """A drive search in the drive operation log; a refused one (no words, a bad filter) is A's `request_error`."""
    return {"op": "search", "args": args, "outcome": "request_error" if failed else "ok", "stubbed": False}


def test_a_refused_search_is_not_a_search():
    assert not searched([_search(failed=True, query="")])
    assert searched([_search(failed=True), _search(query="lease")])


@pytest.mark.parametrize("calls, met", [
    ([_search(query="счёт 15.02.2026")], True),
    ([_search(query="счёт до 2026-02-15")], True),
    ([_search(query="счёт")], False),                                   # the date was dropped
    ([_search(query="счёт 16.02.2026")], False),                        # another date
    ([_search(failed=True, query="счёт 15.02.2026")], False),           # refused: never ran
    ([_search(query="счёт 15.02.2026"), _search(query="счёт", date_to="2026-02-15")], False),
])
def test_date_in_query_needs_the_cases_date_among_the_query_terms(calls, met):
    assert expectation_met("date_in_query", calls, "2026-02-15") is met


def test_every_committed_dated_case_names_its_date():
    gold = json.loads(_GOLD.read_text(encoding="utf-8"))
    validate_g3(gold)
    dated = {c["id"]: c.get("date") for c in gold if c["expect"] == "date_in_query"}
    assert dated["p-invoice-due-date"] == "2026-02-15" and dated["p-lease-starts-march"] == "2026-03-01"
    assert sum(1 for c in gold if c.get("drive") == "off") == 1


def _valid():
    return ([{"id": f"p{i}", "set": "positive", "category": "find", "lang": "en", "text": "q", "expect": "none"}
             for i in range(10)]
            + [{"id": f"n{i}", "set": "negative", "category": "web", "lang": "en", "text": "q", "expect": "none"}
               for i in range(10)])


@pytest.mark.parametrize("mutate, message", [
    (lambda g: [{**g[0], "expect": "date_in_query", "date": "15.02.2026"}] + g[1:], "date must be YYYY-MM-DD"),
    (lambda g: [{**g[0], "date": "2026-02-15"}] + g[1:], "date only on a date_in_query case"),
    (lambda g: [{**g[0], "drive": "maybe"}] + g[1:], "drive must be 'off'"),
    (lambda g: g[:10] + [{**g[10], "drive": "off"}] + g[11:], "a drive-off case must be positive"),
])
def test_malformed_date_and_drive_fields_are_refused(mutate, message):
    validate_g3(_valid())
    with pytest.raises(G3Error, match=message):
        validate_g3(mutate(_valid()))


def _table(neg_hits, neg_runs=10):
    return {"positive": {"runs": 10, "searched": 10, "rate": 1.0},
            "negative": {"runs": neg_runs, "searched": neg_hits, "rate": neg_hits / neg_runs},
            "dated": {"runs": 10, "searched": 10, "rate": 1.0}}


def test_the_negative_rate_has_a_bar():
    bars = make_g3_bars(_table(1), "g", "m", 3, "now")
    assert bars["negative_bar"] == pytest.approx(wilson_upper_bound(1, 10))
    assert judge(_table(2), bars, "g", "m", repeats=3) == "pass"          # within the bound
    assert judge(_table(6), bars, "g", "m", repeats=3) == "regression"    # Smart searches the drive for chit-chat


def test_bars_without_the_negative_bar_are_refused():
    bars = make_g3_bars(_table(1), "g", "m", 3, "now")
    del bars["negative_bar"]
    with pytest.raises(G3Error, match="no negative_bar"):
        judge(_table(1), bars, "g", "m", repeats=3)


def _row(ops, **kw):
    return {"id": "c1", "repeat": 0, "calls": [], "ops": ops, **kw}


def test_more_search_calls_than_the_ceiling_is_reported_not_failed():
    at = [_row([_search(query="a")] * SEARCH_CALL_CEILING)]
    over = [_row([_search(query="a")] * (SEARCH_CALL_CEILING + 1))]
    assert hard_failures(at) == [] and hard_failures(over) == []
    assert search_discipline(at)["cases_over_search_ceiling"] == 0
    assert search_discipline(over)["cases_over_search_ceiling"] == 1


def test_a_repeated_search_after_drive_search_was_off_is_a_hard_failure():
    assert hard_failures([_row([_search(query="a")], drive_off=True)]) == []
    assert hard_failures([_row([_search(query="a"), _search(query="b")], drive_off=True)])


async def test_the_eval_user_can_be_switched_off():
    class _Search:
        async def search(self, user_id, request):
            raise AssertionError("an off user's search never reaches the index")

    user = AsEvalUser(_Search())
    user.off = True
    result = await user.search("owner", DriveSearchRequest(query="lease"))
    assert result.status == STATUS_DISABLED


def test_score_reads_searched_from_rows_as_before():
    table = score([{"set": "negative", "category": "web", "searched": False}])
    assert table["negative"]["rate"] == 0.0
