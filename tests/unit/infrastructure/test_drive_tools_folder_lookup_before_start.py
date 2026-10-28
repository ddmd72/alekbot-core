"""N3: a change cut during its destination-folder lookup was never started, so it is not "still running"."""
import asyncio
import pytest

from src.domain import drive_answer_texts as t
from src.domain import drive_tool_texts as tt
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_pass_through import PassThroughCollector
from src.domain.llm import ToolCall
from src.infrastructure.drive_operations import STILL_RUNNING
from src.infrastructure.drive_tools import DriveToolRun

CTX = {"user_id": "u1", "account_id": "a1"}


class _FakeOps:
    """A DriveOperations stand-in with semantics: the folder lookup can hang, the move can hang; both are recorded."""

    def __init__(self, *, lookup_hangs=False, move_hangs=False):
        self.lookup_hangs, self.move_hangs = lookup_hangs, move_hangs
        self.lookup_started, self.move_started = asyncio.Event(), asyncio.Event()
        self.lookups, self.moves = [], []

    async def folder_path(self, user_id, folder):
        self.lookups.append(folder)
        if not (isinstance(folder, str) and folder.startswith("drive:")):
            return folder
        self.lookup_started.set()
        if self.lookup_hangs:
            await asyncio.sleep(10)
        return "Resolved/Path"

    async def move(self, context, user_id, ref, *, folder, new_name, wait_s):
        self.moves.append(folder)
        self.move_started.set()
        if self.move_hangs:
            await asyncio.sleep(10)
        return DriveOutcome(op=DriveOp.MOVE, cls=OutcomeClass.OK, text="moved", success=True)


def _run(ops):
    collector = PassThroughCollector()
    run = DriveToolRun(ops, collector, (), CTX, "u1", deadline=150.0, clock=lambda: 0.0)
    run.step = 1
    return run, collector


async def _cut(run, started: asyncio.Event, folder: str):
    task = asyncio.ensure_future(run.handlers[tt.MOVE_FILE](
        ToolCall(name=tt.MOVE_FILE, args={"file_ref": "drive:f1", "folder": folder})))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_cut_during_the_folder_lookup_is_not_started_and_never_still_running():
    ops = _FakeOps(lookup_hangs=True)
    run, collector = _run(ops)
    await _cut(run, ops.lookup_started, "drive:dest")
    outcome = collector.results[0].outcome
    assert outcome.cls is OutcomeClass.NOT_FINISHED and outcome.text == t.NOT_STARTED
    assert ops.moves == []   # no mutation was called


async def test_a_cut_during_the_mutation_after_a_successful_lookup_is_still_running():
    ops = _FakeOps(move_hangs=True)
    run, collector = _run(ops)
    await _cut(run, ops.move_started, "drive:dest")
    outcome = collector.results[0].outcome
    assert outcome.cls is OutcomeClass.STILL_RUNNING and outcome.text == STILL_RUNNING
    assert ops.moves == ["Resolved/Path"]


async def test_a_plain_path_folder_needs_no_lookup_and_a_cut_mutation_is_still_running():
    ops = _FakeOps(move_hangs=True)
    run, collector = _run(ops)
    await _cut(run, ops.move_started, "Taxes/2025")
    assert collector.results[0].outcome.cls is OutcomeClass.STILL_RUNNING
    assert ops.moves == ["Taxes/2025"]


async def test_a_completed_move_gets_the_resolved_folder():
    ops = _FakeOps()
    run, collector = _run(ops)
    await run.handlers[tt.MOVE_FILE](ToolCall(name=tt.MOVE_FILE, args={"file_ref": "drive:f1", "folder": "drive:dest"}))
    assert collector.results[0].outcome.cls is OutcomeClass.OK
    assert ops.moves == ["Resolved/Path"]
