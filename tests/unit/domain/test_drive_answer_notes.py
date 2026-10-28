"""Note changes in the drive specialist's answer (DRIVE_SPECIALIST_RFC §7, §9.1, §9.3 A7/A8; design §4.3, Δ7, Δ16): a
note change is an op line; its receipt and its history line come from the collector on every exit, a cut run and a
partial commission included; the tool list without notes is today's. Synthetic data only."""
import pytest

from src.domain import drive_answer_texts as t
from src.domain import drive_tool_texts as tt
from src.domain.agent import DeliveryItem
from src.domain.drive_answer import compose_answer
from src.domain.drive_note import DRIVE_NOTE_ERASE_DELIVERY, DRIVE_NOTE_RECEIPT_DELIVERY
from src.domain.drive_note_tool_texts import NOTE_STILL_RUNNING, drive_note_tool_declarations
from src.domain.drive_op import MUTATING_OPS, DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_pass_through import PassThroughCollector
from src.domain.drive_read_facts import ReadFacts
from src.domain.drive_report import DriveReport
from src.domain.drive_status import DriveStatus

SAVED = "Note saved on Home/bathroom-invoice.pdf (version 1). The system posts the stored note word for word."
CONTEXT = "[Drive note: Home/bathroom-invoice.pdf ref=drive:a v1]"
RECEIPT = DeliveryItem(type=DRIVE_NOTE_RECEIPT_DELIVERY, data={"path": "Home/bathroom-invoice.pdf", "version": 1})
ERASE = DeliveryItem(type=DRIVE_NOTE_ERASE_DELIVERY, data={"command": "$drive-notes erase c0de00"})


def _note(text: str = SAVED, *, context=(CONTEXT,), delivery=(RECEIPT,), op=DriveOp.NOTE_WRITE) -> DriveOutcome:
    return DriveOutcome(op=op, cls=OutcomeClass.OK, text=text, success=True, note_context=tuple(context),
                        delivery=tuple(delivery))


def _collector(*outcomes, step: int = 1) -> PassThroughCollector:
    collector = PassThroughCollector()
    for outcome in outcomes:
        collector.add(step, outcome.op.value, outcome)
    return collector


def test_note_writes_and_erases_are_changes_and_a_note_read_is_not():
    assert {DriveOp.NOTE_WRITE, DriveOp.NOTE_ERASE} <= MUTATING_OPS and DriveOp.NOTE_READ not in MUTATING_OPS
    assert _note().is_change and not _note(op=DriveOp.NOTE_READ).is_change


def test_a_note_change_is_an_op_line_and_its_receipt_and_history_line_reach_the_answer():
    answer = compose_answer(_collector(_note()), DriveReport(note="Saved the note."))
    assert answer.status is DriveStatus.OK
    assert f"1. done: {SAVED}" in answer.text.splitlines()
    assert answer.note_context == [CONTEXT] and answer.delivery_items == [RECEIPT]
    assert answer.drive_context == []          # a note change touches no drive item's refs


def test_the_collector_keeps_every_note_line_and_receipt_in_run_order_without_dedup():
    second = _note(context=(CONTEXT,), delivery=(RECEIPT, ERASE))
    collector = _collector(_note(), second)
    assert collector.note_context() == [CONTEXT, CONTEXT]
    assert collector.delivery_items() == [RECEIPT, RECEIPT, ERASE]


def test_a_cut_run_still_carries_the_receipt_of_a_note_it_stored():
    still = DriveOutcome(op=DriveOp.NOTE_WRITE, cls=OutcomeClass.STILL_RUNNING, text=NOTE_STILL_RUNNING, success=True)
    for collector, status in ((_collector(_note()), DriveStatus.UNFINISHED),
                              (_collector(_note(), still), DriveStatus.STILL_RUNNING)):
        answer = compose_answer(collector, DriveReport(), cut=True)
        assert answer.status is status
        assert answer.delivery_items == [RECEIPT] and answer.note_context == [CONTEXT]
    answer = compose_answer(_collector(_note(), still), DriveReport(), cut=True)
    assert f"What to do: {NOTE_STILL_RUNNING}" in answer.text and f"1. still running: {NOTE_STILL_RUNNING}" in answer.text


def test_a_partial_commission_still_carries_the_receipt():
    failed = DriveOutcome(op=DriveOp.MOVE, cls=OutcomeClass.NOT_FOUND, text="That item is no longer on the drive.",
                          success=False)
    answer = compose_answer(_collector(_note(), failed), DriveReport())
    assert answer.status is DriveStatus.PARTIAL
    assert answer.delivery_items == [RECEIPT] and answer.note_context == [CONTEXT]


def test_a_refused_note_change_is_a_not_done_line_with_no_receipt():
    refused = DriveOutcome(op=DriveOp.NOTE_WRITE, cls=OutcomeClass.REFUSED,
                           text="The note was blocked by the security check. Nothing was stored.", success=False)
    answer = compose_answer(_collector(refused), DriveReport())
    assert answer.status is DriveStatus.REFUSED and answer.delivery_items == [] and answer.note_context == []
    assert "1. not done: The note was blocked by the security check. Nothing was stored." in answer.text


def test_a_correctable_note_error_fixed_in_a_later_step_keeps_its_line_but_not_the_status():
    collector = PassThroughCollector()
    collector.add(1, "replace_note_text", DriveOutcome(op=DriveOp.NOTE_WRITE, cls=OutcomeClass.REQUEST_ERROR,
                                                       text="Read the note with get_note first.", success=False))
    collector.add(2, "replace_note_text", _note())
    answer = compose_answer(collector, DriveReport())
    assert answer.status is DriveStatus.OK
    assert "1. not done: Read the note with get_note first." in answer.text and f"2. done: {SAVED}" in answer.text


def test_get_note_is_a_read_block_under_its_own_kind():
    read = DriveOutcome(op=DriveOp.NOTE_READ, cls=OutcomeClass.OK, text="The owner's note on Home/bathroom.pdf",
                        success=True, facts=ReadFacts(owner_note=True))
    collector = PassThroughCollector()
    collector.add(1, t.KIND_NOTE, read)
    answer = compose_answer(collector, DriveReport())
    assert answer.status is DriveStatus.OK and f"What to do: {t.ANSWER_FROM_RESULTS}" in answer.text
    assert "[Result r1, step 1: note]\nThe owner's note on Home/bathroom.pdf" in answer.text
    assert answer.delivery_items == []


def test_drive_tool_declarations_without_extra_is_today_s_list():
    assert tt.drive_tool_declarations() == tt.drive_tool_declarations(())
    assert [d["name"] for d in tt.drive_tool_declarations()] == [
        tt.SEARCH_DRIVE, tt.SEARCH_DELETED, tt.LIST_FOLDER, tt.OPEN_DRIVE_FILE, tt.GET_RECORDING_LINK,
        tt.COPY_FILE_TO_DRIVE, tt.SAVE_TEXT_FILE, tt.CREATE_FOLDER, tt.MOVE_FILE, tt.APPEND_TO_FILE,
        tt.REPLACE_FILE_CONTENT, tt.DELETE_DRIVE_ITEM, tt.REPORT_TOOL]


@pytest.mark.parametrize("writes", [True, False], ids=["owner_turn", "other_turn"])
def test_the_note_tools_go_after_the_drive_tools_and_before_the_terminal_tool(writes):
    notes = drive_note_tool_declarations(writes=writes)
    names = [d["name"] for d in tt.drive_tool_declarations(notes)]
    plain = [d["name"] for d in tt.drive_tool_declarations()]
    assert names == plain[:-1] + [d["name"] for d in notes] + [tt.REPORT_TOOL]
