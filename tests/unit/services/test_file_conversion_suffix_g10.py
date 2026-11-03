"""G10: chat converts a drive file by the suffix its mime implies when the name has none or disagrees.

`open` replaced `resolve_content` (EXTERNAL_FILES_RFC §4.2): the bytes are read by the document service, whose parser
runs in a child process (§4.8). Here the real service runs over a recording parser: the parser must be asked to read an
`.xlsx` file of kind xlsx; a text file is read in process and never reaches it."""
import mimetypes
import time
from unittest.mock import AsyncMock

import pytest

from src.domain.document_extraction import ExtractionStatus
from src.domain.drive_extraction import ExtractionOutcome
from src.domain.drive_feed_scope import FeedScope
from src.ports.file_storage_port import FileStoragePort
from src.services.document_service import DocumentService
from src.services.drive_connection_service import DriveConnectionService
from src.services.file_conversion_service import FileConversionService
from tests.support.drive_access_stub import allow_access
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import seed
from tests.support.fake_drive_adapter import FakeDriveAdapter
from tests.unit.adapters.drive_extraction_fixtures import make_xlsx
from tests.unit.services.test_document_service_read import FakeExtractor, FakeSplitter

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SHEET_TEXT = "Q3\nHamburg travel | 2500\nAcme licences | 1000"


@pytest.fixture
async def world(tmp_path):
    class World:
        pass
    w = World()
    repo = FakeDriveConnectionRepository()
    w.conn = await seed(repo, connection_id="conn-2", tag="b2", kind="fake_b")
    w.drive = FakeDriveAdapter("fake_b", feed_scope=FeedScope.ACCOUNT)
    w.service = FileConversionService(
        storage=AsyncMock(spec=FileStoragePort), drive=w.drive,
        connections=DriveConnectionService(repo, None), access=allow_access())
    w.parser = FakeExtractor("parser", ExtractionOutcome(status=ExtractionStatus.TEXT, text=SHEET_TEXT,
                                                         extracted_by="parser@3"))
    w.service.attach_documents(DocumentService(w.parser, FakeExtractor("reader"), FakeSplitter()))
    w.xlsx = make_xlsx(tmp_path / "q3.xlsx", [("Q3", [["Hamburg travel", 2500], ["Acme licences", 1000]])]).read_bytes()
    return w


async def _read(w, name, data, mime):
    item = w.drive.plant(w.conn.handle, "root", name, data, mime=mime)
    opened = await w.service.open(f"drive:b2:{item}", "usr-1", "acc-1", deadline_at=time.time() + 60)
    return opened.body or opened.note


def _read_as_xlsx(w):
    (request,) = w.parser.requests
    assert request.kind == "xlsx" and request.path.endswith(".xlsx") and request.name.endswith(".xlsx")


@pytest.mark.parametrize("name", ["Q3 budget", "budget.csv"])
async def test_the_mime_gives_the_suffix_when_the_name_has_none_or_disagrees(world, name):
    out = await _read(world, name, world.xlsx, XLSX)
    assert "Hamburg travel" in out and "[System:" not in out
    _read_as_xlsx(world)


async def test_a_name_that_agrees_keeps_its_suffix(world):
    out = await _read(world, "budget.xlsx", world.xlsx, XLSX)
    assert "Acme licences" in out
    _read_as_xlsx(world)
    assert world.parser.requests[0].name.endswith("budget.xlsx")


async def test_a_text_name_with_a_text_mime_takes_the_plain_text_path(world):
    out = await _read(world, "notes.md", b"Roses in March.", "text/plain")
    assert "Roses in March." in out and "[System:" not in out
    assert world.parser.requests == []


async def test_the_suffix_never_depends_on_the_system_mime_table(world, monkeypatch):
    monkeypatch.setattr(mimetypes, "_db", mimetypes.MimeTypes(filenames=()))
    out = await _read(world, "Q3 budget", world.xlsx, XLSX)
    assert "Hamburg travel" in out
    _read_as_xlsx(world)
