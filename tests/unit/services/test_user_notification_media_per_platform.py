"""`notify_file_bytes` picks the media port by the channel's platform (USER_DRIVE_INDEXING_RFC §26.2 review, 2026-10-14).

Only Slack needs the placeholder message (it turns a stored U… user id into the D… DM channel the upload needs);
Telegram uploads to the stored chat id as is. A platform with no registered port returns False before anything is
posted: a "📎" placeholder in a chat whose file never follows would repeat on every retry of the drive notice."""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from src.domain.notification import NotificationChannel
from src.ports.notification_channel_factory_port import NotificationChannelFactoryPort
from src.ports.notification_state_port import NotificationStatePort
from src.services.user_notification_service import UserNotificationService


def _service(platform, channel_id="12345", **kw):
    state = AsyncMock(spec=NotificationStatePort)
    state.get_primary.return_value = None
    state.get.return_value = NotificationChannel(user_id="u", platform=platform, channel_id=channel_id,
                                                 updated_at=datetime(2026, 1, 1))
    response = MagicMock()
    response.channel_id = "D1"
    response.send_message = AsyncMock(return_value={"ts": "1"})
    factory = MagicMock(spec=NotificationChannelFactoryPort)
    factory.create.return_value = response
    svc = UserNotificationService(state_repo=state, channel_factory=factory, coordinator=MagicMock(),
                                  notification_sla={}, **kw)
    return svc, response, factory


def _port():
    port = AsyncMock()
    port.upload_file = AsyncMock()
    return port


async def test_telegram_uploads_to_the_chat_id_without_a_placeholder():
    svc, response, _ = _service("telegram", "12345")
    slack, telegram = _port(), _port()
    svc.register_platform_media("slack", slack)
    svc.register_platform_media("telegram", telegram)
    assert await svc.notify_file_bytes("u", "a", b"x", "list.txt", "List") is True
    telegram.upload_file.assert_awaited_once_with(file_bytes=b"x", filename="list.txt", title="List",
                                                  channel_id="12345")
    slack.upload_file.assert_not_awaited()
    response.send_message.assert_not_awaited()


async def test_a_platform_without_a_port_posts_nothing():
    svc, response, factory = _service("telegram", "12345", platform_media=_port())   # only Slack's
    assert await svc.notify_file_bytes("u", "a", b"x", "list.txt", "List") is False
    response.send_message.assert_not_awaited()
    factory.create.assert_not_called()


async def test_slack_still_resolves_the_dm_channel_with_a_placeholder():
    slack = _port()
    svc, response, _ = _service("slack", "U1")
    svc.register_platform_media("slack", slack)
    assert await svc.notify_file_bytes("u", "a", b"x", "list.txt", "List") is True
    response.send_message.assert_awaited_once_with("📎")
    slack.upload_file.assert_awaited_once_with(file_bytes=b"x", filename="list.txt", title="List", channel_id="D1")


async def test_the_constructor_port_is_slack_s():
    slack = _port()
    svc, _, _ = _service("slack", "U1", platform_media=slack)
    assert await svc.notify_file_bytes("u", "a", b"x", "list.txt", "List") is True
    slack.upload_file.assert_awaited_once()
