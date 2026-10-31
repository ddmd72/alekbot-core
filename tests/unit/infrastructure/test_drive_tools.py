"""The drive specialist's tools (DRIVE_SPECIALIST_RFC §4.2, §5.4, §5.6, §6, §8; §11.1 "Tools", "Time")."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from src.domain import drive_answer_texts as t
from src.domain import drive_tool_texts as tt
from src.domain.drive_file_content_item import FileContentItem
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_pass_through import PassThroughCollector
from src.domain.llm import ToolCall
from src.domain.user_drive import MOVE_NEEDS_TARGET, DriveItem, MoveOutcome
from src.infrastructure.drive_operations import STILL_RUNNING, DriveOperations
from src.infrastructure.drive_tools import DriveToolRun
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_service_connections import ConnectionReads, connection_of

CTX = {"user_id": "u1", "account_id": "a1"}
FILE = DriveItem(item_id="f1", name="lease.pdf", path="Inbox/lease.pdf", is_folder=False, size_bytes=2048)
FOLDER = DriveItem(item_id="d9", name="Archive", path="Archive", is_folder=True)


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


def _ok(op, text="done", **kw):
    return DriveOutcome(op=op, cls=OutcomeClass.OK, text=text, success=True, **kw)


@pytest.fixture
def ops():
    o = AsyncMock(spec=DriveOperations)
    o.folder_path.side_effect = lambda user_id, folder: folder
    for name, op in (("search", DriveOp.SEARCH), ("search_deleted", DriveOp.SEARCH), ("list_folder", DriveOp.LIST),
                     ("open", DriveOp.OPEN), ("copy", DriveOp.COPY), ("save_text", DriveOp.SAVE_TEXT),
                     ("create_folder", DriveOp.CREATE_FOLDER), ("move", DriveOp.MOVE), ("append", DriveOp.APPEND),
                     ("replace", DriveOp.REPLACE), ("delete", DriveOp.DELETE)):
        getattr(o, name).return_value = _ok(op)
    return o


ITEMS = (FileContentItem("t1", "first text", name="a.md"), FileContentItem("t2", "second", target="drive:f2"))


def _run(ops, items=ITEMS, deadline=150.0, clock=None, **kw):
    collector = PassThroughCollector()
    run = DriveToolRun(ops, collector, items, CTX, "u1", deadline=deadline, clock=clock or Clock(), **kw)
    run.step = 1
    return run, collector


async def _call(run, name, **args):
    return await run.handlers[name](ToolCall(name=name, args=args))


# -- one tool, one call, mapped arguments ---------------------------------------------------------------------

async def test_each_tool_makes_one_call_with_its_arguments(ops):
    run, collector = _run(ops)
    await _call(run, tt.SEARCH_DRIVE, query="lease", folder="Contracts", kind="pdf", date_from="2026-01-01",
                date_to="2026-02-01")
    ops.search.assert_awaited_once_with(CTX, "u1", query="lease", folder="Contracts", kind="pdf",
                                        date_from="2026-01-01", date_to="2026-02-01")
    await _call(run, tt.SEARCH_DELETED, query="garden")
    ops.search_deleted.assert_awaited_once_with(CTX, "u1", "garden")
    await _call(run, tt.LIST_FOLDER, folder="Inbox")
    ops.list_folder.assert_awaited_once_with(CTX, "u1", "Inbox")
    await _call(run, tt.OPEN_DRIVE_FILE, file_ref="drive:f1")
    await _call(run, tt.GET_RECORDING_LINK, file_ref="drive:r1")
    assert [c.args for c in ops.open.await_args_list] == [(CTX, "u1", "drive:f1"), (CTX, "u1", "drive:r1")]
    assert ops.open.await_args_list[1].kwargs == {"original": True}
    await _call(run, tt.CREATE_FOLDER, folder="Archive/2026")
    assert ops.create_folder.await_args.args == (CTX, "u1", "Archive/2026")
    await _call(run, tt.DELETE_DRIVE_ITEM, file_ref="drive:f1")
    assert ops.delete.await_args.args == (CTX, "u1", "drive:f1")
    assert [r.kind for r in collector.results] == [t.KIND_SEARCH, t.KIND_DELETED_SEARCH, t.KIND_LISTING, t.KIND_OPEN,
                                                   t.KIND_LINK, "create_folder", "delete"]


@pytest.mark.parametrize("source", ["report.docx", "docs/u1/plan.pdf", "workspace:files/lease.pdf",
                                    "skill:flight-status/references/airlines.md"])
async def test_copy_passes_every_source_kind_through(ops, source):
    run, _ = _run(ops)
    await _call(run, tt.COPY_FILE_TO_DRIVE, source_ref=source, folder="Contracts", file_name="x.pdf")
    assert ops.copy.await_args.args == (CTX, "u1", source)
    assert ops.copy.await_args.kwargs["folder"] == "Contracts" and ops.copy.await_args.kwargs["file_name"] == "x.pdf"


@pytest.mark.parametrize("source", ["new.pdf", "docs/u1/v2.pdf", "workspace:files/v2.pdf", "skill:s/a.md", "drive:f2"])
async def test_replace_passes_every_source_kind_through_in_its_direction(ops, source):
    run, _ = _run(ops)
    await _call(run, tt.REPLACE_FILE_CONTENT, target_ref="drive:f1", new_content_ref=source)
    assert ops.replace.await_args.args == (CTX, "u1", "drive:f1", source)


async def test_one_move_with_both_fields_is_one_call(ops):
    run, _ = _run(ops)
    await _call(run, tt.MOVE_FILE, file_ref="drive:f1", folder="Archive", new_name="lease-2025.pdf")
    ops.move.assert_awaited_once()
    kwargs = ops.move.await_args.kwargs
    assert (kwargs["folder"], kwargs["new_name"]) == ("Archive", "lease-2025.pdf")


async def test_a_move_with_neither_field_changes_nothing(ops):
    run, collector = _run(ops)
    result = await _call(run, tt.MOVE_FILE, file_ref="drive:f1", folder="", new_name=" ")
    ops.move.assert_not_called()
    assert "Give a destination folder, a new name, or both" in result.result_str
    assert collector.results[0].outcome.cls is OutcomeClass.REQUEST_ERROR


async def test_a_folder_ref_and_its_path_give_the_same_call():
    drive = AsyncMock(spec=UserDriveService)
    reads = ConnectionReads()
    drive.resolve_ref.side_effect = reads.resolve_ref
    drive.get_item.return_value = FOLDER
    drive.move.return_value = MoveOutcome(before=FILE, after=FILE, created=[])
    ops = DriveOperations(conversion_service=AsyncMock(spec=FileConversionService), drive_service=drive)
    run, _ = _run(ops)
    await _call(run, tt.MOVE_FILE, file_ref="drive:a1:f1", folder="drive:a1:d9")
    await _call(run, tt.MOVE_FILE, file_ref="drive:a1:f1", folder="Archive")
    handle = connection_of(reads, "u1").handle
    assert drive.move.await_args_list[0].args == drive.move.await_args_list[1].args == (handle, "f1", "Archive", None)


async def test_empty_or_omitted_refs_never_reach_the_drive():
    drive = AsyncMock(spec=UserDriveService)
    ops = DriveOperations(conversion_service=AsyncMock(spec=FileConversionService), drive_service=drive)
    run, collector = _run(ops)
    await _call(run, tt.DELETE_DRIVE_ITEM)
    await _call(run, tt.DELETE_DRIVE_ITEM, file_ref="")
    await _call(run, tt.REPLACE_FILE_CONTENT, target_ref="drive:f1", new_content_ref="")
    await _call(run, tt.APPEND_TO_FILE, text_item="t1")   # t1 has no target and no file_ref was given
    drive.delete.assert_not_called()
    drive.replace_with.assert_not_called()
    drive.append_text.assert_not_called()
    assert {r.outcome.cls for r in collector.results} == {OutcomeClass.REQUEST_ERROR}


async def test_folder_path_resolves_only_a_readable_folder_ref():
    drive = AsyncMock(spec=UserDriveService)
    ops = DriveOperations(conversion_service=AsyncMock(spec=FileConversionService), drive_service=drive)
    drive.get_item.return_value = FOLDER
    assert await ops.folder_path("u1", "drive:d9") == "Archive"
    assert await ops.folder_path("u1", "Contracts/2026") == "Contracts/2026"
    drive.get_item.return_value = FILE
    assert await ops.folder_path("u1", "drive:f1") == "drive:f1"   # a file ref stays: the service refuses it
    drive.get_item.side_effect = RuntimeError("down")
    assert await ops.folder_path("u1", "drive:d9") == "drive:d9"


async def test_only_file_content_is_marked_as_a_body():
    conversion = AsyncMock(spec=FileConversionService)
    drive = AsyncMock(spec=UserDriveService)
    drive.resolve_ref.side_effect = ConnectionReads().resolve_ref
    ops = DriveOperations(conversion_service=conversion, drive_service=drive)
    drive.get_item.return_value = DriveItem(item_id="n1", name="todo.md", path="Notes/todo.md",
                                                       is_folder=False, size_bytes=10, mime_type="text/markdown")
    conversion.resolve_content.return_value = "buy milk"
    text = await ops.open(CTX, "u1", "drive:a1:n1")
    drive.get_item.return_value = DriveItem(item_id="h1", name="a.heic", path="a.heic", is_folder=False,
                                                       size_bytes=10, mime_type="image/heic")
    heic = await ops.open(CTX, "u1", "drive:a1:h1")
    stored = await ops.fetch_stored(CTX, "u1", "notes.txt")
    assert (text.body, heic.body, stored.body) == (True, False, True)
    assert "buy milk" in text.text and heic.success


# -- file_contents items are bound by code (§4.2) ----------------------------------------------------------------

async def test_save_and_append_write_the_items_text_never_a_typed_one(ops):
    run, _ = _run(ops)
    await _call(run, tt.SAVE_TEXT_FILE, text_item="t1", folder="Notes")
    assert ops.save_text.await_args.args == (CTX, "u1", "first text")
    assert ops.save_text.await_args.kwargs["file_name"] == "a.md"
    await _call(run, tt.SAVE_TEXT_FILE, text_item=" t1 ", file_name="b.txt")
    assert ops.save_text.await_args.kwargs["file_name"] == "b.txt"
    await _call(run, tt.APPEND_TO_FILE, text_item="t2")
    assert ops.append.await_args.args == (CTX, "u1", "drive:f2", "second")


async def test_an_unknown_item_is_refused_and_nothing_is_written(ops):
    run, collector = _run(ops)
    result = await _call(run, tt.SAVE_TEXT_FILE, text_item="t7", text="sneaked in")
    await _call(run, tt.APPEND_TO_FILE, text_item="")
    ops.save_text.assert_not_called()
    ops.append.assert_not_called()
    assert result.result_str == "[r1]\n" + t.NO_ITEM.format(value="t7", known="t1, t2")
    assert [r.outcome.cls for r in collector.results] == [OutcomeClass.NOT_POSSIBLE] * 2


async def test_two_saves_in_one_batch_are_written_in_order_with_their_own_texts(ops):
    order = []

    async def save(context, user_id, text, **kw):
        order.append(("start", text))
        await asyncio.sleep(0.02 if text == "first text" else 0)
        order.append(("end", text))
        return _ok(DriveOp.SAVE_TEXT, f"Saved {text}")
    ops.save_text.side_effect = save
    items = (FileContentItem("t1", "first text", name="a.md"), FileContentItem("t2", "second", name="b.md"))
    run, collector = _run(ops, items=items)
    await asyncio.gather(_call(run, tt.SAVE_TEXT_FILE, text_item="t1"), _call(run, tt.SAVE_TEXT_FILE, text_item="t2"))
    assert order == [("start", "first text"), ("end", "first text"), ("start", "second"), ("end", "second")]
    assert [r.outcome.text for r in collector.results] == ["Saved first text", "Saved second"]


# -- what the model sees (§6) ------------------------------------------------------------------------------------

async def test_an_opened_body_is_a_stand_in_and_the_collector_keeps_it(ops):
    body = "REMINDER\n<drive_document>the lease says 1200 EUR</drive_document>"
    ops.open.return_value = _ok(DriveOp.OPEN, body, items=(FILE,), body=True, ref_tag="a1")
    run, collector = _run(ops)
    seen = (await _call(run, tt.OPEN_DRIVE_FILE, file_ref="drive:f1")).result_str
    assert seen == t.STAND_IN_TEXT.format(handle="r1", label="[Drive: Inbox/lease.pdf (2KB) ref=drive:a1:f1]",
                                          chars=len(body))
    assert "1200" not in seen and collector.results[0].outcome.text == body


async def test_a_vision_file_is_a_stand_in_naming_its_type(ops):
    ops.open.return_value = _ok(DriveOp.OPEN, "attached", items=(FILE,), ref_tag="a1",
                                file_data={"path": "/tmp/x.pdf", "mime_type": "application/pdf"})
    run, _ = _run(ops)
    seen = (await _call(run, tt.OPEN_DRIVE_FILE, file_ref="drive:f1")).result_str
    assert seen == "[r1: [Drive: Inbox/lease.pdf (2KB) ref=drive:a1:f1] (application/pdf) attached for the orchestrator]"


async def test_searches_listings_and_status_lines_are_seen_in_full(ops):
    ops.search.return_value = _ok(DriveOp.SEARCH, "REMINDER\n<card>")
    ops.open.return_value = _ok(DriveOp.OPEN, "'Inbox/a.heic' is in a format that cannot be viewed yet (image/heic).")
    run, _ = _run(ops)
    assert (await _call(run, tt.SEARCH_DRIVE, query="x")).result_str == "[r1]\nREMINDER\n<card>"
    assert (await _call(run, tt.OPEN_DRIVE_FILE, file_ref="drive:h")).result_str.endswith("cannot be viewed yet "
                                                                                          "(image/heic).")


async def test_an_identical_search_is_not_run_again(ops):
    run, collector = _run(ops)
    await _call(run, tt.SEARCH_DRIVE, query="Lease  Porto")
    again = await _call(run, tt.SEARCH_DRIVE, query="lease porto", folder="")
    await _call(run, tt.SEARCH_DRIVE, query="lease porto", kind="pdf")
    assert again.result_str == t.SAME_SEARCH.format(handle="r1")
    assert ops.search.await_count == 2 and collector.repeats_skipped == 1


async def test_a_search_with_a_request_error_can_be_run_again(ops):
    ops.search.return_value = DriveOutcome(op=DriveOp.SEARCH, cls=OutcomeClass.REQUEST_ERROR, text="bad date",
                                           success=False)
    run, _ = _run(ops)
    await _call(run, tt.SEARCH_DRIVE, query="x", date_from="soon")
    await _call(run, tt.SEARCH_DRIVE, query="x", date_from="soon")
    assert ops.search.await_count == 2


async def test_each_result_records_the_step_that_emitted_it(ops):
    run, collector = _run(ops)
    run.step = 3
    await _call(run, tt.LIST_FOLDER)
    assert collector.results[0].step == 3


# -- time (§5.6) -------------------------------------------------------------------------------------------------

async def test_a_call_after_the_deadline_is_not_started(ops):
    run, collector = _run(ops, deadline=10.0, clock=Clock(now=11.0))
    await _call(run, tt.DELETE_DRIVE_ITEM, file_ref="drive:f1")
    await _call(run, tt.SEARCH_DRIVE, query="x")
    ops.delete.assert_not_called()
    ops.search.assert_not_called()
    assert [(r.outcome.cls, r.outcome.text) for r in collector.results] == [
        (OutcomeClass.NOT_FINISHED, t.NOT_STARTED)] * 2


async def test_a_read_still_running_at_the_deadline_did_not_finish(ops):
    async def slow(*a, **k):
        await asyncio.sleep(5)
    ops.search.side_effect = slow
    clock = Clock()
    run, collector = _run(ops, deadline=0.05, clock=lambda: clock.now)
    await _call(run, tt.SEARCH_DRIVE, query="x")
    assert (collector.results[0].outcome.cls, collector.results[0].outcome.text) == (
        OutcomeClass.NOT_FINISHED, t.DID_NOT_FINISH)


@pytest.mark.parametrize("left, wait", [(150.0, 100.0), (50.0, 40.0), (5.0, 1.0)])
async def test_the_mutation_wait_is_clamped_to_the_deadline(ops, left, wait):
    run, _ = _run(ops, deadline=left)
    await _call(run, tt.DELETE_DRIVE_ITEM, file_ref="drive:f1")
    assert ops.delete.await_args.kwargs["wait_s"] == wait


@pytest.mark.parametrize("method, tool, args, expected", [
    # X1 (plan_arbiter_xcut): a started change runs on under DriveOperations' shield, so it is still running.
    ("move", tt.MOVE_FILE, {"file_ref": "drive:f1", "folder": "X"}, (OutcomeClass.STILL_RUNNING, STILL_RUNNING)),
    # a read has no shield: it did not finish
    ("search", tt.SEARCH_DRIVE, {"query": "x"}, (OutcomeClass.NOT_FINISHED, t.DID_NOT_FINISH)),
])
async def test_a_cancelled_call_is_recorded_before_the_cancel_goes_through(ops, method, tool, args, expected):
    started = asyncio.Event()

    async def hang(*a, **k):
        started.set()
        await asyncio.sleep(10)
    getattr(ops, method).side_effect = hang
    run, collector = _run(ops)
    task = asyncio.ensure_future(_call(run, tool, **args))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(collector.results) == 1
    assert (collector.results[0].outcome.cls, collector.results[0].outcome.text) == expected


async def test_still_running_calls_back(ops):
    ops.delete.return_value = DriveOutcome(op=DriveOp.DELETE, cls=OutcomeClass.STILL_RUNNING, text="running",
                                           success=True)
    called = []
    run, _ = _run(ops, on_still_running=lambda: called.append(True))
    await _call(run, tt.DELETE_DRIVE_ITEM, file_ref="drive:f1")
    assert called == [True]


# -- the declarations (§8) -----------------------------------------------------------------------------------------

_SMART_ERA = ("drive_query", "drive_folder", "drive_kind", "drive_date", "drive_deleted", "search_files_in_drive",
              "list_files_in_drive", "open_file_from_drive", "save_file_to_drive", "move_file_in_drive",
              "update_file_in_drive", "delete_file_from_drive", "create_folder_in_drive", "not read",
              "delegate_to_specialist")


def test_declarations_cover_every_handler_plus_the_terminal_tool(ops):
    run, _ = _run(ops)
    names = [d["name"] for d in tt.drive_tool_declarations()]
    assert names[-1] == tt.REPORT_TOOL and set(names[:-1]) == set(run.handlers)


def test_no_parameter_is_named_name_and_schemas_are_flat():
    for decl in tt.drive_tool_declarations():
        props = decl["parameters"]["properties"]
        assert "name" not in props
        for prop in props.values():
            assert set(prop) == {"type", "description"} and prop["type"] == "string"


def test_descriptions_name_no_smart_era_field_or_intent():
    for decl in tt.drive_tool_declarations():
        texts = [decl["description"], *(p["description"] for p in decl["parameters"]["properties"].values())]
        for text in texts:
            for word in _SMART_ERA:
                assert word not in text, (decl["name"], word)


# -- M1: what a cancel records ----------------------------------------------------------------------------------------

async def test_a_mutation_cancelled_while_queued_behind_the_batch_never_started(ops):
    """It waits on the per-execution order lock; a cancel there ran nothing, so it is "not started"."""
    gate = asyncio.Event()
    started = asyncio.Event()

    async def first(*a, **k):
        started.set()
        await gate.wait()
        return _ok(DriveOp.MOVE)
    ops.move.side_effect = first
    run, collector = _run(ops)
    running = asyncio.ensure_future(_call(run, tt.MOVE_FILE, file_ref="drive:f1", folder="A"))
    await started.wait()
    queued = asyncio.ensure_future(_call(run, tt.DELETE_DRIVE_ITEM, file_ref="drive:f2"))
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    gate.set()
    await running
    ops.delete.assert_not_called()
    delete = next(r for r in collector.results if r.outcome.op is DriveOp.DELETE)
    assert delete.outcome.cls is OutcomeClass.NOT_FINISHED and delete.outcome.text == t.NOT_STARTED


async def test_a_mutation_cancelled_once_started_is_recorded_as_still_running(ops):
    """X1 (plan_arbiter_xcut): the shielded delete runs on after the cancel; today's still-running text, never "did not
    finish"."""
    started = asyncio.Event()

    async def hang(*a, **k):
        started.set()
        await asyncio.sleep(10)
    ops.delete.side_effect = hang
    run, collector = _run(ops)
    task = asyncio.ensure_future(_call(run, tt.DELETE_DRIVE_ITEM, file_ref="drive:f1"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    outcome = collector.results[0].outcome
    assert (outcome.op, outcome.cls, outcome.text) == (DriveOp.DELETE, OutcomeClass.STILL_RUNNING, STILL_RUNNING)


async def test_a_cancel_after_the_shield_arms_the_late_failure_notice():
    """DriveOperations: the caller is cancelled while the shielded delete runs on; if it then fails the user is told."""
    drive = AsyncMock(spec=UserDriveService)
    release = asyncio.Event()

    async def slow_delete(*a, **k):
        await release.wait()
        raise RuntimeError("provider down")
    drive.delete.side_effect = slow_delete
    drive.get_item.return_value = FILE
    notification = AsyncMock()
    ops = DriveOperations(conversion_service=AsyncMock(spec=FileConversionService), drive_service=drive,
                          notification=notification)
    task = asyncio.ensure_future(ops.delete(CTX, "u1", "drive:f1", wait_s=30))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(ops._armed) == 1
    release.set()
    await asyncio.sleep(0.05)
    await asyncio.gather(*list(ops._notices), return_exceptions=True)
    assert not ops._armed


# -- M3: an empty ref is not given --------------------------------------------------------------------------------------

@pytest.mark.parametrize("name, args, method", [
    (tt.OPEN_DRIVE_FILE, {"file_ref": ""}, "open"), (tt.OPEN_DRIVE_FILE, {}, "open"),
    (tt.GET_RECORDING_LINK, {"file_ref": " "}, "open"), (tt.COPY_FILE_TO_DRIVE, {"source_ref": ""}, "copy"),
    (tt.COPY_FILE_TO_DRIVE, {"folder": "X"}, "copy")])
async def test_an_empty_ref_is_refused_before_any_call(ops, name, args, method):
    run, collector = _run(ops)
    result = await _call(run, name, **args)
    getattr(ops, method).assert_not_called()
    assert "is required" in result.result_str
    assert collector.results[0].outcome.cls is OutcomeClass.REQUEST_ERROR


# -- M4 and M7 ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", [tt.MOVE_FILE, tt.DELETE_DRIVE_ITEM])
def test_move_and_delete_say_file_or_folder(name):
    declaration = next(d for d in tt.drive_tool_declarations() if d["name"] == name)
    assert "file or folder" in declaration["parameters"]["properties"]["file_ref"]["description"]


async def test_the_move_refusal_is_the_services_own_sentence():
    """One constant: the tool says before any call what UserDriveService.move raises."""
    from unittest.mock import MagicMock

    from src.domain.user_drive import DrivePathError
    with pytest.raises(DrivePathError) as raised:
        await UserDriveService.move(MagicMock(), "u1", "i1", " ", None)
    assert str(raised.value) == MOVE_NEEDS_TARGET
