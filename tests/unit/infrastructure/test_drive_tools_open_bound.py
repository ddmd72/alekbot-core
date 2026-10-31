"""What the specialist's model reads for a refused, a cut and a whole open, and what each arms (USER_DRIVE_INDEXING_RFC
§29.6 L13, §29.9): the refusal text carries no tool, the specialist route is appended here, a cut body has its own
stand-in, a whole body keeps the old one; a refusal arms no content gate and a page does."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.domain import drive_answer_texts as t
from src.domain import drive_tool_texts as tt
from src.domain.drive_excerpt_page import PageSpan
from src.domain.drive_excerpt_range_request import ExcerptRangeRequest
from src.domain.drive_excerpt_render import render_excerpts
from src.domain.drive_op import DriveOp
from src.domain.drive_open_bound import refusal_text
from src.domain.drive_open_bound_info import OpenBoundInfo
from src.domain.drive_open_bound_text import BoundText
from src.domain.drive_open_cut import stand_in_cut
from src.domain.drive_open_texts import ROUTE_SPECIALIST
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_pass_through import PassThroughCollector
from src.domain.drive_read_facts import ReadFacts
from src.domain.drive_text_extent import ExtentState, TextExtent
from src.domain.llm import ToolCall
from src.domain.user_drive import DriveItem, drive_label
from src.infrastructure.drive_operations import DriveOperations
from src.infrastructure.drive_tools import DriveToolRun
from tests.unit.domain.drive_excerpt_builders import chunk, excerpt_file, nonces, result

PATH = "Docs/Oak Street parking rules.md"
ITEM = DriveItem(item_id="item-1", name="Oak Street parking rules.md", path=PATH, is_folder=False)
REMINDER = {"user_id": "u1", "account_id": "a1", "run_origin": "background:reminder"}
PAGEABLE = TextExtent(ExtentState.PAGEABLE, text_chars=200_000, passages=130, version=1)


def _refusal():
    return DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.REFUSED, text=refusal_text(PATH, PAGEABLE), success=False,
                        items=(ITEM,), facts=ReadFacts(too_long=True), open_bound=OpenBoundInfo(PAGEABLE),
                        ref_tag="a1")


def _cut(extent):
    info = OpenBoundInfo(extent, chars=90_000, kept=59_000, cut_state=extent.state)
    return DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.OK, text="HEAD + cut line", success=True, items=(ITEM,),
                        body=True, facts=ReadFacts(cut=True), open_bound=info, ref_tag="a1")


def _whole():
    return DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.OK, text="x" * 500, success=True, items=(ITEM,), body=True,
                        ref_tag="a1")


def _page():
    span = PageSpan(first=1, last=10, total=130, version=1, next=11, path=PATH, ref="drive:a1:item-1")
    res = result(excerpt_file("item-1", PATH, [chunk("item-1", i, f"Clause {i + 1}.") for i in range(10)]),
                 request=ExcerptRangeRequest("drive:a1:item-1", 1, 10, page=True), page=span)
    rendered = render_excerpts(res, nonce=nonces())
    return DriveOutcome(op=DriveOp.PASSAGES, cls=OutcomeClass.OK, text=rendered.text, success=True, items=(ITEM,),
                        facts=ReadFacts(excerpts=True, paged=True), excerpts=res, excerpt_plan=rendered.plan,
                        ref_tag="a1")


def _run(open_outcome=None, context=REMINDER):
    ops = AsyncMock(spec=DriveOperations)
    ops.open.return_value = open_outcome
    ops.read_passages.return_value = _page()
    ops.move.return_value = DriveOutcome(op=DriveOp.MOVE, cls=OutcomeClass.OK, text="done", success=True)
    collector = PassThroughCollector()
    run = DriveToolRun(ops, collector, (), context, "u1", deadline=150.0, clock=lambda: 0.0)
    run.step = 1
    return ops, run, collector


async def _call(run, name, **args):
    return await run.handlers[name](ToolCall(name=name, args=args))


async def test_a_refusal_ends_with_the_specialists_route_naming_the_ref():
    _, run, _ = _run(_refusal())
    seen = await _call(run, tt.OPEN_DRIVE_FILE, file_ref="drive:a1:item-1")
    assert seen.result_str == f"[r1]\n{refusal_text(PATH, PAGEABLE)}\n" + ROUTE_SPECIALIST.format(ref="drive:a1:item-1")


async def test_a_cut_body_gets_the_cut_stand_in():
    extent = TextExtent(ExtentState.NOT_INDEXED)
    _, run, _ = _run(_cut(extent))
    seen = await _call(run, tt.OPEN_DRIVE_FILE, file_ref="drive:a1:item-1")
    expected = stand_in_cut("r1", drive_label(ITEM, "a1"), BoundText("", 90_000, 59_000, extent.state), "drive:a1:item-1", extent)
    assert seen.result_str == expected and "HEAD" not in seen.result_str


async def test_a_stale_cut_stand_in_names_the_find_route():
    extent = TextExtent(ExtentState.STALE, passages=77)
    _, run, _ = _run(_cut(extent))
    seen = await _call(run, tt.OPEN_DRIVE_FILE, file_ref="drive:a1:item-1")
    assert "find_passages with file_refs=drive:a1:item-1" in seen.result_str


async def test_a_whole_body_keeps_the_old_stand_in():
    _, run, _ = _run(_whole())
    seen = await _call(run, tt.OPEN_DRIVE_FILE, file_ref="drive:a1:item-1")
    assert seen.result_str == t.STAND_IN_TEXT.format(handle="r1", label=drive_label(ITEM, "a1"), chars=500)


async def test_a_refusal_does_not_arm_the_content_gate():
    ops, run, _ = _run(_refusal())
    await _call(run, tt.OPEN_DRIVE_FILE, file_ref="drive:a1:item-1")
    run.step = 2
    await _call(run, tt.MOVE_FILE, file_ref="drive:a1:item-1", folder="Archive")
    ops.move.assert_awaited_once()


async def test_a_page_arms_it_like_any_passages_read():
    ops, run, collector = _run(_refusal())
    await _call(run, tt.READ_PASSAGES, file_ref="drive:a1:item-1", passages="from 1")
    run.step = 2
    await _call(run, tt.MOVE_FILE, file_ref="drive:a1:item-1", folder="Archive")
    ops.move.assert_not_called()
    assert collector.results[-1].outcome.cls is OutcomeClass.REFUSED


@pytest.mark.parametrize("passages", ["from 1", "6-8"])
async def test_the_passages_argument_reaches_the_operation_unchanged(passages):
    ops, run, _ = _run(_refusal())
    await _call(run, tt.READ_PASSAGES, file_ref="drive:a1:item-1", passages=passages)
    assert ops.read_passages.await_args.kwargs["passages"] == passages


async def test_one_page_per_commission_a_second_page_is_refused_without_a_call():
    ops, run, collector = _run(_refusal())
    await _call(run, tt.READ_PASSAGES, file_ref="drive:a1:item-1", passages="from 1")
    run.step = 2
    seen = await _call(run, tt.READ_PASSAGES, file_ref="drive:a1:item-1", passages="from 11")
    assert seen.result_str == t.ONE_PAGE
    assert ops.read_passages.await_count == 1 and len(collector.results) == 1


async def test_a_co_emitted_second_page_is_refused_too():
    import asyncio
    ops, run, collector = _run(_refusal())
    first, second = await asyncio.gather(
        _call(run, tt.READ_PASSAGES, file_ref="drive:a1:item-1", passages="from 1"),
        _call(run, tt.READ_PASSAGES, file_ref="drive:a1:item-1", passages="from 11"))
    assert sorted([first.result_str == t.ONE_PAGE, second.result_str == t.ONE_PAGE]) == [False, True]
    assert ops.read_passages.await_count == 1


async def test_neighbour_ranges_and_finds_are_not_limited_by_the_page_rule():
    ops, run, _ = _run(_refusal())
    await _call(run, tt.READ_PASSAGES, file_ref="drive:a1:item-1", passages="from 1")
    run.step = 2
    for rng in ("6-8", "9"):
        seen = await _call(run, tt.READ_PASSAGES, file_ref="drive:a1:item-1", passages=rng)
        assert seen.result_str != t.ONE_PAGE
    assert ops.read_passages.await_count == 3


async def test_a_malformed_page_does_not_use_up_the_page():
    ops, run, _ = _run(_refusal())
    await _call(run, tt.READ_PASSAGES, file_ref="drive:a1:item-1", passages="from 0")
    seen = await _call(run, tt.READ_PASSAGES, file_ref="drive:a1:item-1", passages="from 1")
    assert seen.result_str != t.ONE_PAGE


async def test_a_new_run_starts_with_a_fresh_page():
    _, first, _ = _run(_refusal())
    await _call(first, tt.READ_PASSAGES, file_ref="drive:a1:item-1", passages="from 1")
    _, second, _ = _run(_refusal())
    seen = await _call(second, tt.READ_PASSAGES, file_ref="drive:a1:item-1", passages="from 1")
    assert seen.result_str != t.ONE_PAGE


def test_the_texts_the_review_changed():
    from src.domain import drive_excerpt_render as R
    from src.domain.drive_open_texts import ROUTE_ORCHESTRATOR
    assert "when the commission asks for it" in R.PAGE_NEXT
    assert "report now" in ROUTE_SPECIALIST
    assert "whether it goes on" in t.R_PAGED and "that it goes on" not in t.R_PAGED
    assert "from its beginning" in t.R_TOO_LONG
    assert "only when the user's own words ask" in ROUTE_ORCHESTRATOR
    assert "tool" not in t.ONE_PAGE and tt.READ_PASSAGES not in t.ONE_PAGE
