"""FileManagement's `open_file` on a drive ref the bound refuses (USER_DRIVE_INDEXING_RFC §29.6 L13): an answer, not a
failure, with the orchestrator's route; a chat file stays out of scope."""
from unittest.mock import AsyncMock, MagicMock

from src.agents.file_management_agent import FileManagementAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.document_limits import MAX_CONVERTED_CHARS
from src.domain.document_open_texts import cut_note
from src.domain.document_presentation import present_text
from src.domain.document_read import ReadOutcome, ReadStatus
from src.domain.drive_history import drive_history_lines
from src.domain.drive_open_bound import refusal_text
from src.domain.drive_open_texts import ROUTE_ORCHESTRATOR
from src.domain.drive_text_extent import ExtentState, TextExtent
from src.domain.user_drive import DriveItem
from src.infrastructure.agent_manifest import Intent
from src.infrastructure.drive_operations import DriveOperations
from src.ports.file_storage_port import FileStoragePort
from src.services.user_drive_service import UserDriveService
from tests.support.drive_service_spec_mock import spec_drive_service
from tests.unit.infrastructure.test_drive_operations_open_bound import FakeExtentSearch, long_text
from tests.support.opened_file import opened_body
from tests.unit.infrastructure.test_drive_operations_uncovered_paths import FakeConversion

PATH = "Docs/Oak Street parking rules.md"
SHOWN = f"Drive/{PATH}"    # what the model reads: canonical <mount>/<path> (design §1.2 S-4)
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
    drive = spec_drive_service("u1")
    drive.get_item.return_value = RULES   # the open reads the item through the drive service (MULTI_DRIVE_RFC §6)
    ops = DriveOperations(conversion_service=conversion, drive_service=drive, drive_search=search)
    return FileManagementAgent(
        config=AgentConfig(agent_id="file_management_agent_u1", agent_type="file_management", capabilities={},
                           metadata={}),
        conversion_service=conversion, storage=AsyncMock(spec=FileStoragePort), notification=AsyncMock(),
        drive_service=drive, drive_operations=ops)


async def test_a_refused_drive_open_is_a_success_with_the_route_and_a_history_entry():
    conv = FakeConversion({"drive:a1:r1": RULES}, text=long_text(200_000))
    agent = _agent(conv, FakeExtentSearch({"r1": EXTENT}))
    resp = await agent.execute(_msg("drive:a1:r1"))
    assert resp.status == AgentStatus.SUCCESS
    assert resp.result == refusal_text(SHOWN, EXTENT) + "\n" + ROUTE_ORCHESTRATOR
    assert conv.text_reads == [] and conv.byte_reads == []
    lines = drive_history_lines([resp.history_context["drive_context"]])
    assert lines == [f"[Drive: {SHOWN} ref=drive:a1:r1 — too long to open whole (200,000 characters, 130 passages)]"]


async def test_a_cut_open_is_still_a_success_with_its_body():
    conv = FakeConversion({"drive:a1:r1": RULES}, text=long_text(80_000))
    agent = _agent(conv, FakeExtentSearch())
    resp = await agent.execute(_msg("drive:a1:r1"))
    assert resp.status == AgentStatus.SUCCESS and "[Cut by the system" in resp.result
    assert ROUTE_ORCHESTRATOR not in resp.result


async def test_a_chat_file_of_100000_characters_is_cut_by_its_store_not_refused_by_the_drive_bound(monkeypatch):
    """§29 stays the drive's: a chat file asks no extent and is never refused. Since D20 (EXTERNAL_FILES_RFC §4.5) every
    open path cuts parser text once, at 60 000 characters, with a note naming the cut outside the wrapper."""
    monkeypatch.setattr("src.infrastructure.drive_operations.secrets.token_hex", lambda n: "0123456789abcdef")
    conversion = AsyncMock()
    conversion.open.return_value = opened_body("x" * 100_000)
    drive = AsyncMock(spec=UserDriveService)
    search = FakeExtentSearch({})
    ops = DriveOperations(conversion_service=conversion, drive_service=drive, drive_search=search)
    agent = FileManagementAgent(
        config=AgentConfig(agent_id="file_management_agent_u1", agent_type="file_management", capabilities={},
                           metadata={}),
        conversion_service=conversion, storage=AsyncMock(spec=FileStoragePort), notification=AsyncMock(),
        drive_service=drive, drive_operations=ops)
    resp = await agent.execute(_msg("notes.txt"))
    assert resp.status == AgentStatus.SUCCESS and ROUTE_ORCHESTRATOR not in resp.result
    assert resp.result == present_text("x" * 100_000, store="chat", ref="notes.txt", label="notes.txt",
                                       nonce="0123456789abcdef", outcome=ReadOutcome(status=ReadStatus.TEXT))
    closing = resp.result.index('</external_content nonce="0123456789abcdef">')
    assert "x" * MAX_CONVERTED_CHARS in resp.result[:closing] and "x" * (MAX_CONVERTED_CHARS + 1) not in resp.result
    assert cut_note("notes.txt", MAX_CONVERTED_CHARS, 100_000 - MAX_CONVERTED_CHARS) in resp.result[closing:]
    assert search.extent_asks == []
