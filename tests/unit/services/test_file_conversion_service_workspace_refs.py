"""FileConversionService reads `workspace:` refs through WorkspaceStorePort (AGENT_WORKSPACE_RFC §12): text verbatim in
[File] markers (no mime guessing), binary through conversion, raw bytes for open_file vision / edit_image /
save_file_to_drive / draft_skill from_file. Any folder; only the requesting user's objects."""
from unittest.mock import AsyncMock, patch

import pytest

from src.domain.workspace import OPEN_FILE_MAX_TEXT_BYTES
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


# --- open_file's text limit (OPEN_FILE_MAX_TEXT_BYTES) ---


def _text_of(size: int) -> str:
    return "x" * size


async def test_text_exactly_at_the_limit_is_returned_in_full():
    store = InMemoryWorkspaceStore()
    body = _text_of(OPEN_FILE_MAX_TEXT_BYTES)
    store.seed(USER, "projects/a.md", body)
    text = await _service(store).resolve_content("workspace:projects/a.md", USER)
    assert text == f"[File: workspace:projects/a.md]\n{body}\n[/File: workspace:projects/a.md]"


async def test_text_one_byte_over_the_limit_returns_a_view_note():
    store = InMemoryWorkspaceStore()
    store.seed(USER, "projects/a.md", _text_of(OPEN_FILE_MAX_TEXT_BYTES + 1))
    text = await _service(store).resolve_content("workspace:projects/a.md", USER)
    assert text.startswith("[System: 'workspace:projects/a.md'")
    assert "view" in text
    assert "projects/a.md" in text
    assert "offset" in text
    assert "[File:" not in text


async def test_over_the_limit_note_does_not_carry_the_content():
    store = InMemoryWorkspaceStore()
    store.seed(USER, "projects/a.md", "SECRETMARK" + _text_of(OPEN_FILE_MAX_TEXT_BYTES))
    text = await _service(store).resolve_content("workspace:projects/a.md", USER)
    assert "SECRETMARK" not in text
    assert len(text) < 1000


async def test_text_between_half_the_limit_and_the_limit_is_read_normally():
    store = InMemoryWorkspaceStore()
    body = _text_of(OPEN_FILE_MAX_TEXT_BYTES // 2 + 1024)
    store.seed(USER, "projects/b.md", body)
    text = await _service(store).resolve_content("workspace:projects/b.md", USER)
    assert text == f"[File: workspace:projects/b.md]\n{body}\n[/File: workspace:projects/b.md]"


async def test_raw_bytes_path_is_not_limited():
    store = InMemoryWorkspaceStore()
    body = _text_of(OPEN_FILE_MAX_TEXT_BYTES + 100 * 1024)
    store.seed(USER, "projects/c.md", body)
    data = await _service(store)._download_by_ref("workspace:projects/c.md", USER)
    assert data == body.encode("utf-8")
