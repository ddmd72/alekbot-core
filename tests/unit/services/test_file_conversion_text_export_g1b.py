"""G1b in chat: a text drive file is read up to the export cap, and the 5 MB cap applies to the text left after its
inline images are dropped. Real FileConversionService over the fake drive (account-wide mode), reading through the real
document service (`open` replaced `resolve_content`, EXTERNAL_FILES_RFC §4.2; text is read in process, A3)."""
import time
from unittest.mock import AsyncMock

import pytest

from src.domain.drive_feed_scope import FeedScope
from src.domain.document_limits import MAX_TEXT_EXPORT_BYTES
from src.ports.file_storage_port import FileStoragePort
from src.services.document_service import DocumentService
from src.services.drive_connection_service import DriveConnectionService
from src.services.file_conversion_service import FileConversionService
from src.utils.file_conversion import MAX_FILE_BYTES
from tests.support.drive_access_stub import allow_access
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import seed
from tests.support.fake_drive_adapter import FakeDriveAdapter
from tests.unit.services.test_document_service_read import FakeExtractor, FakeSplitter

MB = 1024 * 1024


@pytest.fixture
async def world():
    class World:
        pass
    w = World()
    repo = FakeDriveConnectionRepository()
    w.conn = await seed(repo, connection_id="conn-2", tag="b2", kind="fake_b")
    w.drive = FakeDriveAdapter("fake_b", feed_scope=FeedScope.ACCOUNT)
    w.service = FileConversionService(
        storage=AsyncMock(spec=FileStoragePort), drive=w.drive,
        connections=DriveConnectionService(repo, None), access=allow_access())
    w.service.attach_documents(DocumentService(FakeExtractor("parser"), FakeExtractor("reader"), FakeSplitter()))
    return w


def _ref(item_id):
    return f"drive:b2:{item_id}"


async def _read(w, name, data, mime):
    item = w.drive.plant(w.conn.handle, "root", name, data, mime=mime)
    opened = await w.service.open(_ref(item), "usr-1", "acc-1", deadline_at=time.time() + 60)
    return opened.body or opened.note


async def test_a_six_megabyte_markdown_of_images_opens_with_its_text(world):
    data = (b"# Garden plan 2026/27\nRoses in March.\n![](data:image/png;base64," + b"A" * (6 * MB) + b")\nDone.\n")
    out = await _read(world, "Garden plan 2026/27", data, "text/markdown")
    assert "Roses in March." in out and "Done." in out
    assert "base64" not in out and "[System:" not in out


async def test_a_six_megabyte_plain_text_gets_the_size_line(world):
    out = await _read(world, "log.txt", (b"real words here\n" * (6 * MB // 16 + 1)), "text/plain")
    assert out.startswith("[System:") and "5 MB" in out


async def test_an_eleven_megabyte_markdown_gets_the_size_line_and_nothing_beyond_the_cap_is_written(world, monkeypatch):
    written = []
    real = world.drive.download_to_file

    async def spy(handle, item_id, dest_path, max_bytes):
        try:
            return await real(handle, item_id, dest_path, max_bytes)
        finally:
            with open(dest_path, "rb") as fh:
                written.append(len(fh.read()))

    monkeypatch.setattr(world.drive, "download_to_file", spy)
    out = await _read(world, "big.md", b"x" * (11 * MB), "text/markdown")
    assert out.startswith("[System:")
    assert written == [MAX_TEXT_EXPORT_BYTES]


async def test_a_six_megabyte_pdf_is_refused_before_any_download(world):
    out = await _read(world, "brief.pdf", b"%PDF" + b"0" * (MAX_FILE_BYTES + MB), "application/pdf")
    assert out.startswith("[System:")
    assert not [c for c in world.drive.calls if c[0].startswith("download")]
