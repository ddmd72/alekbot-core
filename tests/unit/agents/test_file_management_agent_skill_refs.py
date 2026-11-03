"""
Unit tests for FileManagementAgent's `skill:` ref handling (RFC §15.4).

- open_file routes a `skill:` ref straight to the text path (no mime guessing) and, on
  a not-found, surfaces the resolver's own message verbatim (no "re-upload" text).
- delete_file refuses a `skill:` ref outright — skill files change only through
  draft_skill / `$skill delete`, never through delete_file.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.agents.file_management_agent import FileManagementAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent, AgentStatus
from src.infrastructure.agent_manifest import Intent
from src.ports.file_storage_port import FileStoragePort
from src.ports.document_read_port import DocumentReadPort
from src.services.file_conversion_service import FileConversionService
from tests.support.drive_access_stub import allow_access


# ---------------------------------------------------------------------------
# Helpers (copied from tests/unit/agents/test_file_management_agent.py)
# ---------------------------------------------------------------------------

def _make_config():
    return AgentConfig(
        agent_id="file_management_agent_user1",
        agent_type="file_management",
        capabilities={},
        metadata={"user_id": "user1"},
    )


def _make_message(intent: str, file_ref: str = None, user_id: str = "user1"):
    payload = {"intent": intent}
    if file_ref:
        payload["file_ref"] = file_ref
    msg = MagicMock(spec=AgentMessage)
    msg.task_id = "task1"
    msg.intent = AgentIntent.QUERY
    msg.payload = payload
    msg.context = {"user_id": user_id}
    return msg


@pytest.fixture
def mock_conversion():
    return AsyncMock(spec=FileConversionService)


@pytest.fixture
def mock_storage():
    return AsyncMock(spec=FileStoragePort)


@pytest.fixture
def agent(mock_conversion, mock_storage):
    return FileManagementAgent(
        config=_make_config(),
        conversion_service=mock_conversion,
        storage=mock_storage,
    )


# ---------------------------------------------------------------------------
# delete_file refuses skill: refs
# ---------------------------------------------------------------------------

class TestDeleteRefusesSkillRef:

    async def test_delete_file_refuses_skill_ref(self, agent, mock_storage):
        msg = _make_message(Intent.DELETE_FILE, file_ref="skill:fs/r.md")

        resp = await agent.execute(msg)

        assert resp.status == AgentStatus.FAILED
        assert "$skill delete" in resp.error
        mock_storage.delete.assert_not_called()

    async def test_delete_file_skill_ref_mentions_draft_skill(self, agent, mock_storage):
        msg = _make_message(Intent.DELETE_FILE, file_ref="skill:fs/r.md")

        resp = await agent.execute(msg)

        assert "draft_skill" in resp.error
        mock_storage.delete.assert_not_called()


# ---------------------------------------------------------------------------
# open_file routes skill: refs to the text path
# ---------------------------------------------------------------------------

class TestOpenFileSkillRef:

    async def test_open_file_skill_ref_uses_text_path(self, mock_storage):
        # A skill file is the system's own text (EXTERNAL_FILES_RFC P15): it opens through the real conversion service
        # without a read, a byte download or a wrapper (§4.6: skill text is never marked).
        skills = AsyncMock()
        skills.read.return_value = "skill file body"
        conversion = FileConversionService(storage=mock_storage, skill_files=skills, access=allow_access())
        documents = AsyncMock(spec=DocumentReadPort)
        conversion.attach_documents(documents)
        agent = FileManagementAgent(config=_make_config(), conversion_service=conversion, storage=mock_storage)
        msg = _make_message(Intent.OPEN_FILE, file_ref="skill:fs/r.md")

        resp = await agent.execute(msg)

        assert resp.status == AgentStatus.SUCCESS
        assert resp.result == "[File: skill:fs/r.md]\nskill file body\n[/File: skill:fs/r.md]"
        skills.read.assert_awaited_once_with("user1", "skill:fs/r.md")
        documents.read.assert_not_called()
        mock_storage.download.assert_not_called()

    async def test_open_file_skill_ref_not_found_message(self, agent, mock_conversion):
        mock_conversion.open = AsyncMock(side_effect=FileNotFoundError("Skill 'fs' has no file 'r.md'"),
        )
        msg = _make_message(Intent.OPEN_FILE, file_ref="skill:fs/r.md")

        resp = await agent.execute(msg)

        assert resp.status == AgentStatus.FAILED
        assert "re-upload" not in resp.error
        assert "fs" in resp.error
