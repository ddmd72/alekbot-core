"""Live bug 2026-10-10: a re-sent identical chat file was saved to the drive as a second copy.

The chat store (GcsFileStorageAdapter) renames the second upload `a (1).md`; `save_file_to_drive`
used to name the drive file from that storage key, so UserDriveService.save's duplicate check
(same name -> same bytes -> existing) never ran. See USER_DRIVE_RFC §4.7. The copy is DriveOperations.copy since the
cut (DRIVE_SPECIALIST_RFC §5.5; the specialist's `copy_file_to_drive`).
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.user_drive import DriveItem
from src.infrastructure.drive_operations import DriveOperations
from src.ports.user_drive_port import UserDrivePort
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_service_connections import seeded_connections

ROOT = DriveItem(item_id="root", name="Alek-bot", path="", is_folder=True)
FOLDER = DriveItem(item_id="notes", name="Notes", path="Notes", is_folder=True)


class _MemoryDrive:
    """Just enough of a provider: one folder, byte store, provider-style `<stem> 1<ext>` clash suffix."""

    def __init__(self):
        self.files = {}  # name -> bytes
        self.uploads = []

    def port(self):
        port = AsyncMock(spec=UserDrivePort)
        port.get_root.return_value = ROOT
        port.list_children.side_effect = self._list
        port.download.side_effect = self._download
        port.upload.side_effect = self._upload
        return port

    async def _list(self, handle, folder_id):
        if folder_id == "root":
            return [FOLDER]
        return [DriveItem(item_id=f"id:{n}", name=n, path=f"Notes/{n}", is_folder=False, size_bytes=len(b))
                for n, b in self.files.items()]

    async def _download(self, handle, item_id):
        return self.files[item_id.removeprefix("id:")]

    async def _upload(self, handle, parent_id, filename, data, content_type):
        self.uploads.append(filename)
        name, n = filename, 0
        stem, dot, ext = filename.rpartition(".")
        while name in self.files:
            n += 1
            name = f"{stem} {n}.{ext}"
        self.files[name] = data
        return DriveItem(item_id=f"id:{name}", name=name, path=f"Notes/{name}", is_folder=False,
                         size_bytes=len(data))


def _copy(ops, file_ref):
    return ops.copy({"user_id": "u1", "account_id": "a1"}, "u1", file_ref, folder="Notes", wait_s=100.0)


@pytest.fixture
def setup():
    memory = _MemoryDrive()
    chat_files = {}
    conversion = AsyncMock(spec=FileConversionService)

    async def resolve_bytes(ref, user_id):
        return chat_files[ref]

    conversion.resolve_bytes.side_effect = resolve_bytes
    agent = DriveOperations(conversion_service=conversion, notification=AsyncMock(),
                            drive_service=UserDriveService(memory.port(), seeded_connections("u1")), localization=MagicMock(),
                            language_service=AsyncMock())
    return agent, memory, chat_files


class TestResentChatFile:
    async def test_identical_resend_is_already_there(self, setup):
        agent, memory, chat = setup
        chat["adr.md"] = b"same bytes"
        first = await _copy(agent, "adr.md")
        assert first.success
        chat["adr (1).md"] = b"same bytes"  # the chat store's dedup name for the second upload

        second = await _copy(agent, "adr (1).md")

        assert second.success
        assert "Already on the drive" in second.text
        assert memory.uploads == ["adr.md"]
        assert list(memory.files) == ["adr.md"]

    async def test_different_bytes_get_provider_suffix_not_chat_suffix(self, setup):
        agent, memory, chat = setup
        chat["adr.md"] = b"version one"
        await _copy(agent, "adr.md")
        chat["adr (1).md"] = b"version two, longer"

        second = await _copy(agent, "adr (1).md")

        assert second.success
        assert memory.uploads == ["adr.md", "adr.md"]  # asked for the original name both times
        assert sorted(memory.files) == ["adr 1.md", "adr.md"]
        assert "(1)" not in "".join(memory.files)
