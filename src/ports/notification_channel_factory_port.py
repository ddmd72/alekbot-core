"""
NotificationChannelFactoryPort — create a ResponseChannel for background notifications.
The adapter implementation knows about concrete platform adapters (Slack, Telegram).
"""
from abc import ABC, abstractmethod
from typing import Optional

from src.domain.messaging import ResponseChannel


class NotificationChannelFactoryPort(ABC):

    @abstractmethod
    def create(self, platform: str, channel_id: str) -> Optional[ResponseChannel]:
        """
        Create a ResponseChannel for the given platform and channel identifier.
        Returns None if the platform is not configured.
        """
