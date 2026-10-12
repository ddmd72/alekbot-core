"""search_files_in_drive with nothing to search for is refused with a correction, never answered with "the newest
files" (USER_DRIVE_INDEXING_RFC §10.1, §10.4). Smart must fill `query` for delegate_to_specialist, but search reads
only drive_query: a request whose words sit in `query` alone arrives empty."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.file_management_agent import FileManagementAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.drive_search import STATUS_OK, DriveSearchResult
from src.infrastructure.agent_manifest import FILE_MANAGEMENT, Intent
from src.ports.file_storage_port import FileStoragePort
from src.services.drive_search_service import DriveSearchService
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService


def _msg(**payload):
    msg = MagicMock(spec=AgentMessage)
    msg.task_id, msg.intent = "t1", AgentIntent.QUERY
    msg.payload = {"intent": Intent.SEARCH_FILES_IN_DRIVE, **payload}
    msg.context = {"user_id": "u1", "account_id": "a1"}
    return msg


@pytest.fixture
def search():
    svc = AsyncMock(spec=DriveSearchService)
    svc.search.return_value = DriveSearchResult(status=STATUS_OK, matched_total=0)
    return svc


def _agent(search):
    return FileManagementAgent(
        config=AgentConfig(agent_id="file_management_agent_u1", agent_type="file_management", capabilities={},
                           metadata={}),
        conversion_service=AsyncMock(spec=FileConversionService), storage=AsyncMock(spec=FileStoragePort),
        drive_service=AsyncMock(spec=UserDriveService), drive_search=search)


@pytest.mark.parametrize("payload", [
    {"query": "find the lease contract"},
    {"query": "find the lease contract", "drive_query": "  "},
    {"drive_query": "???"},
    {"drive_query": "", "drive_folder": "/"},
])
async def test_nothing_to_search_for_is_refused_with_the_correction(search, payload):
    resp = await _agent(search).execute(_msg(**payload))
    assert resp.status == AgentStatus.FAILED
    assert "drive_query" in resp.error and "drive_date_from" in resp.error and "filter" in resp.error
    search.search.assert_not_called()


@pytest.mark.parametrize("payload", [
    {"drive_kind": "spreadsheet"}, {"drive_folder": "Contracts"}, {"drive_date_from": "2026-10-01"},
    {"drive_date_to": "2026-10-01"}, {"drive_query": "lease"}, {"drive_query": "Rome"},
])
async def test_name_words_or_any_filter_is_enough(search, payload):
    resp = await _agent(search).execute(_msg(**payload))
    assert resp.status == AgentStatus.SUCCESS and search.search.await_count == 1


def test_the_query_field_description_says_it_is_required_and_what_goes_in_it():
    text = FILE_MANAGEMENT.context_schemas[Intent.SEARCH_FILES_IN_DRIVE]["drive_query"]
    assert "REQUIRED unless the request is filter-only" in text
    assert "`query` field is not read" in text
    assert "place or subject" in text and "no type words" in text.lower() and "dates" in text
    intent = FILE_MANAGEMENT.capability_descriptions[Intent.SEARCH_FILES_IN_DRIVE]
    assert "drive_query is required unless" in intent


def test_the_eval_refuses_what_the_agent_refuses():
    from scripts.validation.drive_eval.runner import request_for
    with pytest.raises(ValueError, match="drive_query"):
        request_for({"query": "", "filters": {}})
    assert request_for({"query": "", "filters": {"kind": "pdf"}}).filter_only
