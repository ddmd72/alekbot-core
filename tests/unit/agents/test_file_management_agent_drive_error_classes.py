"""Step 1 drive intents under the Step 2 error classes (USER_DRIVE_INDEXING_RFC §4).

A rejected refresh still reads as "not connected" (DriveReauthRequired is a DriveNotConnectedError).
A 401 after a successful refresh and exhausted throttling are operation failures, never a request to
reconnect a grant that is alive."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.file_management_agent import FileManagementAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.language import LanguageCode
from src.domain.user_drive import DriveAccessDenied, DriveReauthRequired, DriveThrottled
from src.infrastructure.agent_manifest import Intent
from src.ports.file_storage_port import FileStoragePort
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService

_RECONNECT = "connect it in the Cabinet"


def _msg(intent, **payload):
    msg = MagicMock(spec=AgentMessage)
    msg.task_id = "t1"
    msg.intent = AgentIntent.QUERY
    msg.payload = {"intent": intent, **payload}
    msg.context = {"user_id": "u1", "account_id": "a1", "origin_channel_id": "C1", "origin_platform": "slack"}
    return msg


@pytest.fixture
def drive():
    return AsyncMock(spec=UserDriveService)


@pytest.fixture
def agent(drive):
    language = AsyncMock()
    language.resolve_ui_language.return_value = LanguageCode.EN
    return FileManagementAgent(
        config=AgentConfig(agent_id="file_management_agent_u1", agent_type="file_management",
                           capabilities={}, metadata={}),
        conversion_service=AsyncMock(spec=FileConversionService), storage=AsyncMock(spec=FileStoragePort),
        notification=AsyncMock(), drive_service=drive, localization=MagicMock(), language_service=language,
    )


async def test_rejected_refresh_still_asks_to_reconnect(agent, drive):
    drive.list_folder.side_effect = DriveReauthRequired("invalid_grant")
    resp = await agent.execute(_msg(Intent.LIST_FILES_IN_DRIVE))
    assert resp.status == AgentStatus.FAILED and _RECONNECT in resp.error


@pytest.mark.parametrize("exc", [DriveAccessDenied("refused"), DriveThrottled(120.0)])
async def test_operation_errors_never_ask_to_reconnect(agent, drive, exc):
    drive.list_folder.side_effect = exc
    resp = await agent.execute(_msg(Intent.LIST_FILES_IN_DRIVE))
    assert resp.status == AgentStatus.FAILED
    assert _RECONNECT not in resp.error and type(exc).__name__ in resp.error
