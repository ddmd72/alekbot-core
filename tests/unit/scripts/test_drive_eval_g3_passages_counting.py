"""G3 scoring after passages (plan S3 follow-up): `find_passages` is a drive lookup (rates, ceilings, the drive-off check),
its `question` is the query a date is read from, and a run that only found passages is not judged on date fields; the
eval's drive-off switch stubs the passages reads as production's gate does."""
import pytest

from scripts.validation.drive_eval import smart_dry_run as g3
from scripts.validation.drive_eval.runner import EVAL_USER
from scripts.validation.drive_eval.specialist_fixture import EvalUserSearch
from src.domain.drive_search import STATUS_DISABLED


def _op(op, outcome="ok", **args):
    return {"op": op, "args": args, "outcome": outcome, "stubbed": False}


def test_a_passages_lookup_counts_as_a_search_for_the_rates_and_the_calls():
    ops = [_op("find_passages", question="how many reviewers")]
    assert g3.searched(ops) and g3.search_calls(ops) == 1 and g3.drive_counts(ops)["searches"] == 1
    assert not g3.searched([_op("find_passages", "request_error", question="")])
    assert not g3.searched([_op("read_passages", file_ref="drive:x", passages="1-3")])   # a read of known passages
    assert g3.search_calls([_op("find_passages", question="a"), _op("search", query="b")]) == 2


def test_the_date_in_the_question_meets_a_date_in_query_expectation():
    ops = [_op("find_passages", question="invoice of 2026-09-15")]
    assert g3.expectation_met("date_in_query", ops, "2026-09-15")
    assert not g3.expectation_met("date_in_query", ops, "2026-09-16")
    assert not g3.expectation_met("date_in_query", [_op("find_passages", "request_error", question="2026-09-15")],
                                  "2026-09-15")


def test_a_passages_only_run_is_recognised_and_a_run_with_a_search_is_not():
    assert g3.passages_only([_op("find_passages", question="x")])
    assert not g3.passages_only([_op("find_passages", question="x"), _op("search", query="x")])
    assert not g3.passages_only([_op("search", query="x")]) and not g3.passages_only([])
    assert not g3.passages_only([_op("find_passages", "failed", question="x")])


def test_a_passages_lookup_has_no_date_fields_so_it_cannot_satisfy_a_date_filter():
    assert not g3.expectation_met("date_filter", [_op("find_passages", question="last year")])
    assert g3.expectation_met("none", [_op("find_passages", question="x")])


def _row(ops, **kw):
    return {"id": "c", "repeat": 0, "ops": ops, "calls": [], **kw}


def test_the_drive_off_check_counts_passages_lookups():
    two = [_op("search", query="a"), _op("find_passages", question="a")]
    assert any("drive search was off" in f for f in g3.hard_failures([_row(two, drive_off=True)]))
    assert g3.hard_failures([_row(two[:1], drive_off=True)]) == []


def test_a_time_span_case_answered_from_passages_is_not_failed_for_missing_date_fields():
    only = [_op("find_passages", question="receipts from last year")]
    assert g3.hard_failures([_row(only, time_span=True)]) == []
    searched = [_op("search", query="receipts")]
    assert any("time span" in f for f in g3.hard_failures([_row(searched, time_span=True)]))


def test_the_rates_use_the_passages_lookup():
    rows = [{"set": "positive", "category": "content", "searched": g3.searched([_op("find_passages", question="x")])},
            {"set": "positive", "category": "content", "searched": g3.searched([])}]
    assert g3.score(rows)["positive:content"]["searched"] == 1


# -- the drive-off switch -------------------------------------------------------------------------------------------------

class _Real:
    def __init__(self):
        self.calls = []

    async def excerpts(self, user_id, request, **kw):
        self.calls.append(("excerpts", user_id))
        return "real-excerpts"

    async def excerpt_range(self, user_id, request):
        self.calls.append(("excerpt_range", user_id))
        return "real-range"


@pytest.mark.parametrize("method", ["excerpts", "excerpt_range"])
async def test_a_drive_off_case_gets_the_disabled_answer_and_never_the_real_index(method):
    real = _Real()
    user = EvalUserSearch(real)
    user.off = True
    result = await getattr(user, method)("owner", "REQ")
    assert result.status == STATUS_DISABLED and result.request == "REQ" and real.calls == []


@pytest.mark.parametrize("method,expected", [("excerpts", "real-excerpts"), ("excerpt_range", "real-range")])
async def test_with_the_switch_on_the_passages_reads_run_as_the_eval_user(method, expected):
    real = _Real()
    user = EvalUserSearch(real)
    assert await getattr(user, method)("owner", "REQ") == expected
    assert real.calls == [(method, EVAL_USER)]
