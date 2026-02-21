"""
Base adapter for all messaging platforms.
Implements Hexagonal Architecture - Driving Adapter port.
"""
from abc import ABC, abstractmethod
from typing import Optional, List
from ...domain.messaging import FileAttachment
from ...ports.conversation_handler_port import ConversationHandlerPort
from ...ports.platform_auth_port import PlatformAuthPort
from ...ports.audio_transcription_port import AudioTranscriptionPort
from ...utils.circuit_breaker import CircuitBreaker, CircuitBreakerConfig


class PlatformAdapter(ABC):
    """
    Abstract base class for all platform adapters (Slack, Telegram, etc.).

    Each platform implements:
    - start/stop lifecycle
    - event handling
    - file translation
    """

    def __init__(
        self,
        conversation_handler: ConversationHandlerPort,
        iam_service: PlatformAuthPort,
        audio_service: Optional[AudioTranscriptionPort] = None,
    ):
        """
        Initialize platform adapter.

        Args:
            conversation_handler: ConversationHandlerPort for processing messages
            iam_service: PlatformAuthPort for authorization
            audio_service: Optional audio transcription port (mp3/wav → text)
        """
        self.conversation_handler = conversation_handler
        self.iam_service = iam_service
        self.audio_service = audio_service

        # Circuit breaker for platform API calls
        self.circuit_breaker = CircuitBreaker(
            CircuitBreakerConfig(
                failure_threshold=5,
                recovery_timeout=60.0
            )
        )

    @abstractmethod
    async def start(self) -> None:
        """Start the adapter (connect, start server, etc.)."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully stop the adapter."""
        pass

    @abstractmethod
    async def _translate_platform_files(self, platform_files: list) -> List[FileAttachment]:
        """
        Translate platform-specific file objects to FileAttachment DTOs.

        Args:
            platform_files: Platform-specific file objects

        Returns:
            List of FileAttachment DTOs
        """
        pass

    @abstractmethod
    def get_platform_name(self) -> str:
        """Return platform name (slack, telegram, etc.)."""
        pass
