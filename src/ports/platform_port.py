"""
PlatformPort — abstract interface for messaging platform adapters.

Defines the contract that all platform adapters (Slack, Telegram, etc.) must satisfy.
Concrete implementation details (constructor injection, CircuitBreaker setup) belong
in the adapter layer, not here.

Moved from src/adapters/platform/base_adapter.py (TD-V4, 2026-03-08).
"""
from abc import ABC, abstractmethod
from typing import List

from src.domain.messaging import FileAttachment


class PlatformPort(ABC):
    """Abstract interface for messaging platform adapters."""

    @abstractmethod
    async def start(self) -> None:
        """Start the adapter (connect, start server, etc.)."""

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully stop the adapter."""

    @abstractmethod
    async def _translate_platform_files(self, platform_files: list) -> List[FileAttachment]:
        """Translate platform-specific file objects to FileAttachment DTOs."""

    @abstractmethod
    def get_platform_name(self) -> str:
        """Return platform identifier (slack, telegram, etc.)."""
