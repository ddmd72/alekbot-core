"""DriveToolRun: the run's deadline passing while a change waits behind the batch's order lock.

The waiting change is reported not started and nothing reaches DriveOperations for it."""
import asyncio

from src.domain import drive_answer_texts as t
from src.domain import drive_tool_texts as tt
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_pass_through import PassThroughCollector
from src.domain.llm import ToolCall
from src.infrastructure.drive_tools import DriveToolRun

CTX = {"user_id": "u1", "account_id": "a1"}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeOps:
    """The first move holds the order lock until released; the run's time is spent while it holds."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.moves: list[str] = []
        self.first_started, self.release = asyncio.Event(), asyncio.Event()

    async def folder_path(self, user_id, folder):
        return folder

    async def move(self, context, user_id, ref, *, folder, new_name, wait_s):
        self.moves.append(ref)
        if len(self.moves) == 1:
            self.first_started.set()
            await self.release.wait()
            self.clock.now = 200.0   # the deadline (150) passes while the first change runs
        return DriveOutcome(op=DriveOp.MOVE, cls=OutcomeClass.OK, text=f"moved {ref}", success=True)


def _move(ref: str) -> ToolCall:
    return ToolCall(name=tt.MOVE_FILE, args={"file_ref": ref, "folder": "Lisbon lease"})


async def test_a_change_waking_on_the_order_lock_past_the_deadline_is_not_started():
    clock = Clock()
    ops, collector = FakeOps(clock), PassThroughCollector()
    run = DriveToolRun(ops, collector, (), CTX, "u1", deadline=150.0, clock=clock)
    run.step = 1
    first = asyncio.ensure_future(run.handlers[tt.MOVE_FILE](_move("drive:f1")))
    await ops.first_started.wait()
    second = asyncio.ensure_future(run.handlers[tt.MOVE_FILE](_move("drive:f2")))
    await asyncio.sleep(0)   # the second is queued on the lock, the deadline still ahead
    assert not second.done()
    ops.release.set()
    await asyncio.gather(first, second)

    assert ops.moves == ["drive:f1"]   # nothing reached DriveOperations for the second change
    done, skipped = (r.outcome for r in collector.results)
    assert done.cls is OutcomeClass.OK
    assert skipped.cls is OutcomeClass.NOT_FINISHED and skipped.text == t.NOT_STARTED
    assert not skipped.success
