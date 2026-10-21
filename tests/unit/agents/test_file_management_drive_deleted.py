"""`drive_deleted` on search_files_in_drive, and the Step 1 guard's line (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md
§27.9.8, §27.9.11)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.file_management_agent import FileManagementAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.drive_search import STATUS_OK, DriveSearchResult
from src.domain.language import LanguageCode
from src.domain.user_drive import DriveMirrorWriteRefused
from src.infrastructure.agent_manifest import Intent
from src.ports.file_storage_port import FileStoragePort
from src.services.drive_search_service import DriveSearchService
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService


def _msg(intent=Intent.SEARCH_FILES_IN_DRIVE, **payload):
    msg = MagicMock(spec=AgentMessage)
    msg.task_id, msg.intent = "t1", AgentIntent.QUERY
    msg.payload = {"intent": intent, **payload}
    msg.context = {"user_id": "u1", "account_id": "a1"}
    return msg


@pytest.fixture
def search():
    svc = AsyncMock(spec=DriveSearchService)
    svc.search.return_value = DriveSearchResult(status=STATUS_OK, matched_total=0)
    svc.search_deleted.return_value = "Deleted files (0):\nNo deleted file matched."
    return svc


@pytest.fixture
def drive_service():
    return AsyncMock(spec=UserDriveService)


def _agent(search, drive_service, **extra):
    return FileManagementAgent(
        config=AgentConfig(agent_id="file_management_agent_u1", agent_type="file_management", capabilities={},
                           metadata={}),
        conversion_service=AsyncMock(spec=FileConversionService), storage=AsyncMock(spec=FileStoragePort),
        drive_service=drive_service, drive_search=search, **extra)


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "yes", "Yes", "1", " true ", True])
async def test_drive_deleted_parsed_from_strings(search, drive_service, value):
    resp = await _agent(search, drive_service).execute(_msg(drive_query="budget", drive_deleted=value))
    assert resp.status == AgentStatus.SUCCESS
    search.search_deleted.assert_awaited_once_with("u1", "budget")
    search.search.assert_not_called()
    assert "Deleted files (0):" in resp.result


@pytest.mark.parametrize("value", ["false", "no", "0", "", "maybe", None, False])
async def test_anything_else_is_an_ordinary_search(search, drive_service, value):
    resp = await _agent(search, drive_service).execute(_msg(drive_query="budget", drive_deleted=value))
    assert resp.status == AgentStatus.SUCCESS
    search.search.assert_awaited_once()
    search.search_deleted.assert_not_called()


async def test_an_empty_query_is_allowed_with_the_flag(search, drive_service):
    resp = await _agent(search, drive_service).execute(_msg(drive_deleted="true"))
    assert resp.status == AgentStatus.SUCCESS
    search.search_deleted.assert_awaited_once_with("u1", "")


async def test_filters_are_not_required_and_not_read_with_the_flag(search, drive_service):
    await _agent(search, drive_service).execute(_msg(drive_deleted="true", drive_kind="not a kind"))
    search.search_deleted.assert_awaited_once()


async def test_the_result_carries_the_reference_reminder(search, drive_service):
    from src.domain.drive_search import REFERENCE_REMINDER
    resp = await _agent(search, drive_service).execute(_msg(drive_deleted="true"))
    assert resp.result.startswith(REFERENCE_REMINDER)


async def test_no_search_service_says_so(drive_service):
    resp = await _agent(None, drive_service).execute(_msg(drive_deleted="true"))
    assert resp.status == AgentStatus.FAILED


# -- the Step 1 guard's line ----------------------------------------------------------------------------------

@pytest.mark.parametrize("intent,payload", [
    (Intent.SAVE_FILE_TO_DRIVE, {"text": "x", "name": "a.md", "folder": "_Transcripts"}),
    (Intent.DELETE_FILE_FROM_DRIVE, {"file_ref": "drive:f1"}),
    (Intent.MOVE_FILE_IN_DRIVE, {"file_ref": "drive:f1", "folder": "Docs"}),
    (Intent.CREATE_FOLDER_IN_DRIVE, {"folder": "_Transcripts/x"}),
    (Intent.UPDATE_FILE_IN_DRIVE, {"file_ref": "drive:f1", "append_text": "x"}),
])
async def test_a_refused_write_answers_the_localized_line(search, drive_service, intent, payload):
    for method in ("save", "ensure_folder", "move", "delete", "append_text", "replace_with"):
        getattr(drive_service, method).side_effect = DriveMirrorWriteRefused("mirror folder")
    localization = MagicMock()
    localization.get_ui_string.return_value = "Цю папку веде бот; змінюйте її в OneDrive."
    language = AsyncMock()
    language.resolve_ui_language.return_value = LanguageCode.UK
    agent = _agent(search, drive_service, localization=localization, language_service=language)
    resp = await agent.execute(_msg(intent, **payload))
    assert resp.status == AgentStatus.FAILED
    assert resp.error == "Цю папку веде бот; змінюйте її в OneDrive."


async def test_a_refused_write_without_localization_falls_back_to_english(search, drive_service):
    drive_service.delete.side_effect = DriveMirrorWriteRefused("mirror folder")
    resp = await _agent(search, drive_service).execute(_msg(Intent.DELETE_FILE_FROM_DRIVE, file_ref="drive:f1"))
    assert resp.status == AgentStatus.FAILED
    assert resp.error == "This folder is managed by the bot; change it in OneDrive."


def test_the_line_exists_in_every_language():
    from src.domain.ui_messages import UIMessage
    from src.locales import en, es, fr, uk
    for module in (en, es, fr, uk):
        strings = next(v for k, v in vars(module).items() if isinstance(v, dict) and UIMessage.DRIVE_DELETE_FAILED.value in v)
        assert strings[UIMessage.DRIVE_MIRROR_WRITE_REFUSED.value].strip()
