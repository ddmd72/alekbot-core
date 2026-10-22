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


def _op(op, outcome="ok", d29=False, **args):
    """One drive operation log entry (dry_run_drive.DriveOpLog): reads as they ran; G3's annotate adds `d29`."""
    return {"op": op, "args": args, "outcome": outcome, "stubbed": False, **({"d29": True} if d29 else {})}


def search(failed=False, d29=False, **kw):
    return _op("search", "request_error" if failed else "ok", d29=d29, **kw)   # a refused search: request_error


def open_(ref="drive:x"):
    return _op("open", ref=ref)


def listing():
    return _op("list_folder", folder="Travel")


def _row(calls, **case):
    return {"id": case.pop("id", "c"), "set": "positive", "category": "find", "repeat": 0, "calls": [], "ops": calls,
            "answer": case.pop("answer", ""), **case}


def test_drive_counts_split_searches_opens_and_listings():
    # Smart's open_file on a drive: ref is DriveOperations.open; on a chat file it is fetch_stored, not a drive open;
    # Smart's own search_web is not a drive operation at all
    calls = [search(query="rome"), search(failed=True), open_(), _op("open", ref="drive:y"),
             _op("fetch_stored", ref="chat:z"), listing()]
    assert drive_counts(calls) == {"searches": 1, "opens": 2, "listings": 1}   # a refused search never ran


@pytest.mark.parametrize("calls, fails", [
    ([search(query="rome"), open_(), open_()], False),
    ([search(query="rome"), search(query="рим"), search(query="roma")], True),
    ([search(query="rome"), open_(), open_(), open_()], True),
    ([search(query="rome"), listing(), listing()], True),
])
def test_the_incident_case_is_bounded(calls, fails):
    row = _row(calls, incident=True, answer="Your hotel booking and 12 photos",
               answer_must_include=[["booking", "hotel"]])
    assert bool(hard_failures([row])) is fails


def test_the_incident_case_must_name_the_right_files():
    row = _row([search(query="rome")], incident=True, answer="Nothing about Rome was found.",
               answer_must_include=[["booking", "hotel"], ["photo"]])
    assert any("answer" in f for f in hard_failures([row]))


def test_a_time_span_case_needs_the_span_in_the_date_fields():
    good = _row([search(query="receipts", date_from="2026-02-01", date_to="2026-05-31")],
                time_span=True)
    bad = _row([search(query="receipts February to May 2026")], time_span=True)
    assert hard_failures([good]) == []
    assert any("date fields" in f for f in hard_failures([bad]))


def test_after_a_d29_line_smart_searches_again_at_most_once():
    once = _row([search(query="dog vaccination", d29=True), search(query="vaccination")])
    twice = _row([search(query="dog vaccination", d29=True), search(query="vaccination", d29=True),
                  search(query="dog")])
    assert hard_failures([once]) == []
    assert any("D29" in f for f in hard_failures([twice]))


def test_no_case_run_crawls():
    calls = [search(query="a")] + [open_(f"drive:{i}") for i in range(NO_CRAWL_DRIVE_CALLS)]
    assert any("crawl" in f for f in hard_failures([_row(calls)]))


@pytest.mark.parametrize("answer, groups, ok", [
    ("I found your hotel booking (Бронювання готелю.docx) and twelve photos.", [["booking", "бронь"], ["photo"]], True),
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


def test_the_v2_gold_is_valid_and_carries_the_hard_check_cases():
    gold = json.loads((_DIR / "gold_g3.json").read_text(encoding="utf-8"))
    v2 = json.loads((_DIR / "gold_g3_v2.json").read_text(encoding="utf-8"))
    validate_g3(gold + v2)
    assert any(c.get("incident") for c in v2)
    assert any(c.get("time_span") for c in v2)
    assert any(c.get("d29") for c in v2)
    assert all(c.get("answer_must_include") for c in v2 if c.get("incident"))


def test_validate_refuses_malformed_v2_fields():
    base = json.loads((_DIR / "gold_g3.json").read_text(encoding="utf-8"))
    bad = {"id": "x", "set": "negative", "category": "web", "lang": "en", "text": "hi", "expect": "none",
           "incident": True, "answer_must_include": "booking"}
    with pytest.raises(G3Error) as err:
        validate_g3(base + [bad])
    assert "incident" in str(err.value) and "answer_must_include" in str(err.value)


async def test_the_dispatch_marks_a_search_whose_result_has_a_d29_line():
    """The mark moved from Smart's dispatch to the drive operation log (DryRunDriveOperations' annotate hook)."""
    from unittest.mock import MagicMock

    from scripts.validation.drive_eval.dry_run_drive import DriveOpLog, DryRunDriveOperations
    from scripts.validation.drive_eval.smart_dry_run import annotate_drive_read
    from src.domain.drive_op import DriveOp
    from src.domain.drive_outcome import DriveOutcome
    from src.domain.drive_outcome_class import OutcomeClass
    from src.infrastructure.drive_operations import DriveOperations

    class Searching(DriveOperations):
        async def search(self, context, user_id, *, query=None, folder=None, kind=None, date_from=None, date_to=None):
            text = 'No file holds "vaccination".' if "vaccination" in str(query) else "ok"
            return DriveOutcome(op=DriveOp.SEARCH, cls=OutcomeClass.OK, text=text, success=True)

    log = DriveOpLog()
    ops = Searching(conversion_service=MagicMock())
    with DryRunDriveOperations(log, cls=Searching, annotate=annotate_drive_read):
        await ops.search({}, "u", query="dog vaccination")
        await ops.search({}, "u", query="dog")
    assert log.entries[0].get("d29") is True and "d29" not in log.entries[1]
