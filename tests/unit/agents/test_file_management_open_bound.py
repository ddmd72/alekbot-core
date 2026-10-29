"""FileManagement's `open_file` on a drive ref the bound refuses (USER_DRIVE_INDEXING_RFC §29.6 L13): an answer, not a
failure, with the orchestrator's route; a chat file stays out of scope."""
from unittest.mock import AsyncMock, MagicMock

from src.agents.file_management_agent import FileManagementAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.drive_history import drive_history_lines
from src.domain.drive_open_bound import refusal_text
from src.domain.drive_open_texts import ROUTE_ORCHESTRATOR
from src.domain.drive_text_extent import ExtentState, TextExtent
from src.domain.user_drive import DriveItem
from src.infrastructure.agent_manifest import Intent
from src.infrastructure.drive_operations import DriveOperations
from src.ports.file_storage_port import FileStoragePort
from src.services.user_drive_service import UserDriveService
from tests.unit.infrastructure.test_drive_operations_open_bound import FakeExtentSearch, long_text
from tests.unit.infrastructure.test_drive_operations_uncovered_paths import FakeConversion

PATH = "Docs/Oak Street parking rules.md"
RULES = DriveItem(item_id="r1", name="Oak Street parking rules.md", path=PATH, is_folder=False, size_bytes=300_000,
                  mime_type="text/markdown")
EXTENT = TextExtent(ExtentState.PAGEABLE, text_chars=200_000, passages=130, version=1)


def _msg(ref):
    msg = MagicMock(spec=AgentMessage)
    msg.task_id, msg.intent = "t1", AgentIntent.QUERY
    msg.payload = {"intent": Intent.OPEN_FILE, "file_ref": ref}
    msg.context = {"user_id": "u1", "account_id": "a1", "origin_channel_id": "C1", "origin_platform": "slack"}
    return msg


def _agent(conversion, search):
    drive = AsyncMock(spec=UserDriveService)
    ops = DriveOperations(conversion_service=conversion, drive_service=drive, drive_search=search)
    return FileManagementAgent(
        config=AgentConfig(agent_id="file_management_agent_u1", agent_type="file_management", capabilities={},
                           metadata={}),
        conversion_service=conversion, storage=AsyncMock(spec=FileStoragePort), notification=AsyncMock(),
        drive_service=drive, drive_operations=ops)


async def test_a_refused_drive_open_is_a_success_with_the_route_and_a_history_entry():
    conv = FakeConversion({"drive:r1": RULES}, text=long_text(200_000))
    agent = _agent(conv, FakeExtentSearch({"r1": EXTENT}))
    resp = await agent.execute(_msg("drive:r1"))
    assert resp.status == AgentStatus.SUCCESS
    assert resp.result == refusal_text(PATH, EXTENT) + "\n" + ROUTE_ORCHESTRATOR
    assert conv.text_reads == [] and conv.byte_reads == []
    lines = drive_history_lines([resp.history_context["drive_context"]])
    assert lines == [f"[Drive: {PATH} ref=drive:r1 — too long to open whole (200,000 characters, 130 passages)]"]


async def test_a_cut_open_is_still_a_success_with_its_body():
    conv = FakeConversion({"drive:r1": RULES}, text=long_text(80_000))
    agent = _agent(conv, FakeExtentSearch())
    resp = await agent.execute(_msg("drive:r1"))
    assert resp.status == AgentStatus.SUCCESS and "[Cut by the system" in resp.result
    assert ROUTE_ORCHESTRATOR not in resp.result


async def test_a_chat_file_of_100000_characters_still_opens_whole():
    conversion = AsyncMock()
    conversion.resolve_content.return_value = "x" * 100_000
    drive = AsyncMock(spec=UserDriveService)
    search = FakeExtentSearch({})
    ops = DriveOperations(conversion_service=conversion, drive_service=drive, drive_search=search)
    agent = FileManagementAgent(
        config=AgentConfig(agent_id="file_management_agent_u1", agent_type="file_management", capabilities={},
                           metadata={}),
        conversion_service=conversion, storage=AsyncMock(spec=FileStoragePort), notification=AsyncMock(),
        drive_service=drive, drive_operations=ops)
    resp = await agent.execute(_msg("notes.txt"))
    assert resp.status == AgentStatus.SUCCESS and len(resp.result) >= 100_000
    assert search.extent_asks == []
