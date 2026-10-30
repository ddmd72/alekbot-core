"""UserDriveService nudges the drive index after each successful mutation
(USER_DRIVE_INDEXING_RFC §7.1 step 1, §13; RFC Δ3)."""
from unittest.mock import AsyncMock

import pytest

from src.domain.drive_index_params import TICK_DEADLINE_S
from src.domain.drive_task_keys import DRIVE_SYNC_TASK, nudge_task_key, parse_nudge
from src.domain.user_drive import DriveItem, DrivePathError
from src.ports.user_drive_port import UserDrivePort
from src.services.user_drive_service import UserDriveService
from tests.support.drive_connections import connection_service
from tests.support.drive_handles import drive_handle
from tests.unit.services.drive_index_fakes import FakeQueue

H = drive_handle("u1")
ROOT = DriveItem(item_id="root", name="Alek-bot", path="", is_folder=True)
INBOX = DriveItem(item_id="inbox", name="Inbox", path="Inbox", is_folder=True)


def _folder(item_id, name, parent=""):
    return DriveItem(item_id=item_id, name=name, path=f"{parent}/{name}" if parent else name, is_folder=True)


def _file(item_id, name, parent="", size=10, mime="text/plain"):
    return DriveItem(item_id=item_id, name=name, path=f"{parent}/{name}" if parent else name,
                     is_folder=False, size_bytes=size, mime_type=mime)


@pytest.fixture
def drive():
    d = AsyncMock(spec=UserDrivePort)
    d.get_root.return_value = ROOT
    return d


@pytest.fixture
def queue():
    return FakeQueue()


@pytest.fixture
def service(drive, queue):
    return UserDriveService(drive, connection_service(), nudge_queue=queue)


def _only_nudge(queue):
    [task] = queue.tasks
    assert task["task_type"] == DRIVE_SYNC_TASK
    assert task["deadline"] == TICK_DEADLINE_S
    assert task["payload"]["user_id"] == "u1"
    nudge = parse_nudge(task["payload"])
    assert nudge is not None and nudge.attempt == 0
    assert task["key"] == nudge_task_key(H.scope, nudge.mutation_id, 0)
    return nudge


class TestNudgePerMutation:
    async def test_save_into_new_folders_carries_folders_and_file(self, service, drive, queue):
        drive.list_children.side_effect = [[], [], []]
        drive.create_folder.side_effect = [_folder("a", "Work"), _folder("b", "2026", "Work")]
        drive.upload.return_value = _file("f", "x.pdf", "Work/2026")
        await service.save(H, b"x", "x.pdf", "application/pdf", "Work/2026")
        assert _only_nudge(queue).item_ids == ("a", "b", "f")

    async def test_save_identical_existing_file_sends_nothing(self, service, drive, queue):
        existing = _file("e", "a.pdf", "Inbox", size=3)
        drive.list_children.side_effect = [[INBOX], [existing]]
        drive.download.return_value = b"abc"
        out = await service.save(H, b"abc", "a.pdf", "application/pdf", None)
        assert out.already_existed and queue.tasks == []

    async def test_create_folder(self, service, drive, queue):
        drive.list_children.side_effect = [[]]
        drive.create_folder.return_value = _folder("n", "Notes")
        await service.ensure_folder(H, "Notes")
        assert _only_nudge(queue).item_ids == ("n",)

    async def test_existing_folder_sends_nothing(self, service, drive, queue):
        drive.list_children.side_effect = [[_folder("n", "Notes")]]
        await service.ensure_folder(H, "Notes")
        assert queue.tasks == []

    async def test_move_carries_item_and_created_destination(self, service, drive, queue):
        drive.get_item.return_value = _file("f", "a.txt", "Inbox")
        drive.list_children.side_effect = [[]]
        drive.create_folder.return_value = _folder("d", "Done")
        drive.move.return_value = _file("f", "a.txt", "Done")
        await service.move(H, "f", "Done", None)
        assert _only_nudge(queue).item_ids == ("d", "f")

    async def test_rename(self, service, drive, queue):
        drive.get_item.return_value = _file("f", "a.txt", "Inbox")
        drive.move.return_value = _file("f", "b.txt", "Inbox")
        await service.move(H, "f", None, "b.txt")
        assert _only_nudge(queue).item_ids == ("f",)

    async def test_append(self, service, drive, queue):
        drive.get_item.return_value = _file("t", "n.md", size=3, mime="text/markdown")
        drive.download.return_value = b"abc"
        drive.replace_content.return_value = _file("t", "n.md", size=8, mime="text/markdown")
        await service.append_text(H, "t", "more")
        assert _only_nudge(queue).item_ids == ("t",)

    async def test_replace(self, service, drive, queue):
        drive.get_item.return_value = _file("t", "n.md")
        drive.replace_content.return_value = _file("t", "n.md", size=2)
        await service.replace_with(H, "t", b"hi", "text/markdown")
        assert _only_nudge(queue).item_ids == ("t",)

    async def test_delete(self, service, drive, queue):
        drive.get_item.return_value = _file("f", "a.txt")
        await service.delete(H, "f")
        assert _only_nudge(queue).item_ids == ("f",)

    async def test_each_mutation_gets_its_own_mutation_id(self, service, drive, queue):
        drive.get_item.return_value = _file("f", "a.txt")
        await service.delete(H, "f")
        await service.delete(H, "f")
        ids = {parse_nudge(t["payload"]).mutation_id for t in queue.tasks}
        assert len(queue.tasks) == 2 and len(ids) == 2


class TestNudgeNeverHurtsTheMutation:
    async def test_failed_enqueue_is_logged_not_raised(self, drive, caplog):
        queue = AsyncMock()
        queue.enqueue_worker_task.side_effect = RuntimeError("queue down")
        service = UserDriveService(drive, connection_service(), nudge_queue=queue)
        drive.get_item.return_value = _file("f", "a.txt")
        out = await service.delete(H, "f")
        assert out.item.item_id == "f"
        drive.delete.assert_awaited_once_with(H, "f")
        assert "nudge failed" in caplog.text

    async def test_failed_mutation_sends_no_nudge(self, service, drive, queue):
        drive.get_item.return_value = _file("f", "a.txt")
        drive.delete.side_effect = RuntimeError("provider 500")
        with pytest.raises(RuntimeError):
            await service.delete(H, "f")
        assert queue.tasks == []

    async def test_refused_mutation_sends_no_nudge(self, service, drive, queue):
        drive.get_item.return_value = _folder("d", "Docs")
        with pytest.raises(DrivePathError):
            await service.replace_with(H, "d", b"x", "text/plain")
        assert queue.tasks == []

    async def test_without_queue_nothing_is_sent(self, drive):
        service = UserDriveService(drive, connection_service())
        drive.get_item.return_value = _file("f", "a.txt")
        await service.delete(H, "f")   # no queue configured: the mutation runs, nothing else happens
        drive.delete.assert_awaited_once_with(H, "f")
