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

    async def get(self, channel_id: str) -> Optional[ChannelBinding]:
        """Return binding for channel. Cache miss → Firestore lookup."""
        cached = self._cache.get(channel_id)
        if cached:
            binding, ts = cached
            if time.time() - ts < self._CACHE_TTL:
                return binding

        binding = await self._port.get(channel_id)
        self._cache[channel_id] = (binding, time.time())
        return binding

    async def bind(self, binding: ChannelBinding) -> None:
        """Create or overwrite a channel binding."""
        await self._port.save(binding)
        self._cache[binding.channel_id] = (binding, time.time())
        logger.info(
            "🔗 Channel %s bound to agent_type=%s (by %s)",
            binding.channel_id, binding.agent_type, binding.created_by[:8],
        )

    async def unbind(self, channel_id: str) -> None:
        """Remove a channel binding."""
        await self._port.delete(channel_id)
        self._cache[channel_id] = (None, time.time())
        logger.info("🔓 Channel %s unbound", channel_id)

    def invalidate(self, channel_id: str) -> None:
        """Remove a single entry from cache."""
        self._cache.pop(channel_id, None)
