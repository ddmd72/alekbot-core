"""DriveDisconnectService failure paths (slice 2b P-F): a failed note read and a failed sync enqueue are logged and
never undo or stop the disconnect."""
from unittest.mock import AsyncMock, MagicMock

from src.services import drive_disconnect_service
from tests.unit.services.test_drive_disconnect_service import U, World, _item_note


async def test_a_failed_notes_read_is_logged_archives_nothing_and_the_flow_goes_on(monkeypatch):
    log = MagicMock()
    monkeypatch.setattr(drive_disconnect_service, "logger", log)
    w = World()
    _, second = await w.two()
    w.put(_item_note("b2", "Tom's plan.docx", "n-second"))
    w.notes.active = AsyncMock(side_effect=RuntimeError("store down"))

    assert await w.service.disconnect(U, second.connection_id) is True

    (message,), kwargs = log.error.call_args
    assert message == "Disconnect: reading the notes of user=usr-1 failed: RuntimeError" and kwargs["exc_info"] is True
    assert (await w.notes.get(U, "n-second")).state == "active"
    assert len(w.queue.tasks) == 1


async def test_a_failed_sync_enqueue_is_logged_and_the_disconnect_still_ends(monkeypatch):
    log = MagicMock()
    monkeypatch.setattr(drive_disconnect_service, "logger", log)
    w = World()
    _, second = await w.two()
    w.queue.enqueue_worker_task = AsyncMock(side_effect=RuntimeError("queue down"))

    assert await w.service.disconnect(U, second.connection_id) is True

    (message,), kwargs = log.error.call_args
    assert message == "Disconnect done but the sync task was not queued: queue down" and kwargs["exc_info"] is True
