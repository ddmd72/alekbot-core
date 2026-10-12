"""An attached or sent drive file is named by its real path (USER_DRIVE_INDEXING_RFC §10.4, D2): the label sits
outside any wrapper, next to the reference-material reminder, and is not entity-escaped, so the path the model
copies back resolves."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.file_management_agent import FileManagementAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage
from src.domain.drive_search import REFERENCE_REMINDER_ATTACHED
from src.domain.user_drive import DriveItem
from src.infrastructure.agent_manifest import Intent
from src.ports.file_storage_port import FileStoragePort
from src.services.drive_search_service import DriveSearchService
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService

PDF = DriveItem(item_id="p1", name="Tom's plan.pdf", path="R&D/Tom's plan.pdf", is_folder=False,
                size_bytes=2048, mime_type="application/pdf")
CLIP = DriveItem(item_id="v1", name="Q&A.mp4", path="R&D/Q&A.mp4", is_folder=False, size_bytes=100,
                 mime_type="video/mp4")


def _msg(**payload):
    msg = MagicMock(spec=AgentMessage)
    msg.task_id, msg.intent = "t1", AgentIntent.QUERY
    msg.payload = {"intent": Intent.OPEN_FILE_FROM_DRIVE, **payload}
    msg.context = {"user_id": "u1", "account_id": "a1"}
    return msg


@pytest.mark.parametrize("item", [PDF, CLIP], ids=["attached", "sent"])
async def test_the_label_keeps_the_real_path(item):
    conversion = AsyncMock(spec=FileConversionService)
    conversion.get_drive_item.return_value = item
    conversion.resolve_bytes.return_value = b"%PDF" if item is PDF else b"video"
    agent = FileManagementAgent(
        config=AgentConfig(agent_id="file_management_agent_u1", agent_type="file_management", capabilities={},
                           metadata={}),
        conversion_service=conversion, storage=AsyncMock(spec=FileStoragePort), notification=AsyncMock(),
        drive_service=AsyncMock(spec=UserDriveService), drive_search=AsyncMock(spec=DriveSearchService))
    resp = await agent.execute(_msg(file_ref=item.ref))
    assert resp.result.startswith(REFERENCE_REMINDER_ATTACHED + "\n")
    assert f"[Drive: {item.path} (" in resp.result and "&amp;" not in resp.result and "&#x27;" not in resp.result
