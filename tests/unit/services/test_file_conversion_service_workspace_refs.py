"""FileConversionService reads `workspace:` refs through WorkspaceStorePort (AGENT_WORKSPACE_RFC §12): text verbatim in
[File] markers (no mime guessing), binary through conversion, raw bytes for open_file vision / edit_image /
save_file_to_drive / draft_skill from_file. Any folder; only the requesting user's objects."""
from unittest.mock import AsyncMock, patch

import pytest

from src.ports.file_storage_port import FileStoragePort
from src.services.file_conversion_service import FileConversionService, _is_drive_ref
from tests.unit.services.workspace_fakes import InMemoryWorkspaceStore

USER = "u1"


def _service(store=None, storage=None):
    return FileConversionService(storage=storage or AsyncMock(spec=FileStoragePort), workspace=store)


async def test_text_ref_is_returned_verbatim_in_markers():
    store = InMemoryWorkspaceStore()
    store.seed(USER, "scratch/sources.md", "# Sources\n- a")
    text = await _service(store).resolve_content("workspace:scratch/sources.md", USER)
    assert text == "[File: workspace:scratch/sources.md]\n# Sources\n- a\n[/File: workspace:scratch/sources.md]"
    assert store.calls == [("read", "scratch/sources.md")]


async def test_binary_ref_goes_through_conversion():
    store = InMemoryWorkspaceStore()
    store.seed(USER, "files/lease.pdf", b"%PDF-1.4")
    with patch("src.services.file_conversion_service.convert_file_to_text",
               AsyncMock(return_value="LEASE TEXT")) as convert:
        text = await _service(store).resolve_content("workspace:files/lease.pdf", USER)
    assert text == "LEASE TEXT"
    assert convert.await_args.args[1:3] == ("workspace:files/lease.pdf", "application/pdf")


async def test_resolve_bytes():
    store = InMemoryWorkspaceStore()
    store.seed(USER, "files/photo.jpg", b"\xff\xd8jpeg")
    assert await _service(store).resolve_bytes("workspace:files/photo.jpg", USER) == b"\xff\xd8jpeg"


async def test_only_the_requesting_users_objects():
    store = InMemoryWorkspaceStore()
    store.seed("u2", "scratch/a.md", "theirs")
    with pytest.raises(FileNotFoundError):
        await _service(store).resolve_bytes("workspace:scratch/a.md", USER)


async def test_unavailable_without_the_store():
    with pytest.raises(FileNotFoundError):
        await _service(None).resolve_content("workspace:scratch/a.md", USER)


async def test_invalid_path_is_not_found():
    with pytest.raises(FileNotFoundError):
        await _service(InMemoryWorkspaceStore()).resolve_bytes("workspace:projects/../x.md", USER)


def test_never_a_drive_ref():
    assert not _is_drive_ref("workspace:files/a.pdf")


async def test_uploads_still_go_to_file_storage():
    storage = AsyncMock(spec=FileStoragePort)
    storage.download.return_value = b"x"
    assert await _service(InMemoryWorkspaceStore(), storage).resolve_bytes("report.pdf", USER) == b"x"
    storage.download.assert_awaited_once_with("report.pdf", USER)
