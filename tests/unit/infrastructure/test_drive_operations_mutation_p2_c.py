"""DriveOperations mutation survivors (weekly run P2-c): the failure branches that must answer, not return nothing.

A stored file that is gone answers a NOT_FOUND outcome; an item gone from the drive during a copy answers GONE; the
mirror-refusal line survives a failing language lookup (English); an archived-transcript read that finds the item gone
lets the open answer GONE."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain import drive_ref
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_resolved_ref import ResolvedDriveRef
from src.domain.language import LanguageCode
from src.domain.ui_messages import UIMessage
from src.domain.user_drive import DriveItem, DriveItemNotFoundError, DriveMirrorWriteRefused
from src.infrastructure.drive_operations import GONE, DriveOperations
from src.services.drive_search_service import DriveSearchService
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_connection_service_fake import connection
from tests.support.drive_service_connections import ConnectionReads

CTX = {"user_id": "user-1", "account_id": "acct-1"}
CONN = connection("user-1", ref_tag="a1", drive_identity="microsoft_onedrive:drv")
NOTE = DriveItem(item_id="n1", name="notes.md", path="Documents/notes.md", is_folder=False, size_bytes=300,
                 mime_type="text/markdown")
MIRROR_EN = ("This folder is managed by the bot and its files are overwritten; "
             "to keep your own version, copy the file to another folder.")


async def test_a_stored_file_that_is_gone_answers_not_found_with_the_storage_sentence():
    conversion = AsyncMock(spec=FileConversionService)
    conversion.open.side_effect = FileNotFoundError("gone")
    out = await DriveOperations(conversion_service=conversion).fetch_stored(CTX, "user-1", "invoice.pdf")
    assert (out.op, out.cls, out.success) == (DriveOp.OPEN, OutcomeClass.NOT_FOUND, False)
    assert out.text.startswith("File 'invoice.pdf' not found in storage.")


async def test_a_copy_source_gone_from_the_drive_answers_gone():
    conversion = AsyncMock(spec=FileConversionService)
    conversion.resolve_bytes.side_effect = DriveItemNotFoundError("x")
    drive = AsyncMock(spec=UserDriveService)
    out = await DriveOperations(conversion_service=conversion, drive_service=drive).copy(
        CTX, "user-1", "chat_file.pdf", wait_s=5)
    assert (out.op, out.cls, out.text, out.success) == (DriveOp.COPY, OutcomeClass.NOT_FOUND, GONE, False)
    assert drive.mock_calls == []   # nothing was stored


@pytest.mark.parametrize("with_localization", [False, True])
async def test_the_mirror_refusal_line_falls_back_to_english_when_the_language_lookup_fails(with_localization):
    drive = AsyncMock(spec=UserDriveService)
    reads = ConnectionReads()
    drive.primary.side_effect = reads.primary
    drive.mounts.side_effect = reads.mounts
    drive.resolve_ref.side_effect = reads.resolve_ref
    drive.move.side_effect = DriveMirrorWriteRefused("x")
    language = AsyncMock()
    language.resolve_ui_language.side_effect = RuntimeError("boom")
    localization = None
    if with_localization:
        localization = MagicMock()
        localization.get_ui_string.side_effect = lambda lang, m: f"{lang.value}:{m.value}"
    ops = DriveOperations(conversion_service=AsyncMock(spec=FileConversionService), drive_service=drive,
                          localization=localization, language_service=language)
    out = await ops.move(CTX, "user-1", "drive:a1:f1", folder="Archive", wait_s=5)
    assert (out.cls, out.success) == (OutcomeClass.REFUSED, False)
    expected = (f"{LanguageCode.EN.value}:{UIMessage.DRIVE_MIRROR_WRITE_REFUSED.value}" if with_localization
                else MIRROR_EN)
    assert out.text == expected


async def test_an_archived_transcript_lookup_that_finds_the_item_gone_answers_gone():
    drive = AsyncMock(spec=UserDriveService)
    drive.resolve_ref.side_effect = lambda user_id, ref: ResolvedDriveRef(
        connection=CONN, item_id=drive_ref.parse(ref).item_id)
    drive.get_item.return_value = NOTE
    search = AsyncMock(spec=DriveSearchService)
    search.archived_text.side_effect = DriveItemNotFoundError("x")
    search.access_note.return_value = None
    ops = DriveOperations(conversion_service=AsyncMock(spec=FileConversionService), drive_service=drive,
                          drive_search=search)
    out = await ops.open(CTX, "user-1", "drive:a1:n1", deadline_at=1.0)
    assert (out.op, out.cls, out.text, out.success) == (DriveOp.OPEN, OutcomeClass.NOT_FOUND, GONE, False)
