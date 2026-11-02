"""A destination folder ref the access policy refuses (MULTI_DRIVE_RFC §11.3): `DriveToolRun._folder` reads it in `prep`,
outside `DriveOperations._call`; the policy's line comes back as the tool's refusal, the change is never called, and
nothing escapes the tool layer."""
import pytest

from src.domain import drive_tool_texts as tt
from src.domain.drive_access_policy import OUTSIDE_SCOPE_LINE
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_pass_through import PassThroughCollector
from src.domain.llm import ToolCall
from src.domain.user_drive import DriveAccessRefused
from src.infrastructure.drive_tools import DriveToolRun

CTX = {"user_id": "usr-1", "account_id": "acc-1"}
LINE = OUTSIDE_SCOPE_LINE.format(drive="Acme")


class _Ops:
    def __init__(self):
        self.calls = []

    async def folder_path(self, user_id, folder):
        if isinstance(folder, str) and folder.startswith("drive:"):
            raise DriveAccessRefused(LINE)
        return folder

    async def create_folder(self, context, user_id, folder, *, wait_s):
        self.calls.append(("create_folder", folder))
        return DriveOutcome(op=DriveOp.CREATE_FOLDER, cls=OutcomeClass.OK, text="made", success=True)

    async def move(self, context, user_id, ref, *, folder, new_name, wait_s):
        self.calls.append(("move", folder))
        return DriveOutcome(op=DriveOp.MOVE, cls=OutcomeClass.OK, text="moved", success=True)


def _run(ops):
    collector = PassThroughCollector()
    run = DriveToolRun(ops, collector, (), CTX, "usr-1", deadline=150.0, clock=lambda: 0.0)
    run.step = 1
    return run, collector


@pytest.mark.parametrize("tool,args", [
    (tt.CREATE_FOLDER, {"folder": "drive:b2:item-3"}),
    (tt.MOVE_FILE, {"file_ref": "drive:b2:item-2", "folder": "drive:b2:item-3"}),
])
async def test_the_policys_line_is_the_tool_result_and_the_change_is_not_called(tool, args):
    ops = _Ops()
    run, collector = _run(ops)
    result = await run.handlers[tool](ToolCall(name=tool, args=args))
    outcome = collector.results[0].outcome
    assert outcome.cls is OutcomeClass.REFUSED and not outcome.success and outcome.text == LINE
    assert LINE in result.result_str
    assert ops.calls == []


async def test_a_path_folder_still_runs_the_change():
    ops = _Ops()
    run, collector = _run(ops)
    await run.handlers[tt.CREATE_FOLDER](ToolCall(name=tt.CREATE_FOLDER, args={"folder": "Acme/R&D"}))
    assert ops.calls == [("create_folder", "Acme/R&D")] and collector.results[0].outcome.success
