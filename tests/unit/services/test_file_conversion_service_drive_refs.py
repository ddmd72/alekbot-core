"""drive: refs in FileConversionService (docs/10_rfcs/USER_DRIVE_RFC.md §4.6)."""
from unittest.mock import AsyncMock, patch

import pytest

from src.domain.user_drive import (
    MAX_DRIVE_DOWNLOAD_BYTES,
    DriveFileTooLargeError,
    DriveItem,
    DrivePathError,
)
from src.ports.file_storage_port import FileStoragePort
from src.ports.user_drive_port import UserDrivePort
from src.services.file_conversion_service import FileConversionService
from src.utils.file_conversion import MAX_FILE_BYTES


def _item(size, name="notes.md", mime="text/markdown", folder=False):
    return DriveItem(item_id="i1", name=name, path=f"Docs/{name}", is_folder=folder,
                     size_bytes=size, mime_type=mime)


@pytest.fixture
def drive():
    return AsyncMock(spec=UserDrivePort)


@pytest.fixture
def svc(drive):
    return FileConversionService(storage=AsyncMock(spec=FileStoragePort), drive=drive)


class TestDriveRefs:
    async def test_text_uses_metadata_mime_and_path(self, svc, drive):
        drive.get_item.return_value = _item(20)
        drive.download.return_value = b"# hello"
        conv = AsyncMock(return_value="[File: Docs/notes.md]\n# hello\n[/File: Docs/notes.md]")
        with patch("src.services.file_conversion_service.convert_file_to_text", new=conv):
            out = await svc.resolve_content("drive:i1", "u1")
        assert "# hello" in out
        assert conv.call_args.args[1:3] == ("Docs/notes.md", "text/markdown")

    async def test_text_over_cap_refused_before_download(self, svc, drive):
        drive.get_item.return_value = _item(MAX_FILE_BYTES + 1)
        assert (await svc.resolve_content("drive:i1", "u1")).startswith("[System:")
        drive.download.assert_not_called()

    async def test_folder_is_not_readable(self, svc, drive):
        drive.get_item.return_value = _item(0, name="Docs", mime="", folder=True)
        out = await svc.resolve_content("drive:i1", "u1")
        assert "folder" in out and "list_files_in_drive" in out
        drive.download.assert_not_called()

    async def test_bytes_over_cap_raise_before_download(self, svc, drive):
        drive.get_item.return_value = _item(MAX_DRIVE_DOWNLOAD_BYTES + 1, name="v.mp4", mime="video/mp4")
        with pytest.raises(DriveFileTooLargeError):
            await svc.resolve_bytes("drive:i1", "u1")
        drive.download.assert_not_called()

    async def test_bytes_at_cap_are_downloaded(self, svc, drive):
        drive.get_item.return_value = _item(MAX_DRIVE_DOWNLOAD_BYTES, name="v.mp4", mime="video/mp4")
        drive.download.return_value = b"data"
        assert await svc.resolve_bytes("drive:i1", "u1") == b"data"

    async def test_folder_bytes_refused_before_download(self, svc, drive):
        drive.get_item.return_value = _item(0, name="Docs", mime="", folder=True)
        with pytest.raises(DrivePathError):
            await svc.resolve_bytes("drive:i1", "u1")
        drive.download.assert_not_called()

    async def test_label_punctuation_ref_resolves(self, svc, drive):
        drive.get_item.return_value = _item(3)
        drive.download.return_value = b"abc"
        await svc.resolve_bytes("ref=drive:i1]", "u1")
        drive.get_item.assert_awaited_once_with("u1", "i1")

    async def test_no_drive_wired_is_not_found(self):
        with pytest.raises(FileNotFoundError):
            await FileConversionService(storage=AsyncMock(spec=FileStoragePort)).get_drive_item("drive:i1", "u1")

    async def test_no_provider_name_in_alerts(self, svc, drive):
        drive.get_item.return_value = _item(MAX_FILE_BYTES + 1)
        out = await svc.resolve_content("drive:i1", "u1")
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
                                    skill_files=skills, drive=drive)
        key = "docs/u1/0f8e3b2a-1c2d-4e5f-8a9b-0c1d2e3f4a5b-drive:plan.pdf"
        assert await svc.resolve_bytes(key, "u1") == b"%PDF"
        media.fetch.assert_awaited_once_with(key)
        assert "body" in await svc.resolve_content("skill:notes/drive:x.md", "u1")
        drive.get_item.assert_not_called()
