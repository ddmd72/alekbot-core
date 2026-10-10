"""save_file_to_drive text mode (USER_DRIVE_RFC §4.7.1)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.file_management_agent import FileManagementAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.user_drive import MAX_DRIVE_TEXT_FILE_BYTES, DriveItem
from src.infrastructure.agent_manifest import FILE_MANAGEMENT, Intent
from src.ports.file_storage_port import FileStoragePort
from src.ports.user_drive_port import UserDrivePort
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService

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

    async def _list(self, user_id, folder_id):
        if folder_id == "root":
            return [INBOX]
        return [DriveItem(item_id=f"id:{n}", name=n, path=f"Inbox/{n}", is_folder=False, size_bytes=len(b))
                for n, b in self.files.items()]

    async def _download(self, user_id, item_id):
        return self.files[item_id.removeprefix("id:")]

    async def _upload(self, user_id, parent_id, filename, data, content_type):
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


def _msg(**payload):
    msg = MagicMock(spec=AgentMessage)
    msg.task_id = "t1"
    msg.intent = AgentIntent.QUERY
    msg.payload = {"intent": Intent.SAVE_FILE_TO_DRIVE, **payload}
    msg.context = {"user_id": "u1", "account_id": "a1"}
    return msg


@pytest.fixture
def setup():
    memory = _MemoryDrive()
    notification = AsyncMock()
    agent = FileManagementAgent(
        config=AgentConfig(agent_id="file_management_agent_u1", agent_type="file_management",
                           capabilities={}, metadata={}),
        conversion_service=AsyncMock(spec=FileConversionService), storage=AsyncMock(spec=FileStoragePort),
        notification=notification, drive_service=UserDriveService(memory.port()),
        localization=MagicMock(), language_service=AsyncMock(),
    )
    return agent, memory, notification


class TestTextMode:
    async def test_cyrillic_markdown_exact_bytes_default_inbox(self, setup):
        agent, memory, notification = setup
        res = await agent.execute(_msg(text="# Нотатка\r\nпривіт\rкінець", name="note.md"))
        assert res.status == AgentStatus.SUCCESS
        assert memory.files["note.md"] == "# Нотатка\nпривіт\nкінець".encode("utf-8")
        assert not memory.files["note.md"].startswith(b"\xef\xbb\xbf")
        assert memory.types == ["text/markdown"]
        assert "Inbox/note.md" in res.result
        notification.notify_raw.assert_not_called()

    async def test_drive_context_present(self, setup):
        agent, _, _ = setup
        res = await agent.execute(_msg(text="a", name="a.txt"))
        assert res.history_context["drive_context"][0]["path"] == "Inbox/a.txt"

    async def test_csv_and_svg_types(self, setup):
        agent, memory, _ = setup
        await agent.execute(_msg(text="a,b\r\n1,2", name="t.csv"))
        await agent.execute(_msg(text="<svg/>", name="i.svg"))
        assert memory.types == ["text/csv", "image/svg+xml"]
        assert memory.files["t.csv"] == b"a,b\n1,2"

    async def test_same_text_already_there(self, setup):
        agent, memory, _ = setup
        await agent.execute(_msg(text="same", name="n.md"))
        res = await agent.execute(_msg(text="same", name="n.md"))
        assert "Already on the drive" in res.result
        assert memory.uploads == ["n.md"]

    async def test_different_text_gets_suffix(self, setup):
        agent, memory, _ = setup
        await agent.execute(_msg(text="one", name="n.md"))
        res = await agent.execute(_msg(text="two!", name="n.md"))
        assert res.status == AgentStatus.SUCCESS
        assert sorted(memory.files) == ["n 1.md", "n.md"]
        assert "n 1.md" in res.result

    async def test_explicit_folder(self, setup):
        agent, memory, _ = setup
        res = await agent.execute(_msg(text="x", name="n.md", folder="Inbox"))
        assert res.status == AgentStatus.SUCCESS


class TestRefusals:
    async def _refused(self, setup, **payload):
        agent, memory, _ = setup
        res = await agent.execute(_msg(**payload))
        assert res.status != AgentStatus.SUCCESS
        memory.mock.upload.assert_not_called()
        memory.mock.get_root.assert_not_called()
        memory.mock.list_children.assert_not_called()
        return res

    async def test_both_sources(self, setup):
        res = await self._refused(setup, file_ref="a.md", text="x", name="a.md")
        assert "exactly one" in res.error.lower()

    async def test_neither_source(self, setup):
        await self._refused(setup)

    async def test_missing_name(self, setup):
        res = await self._refused(setup, text="x")
        assert "name" in res.error

    @pytest.mark.parametrize("name", ["p.png", "d.pdf", "noext"])
    async def test_disallowed_extension(self, setup, name):
        res = await self._refused(setup, text="x", name=name)
        assert "file_ref" in res.error

    async def test_over_limit(self, setup):
        await self._refused(setup, text="a" * (MAX_DRIVE_TEXT_FILE_BYTES + 1), name="big.txt")


def test_manifest_schema_has_text():
    schema = FILE_MANAGEMENT.context_schemas[Intent.SAVE_FILE_TO_DRIVE]
    assert "text" in schema and "name" in schema
