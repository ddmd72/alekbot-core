"""`PlatformKit` and `register_late_platform`: the one late binding of a platform (Telegram) reaches the notification
service, the channel factory and the `/worker` handler together, and nothing of it is there before the call (the graph makes it only after the
Telegram adapter's whole chain built). The service and the factory are the real ones."""
import dataclasses
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.notification_channel_factory import NotificationChannelFactory
from src.composition.platform_kit import PlatformKit, register_late_platform
from src.domain.notification import NotificationChannel
from src.handlers.worker_handler import WorkerHandler
from src.ports.notification_state_port import NotificationStatePort
from src.services.user_notification_service import UserNotificationService


class Media:
    def __init__(self) -> None:
        self.uploads = []

    async def upload_file(self, **kwargs) -> None:
        self.uploads.append(kwargs)


def _world():
    state = AsyncMock(spec=NotificationStatePort)
    state.get_primary.return_value = None
    state.get.return_value = NotificationChannel(user_id="user-1", platform="telegram", channel_id="555",
                                                 updated_at=datetime(2026, 1, 1))
    slack_channel = MagicMock(name="slack-channel")
    channels = NotificationChannelFactory({"slack": lambda cid: slack_channel})
    notifications = UserNotificationService(state_repo=state, channel_factory=channels, coordinator=MagicMock(),
                                            notification_sla={})
    return notifications, channels, slack_channel


def _telegram_kit(media=None):
    channel = MagicMock(name="telegram-channel")
    return PlatformKit(platform="telegram", media=media or Media(), rich_content=MagicMock(name="rich"),
                       channel=lambda cid: channel if cid == "555" else None), channel


async def test_before_the_late_binding_telegram_is_nowhere():
    notifications, channels, _ = _world()

    assert channels.create("telegram", "555") is None
    assert await notifications.notify_file_bytes("user-1", "acct-1", b"x", "list.txt", "List") is False


async def test_the_late_binding_reaches_the_service_and_the_channel_factory_together():
    notifications, channels, slack_channel = _world()
    media = Media()
    kit, telegram_channel = _telegram_kit(media)

    register_late_platform(kit, notifications=notifications, channels=channels, worker=MagicMock(),
                           queued_update=AsyncMock())

    assert channels.create("telegram", "555") is telegram_channel
    assert channels.create("slack", "C1") is slack_channel
    assert notifications._platform_rich_content["telegram"] is kit.rich_content
    assert await notifications.notify_file_bytes("user-1", "acct-1", b"x", "list.txt", "List") is True
    assert media.uploads == [{"file_bytes": b"x", "filename": "list.txt", "title": "List", "channel_id": "555"}]


def _worker_handler(slack_worker=None) -> WorkerHandler:
    return WorkerHandler(agent_worker_handler=MagicMock(), email_indexing_service=MagicMock(),
                         consolidation_service=None, agent_factory=MagicMock(), user_repo=MagicMock(),
                         slack_worker=slack_worker)


async def test_the_late_binding_binds_the_telegram_worker_and_only_then():
    notifications, channels, _ = _world()
    kit, _ = _telegram_kit()
    slack = AsyncMock(return_value=({"ok": True}, 200))
    worker = _worker_handler(slack_worker=slack)
    queued_update = AsyncMock(return_value=({"ok": True}, 200))
    update = {"task_type": "telegram_update", "update": {"update_id": 1}}
    slack_event = {"event": {"type": "message"}, "session_id": "user-1:chan-1"}

    assert await worker.handle(dict(update)) == ({"error": "telegram not configured"}, 503)

    register_late_platform(kit, notifications=notifications, channels=channels, worker=worker,
                           queued_update=queued_update)

    assert await worker.handle(dict(update)) == ({"ok": True}, 200)
    queued_update.assert_awaited_once_with(update)
    assert await worker.handle(dict(slack_event)) == ({"ok": True}, 200)
    slack.assert_awaited_once_with(slack_event, {})


def test_the_constructor_mappings_are_copied():
    media = {"slack": Media()}
    makers = {"slack": lambda cid: None}
    notifications = UserNotificationService(state_repo=MagicMock(), channel_factory=MagicMock(),
                                            coordinator=MagicMock(), notification_sla={}, platform_media=media)
    channels = NotificationChannelFactory(makers)

    media["telegram"] = Media()
    makers["telegram"] = lambda cid: MagicMock()

    assert "telegram" not in notifications._platform_media
    assert channels.create("telegram", "1") is None


def test_the_kit_is_frozen():
    kit, _ = _telegram_kit()

    with pytest.raises(dataclasses.FrozenInstanceError):
        kit.platform = "slack"  # type: ignore[misc]
