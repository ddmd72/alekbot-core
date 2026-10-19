"""Mutations on a `drive:` ref the index knows the bot cannot open (USER_DRIVE_INDEXING_RFC §26.2, Δ55, review of S1):
move, delete and update answer the index's access sentence instead of "no longer on the drive"; an item the index does
not know keeps that answer."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.file_management_agent import FileManagementAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.drive_search_texts import ACCESS_NOTE_FILE
from src.domain.user_drive import DriveItemNotFoundError
from src.infrastructure.agent_manifest import Intent
from src.ports.file_storage_port import FileStoragePort
from src.services.drive_search_service import DriveSearchService
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService


def _msg(intent, **payload):
    msg = MagicMock(spec=AgentMessage)
    msg.task_id = "t1"
    msg.intent = AgentIntent.QUERY
    msg.payload = {"intent": intent, **payload}
    msg.context = {"user_id": "u1", "account_id": "a1"}
    return msg


@pytest.fixture
def drive():
    d = AsyncMock(spec=UserDriveService)
    for name in ("move", "delete", "append_text", "get_item"):
        getattr(d, name).side_effect = DriveItemNotFoundError("f1")
    return d


@pytest.fixture
def search():
    s = AsyncMock(spec=DriveSearchService)
    s.access_note.side_effect = lambda user_id, item_id=None, path=None: ACCESS_NOTE_FILE if item_id == "f1" else None
    return s


@pytest.fixture
def agent(drive, search):
    return FileManagementAgent(
        config=AgentConfig(agent_id="file_management_agent_u1", agent_type="file_management", capabilities={},
                           metadata={}),
        conversion_service=AsyncMock(spec=FileConversionService), storage=AsyncMock(spec=FileStoragePort),
        drive_service=drive, drive_search=search)


@pytest.mark.parametrize("intent, payload", [
    (Intent.MOVE_FILE_IN_DRIVE, {"folder": "Archive"}),
    (Intent.DELETE_FILE_FROM_DRIVE, {}),
    (Intent.UPDATE_FILE_IN_DRIVE, {"append_text": "x"}),
])
async def test_a_mutation_of_a_closed_file_answers_the_access_sentence(agent, intent, payload):
    resp = await agent.execute(_msg(intent, file_ref="drive:f1", **payload))
    assert resp.status == AgentStatus.FAILED and resp.error == ACCESS_NOTE_FILE


async def test_an_unknown_item_still_answers_gone(agent, drive):
    drive.delete.side_effect = DriveItemNotFoundError("f9")
    resp = await agent.execute(_msg(Intent.DELETE_FILE_FROM_DRIVE, file_ref="drive:f9"))
    assert resp.error == FileManagementAgent._GONE
