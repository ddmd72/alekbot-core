"""A stored file over the size a read allows: `view` answers the usual refusal note, `open_file` its too-large note
(by kind, from the stored size, nothing downloaded), and a folder-wide grep skips the file and says so."""
import time
from unittest.mock import AsyncMock

import pytest

from src.domain.document_limits import MAX_FILE_BYTES as DOC_CAP
from src.domain.workspace import MAX_FILE_BYTES, OPEN_FILE_MAX_TEXT_BYTES, WorkspaceCall
from src.ports.file_storage_port import FileStoragePort
from src.services.file_conversion_service import FileConversionService
from src.services.workspace_service import WorkspaceService
from tests.support.drive_access_stub import allow_access
from tests.unit.services.test_file_conversion_open import RecordingDocuments
from tests.unit.services.workspace_fakes import InMemoryWorkspaceStore

USER = "u1"
P = "projects/a.md"
BIG = "projects/big.md"


def _call():
    return WorkspaceCall(user_id=USER, origin="chat")


def _store():
    store = InMemoryWorkspaceStore()
    store.seed(USER, P, "needle here\n")
    store.seed(USER, BIG, "needle too\n", size=MAX_FILE_BYTES + 1)
    store.seed(USER, "binary_files/lease.pdf", b"%PDF", size=DOC_CAP + 1)
    return store


async def test_view_answers_the_refusal_note():
    result = await WorkspaceService(_store()).run(_call(), "view", {"path": BIG})
    assert result.failed and result.text == f"Tool note: {BIG}: file over 10.0 MB."


async def test_folder_grep_skips_the_oversized_file_and_names_it():
    result = await WorkspaceService(_store()).run(_call(), "grep", {"pattern": "needle", "path": "projects/"})
    assert not result.failed and P in result.text and "needle too" not in result.text
    assert f"(skipped, over the 10.0 MB file cap: {BIG})" in result.text


async def test_grep_of_the_oversized_file_itself_is_refused():
    result = await WorkspaceService(_store()).run(_call(), "grep", {"pattern": "needle", "path": BIG})
    assert result.failed and "file over 10.0 MB" in result.text


def _service(store, documents):
    return FileConversionService(storage=AsyncMock(spec=FileStoragePort), workspace=store, access=allow_access(),
                                 documents=documents)


async def _open(service, ref):
    return await service.open(ref, USER, USER, deadline_at=time.time() + 60)


async def test_open_file_of_a_big_text_file_points_to_view_windows():
    store = InMemoryWorkspaceStore()
    store.seed(USER, P, "x", size=OPEN_FILE_MAX_TEXT_BYTES + 1)
    documents = RecordingDocuments()
    opened = await _open(_service(store, documents), f"workspace:{P}")
    assert "open_file reads a workspace text file only up to 512KB" in opened.note and f"path {P}" in opened.note
    assert documents.requests == []


async def test_open_file_of_a_big_binary_file_uses_the_size_line_with_the_document_cap():
    documents = RecordingDocuments()
    opened = await _open(_service(_store(), documents), "workspace:binary_files/lease.pdf")
    assert "files above 5.0MB cannot be read as text yet" in opened.note
    assert documents.requests == []


@pytest.mark.parametrize("ref", [f"workspace:{P}"])
async def test_a_file_within_its_cap_still_opens(ref):
    opened = await _open(_service(_store(), RecordingDocuments()), ref)
    assert "needle here" in opened.body
