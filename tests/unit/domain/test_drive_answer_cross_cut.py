"""Cross-cut review fixes X4-X7 and X11 on the drive specialist's answer."""
from src.domain import drive_answer_texts as t
from src.domain.drive_answer import compose_answer
from src.domain.drive_line import DriveLine
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_pass_through import PassThroughCollector
from src.domain.drive_read_facts import ReadFacts
from src.domain.drive_report import DriveReport
from src.domain.drive_search import DISABLED_TEXT
from src.domain.drive_search_line import SearchLine
from src.domain.drive_search_texts import REBUILDING_LINE
from src.domain.drive_status import DriveStatus
from src.domain.user_drive import DriveItem

A = DriveItem(item_id="a", name="a.pdf", path="Inbox/a.pdf", is_folder=False)


def run(*entries, report=DriveReport(), **kw):
    c = PassThroughCollector()
    for step, kind, outcome in entries:
        c.add(step, kind, outcome)
    return compose_answer(c, report, **kw)


def out(op, cls, text, success=True, **kw):
    return DriveOutcome(op=op, cls=cls, text=text, success=success, **kw)


NOT_FOUND_SEARCH = out(DriveOp.SEARCH, OutcomeClass.NOT_FOUND, "REMINDER\nNo file holds it.", success=False,
                       facts=ReadFacts())
CARDS = out(DriveOp.SEARCH, OutcomeClass.OK, "REMINDER\n<card>", items=(A,), facts=ReadFacts(cards=1))
MOVED = out(DriveOp.MOVE, OutcomeClass.OK, "Moved: Inbox/a.pdf → Archive/a.pdf.", items=(A,))


def what_to_do(answer):
    return answer.text.splitlines()[1]


def test_x4_a_not_found_search_says_answer_from_the_results_not_the_whole_read_text():
    answer = run((1, t.KIND_SEARCH, NOT_FOUND_SEARCH))
    assert answer.status is DriveStatus.NOT_FOUND
    assert what_to_do(answer) == "What to do: " + t.ANSWER_FROM_RESULTS
    assert answer.text.count("No file holds it.") == 1   # the block only, not the head too


def test_x4_other_read_failures_keep_their_text():
    failed = out(DriveOp.OPEN, OutcomeClass.FAILED, "Could not read it.", success=False)
    assert what_to_do(run((1, t.KIND_OPEN, failed))) == "What to do: Could not read it."


def test_x5_reconnect_needed_with_no_operation_is_todays_not_connected():
    answer = run(drive_line=DriveLine.RECONNECT_NEEDED)
    assert answer.status is DriveStatus.NOT_CONNECTED
    assert what_to_do(answer) == "What to do: The user's drive is not connected or its access expired. " \
                                 "Ask them to connect it in the Cabinet."


def test_x6_a_listing_alone_with_search_off_carries_the_disabled_text():
    listing = out(DriveOp.LIST, OutcomeClass.OK, "[Drive: Inbox ref=drive:i]")
    answer = run((1, t.KIND_LISTING, listing), search_line=SearchLine.OFF)
    assert DISABLED_TEXT in answer.text.splitlines()


def test_x6_a_listing_alone_while_rebuilding_carries_the_rebuilding_line():
    listing = out(DriveOp.LIST, OutcomeClass.OK, "[Drive: Inbox ref=drive:i]")
    assert REBUILDING_LINE in run((1, t.KIND_LISTING, listing), search_line=SearchLine.REBUILDING).text


def test_x6_no_state_line_when_search_is_on_a_search_ran_or_a_change_ran():
    listing = out(DriveOp.LIST, OutcomeClass.OK, "[Drive: Inbox ref=drive:i]")
    assert DISABLED_TEXT not in run((1, t.KIND_LISTING, listing), search_line=SearchLine.ON).text
    assert DISABLED_TEXT not in run((1, t.KIND_LISTING, listing), (2, "move", MOVED),
                                    search_line=SearchLine.OFF).text
    indexing_off = out(DriveOp.SEARCH, OutcomeClass.INDEXING_OFF, DISABLED_TEXT, success=False)
    assert run((1, t.KIND_SEARCH, indexing_off), search_line=SearchLine.OFF).text.count(DISABLED_TEXT) >= 1


def test_x7_the_not_found_trio_fires_for_a_search_but_not_for_the_deleted_search():
    assert t.R_BUILDING in run((1, t.KIND_SEARCH, NOT_FOUND_SEARCH)).reading
    deleted = run((1, t.KIND_DELETED_SEARCH, NOT_FOUND_SEARCH))
    assert not set(deleted.reading) & {t.R_BUILDING, t.R_NOT_SEARCHED, t.R_FOLDER_CARD}


def test_x7_a_lookup_search_before_a_successful_change_adds_no_search_reading_lines():
    answer = run((1, t.KIND_SEARCH, CARDS), (2, "move", MOVED))
    assert answer.status is DriveStatus.OK and answer.reading == ()
    assert "[Result r1, step 1: search]" in answer.text   # the block itself stays
    assert run((1, t.KIND_SEARCH, CARDS)).reading   # a find keeps them


def test_x11_unfinished_tells_smart_not_to_commission_again():
    answer = run((1, t.KIND_SEARCH, CARDS), cut=True)
    assert answer.status is DriveStatus.UNFINISHED
    assert "do not commission the same request again in this turn" in what_to_do(answer)
