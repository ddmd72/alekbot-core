"""
Platform adapter factory.
Registry pattern for multi-platform support.
"""
from typing import Dict, Type, Optional
from ...ports.platform_port import PlatformPort
from ...utils.logger import logger


class PlatformAdapterFactory:
    """
    Registry and factory for platform adapters.

    Usage:
        # Register platforms
        PlatformAdapterFactory.register("slack", SlackHTTPAdapter)
        PlatformAdapterFactory.register("telegram", TelegramWebhookAdapter)

        # Create adapter
        adapter = PlatformAdapterFactory.create("telegram", **config)
    """

    _adapters: Dict[str, Type[PlatformPort]] = {}

    @classmethod
    def register(cls, platform: str, adapter_class: Type[PlatformPort]):
        """
        Register a platform adapter.

        Args:
            platform: Platform name (slack, telegram, etc.)
            adapter_class: Adapter class (must implement PlatformPort)
        """
        cls._adapters[platform] = adapter_class
        logger.info(f"✅ Registered platform adapter: {platform}")

    @classmethod
    def create(cls, platform: str, **kwargs) -> Optional[PlatformPort]:
        """
        Create adapter instance for platform.

        Args:
            platform: Platform name (slack, telegram)
            **kwargs: Adapter-specific configuration

        Returns:
            PlatformAdapter instance or None if not registered
        """
        adapter_class = cls._adapters.get(platform)
        if not adapter_class:
            logger.error(f"❌ Unknown platform: {platform}")
            return None

        return adapter_class(**kwargs)

    @classmethod
    def list_platforms(cls) -> list:
        """List all registered platforms."""
        return list(cls._adapters.keys())
