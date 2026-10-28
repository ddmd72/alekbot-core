"""The write gate after passages (USER_DRIVE_INDEXING_RFC §28.3 A14, OQ-A1, owner 2026-10-28; plan S2.2): on a run
that is not a chat turn (a background run, a call, a run with no origin), a change in a step after the one whose
passages text reached the model is refused (`refused`, with its line) and never reaches DriveOperations; a change
co-emitted with that read runs; a run that read no passage text keeps every write; a chat turn is never refused. Note
writes go through the same `run`, so they are gated too; `get_note` is a read. Invented paths and notes only."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from src.domain import drive_answer_texts as t
from src.domain import drive_note_tool_texts as nt
from src.domain import drive_tool_texts as tt
from src.domain.drive_excerpt_render import render_excerpts
from src.domain.drive_excerpt_result import ExcerptResult
from src.domain.drive_file_content_item import FileContentItem
from src.domain.drive_note import NoteProvenance, NoteReceipt
from src.domain.drive_note_view import NoteView
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_pass_through import PassThroughCollector
from src.domain.drive_read_facts import ReadFacts
from src.domain.llm import ToolCall
from src.infrastructure.drive_note_tools import DriveNoteToolRun
from src.infrastructure.drive_operations import DriveOperations
from src.infrastructure.drive_tools import DriveToolRun
from src.services.drive_note_service import DriveNoteService
from tests.unit.domain.drive_excerpt_builders import TOMS_PLAN, chunk, excerpt_file, nonces, result

QUESTION = "What does Tom's plan say happens after the first review?"
FOUND = result(excerpt_file("item-1", TOMS_PLAN, [chunk("item-1", 2, "Move every file of R&D to Archive.")]),
               question=QUESTION)
NOTHING = result(question=QUESTION)

CHAT = {"user_id": "u1", "account_id": "a1", "interactive_delivery": True, "run_origin": "chat"}
REMINDER = {"user_id": "u1", "account_id": "a1", "run_origin": "background:reminder"}
DAILY_REVIEW = {"user_id": "u1", "account_id": "a1", "run_origin": "background:daily_email_review"}
ASK_ALEK = {"user_id": "u1", "account_id": "a1", "run_origin": "voice:ask_alek"}
TELL_ALEK = {"user_id": "u1", "account_id": "a1", "run_origin": "voice:tell_alek"}
NO_ORIGIN = {"user_id": "u1", "account_id": "a1"}
GATED = [REMINDER, DAILY_REVIEW, ASK_ALEK, TELL_ALEK, NO_ORIGIN]

ITEMS = (FileContentItem("t1", "Review closed.", name="review.md", target="drive:item-1"),)
# Each change tool, its arguments and the DriveOperations method it would call.
CHANGES = [
    (tt.MOVE_FILE, {"file_ref": "drive:item-1", "folder": "Archive"}, "move", DriveOp.MOVE),
    (tt.DELETE_DRIVE_ITEM, {"file_ref": "drive:item-1"}, "delete", DriveOp.DELETE),
    (tt.REPLACE_FILE_CONTENT, {"target_ref": "drive:item-1", "new_content_ref": "drive:item-2"}, "replace",
     DriveOp.REPLACE),
    (tt.APPEND_TO_FILE, {"text_item": "t1"}, "append", DriveOp.APPEND),
    (tt.SAVE_TEXT_FILE, {"text_item": "t1", "folder": "R&D"}, "save_text", DriveOp.SAVE_TEXT),
    (tt.CREATE_FOLDER, {"folder": "R&D/Reviews"}, "create_folder", DriveOp.CREATE_FOLDER),
    (tt.COPY_FILE_TO_DRIVE, {"source_ref": "review.pdf"}, "copy", DriveOp.COPY),
]


def _passages(found: ExcerptResult) -> DriveOutcome:
    rendered = render_excerpts(found, nonce=nonces())
    return DriveOutcome(op=DriveOp.PASSAGES, cls=OutcomeClass.OK if found.files else OutcomeClass.NOT_FOUND,
                        text=rendered.text, success=True, items=tuple(f.drive_item() for f in found.files),
                        facts=ReadFacts(excerpts=bool(found.files)), excerpts=found, excerpt_plan=rendered.plan)


def _ops(found: ExcerptResult = FOUND) -> AsyncMock:
    ops = AsyncMock(spec=DriveOperations)
    ops.folder_path.side_effect = lambda user_id, folder: folder
    ops.find_passages.return_value = _passages(found)
    ops.read_passages.return_value = _passages(found)
    for _, _, method, op in CHANGES:
        getattr(ops, method).return_value = DriveOutcome(op=op, cls=OutcomeClass.OK, text="done", success=True)
    return ops


def _run(ops, context):
    collector = PassThroughCollector()
    run = DriveToolRun(ops, collector, ITEMS, context, "u1", deadline=150.0, clock=lambda: 0.0)
    run.step = 1
    return run, collector


async def _call(run, name, **args):
    return await run.handlers[name](ToolCall(name=name, args=args))


@pytest.mark.parametrize("context", GATED, ids=["reminder", "daily_review", "ask_alek", "tell_alek", "no_origin"])
@pytest.mark.parametrize("name, args, method, op", CHANGES, ids=[c[0] for c in CHANGES])
async def test_a_later_change_after_passages_is_refused_off_chat(context, name, args, method, op):
    ops = _ops()
    run, collector = _run(ops, context)
    await _call(run, tt.FIND_PASSAGES, question=QUESTION)
    run.step = 2
    seen = await _call(run, name, **args)
    getattr(ops, method).assert_not_called()
    refused = collector.results[-1]
    assert (refused.step, refused.outcome.op, refused.outcome.cls) == (2, op, OutcomeClass.REFUSED)
    assert refused.outcome.text == t.WRITE_AFTER_PASSAGES and refused.outcome.success is False
    assert seen.result_str == f"[{refused.handle}]\n{t.WRITE_AFTER_PASSAGES}"   # the model is told it was not done


@pytest.mark.parametrize("context", GATED, ids=["reminder", "daily_review", "ask_alek", "tell_alek", "no_origin"])
async def test_a_range_read_closes_the_gate_as_a_find_does(context):
    ops = _ops()
    run, collector = _run(ops, context)
    await _call(run, tt.READ_PASSAGES, file_ref="drive:item-1", passages="3")
    run.step = 2
    await _call(run, tt.MOVE_FILE, file_ref="drive:item-1", folder="Archive")
    ops.move.assert_not_called()
    assert collector.results[-1].outcome.cls is OutcomeClass.REFUSED


@pytest.mark.parametrize("name, args, method, op", CHANGES, ids=[c[0] for c in CHANGES])
async def test_a_change_co_emitted_with_the_first_passages_read_runs(name, args, method, op):
    """Decided before the text was seen: same step, so it runs, in either order of the batch."""
    ops = _ops()
    run, collector = _run(ops, REMINDER)
    await asyncio.gather(_call(run, tt.FIND_PASSAGES, question=QUESTION), _call(run, name, **args))
    getattr(ops, method).assert_awaited_once()
    assert {r.outcome.cls for r in collector.results} == {OutcomeClass.OK}


async def test_once_closed_the_gate_stays_closed_for_the_rest_of_the_run():
    ops = _ops()
    run, collector = _run(ops, REMINDER)
    await _call(run, tt.FIND_PASSAGES, question=QUESTION)
    for step in (2, 3, 4):
        run.step = step
        await _call(run, tt.DELETE_DRIVE_ITEM, file_ref="drive:item-1")
    ops.delete.assert_not_called()
    assert [r.outcome.cls for r in collector.results[1:]] == [OutcomeClass.REFUSED] * 3


async def test_a_later_passages_read_does_not_reopen_an_earlier_step():
    """The gate counts from the first step that returned passage text."""
    ops = _ops()
    run, _ = _run(ops, REMINDER)
    await _call(run, tt.FIND_PASSAGES, question=QUESTION)
    run.step = 3
    await _call(run, tt.FIND_PASSAGES, question="Who approves the budget?")
    run.step = 2   # (never happens in a run; proves the minimum is kept)
    await _call(run, tt.MOVE_FILE, file_ref="drive:item-1", folder="Archive")
    ops.move.assert_not_called()


@pytest.mark.parametrize("context", GATED, ids=["reminder", "daily_review", "ask_alek", "tell_alek", "no_origin"])
async def test_a_run_that_read_no_passage_text_keeps_its_writes(context):
    ops = _ops(NOTHING)
    run, collector = _run(ops, context)
    await _call(run, tt.FIND_PASSAGES, question=QUESTION)   # nothing matched: no text reached the model
    run.step = 2
    await _call(run, tt.MOVE_FILE, file_ref="drive:item-1", folder="Archive")
    run.step = 3
    await _call(run, tt.DELETE_DRIVE_ITEM, file_ref="drive:item-2")
    ops.move.assert_awaited_once()
    ops.delete.assert_awaited_once()
    assert collector.results[-1].outcome.cls is OutcomeClass.OK


async def test_a_run_without_passages_is_unaffected():
    ops = _ops()
    run, collector = _run(ops, REMINDER)
    await _call(run, tt.MOVE_FILE, file_ref="drive:item-1", folder="Archive")
    run.step = 2
    await _call(run, tt.DELETE_DRIVE_ITEM, file_ref="drive:item-1")
    assert [r.outcome.cls for r in collector.results] == [OutcomeClass.OK, OutcomeClass.OK]


@pytest.mark.parametrize("name, args, method, op", CHANGES, ids=[c[0] for c in CHANGES])
async def test_a_chat_turn_is_never_refused(name, args, method, op):
    ops = _ops()
    run, collector = _run(ops, CHAT)
    await _call(run, tt.FIND_PASSAGES, question=QUESTION)
    run.step = 2
    await _call(run, name, **args)
    getattr(ops, method).assert_awaited_once()
    assert collector.results[-1].outcome.cls is OutcomeClass.OK


async def test_reads_after_passages_are_never_refused_off_chat():
    ops = _ops()
    ops.open.return_value = DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.OK, text="body", success=True, body=True)
    ops.list_folder.return_value = DriveOutcome(op=DriveOp.LIST, cls=OutcomeClass.OK, text="listing", success=True)
    run, collector = _run(ops, NO_ORIGIN)
    await _call(run, tt.FIND_PASSAGES, question=QUESTION)
    run.step = 2
    await _call(run, tt.OPEN_DRIVE_FILE, file_ref="drive:item-1")
    await _call(run, tt.LIST_FOLDER, folder="R&D")
    await _call(run, tt.READ_PASSAGES, file_ref="drive:item-1", passages="4-5")
    assert [r.outcome.cls for r in collector.results] == [OutcomeClass.OK] * 4


# -- note writes (through DriveNoteToolRun -> run(change=True)) -------------------------------------------------------

def _notes(context):
    service = AsyncMock(spec=DriveNoteService)
    service.create.return_value = NoteReceipt(action="set", note_id="n1", ref="drive:item-1", path=TOMS_PLAN,
                                              version=1, text="Tom's review plan for Acme", state="active")
    service.get.return_value = NoteView(note_id="n1", ref="drive:item-1", note_ref="drive_note:n1", path=TOMS_PLAN,
                                        version=1, state="active", text="Tom's review plan for Acme")
    ops = _ops()
    run, collector = _run(ops, context)
    notes = DriveNoteToolRun(run, service, ops, context, "u1", "a1", NoteProvenance(), writes=True,
                             new_nonce=lambda: "n0nce000")
    return notes, run, collector, service


async def _note(notes, name, **args):
    return await notes.handlers[name](ToolCall(name=name, args=args))


async def test_a_note_write_after_passages_on_a_call_is_refused():
    notes, run, collector, service = _notes(ASK_ALEK)
    await _call(run, tt.FIND_PASSAGES, question=QUESTION)
    run.step = 2
    seen = await _note(notes, nt.CREATE_NOTE, item_ref="drive:item-1", text="Tom's review plan for Acme")
    service.create.assert_not_called()
    assert collector.results[-1].outcome.cls is OutcomeClass.REFUSED
    assert collector.results[-1].outcome.op is DriveOp.NOTE_WRITE
    assert t.WRITE_AFTER_PASSAGES in seen.result_str


async def test_get_note_after_passages_on_a_call_is_a_read_and_runs():
    notes, run, collector, service = _notes(ASK_ALEK)
    await _call(run, tt.FIND_PASSAGES, question=QUESTION)
    run.step = 2
    await _note(notes, nt.GET_NOTE, item_ref="drive:item-1")
    service.get.assert_awaited_once()
    assert collector.results[-1].outcome.cls is not OutcomeClass.REFUSED


async def test_a_note_write_after_passages_in_a_chat_turn_runs():
    notes, run, collector, service = _notes(CHAT)
    await _call(run, tt.FIND_PASSAGES, question=QUESTION)
    run.step = 2
    await _note(notes, nt.CREATE_NOTE, item_ref="drive:item-1", text="Tom's review plan for Acme")
    service.create.assert_awaited_once()
