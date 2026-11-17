"""
NotificationChannelFactory — creates ResponseChannels for background notifications.

Platform-specific channel creation is delegated to the channel makers the composition layer
(build_application_graph) hands in at construction. This adapter has no knowledge of
concrete platform implementations.
"""
from typing import Callable, Dict, Mapping, Optional

from ..domain.messaging import ResponseChannel
from ..ports.notification_channel_factory_port import NotificationChannelFactoryPort
from ..utils.logger import logger

ChannelMaker = Callable[[str], Optional[ResponseChannel]]


class NotificationChannelFactory(NotificationChannelFactoryPort):
    """
    Creates ResponseChannels from stored (platform, channel_id) pairs.

    The factory itself never imports concrete platform adapters.
    """

    def __init__(self, factories: Mapping[str, ChannelMaker]):
        self._factories: Dict[str, ChannelMaker] = dict(factories)

    def register_platform(self, platform: str, factory: ChannelMaker) -> None:
        """Add a platform built after this factory; only `register_late_platform` (composition) calls it."""
        self._factories[platform] = factory

    def create(self, platform: str, channel_id: str) -> Optional[ResponseChannel]:
        factory = self._factories.get(platform)
        if not factory:
            logger.warning(f"[NotificationFactory] No factory registered for platform: {platform}")
            return None
        return factory(channel_id)
