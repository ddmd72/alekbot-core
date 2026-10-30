"""The specialist's passages tools (USER_DRIVE_INDEXING_RFC §28.3 A2, A3, A6, A11, A18; plan S2.2): each handler makes
one DriveOperations call with its arguments and the run's ledger sets, the outcome lands in the collector and its
ledger, and the model reads the passages in full (no stand-in). DriveOperations is a spec mock that renders typed
results the way the real one does (`render_excerpts`), so the ledger sets the next call gets are the real ones."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.domain import drive_answer_texts as t
from src.domain import drive_tool_texts as tt
from src.domain.drive_excerpt_context import ExcerptContext, folder_line
from src.domain.drive_excerpt_file_context import FileContext
from src.domain.drive_excerpt_folder_note import FolderNote
from src.domain.drive_excerpt_range_request import ExcerptRangeRequest
from src.domain.drive_excerpt_render import render_excerpts
from src.domain.drive_excerpt_result import ExcerptResult
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_pass_through import PassThroughCollector
from src.domain.drive_read_facts import ReadFacts
from src.domain.llm import ToolCall
from src.infrastructure.drive_operations import DriveOperations
from src.infrastructure.drive_tools import DriveToolRun
from tests.unit.domain.drive_excerpt_builders import TOMS_PLAN, chunk, excerpt_file, nonces, result

CTX = {"user_id": "u1", "account_id": "a1", "interactive_delivery": True}
QUESTION = "What does Tom's plan say happens after the first review?"
RD = FolderNote("f-rd", "R&D", 1, "noted by the owner: «Acme specifications; the review process lives here»")
OWN = "noted by the owner: «Tom's draft, not final»"
NOTED = ExcerptContext(files=(FileContext("item-1", TOMS_PLAN, ("f-rd",), "Tom Baker's plan for the review", OWN),),
                       folders={"f-rd": RD})
FIRST = result(excerpt_file("item-1", TOMS_PLAN, [chunk("item-1", 2, "After the first review, Acme signs off.")]),
               question=QUESTION, context=NOTED)


def outcome_of(found: ExcerptResult, *, shown_folders=frozenset(), described_files=frozenset()) -> DriveOutcome:
    """What DriveOperations returns for `found` (same render, same fields)."""
    rendered = render_excerpts(found, nonce=nonces(), shown_folders=shown_folders, described_files=described_files)
    return DriveOutcome(op=DriveOp.PASSAGES, cls=OutcomeClass.OK if found.files else OutcomeClass.NOT_FOUND,
                        text=rendered.text, success=True, items=tuple(f.drive_item() for f in found.files),
                        facts=ReadFacts(excerpts=bool(found.files), owner_note=rendered.plan.has_owner_line),
                        excerpts=found, excerpt_plan=rendered.plan, request_text=QUESTION, ref_tag="a1")


def _ops(*finds: ExcerptResult, ranged=None):
    ops = AsyncMock(spec=DriveOperations)
    queue = list(finds)

    async def find(context, user_id, **kw):
        return outcome_of(queue.pop(0), shown_folders=kw["shown_folders"], described_files=kw["described_files"])

    async def read(context, user_id, **kw):
        return outcome_of(ranged, shown_folders=kw["shown_folders"], described_files=kw["described_files"])
    ops.find_passages.side_effect = find
    ops.read_passages.side_effect = read
    return ops


def _run(ops, context=CTX):
    collector = PassThroughCollector()
    run = DriveToolRun(ops, collector, (), context, "u1", deadline=150.0, clock=lambda: 0.0)
    run.step = 1
    return run, collector


async def _call(run, name, **args):
    return await run.handlers[name](ToolCall(name=name, args=args))


async def test_find_passages_makes_one_call_with_its_arguments_and_an_empty_ledger():
    ops = _ops(FIRST)
    run, collector = _run(ops)
    await _call(run, tt.FIND_PASSAGES, question=QUESTION, folder="R&D", file_refs="drive:a1:item-1")
    ops.find_passages.assert_awaited_once_with(
        CTX, "u1", question=QUESTION, folder="R&D", file_refs="drive:a1:item-1", shown=frozenset(),
        shown_folders=frozenset(), described_files=frozenset())
    (r,) = collector.results
    assert (r.handle, r.step, r.kind, r.outcome.op) == ("r1", 1, t.KIND_PASSAGES, DriveOp.PASSAGES)


async def test_omitted_optional_fields_reach_the_operation_as_not_given():
    ops = _ops(FIRST)
    run, _ = _run(ops)
    await _call(run, tt.FIND_PASSAGES, question=QUESTION)
    kw = ops.find_passages.await_args.kwargs
    assert kw["folder"] is None and kw["file_refs"] is None


async def test_the_model_reads_the_passages_in_full_not_a_stand_in():
    ops = _ops(FIRST)
    run, collector = _run(ops)
    seen = await _call(run, tt.FIND_PASSAGES, question=QUESTION)
    assert seen.result_str == f"[r1]\n{collector.results[0].outcome.text}"
    assert "After the first review, Acme signs off." in seen.result_str
    assert "delivered to the orchestrator as is; you do not see" not in seen.result_str


async def test_a_found_passage_enters_the_ledger_and_drive_context():
    run, collector = _run(_ops(FIRST))
    await _call(run, tt.FIND_PASSAGES, question=QUESTION)
    assert collector.ledger.keys() == frozenset({("item-1", 1, 2)})
    assert collector.ledger.folders_shown() == frozenset({"f-rd"})
    assert collector.ledger.files_described() == frozenset({"item-1"})
    assert collector.drive_context() == [{"path": TOMS_PLAN, "ref": "drive:a1:item-1", "op": "passages", "passages": [3]}]


async def test_a_second_find_gets_the_ledgers_sets_and_prints_the_context_no_more():
    second = result(excerpt_file("item-1", TOMS_PLAN, [chunk("item-1", 5, "The second review closes the plan.")]),
                    question=QUESTION, context=NOTED)
    ops = _ops(FIRST, second)
    run, _ = _run(ops)
    first_seen = await _call(run, tt.FIND_PASSAGES, question=QUESTION)
    run.step = 2
    second_seen = await _call(run, tt.FIND_PASSAGES, question="When does the second review happen?")
    kw = ops.find_passages.await_args_list[1].kwargs
    assert kw["shown"] == frozenset({("item-1", 1, 2)})
    assert kw["shown_folders"] == frozenset({"f-rd"}) and kw["described_files"] == frozenset({"item-1"})
    assert folder_line(RD) in first_seen.result_str and OWN in first_seen.result_str
    assert folder_line(RD) not in second_seen.result_str and OWN not in second_seen.result_str
    assert "The second review closes the plan." in second_seen.result_str


async def test_a_repeated_match_is_listed_by_id():
    again = result(question=QUESTION, repeated=(("item-1", 1, 2),), ref_tag="a1")
    ops = _ops(FIRST, again)
    run, _ = _run(ops)
    await _call(run, tt.FIND_PASSAGES, question=QUESTION)
    seen = await _call(run, tt.FIND_PASSAGES, question="What comes after the first review?")
    assert "Already shown earlier in this request and matched again: drive:a1:item-1:3" in seen.result_str


async def test_read_passages_makes_one_call_with_the_context_sets_only():
    ranged = result(excerpt_file("item-1", TOMS_PLAN, [chunk("item-1", 2, "After the first review, Acme signs off."),
                                                       chunk("item-1", 3, "Then Lukas Weber approves the budget.")]),
                    request=ExcerptRangeRequest("drive:a1:item-1", 3, 4), context=NOTED)
    ops = _ops(FIRST, ranged=ranged)
    run, collector = _run(ops)
    await _call(run, tt.FIND_PASSAGES, question=QUESTION)
    run.step = 2
    seen = await _call(run, tt.READ_PASSAGES, file_ref="drive:a1:item-1", passages="3-4")
    ops.read_passages.assert_awaited_once_with(CTX, "u1", file_ref="drive:a1:item-1", passages="3-4",
                                               shown_folders=frozenset({"f-rd"}),
                                               described_files=frozenset({"item-1"}))
    assert "shown" not in ops.read_passages.await_args.kwargs   # a named range is delivered whole
    assert "Then Lukas Weber approves the budget." in seen.result_str and seen.result_str.startswith("[r2]\n")
    assert collector.ledger.keys() == frozenset({("item-1", 1, 2), ("item-1", 1, 3)})
    assert collector.results[1].kind == t.KIND_PASSAGES


@pytest.mark.parametrize("name", [tt.FIND_PASSAGES, tt.READ_PASSAGES])
async def test_both_passages_tools_are_unordered_reads(name):
    """A read runs beside a batch's changes, never behind the order lock; past the deadline it does not start."""
    ops = _ops(FIRST, ranged=FIRST)
    collector = PassThroughCollector()
    run = DriveToolRun(ops, collector, (), CTX, "u1", deadline=0.0, clock=lambda: 1.0)
    await _call(run, name, question=QUESTION, file_ref="drive:a1:item-1", passages="3")
    assert collector.results[0].outcome.cls is OutcomeClass.NOT_FINISHED
    assert not ops.find_passages.called and not ops.read_passages.called


async def test_a_lock_held_by_a_change_does_not_block_a_passages_read():
    ops = _ops(FIRST)
    run, collector = _run(ops)
    async with run._order:   # a change of the batch is running
        await _call(run, tt.FIND_PASSAGES, question=QUESTION)
    assert collector.results[0].outcome.cls is OutcomeClass.OK


def test_the_two_declarations_follow_open_drive_file_with_their_required_fields():
    decls = tt.drive_tool_declarations()
    names = [d["name"] for d in decls]
    at = names.index(tt.OPEN_DRIVE_FILE)
    assert names[at + 1:at + 3] == [tt.FIND_PASSAGES, tt.READ_PASSAGES]
    find, read = decls[at + 1], decls[at + 2]
    assert set(find["parameters"]["properties"]) == {"question", "folder", "file_refs"}
    assert find["parameters"]["required"] == ["question"]
    assert set(read["parameters"]["properties"]) == {"file_ref", "passages"}
    assert read["parameters"]["required"] == ["file_ref", "passages"]
