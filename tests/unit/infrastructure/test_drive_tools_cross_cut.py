"""Cross-cut review fixes X1-X3 on the drive specialist's tools (docs: plan_arbiter_xcut rulings)."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from src.domain import drive_answer_texts as t
from src.domain import drive_tool_texts as tt
from src.domain.drive_answer import compose_answer
from src.domain.drive_file_content_item import FileContentItem
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_pass_through import PassThroughCollector
from src.domain.drive_report import DriveReport
from src.domain.drive_status import DriveStatus
from src.domain.llm import ToolCall
from src.infrastructure.drive_operations import STILL_RUNNING, DriveOperations
from src.infrastructure.drive_tools import DriveToolRun

CTX = {"user_id": "u1", "account_id": "a1"}
ITEMS = (FileContentItem("t1", "- call the bank", target="Inbox/todo.md"),)


def _ok(op, text="done"):
    return DriveOutcome(op=op, cls=OutcomeClass.OK, text=text, success=True)


def _still(op):
    return DriveOutcome(op=op, cls=OutcomeClass.STILL_RUNNING, text=STILL_RUNNING, success=True)


@pytest.fixture
def ops():
    o = AsyncMock(spec=DriveOperations)
    o.folder_path.side_effect = lambda user_id, folder: folder
    for name, op in (("open", DriveOp.OPEN), ("move", DriveOp.MOVE), ("append", DriveOp.APPEND),
                     ("replace", DriveOp.REPLACE), ("delete", DriveOp.DELETE)):
        getattr(o, name).return_value = _ok(op)
    return o


def _run(ops, **kw):
    collector = PassThroughCollector()
    run = DriveToolRun(ops, collector, ITEMS, CTX, "u1", deadline=150.0, clock=lambda: 0.0, **kw)
    run.step = 1
    return run, collector


async def _call(run, name, **args):
    return await run.handlers[name](ToolCall(name=name, args=args))


# -- X1 ---------------------------------------------------------------------------------------------------------------

async def test_a_two_move_batch_whose_first_is_still_running_never_reports_did_not_finish(ops):
    fired = []
    run, collector = _run(ops, on_still_running=lambda: fired.append(True))
    ops.move.side_effect = [_still(DriveOp.MOVE), _ok(DriveOp.MOVE)]
    first = asyncio.ensure_future(_call(run, tt.MOVE_FILE, file_ref="drive:a", folder="X"))
    second = asyncio.ensure_future(_call(run, tt.MOVE_FILE, file_ref="drive:b", folder="X"))
    await asyncio.gather(first, second)
    assert [r.outcome.cls for r in collector.results] == [OutcomeClass.STILL_RUNNING, OutcomeClass.NOT_FINISHED]
    assert collector.results[1].outcome.text == t.NOT_STARTED_BEHIND
    assert ops.move.await_count == 1   # the second move never reached the service
    answer = compose_answer(collector, DriveReport())
    assert answer.status is DriveStatus.STILL_RUNNING
    assert t.DID_NOT_FINISH not in answer.text
    # both moves share step 1: the first's line says running; the second's own "not done" is its true verdict
    assert "1. " + t.OP_RUNNING + ":" in answer.text
    assert fired == [True]


async def test_a_started_append_cut_by_the_deadline_is_still_running_never_not_done(ops):
    started = asyncio.Event()

    async def hang(*a, **k):
        started.set()
        await asyncio.sleep(10)
    ops.append.side_effect = hang
    run, collector = _run(ops)
    task = asyncio.ensure_future(_call(run, tt.APPEND_TO_FILE, text_item="t1", file_ref="drive:f1"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    outcome = collector.results[0].outcome
    assert outcome.cls is OutcomeClass.STILL_RUNNING and outcome.text == STILL_RUNNING
    answer = compose_answer(collector, DriveReport(), cut=True)
    assert answer.status is DriveStatus.STILL_RUNNING
    assert t.DID_NOT_FINISH not in answer.text and "may complete" in answer.text
    assert "1. " + t.OP_NOT_DONE + ":" not in answer.text and "1. " + t.OP_RUNNING + ":" in answer.text


async def test_a_change_cancelled_while_queued_is_still_not_started(ops):
    release = asyncio.Event()

    async def slow(*a, **k):
        await release.wait()
        return _ok(DriveOp.MOVE)
    ops.move.side_effect = slow
    run, collector = _run(ops)
    first = asyncio.ensure_future(_call(run, tt.MOVE_FILE, file_ref="drive:a", folder="X"))
    await asyncio.sleep(0)
    second = asyncio.ensure_future(_call(run, tt.MOVE_FILE, file_ref="drive:b", folder="X"))
    await asyncio.sleep(0)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert collector.results[0].outcome.text == t.NOT_STARTED
    release.set()
    await first


# -- X2 / X3 ----------------------------------------------------------------------------------------------------------

async def test_opening_a_path_is_a_correctable_ref_error_not_a_storage_lookup(ops):
    run, collector = _run(ops)
    res = await _call(run, tt.OPEN_DRIVE_FILE, file_ref="Contracts/Lease 2025.pdf")
    ops.open.assert_not_called()
    assert collector.results[0].outcome.cls is OutcomeClass.REQUEST_ERROR
    assert "file_ref is required" in res.result_str and "drive: ref" in res.result_str


@pytest.mark.parametrize("ref", ["drive:abc", "lease.pdf", "workspace:files/a.pdf", "skill:x/y.md",
                                 "docs/report.docx", "email_review/2026-10-22.html"])
async def test_opening_other_refs_still_goes_to_the_service(ops, ref):
    run, _ = _run(ops)
    await _call(run, tt.OPEN_DRIVE_FILE, file_ref=ref)
    ops.open.assert_awaited_once()


@pytest.mark.parametrize("tool,args,op,field", [
    (tt.APPEND_TO_FILE, {"text_item": "t1", "file_ref": "Inbox/todo.md"}, DriveOp.APPEND, "file_ref"),
    (tt.MOVE_FILE, {"file_ref": "Inbox/a.pdf", "folder": "X"}, DriveOp.MOVE, "file_ref"),
    (tt.DELETE_DRIVE_ITEM, {"file_ref": "Inbox/a.pdf"}, DriveOp.DELETE, "file_ref"),
    (tt.REPLACE_FILE_CONTENT, {"target_ref": "Inbox/a.txt", "new_content_ref": "b.txt"}, DriveOp.REPLACE,
     "target_ref"),
])
async def test_a_path_as_a_target_ref_is_correctable_not_a_chat_file_hint(ops, tool, args, op, field):
    run, collector = _run(ops)
    res = await _call(run, tool, **args)
    getattr(ops, op.value).assert_not_called()
    assert collector.results[0].outcome.cls is OutcomeClass.REQUEST_ERROR
    assert f"{field} is required" in res.result_str
    assert "chat file" not in res.result_str and "copy_file_to_drive" not in res.result_str


async def test_an_append_target_path_in_file_contents_is_refused_the_same_way(ops):
    run, collector = _run(ops)
    res = await _call(run, tt.APPEND_TO_FILE, text_item="t1")   # item.target = "Inbox/todo.md"
    ops.append.assert_not_called()
    assert "file_ref is required" in res.result_str


async def test_a_bare_name_keeps_the_services_own_text(ops):
    run, _ = _run(ops)
    await _call(run, tt.DELETE_DRIVE_ITEM, file_ref="lease.pdf")
    ops.delete.assert_awaited_once()
