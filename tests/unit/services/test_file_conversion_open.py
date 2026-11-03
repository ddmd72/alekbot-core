"""FileConversionService.open (EXTERNAL_FILES_RFC §4.2, U3 part C): every store resolves, sizes on metadata, stages and
reads through the attached DocumentReadPort; nothing is parsed in this process. Real service over fakes."""
import logging
import os
from typing import List, Optional
from unittest.mock import AsyncMock

import pytest

from src.domain.document_limits import MAX_FILE_BYTES, MAX_TEXT_EXPORT_BYTES
from src.domain.document_open_texts import (
    audio_unavailable_note, conversion_alert, deadline_note, model_unavailable_note, parser_unavailable_note,
    size_alert,
)
from src.domain.document_read import CALLER_OPEN, CALLER_PREFETCH, ReadOutcome, ReadRequest, ReadStatus
from src.domain.drive_feed_scope import FeedScope
from src.domain.native_format import NativeFormat
from src.domain.workspace import OPEN_FILE_MAX_TEXT_BYTES
from src.ports.document_read_port import DocumentReadPort
from src.ports.file_storage_port import FileStoragePort
from src.services.drive_connection_service import DriveConnectionService
from src.services.file_conversion_service import FileConversionService
from tests.support.drive_access_stub import allow_access
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import seed
from tests.support.fake_drive_adapter import FakeDriveAdapter
from tests.unit.services.workspace_fakes import InMemoryWorkspaceStore

MB = 1024 * 1024
PDF = "application/pdf"
DEADLINE = 1_900_000_000.0


class RecordingDocuments(DocumentReadPort):
    def __init__(self, outcome: Optional[ReadOutcome] = None, raises: Optional[Exception] = None) -> None:
        self.requests: List[ReadRequest] = []
        self.outcome = outcome or ReadOutcome(status=ReadStatus.TEXT, kind="pdf", text="Total due: 680,00 EUR")
        self.raises = raises

    async def read(self, request: ReadRequest) -> ReadOutcome:
        self.requests.append(request)
        if self.raises:
            raise self.raises
        return self.outcome


class CountingStorage(FileStoragePort):
    """Chat uploads: bytes by name; counts downloads."""
    def __init__(self, files=None) -> None:
        self.files = files or {}
        self.downloads: List[str] = []

    async def download(self, filename, user_id):
        self.downloads.append(filename)
        return self.files[filename]

    async def upload(self, *a, **k): raise NotImplementedError
    async def delete(self, *a, **k): raise NotImplementedError
    async def exists(self, *a, **k): raise NotImplementedError
    async def list_files(self, *a, **k): raise NotImplementedError
    async def get_url(self, *a, **k): raise NotImplementedError


class StagingDrive(FakeDriveAdapter):
    """Remembers where `download_to_file` was asked to write and counts both download methods."""
    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.staged: List[str] = []
        self.byte_downloads = 0

    async def download(self, handle, item_id):
        self.byte_downloads += 1
        return await super().download(handle, item_id)

    async def download_to_file(self, handle, item_id, dest_path, max_bytes):
        self.staged.append(dest_path)
        return await super().download_to_file(handle, item_id, dest_path, max_bytes)


@pytest.fixture
async def world():
    class World:
        pass
    w = World()
    repo = FakeDriveConnectionRepository()
    w.conn = await seed(repo, user_id="ann", account_id="ann", connection_id="conn-2", tag="b2", kind="fake_b")
    w.drive = StagingDrive("fake_b", feed_scope=FeedScope.ACCOUNT)
    w.storage = CountingStorage({"invoice_painting_es.pdf": b"%PDF-1", "gym_pass_uk.md": b"# Gym pass\nOpen 7-22"})
    w.media = AsyncMock()
    w.media.fetch.return_value = b"%PDF-delivered"
    w.workspace = InMemoryWorkspaceStore()
    w.documents = RecordingDocuments()
    w.service = FileConversionService(
        storage=w.storage, media_storage=w.media, drive=w.drive, workspace=w.workspace,
        connections=DriveConnectionService(repo, None), access=allow_access())
    w.service.attach_documents(w.documents)
    return w


def _ref(item_id):
    return f"drive:b2:{item_id}"


def _plant(w, name, data, mime):
    return _ref(w.drive.plant(w.conn.handle, "root", name, data, mime=mime))


# -- one read request per store ---------------------------------------------------------------------------------

async def test_a_chat_file_is_read_with_its_store_caller_mime_consumer_and_deadline(world):
    consumer = (NativeFormat(PDF, 5 * MB, 100),)
    opened = await world.service.open("invoice_painting_es.pdf", "ann", "ann-acc", consumer=consumer,
                                      deadline_at=DEADLINE)
    (request,) = world.documents.requests
    assert (request.store, request.caller, request.declared_mime) == ("chat", CALLER_OPEN, PDF)
    assert (request.native_formats, request.deadline_at) == (consumer, DEADLINE)
    assert (request.user_id, request.account_id, request.content) == ("ann", "ann-acc", b"%PDF-1")
    assert request.request_id
    assert opened.body == "[File: invoice_painting_es.pdf]\nTotal due: 680,00 EUR\n[/File: invoice_painting_es.pdf]"
    assert opened.outcome is world.documents.outcome


async def test_the_caller_is_passed_through(world):
    await world.service.open("gym_pass_uk.md", "ann", "ann", deadline_at=DEADLINE, caller=CALLER_PREFETCH)
    assert world.documents.requests[0].caller == CALLER_PREFETCH
    assert world.documents.requests[0].declared_mime == "text/markdown"


async def test_a_delivered_key_reads_as_delivered_after_the_ownership_check(world):
    key = "docs/ann/0b1c-motor_policy_en.pdf"
    opened = await world.service.open(key, "ann", "ann", deadline_at=DEADLINE)
    assert world.documents.requests[0].store == "delivered"
    assert world.documents.requests[0].content == b"%PDF-delivered"
    assert opened.body.startswith(f"[File: {key}]")


async def test_a_delivered_key_of_another_user_is_refused_before_any_fetch(world):
    with pytest.raises(PermissionError):
        await world.service.open("docs/bob/0b1c-motor_policy_en.pdf", "ann", "ann", deadline_at=DEADLINE)
    world.media.fetch.assert_not_called()
    assert world.documents.requests == []


async def test_a_workspace_binary_reads_as_workspace(world):
    world.workspace.seed("ann", "binary_files/flat_viewing_uk.md.pdf", b"%PDF-ws")
    await world.service.open("workspace:binary_files/flat_viewing_uk.md.pdf", "ann", "ann", deadline_at=DEADLINE)
    (request,) = world.documents.requests
    assert (request.store, request.declared_mime, request.content) == ("workspace", PDF, b"%PDF-ws")


async def test_a_workspace_text_is_decoded_verbatim_without_a_read(world):
    world.workspace.seed("ann", "scratch/flat_viewing_uk.md", "# Flat")
    opened = await world.service.open("workspace:scratch/flat_viewing_uk.md", "ann", "ann", deadline_at=DEADLINE)
    assert opened.body == ("[File: workspace:scratch/flat_viewing_uk.md]\n# Flat\n"
                           "[/File: workspace:scratch/flat_viewing_uk.md]")
    assert world.documents.requests == []


async def test_a_workspace_text_that_is_not_utf8_is_read_as_a_file(world):
    world.workspace.seed("ann", "scratch/old_notes.md", b"\xff\xfe\x00bad")
    await world.service.open("workspace:scratch/old_notes.md", "ann", "ann", deadline_at=DEADLINE)
    assert world.documents.requests[0].store == "workspace"


async def test_a_drive_file_reads_as_drive_under_its_path(world):
    ref = _plant(world, "lab_report_2025_03.pdf", b"%PDF-drive", PDF)
    item = await world.service.get_drive_item(ref, "ann")
    opened = await world.service.open(ref, "ann", "ann", deadline_at=DEADLINE)
    (request,) = world.documents.requests
    assert (request.store, request.declared_mime, request.content) == ("drive", PDF, b"%PDF-drive")
    assert opened.body.startswith(f"[File: {item.path}]\n")


async def test_a_native_sheet_without_an_extension_is_read_under_a_name_with_the_mime_suffix(world):
    xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ref = _plant(world, "Budget 2026", b"PK-sheet", xlsx)
    await world.service.open(ref, "ann", "ann", deadline_at=DEADLINE)
    assert world.documents.requests[0].name.endswith(".xlsx")


async def test_a_skill_ref_is_never_read_or_marked(world):
    skills = AsyncMock()
    skills.read.return_value = "Step 1: greet"
    service = FileConversionService(storage=world.storage, skill_files=skills, access=allow_access())
    service.attach_documents(world.documents)
    opened = await service.open("skill:greeter/SKILL.md", "ann", "ann", deadline_at=DEADLINE)
    assert opened.body == "[File: skill:greeter/SKILL.md]\nStep 1: greet\n[/File: skill:greeter/SKILL.md]"
    assert opened.outcome is None and world.documents.requests == []


async def test_an_email_ref_is_refused(world):
    with pytest.raises(FileNotFoundError, match="email tool"):
        await world.service.open("email:msg-1/att-1", "ann", "ann", deadline_at=DEADLINE)
    assert world.documents.requests == []


# -- sizes on metadata, before any download ---------------------------------------------------------------------

async def test_a_drive_folder_gives_the_folder_line_and_no_download(world):
    folder = world.drive.plant(world.conn.handle, "root", "Documents", folder=True)
    opened = await world.service.open(_ref(folder), "ann", "ann", deadline_at=DEADLINE)
    assert "is a folder, not a file. List the folder to see what is inside.]" in opened.note
    assert world.drive.byte_downloads == 0 and world.drive.staged == [] and world.documents.requests == []


async def test_a_drive_file_over_the_cap_gives_the_size_line_before_download(world):
    ref = _plant(world, "scan_0012.pdf", b"x" * (MAX_FILE_BYTES + 1), PDF)
    opened = await world.service.open(ref, "ann", "ann", deadline_at=DEADLINE)
    assert opened.note.startswith("[System:") and "files above 5.0MB" in opened.note
    assert world.drive.byte_downloads == 0 and world.documents.requests == []


async def test_the_consumers_declared_pdf_cap_lets_a_larger_pdf_download(world):
    ref = _plant(world, "scan_0012.pdf", b"x" * (MAX_FILE_BYTES + 1), PDF)
    consumer = (NativeFormat(PDF, 20 * MB, 100),)
    await world.service.open(ref, "ann", "ann", consumer=consumer, deadline_at=DEADLINE)
    assert world.drive.byte_downloads == 1 and len(world.documents.requests) == 1


async def test_a_drive_pdf_over_the_declared_pdf_cap_is_refused_before_download(world):
    ref = _plant(world, "scan_0012.pdf", b"x" * (20 * MB + 1), PDF)
    consumer = (NativeFormat(PDF, 20 * MB, 100),)
    opened = await world.service.open(ref, "ann", "ann", consumer=consumer, deadline_at=DEADLINE)
    assert "[System:" in opened.note and "files above 20.0MB" in opened.note
    assert world.drive.byte_downloads == 0


async def test_a_drive_text_over_the_export_cap_gives_the_export_note(world):
    ref = _plant(world, "log.txt", b"y" * (MAX_TEXT_EXPORT_BYTES + 1), "text/plain")
    opened = await world.service.open(ref, "ann", "ann", deadline_at=DEADLINE)
    assert opened.note.startswith("[System:") and "files above 10.0MB" in opened.note
    assert world.documents.requests == []


async def test_a_workspace_text_over_512_kib_gives_its_note_before_the_read(world):
    world.workspace.seed("ann", "scratch/big.md", "z" * (OPEN_FILE_MAX_TEXT_BYTES + 1))
    opened = await world.service.open("workspace:scratch/big.md", "ann", "ann", deadline_at=DEADLINE)
    assert "open_file reads a workspace text file only up to 512KB" in opened.note
    assert "command view, path scratch/big.md" in opened.note and world.documents.requests == []


# -- staging and native ----------------------------------------------------------------------------------------

async def test_the_staging_directory_is_gone_after_a_drive_text_read(world):
    ref = _plant(world, "notes.txt", b"hello", "text/plain")
    await world.service.open(ref, "ann", "ann", deadline_at=DEADLINE)
    assert world.documents.requests[0].content == b"hello"
    (staged,) = world.drive.staged
    assert not os.path.exists(os.path.dirname(staged))


async def test_the_staging_directory_is_gone_when_the_read_raises(world):
    world.documents.raises = RuntimeError("boom")
    ref = _plant(world, "notes.txt", b"hello", "text/plain")
    with pytest.raises(RuntimeError):
        await world.service.open(ref, "ann", "ann", deadline_at=DEADLINE)
    assert not os.path.exists(os.path.dirname(world.drive.staged[0]))


async def test_a_native_outcome_leaves_a_readable_file_outside_the_staging_directory(world):
    world.documents.outcome = ReadOutcome(status=ReadStatus.NATIVE, kind="pdf", native_mime=PDF)
    ref = _plant(world, "notes.pdf", b"%PDF-native", PDF)
    opened = await world.service.open(ref, "ann", "ann", consumer=(NativeFormat(PDF, 5 * MB, 10),),
                                      deadline_at=DEADLINE)
    try:
        assert opened.native_mime == PDF and opened.native_path.endswith(".pdf")
        with open(opened.native_path, "rb") as f:
            assert f.read() == b"%PDF-native"
    finally:
        os.remove(opened.native_path)


# -- failures: the note as today, byte for byte, and a log line ------------------------------------------------

@pytest.mark.parametrize("status,detail,expected,level", [
    (ReadStatus.NO_TEXT, "", conversion_alert("invoice_painting_es.pdf", PDF), logging.WARNING),
    (ReadStatus.UNSUPPORTED, "", conversion_alert("invoice_painting_es.pdf", PDF), logging.WARNING),
    (ReadStatus.ENCRYPTED, "", conversion_alert("invoice_painting_es.pdf", PDF), logging.WARNING),
    (ReadStatus.ERROR, "crash", conversion_alert("invoice_painting_es.pdf", PDF), logging.ERROR),
    (ReadStatus.MEMORY, "oom", conversion_alert("invoice_painting_es.pdf", PDF), logging.ERROR),
    (ReadStatus.TIMEOUT, "", conversion_alert("invoice_painting_es.pdf", PDF), logging.ERROR),
    (ReadStatus.TIMEOUT, "deadline", deadline_note("invoice_painting_es.pdf"), logging.ERROR),
    (ReadStatus.MODEL_UNAVAILABLE, "", model_unavailable_note("invoice_painting_es.pdf"), logging.ERROR),
    (ReadStatus.PARSER_UNAVAILABLE, "busy", parser_unavailable_note("invoice_painting_es.pdf"), logging.ERROR),
    (ReadStatus.TOO_LARGE, "", size_alert("invoice_painting_es.pdf", 6, MAX_FILE_BYTES), logging.WARNING),
])
async def test_a_failed_read_gives_the_note_and_a_log_line(world, caplog, status, detail, expected, level):
    world.documents.outcome = ReadOutcome(status=status, kind="pdf", detail=detail)
    with caplog.at_level(logging.WARNING):
        opened = await world.service.open("invoice_painting_es.pdf", "ann", "ann", deadline_at=DEADLINE)
    assert opened.note == expected and not opened.body
    lines = [r for r in caplog.records if "[FileOpen]" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == level
    assert status.value in lines[0].getMessage() and "invoice_painting_es.pdf" in lines[0].getMessage()


async def test_an_audio_file_left_unsupported_gives_the_audio_note(world):
    world.storage.files["voice_note_es.mp3"] = b"ID3"
    world.documents.outcome = ReadOutcome(status=ReadStatus.UNSUPPORTED, kind="audio")
    opened = await world.service.open("voice_note_es.mp3", "ann", "ann", deadline_at=DEADLINE)
    assert opened.note == audio_unavailable_note("voice_note_es.mp3")


async def test_a_read_that_raises_is_logged_and_propagates(world, caplog):
    world.documents.raises = RuntimeError("boom")
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        await world.service.open("gym_pass_uk.md", "ann", "ann", deadline_at=DEADLINE)
    assert any(r.levelno == logging.ERROR and "[FileOpen]" in r.getMessage() for r in caplog.records)


async def test_without_a_document_service_the_converter_note_and_an_error_log(world, caplog):
    service = FileConversionService(storage=world.storage, access=allow_access())
    with caplog.at_level(logging.ERROR):
        opened = await service.open("gym_pass_uk.md", "ann", "ann", deadline_at=DEADLINE)
    assert opened.note == parser_unavailable_note("gym_pass_uk.md")
    assert any(r.levelno == logging.ERROR and "[FileOpen]" in r.getMessage() for r in caplog.records)


async def test_a_too_large_drive_file_logs_a_warning(world, caplog):
    ref = _plant(world, "scan_0012.pdf", b"x" * (MAX_FILE_BYTES + 1), PDF)
    with caplog.at_level(logging.WARNING):
        await world.service.open(ref, "ann", "ann", deadline_at=DEADLINE)
    assert any(r.levelno == logging.WARNING and "too_large" in r.getMessage() for r in caplog.records)
