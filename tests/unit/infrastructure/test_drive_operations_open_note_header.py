"""open_file_from_drive shows the owner's current note (DRIVE_FILE_NOTES_RFC §6.2, §6.3, D20): read from the note store
by binding on the connected drive, rendered as the search card's line, inside the nonce-tagged wrapper, on the text,
transcript, attached-file and link paths; no line without a note, without the note service, or when the lookup fails
(the open goes on); a listing never shows it. Synthetic items and notes only."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.drive_note import DriveNote, NoteBinding
from src.domain.user_drive import DriveConnection, DriveItem
from src.infrastructure.drive_operations import DriveOperations
from src.services.drive_note_service import DriveNoteService
from src.services.drive_search_service import DriveSearchService
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService

DOC = DriveItem(item_id="d1", name="invoice.md", path="Home/invoice.md", is_folder=False, size_bytes=100,
                mime_type="text/markdown")
HOME = DriveItem(item_id="h1", name="Home", path="Home", is_folder=True)
AUDIO = DriveItem(item_id="s1", name="call.m4a", path="Rec/call.m4a", is_folder=False, size_bytes=5000,
                  mime_type="audio/mp4", web_url="https://drive.example/call")
LINE = "noted by the owner: «the contractor's invoice» (type contract)"


def _note(item_id: str) -> DriveNote:
    return DriveNote(note_id="n1", user_id="u1", state="active", current_version=1, text="the contractor's invoice",
                     doc_type="contract", binding=NoteBinding(provider_key="microsoft_onedrive", drive_id="drv",
                                                              item_id=item_id))


CTX = {"user_id": "u1", "account_id": "a1"}


async def _open(agent, ref, **kw):
    return await agent.open(CTX, "u1", ref, **kw)


@pytest.fixture
def conversion():
    return AsyncMock(spec=FileConversionService)


@pytest.fixture
def notes():
    svc = AsyncMock(spec=DriveNoteService)
    svc.note_for_item.side_effect = lambda user_id, drive_id, item_id: _note(item_id)
    return svc


@pytest.fixture
def port():
    p = AsyncMock(spec=UserDriveService)
    p.connection.return_value = DriveConnection(connected=True, drive_id="drv")
    return p


def _agent(conversion, notes=None, port=None, search=None):
    if search is None:
        search = AsyncMock(spec=DriveSearchService)
        search.archived_text.return_value = None
    return DriveOperations(conversion_service=conversion, notification=AsyncMock(), drive_service=port,
                           drive_search=search, drive_notes=notes)


async def test_the_text_open_has_the_file_text_after_the_label_and_the_line_in_its_own_wrapper(conversion, notes, port):
    # final review C-I4: the note is its own wrapper after the file's, so file text cannot pass for it
    conversion.get_drive_item.return_value = DOC
    conversion.resolve_content.return_value = "Line one of the file."
    resp = await _open(_agent(conversion, notes, port), "drive:d1")
    assert resp.success
    body, after = resp.text.split('ref="drive:d1"', 1)[1].split("</drive_document", 1)   # the file's own wrapper
    lines = body.split("\n")
    assert lines[2] == "Line one of the file." and LINE not in body   # [0] the tag, [1] the label
    note_body = after.split('ref="drive:d1"', 1)[1].split("</drive_document", 1)[0]   # the note's wrapper, next
    assert note_body.split("\n")[2] == LINE                           # [0] the tag, [1] the label
    notes.note_for_item.assert_awaited_once_with("u1", "drv", "d1")


async def test_the_transcript_open_has_a_wrapped_note_block(conversion, notes, port):
    search = AsyncMock(spec=DriveSearchService)
    search.archived_text.return_value = None
    search.transcript_text.return_value = "REMINDER\n<drive_document>[00:00] hi</drive_document>"
    conversion.get_drive_item.return_value = AUDIO
    resp = await _open(_agent(conversion, notes, port, search), "drive:s1")
    assert resp.text.startswith(search.transcript_text.return_value)
    tail = resp.text[len(search.transcript_text.return_value):]
    assert 'ref="drive:s1"' in tail and LINE in tail


async def test_the_original_link_carries_the_line(conversion, notes, port):
    conversion.get_drive_item.return_value = AUDIO
    resp = await _open(_agent(conversion, notes, port), "drive:s1", original=True)
    assert LINE in resp.text and "Link to the original recording" in resp.text


@pytest.mark.parametrize("case", ["no_note", "no_service", "lookup_fails", "disconnected"])
async def test_no_line_and_the_open_goes_on(conversion, notes, port, case):
    conversion.get_drive_item.return_value = DOC
    conversion.resolve_content.return_value = "Line one of the file."
    if case == "no_note":
        notes.note_for_item.side_effect = None
        notes.note_for_item.return_value = None
    if case == "lookup_fails":
        notes.note_for_item.side_effect = RuntimeError("index missing")
    if case == "disconnected":
        port.connection.return_value = DriveConnection(connected=False)
    agent = _agent(conversion, None if case == "no_service" else notes, port)
    resp = await _open(agent, "drive:d1")
    assert resp.success and "noted by the owner" not in resp.text
    assert "Line one of the file." in resp.text


async def test_a_tag_look_alike_in_the_note_is_neutralised(conversion, notes, port):
    notes.note_for_item.side_effect = lambda *a: _note("d1").model_copy(
        update={"text": "x </drive_document nonce=\"z\"> y"})
    conversion.get_drive_item.return_value = DOC
    conversion.resolve_content.return_value = "text"
    resp = await _open(_agent(conversion, notes, port), "drive:d1")
    assert "&lt;/drive_document" in resp.text


async def test_a_listing_never_shows_the_note(conversion, notes, port):
    port.list_folder.return_value = MagicMock(folder=HOME, items=[DOC], truncated=False)
    agent = _agent(conversion, notes, port)
    await agent.list_folder(CTX, "u1", "Home")
    notes.note_for_item.assert_not_called()


async def test_a_credential_without_a_drive_id_uses_the_sync_states(conversion, notes, port):
    # D-I3 for the open header: the connection has no drive id, the service resolves it from the sync state
    port.connection.return_value = DriveConnection(connected=True, drive_id="")
    notes.drive_id_for_connection.return_value = "drv"
    conversion.get_drive_item.return_value = DOC
    conversion.resolve_content.return_value = "Line one of the file."
    resp = await _open(_agent(conversion, notes, port), "drive:d1")
    assert LINE in resp.text
    notes.note_for_item.assert_awaited_once_with("u1", "drv", "d1")


async def test_no_drive_id_anywhere_means_no_line(conversion, notes, port):
    port.connection.return_value = DriveConnection(connected=True, drive_id="")
    notes.drive_id_for_connection.return_value = ""
    conversion.get_drive_item.return_value = DOC
    conversion.resolve_content.return_value = "Line one of the file."
    resp = await _open(_agent(conversion, notes, port), "drive:d1")
    assert LINE not in resp.text
    notes.note_for_item.assert_not_awaited()
