"""
Base adapter for all messaging platforms.
Implements Hexagonal Architecture - Driving Adapter port.
"""
from abc import ABC, abstractmethod
from typing import Optional, List, Any, TYPE_CHECKING
from ...domain.messaging import FileAttachment
from ...handlers.conversation_handler import ConversationHandler
from ...infrastructure.agent_coordinator import AgentCoordinator
from ...services.user_agent_factory import UserAgentFactory
from ...services.iam_service import IAMService
from ...ports.file_service import FileService
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
        coordinator: AgentCoordinator,
        agent_factory: UserAgentFactory,
        iam_service: IAMService,
        file_service: FileService,
        consolidation_queue: Optional[Any] = None,
        consolidation_config: Optional[Any] = None,
        audio_service: Optional[AudioTranscriptionPort] = None,
    ):
        """
        Initialize platform adapter.

        Args:
            coordinator: AgentCoordinator instance
            agent_factory: UserAgentFactory instance
            iam_service: IAMService instance
            file_service: FileService instance
            consolidation_queue: Optional consolidation queue port
            consolidation_config: Optional consolidation config
            audio_service: Optional audio transcription port (mp3/wav → text)
        """
        self.coordinator = coordinator
        self.agent_factory = agent_factory
        self.iam_service = iam_service
        self.file_service = file_service
        self.consolidation_queue = consolidation_queue
        self.consolidation_config = consolidation_config
        self.audio_service = audio_service

        # Shared ConversationHandler — stateless, safe to reuse across requests
        self.conversation_handler = ConversationHandler(
            coordinator=coordinator,
            agent_factory=agent_factory,
            file_service=file_service,
            consolidation_queue=consolidation_queue,
            global_config=consolidation_config,
            audio_service=audio_service,
        )

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
