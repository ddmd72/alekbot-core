"""
ChannelBindingPort — store and retrieve channel-to-agent bindings.

Used by ConversationHandler to determine whether a channel has a direct
agent routing override (bypass Router).
"""

from abc import ABC, abstractmethod
from typing import Optional

from src.domain.channel_binding import ChannelBinding


class ChannelBindingPort(ABC):
    """Channel -> agent routing overrides, one binding per channel id.

    Tenancy: keyed by channel id only; no user scoping (`created_by` is recorded, never checked). Nothing lists or
    purges bindings: the binding of an archived channel stays until `delete`. Backend errors propagate.
    """

    @abstractmethod
    async def get(self, channel_id: str) -> Optional[ChannelBinding]:
        """The channel's binding, or None when not bound.

        companion_config: None when absent; within it a missing optional field takes the CompanionConfig default, a
        missing window_threshold or batch_size raises KeyError, an unknown text_mode or session domain raises
        ValueError. A missing top-level field raises KeyError.
        """

    @abstractmethod
    async def save(self, binding: ChannelBinding) -> None:
        """Create or replace the channel's binding (the whole record).

        Idempotency: a repeat stores the same binding.
        """

    @abstractmethod
    async def delete(self, channel_id: str) -> None:
        """Remove the channel's binding; a missing one is a no-op."""
