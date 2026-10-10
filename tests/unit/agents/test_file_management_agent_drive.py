"""Drive intents in FileManagementAgent (docs/10_rfcs/USER_DRIVE_RFC.md §4.4–§4.13)."""
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
def agent(conversion, drive, notifier):
    localization = MagicMock()
    localization.get_ui_string.side_effect = lambda lang, m: {
        UIMessage.DRIVE_DELETED_FILE: "DEL {path}",
        UIMessage.DRIVE_DELETED_FOLDER: "DELDIR {path} {count}",
        UIMessage.DRIVE_REPLACED: "REP {path} {before} {after}",
    }[m]
    language = AsyncMock()
    language.resolve_ui_language.return_value = LanguageCode.UK
    return FileManagementAgent(
        config=AgentConfig(agent_id="file_management_agent_u1", agent_type="file_management",
                           capabilities={}, metadata={}),
        conversion_service=conversion, storage=AsyncMock(spec=FileStoragePort),
        notification=notifier, drive_service=drive, localization=localization, language_service=language,
    )


class TestStoreGuards:
    async def test_delete_file_refuses_drive_ref(self, agent, drive):
        resp = await agent.execute(_msg(Intent.DELETE_FILE, file_ref="drive:f1"))
        assert resp.status == AgentStatus.FAILED and "delete_file_from_drive" in resp.error
        drive.delete.assert_not_called()

    async def test_mutating_drive_intents_refuse_chat_ref(self, agent):
        for intent in (Intent.MOVE_FILE_IN_DRIVE, Intent.DELETE_FILE_FROM_DRIVE, Intent.UPDATE_FILE_IN_DRIVE):
            resp = await agent.execute(_msg(intent, file_ref="report.docx", folder="X", append_text="t"))
            assert resp.status == AgentStatus.FAILED and "save_file_to_drive" in resp.error

    async def test_save_refuses_drive_ref_before_naming_or_download(self, agent, conversion, drive):
        with patch("src.agents.file_management_agent.drive_filename") as naming:
            resp = await agent.execute(_msg(Intent.SAVE_FILE_TO_DRIVE, file_ref="drive:f1"))
        assert resp.status == AgentStatus.FAILED and "move_file_in_drive" in resp.error
        naming.assert_not_called()
        conversion.resolve_bytes.assert_not_called()
        drive.save.assert_not_called()

    async def test_open_file_is_lenient_with_drive_ref(self, agent, conversion):
        conversion.get_drive_item.return_value = NOTE
        conversion.resolve_content.return_value = "[File: Notes/todo.md]\nx\n[/File: Notes/todo.md]"
        resp = await agent.execute(_msg(Intent.OPEN_FILE, file_ref="drive:n1"))
        assert resp.status == AgentStatus.SUCCESS
        conversion.resolve_content.assert_awaited_once_with("drive:n1", "u1")

    async def test_open_file_from_drive_is_lenient_with_chat_ref(self, agent, conversion):
        conversion.resolve_content.return_value = "text"
        resp = await agent.execute(_msg(Intent.OPEN_FILE_FROM_DRIVE, file_ref="notes.txt"))
        assert resp.status == AgentStatus.SUCCESS and resp.result == "text"
        conversion.get_drive_item.assert_not_called()

    async def test_unknown_intent_lists_no_search(self, agent):
        resp = await agent.execute(_msg("search_files_in_drive", search_text="x"))
        assert resp.status == AgentStatus.FAILED


class TestOpenCaps:
    async def test_large_image_refused_without_download(self, agent, conversion):
        conversion.get_drive_item.return_value = BIG_IMAGE
        resp = await agent.execute(_msg(Intent.OPEN_FILE_FROM_DRIVE, file_ref="drive:i1"))
        assert resp.status == AgentStatus.SUCCESS and "cannot be opened yet" in resp.result
        conversion.resolve_bytes.assert_not_called()

    async def test_large_pdf_refused_without_download(self, agent, conversion):
        conversion.get_drive_item.return_value = BIG_PDF
        resp = await agent.execute(_msg(Intent.OPEN_FILE, file_ref="drive:p1"))
        assert resp.status == AgentStatus.SUCCESS and "cannot be opened yet" in resp.result
        conversion.resolve_bytes.assert_not_called()

    async def test_heic_refused_without_download(self, agent, conversion):
        conversion.get_drive_item.return_value = HEIC
        resp = await agent.execute(_msg(Intent.OPEN_FILE_FROM_DRIVE, file_ref="drive:h1"))
        assert resp.status == AgentStatus.SUCCESS and "cannot be viewed yet" in resp.result
        conversion.resolve_bytes.assert_not_called()
        conversion.resolve_content.assert_not_called()

    async def test_small_image_is_attached_for_vision(self, agent, conversion):
        conversion.get_drive_item.return_value = SMALL_PNG
        conversion.resolve_bytes.return_value = b"\x89PNG"
        resp = await agent.execute(_msg(Intent.OPEN_FILE, file_ref="drive:i2"))
        assert resp.status == AgentStatus.SUCCESS
        assert resp.metadata["file_data"]["mime_type"] == "image/png"
        assert resp.history_context == {"drive_context": [{"path": "a.png", "ref": "drive:i2"}]}

    async def test_folder_is_pointed_to_listing(self, agent, conversion):
        conversion.get_drive_item.return_value = FOLDER
        resp = await agent.execute(_msg(Intent.OPEN_FILE_FROM_DRIVE, file_ref="drive:d1"))
        assert resp.status == AgentStatus.FAILED and "list_files_in_drive" in resp.error


class TestSaveAndList:
    async def test_save_uses_readable_name_and_history_context(self, agent, conversion, drive):
        conversion.resolve_bytes.return_value = b"%PDF"
        drive.save.return_value = SaveOutcome(item=FILE, created=["Inbox"])
        ref = "docs/u1/3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b-lease.pdf"
        resp = await agent.execute(_msg(Intent.SAVE_FILE_TO_DRIVE, file_ref=ref))
        drive.save.assert_awaited_once_with("u1", b"%PDF", "lease.pdf", "application/pdf", None)
        assert "[Drive: Inbox/lease.pdf" in resp.result
        assert resp.history_context == {"drive_context": [{"path": "Inbox/lease.pdf", "ref": "drive:f1"}]}

    async def test_save_already_existed_and_renamed_messages(self, agent, conversion, drive):
        conversion.resolve_bytes.return_value = b"x"
        drive.save.return_value = SaveOutcome(item=FILE, created=[], already_existed=True)
        resp = await agent.execute(_msg(Intent.SAVE_FILE_TO_DRIVE, file_ref="lease.pdf"))
        assert resp.result.startswith("Already on the drive")
        renamed = DriveItem(item_id="f2", name="lease 1.pdf", path="Inbox/lease 1.pdf", is_folder=False)
        drive.save.return_value = SaveOutcome(item=renamed, created=[], renamed=True)
        resp = await agent.execute(_msg(Intent.SAVE_FILE_TO_DRIVE, file_ref="lease.pdf"))
        assert 'Saved as "lease 1.pdf"' in resp.result

    async def test_save_of_missing_chat_file_is_not_a_drive_config_error(self, agent, conversion):
        conversion.resolve_bytes.side_effect = FileNotFoundError("nope")
        resp = await agent.execute(_msg(Intent.SAVE_FILE_TO_DRIVE, file_ref="gone.pdf"))
        assert resp.status == AgentStatus.FAILED
        assert "not configured" not in resp.error and "gone.pdf" in resp.error

    async def test_list_labels(self, agent, drive):
        root = DriveItem(item_id="r", name="Alek-bot", path="", is_folder=True)
        drive.list_folder.return_value = ListOutcome(folder=root, items=[FOLDER, FILE], truncated=False)
        resp = await agent.execute(_msg(Intent.LIST_FILES_IN_DRIVE))
        assert "[Drive folder: Meetings/2025/" in resp.result and "[Drive: Inbox/lease.pdf" in resp.result


class TestReceipts:
    async def test_delete_folder_receipt_with_count(self, agent, drive, notifier):
        drive.delete.return_value = DeleteOutcome(item=FOLDER, file_count=14)
        resp = await agent.execute(_msg(Intent.DELETE_FILE_FROM_DRIVE, file_ref="drive:d1"))
        assert resp.status == AgentStatus.SUCCESS
        notifier.notify_raw.assert_awaited_once_with(
            "u1", "a1", "DELDIR Meetings/2025 14", channel_id_override="C1", platform_override="slack")

    async def test_delete_file_receipt(self, agent, drive, notifier):
        drive.delete.return_value = DeleteOutcome(item=FILE, file_count=1)
        await agent.execute(_msg(Intent.DELETE_FILE_FROM_DRIVE, file_ref="drive:f1"))
        assert notifier.notify_raw.call_args.args[2] == "DEL Inbox/lease.pdf"

    async def test_replace_receipt_with_sizes(self, agent, conversion, drive, notifier):
        conversion.resolve_bytes.return_value = b"x" * 3000
        after = DriveItem(item_id="f1", name="lease.pdf", path="Inbox/lease.pdf", is_folder=False,
                          size_bytes=3000, mime_type="application/pdf")
        drive.get_item.return_value = FILE
        drive.replace_with.return_value = UpdateOutcome(item=after, before_size=2048, mode="replace")
        resp = await agent.execute(_msg(Intent.UPDATE_FILE_IN_DRIVE, file_ref="drive:f1", source_ref="new.pdf"))
        assert resp.status == AgentStatus.SUCCESS
        assert notifier.notify_raw.call_args.args[2] == "REP Inbox/lease.pdf 2KB 3KB"

    async def test_append_has_no_receipt(self, agent, drive, notifier):
        drive.append_text.return_value = UpdateOutcome(item=NOTE, before_size=10, mode="append")
        resp = await agent.execute(_msg(Intent.UPDATE_FILE_IN_DRIVE, file_ref="drive:n1", append_text="- two"))
        assert resp.status == AgentStatus.SUCCESS and resp.result.startswith("Appended to")
        drive.append_text.assert_awaited_once_with("u1", "n1", "- two")
        notifier.notify_raw.assert_not_called()

    async def test_update_needs_exactly_one_mode(self, agent):
        resp = await agent.execute(_msg(Intent.UPDATE_FILE_IN_DRIVE, file_ref="drive:n1"))
        assert resp.status == AgentStatus.FAILED and "exactly one" in resp.error

    async def test_move_has_no_receipt(self, agent, drive, notifier):
        moved = DriveItem(item_id="f1", name="lease.pdf", path="Contracts/lease.pdf", is_folder=False)
        drive.move.return_value = MoveOutcome(before=FILE, after=moved, created=[])
        resp = await agent.execute(_msg(Intent.MOVE_FILE_IN_DRIVE, file_ref="drive:f1", folder="Contracts"))
        assert "Inbox/lease.pdf → [Drive: Contracts/lease.pdf" in resp.result
        notifier.notify_raw.assert_not_called()

    async def test_create_folder_has_no_receipt(self, agent, drive, notifier):
        from src.domain.user_drive import FolderResolution
        drive.ensure_folder.return_value = FolderResolution(folder=FOLDER, created=["Meetings/2025"])
        resp = await agent.execute(_msg(Intent.CREATE_FOLDER_IN_DRIVE, folder="Meetings/2025"))
        assert resp.status == AgentStatus.SUCCESS and "[Drive folder: Meetings/2025/" in resp.result
        notifier.notify_raw.assert_not_called()

    async def test_receipt_failure_does_not_fail_the_delete(self, agent, drive, notifier):
        drive.delete.return_value = DeleteOutcome(item=FILE, file_count=1)
        notifier.notify_raw.side_effect = RuntimeError("slack down")
        resp = await agent.execute(_msg(Intent.DELETE_FILE_FROM_DRIVE, file_ref="drive:f1"))
        assert resp.status == AgentStatus.SUCCESS

    async def test_receipt_text_uses_the_users_language(self, agent, drive):
        drive.delete.return_value = DeleteOutcome(item=FILE, file_count=1)
        await agent.execute(_msg(Intent.DELETE_FILE_FROM_DRIVE, file_ref="drive:f1"))
        assert agent._localization.get_ui_string.call_args.args[0] == LanguageCode.UK


class TestShield:
    async def test_cancelled_delete_still_completes_with_receipt(self, agent, drive, notifier):
        gate = asyncio.Event()

        async def slow_delete(user_id, item_id):
            await gate.wait()
            return DeleteOutcome(item=FILE, file_count=1)

        drive.delete.side_effect = slow_delete
        task = asyncio.create_task(agent.execute(_msg(Intent.DELETE_FILE_FROM_DRIVE, file_ref="drive:f1")))
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
    async def test_slow_mutation_answers_still_running(self, agent, drive, notifier):
        gate = asyncio.Event()

        async def slow_delete(user_id, item_id):
            await gate.wait()
            return DeleteOutcome(item=FILE, file_count=1)

        drive.delete.side_effect = slow_delete
        agent.MUTATION_WAIT_S = 0.01
        resp = await agent.execute(_msg(Intent.DELETE_FILE_FROM_DRIVE, file_ref="drive:f1"))
        assert resp.status == AgentStatus.SUCCESS and "still running" in resp.result
        assert "search" not in resp.result.lower() and "listing" in resp.result
        gate.set()
        for _ in range(5):
            await asyncio.sleep(0)
        notifier.notify_raw.assert_awaited_once()  # the receipt still arrives

    async def test_one_users_mutations_run_one_at_a_time(self, agent, conversion, drive):
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
        await asyncio.gather(*(agent.execute(_msg(Intent.SAVE_FILE_TO_DRIVE, file_ref="a.pdf")) for _ in range(3)))
        assert peak == 1

    async def test_abandoned_retry_dedupes_after_first(self, agent, conversion, drive):
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
        first = asyncio.create_task(agent.execute(_msg(Intent.SAVE_FILE_TO_DRIVE, file_ref="a.pdf")))
        for _ in range(3):
            await asyncio.sleep(0)
        first.cancel()
        second = asyncio.create_task(agent.execute(_msg(Intent.SAVE_FILE_TO_DRIVE, file_ref="a.pdf")))
        for _ in range(3):
            await asyncio.sleep(0)
        assert len(calls) == 1  # second is waiting on the per-user lock
        gate.set()
        resp = await second
        assert resp.result.startswith("Already on the drive")


class TestErrors:
    async def test_not_connected_message(self, agent, drive):
        drive.list_folder.side_effect = DriveNotConnectedError("x")
        resp = await agent.execute(_msg(Intent.LIST_FILES_IN_DRIVE))
        assert resp.status == AgentStatus.FAILED and "connect it in the Cabinet" in resp.error

    async def test_not_found_message(self, agent, drive):
        drive.delete.side_effect = DriveItemNotFoundError("x")
        resp = await agent.execute(_msg(Intent.DELETE_FILE_FROM_DRIVE, file_ref="drive:zz"))
        assert resp.status == AgentStatus.FAILED and "no longer on the drive" in resp.error

    async def test_conversion_not_wired_is_not_configured(self, agent, conversion):
        conversion.get_drive_item.side_effect = FileNotFoundError("unavailable")
        resp = await agent.execute(_msg(Intent.OPEN_FILE, file_ref="drive:n1"))
        assert resp.status == AgentStatus.FAILED and resp.error == "The drive is not configured."

    async def test_name_conflict_is_a_clear_refusal(self, agent, drive):
        drive.move.side_effect = DriveNameConflictError("taken")
        resp = await agent.execute(_msg(Intent.MOVE_FILE_IN_DRIVE, file_ref="drive:f1", folder="X"))
        assert resp.status == AgentStatus.FAILED and resp.error == "A file with that name already exists there."

    async def test_path_error_passthrough(self, agent, drive):
        drive.move.side_effect = DrivePathError("'A' cannot be moved into itself")
        resp = await agent.execute(_msg(Intent.MOVE_FILE_IN_DRIVE, file_ref="drive:f1", folder="A/B"))
        assert resp.error == "'A' cannot be moved into itself"

    async def test_root_protected_passthrough(self, agent, drive):
        drive.delete.side_effect = DriveRootProtectedError("The drive area itself cannot be deleted or moved")
        resp = await agent.execute(_msg(Intent.DELETE_FILE_FROM_DRIVE, file_ref="drive:r"))
        assert resp.status == AgentStatus.FAILED and "cannot be deleted" in resp.error

    async def test_unexpected_error_is_named_not_leaked(self, agent, drive):
        drive.list_folder.side_effect = RuntimeError("secret detail")
        resp = await agent.execute(_msg(Intent.LIST_FILES_IN_DRIVE))
        assert resp.error == "Drive operation failed: RuntimeError."

    async def test_no_drive_wired(self, conversion):
        a = FileManagementAgent(
            config=AgentConfig(agent_id="x", agent_type="file_management", capabilities={}, metadata={}),
            conversion_service=conversion, storage=AsyncMock(spec=FileStoragePort))
        resp = await a.execute(_msg(Intent.LIST_FILES_IN_DRIVE))
        assert resp.error == "The drive is not configured."


class TestNoProviderName:
    async def test_model_facing_texts_never_name_a_provider(self, agent, drive, conversion):
        import re
        from src.agents import file_management_agent as mod
        from src.locales import en, es, fr, uk
        banned = re.compile(r"onedrive|microsoft|google", re.I)
        for loc in (en, uk, fr, es):
            for key in ("drive_deleted_file", "drive_deleted_folder", "drive_replaced"):
                assert not banned.search(loc.UI_STRINGS[key])
        assert not banned.search(agent.STILL_RUNNING) and not banned.search(agent._NOT_CONNECTED)
        assert mod  # module imported
