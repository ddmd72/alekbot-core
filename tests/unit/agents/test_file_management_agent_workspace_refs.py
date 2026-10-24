"""open_file reads workspace copies only under files/ — notes are read with the workspace tool, which frames them
as data — and delete_file refuses workspace refs (AGENT_WORKSPACE_RFC §12)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.file_management_agent import FileManagementAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.infrastructure.agent_manifest import FILE_MANAGEMENT, Intent
from src.ports.file_storage_port import FileStoragePort
from src.services.file_conversion_service import FileConversionService


def _message(intent, file_ref):
    msg = MagicMock(spec=AgentMessage)
    msg.task_id = "task1"
    msg.intent = AgentIntent.QUERY
    msg.payload = {"intent": intent, "file_ref": file_ref}
    msg.context = {"user_id": "user1"}
    return msg


@pytest.fixture
def conversion():
    return AsyncMock(spec=FileConversionService)


@pytest.fixture
def storage():
    return AsyncMock(spec=FileStoragePort)


@pytest.fixture
def agent(conversion, storage):
    config = AgentConfig(agent_id="file_management_agent_user1", agent_type="file_management", capabilities={},
                         metadata={"user_id": "user1"})
    return FileManagementAgent(config=config, conversion_service=conversion, storage=storage)


class TestOpen:
    async def test_a_pdf_copy_opens_for_vision(self, agent, conversion):
        conversion.resolve_bytes.return_value = b"%PDF"
        resp = await agent.execute(_message(Intent.OPEN_FILE, "workspace:files/lease.pdf"))
        assert resp.status == AgentStatus.SUCCESS
        assert resp.metadata["file_data"]["mime_type"] == "application/pdf"
        conversion.resolve_bytes.assert_awaited_once_with("workspace:files/lease.pdf", "user1")

    async def test_a_text_copy_opens_as_text(self, agent, conversion):
        conversion.resolve_content.return_value = "[File: workspace:files/notes.txt]\nhi\n[/File: workspace:files/notes.txt]"
        resp = await agent.execute(_message(Intent.OPEN_FILE, "workspace:files/notes.txt"))
        assert resp.status == AgentStatus.SUCCESS and "hi" in resp.result
        conversion.resolve_content.assert_awaited_once_with("workspace:files/notes.txt", "user1")

    async def test_a_note_outside_files_is_refused(self, agent, conversion):
        resp = await agent.execute(_message(Intent.OPEN_FILE, "workspace:projects/garden-shed.md"))
        assert resp.status == AgentStatus.FAILED
        assert "view it with the workspace tool" in resp.error
        conversion.resolve_content.assert_not_called()
        conversion.resolve_bytes.assert_not_called()

    async def test_an_invalid_ref(self, agent):
        resp = await agent.execute(_message(Intent.OPEN_FILE, "workspace:Files/Lease.pdf"))
        assert resp.status == AgentStatus.FAILED and "invalid workspace path" in resp.error

    async def test_a_missing_copy(self, agent, conversion):
        conversion.resolve_bytes.side_effect = FileNotFoundError("files/lease.pdf")
        resp = await agent.execute(_message(Intent.OPEN_FILE, "workspace:files/lease.pdf"))
        assert resp.status == AgentStatus.FAILED and "is not in your workspace" in resp.error
        assert "re-upload" not in resp.error

    async def test_a_store_error_has_its_own_message(self, agent, conversion):
        conversion.resolve_bytes.side_effect = RuntimeError("gcs down")
        resp = await agent.execute(_message(Intent.OPEN_FILE, "workspace:files/lease.pdf"))
        assert resp.status == AgentStatus.FAILED
        assert resp.error == "The workspace could not be read right now (RuntimeError); try again later."


class TestDelete:
    async def test_refused(self, agent, storage):
        resp = await agent.execute(_message(Intent.DELETE_FILE, "workspace:files/lease.pdf"))
        assert resp.status == AgentStatus.FAILED and "workspace tool's delete" in resp.error
        storage.delete.assert_not_called()


def test_the_manifest_names_workspace_copies():
    assert "workspace:files/<name>" in FILE_MANAGEMENT.capability_descriptions[Intent.OPEN_FILE]
    assert "workspace:files/lease.pdf" in FILE_MANAGEMENT.context_schemas[Intent.OPEN_FILE]["file_ref"]
