"""
CompanionWindowResolver — per-session override for FirestoreSessionStore's
sliding-window threshold/batch_size, sourced from ChannelBinding.companion_config
(RFC docs/10_rfcs/COMPANION_AGENTS_RFC.md §5: window threshold is per-channel
policy, not a store-wide constant).

session_id is "<prefix>:<channel_id>" for both Alek ("user_id:channel_id")
and companion ("platform:channel_id") sessions — this resolver does not
need to tell them apart. It always extracts channel_id and asks
ChannelBindingService; an unbound (or bound-without-companion_config)
channel returns None, so FirestoreSessionStore falls back to its
constructor defaults (Task 8).

Not yet reachable in production: bound channels are stateless today (no
SessionStore writes at all) until Phase F flips SessionMode for
companion_config-bearing bindings.
"""
from typing import Optional, Tuple

from .channel_binding_service import ChannelBindingService
from ..utils.logger import logger


class CompanionWindowResolver:

    def __init__(self, channel_binding_service: ChannelBindingService) -> None:
        self._bindings = channel_binding_service

    async def resolve(self, session_id: str) -> Optional[Tuple[int, int]]:
        if ":" not in session_id:
            return None
        channel_id = session_id.split(":", 1)[1]
        try:
            binding = await self._bindings.get(channel_id)
        except Exception as exc:
            logger.warning(
                "⚠️ [CompanionWindowResolver] binding lookup failed for %s: %s",
                channel_id[:12], exc,
            )
            return None
        if binding is None or binding.companion_config is None:
            return None
        return (binding.companion_config.window_threshold, binding.companion_config.batch_size)
