"""
NotificationChannelFactory — creates ResponseChannels for background notifications.
Knows about concrete platform adapters (Slack, Telegram) — this is its sole responsibility.
"""
from typing import Optional

from ..adapters.slack.response_channel import SlackResponseChannel
from ..adapters.telegram.response_channel import TelegramResponseChannel
from ..domain.messaging import ResponseChannel
from ..ports.notification_channel_factory_port import NotificationChannelFactoryPort
from ..utils.logger import logger


class NotificationChannelFactory(NotificationChannelFactoryPort):
    """
    Creates ResponseChannels from stored (platform, channel_id) pairs.
    Adapters are injected after creation via set_* methods (Telegram may not be available at startup).
    """

    def __init__(self):
        self._slack_adapter = None
        self._telegram_adapter = None

    def set_slack_adapter(self, adapter) -> None:
        self._slack_adapter = adapter

    def set_telegram_adapter(self, adapter) -> None:
        self._telegram_adapter = adapter

    def create(self, platform: str, channel_id: str) -> Optional[ResponseChannel]:
        if platform == "slack":
            if not self._slack_adapter:
                logger.warning("[NotificationFactory] Slack adapter not set")
                return None
            return SlackResponseChannel(
                app_client=self._slack_adapter.app.client,
                channel_id=channel_id,
                bot_token=self._slack_adapter.slack_bot_token,
            )

        if platform == "telegram":
            if not self._telegram_adapter:
                logger.warning("[NotificationFactory] Telegram adapter not set")
                return None
            try:
                return TelegramResponseChannel(
                    bot=self._telegram_adapter.bot,
                    chat_id=int(channel_id),
                )
            except (ValueError, TypeError) as e:
                logger.error(f"[NotificationFactory] Invalid Telegram chat_id '{channel_id}': {e}")
                return None

        logger.warning(f"[NotificationFactory] Unknown platform: {platform}")
        return None
