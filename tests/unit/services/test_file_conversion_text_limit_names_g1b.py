"""G1b: a refusal names the limit that applied: 10 MB for a text export, 5 MB otherwise. Drive files open through
`open` (it replaced `resolve_content`, EXTERNAL_FILES_RFC §4.2) over the real document service."""
import time
from unittest.mock import AsyncMock

import pytest

from src.domain.drive_feed_scope import FeedScope
from src.ports.file_storage_port import FileStoragePort
from src.services.document_service import DocumentService
from src.services.drive_connection_service import DriveConnectionService
from src.services.file_conversion_service import FileConversionService
from src.utils.file_conversion import convert_file_to_text
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


async def _read(w, name, data, mime):
    item = w.drive.plant(w.conn.handle, "root", name, data, mime=mime)
    opened = await w.service.open(f"drive:b2:{item}", "usr-1", "acc-1", deadline_at=time.time() + 60)
    return opened.body or opened.note


async def test_a_text_export_refused_above_the_export_cap_names_ten_megabytes(world):
    out = await _read(world, "big.md", b"x" * (11 * MB), "text/markdown")
    assert "files above 10.0MB" in out and "5.0MB cannot" not in out


async def test_a_pdf_refused_above_the_file_cap_names_five_megabytes(world):
    out = await _read(world, "brief.pdf", b"%PDF" + b"0" * (6 * MB), "application/pdf")
    assert "files above 5.0MB" in out


async def test_the_converter_names_ten_megabytes_for_text_over_the_export_cap(tmp_path):
    path = tmp_path / "big.md"
    path.write_bytes(b"x" * (11 * MB))
    out = await convert_file_to_text(str(path), "big.md", "text/markdown")
    assert "10 MB limit" in out


async def test_the_converter_names_five_megabytes_for_text_left_over_five(tmp_path):
    path = tmp_path / "log.txt"
    path.write_bytes(b"real words\n" * (6 * MB // 11 + 1))
    out = await convert_file_to_text(str(path), "log.txt", "text/plain")
    assert "5 MB limit" in out
