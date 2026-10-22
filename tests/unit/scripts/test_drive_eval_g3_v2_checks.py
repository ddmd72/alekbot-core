"""G3 under Search v2 (USER_DRIVE_INDEXING_RFC §25.7): each case run counts its searches, opens and folder listings;
hard checks with no tolerance — the incident-shaped case makes at most 2 searches, 2 opens and 1 listing and names the
right files; a time-span case puts the span in the date fields; after a result with a D29 line Smart searches again
at most once; no case run crawls (more than NO_CRAWL_DRIVE_CALLS drive reads). The answer check is scored as a rate."""
import json
from pathlib import Path

import pytest

from scripts.validation.drive_eval.smart_dry_run import (
    NO_CRAWL_DRIVE_CALLS, G3Error, answer_ok, drive_counts, hard_failures, score, validate_g3,
)

_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "drive_eval"


def _call(intent, failed=False, d29=False, **context):
    return {"intent": intent, "context": context, "route": "fixture", "failed": failed, "d29": d29}


def search(**kw):
    return _call("search_files_in_drive", **kw)


def open_(ref="drive:x"):
    return _call("open_file_from_drive", file_ref=ref)


def listing():
    return _call("list_files_in_drive", folder="Travel")


def _row(calls, **case):
    return {"id": case.pop("id", "c"), "set": "positive", "category": "find", "repeat": 0, "calls": calls,
            "answer": case.pop("answer", ""), **case}


def test_drive_counts_split_searches_opens_and_listings():
    calls = [search(drive_query="rome"), search(failed=True), open_(), _call("open_file", file_ref="drive:y"),
             _call("open_file", file_ref="chat:z"), listing(), _call("search_web", query="rome")]
    assert drive_counts(calls) == {"searches": 1, "opens": 2, "listings": 1}   # a refused search never ran


@pytest.mark.parametrize("calls, fails", [
    ([search(drive_query="rome"), open_(), open_()], False),
    ([search(drive_query="rome"), search(drive_query="рим"), search(drive_query="roma")], True),
    ([search(drive_query="rome"), open_(), open_(), open_()], True),
    ([search(drive_query="rome"), listing(), listing()], True),
])
def test_the_incident_case_is_bounded(calls, fails):
    row = _row(calls, incident=True, answer="Your hotel booking and 12 photos",
               answer_must_include=[["booking", "hotel"]])
    assert bool(hard_failures([row])) is fails


def test_the_incident_case_must_name_the_right_files():
    row = _row([search(drive_query="rome")], incident=True, answer="Nothing about Rome was found.",
               answer_must_include=[["booking", "hotel"], ["photo"]])
    assert any("answer" in f for f in hard_failures([row]))


def test_a_time_span_case_needs_the_span_in_the_date_fields():
    good = _row([search(drive_query="receipts", drive_date_from="2026-02-01", drive_date_to="2026-05-31")],
                time_span=True)
    bad = _row([search(drive_query="receipts February to May 2026")], time_span=True)
    assert hard_failures([good]) == []
    assert any("date fields" in f for f in hard_failures([bad]))


def test_after_a_d29_line_smart_searches_again_at_most_once():
    once = _row([search(drive_query="dog vaccination", d29=True), search(drive_query="vaccination")])
    twice = _row([search(drive_query="dog vaccination", d29=True), search(drive_query="vaccination", d29=True),
                  search(drive_query="dog")])
    assert hard_failures([once]) == []
    assert any("D29" in f for f in hard_failures([twice]))


def test_no_case_run_crawls():
    calls = [search(drive_query="a")] + [open_(f"drive:{i}") for i in range(NO_CRAWL_DRIVE_CALLS)]
    assert any("crawl" in f for f in hard_failures([_row(calls)]))


@pytest.mark.parametrize("answer, groups, ok", [
    ("I found your hotel booking (Бронь отеля.docx) and twelve photos.", [["booking", "бронь"], ["photo"]], True),
    ("I found twelve photos.", [["booking", "бронь"], ["photo"]], False),
    ("Нашёл БРОНЬ отеля", [["бронь"]], True),
    ("anything", [], True),
])
def test_answer_ok_needs_one_of_each_group(answer, groups, ok):
    assert answer_ok(answer, groups) is ok


def test_the_answer_rate_is_scored_over_the_cases_that_set_a_check():
    rows = [dict(_row([], answer="hotel booking", answer_must_include=[["booking"]]), searched=True,
                 answer_hit=True),
            dict(_row([], answer="no", answer_must_include=[["booking"]]), searched=True, answer_hit=False),
            dict(_row([]), searched=True)]
    table = score(rows)
    assert table["answer"]["runs"] == 2 and table["answer"]["rate"] == 0.5


@pytest.mark.datafile
def test_the_v2_gold_is_valid_and_carries_the_hard_check_cases():
    gold = json.loads((_DIR / "gold_g3.json").read_text(encoding="utf-8"))
    v2 = json.loads((_DIR / "gold_g3_v2.json").read_text(encoding="utf-8"))
    validate_g3(gold + v2)
    assert any(c.get("incident") for c in v2)
    assert any(c.get("time_span") for c in v2)
    assert any(c.get("d29") for c in v2)
    assert all(c.get("answer_must_include") for c in v2 if c.get("incident"))


@pytest.mark.datafile
def test_validate_refuses_malformed_v2_fields():
    base = json.loads((_DIR / "gold_g3.json").read_text(encoding="utf-8"))
    bad = {"id": "x", "set": "negative", "category": "web", "lang": "en", "text": "hi", "expect": "none",
           "incident": True, "answer_must_include": "booking"}
    with pytest.raises(G3Error) as err:
        validate_g3(base + [bad])
    assert "incident" in str(err.value) and "answer_must_include" in str(err.value)


async def test_the_dispatch_marks_a_search_whose_result_has_a_d29_line():
    from scripts.validation.drive_eval.smart_dry_run import DryRunDispatch
    from src.infrastructure.delegation_engine import ToolResult

    class Call:
        name = "delegate_to_specialist"

        def __init__(self, query):
            self.args = {"intent": "search_files_in_drive", "context": {"drive_query": query}}

    async def fixture_dispatch(engine, tool_call, context, *a, **kw):
        text = 'No file holds "vaccination".' if "vaccination" in tool_call.args["context"]["drive_query"] else "ok"
        return ToolResult(name=tool_call.name, result_str=text)

    dispatch = DryRunDispatch(fixture_dispatch, fixture=None, read_only=frozenset())
    await dispatch(None, Call("dog vaccination"), {})
    await dispatch(None, Call("dog"), {})
    assert dispatch.calls[0].get("d29") is True and "d29" not in dispatch.calls[1]
