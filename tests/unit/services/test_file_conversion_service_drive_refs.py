"""drive: refs in FileConversionService (docs/10_rfcs/USER_DRIVE_RFC.md §4.6). `open` replaced `resolve_content`
(EXTERNAL_FILES_RFC §4.2): the read goes through the attached document service."""
import time
from unittest.mock import AsyncMock

import pytest

from src.domain.user_drive import (
    DriveFileTooLargeError,
    DriveItem,
    DrivePathError,
)
from src.domain.document_limits import MAX_DRIVE_DOWNLOAD_BYTES
from src.ports.file_storage_port import FileStoragePort
from src.ports.user_drive_port import UserDrivePort
from src.domain.document_read import ReadRequest
from src.ports.document_read_port import DocumentReadPort
from src.services.document_service import DocumentService
from src.services.file_conversion_service import FileConversionService
from src.utils.file_conversion import MAX_FILE_BYTES
from tests.support.drive_access_stub import allow_access
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import connection_service, seed
from tests.unit.services.test_document_service_read import FakeExtractor, FakeSplitter


class RecordingDocuments(DocumentReadPort):
    """The real document service (text reads in process), recording each request."""
    def __init__(self) -> None:
        self.requests = []
        self._inner = DocumentService(FakeExtractor("parser"), FakeExtractor("reader"), FakeSplitter())

    async def read(self, request: ReadRequest):
        self.requests.append(request)
        return await self._inner.read(request)


def _serve(drive, data: bytes) -> None:
    """The drive's streamed download writes `data` (up to the cap) where it is asked to."""
    async def download_to_file(handle, item_id, dest_path, max_bytes):
        with open(dest_path, "wb") as fh:
            fh.write(data[:max_bytes])
        return len(data)
    drive.download_to_file.side_effect = download_to_file


async def _open(svc, ref):
    return await svc.open(ref, "u1", "u1", deadline_at=time.time() + 60)


def _item(size, name="notes.md", mime="text/markdown", folder=False):
    return DriveItem(item_id="i1", name=name, path=f"Docs/{name}", is_folder=folder,
                     size_bytes=size, mime_type=mime)


@pytest.fixture
def drive():
    return AsyncMock(spec=UserDrivePort)


@pytest.fixture
async def connection():
    repo = FakeDriveConnectionRepository()
    conn = await seed(repo, "u1", "conn-1", tag="a1")
    return conn, connection_service(repo)


@pytest.fixture
def documents():
    return RecordingDocuments()


@pytest.fixture
def svc(drive, connection, documents):
    service = FileConversionService(storage=AsyncMock(spec=FileStoragePort), drive=drive, connections=connection[1],
                                    access=allow_access())
    service.attach_documents(documents)
    return service


class TestDriveRefs:
    async def test_text_uses_metadata_mime_and_path(self, svc, drive, documents):
        drive.get_item.return_value = _item(20)
        _serve(drive, b"# hello")
        out = (await _open(svc, "drive:a1:i1")).body
        assert "# hello" in out and out.startswith("[File: Docs/notes.md]")
        (request,) = documents.requests
        assert (request.name, request.declared_mime) == ("Docs/notes.md", "text/markdown")

    async def test_text_over_cap_refused_before_download(self, svc, drive):
        drive.get_item.return_value = _item(MAX_FILE_BYTES + 1)
        _serve(drive, b"x" * (MAX_FILE_BYTES + 1))
        assert (await _open(svc, "drive:a1:i1")).note.startswith("[Tool note:")
        drive.download.assert_not_called()

    async def test_folder_is_not_readable(self, svc, drive):
        drive.get_item.return_value = _item(0, name="Docs", mime="", folder=True)
        out = (await _open(svc, "drive:a1:i1")).note
        assert "folder" in out and "List the folder to see what is inside." in out
        drive.download.assert_not_called()
        drive.download_to_file.assert_not_called()

    async def test_bytes_over_cap_raise_before_download(self, svc, drive):
        drive.get_item.return_value = _item(MAX_DRIVE_DOWNLOAD_BYTES + 1, name="v.mp4", mime="video/mp4")
        with pytest.raises(DriveFileTooLargeError):
            await svc.resolve_bytes("drive:a1:i1", "u1")
        drive.download.assert_not_called()

    async def test_bytes_at_cap_are_downloaded(self, svc, drive):
        drive.get_item.return_value = _item(MAX_DRIVE_DOWNLOAD_BYTES, name="v.mp4", mime="video/mp4")
        drive.download.return_value = b"data"
        assert await svc.resolve_bytes("drive:a1:i1", "u1") == b"data"

    async def test_folder_bytes_refused_before_download(self, svc, drive):
        drive.get_item.return_value = _item(0, name="Docs", mime="", folder=True)
        with pytest.raises(DrivePathError):
            await svc.resolve_bytes("drive:a1:i1", "u1")
        drive.download.assert_not_called()

    async def test_label_punctuation_ref_resolves(self, svc, drive, connection):
        drive.get_item.return_value = _item(3)
        drive.download.return_value = b"abc"
        await svc.resolve_bytes("ref=drive:a1:i1]", "u1")
        drive.get_item.assert_awaited_once_with(connection[0].handle, "i1")

    async def test_no_drive_wired_is_not_found(self):
        with pytest.raises(FileNotFoundError):
            await FileConversionService(storage=AsyncMock(spec=FileStoragePort), access=allow_access()).get_drive_item("drive:i1", "u1")

    async def test_no_provider_name_in_alerts(self, svc, drive):
        drive.get_item.return_value = _item(MAX_FILE_BYTES + 1)
        _serve(drive, b"x" * (MAX_FILE_BYTES + 1))
        out = (await _open(svc, "drive:a1:i1")).note
        assert out.startswith("[Tool note:")
        for word in ("onedrive", "microsoft", "google"):
            assert word not in out.lower()

    async def test_non_drive_refs_unchanged(self, svc, drive):
        svc._storage.download.return_value = b"x"
        await svc.resolve_bytes("report.docx", "u1")
        drive.get_item.assert_not_called()

    async def test_other_store_ref_containing_drive_colon_keeps_its_store(self, drive):
        """A delivered key / skill ref is decided by its prefix: `drive:` inside the
        filename must not pull it onto the drive (keys embed the filename unsanitised)."""
        media = AsyncMock()
        media.fetch.return_value = b"%PDF"
        skills = AsyncMock()
        skills.read.return_value = "body"
        svc = FileConversionService(storage=AsyncMock(spec=FileStoragePort), media_storage=media,
                                    skill_files=skills, drive=drive, access=allow_access())
        key = "docs/u1/0f8e3b2a-1c2d-4e5f-8a9b-0c1d2e3f4a5b-drive:plan.pdf"
        assert await svc.resolve_bytes(key, "u1") == b"%PDF"
        media.fetch.assert_awaited_once_with(key)
        assert "body" in (await _open(svc, "skill:notes/drive:x.md")).body
        drive.get_item.assert_not_called()
