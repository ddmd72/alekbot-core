"""DriveOperations: today's drive code behind typed outcomes (DRIVE_SPECIALIST_RFC §5.5, §7.2, §11.1 "Outcomes").

Each exception class maps to its outcome class with today's text byte for byte; search classes and read facts come
from the typed result; late-failure notices are keyed on DriveOp; one instance shared by two agents holds one lock."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.drive_deleted_result import DeletedSearchResult
from src.agents.file_management_agent import FileManagementAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_read_facts import ReadFacts
from src.domain.drive_search import (
    STATUS_DISABLED, STATUS_DISCONNECTED, STATUS_FOLDER_NOT_FOUND, STATUS_OK, DriveSearchCard, DriveSearchEnvelope,
    DriveSearchResult,
)
from src.domain.drive_tier_result import CoverageFacts, TieredResult
from src.domain.language import LanguageCode
from src.domain.ui_messages import UIMessage
from src.domain.user_drive import (
    DeleteOutcome, DriveFileTooLargeError, DriveItem, DriveItemNotFoundError, DriveMirrorWriteRefused,
    DriveNameConflictError, DriveNotConnectedError, DrivePathError, DriveRootProtectedError, ListOutcome,
    MoveOutcome,
)
from src.infrastructure.agent_manifest import Intent
from src.infrastructure.drive_operations import (
    GONE, NAME_TAKEN, NOT_CONFIGURED, NOT_CONNECTED, STILL_RUNNING, DriveOperations,
)
from src.ports.file_storage_port import FileStoragePort
from src.services.drive_search_service import DriveSearchService
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_service_connections import ConnectionReads

CTX = {"user_id": "u1", "account_id": "a1", "origin_channel_id": "C1", "origin_platform": "slack"}
FILE = DriveItem(item_id="f1", name="lease.pdf", path="Inbox/lease.pdf", is_folder=False, size_bytes=2048,
                 mime_type="application/pdf")
FOLDER = DriveItem(item_id="d1", name="Inbox", path="Inbox", is_folder=True)


class _Reads(ConnectionReads):
    pass


@pytest.fixture
def drive():
    d = AsyncMock(spec=UserDriveService)
    reads = _Reads()
    d.primary.side_effect = reads.primary
    d.resolve_ref.side_effect = reads.resolve_ref
    d.get_item.return_value = FILE
    return d


@pytest.fixture
def search():
    return AsyncMock(spec=DriveSearchService)


@pytest.fixture
def notifier():
    return AsyncMock()


@pytest.fixture
def ops(drive, search, notifier):
    localization = MagicMock()
    localization.get_ui_string.side_effect = lambda lang, m: {
        UIMessage.DRIVE_DELETE_FAILED: "DELFAIL {path}", UIMessage.DRIVE_DELETED_FILE: "DEL {path}",
        UIMessage.DRIVE_MIRROR_WRITE_REFUSED: "MIRROR"}[m]
    language = AsyncMock()
    language.resolve_ui_language.return_value = LanguageCode.EN
    search.access_note.return_value = None
    return DriveOperations(conversion_service=AsyncMock(spec=FileConversionService), notification=notifier,
                           drive_service=drive, localization=localization, language_service=language,
                           drive_search=search)


@pytest.mark.parametrize("exc, cls, text", [
    (DriveNotConnectedError("x"), OutcomeClass.NOT_CONNECTED, NOT_CONNECTED),
    (DriveItemNotFoundError("x"), OutcomeClass.NOT_FOUND, GONE),
    (FileNotFoundError("x"), OutcomeClass.FAILED, NOT_CONFIGURED),
    (DriveNameConflictError("x"), OutcomeClass.REFUSED, NAME_TAKEN),
    (DriveFileTooLargeError(FILE, 1024), OutcomeClass.REFUSED,
     "'Inbox/lease.pdf' is 2KB; files above 1KB cannot be used yet."),
    (DriveRootProtectedError("The drive area itself cannot be deleted or moved"), OutcomeClass.REFUSED,
     "The drive area itself cannot be deleted or moved"),
    (DrivePathError("No folder 'Inbox/X' on the drive"), OutcomeClass.NOT_FOUND, "No folder 'Inbox/X' on the drive"),
    (DrivePathError("Give a destination folder, a new name, or both"), OutcomeClass.REFUSED,
     "Give a destination folder, a new name, or both"),
    (DriveMirrorWriteRefused("x"), OutcomeClass.REFUSED, "MIRROR"),
    (RuntimeError("boom"), OutcomeClass.FAILED, "Drive operation failed: RuntimeError."),
    (TimeoutError("provider"), OutcomeClass.FAILED, "Drive operation failed: TimeoutError."),
])
async def test_each_exception_maps_to_its_class_with_todays_text(ops, drive, exc, cls, text):
    drive.move.side_effect = exc
    out = await ops.move(CTX, "u1", "drive:f1", folder="Archive", wait_s=5)
    assert (out.op, out.cls, out.text, out.success) == (DriveOp.MOVE, cls, text, False)


async def test_an_item_the_bot_cannot_open_is_refused_with_the_index_sentence(ops, drive, search):
    drive.delete.side_effect = DriveItemNotFoundError("x")
    search.access_note.return_value = "The file exists, but the bot cannot open it"
    out = await ops.delete(CTX, "u1", "drive:f1", wait_s=5)
    assert out.cls is OutcomeClass.REFUSED and out.facts.inaccessible
    assert out.text == "The file exists, but the bot cannot open it"
    search.access_note.assert_awaited_once_with("u1", item_id="f1")


async def test_not_configured_answers_before_any_call():
    ops = DriveOperations(conversion_service=AsyncMock(spec=FileConversionService))
    out = await ops.list_folder(CTX, "u1", "Inbox")
    assert (out.cls, out.text, out.success, ops.configured) == (OutcomeClass.FAILED, NOT_CONFIGURED, False, False)


async def test_request_shape_errors_are_request_errors(ops, drive):
    moved = await ops.move(CTX, "u1", "report.docx", folder="X", wait_s=5)
    copied = await ops.copy(CTX, "u1", "drive:f1", wait_s=5)
    named = await ops.save_text(CTX, "u1", "hello", file_name="notes", wait_s=5)
    empty = await ops.save_text(CTX, "u1", "", file_name="a.md", wait_s=5)
    for out in (moved, copied, named, empty):
        assert out.cls is OutcomeClass.REQUEST_ERROR and not out.success
    assert copied.text.startswith("'drive:f1' is already on the drive")   # its tail is reworded in the cut (§7.6)
    drive.move.assert_not_called()
    drive.save.assert_not_called()


async def test_a_listing_is_ok_with_its_items(ops, drive):
    drive.list_folder.return_value = ListOutcome(folder=FOLDER, items=[FILE], truncated=False)
    out = await ops.list_folder(CTX, "u1", "Inbox")
    assert out.cls is OutcomeClass.OK and out.success
    assert out.drive_context() == [{"path": "Inbox", "ref": "drive:d1"}, {"path": "Inbox/lease.pdf", "ref": "drive:f1"}]


def _card(**kw):
    base = dict(ref="drive:f1", item_id="f1", name="lease.pdf", path="Inbox/lease.pdf", kind="pdf",
                is_folder=False, summary="lease", nonce="abcdef12")
    return DriveSearchCard(**{**base, **kw})


@pytest.mark.parametrize("result, cls", [
    (DriveSearchResult(status=STATUS_DISABLED), OutcomeClass.INDEXING_OFF),
    (DriveSearchResult(status=STATUS_DISCONNECTED), OutcomeClass.NOT_CONNECTED),
    (DriveSearchResult(status=STATUS_DISCONNECTED, other_drive=True), OutcomeClass.REBUILDING),
    (DriveSearchResult(status=STATUS_OK, awaiting_rebuild=True), OutcomeClass.REBUILDING),
    (DriveSearchResult(status=STATUS_FOLDER_NOT_FOUND), OutcomeClass.NOT_FOUND),
    (DriveSearchResult(status=STATUS_OK), OutcomeClass.NOT_FOUND),
    (DriveSearchResult(status=STATUS_OK, cards=(_card(),), ref_tag="a1"), OutcomeClass.OK),
])
async def test_search_class_comes_from_the_typed_result(ops, search, result, cls):
    search.search.return_value = result
    out = await ops.search(CTX, "u1", query="lease")
    assert out.cls is cls and out.success


async def test_a_bad_date_is_a_request_error(ops, search):
    out = await ops.search(CTX, "u1", query="lease", date_from="soon")
    assert out.cls is OutcomeClass.REQUEST_ERROR and not out.success
    search.search.assert_not_called()


async def test_read_facts_come_from_the_cards(ops, search):
    search.search.return_value = DriveSearchResult(
        status=STATUS_OK,
        cards=(_card(passage="…deposit…", card_accuracy=80), _card(kind="image", capture="scan", inaccessible=True)),
        envelope=DriveSearchEnvelope(building=True),
        tiered=TieredResult(concepts=("lease",), coverage=CoverageFacts(not_read_yet=2)), ref_tag="a1")
    out = await ops.search(CTX, "u1", query="lease")
    # Reviewer (USER_DRIVE_INDEXING_RFC §28.3 A13, OQ-A2): the first card scores 80% with no capture recorded, so its
    # text is not known to be born-digital and low_accuracy_image is set
    assert out.facts == ReadFacts(cards=2, passage=True, accuracy=True, scan=True, inaccessible=True, image=True,
                                  coverage_incomplete=True, building=True, low_accuracy_image=True)


async def test_plain_cards_carry_no_triggers(ops, search):
    search.search.return_value = DriveSearchResult(status=STATUS_OK, cards=(_card(),),
                                                   tiered=TieredResult(concepts=("lease",)), ref_tag="a1")
    out = await ops.search(CTX, "u1", query="lease")
    assert out.facts == ReadFacts(cards=1)


async def test_the_deleted_search_is_ok_with_hits_and_not_found_without(ops, search):
    search.search_deleted.return_value = DeletedSearchResult("hits", [FILE], ref_tag="a1")
    hit = await ops.search_deleted(CTX, "u1", "garden")
    search.search_deleted.return_value = DeletedSearchResult("none", [])
    miss = await ops.search_deleted(CTX, "u1", "garden")
    assert (hit.cls, miss.cls) == (OutcomeClass.OK, OutcomeClass.NOT_FOUND)
    assert hit.text.endswith("\nhits") and hit.success and miss.success


def _late(exc=None, delay=0.15):
    async def run(*a, **k):
        await asyncio.sleep(delay)
        if exc:
            raise exc
        return DeleteOutcome(item=FILE, file_count=0)
    return run


async def _settle(ops):
    while ops._shielded or ops._notices:
        await asyncio.sleep(0.01)


async def test_still_running_is_a_success_of_its_own_class(ops, drive):
    drive.move.side_effect = _late()
    out = await ops.move(CTX, "u1", "drive:f1", folder="X", wait_s=0.01)
    assert (out.cls, out.text, out.success) == (OutcomeClass.STILL_RUNNING, STILL_RUNNING, True)
    await _settle(ops)


async def test_a_late_failing_delete_posts_its_notice_keyed_on_the_op(ops, drive, notifier):
    drive.delete.side_effect = _late(RuntimeError("boom"))
    out = await ops.delete(CTX, "u1", "drive:f1", wait_s=0.01)
    assert out.cls is OutcomeClass.STILL_RUNNING
    await _settle(ops)
    notifier.notify_raw.assert_awaited_once()
    assert notifier.notify_raw.await_args.args[:3] == ("u1", "a1", "DELFAIL Inbox/lease.pdf")


async def test_a_late_failing_append_posts_no_notice(ops, drive, notifier):
    async def late_append(*a, **k):
        await asyncio.sleep(0.15)
        raise RuntimeError("boom")
    drive.append_text.side_effect = late_append
    out = await ops.append(CTX, "u1", "drive:f1", "more", wait_s=0.01)
    assert out.cls is OutcomeClass.STILL_RUNNING
    await _settle(ops)
    notifier.notify_raw.assert_not_called()


def _agent(ops):
    return FileManagementAgent(
        config=AgentConfig(agent_id="file_management_agent_u1", agent_type="file_management", capabilities={},
                           metadata={}),
        conversion_service=AsyncMock(spec=FileConversionService), storage=AsyncMock(spec=FileStoragePort),
        drive_operations=ops)


def _msg(intent, **payload):
    msg = MagicMock(spec=AgentMessage)
    msg.task_id, msg.intent, msg.payload, msg.context = "t1", AgentIntent.QUERY, {"intent": intent, **payload}, CTX
    return msg


async def test_the_agent_uses_the_shared_instance(ops):
    agent = _agent(ops)
    assert agent.drive_operations is ops
    assert agent._shielded is ops._shielded and agent._armed is ops._armed


async def test_one_user_one_lock_across_two_holders(ops, drive):
    """Two agents holding one DriveOperations serialize one user's mutations (§5.5)."""
    order = []

    async def move(handle, item_id, folder, new_name):
        order.append(("start", folder))
        await asyncio.sleep(0.05)
        order.append(("end", folder))
        return MoveOutcome(before=FILE, after=FILE, created=[])
    drive.move.side_effect = move
    first, second = _agent(ops), _agent(ops)
    # Reviewer edit at the cut: FileManagement has no drive mutation intent any more (DRIVE_SPECIALIST_RFC §5.5); the
    # first holder's mutation goes through the operations object it holds, the order assertion is unchanged
    await asyncio.gather(first.drive_operations.move(CTX, "u1", "drive:f1", folder="A", wait_s=5),
                         ops.move(CTX, "u1", "drive:f1", folder="B", wait_s=5))
    assert order == [("start", "A"), ("end", "A"), ("start", "B"), ("end", "B")]
    assert second.drive_operations is first.drive_operations


# Reviewer: test_the_agent_maps_an_outcome_back_to_todays_response retired at the cut. Its intent (update) left
# FileManagement (DRIVE_SPECIALIST_RFC §5.5); the outcome-to-AgentResponse mapping of the one drive path it keeps, the
# lenient open_file on a drive: ref, is pinned by test_file_management_agent_drive.py::TestOpenCaps::
# test_small_image_is_attached_for_vision (SUCCESS, file_data, history_context) and ::test_large_pdf_refused_without_download
# (SUCCESS with the outcome's text), both through FileManagementAgent.execute.
