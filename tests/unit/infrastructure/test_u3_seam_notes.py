"""U3 seam fixes: a failed read is an honest note (never file text) plus a log line; the notes of a storage error and
of a resolver crash say what happened. Invented items only."""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from src.domain.document_open_texts import open_failed_note, storage_read_note
from src.domain.document_read import ReadStatus
from src.domain.drive_outcome_class import OutcomeClass
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_registry import AgentDescriptor, AgentRegistry, ExecutionMode
from src.services.file_conversion_service import FileConversionService
from tests.support.opened_file import opened_note
from tests.unit.infrastructure.test_drive_operations_open_through_service import CTX, NOTES, _ops

CHAT_REF = "invoice_painting_es.pdf"


@pytest.mark.parametrize("status, level", [(ReadStatus.PARSER_UNAVAILABLE, logging.ERROR),
                                           (ReadStatus.ENCRYPTED, logging.WARNING)])
async def test_a_drive_note_is_a_failed_open_not_file_text(status, level, caplog):
    note = f"[System: The file '{NOTES.path}' could not be read. Do not guess what it contains.]"
    conversion = AsyncMock(spec=FileConversionService)
    conversion.open.return_value = opened_note(note, status, detail="busy")
    with caplog.at_level(logging.DEBUG):
        out = await _ops(conversion).open(CTX, "ann", "drive:a1:n1", deadline_at=1.0)
    assert not out.success and out.cls is OutcomeClass.FAILED and not out.body
    assert out.text.endswith(f"\n{note}") and out.text.startswith("[Drive: ") and "ref=drive:a1:n1" in out.text
    assert "<drive_document" not in out.text and "untrusted" not in out.text.lower()
    records = [r for r in caplog.records if "open gave a note" in r.getMessage()]
    assert len(records) == 1 and records[0].levelno == level and NOTES.name not in records[0].getMessage()


async def test_a_storage_download_error_says_storage_not_conversion(caplog):
    conversion = AsyncMock(spec=FileConversionService)
    conversion.open.side_effect = RuntimeError("503 from storage")
    with caplog.at_level(logging.DEBUG):
        out = await _ops(conversion).fetch_stored(CTX, "ann", CHAT_REF, deadline_at=1.0)
    assert not out.success and out.cls is OutcomeClass.FAILED
    assert out.text == storage_read_note(CHAT_REF)
    assert "converted" not in out.text and "storage" in out.text
    assert any(r.levelno == logging.ERROR and "fetch failed" in r.getMessage() for r in caplog.records)


async def test_a_resolver_crash_gets_the_could_not_be_opened_note(caplog):
    registry = AgentRegistry()
    registry.register(AgentDescriptor(agent_id="files", agent_type="files",
                                      capabilities={"open_x": ExecutionMode.SYNC}, prefetch_file_ref=True))
    coord = AgentCoordinator(registry=registry)
    coord._file_ref_resolver = AsyncMock(side_effect=RuntimeError("boom"))
    params = {"file_ref": CHAT_REF}
    with caplog.at_level(logging.DEBUG):
        await coord.prefetch_file_ref("files", params, {"user_id": "ann"}, deadline_at=1.9e9)
    assert params["file_note"] == open_failed_note(CHAT_REF)
    assert "supported text format" not in params["file_note"]
    assert any(r.levelno == logging.ERROR and CHAT_REF in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("ref", [CHAT_REF, "workspace:binary_files/flat_viewing_uk.pdf"])
@pytest.mark.parametrize("status, level", [(ReadStatus.MEMORY, logging.ERROR), (ReadStatus.ENCRYPTED, logging.WARNING)])
async def test_a_stored_note_is_a_failed_open_unwrapped(ref, status, level, caplog):
    note = f"[System: The file '{ref}' could not be read. Do not guess what it contains.]"
    conversion = AsyncMock(spec=FileConversionService)
    conversion.open.return_value = opened_note(note, status, detail="oom")
    with caplog.at_level(logging.DEBUG):
        out = await _ops(conversion).fetch_stored(CTX, "ann", ref, deadline_at=1.0)
    assert not out.success and out.cls is OutcomeClass.FAILED and not out.body
    assert out.text == note and "untrusted" not in out.text.lower()
    records = [r for r in caplog.records if "open gave a note" in r.getMessage()]
    assert len(records) == 1 and records[0].levelno == level and status.value in records[0].getMessage()
