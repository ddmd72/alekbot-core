"""What the notification path needs of one chat platform: its media port, its rich-content delivery and the maker of a
response channel for a stored channel id. Slack's kit is built before the notification service and handed to it and to
the channel factory at construction; Telegram's is completed only after its adapter and bound late (`register_late_platform`).
"""
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Tuple

from ..adapters.notification_channel_factory import NotificationChannelFactory
from ..domain.messaging import ResponseChannel
from ..handlers.worker_handler import WorkerHandler
from ..ports.platform_media_port import PlatformMediaPort
from ..services.rich_content_service import RichContentService
from ..services.user_notification_service import UserNotificationService


@dataclass(frozen=True)
class PlatformKit:
    """One platform's outbound parts, shared by its chat handler (`rich_content`) and the notification path."""
    platform: str
    media: PlatformMediaPort
    rich_content: RichContentService
    channel: Callable[[str], Optional[ResponseChannel]]


def register_late_platform(kit: PlatformKit, *, notifications: UserNotificationService,
                           channels: NotificationChannelFactory, worker: WorkerHandler,
                           queued_update: Callable[[dict], Awaitable[Tuple[dict, int]]]) -> None:
    """The one late binding of a platform, used for Telegram only: its channel maker, its media and rich content, and
    its queued-update entry on the `/worker` handler. It cannot be a constructor argument: the Telegram adapter's chat
    handler and the agent factory take the notification service, so the service exists before the adapter, and the
    channel maker needs the adapter's own bot. Telegram is all or nothing (owner ruling 2026-11-17): the graph calls
    this only after the adapter and its whole chain were built, so a failed build registers nothing."""
    channels.register_platform(kit.platform, kit.channel)
    notifications.register_platform(kit.platform, media=kit.media, rich_content=kit.rich_content)
    worker.register_telegram_worker(queued_update)
