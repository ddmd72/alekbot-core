"""
ChannelBindingService — in-memory cached facade over ChannelBindingPort.

Provides fast lookup for the hot path (every incoming message checks binding)
with TTL-based cache invalidation.
"""

import time
from typing import Dict, Optional, Tuple

from ..domain.channel_binding import ChannelBinding
from ..ports.channel_binding_port import ChannelBindingPort
from ..utils.logger import logger


class ChannelBindingService:

    _CACHE_TTL = 300  # 5 minutes

    def __init__(self, port: ChannelBindingPort) -> None:
        self._port = port
        self._cache: Dict[str, Tuple[Optional[ChannelBinding], float]] = {}
        # Per channel, advanced by every bind, unbind and invalidate. A cache-miss read writes back only if none ran
        # while it awaited the port: a value read before a bind and written after it would undo the bind for the TTL.
        self._versions: Dict[str, int] = {}

    async def get(self, channel_id: str) -> Optional[ChannelBinding]:
        """Return binding for channel. Cache miss → Firestore lookup."""
        cached = self._cache.get(channel_id)
        if cached:
            binding, ts = cached
            if time.time() - ts < self._CACHE_TTL:
                return binding

        version = self._versions.get(channel_id, 0)
        binding = await self._port.get(channel_id)
        if self._versions.get(channel_id, 0) == version:
            self._cache[channel_id] = (binding, time.time())
        return binding

    def _set(self, channel_id: str, binding: Optional[ChannelBinding]) -> None:
        self._versions[channel_id] = self._versions.get(channel_id, 0) + 1
        self._cache[channel_id] = (binding, time.time())

    async def bind(self, binding: ChannelBinding) -> None:
        """Create or overwrite a channel binding."""
        await self._port.save(binding)
        self._set(binding.channel_id, binding)
        logger.info(
            "🔗 Channel %s bound to agent_type=%s (by %s)",
            binding.channel_id, binding.agent_type, binding.created_by[:8],
        )

    async def unbind(self, channel_id: str) -> None:
        """Remove a channel binding."""
        await self._port.delete(channel_id)
        self._set(channel_id, None)
        logger.info("🔓 Channel %s unbound", channel_id)

    def invalidate(self, channel_id: str) -> None:
        """Remove a single entry from cache."""
        self._versions[channel_id] = self._versions.get(channel_id, 0) + 1
        self._cache.pop(channel_id, None)
