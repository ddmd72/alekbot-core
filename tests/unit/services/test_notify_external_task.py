"""UserNotificationService.notify_external_task and ui_strings, the paths the delivery task's tests do not reach
(EXTERNAL_TASK_QUEUE_RFC §5.9): a channel that cannot be built, a service without a session store, a history
write that fails, and a service without localization. Alex Doe is `u1`."""
import logging

import pytest

from tests.unit.services.test_notification_drive_note_receipt import _service

NOTICE = "Your research task is done."
EVENT = "[System: external task finished]"
KEPT = "I told you the research task was done."


@pytest.fixture(autouse=True)
def _capture(caplog):
    caplog.set_level(logging.DEBUG)


async def _notify(service):
    return await service.notify_external_task("u1", "a1", text=NOTICE, event_line=EVENT, kept_line=KEPT)


async def test_a_channel_that_cannot_be_built_posts_nothing_and_reports_false(caplog):
    service, channel, store, factory = _service()
    factory.create.return_value = None
    assert await _notify(service) is False
    assert channel.posted == []
    store.append_messages_batch.assert_not_called()
    assert any("Cannot create channel" in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)


async def test_without_a_session_store_the_notice_is_posted_and_no_history_is_written():
    service, channel, _, _ = _service()
    service._session_store = None
    assert await _notify(service) is True
    assert channel.posted == [("literal", NOTICE)]


async def test_a_failed_history_write_is_logged_and_the_delivery_still_counts(caplog):
    service, channel, store, _ = _service()
    store.append_messages_batch.side_effect = RuntimeError("firestore down")
    assert await _notify(service) is True
    assert channel.posted == [("literal", NOTICE)]
    assert any("history pair failed" in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)


async def test_without_localization_there_are_no_ui_strings():
    service, _, _, _ = _service(localization=False)
    assert await service.ui_strings("u1") is None
