"""`notify_raw` and `notify_file_bytes` report whether they sent (USER_DRIVE_INDEXING_RFC §26.2, Δ53): the drive
fan-out's notice posts its text only once the attached list was sent, and takes its flags down when nothing was. Both
still swallow delivery errors; existing callers ignore the result."""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from src.domain.notification import NotificationChannel
from src.ports.notification_channel_factory_port import NotificationChannelFactoryPort
from src.ports.notification_state_port import NotificationStatePort
from src.services.user_notification_service import UserNotificationService


def _service(*, channel=True, send_error=None, upload_error=None, media=True):
    state = AsyncMock(spec=NotificationStatePort)
    state.get_primary.return_value = None
    state.get.return_value = NotificationChannel(user_id="u", platform="slack", channel_id="C1",
                                                 updated_at=datetime(2026, 1, 1)) if channel else None
    response = MagicMock()
    response.channel_id = "D1"
    response.send_long_text = AsyncMock(side_effect=send_error)
    response.send_message = AsyncMock(return_value={"ts": "1"})
    factory = MagicMock(spec=NotificationChannelFactoryPort)
    factory.create.return_value = response
    platform_media = AsyncMock()
    platform_media.upload_file = AsyncMock(side_effect=upload_error)
    return UserNotificationService(state_repo=state, channel_factory=factory, coordinator=MagicMock(),
                                   notification_sla={}, platform_media=platform_media if media else None)


async def test_notify_raw_says_sent():
    assert await _service().notify_raw("u", "a", "hello") is True


async def test_notify_raw_says_not_sent_without_a_channel_or_on_an_error():
    assert await _service(channel=False).notify_raw("u", "a", "hello") is False
    assert await _service(send_error=RuntimeError("slack down")).notify_raw("u", "a", "hello") is False


async def test_notify_file_bytes_says_sent():
    assert await _service().notify_file_bytes("u", "a", b"x", "list.txt", "List") is True


async def test_notify_file_bytes_says_not_sent():
    assert await _service(channel=False).notify_file_bytes("u", "a", b"x", "list.txt", "List") is False
    assert await _service(media=False).notify_file_bytes("u", "a", b"x", "list.txt", "List") is False
    assert await _service(upload_error=RuntimeError("upload failed")).notify_file_bytes(
        "u", "a", b"x", "list.txt", "List") is False
