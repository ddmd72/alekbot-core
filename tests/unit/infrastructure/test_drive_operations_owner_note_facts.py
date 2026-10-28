"""The owner-note read fact and its wiring (DRIVE_SPECIALIST_RFC §9.3 B8; U2 design §4.5, Δ9): an open that added the
note's block reports `owner_note`, a search with a noted card does, and neither without a note; the factory hands
DriveOperations the note service; the module reads no UserDrivePort. Invented items and notes only."""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.domain.drive_note import DriveNote, NoteBinding
from src.domain.user_drive import DriveConnection, DriveItem
from src.infrastructure.drive_operations import DriveOperations
from src.services.drive_note_service import DriveNoteService
from src.services.drive_search_service import DriveSearchService
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService

CTX = {"user_id": "u1", "account_id": "a1"}
DOC = DriveItem(item_id="d1", name="bathroom-invoice.md", path="Home/bathroom-invoice.md", is_folder=False,
                size_bytes=100, mime_type="text/markdown")


def _ops(with_note: bool) -> DriveOperations:
    conversion = AsyncMock(spec=FileConversionService)
    conversion.get_drive_item.return_value = DOC
    conversion.resolve_content.return_value = "Line one of the file."
    drive = AsyncMock(spec=UserDriveService)
    drive.connection.return_value = DriveConnection(connected=True, drive_id="drv")
    search = AsyncMock(spec=DriveSearchService)
    search.archived_text.return_value = None
    notes = AsyncMock(spec=DriveNoteService)
    notes.note_for_item.return_value = DriveNote(
        note_id="n1", user_id="u1", state="active", current_version=1, text="the contractor's invoice",
        binding=NoteBinding(provider_key="microsoft_onedrive", drive_id="drv", item_id="d1")) if with_note else None
    return DriveOperations(conversion_service=conversion, drive_service=drive, drive_search=search, drive_notes=notes)


async def test_an_open_that_added_the_note_block_reports_the_fact():
    out = await _ops(True).open(CTX, "u1", "drive:d1")
    assert out.facts.owner_note is True and "noted by the owner" in out.text


async def test_an_open_without_a_note_reports_no_fact():
    out = await _ops(False).open(CTX, "u1", "drive:d1")
    assert out.facts.owner_note is False


def test_the_factory_hands_drive_operations_the_note_service():
    from src.composition.user_agent_factory import UserAgentFactory
    factory = UserAgentFactory.__new__(UserAgentFactory)
    factory.file_conversion_service = MagicMock()
    factory.notification_service = factory.user_drive_service = factory.localization = None
    factory.language_service = factory.drive_search_service = None
    factory.drive_note_service = MagicMock(name="notes")
    ctx = MagicMock()
    ctx.drive_operations = None
    assert factory._drive_operations(ctx)._drive_notes is factory.drive_note_service


def test_drive_operations_imports_no_user_drive_port():
    tree = ast.parse(Path("src/infrastructure/drive_operations.py").read_text())
    names = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
             for a in n.names} | {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not [x for x in names if "user_drive_port" in x or x == "UserDrivePort"]


async def test_a_search_with_a_noted_card_reports_the_fact():
    from src.domain.drive_search import STATUS_OK, DriveSearchCard, DriveSearchResult
    from src.domain.drive_tier_result import TieredResult

    def card(**kw):
        base = dict(ref="drive:f1", item_id="f1", name="lease.pdf", path="Inbox/lease.pdf", kind="pdf",
                    is_folder=False, summary="lease", nonce="abcdef12")
        return DriveSearchCard(**{**base, **kw})

    ops = _ops(False)
    ops._drive_search.search.return_value = DriveSearchResult(
        status=STATUS_OK, cards=(card(), card(owner_note="noted by the owner: «the contractor's invoice»")),
        tiered=TieredResult(concepts=("lease",)))
    assert (await ops.search(CTX, "u1", query="lease")).facts.owner_note is True
    ops._drive_search.search.return_value = DriveSearchResult(status=STATUS_OK, cards=(card(),),
                                                              tiered=TieredResult(concepts=("lease",)))
    assert (await ops.search(CTX, "u1", query="lease")).facts.owner_note is False
