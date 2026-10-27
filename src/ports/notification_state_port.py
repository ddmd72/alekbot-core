"""
NotificationStatePort — store and retrieve the last active messaging channel per user.
Used by UserNotificationService to send background notifications.
"""
from abc import ABC, abstractmethod
from typing import Optional

from src.domain.notification import NotificationChannel


class NotificationStatePort(ABC):
    """Port for a user's notification channels: the last active one and the primary one ($primary), one record each
    per user_id.

    Holds for every method:
    - Tenancy: user_id only.
    - Atomicity: each save replaces the record; last writer wins (the latest message wins).
    - Dates: a stored updated_at reads as naive UTC; a record without one reads as now (aware UTC). No caller reads it.
    - Legacy: a record missing user_id, platform or channel_id, or with an updated_at that is not a timestamp: no
      writer produces one; behaviour pending the data audit.
    - Errors: backend errors propagate.
    """

    @abstractmethod
    async def save(self, user_id: str, platform: str, channel_id: str) -> None:
        """Replace the user's last active channel. Called on every incoming message."""

    @abstractmethod
    async def get(self, user_id: str) -> Optional[NotificationChannel]:
        """Return last active channel for user, or None if never recorded."""

    @abstractmethod
    async def save_primary(self, user_id: str, platform: str, channel_id: str) -> None:
        """Replace the user's primary channel, kept apart from the last active one. Explicit via $primary command."""

    @abstractmethod
    async def get_primary(self, user_id: str) -> Optional[NotificationChannel]:
        """Return primary channel for user, or None if not set (fallback to last active)."""
