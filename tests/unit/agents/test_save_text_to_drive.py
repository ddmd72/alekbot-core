"""Saving a composed text as a new text file (USER_DRIVE_RFC §4.7.1): `save_file_to_drive`'s text mode until the cut,
DriveOperations.save_text (the specialist's `save_text_file`) since (DRIVE_SPECIALIST_RFC §5.5, §8.1)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.user_drive import MAX_DRIVE_TEXT_FILE_BYTES, DriveItem
from src.infrastructure.drive_operations import DriveOperations
from src.ports.user_drive_port import UserDrivePort
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_service_connections import seeded_connections

ROOT = DriveItem(item_id="root", name="Alek-bot", path="", is_folder=True)
INBOX = DriveItem(item_id="inbox", name="Inbox", path="Inbox", is_folder=True)


class _MemoryDrive:
    def __init__(self):
        self.files = {}
        self.uploads = []
        self.types = []

    def port(self):
        port = AsyncMock(spec=UserDrivePort)
        port.get_root.return_value = ROOT
        port.list_children.side_effect = self._list
        port.download.side_effect = self._download
        port.upload.side_effect = self._upload
        self.mock = port
        return port

    async def _list(self, handle, folder_id):
        if folder_id == "root":
            return [INBOX]
        return [DriveItem(item_id=f"id:{n}", name=n, path=f"Inbox/{n}", is_folder=False, size_bytes=len(b))
                for n, b in self.files.items()]

    async def _download(self, handle, item_id):
        return self.files[item_id.removeprefix("id:")]

    async def _upload(self, handle, parent_id, filename, data, content_type):
        self.uploads.append(filename)
        self.types.append(content_type)
        name, n = filename, 0
        stem, dot, ext = filename.rpartition(".")
        while name in self.files:
            n += 1
            name = f"{stem} {n}.{ext}"
        self.files[name] = data
        return DriveItem(item_id=f"id:{name}", name=name, path=f"Inbox/{name}", is_folder=False,
                         size_bytes=len(data))


CTX = {"user_id": "u1", "account_id": "a1"}


def _save(ops, text, name=None, folder=None):
    return ops.save_text(CTX, "u1", text, file_name=name, folder=folder, wait_s=100.0)


@pytest.fixture
def setup():
    memory = _MemoryDrive()
    notification = AsyncMock()
    agent = DriveOperations(
        conversion_service=AsyncMock(spec=FileConversionService),
        notification=notification, drive_service=UserDriveService(memory.port(), seeded_connections("u1")),
        localization=MagicMock(), language_service=AsyncMock(),
    )
    return agent, memory, notification


class TestTextMode:
    async def test_cyrillic_markdown_exact_bytes_default_inbox(self, setup):
        agent, memory, notification = setup
        res = await _save(agent, "# Нотатка\r\nпривіт\rкінець", "note.md")
        assert res.success
        assert memory.files["note.md"] == "# Нотатка\nпривіт\nкінець".encode("utf-8")
        assert not memory.files["note.md"].startswith(b"\xef\xbb\xbf")
        assert memory.types == ["text/markdown"]
        assert "Inbox/note.md" in res.text
        notification.notify_raw.assert_not_called()

    async def test_drive_context_present(self, setup):
        agent, _, _ = setup
        res = await _save(agent, "a", "a.txt")
        assert res.drive_context()[0]["path"] == "Inbox/a.txt"

    async def test_csv_and_svg_types(self, setup):
        agent, memory, _ = setup
        await _save(agent, "a,b\r\n1,2", "t.csv")
        await _save(agent, "<svg/>", "i.svg")
        assert memory.types == ["text/csv", "image/svg+xml"]
        assert memory.files["t.csv"] == b"a,b\n1,2"

    async def test_same_text_already_there(self, setup):
        agent, memory, _ = setup
        await _save(agent, "same", "n.md")
        res = await _save(agent, "same", "n.md")
        assert "Already on the drive" in res.text
        assert memory.uploads == ["n.md"]

    async def test_different_text_gets_suffix(self, setup):
        agent, memory, _ = setup
        await _save(agent, "one", "n.md")
        res = await _save(agent, "two!", "n.md")
        assert res.success
        assert sorted(memory.files) == ["n 1.md", "n.md"]
        assert "n 1.md" in res.text

    async def test_explicit_folder(self, setup):
        agent, memory, _ = setup
        res = await _save(agent, "x", "n.md", folder="Inbox")
        assert res.success


class TestRefusals:
    async def _refused(self, setup, text, name=None):
        agent, memory, _ = setup
        res = await _save(agent, text, name)
        assert not res.success
        memory.mock.upload.assert_not_called()
        memory.mock.get_root.assert_not_called()
        memory.mock.list_children.assert_not_called()
        return res

    async def test_empty_text_names_the_problem(self, setup):
        res = await self._refused(setup, "", "n.md")
        assert "empty" in res.text

    async def test_lone_surrogate_is_refused_not_an_error(self, setup):
        res = await self._refused(setup, "\ud800", "n.md")
        assert "UTF-8" in res.text and "Drive operation failed" not in res.text

    async def test_missing_name(self, setup):
        res = await self._refused(setup, "x")
        assert "name" in res.text

    @pytest.mark.parametrize("name", ["p.png", "d.pdf", "noext"])
    async def test_disallowed_extension(self, setup, name):
        res = await self._refused(setup, "x", name)
        assert "file_ref" in res.text

    async def test_over_limit(self, setup):
        await self._refused(setup, "a" * (MAX_DRIVE_TEXT_FILE_BYTES + 1), "big.txt")


def test_rendered_delegate_schema_describes_text_and_name_for_this_intent():
    """Retargeted at the cut (DRIVE_SPECIALIST_RFC §4.2, §8.1): the composed text reaches the specialist in Smart's
    `file_contents` field, exactly as it goes into the file; the file-name rule is in `save_text_file.file_name`."""
    from src.agents.base_agent import BaseAgent
    from src.domain import drive_tool_texts as tt
    from src.infrastructure.agent_manifest import ALL_DESCRIPTORS
    from src.infrastructure.agent_registry import AgentRegistry
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    declaration = BaseAgent._build_delegate_tool_declaration(registry.get_available_intents())
    props = declaration["parameters"]["properties"]["context"]["properties"]
    assert "exactly as it goes into the file" in props["file_contents"]["description"]
    save = next(t for t in tt.drive_tool_declarations() if t["name"] == tt.SAVE_TEXT_FILE)
    name = save["parameters"]["properties"]["file_name"]["description"]
    assert ".md" in name and "ending in" in name
