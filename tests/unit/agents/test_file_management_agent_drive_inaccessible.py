"""List and open for files the bot cannot open (USER_DRIVE_INDEXING_RFC §26.2, D33, Δ55).

`list_files_in_drive` takes a `drive:` folder ref as well as a path (listed by id). When list or open meets "not found"
(DriveItemNotFoundError, or a path that does not resolve), the agent asks the index first: an item it knows to be
closed answers with the search card's sentence; anything else keeps today's answer. Since the cut the list and the
open are DriveOperations' (DRIVE_SPECIALIST_RFC §5.5); Smart's lenient open_file stays on FileManagement."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.file_management_agent import FileManagementAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.drive_search_texts import ACCESS_NOTE_FILE, ACCESS_NOTE_FOLDER
from src.domain.user_drive import DriveItem, DriveItemNotFoundError, DrivePathError, ListOutcome
from src.infrastructure.agent_manifest import Intent
from src.infrastructure.drive_operations import GONE, DriveOperations
from src.ports.file_storage_port import FileStoragePort
from src.services.drive_search_service import DriveSearchService
from src.services.file_conversion_service import FileConversionService
from tests.support.drive_service_spec_mock import connection_of, handle_of, spec_drive_service

FOLDER = DriveItem(item_id="d1", name="Shared", path="Shared", is_folder=True)
FILE = DriveItem(item_id="f1", name="lease.pdf", path="Shared/lease.pdf", is_folder=False, size_bytes=10,
                 mime_type="application/pdf")


def _msg(intent, **payload):
    msg = MagicMock(spec=AgentMessage)
    msg.task_id = "t1"
    msg.intent = AgentIntent.QUERY
    msg.payload = {"intent": intent, **payload}
    msg.context = {"user_id": "u1", "account_id": "a1"}
    return msg


@pytest.fixture
def drive():
    return spec_drive_service("u1")


@pytest.fixture
def conversion():
    return AsyncMock(spec=FileConversionService)


@pytest.fixture
def search():
    s = AsyncMock(spec=DriveSearchService)
    s.access_note.return_value = None
    return s


CTX = {"user_id": "u1", "account_id": "a1"}


@pytest.fixture
def agent(drive, conversion, search):
    return DriveOperations(conversion_service=conversion, drive_service=drive, drive_search=search)


@pytest.fixture
def files(agent, conversion):
    """FileManagement on the same DriveOperations: Smart's lenient open_file on a drive: ref."""
    return FileManagementAgent(
        config=AgentConfig(agent_id="file_management_agent_u1", agent_type="file_management", capabilities={},
                           metadata={}),
        conversion_service=conversion, storage=AsyncMock(spec=FileStoragePort), drive_operations=agent)


async def test_list_by_drive_ref_lists_by_id(agent, drive):
    drive.list_folder_by_id.return_value = ListOutcome(folder=FOLDER, items=[FILE], truncated=False)
    resp = await agent.list_folder(CTX, "u1", "drive:a1:d1")
    assert resp.success and "lease.pdf" in resp.text
    drive.list_folder_by_id.assert_awaited_once_with(handle_of("u1"), "d1")
    drive.list_folder.assert_not_called()


async def test_list_by_path_is_unchanged(agent, drive):
    drive.list_folder.return_value = ListOutcome(folder=FOLDER, items=[FILE], truncated=False)
    resp = await agent.list_folder(CTX, "u1", "Shared")
    assert resp.success
    drive.list_folder.assert_awaited_once_with(handle_of("u1"), "Shared")


async def test_list_by_ref_404_answers_from_the_index(agent, drive, search):
    drive.list_folder_by_id.side_effect = DriveItemNotFoundError("d1")
    search.access_note.return_value = ACCESS_NOTE_FOLDER
    resp = await agent.list_folder(CTX, "u1", "drive:a1:d1")
    assert not resp.success and resp.text == ACCESS_NOTE_FOLDER
    search.access_note.assert_awaited_once_with(connection_of("u1"), item_id="d1")


async def test_list_by_path_that_does_not_resolve_answers_from_the_index(agent, drive, search):
    drive.list_folder.side_effect = DrivePathError("No folder 'Shared' on the drive")
    search.access_note.return_value = ACCESS_NOTE_FOLDER
    resp = await agent.list_folder(CTX, "u1", "Shared")
    assert resp.text == ACCESS_NOTE_FOLDER
    search.access_note.assert_awaited_once_with(connection_of("u1"), path="Shared")


async def test_unknown_to_the_index_keeps_todays_answers(agent, drive, search):
    drive.list_folder.side_effect = DrivePathError("No folder 'Nowhere' on the drive")
    resp = await agent.list_folder(CTX, "u1", "Nowhere")
    assert resp.text == "No folder 'Nowhere' on the drive"
    drive.list_folder_by_id.side_effect = DriveItemNotFoundError("x")
    resp = await agent.list_folder(CTX, "u1", "drive:a1:x")
    assert resp.text == GONE


async def test_open_404_answers_from_the_index(agent, files, drive, search):
    drive.get_item.side_effect = DriveItemNotFoundError("f1")
    search.access_note.return_value = ACCESS_NOTE_FILE
    opened = await agent.open(CTX, "u1", "drive:a1:f1")                       # the specialist's open_drive_file
    assert not opened.success and opened.text == ACCESS_NOTE_FILE
    resp = await files.execute(_msg(Intent.OPEN_FILE, file_ref="drive:a1:f1"))   # Smart's lenient open_file
    assert resp.status == AgentStatus.FAILED and resp.error == ACCESS_NOTE_FILE
    search.access_note.assert_awaited_with(connection_of("u1"), item_id="f1")


async def test_open_404_of_an_unknown_item_is_still_gone(agent, drive):
    drive.get_item.side_effect = DriveItemNotFoundError("f9")
    resp = await agent.open(CTX, "u1", "drive:a1:f9")
    assert resp.text == GONE


async def test_a_failing_index_lookup_never_hides_the_original_answer(agent, drive, search):
    drive.get_item.side_effect = DriveItemNotFoundError("f1")
    search.access_note.side_effect = RuntimeError("store down")
    resp = await agent.open(CTX, "u1", "drive:a1:f1")
    assert resp.text == GONE


def test_the_manifest_says_a_folder_ref_is_accepted():
    # Reviewer edit at the cut (DRIVE_SPECIALIST_RFC §8, §8.1): listing is the specialist's `list_folder`, whose
    # `folder` parameter (one text for every tool's folder) takes a path or a folder's drive: ref
    from src.domain import drive_tool_texts as tt
    listing = next(t for t in tt.drive_tool_declarations() if t["name"] == tt.LIST_FOLDER)
    assert listing["parameters"]["properties"]["folder"]["description"] == tt.LIST_FOLDER_PARAM
    assert "or a folder's drive: ref" in tt.LIST_FOLDER_PARAM
