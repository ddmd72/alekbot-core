"""The openers read through `FileConversionService.open` (EXTERNAL_FILES_RFC §4.2, §4.4, P15, D20): each passes its own
deadline, a stored file is marked and cut by its store's presenter, a native file attaches with its store's reminder,
the drive keeps its own presentation (§29) and every failure answers a note with a log line. Invented items only."""
from __future__ import annotations

import logging
import re
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agents.file_management_agent import FileManagementAgent
from src.domain import drive_ref
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage
from src.domain.document_limits import MAX_CONVERTED_CHARS, OPEN_MAX_CHARS
from src.domain.document_open_texts import conversion_alert, shown_line, storage_read_note
from src.domain.document_read import INTERACTIVE_OPEN_S, OPEN_MARGIN_S, ReadOutcome, ReadStatus
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_mounts import canonical_path
from src.domain.drive_op import DriveOp
from src.domain.drive_pass_through import PassThroughCollector
from src.domain.drive_open_bound import bound, cut_line, path_subject
from src.domain.drive_resolved_ref import ResolvedDriveRef
from src.domain.drive_text_extent import ExtentState, TextExtent
from src.domain.file_ref import FileRefKind
from src.domain.llm import ToolCall
from src.domain.native_format import NATIVE_FORMATS_KEY, NativeFormat, to_plain
from src.domain.untrusted_text import (
    EXTERNAL_REMINDER_ATTACHED, REFERENCE_REMINDER, REFERENCE_REMINDER_ATTACHED, wrap_untrusted,
)
from src.domain.user_drive import DriveItem, drive_label
from src.infrastructure.agent_manifest import Intent
from src.infrastructure.drive_operations import DriveOperations
from src.infrastructure.drive_tools import DriveToolRun
from src.services.drive_search_service import DriveSearchService
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_connection_service_fake import connection
from tests.support.opened_file import opened_body, opened_native, opened_note

CONN = connection("ann", ref_tag="a1", drive_identity="microsoft_onedrive:drv")
PDF = to_plain([NativeFormat("application/pdf", 20 * 1024 * 1024, 100)])
CTX = {"user_id": "ann", "account_id": "bob"}
NATIVE_CTX = {**CTX, NATIVE_FORMATS_KEY: PDF}
LAB = DriveItem(item_id="p1", name="lab_report_2025_03.pdf", path="Documents/lab_report_2025_03.pdf",
                is_folder=False, size_bytes=2048, mime_type="application/pdf")
SCAN = DriveItem(item_id="s1", name="scan_0012.pdf", path="Documents/scan_0012.pdf", is_folder=False,
                 size_bytes=4096, mime_type="application/pdf")
NOTES = DriveItem(item_id="n1", name="notes.md", path="Documents/notes.md", is_folder=False, size_bytes=300,
                  mime_type="text/markdown")
CLIP = DriveItem(item_id="v1", name="clip.mp4", path="Documents/clip.mp4", is_folder=False, size_bytes=900,
                 mime_type="video/mp4")
CHAT_REF = "invoice_painting_es.pdf"
WORKSPACE_REF = "workspace:binary_files/flat_viewing_uk.md"


@pytest.fixture
def conversion():
    return AsyncMock(spec=FileConversionService)


def _ops(conversion, item=NOTES, extent=TextExtent.UNKNOWN, notification=None):
    drive = AsyncMock(spec=UserDriveService)
    drive.resolve_ref.side_effect = lambda user_id, ref: ResolvedDriveRef(connection=CONN,
                                                                          item_id=drive_ref.parse(ref).item_id)
    drive.get_item.return_value = item
    search = AsyncMock(spec=DriveSearchService)
    search.archived_text.return_value = None
    search.text_extent.return_value = extent
    return DriveOperations(conversion_service=conversion, notification=notification or AsyncMock(),
                           drive_service=drive, drive_search=search)


@pytest.fixture
def fixed_nonce(monkeypatch):
    monkeypatch.setattr("src.infrastructure.drive_operations.secrets.token_hex", lambda n: "0123456789abcdef")


# -- deadlines -------------------------------------------------------------------------------------------------------

def _agent(ops, timeout_ms=120_000):
    return FileManagementAgent(config=AgentConfig(agent_id="file_management_agent_ann", agent_type="file_management",
                                                  capabilities={}, metadata={}, timeout_ms=timeout_ms),
                               conversion_service=AsyncMock(), storage=AsyncMock(), drive_operations=ops)


def _message(ref):
    return AgentMessage.create(sender="orchestrator", recipient="file_management_agent_ann",
                               intent=AgentIntent.QUERY, payload={"intent": Intent.OPEN_FILE, "file_ref": ref},
                               context=dict(CTX))


@pytest.mark.parametrize("ref, method", [(CHAT_REF, "fetch_stored"), ("drive:a1:n1", "open")])
async def test_file_management_passes_a_deadline_90_seconds_ahead(monkeypatch, ref, method):
    monkeypatch.setattr("src.agents.file_management_agent.time", SimpleNamespace(time=lambda: 1_000.0))
    ops = AsyncMock(spec=DriveOperations)
    ops.open.return_value = ops.fetch_stored.return_value = DriveOutcome(
        op=DriveOp.OPEN, cls=OutcomeClass.OK, text="ok", success=True)
    await _agent(ops).execute(_message(ref))
    assert getattr(ops, method).await_args.kwargs["deadline_at"] == 1_000.0 + 120 - OPEN_MARGIN_S == 1_090.0


async def test_the_drive_tool_passes_what_is_left_minus_the_margin(monkeypatch):
    monkeypatch.setattr("src.infrastructure.drive_tools.time", SimpleNamespace(time=lambda: 2_000.0,
                                                                              monotonic=lambda: 0.0))
    ops = AsyncMock(spec=DriveOperations)
    ops.open.return_value = DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.OK, text="ok", success=True)
    clock = SimpleNamespace(now=40.0)
    run = DriveToolRun(ops, PassThroughCollector(), (), CTX, "ann", deadline=150.0, clock=lambda: clock.now)
    run.step = 1
    await run.handlers["open_drive_file"](ToolCall(name="open_drive_file", args={"file_ref": "drive:a1:n1"}))
    assert ops.open.await_args.kwargs["deadline_at"] == 2_000.0 + (150.0 - 40.0) - OPEN_MARGIN_S


async def test_the_recording_link_reads_nothing_and_passes_no_deadline():
    ops = AsyncMock(spec=DriveOperations)
    ops.open.return_value = DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.OK, text="link", success=True)
    run = DriveToolRun(ops, PassThroughCollector(), (), CTX, "ann", deadline=150.0, clock=lambda: 0.0)
    run.step = 1
    await run.handlers["get_recording_link"](ToolCall(name="get_recording_link", args={"file_ref": "drive:a1:v1"}))
    assert "deadline_at" not in ops.open.await_args.kwargs


async def test_no_deadline_means_the_interactive_default(conversion, monkeypatch):
    monkeypatch.setattr("src.infrastructure.drive_operations.time", SimpleNamespace(time=lambda: 500.0))
    conversion.open.return_value = opened_body("hello")
    await _ops(conversion).fetch_stored(CTX, "ann", CHAT_REF)
    assert conversion.open.await_args.kwargs["deadline_at"] == 500.0 + INTERACTIVE_OPEN_S
    assert conversion.open.await_args.args == (CHAT_REF, "ann", "bob")


# -- stored files ----------------------------------------------------------------------------------------------------

async def test_a_stored_pdf_the_service_returns_native_attaches_with_the_neutral_reminder(conversion):
    conversion.open.return_value = opened_native("/tmp/invoice.pdf", "application/pdf")
    out = await _ops(conversion).fetch_stored(NATIVE_CTX, "ann", CHAT_REF, deadline_at=1.0)
    assert out.success and out.file_data == {"path": "/tmp/invoice.pdf", "mime_type": "application/pdf"}
    assert out.text == (f"{EXTERNAL_REMINDER_ATTACHED}\nFile '{CHAT_REF}' (application/pdf) is attached. "
                        "You can see and analyse it directly.")
    assert conversion.open.await_args.kwargs["consumer"] == tuple(NativeFormat(**f) for f in PDF)


async def test_no_declaration_asks_the_service_for_text_only(conversion):
    conversion.open.return_value = opened_body("hello")
    await _ops(conversion).fetch_stored(CTX, "ann", CHAT_REF, deadline_at=1.0)
    assert conversion.open.await_args.kwargs["consumer"] == ()


@pytest.mark.parametrize("ref, kind", [(CHAT_REF, FileRefKind.CHAT), (WORKSPACE_REF, FileRefKind.WORKSPACE),
                                       ("docs/report_2025.docx", FileRefKind.DELIVERED)])
async def test_stored_text_is_marked_and_cut_at_60000_with_the_note_outside(conversion, ref, kind):
    conversion.open.return_value = opened_body("a" * (MAX_CONVERTED_CHARS - 5) + "\n\n" + "b" * 100)
    out = await _ops(conversion).fetch_stored(CTX, "ann", ref, deadline_at=1.0)
    assert out.success and out.body
    closing = re.search(r'</external_content nonce="[0-9a-f]{16}">', out.text)
    assert re.search(r'<external_content nonce="[0-9a-f]{16}" ref=', out.text) and closing
    assert "b" * 100 not in out.text and "a" * (MAX_CONVERTED_CHARS - 5) in out.text[:closing.start()]
    assert "[System:" not in out.text[:closing.start()] and "[System:" in out.text[closing.end():]


async def test_a_stored_note_is_a_failed_open_shown_as_it_is(conversion):
    # A note is the system's sentence, never file content: a failed open, unmarked, never a body (U3 seam fix).
    note = conversion_alert(CHAT_REF, "application/pdf")
    conversion.open.return_value = opened_note(note, ReadStatus.ENCRYPTED)
    out = await _ops(conversion).fetch_stored(CTX, "ann", CHAT_REF, deadline_at=1.0)
    assert not out.success and out.cls is OutcomeClass.FAILED and not out.body
    assert out.text == note


async def test_a_skill_file_stays_the_systems_own_unmarked_text(conversion):
    conversion.open.return_value = opened_body("skill body")
    out = await _ops(conversion).fetch_stored(CTX, "ann", "skill:fs/r.md", deadline_at=1.0)
    assert out.text == "skill body"


async def test_a_stored_video_never_calls_open(conversion):
    conversion.resolve_bytes.return_value = b"video"
    out = await _ops(conversion).fetch_stored(CTX, "ann", "walkthrough.mp4", deadline_at=1.0)
    assert out.success and conversion.open.await_count == 0 and conversion.resolve_bytes.await_count == 1


# -- drive files -----------------------------------------------------------------------------------------------------

async def test_a_drive_text_open_is_byte_identical_to_todays_presentation(conversion, fixed_nonce):
    text = "Line one of the file.\n\nLine two."
    conversion.open.return_value = opened_body(text)
    out = await _ops(conversion).open(CTX, "ann", "drive:a1:n1", deadline_at=1.0)
    shown = out.items[0]
    wrapped = wrap_untrusted(f"{drive_label(shown, 'a1')}\n{text}", nonce="0123456789abcdef",
                             ref=drive_ref.format("a1", "n1"), store=FileRefKind.DRIVE, label=shown.path)
    assert out.text == f"{REFERENCE_REMINDER}\n{wrapped}" and out.body
    assert conversion.open.await_args.args[:3] == ("drive:a1:n1", "ann", "bob")


async def test_a_cut_drive_open_keeps_the_cut_line_and_adds_the_shown_line_after_it(conversion, fixed_nonce):
    text = "p" * (OPEN_MAX_CHARS - 10) + "\n\n" + "q" * 500
    outcome = ReadOutcome(status=ReadStatus.TEXT, pages_total=40, pages_read=12, skipped_pages=(7,))
    conversion.open.return_value = opened_body(text, outcome)
    out = await _ops(conversion, SCAN).open(CTX, "ann", "drive:a1:s1", deadline_at=1.0)
    shown = out.items[0]   # the item as the model sees it: its path carries the drive's mount
    cut = bound(text, TextExtent.UNKNOWN)
    wrapped = wrap_untrusted(f"{drive_label(shown, 'a1')}\n{cut.text}", nonce="0123456789abcdef",
                             ref=drive_ref.format("a1", "s1"), store=FileRefKind.DRIVE, label=shown.path)
    line = cut_line(path_subject(shown.path), cut, TextExtent.UNKNOWN)
    assert out.text == f"{REFERENCE_REMINDER}\n{wrapped}\n{line}\n{shown_line(shown.path, outcome)}"
    assert out.facts.cut and "pages 1–12 of 40 read in full" in out.text


async def test_a_scans_reader_text_gets_the_shown_line_outside_the_wrapper(conversion, fixed_nonce):
    outcome = ReadOutcome(status=ReadStatus.TEXT, pages_total=3, pages_read=3)
    conversion.open.return_value = opened_body("scanned words", outcome)
    out = await _ops(conversion, SCAN).open(CTX, "ann", "drive:a1:s1", deadline_at=1.0)
    assert out.text.endswith(shown_line(out.items[0].path, outcome))
    assert "</drive_document" in out.text.split("[System:")[0]


async def test_a_drive_pdf_the_service_returns_native_attaches_with_the_drive_reminder(conversion):
    conversion.open.return_value = opened_native("/tmp/lab.pdf", "application/pdf")
    out = await _ops(conversion, LAB).open(NATIVE_CTX, "ann", "drive:a1:p1", deadline_at=1.0)
    assert out.file_data == {"path": "/tmp/lab.pdf", "mime_type": "application/pdf"}
    assert out.text.startswith(REFERENCE_REMINDER_ATTACHED) and "is attached" in out.text


async def test_a_refused_drive_item_never_calls_open(conversion):
    extent = TextExtent(ExtentState.PAGEABLE, text_chars=OPEN_MAX_CHARS + 1, passages=40)
    out = await _ops(conversion, NOTES, extent).open(CTX, "ann", "drive:a1:n1", deadline_at=1.0)
    assert out.cls is OutcomeClass.REFUSED and out.facts.too_long
    assert conversion.open.await_count == 0 and conversion.resolve_bytes.await_count == 0


async def test_a_drive_video_never_calls_open(conversion):
    conversion.resolve_bytes.return_value = b"video"
    notification = AsyncMock()
    notification.notify_file_bytes.return_value = True
    ops = _ops(conversion, CLIP, notification=notification)
    ops._drive_search.transcript_text.return_value = None   # no transcript: the video is sent as a file
    out = await ops.open(CTX, "ann", "drive:a1:v1", deadline_at=1.0)
    assert out.success and "sent to the user" in out.text
    assert conversion.open.await_count == 0


async def test_an_archived_transcript_read_goes_through_open_as_text(conversion):
    ops = _ops(conversion, NOTES)
    conversion.open.return_value = opened_body("transcript words")

    async def archived(conn, item_id, *, label, path, file_text):
        return await file_text()
    ops._drive_search.archived_text.side_effect = archived
    out = await ops.open(CTX, "ann", "drive:a1:n1", deadline_at=77.0)
    assert out.text == "transcript words"
    assert conversion.open.await_args.kwargs["consumer"] == () and conversion.open.await_args.kwargs["deadline_at"] == 77.0


# -- failure paths: a specific note and a log line -------------------------------------------------------------------

@pytest.mark.parametrize("status, level", [(ReadStatus.PARSER_UNAVAILABLE, logging.ERROR),
                                           (ReadStatus.MODEL_UNAVAILABLE, logging.ERROR),
                                           (ReadStatus.MEMORY, logging.ERROR),
                                           (ReadStatus.ERROR, logging.ERROR),
                                           (ReadStatus.TIMEOUT, logging.WARNING),
                                           (ReadStatus.ENCRYPTED, logging.WARNING),
                                           (ReadStatus.UNSUPPORTED, logging.WARNING),
                                           (ReadStatus.TOO_LARGE, logging.WARNING),
                                           (ReadStatus.NO_TEXT, logging.WARNING)])
async def test_a_stored_note_is_answered_and_logged(conversion, caplog, status, level):
    note = f"[System: note for {status.value}]"
    conversion.open.return_value = opened_note(note, status, detail="busy")
    with caplog.at_level(logging.WARNING):
        out = await _ops(conversion).fetch_stored(CTX, "ann", CHAT_REF, deadline_at=1.0)
    assert not out.success and out.cls is OutcomeClass.FAILED and not out.body and out.text == note
    records =[r for r in caplog.records if "open gave a note" in r.getMessage()]
    assert len(records) == 1 and records[0].levelno == level
    assert CHAT_REF in records[0].getMessage() and status.value in records[0].getMessage()


async def test_a_drive_note_is_a_failed_open_outside_the_wrapper_and_logged(conversion, caplog):
    # A note is never file text: no wrapper, no reminder, no body, no success; the drive's label line names the file.
    note = "[System: the parser is busy]"
    conversion.open.return_value = opened_note(note, ReadStatus.PARSER_UNAVAILABLE, detail="busy")
    with caplog.at_level(logging.WARNING):
        out = await _ops(conversion, SCAN).open(CTX, "ann", "drive:a1:s1", deadline_at=1.0)
    shown = replace(SCAN, path=canonical_path(CONN.mount_name, SCAN.path))
    assert not out.success and out.cls is OutcomeClass.FAILED and not out.body and out.file_data is None
    assert out.text == f"{drive_label(shown, 'a1')}\n{note}"
    assert "<drive_document" not in out.text and REFERENCE_REMINDER not in out.text
    assert any(r.levelno == logging.ERROR and "busy" in r.getMessage() for r in caplog.records)
    assert len([r for r in caplog.records if "open gave a note" in r.getMessage()]) == 1


async def test_an_open_that_raises_is_a_specific_answer_and_an_error_line(conversion, caplog):
    conversion.open.side_effect = RuntimeError("parser exploded")
    with caplog.at_level(logging.WARNING):
        out = await _ops(conversion).fetch_stored(CTX, "ann", CHAT_REF, deadline_at=1.0)
    assert not out.success and out.cls is OutcomeClass.FAILED
    assert out.text == storage_read_note(CHAT_REF) and "parser exploded" not in out.text
    assert any(r.levelno == logging.ERROR for r in caplog.records)
