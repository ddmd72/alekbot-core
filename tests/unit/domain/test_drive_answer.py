"""The drive specialist's answer, built by code (DRIVE_SPECIALIST_RFC §6, §7; §11.1 "Answer", "Reading lines",
"Pass-through", "drive_context")."""
import pytest

from src.domain import drive_answer_texts as t
from src.domain.drive_answer import compose_answer, reading_lines
from src.domain.drive_line import DriveLine
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_pass_through import PassThroughCollector
from src.domain.drive_read_facts import ReadFacts
from src.domain.drive_report import DriveReport
from src.domain.drive_status import DriveStatus
from src.domain.drive_search_texts import REBUILDING_LINE
from src.domain.user_drive import DriveItem

A = DriveItem(item_id="a", name="a.pdf", path="Inbox/a.pdf", is_folder=False)
B = DriveItem(item_id="b", name="b.pdf", path="Inbox/b.pdf", is_folder=False)
INBOX = DriveItem(item_id="i", name="Inbox", path="Inbox", is_folder=True)


def ok(op, text, items=(), **kw):
    return DriveOutcome(op=op, cls=OutcomeClass.OK, text=text, success=True, items=tuple(items), **kw)


def bad(op, cls, text, success=False, **kw):
    return DriveOutcome(op=op, cls=cls, text=text, success=success, **kw)


def run(*entries, report=DriveReport(), **kw):
    c = PassThroughCollector()
    for step, kind, outcome in entries:
        c.add(step, kind, outcome)
    return compose_answer(c, report, **kw)


SEARCH_CARDS = ok(DriveOp.SEARCH, "REMINDER\n<card a>\n<card b>", [A, B], facts=ReadFacts(cards=2))
SEARCH_ONE = ok(DriveOp.SEARCH, "REMINDER\n<card a>", [A], facts=ReadFacts(cards=1))
OPENED = ok(DriveOp.OPEN, "REMINDER\n<drive_document>lease text</drive_document>", [A], body=True)
LISTING = ok(DriveOp.LIST, "[Drive: Inbox ref=drive:i]\n  [Drive: Inbox/a.pdf ref=drive:a]", [INBOX, A])
MOVED = ok(DriveOp.MOVE, "Moved: Inbox/a.pdf → [Drive: Archive/a.pdf ref=drive:a].", [A])
SAVED_AGAIN = ok(DriveOp.COPY, "Already on the drive: [Drive: Inbox/a.pdf ref=drive:a].", [A])
FOLDER_EXISTED = ok(DriveOp.CREATE_FOLDER, "[Drive: Archive ref=drive:x] It already existed.")
GONE = bad(DriveOp.MOVE, OutcomeClass.NOT_FOUND, "That item is no longer on the drive (deleted or moved out). List again.")


# -- statuses and precedence (§7.2) ----------------------------------------------------------------------------

def test_a_read_with_content_is_ok_answer_from_the_results():
    answer = run((1, t.KIND_SEARCH, SEARCH_CARDS))
    assert answer.status is DriveStatus.OK
    assert answer.text.splitlines()[:2] == ["Drive result: ok", "What to do: Answer from the results below."]


def test_changes_done_are_ok_done_with_one_op_line_each_in_step_order():
    answer = run((1, t.KIND_SEARCH, SEARCH_ONE), (2, "copy", SAVED_AGAIN), (2, "create_folder", FOLDER_EXISTED),
                 (3, "move", MOVED))
    lines = answer.text.splitlines()
    assert lines[:2] == ["Drive result: ok", "What to do: Done."]
    assert lines[2:5] == ["2. done: Already on the drive: [Drive: Inbox/a.pdf ref=drive:a].",
                          "2. done: [Drive: Archive ref=drive:x] It already existed.",
                          "3. done: Moved: Inbox/a.pdf → [Drive: Archive/a.pdf ref=drive:a]."]


def test_still_running_wins_with_todays_text():
    running = bad(DriveOp.DELETE, OutcomeClass.STILL_RUNNING, "The drive operation is still running …", success=True)
    answer = run((1, "move", MOVED), (1, "delete", running), (1, "move", GONE))
    assert answer.status is DriveStatus.STILL_RUNNING
    assert "What to do: The drive operation is still running …" in answer.text


def test_a_cut_operation_is_unfinished_and_keeps_what_ran():
    cut = bad(DriveOp.OPEN, OutcomeClass.NOT_FINISHED, t.DID_NOT_FINISH)
    answer = run((1, "move", MOVED), (2, t.KIND_OPEN, cut))
    assert answer.status is DriveStatus.UNFINISHED
    assert f"What to do: {t.UNFINISHED_LINE}" in answer.text and "1. done: Moved:" in answer.text


def test_a_cut_run_with_nothing_done_is_unfinished():
    assert run(cut=True).status is DriveStatus.UNFINISHED


def test_a_cut_run_is_unfinished_whatever_finished_and_keeps_what_ran():
    """§7.2: the run ended without the specialist's report (deadline, engine exception, turn limit). Smart must not
    read "ok" for a commission whose later steps never ran; the blocks and op lines stay."""
    answer = run((1, t.KIND_SEARCH, SEARCH_ONE), (2, "move", MOVED), cut=True)
    assert answer.status is DriveStatus.UNFINISHED
    assert f"What to do: {t.UNFINISHED_LINE}" in answer.text
    assert "2. done: Moved:" in answer.text and "[Result r1, step 1: search]" in answer.text


def test_a_cut_run_with_only_reads_finished_is_unfinished_not_ok():
    answer = run((1, t.KIND_SEARCH, SEARCH_ONE), cut=True)
    assert answer.status is DriveStatus.UNFINISHED and "<card a>" in answer.text


def test_a_report_keeps_todays_precedence_when_the_run_was_not_cut():
    assert run((1, "move", MOVED), report=DriveReport(note="Moved it.")).status is DriveStatus.OK


def test_some_done_some_not_is_partial():
    answer = run((1, "move", MOVED), (1, "move", GONE))
    assert answer.status is DriveStatus.PARTIAL
    assert "What to do: Only part was done:" in answer.text
    assert "1. not done: That item is no longer on the drive" in answer.text


def test_none_done_takes_the_first_failed_changes_class_and_text():
    refused = bad(DriveOp.MOVE, OutcomeClass.REFUSED, "A file with that name already exists there.")
    answer = run((1, "move", refused), (1, "move", GONE))
    assert answer.status is DriveStatus.REFUSED
    assert "What to do: A file with that name already exists there." in answer.text


def test_a_text_tool_without_its_item_is_not_possible():
    missing = bad(DriveOp.SAVE_TEXT, OutcomeClass.NOT_POSSIBLE, t.NO_ITEM.format(value="t3", known="t1"))
    answer = run((1, "save_text", missing))
    assert answer.status is DriveStatus.NOT_POSSIBLE
    assert f"What to do: {t.NOT_ATTACHED}" in answer.text


def test_a_fixed_correctable_error_is_not_an_outcome():
    """It does not reach the status or "What to do" (its op line stays, see the next tests)."""
    wrong = bad(DriveOp.SAVE_TEXT, OutcomeClass.NOT_POSSIBLE, "No file_contents item 't9'")
    saved = ok(DriveOp.SAVE_TEXT, "Saved [Drive: Inbox/n.md ref=drive:n].")
    answer = run((1, "save_text", wrong), (2, "save_text", saved))
    assert answer.status is DriveStatus.OK and "t9" not in answer.text.splitlines()[1]


def test_a_fixed_change_error_keeps_its_op_line_and_leaves_the_status():
    """§7.2 (parity): Smart sees every failed call today. A settled correctable error is out of the status only."""
    wrong = bad(DriveOp.SAVE_TEXT, OutcomeClass.NOT_POSSIBLE, "No file_contents item 't9'")
    saved = ok(DriveOp.SAVE_TEXT, "Saved [Drive: Inbox/n.md ref=drive:n].")
    answer = run((1, "save_text", wrong), (2, "save_text", saved))
    lines = answer.text.splitlines()
    assert answer.status is DriveStatus.OK and lines[1] == "What to do: Done."
    assert "1. not done: No file_contents item 't9'" in lines
    assert "2. done: Saved [Drive: Inbox/n.md ref=drive:n]." in lines


def test_a_failed_change_of_one_target_is_not_hidden_by_a_later_change_of_another():
    """The case the heuristic cannot tell from a correction: a.pdf failed in step 1, b.pdf moved in step 2."""
    chat_file = bad(DriveOp.MOVE, OutcomeClass.REQUEST_ERROR, "'a.pdf' is a chat file, not on the user's drive")
    answer = run((1, "move", chat_file), (2, "move", MOVED))
    lines = answer.text.splitlines()
    assert "1. not done: 'a.pdf' is a chat file, not on the user's drive" in lines
    assert "2. done: Moved: Inbox/a.pdf → [Drive: Archive/a.pdf ref=drive:a]." in lines


def test_a_fixed_read_error_is_still_dropped_from_the_blocks():
    shape = bad(DriveOp.SEARCH, OutcomeClass.REQUEST_ERROR, "kind 'zip' is not a kind")
    answer = run((1, t.KIND_SEARCH, shape), (2, t.KIND_SEARCH, SEARCH_ONE))
    assert "zip" not in answer.text and answer.status is DriveStatus.OK


def test_an_error_in_the_same_batch_is_not_fixed_by_its_siblings():
    wrong = bad(DriveOp.SAVE_TEXT, OutcomeClass.NOT_POSSIBLE, "No file_contents item 't9'")
    saved = ok(DriveOp.SAVE_TEXT, "Saved [Drive: Inbox/n.md ref=drive:n].")
    assert run((1, "save_text", wrong), (1, "save_text", saved)).status is DriveStatus.PARTIAL


def test_an_unfixed_request_error_on_a_change_is_refused():
    shape = bad(DriveOp.MOVE, OutcomeClass.REQUEST_ERROR, "Give a destination folder, a new name, or both")
    assert run((1, "move", shape)).status is DriveStatus.REFUSED


def test_a_question_without_changes_is_ambiguous():
    answer = run((1, t.KIND_SEARCH, SEARCH_CARDS), report=DriveReport(question="Which lease: a.pdf or b.pdf?"))
    assert answer.status is DriveStatus.AMBIGUOUS
    assert "What to do: Which lease: a.pdf or b.pdf?" in answer.text and "<card b>" in answer.text


def test_a_question_cannot_override_a_failure_or_a_done_change():
    q = DriveReport(question="Which one?")
    assert run((1, "move", GONE), report=q).status is DriveStatus.NOT_FOUND
    partial = run((1, "move", MOVED), report=q)
    assert partial.status is DriveStatus.PARTIAL and "What to do: Only part was done: Which one?" in partial.text


def test_not_done_after_a_change_is_partial_and_names_it():
    answer = run((1, "move", MOVED), report=DriveReport(not_done="restoring a deleted file"))
    assert answer.status is DriveStatus.PARTIAL
    assert "Only part was done: Not done: restoring a deleted file" in answer.text


def test_not_done_after_reads_only_is_not_possible():
    deleted = ok(DriveOp.SEARCH, "REMINDER\nnothing deleted matches")
    answer = run((1, t.KIND_DELETED_SEARCH, deleted), report=DriveReport(not_done="restoring a deleted file"))
    assert answer.status is DriveStatus.NOT_POSSIBLE
    assert "What to do: Nothing was done: restoring a deleted file" in answer.text


def test_a_run_with_no_operation_and_no_question_is_not_possible():
    assert "What to do: Nothing was done: no drive operation ran" in run().text
    assert run(report=DriveReport(note="I could not tell what to do.")).text.splitlines()[1] == (
        "What to do: Nothing was done: I could not tell what to do.")


def test_a_connection_state_only_when_no_operation_ran():
    none_ran = run(drive_line=DriveLine.NOT_CONNECTED)
    assert none_ran.status is DriveStatus.NOT_CONNECTED
    assert "Ask them to connect it in the Cabinet." in none_ran.text
    ran = run((1, t.KIND_SEARCH, SEARCH_ONE), drive_line=DriveLine.NOT_CONNECTED)
    assert ran.status is DriveStatus.OK and "Cabinet" not in ran.text
    assert run(drive_line=DriveLine.NOT_CONFIGURED).text.splitlines()[:2] == [
        "Drive result: failed", "What to do: The drive is not configured."]


def test_reads_only_without_content_take_the_most_informative_failure():
    off = bad(DriveOp.SEARCH, OutcomeClass.INDEXING_OFF, "Drive search is off for this user", success=True)
    gone = bad(DriveOp.OPEN, OutcomeClass.NOT_FOUND, "That item is no longer on the drive")
    answer = run((1, t.KIND_OPEN, gone), (1, t.KIND_SEARCH, off))
    assert answer.status is DriveStatus.INDEXING_OFF
    assert answer.text.splitlines()[1] == "What to do: Drive search is off for this user"


def test_a_search_state_line_only_when_a_search_ran():
    rebuilding = bad(DriveOp.SEARCH, OutcomeClass.REBUILDING, f"REMINDER\nenvelope\n{REBUILDING_LINE}", success=True)
    with_search = run((1, t.KIND_SEARCH, rebuilding), (2, "move", MOVED))
    assert with_search.text.splitlines()[2] == REBUILDING_LINE
    save_only = run((1, "copy", SAVED_AGAIN))
    assert REBUILDING_LINE not in save_only.text


# -- pass-through (§6) --------------------------------------------------------------------------------------------

def test_search_and_open_both_reach_the_orchestrator_verbatim_in_order():
    answer = run((1, t.KIND_SEARCH, SEARCH_CARDS), (2, t.KIND_OPEN, OPENED))
    text = answer.text
    assert "[Result r1, step 1: search]\nREMINDER\n<card a>\n<card b>" in text
    assert "[Result r2, step 2: open]\nREMINDER\n<drive_document>lease text</drive_document>" in text
    assert text.index("[Result r1") < text.index("[Result r2")


def test_two_searches_both_reach_and_byte_identical_repeats_are_dropped():
    other = ok(DriveOp.SEARCH, "REMINDER\n<card b>", [B], facts=ReadFacts(cards=1))
    answer = run((1, t.KIND_SEARCH, SEARCH_ONE), (2, t.KIND_SEARCH, other), (3, t.KIND_SEARCH, SEARCH_ONE))
    assert answer.text.count("<card a>") == 1 and "<card b>" in answer.text
    assert "[Result r3" not in answer.text


def test_a_listing_before_changes_is_labelled():
    answer = run((1, t.KIND_LISTING, LISTING), (2, "move", MOVED), (3, "move", MOVED), (4, "move", MOVED))
    assert "[Result r1, step 1: listing, before the changes in steps 2–4]" in answer.text
    one = run((1, t.KIND_LISTING, LISTING), (2, "move", MOVED))
    assert "[Result r1, step 1: listing, before the changes in step 2]" in one.text


def test_files_are_a_list_in_run_order_and_drive_context_is_not_deduplicated():
    img1 = ok(DriveOp.OPEN, "attached 1", [A], file_data={"path": "/tmp/1.jpg", "mime_type": "image/jpeg"})
    img2 = ok(DriveOp.OPEN, "attached 2", [B], file_data={"path": "/tmp/2.pdf", "mime_type": "application/pdf"})
    answer = run((1, t.KIND_SEARCH, SEARCH_CARDS), (2, t.KIND_OPEN, img1), (2, t.KIND_OPEN, img2))
    assert [f["path"] for f in answer.files] == ["/tmp/1.jpg", "/tmp/2.pdf"]
    assert [e["ref"] for e in answer.drive_context] == ["drive:a", "drive:b", "drive:a", "drive:b"]


def test_the_note_is_capped_and_comes_after_the_reading_lines():
    answer = run((1, t.KIND_SEARCH, SEARCH_ONE), report=DriveReport(note="x" * 500))
    note_line = next(line for line in answer.text.splitlines() if line.startswith(t.NOTE_PREFIX))
    assert len(note_line) == len(t.NOTE_PREFIX) + t.NOTE_MAX_CHARS
    lines = answer.text.splitlines()
    assert max(i for i, line in enumerate(lines) if line.startswith(t.READING_PREFIX)) < lines.index(note_line)


def test_a_plain_text_reply_is_taken_as_the_note():
    assert DriveReport.of(None, " I searched for the lease. ").note == "I searched for the lease."
    assert DriveReport.of({"note": "n", "question": 3}).question == ""


# -- reading lines (§7.3) ------------------------------------------------------------------------------------------

def _blocks(*outcomes, kind=t.KIND_SEARCH):
    c = PassThroughCollector()
    for o in outcomes:
        c.add(1, kind, o)
    return list(c.results)


FIND_LINES = [t.R_FIND, t.R_TIERS, t.R_TRUST, t.R_RELATED, t.R_COVERAGE, t.R_MANY, t.R_ALL]


@pytest.mark.parametrize("facts, expected", [
    (ReadFacts(cards=1), FIND_LINES),
    (ReadFacts(cards=1, passage=True), FIND_LINES + [t.R_PASSAGE]),
    (ReadFacts(cards=1, accuracy=True), FIND_LINES + [t.R_ACCURACY, t.R_ACCURACY_FIND, t.R_ACCURACY_VALUE]),
    (ReadFacts(cards=1, scan=True), FIND_LINES + [t.R_SCAN]),
    (ReadFacts(cards=1, inaccessible=True), FIND_LINES + [t.R_NO_ACCESS]),
    (ReadFacts(cards=1, image=True), FIND_LINES + [t.R_PHOTOS]),
    (ReadFacts(cards=2), FIND_LINES + [t.R_VERSIONS]),
    (ReadFacts(cards=1, coverage_incomplete=True), FIND_LINES + [t.R_BUILDING, t.R_NOT_SEARCHED, t.R_FOLDER_CARD]),
    (ReadFacts(cards=1, building=True), FIND_LINES + [t.R_BUILDING, t.R_NOT_SEARCHED, t.R_FOLDER_CARD]),
])
def test_each_trigger_adds_exactly_its_lines(facts, expected):
    assert reading_lines(_blocks(ok(DriveOp.SEARCH, "cards", facts=facts))) == expected


def test_a_not_found_search_adds_only_the_nothing_found_lines():
    miss = bad(DriveOp.SEARCH, OutcomeClass.NOT_FOUND, "No matching files or folders.", success=True)
    assert reading_lines(_blocks(miss)) == [t.R_BUILDING, t.R_NOT_SEARCHED, t.R_FOLDER_CARD]


def test_an_opened_file_adds_the_document_lines_and_a_listing_adds_none():
    assert reading_lines(_blocks(OPENED, kind=t.KIND_OPEN)) == [t.R_NUMBERS, t.R_DOC_VS_MEMORY, t.R_NOT_READ]
    assert reading_lines(_blocks(LISTING, kind=t.KIND_LISTING)) == []
    link = ok(DriveOp.OPEN, "REMINDER\nLink to the original recording: https://x", [A])
    assert reading_lines(_blocks(link, kind=t.KIND_LINK)) == []


def test_an_open_that_delivered_no_content_adds_no_document_lines():
    """M2: the HEIC "cannot be viewed yet" answer and the "video sent to the chat" answer are successes without a body
    or a file; the numbers / document-vs-memory lines would tell Smart to check figures that were never shown."""
    heic = ok(DriveOp.OPEN, "A HEIC photo cannot be viewed yet.", [A])
    video = ok(DriveOp.OPEN, "The video was sent to the user's chat.", [A])
    assert reading_lines(_blocks(heic, kind=t.KIND_OPEN)) == []
    assert reading_lines(_blocks(video, kind=t.KIND_OPEN)) == []


def test_an_opened_vision_file_adds_the_document_lines():
    image = ok(DriveOp.OPEN, "Opened [Drive: Inbox/a.pdf]", [A], file_data={"path": "/tmp/a.pdf", "mime_type": "application/pdf"})
    assert reading_lines(_blocks(image, kind=t.KIND_OPEN)) == [t.R_NUMBERS, t.R_DOC_VS_MEMORY, t.R_NOT_READ]


def test_an_access_note_on_a_failed_open_adds_the_bot_cannot_open_line():
    note = bad(DriveOp.OPEN, OutcomeClass.REFUSED, "The file exists, but the bot cannot open it",
               facts=ReadFacts(inaccessible=True))
    assert reading_lines(_blocks(note, kind=t.KIND_OPEN)) == [t.R_NO_ACCESS]


def test_reading_lines_are_rendered_with_their_prefix():
    answer = run((1, t.KIND_SEARCH, SEARCH_ONE))
    assert f"Reading: {t.R_FIND}" in answer.text and answer.reading == tuple(FIND_LINES)
