"""The indexer's PDF download cap in the tick, and the chat/open cap that stays below it
(decisions/drive_indexer_pdf_download_cap.md)."""
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from src.domain.drive_index import Reason, RecordState
from src.domain.drive_index_params import MAX_DOWNLOAD_BYTES, MAX_PDF_DOWNLOAD_BYTES
from src.domain.user_drive import MAX_DRIVE_DOWNLOAD_BYTES, DriveFileTooLargeError, DriveItem
from src.ports.file_storage_port import FileStoragePort
from src.ports.user_drive_port import UserDrivePort
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.services.drive_connection_service import DriveConnectionService
from src.services.file_conversion_service import FileConversionService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connection_service_fake import connection

from .drive_content_world import ContentWorld, admitted, prose  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("admitted")
BETWEEN = (MAX_DRIVE_DOWNLOAD_BYTES + MAX_PDF_DOWNLOAD_BYTES) // 2


@pytest.fixture
def w():
    world = ContentWorld()
    world.caps = []
    download = world.drive.download_to_file

    async def recording(user_id, item_id, dest_path, max_bytes):
        world.caps.append((item_id, max_bytes))
        return await download(user_id, item_id, dest_path, max_bytes)

    world.drive.download_to_file = recording
    return world


def _claims(w, item_id, size):
    """The listing reports `size` bytes (the content stays small: only the metadata decides admission)."""
    w.drive.items[item_id] = replace(w.drive.items[item_id], size_bytes=size)


async def test_a_pdf_between_the_caps_is_downloaded_under_the_pdf_cap_and_indexed(w):
    doc = w.add_doc("large.pdf", prose(60))
    _claims(w, doc, BETWEEN)
    await w.settle()
    assert w.caps == [(doc, MAX_PDF_DOWNLOAD_BYTES)]
    assert w.rec(doc).state is RecordState.INDEXED and [r.kind for r in w.parser.requests] == ["pdf"]


async def test_a_docx_of_the_same_size_is_too_large_and_never_downloaded(w):
    doc = w.add_doc("large.docx", prose(60))
    _claims(w, doc, BETWEEN)
    await w.settle()
    assert (w.rec(doc).state, w.rec(doc).reason) == (RecordState.METADATA_ONLY, Reason.TOO_LARGE)
    assert w.caps == [] and w.parser.requests == []


async def test_a_docx_under_the_chat_cap_is_downloaded_under_it(w):
    doc = w.add_doc("small.docx", prose(60))
    await w.settle()
    assert w.caps == [(doc, MAX_DOWNLOAD_BYTES)]


async def test_a_pdf_over_the_pdf_cap_is_too_large_and_never_downloaded(w):
    doc = w.add_doc("huge.pdf", prose(60))
    _claims(w, doc, MAX_PDF_DOWNLOAD_BYTES + 1)
    await w.settle()
    assert (w.rec(doc).state, w.rec(doc).reason) == (RecordState.METADATA_ONLY, Reason.TOO_LARGE)
    assert w.caps == []


async def test_chat_open_refuses_the_pdf_the_indexer_admits():
    drive = AsyncMock(spec=UserDrivePort)
    drive.get_item.return_value = DriveItem(item_id="i1", name="large.pdf", path="Docs/large.pdf", is_folder=False,
                                            size_bytes=BETWEEN, mime_type="application/pdf")
    repo = FakeDriveConnectionRepository()
    await repo.create(connection("u1", "conn-1", ref_tag="a1"))   # the ref's tag names this connection (§9)
    connections = DriveConnectionService(repo, AsyncMock(spec=OAuthCredentialsPort))
    svc = FileConversionService(storage=AsyncMock(spec=FileStoragePort), drive=drive, connections=connections)
    with pytest.raises(DriveFileTooLargeError):
        await svc.resolve_bytes("drive:a1:i1", "u1")
    drive.download.assert_not_called()
