"""
NotificationChannelFactory — creates ResponseChannels for background notifications.

Platform-specific channel creation is delegated to registered factory callables,
injected from the composition layer (main.py). This adapter has no knowledge of
concrete platform implementations.
"""
from typing import Callable, Dict, Optional

from ..domain.messaging import ResponseChannel
from ..ports.notification_channel_factory_port import NotificationChannelFactoryPort
from ..utils.logger import logger


class NotificationChannelFactory(NotificationChannelFactoryPort):
    """
    Creates ResponseChannels from stored (platform, channel_id) pairs.

    Platform factory callables are registered via register_factory() from the
    composition layer. The factory itself never imports concrete platform adapters.
    """

    def __init__(self):
        self._factories: Dict[str, Callable[[str], Optional[ResponseChannel]]] = {}

    def register_factory(
        self,
        platform: str,
        factory: Callable[[str], Optional[ResponseChannel]],
    ) -> None:
        """Register a channel factory callable for a platform."""
        self._factories[platform] = factory

    def create(self, platform: str, channel_id: str) -> Optional[ResponseChannel]:
        factory = self._factories.get(platform)
        if not factory:
            logger.warning(f"[NotificationFactory] No factory registered for platform: {platform}")
            return None
        return factory(channel_id)
