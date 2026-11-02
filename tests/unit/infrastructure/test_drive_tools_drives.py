"""The `drives` argument of search_drive and find_passages reaches the operation; two searches that differ only in
`drives` are not "identical" (MULTI_DRIVE_RFC §11.2, slice 2a E-19)."""
from unittest.mock import AsyncMock

import pytest

from src.domain import drive_answer_texts as answer_texts
from src.domain import drive_tool_texts as tt
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_pass_through import PassThroughCollector, search_key
from src.domain.llm import ToolCall
from src.infrastructure.drive_operations import DriveOperations
from src.infrastructure.drive_tools import DriveToolRun

CTX = {"user_id": "u1", "account_id": "a1"}


class Clock:
    def __call__(self):
        return 0.0


@pytest.fixture
def ops():
    o = AsyncMock(spec=DriveOperations)
    o.search.return_value = DriveOutcome(op=DriveOp.SEARCH, cls=OutcomeClass.OK, text="r", success=True)
    o.find_passages.return_value = DriveOutcome(op=DriveOp.PASSAGES, cls=OutcomeClass.OK, text="p", success=True)
    return o


def _run(ops):
    run = DriveToolRun(ops, PassThroughCollector(), (), CTX, "u1", deadline=150.0, clock=Clock())
    run.step = 1
    return run


async def _call(run, name, **args):
    return await run.handlers[name](ToolCall(name=name, args=args))


async def test_drives_reaches_search_and_passages(ops):
    run = _run(ops)
    await _call(run, tt.SEARCH_DRIVE, query="plan", drives="Acme")
    assert ops.search.await_args.kwargs["drives"] == "Acme"
    await _call(run, tt.FIND_PASSAGES, question="what happens after the review", drives="Acme")
    assert ops.find_passages.await_args.kwargs["drives"] == "Acme"


async def test_without_drives_the_call_carries_no_drives_argument(ops):
    run = _run(ops)
    await _call(run, tt.SEARCH_DRIVE, query="plan")
    await _call(run, tt.FIND_PASSAGES, question="what happens after the review")
    assert "drives" not in ops.search.await_args.kwargs and "drives" not in ops.find_passages.await_args.kwargs


async def test_two_searches_differing_only_in_drives_both_run(ops):
    run = _run(ops)
    await _call(run, tt.SEARCH_DRIVE, query="plan", drives="Drive")
    await _call(run, tt.SEARCH_DRIVE, query="plan", drives="Acme")
    assert ops.search.await_count == 2
    again = await _call(run, tt.SEARCH_DRIVE, query="plan", drives="Acme")
    assert ops.search.await_count == 2 and again.result_str == answer_texts.SAME_SEARCH.format(handle="r2")


def test_the_search_key_includes_drives():
    assert search_key({"query": "plan", "drives": "Acme"}) != search_key({"query": "plan", "drives": "Drive"})
    assert search_key({"query": "plan", "drives": " ACME "}) == search_key({"query": "plan", "drives": "acme"})
