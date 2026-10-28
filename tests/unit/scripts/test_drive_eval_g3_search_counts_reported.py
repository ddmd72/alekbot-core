"""G3, specialist era (owner, 2026-10-28): search counts are reported metrics, not hard failures. Crawling (opens plus
listings), drive-off searching and the incident case's opens and listings stay hard."""
from scripts.validation.drive_eval.smart_dry_run import (
    NO_CRAWL_DRIVE_CALLS, hard_failures, search_discipline, search_discipline_lines,
)


def _op(op, outcome="ok", d29=False, **args):
    return {"op": op, "args": args, "outcome": outcome, "stubbed": False, **({"d29": True} if d29 else {})}


def _search(d29=False, query="rome"):
    return _op("search", d29=d29, query=query)


def _row(ops, **case):
    return {"id": "c", "set": "positive", "category": "find", "repeat": 0, "calls": [], "ops": ops,
            "answer": case.pop("answer", ""), **case}


def test_six_parallel_searches_are_reported_not_failed():
    rows = [_row([_search(query=f"w{i}") for i in range(6)])]
    assert hard_failures(rows) == []
    report = search_discipline(rows)
    assert report["cases_over_search_ceiling"] == 1 and report["total_searches"] == 6
    assert any("<- over" in line for line in search_discipline_lines(report))


def test_four_searches_after_a_d29_line_are_reported_not_failed():
    rows = [_row([_search(d29=True)] + [_search(query=f"s{i}") for i in range(4)])]
    assert hard_failures(rows) == []
    report = search_discipline(rows)
    assert report["research_after_d29"] == 4 and report["cases_over_d29_research_max"] == 1


def test_seven_opens_still_fail_as_crawl():
    rows = [_row([_search()] + [_op("open", ref=f"drive:{i}") for i in range(NO_CRAWL_DRIVE_CALLS + 1)])]
    assert any("crawled" in f for f in hard_failures(rows))


def test_searches_do_not_count_toward_the_crawl_bar():
    rows = [_row([_search(query=f"w{i}") for i in range(NO_CRAWL_DRIVE_CALLS)] + [_op("open", ref="drive:x")])]
    assert hard_failures(rows) == []


def test_an_incident_case_with_three_opens_still_fails():
    rows = [_row([_search()] + [_op("open", ref=f"drive:{i}") for i in range(3)], incident=True,
                 answer="booking", answer_must_include=[["booking"]])]
    assert any("incident case over its ceilings" in f for f in hard_failures(rows))


def test_an_incident_case_over_its_search_ceiling_alone_is_reported():
    rows = [_row([_search(query=f"w{i}") for i in range(3)] + [_op("open", ref="drive:x")], incident=True,
                 answer="booking", answer_must_include=[["booking"]])]
    assert hard_failures(rows) == []
    assert search_discipline(rows)["cases"][0]["over"] is True


def test_drive_off_with_a_search_still_fails():
    rows = [_row([_search(), _search(query="again")], drive_off=True)]
    assert any("drive search was off" in f for f in hard_failures(rows))
