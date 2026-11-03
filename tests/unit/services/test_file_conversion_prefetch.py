"""FileConversionService.prefetch (EXTERNAL_FILES_RFC §4.2, P18): text only, marked and cut (D20), a note instead of
text when the file cannot be shown, and never an exception."""
import logging
from typing import List, Optional

from tests.unit.services.test_file_conversion_open import (  # noqa: F401  (fixture and helpers)
    DEADLINE, PDF, RecordingDocuments, _plant, world,
)
from src.domain.document_limits import MAX_CONVERTED_CHARS
from src.domain.document_open_texts import audio_unavailable_note, conversion_alert
from src.domain.document_read import CALLER_PREFETCH, ReadOutcome, ReadStatus


async def test_prefetch_reads_as_text_only_under_the_prefetch_caller(world):
    prefetched = await world.service.prefetch("invoice_painting_es.pdf", "ann", "ann", deadline_at=DEADLINE)
    (request,) = world.documents.requests
    assert request.caller == CALLER_PREFETCH and request.native_formats == () and request.deadline_at == DEADLINE
    assert prefetched.note == ""
    assert "Total due: 120 EUR" in prefetched.content
    assert "external_content" in prefetched.content and "[File:" not in prefetched.content


async def test_prefetch_marks_a_drive_text_with_the_drive_tag(world):
    ref = _plant(world, "lab_report_2025_03.pdf", b"%PDF", PDF)
    prefetched = await world.service.prefetch(ref, "ann", "ann", deadline_at=DEADLINE)
    assert "drive_document" in prefetched.content and "Total due: 120 EUR" in prefetched.content


async def test_prefetch_cuts_long_text_at_the_one_presentation_cap(world):
    long_text = ("Paragraph about rent.\n\n" * 5000)
    world.documents.outcome = ReadOutcome(status=ReadStatus.TEXT, kind="pdf", text=long_text)
    prefetched = await world.service.prefetch("invoice_painting_es.pdf", "ann", "ann", deadline_at=DEADLINE)
    assert "was truncated" in prefetched.content
    assert prefetched.content.count("Paragraph about rent.") <= MAX_CONVERTED_CHARS // len("Paragraph about rent.")


async def test_prefetch_of_a_workspace_text_is_marked(world):
    world.workspace.seed("ann", "scratch/flat_viewing_uk.md", "# Flat\n- 2 rooms")
    prefetched = await world.service.prefetch("workspace:scratch/flat_viewing_uk.md", "ann", "ann",
                                              deadline_at=DEADLINE)
    assert "# Flat" in prefetched.content and "external_content" in prefetched.content


async def test_prefetch_of_a_skill_ref_is_the_unmarked_body(world):
    from unittest.mock import AsyncMock
    from src.services.file_conversion_service import FileConversionService
    from tests.support.drive_access_stub import allow_access
    skills = AsyncMock()
    skills.read.return_value = "Step 1"
    service = FileConversionService(storage=world.storage, skill_files=skills, access=allow_access())
    service.attach_documents(world.documents)
    prefetched = await service.prefetch("skill:greeter/SKILL.md", "ann", "ann", deadline_at=DEADLINE)
    assert prefetched.content == "[File: skill:greeter/SKILL.md]\nStep 1\n[/File: skill:greeter/SKILL.md]"


async def test_prefetch_of_a_folder_is_a_note(world):
    folder = world.drive.plant(world.conn.handle, "root", "Documents", folder=True)
    prefetched = await world.service.prefetch(f"drive:b2:{folder}", "ann", "ann", deadline_at=DEADLINE)
    assert prefetched.content == "" and "is a folder, not a file" in prefetched.note


async def test_prefetch_of_a_recording_is_the_audio_note(world):
    world.storage.files["voice_note_es.mp3"] = b"ID3"
    world.documents.outcome = ReadOutcome(status=ReadStatus.UNSUPPORTED, kind="audio")
    prefetched = await world.service.prefetch("voice_note_es.mp3", "ann", "ann", deadline_at=DEADLINE)
    assert prefetched.note == audio_unavailable_note("voice_note_es.mp3") and prefetched.content == ""


async def test_prefetch_of_a_failed_read_is_the_conversion_note(world, caplog):
    world.documents.outcome = ReadOutcome(status=ReadStatus.ERROR, kind="pdf", detail="crash")
    with caplog.at_level(logging.ERROR):
        prefetched = await world.service.prefetch("invoice_painting_es.pdf", "ann", "ann", deadline_at=DEADLINE)
    assert prefetched.note == conversion_alert("invoice_painting_es.pdf", PDF)
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_prefetch_turns_a_store_error_into_a_note_and_a_warning(world, caplog):
    with caplog.at_level(logging.WARNING):
        prefetched = await world.service.prefetch("docs/bob/0b1c-motor_policy_en.pdf", "ann", "ann",
                                                  deadline_at=DEADLINE)
    assert prefetched.content == "" and "docs/bob/0b1c-motor_policy_en.pdf" in prefetched.note
    assert "could not be opened" in prefetched.note
    assert any(r.levelno == logging.WARNING and "PermissionError" in r.getMessage() for r in caplog.records)


async def test_prefetch_turns_a_raising_read_into_a_note(world):
    world.documents.raises = RuntimeError("boom")
    prefetched = await world.service.prefetch("gym_pass_uk.md", "ann", "ann", deadline_at=DEADLINE)
    assert "could not be opened" in prefetched.note


async def test_prefetch_of_an_email_ref_is_refused_as_a_note(world):
    prefetched = await world.service.prefetch("email:msg-1/att-1", "ann", "ann", deadline_at=DEADLINE)
    assert "could not be opened" in prefetched.note and world.documents.requests == []
