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

    @abstractmethod
    async def save_primary(self, user_id: str, platform: str, channel_id: str) -> None:
        """Set the primary notification channel for a user. Explicit via $primary command."""

    @abstractmethod
    async def get_primary(self, user_id: str) -> Optional[NotificationChannel]:
        """Return primary channel for user, or None if not set (fallback to last active)."""
