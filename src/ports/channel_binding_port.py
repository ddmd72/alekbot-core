"""
ChannelBindingPort — store and retrieve channel-to-agent bindings.

Used by ConversationHandler to determine whether a channel has a direct
agent routing override (bypass Router).
"""

from abc import ABC, abstractmethod
from typing import Optional

from src.domain.channel_binding import ChannelBinding


class ChannelBindingPort(ABC):

    @abstractmethod
    async def get(self, channel_id: str) -> Optional[ChannelBinding]:
        """Return binding for channel, or None if not bound."""

    @abstractmethod
    async def save(self, binding: ChannelBinding) -> None:
        """Create or overwrite a channel binding."""

    @abstractmethod
    async def delete(self, channel_id: str) -> None:
        """Remove a channel binding. No-op if not found."""
