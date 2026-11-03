"""DriveOperations mutation survivors, batch p2-b: the wait of a mutation (`save_text`, `create_folder`), the
described-files context of a passages render, and the log of a failed late note receipt."""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

from src.domain.drive_excerpt_context import ExcerptContext
from src.domain.drive_excerpt_file_context import FileContext
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome_class import OutcomeClass
from src.infrastructure.drive_operations import STILL_RUNNING, DriveOperations
from src.services.drive_search_service import DriveSearchService
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_service_connections import ConnectionReads
from tests.unit.domain.drive_excerpt_builders import TOMS_PLAN, chunk, excerpt_file, result

CTX = {"user_id": "u1", "account_id": "a1"}


class _Reads(ConnectionReads):
    pass


def _ops():
    drive = AsyncMock(spec=UserDriveService)
    reads = _Reads()
    drive.primary.side_effect = reads.primary
    drive.mounts.side_effect = reads.mounts
    drive.resolve_ref.side_effect = reads.resolve_ref
    ops = DriveOperations(conversion_service=MagicMock(spec=FileConversionService), drive_service=drive)
    return ops, drive


def _slow(delay: float = 0.15):
    async def run(*a, **k):
        await asyncio.sleep(delay)
        raise RuntimeError("provider is slow and then fails")
    return run


async def _settle(ops):
    while ops._shielded or ops._notices:
        await asyncio.sleep(0.01)


async def test_a_slow_save_text_answers_still_running_after_its_wait():
    ops, drive = _ops()
    drive.save.side_effect = _slow()
    out = await ops.save_text(CTX, "u1", "hello", file_name="note.md", wait_s=0.01)
    assert (out.op, out.cls, out.text, out.success) == (DriveOp.SAVE_TEXT, OutcomeClass.STILL_RUNNING,
                                                        STILL_RUNNING, True)
    await _settle(ops)


async def test_a_slow_create_folder_answers_still_running_after_its_wait():
    ops, drive = _ops()
    drive.ensure_folder.side_effect = _slow()
    out = await ops.create_folder(CTX, "u1", "Work", wait_s=0.01)
    assert (out.op, out.cls, out.text, out.success) == (DriveOp.CREATE_FOLDER, OutcomeClass.STILL_RUNNING,
                                                        STILL_RUNNING, True)
    await _settle(ops)


async def test_a_file_the_reader_already_has_described_prints_no_description_again():
    described = FileContext("item-1", TOMS_PLAN, (), description="A plan of the Acme review steps.")
    found = result(excerpt_file("item-1", TOMS_PLAN, [chunk("item-1", 2, "After the first review, Acme signs off.")],
                                ref_tag="a1"),
                   question="q", context=ExcerptContext(files=(described,), folders={}), ref_tag="a1")
    search = AsyncMock(spec=DriveSearchService)
    search.excerpts.return_value = found
    ops = DriveOperations(conversion_service=MagicMock(spec=FileConversionService),
                          drive_service=AsyncMock(spec=UserDriveService), drive_search=search)
    fresh = await ops.find_passages(CTX, "u1", question="q")
    again = await ops.find_passages(CTX, "u1", question="q", described_files=frozenset({"item-1"}))
    assert "A plan of the Acme review steps." in fresh.text
    assert "A plan of the Acme review steps." not in again.text
    assert "After the first review, Acme signs off." in again.text


async def test_a_failing_late_note_receipt_is_logged_with_its_error_class(caplog):
    notification = AsyncMock()
    notification.notify_drive_note_receipt.side_effect = RuntimeError("chat down")
    ops = DriveOperations(conversion_service=MagicMock(), notification=notification)
    item = MagicMock(type="drive_note_receipt")
    with caplog.at_level(logging.ERROR):
        ops.post_late_note_receipt("user-12345", "acct-1", item)
        for _ in range(5):
            await asyncio.sleep(0)
    lines = [r.getMessage() for r in caplog.records if "failed" in r.getMessage()]
    assert any("RuntimeError" in line and "user-123" in line for line in lines)
