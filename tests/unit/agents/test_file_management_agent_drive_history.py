"""FileManagement's open_file of a drive ref names what was done in `drive_context` (DRIVE_SPECIALIST_RFC §7.4), with the
same rule as the drive specialist (`drive_history.history_entries`): content shown → "opened whole", otherwise
"open answered without its content"; a chat-store open touches no drive item. Invented items only."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.file_management_agent import FileManagementAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.drive_history import drive_history_lines
from src.domain.language import LanguageCode
from src.domain.user_drive import DriveItem
from src.infrastructure.agent_manifest import Intent
from src.infrastructure.drive_operations import DriveOperations
from src.ports.file_storage_port import FileStoragePort
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService

LAB = DriveItem(item_id="abc123", name="lab-report.pdf", path="Health/lab-report.pdf", is_folder=False,
                size_bytes=2048, mime_type="application/pdf")
NOTES = DriveItem(item_id="txt01", name="a.txt", path="Folder/a.txt", is_folder=False, size_bytes=10,
                  mime_type="text/plain")
HEIC = DriveItem(item_id="h1", name="scan.heic", path="scan.heic", is_folder=False, size_bytes=1024,
                 mime_type="image/heic")


def _msg(ref):
    msg = MagicMock(spec=AgentMessage)
    msg.task_id, msg.intent = "t1", AgentIntent.QUERY
    msg.payload = {"intent": Intent.OPEN_FILE, "file_ref": ref}
    msg.context = {"user_id": "u1", "account_id": "a1", "origin_channel_id": "C1", "origin_platform": "slack"}
    return msg


@pytest.fixture
def conversion():
    return AsyncMock(spec=FileConversionService)


@pytest.fixture
def agent(conversion):
    language = AsyncMock()
    language.resolve_ui_language.return_value = LanguageCode.EN
    drive = AsyncMock(spec=UserDriveService)
    ops = DriveOperations(conversion_service=conversion, notification=AsyncMock(), drive_service=drive,
                          localization=MagicMock(), language_service=language)
    return FileManagementAgent(
        config=AgentConfig(agent_id="file_management_agent_u1", agent_type="file_management", capabilities={},
                           metadata={}),
        conversion_service=conversion, storage=AsyncMock(spec=FileStoragePort), notification=AsyncMock(),
        drive_service=drive, drive_operations=ops)


async def _lines(agent, ref):
    resp = await agent.execute(_msg(ref))
    assert resp.status == AgentStatus.SUCCESS
    return drive_history_lines([resp.history_context["drive_context"]])   # Smart's fold wraps one call one level


async def test_an_opened_text_file_is_opened_whole(agent, conversion):
    conversion.get_drive_item.return_value = NOTES
    conversion.resolve_content.return_value = "line one"
    assert await _lines(agent, "drive:txt01") == ["[Drive: Folder/a.txt ref=drive:txt01 — opened whole]"]


async def test_a_pdf_attached_for_vision_is_opened_whole(agent, conversion):
    conversion.get_drive_item.return_value = LAB
    conversion.resolve_bytes.return_value = b"%PDF"
    assert await _lines(agent, "drive:abc123") == [
        "[Drive: Health/lab-report.pdf ref=drive:abc123 — opened whole]"]


async def test_an_open_that_shows_no_content_says_so(agent, conversion):
    conversion.get_drive_item.return_value = HEIC
    assert await _lines(agent, "drive:h1") == ["[Drive: scan.heic ref=drive:h1 — open answered without its content]"]


async def test_a_chat_store_open_touches_no_drive_item(agent, conversion):
    conversion.resolve_content.return_value = "text"
    resp = await agent.execute(_msg("notes.txt"))
    assert resp.status == AgentStatus.SUCCESS and resp.history_context is None
