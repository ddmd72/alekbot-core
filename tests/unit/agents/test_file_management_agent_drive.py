"""The drive operations of FileManagementAgent's former drive intents (docs/10_rfcs/USER_DRIVE_RFC.md §4.4–§4.13), on
DriveOperations since the cut (DRIVE_SPECIALIST_RFC §5.5, §10 step 4). FileManagement keeps open_file / delete_file:
those tests run through the agent; the drive operations run on the user's DriveOperations, with today's texts."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.file_management_agent import FileManagementAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.language import LanguageCode
from src.domain.ui_messages import UIMessage
from src.domain.user_drive import (
    DeleteOutcome,
    DriveItem,
    DriveItemNotFoundError,
    DriveNameConflictError,
    DriveNotConnectedError,
    DrivePathError,
    DriveRootProtectedError,
    ListOutcome,
    MoveOutcome,
    SaveOutcome,
    UpdateOutcome,
)
from src.infrastructure.agent_manifest import Intent
from src.infrastructure.drive_operations import NOT_CONNECTED, STILL_RUNNING, DriveOperations, wrong_store_text
from src.ports.file_storage_port import FileStoragePort
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService

FILE = DriveItem(item_id="f1", name="lease.pdf", path="Inbox/lease.pdf", is_folder=False,
                 size_bytes=2048, mime_type="application/pdf")
NOTE = DriveItem(item_id="n1", name="todo.md", path="Notes/todo.md", is_folder=False,
                 size_bytes=10, mime_type="text/markdown")
FOLDER = DriveItem(item_id="d1", name="2025", path="Meetings/2025", is_folder=True)
BIG_IMAGE = DriveItem(item_id="i1", name="scan.png", path="scan.png", is_folder=False,
                      size_bytes=6 * 1024 * 1024, mime_type="image/png")
BIG_PDF = DriveItem(item_id="p1", name="book.pdf", path="book.pdf", is_folder=False,
                    size_bytes=21 * 1024 * 1024, mime_type="application/pdf")
HEIC = DriveItem(item_id="h1", name="IMG_0001.HEIC", path="IMG_0001.HEIC", is_folder=False,
                 size_bytes=1024, mime_type="image/heic")
SMALL_PNG = DriveItem(item_id="i2", name="a.png", path="a.png", is_folder=False,
                      size_bytes=100, mime_type="image/png")


def _msg(intent, **payload):
    msg = MagicMock(spec=AgentMessage)
    msg.task_id = "t1"
    msg.intent = AgentIntent.QUERY
    msg.payload = {"intent": intent, **payload}
    msg.context = {"user_id": "u1", "account_id": "a1", "origin_channel_id": "C1", "origin_platform": "slack"}
    return msg


@pytest.fixture
def conversion():
    return AsyncMock(spec=FileConversionService)


@pytest.fixture
def drive():
    return AsyncMock(spec=UserDriveService)


@pytest.fixture
def notifier():
    return AsyncMock()


@pytest.fixture
def localization():
    loc = MagicMock()
    loc.get_ui_string.side_effect = lambda lang, m: {
        UIMessage.DRIVE_DELETED_FILE: "DEL {path}",
        UIMessage.DRIVE_DELETED_FOLDER: "DELDIR {path} {count}",
        UIMessage.DRIVE_REPLACED: "REP {path} {before} {after}",
    }[m]
    return loc


@pytest.fixture
def ops(conversion, drive, notifier, localization):
    language = AsyncMock()
    language.resolve_ui_language.return_value = LanguageCode.UK
    return DriveOperations(conversion_service=conversion, notification=notifier, drive_service=drive,
                           localization=localization, language_service=language)


@pytest.fixture
def agent(conversion, drive, notifier, ops):
    return FileManagementAgent(
        config=AgentConfig(agent_id="file_management_agent_u1", agent_type="file_management",
                           capabilities={}, metadata={}),
        conversion_service=conversion, storage=AsyncMock(spec=FileStoragePort),
        notification=notifier, drive_service=drive, drive_operations=ops,
    )


CTX = {"user_id": "u1", "account_id": "a1", "origin_channel_id": "C1", "origin_platform": "slack"}
WAIT = 100.0   # FileManagement's former MUTATION_WAIT_S, now a call argument


class TestStoreGuards:
    async def test_delete_file_refuses_drive_ref(self, agent, drive):
        resp = await agent.execute(_msg(Intent.DELETE_FILE, file_ref="drive:f1"))
        assert resp.status == AgentStatus.FAILED
        assert resp.error == "'drive:f1' is on the user's drive; delete it through work_with_drive_files."   # §4.4
        drive.delete.assert_not_called()

    async def test_mutating_drive_intents_refuse_chat_ref(self, ops):
        for call in (ops.move(CTX, "u1", "report.docx", folder="X", wait_s=WAIT),
                     ops.delete(CTX, "u1", "report.docx", wait_s=WAIT),
                     ops.append(CTX, "u1", "report.docx", "t", wait_s=WAIT),
                     ops.replace(CTX, "u1", "report.docx", "new.pdf", wait_s=WAIT)):
            out = await call
            assert not out.success and out.text == wrong_store_text("report.docx")
            assert "copy_file_to_drive" in out.text

    async def test_save_refuses_drive_ref_before_naming_or_download(self, ops, conversion, drive):
        with patch("src.infrastructure.drive_operations.drive_filename") as naming:
            out = await ops.copy(CTX, "u1", "drive:f1", wait_s=WAIT)
        assert not out.success and "move_file" in out.text
        naming.assert_not_called()
        conversion.resolve_bytes.assert_not_called()
        drive.save.assert_not_called()

    async def test_open_file_is_lenient_with_drive_ref(self, agent, conversion):
        conversion.get_drive_item.return_value = NOTE
        conversion.resolve_content.return_value = "[File: Notes/todo.md]\nx\n[/File: Notes/todo.md]"
        resp = await agent.execute(_msg(Intent.OPEN_FILE, file_ref="drive:n1"))
        assert resp.status == AgentStatus.SUCCESS
        conversion.resolve_content.assert_awaited_once_with("drive:n1", "u1")

    async def test_open_file_from_drive_is_lenient_with_chat_ref(self, ops, conversion):
        conversion.resolve_content.return_value = "text"
        out = await ops.open(CTX, "u1", "notes.txt")
        assert out.success and out.text == "text"
        conversion.get_drive_item.assert_not_called()

    async def test_unknown_intent_lists_no_search(self, agent):
        resp = await agent.execute(_msg("search_files_in_drive", search_text="x"))
        assert resp.status == AgentStatus.FAILED


class TestOpenCaps:
    async def test_large_image_refused_without_download(self, ops, conversion):
        conversion.get_drive_item.return_value = BIG_IMAGE
        out = await ops.open(CTX, "u1", "drive:i1")
        assert out.success and "cannot be opened yet" in out.text
        conversion.resolve_bytes.assert_not_called()

    async def test_large_pdf_refused_without_download(self, agent, conversion):
        conversion.get_drive_item.return_value = BIG_PDF
        resp = await agent.execute(_msg(Intent.OPEN_FILE, file_ref="drive:p1"))
        assert resp.status == AgentStatus.SUCCESS and "cannot be opened yet" in resp.result
        conversion.resolve_bytes.assert_not_called()

    async def test_heic_refused_without_download(self, ops, conversion):
        conversion.get_drive_item.return_value = HEIC
        out = await ops.open(CTX, "u1", "drive:h1")
        assert out.success and "cannot be viewed yet" in out.text
        conversion.resolve_bytes.assert_not_called()
        conversion.resolve_content.assert_not_called()

    async def test_small_image_is_attached_for_vision(self, agent, conversion):
        conversion.get_drive_item.return_value = SMALL_PNG
        conversion.resolve_bytes.return_value = b"\x89PNG"
        resp = await agent.execute(_msg(Intent.OPEN_FILE, file_ref="drive:i2"))
        assert resp.status == AgentStatus.SUCCESS
        assert resp.metadata["file_data"]["mime_type"] == "image/png"
        assert resp.history_context == {"drive_context": [{"path": "a.png", "ref": "drive:i2", "op": "opened"}]}

    async def test_folder_is_pointed_to_listing(self, ops, conversion):
        conversion.get_drive_item.return_value = FOLDER
        out = await ops.open(CTX, "u1", "drive:d1")
        assert not out.success and out.text == "'Meetings/2025' is a folder; list it instead."


class TestSaveAndList:
    async def test_save_uses_readable_name_and_history_context(self, ops, conversion, drive):
        conversion.resolve_bytes.return_value = b"%PDF"
        drive.save.return_value = SaveOutcome(item=FILE, created=["Inbox"])
        ref = "docs/u1/3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b-lease.pdf"
        out = await ops.copy(CTX, "u1", ref, wait_s=WAIT)
        drive.save.assert_awaited_once_with("u1", b"%PDF", "lease.pdf", "application/pdf", None)
        assert "[Drive: Inbox/lease.pdf" in out.text
        assert out.drive_context() == [{"path": "Inbox/lease.pdf", "ref": "drive:f1"}]

    async def test_save_already_existed_and_renamed_messages(self, ops, conversion, drive):
        conversion.resolve_bytes.return_value = b"x"
        drive.save.return_value = SaveOutcome(item=FILE, created=[], already_existed=True)
        out = await ops.copy(CTX, "u1", "lease.pdf", wait_s=WAIT)
        assert out.text.startswith("Already on the drive")
        renamed = DriveItem(item_id="f2", name="lease 1.pdf", path="Inbox/lease 1.pdf", is_folder=False)
        drive.save.return_value = SaveOutcome(item=renamed, created=[], renamed=True)
        out = await ops.copy(CTX, "u1", "lease.pdf", wait_s=WAIT)
        assert 'Saved as "lease 1.pdf"' in out.text

    async def test_save_of_missing_chat_file_is_not_a_drive_config_error(self, ops, conversion):
        conversion.resolve_bytes.side_effect = FileNotFoundError("nope")
        out = await ops.copy(CTX, "u1", "gone.pdf", wait_s=WAIT)
        assert not out.success
        assert "not configured" not in out.text and "gone.pdf" in out.text

    async def test_list_labels(self, ops, drive):
        root = DriveItem(item_id="r", name="Alek-bot", path="", is_folder=True)
        drive.list_folder.return_value = ListOutcome(folder=root, items=[FOLDER, FILE], truncated=False)
        out = await ops.list_folder(CTX, "u1")
        assert "[Drive folder: Meetings/2025/" in out.text and "[Drive: Inbox/lease.pdf" in out.text


class TestReceipts:
    async def test_delete_folder_receipt_with_count(self, ops, drive, notifier):
        drive.delete.return_value = DeleteOutcome(item=FOLDER, file_count=14)
        out = await ops.delete(CTX, "u1", "drive:d1", wait_s=WAIT)
        assert out.success
        notifier.notify_raw.assert_awaited_once_with(
            "u1", "a1", "DELDIR Meetings/2025 14", channel_id_override="C1", platform_override="slack", literal=True)

    async def test_delete_file_receipt(self, ops, drive, notifier):
        drive.delete.return_value = DeleteOutcome(item=FILE, file_count=1)
        await ops.delete(CTX, "u1", "drive:f1", wait_s=WAIT)
        assert notifier.notify_raw.call_args.args[2] == "DEL Inbox/lease.pdf"

    async def test_replace_receipt_with_sizes(self, ops, conversion, drive, notifier):
        conversion.resolve_bytes.return_value = b"x" * 3000
        after = DriveItem(item_id="f1", name="lease.pdf", path="Inbox/lease.pdf", is_folder=False,
                          size_bytes=3000, mime_type="application/pdf")
        drive.get_item.return_value = FILE
        drive.replace_with.return_value = UpdateOutcome(item=after, before_size=2048, mode="replace")
        out = await ops.replace(CTX, "u1", "drive:f1", "new.pdf", wait_s=WAIT)
        assert out.success
        assert notifier.notify_raw.call_args.args[2] == "REP Inbox/lease.pdf 2KB 3KB"

    async def test_append_has_no_receipt(self, ops, drive, notifier):
        drive.append_text.return_value = UpdateOutcome(item=NOTE, before_size=10, mode="append")
        out = await ops.append(CTX, "u1", "drive:n1", "- two", wait_s=WAIT)
        assert out.success and out.text.startswith("Appended to")
        drive.append_text.assert_awaited_once_with("u1", "n1", "- two")
        notifier.notify_raw.assert_not_called()

    async def test_move_has_no_receipt(self, ops, drive, notifier):
        moved = DriveItem(item_id="f1", name="lease.pdf", path="Contracts/lease.pdf", is_folder=False)
        drive.move.return_value = MoveOutcome(before=FILE, after=moved, created=[])
        out = await ops.move(CTX, "u1", "drive:f1", folder="Contracts", wait_s=WAIT)
        assert "Inbox/lease.pdf → [Drive: Contracts/lease.pdf" in out.text
        notifier.notify_raw.assert_not_called()

    async def test_create_folder_has_no_receipt(self, ops, drive, notifier):
        from src.domain.user_drive import FolderResolution
        drive.ensure_folder.return_value = FolderResolution(folder=FOLDER, created=["Meetings/2025"])
        out = await ops.create_folder(CTX, "u1", "Meetings/2025", wait_s=WAIT)
        assert out.success and "[Drive folder: Meetings/2025/" in out.text
        notifier.notify_raw.assert_not_called()

    async def test_save_has_no_receipt(self, ops, conversion, drive, notifier):
        conversion.resolve_bytes.return_value = b"x"
        drive.save.return_value = SaveOutcome(item=FILE, created=["Inbox"])
        out = await ops.copy(CTX, "u1", "lease.pdf", wait_s=WAIT)
        assert out.success
        notifier.notify_raw.assert_not_called()

    async def test_receipt_failure_does_not_fail_the_delete(self, ops, drive, notifier):
        drive.delete.return_value = DeleteOutcome(item=FILE, file_count=1)
        notifier.notify_raw.side_effect = RuntimeError("slack down")
        out = await ops.delete(CTX, "u1", "drive:f1", wait_s=WAIT)
        assert out.success

    async def test_receipt_text_uses_the_users_language(self, ops, drive, localization):
        drive.delete.return_value = DeleteOutcome(item=FILE, file_count=1)
        await ops.delete(CTX, "u1", "drive:f1", wait_s=WAIT)
        assert localization.get_ui_string.call_args.args[0] == LanguageCode.UK


class TestShield:
    async def test_cancelled_delete_still_completes_with_receipt(self, ops, drive, notifier):
        gate = asyncio.Event()

        async def slow_delete(user_id, item_id):
            await gate.wait()
            return DeleteOutcome(item=FILE, file_count=1)

        drive.delete.side_effect = slow_delete
        task = asyncio.create_task(ops.delete(CTX, "u1", "drive:f1", wait_s=WAIT))
        for _ in range(3):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gate.set()
        for _ in range(5):
            await asyncio.sleep(0)
        notifier.notify_raw.assert_awaited_once()


class TestMutationBudget:
    async def test_slow_mutation_answers_still_running(self, ops, drive, notifier):
        gate = asyncio.Event()

        async def slow_delete(user_id, item_id):
            await gate.wait()
            return DeleteOutcome(item=FILE, file_count=1)

        drive.delete.side_effect = slow_delete
        out = await ops.delete(CTX, "u1", "drive:f1", wait_s=0.01)
        assert out.success and "still running" in out.text
        assert "search" not in out.text.lower() and "listing" in out.text
        gate.set()
        for _ in range(5):
            await asyncio.sleep(0)
        notifier.notify_raw.assert_awaited_once()  # the receipt still arrives

    async def test_provider_timeout_inside_a_mutation_is_a_failure(self, ops, drive, notifier):
        """A TimeoutError raised BY the mutation (aiohttp's socket timeouts subclass it) is not
        the agent's own wait running out: the model must hear "failed", never "still running"."""
        drive.delete.side_effect = asyncio.TimeoutError("socket timed out")
        out = await ops.delete(CTX, "u1", "drive:f1", wait_s=WAIT)
        assert not out.success
        assert "still running" not in (out.text or "")
        assert out.text.startswith("Drive operation failed")
        notifier.notify_raw.assert_not_called()

    async def test_lock_is_released_after_a_failed_mutation(self, ops, drive):
        drive.move.side_effect = DrivePathError("bad")
        first = await ops.move(CTX, "u1", "drive:f1", folder="A", wait_s=WAIT)
        assert not first.success
        drive.move.side_effect = None
        drive.move.return_value = MoveOutcome(before=FILE, after=FILE, created=[])
        second = await ops.move(CTX, "u1", "drive:f1", folder="A", wait_s=0.05)
        assert second.success and second.text.startswith("Moved")

    async def test_one_users_mutations_run_one_at_a_time(self, ops, conversion, drive):
        conversion.resolve_bytes.return_value = b"x"
        active, peak = 0, 0

        async def save(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return SaveOutcome(item=FILE, created=[])

        drive.save.side_effect = save
        await asyncio.gather(*(ops.copy(CTX, "u1", "a.pdf", wait_s=WAIT) for _ in range(3)))
        assert peak == 1

    async def test_abandoned_retry_dedupes_after_first(self, ops, conversion, drive):
        """A retried save waits for the in-flight one, then sees its result."""
        conversion.resolve_bytes.return_value = b"x"
        gate = asyncio.Event()
        calls = []

        async def save(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                await gate.wait()
                return SaveOutcome(item=FILE, created=[])
            return SaveOutcome(item=FILE, created=[], already_existed=True)

        drive.save.side_effect = save
        first = asyncio.create_task(ops.copy(CTX, "u1", "a.pdf", wait_s=WAIT))
        for _ in range(3):
            await asyncio.sleep(0)
        first.cancel()
        second = asyncio.create_task(ops.copy(CTX, "u1", "a.pdf", wait_s=WAIT))
        for _ in range(3):
            await asyncio.sleep(0)
        assert len(calls) == 1  # second is waiting on the per-user lock
        gate.set()
        out = await second
        assert out.text.startswith("Already on the drive")


class TestErrors:
    async def test_not_connected_message(self, ops, drive):
        drive.list_folder.side_effect = DriveNotConnectedError("x")
        out = await ops.list_folder(CTX, "u1")
        assert not out.success and "connect it in the Cabinet" in out.text

    async def test_not_found_message(self, ops, drive):
        drive.delete.side_effect = DriveItemNotFoundError("x")
        out = await ops.delete(CTX, "u1", "drive:zz", wait_s=WAIT)
        assert not out.success and "no longer on the drive" in out.text

    async def test_conversion_not_wired_is_not_configured(self, agent, conversion):
        conversion.get_drive_item.side_effect = FileNotFoundError("unavailable")
        resp = await agent.execute(_msg(Intent.OPEN_FILE, file_ref="drive:n1"))
        assert resp.status == AgentStatus.FAILED and resp.error == "The drive is not configured."

    async def test_name_conflict_is_a_clear_refusal(self, ops, drive):
        drive.move.side_effect = DriveNameConflictError("taken")
        out = await ops.move(CTX, "u1", "drive:f1", folder="X", wait_s=WAIT)
        assert not out.success and out.text == "A file with that name already exists there."

    async def test_path_error_passthrough(self, ops, drive):
        drive.move.side_effect = DrivePathError("'A' cannot be moved into itself")
        out = await ops.move(CTX, "u1", "drive:f1", folder="A/B", wait_s=WAIT)
        assert out.text == "'A' cannot be moved into itself"

    async def test_root_protected_passthrough(self, ops, drive):
        drive.delete.side_effect = DriveRootProtectedError("The drive area itself cannot be deleted or moved")
        out = await ops.delete(CTX, "u1", "drive:r", wait_s=WAIT)
        assert not out.success and "cannot be deleted" in out.text

    async def test_unexpected_error_is_named_not_leaked(self, ops, drive):
        drive.list_folder.side_effect = RuntimeError("secret detail")
        out = await ops.list_folder(CTX, "u1")
        assert out.text == "Drive operation failed: RuntimeError."

    async def test_no_drive_wired(self, conversion):
        a = DriveOperations(conversion_service=conversion)
        out = await a.list_folder(CTX, "u1")
        assert out.text == "The drive is not configured."


class TestNoProviderName:
    async def test_model_facing_texts_never_name_a_provider(self, agent, drive, conversion):
        import re
        from src.agents import file_management_agent as mod
        from src.locales import en, es, fr, uk
        banned = re.compile(r"onedrive|microsoft|google", re.I)
        for loc in (en, uk, fr, es):
            for key in ("drive_deleted_file", "drive_deleted_folder", "drive_replaced"):
                assert not banned.search(loc.UI_STRINGS[key])
        assert not banned.search(STILL_RUNNING) and not banned.search(NOT_CONNECTED)
        from src.domain import drive_tool_texts as tool_texts   # the specialist's tool texts (§8.1)
        texts = [v for v in vars(tool_texts).values() if isinstance(v, str)]
        texts += [str(t) for t in tool_texts.drive_tool_declarations()]
        assert not [t for t in texts if banned.search(t)]
        assert mod  # module imported
