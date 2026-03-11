"""
NotificationStatePort — store and retrieve the last active messaging channel per user.
Used by UserNotificationService to send background notifications.
"""
from abc import ABC, abstractmethod
from typing import Optional

from src.domain.notification import NotificationChannel


class NotificationStatePort(ABC):

    @abstractmethod
    async def save(self, user_id: str, platform: str, channel_id: str) -> None:
        """Upsert the last active channel for a user. Called on every incoming message."""

    @abstractmethod
    async def get(self, user_id: str) -> Optional[NotificationChannel]:
        """Return last active channel for user, or None if never recorded."""
