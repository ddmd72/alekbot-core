"""
Base Slack Adapter Interface
Defines the contract for both Socket Mode and HTTP Mode adapters
"""
from abc import abstractmethod
from typing import Optional
from slack_bolt.async_app import AsyncApp

from ..platform.base_adapter import PlatformAdapter


class SlackAdapter(PlatformAdapter):
    """
    Abstract base class for Slack adapters.
    Implements Hexagonal Architecture - this is a Driving Adapter port.
    
    Updated (2026-02-09): Inherits from PlatformAdapter for multi-platform support.
    """

    def __init__(
        self,
        app: AsyncApp,
        config: dict,
        coordinator=None,
        agent_factory=None,
        iam_service=None,
        file_service=None,
        consolidation_queue=None,
        consolidation_config=None,
        **kwargs
    ):
        """
        Initialize the Slack adapter.

        Args:
            app: Slack Bolt AsyncApp instance
            config: Configuration dictionary
            coordinator: AgentCoordinator instance
            agent_factory: UserAgentFactory instance
            iam_service: IAMService instance
            file_service: FileService instance
            consolidation_queue: ConsolidationQueue instance
            consolidation_config: Consolidation configuration dict
            **kwargs: Additional arguments for PlatformAdapter
        """
        super().__init__(
            coordinator=coordinator,
            agent_factory=agent_factory,
            iam_service=iam_service,
            file_service=file_service,
            consolidation_queue=consolidation_queue,
            consolidation_config=consolidation_config,
            **kwargs
        )
        self.app = app
        self.config = config

    def get_platform_name(self) -> str:
        """Return platform name."""
        return "slack"

    @abstractmethod
    def register_handlers(self) -> None:
        """
        Register Slack event handlers (messages, mentions, etc.).
        Implementation differs between Socket Mode and HTTP Mode.
        """
        pass

    def get_mode_name(self) -> str:
        """
        Return the name of this adapter mode.
        """
        return self.__class__.__name__

    async def _translate_platform_files(self, platform_files: list) -> list:
        """
        Translate Slack files to FileAttachment DTOs.
        
        Slack files have direct URL access (no API call needed like Telegram).
        
        Args:
            platform_files: List of Slack file objects
            
        Returns:
            List of FileAttachment DTOs
        """
        from ...domain.messaging import FileAttachment
        
        attachments = []
        for file_obj in platform_files:
            try:
                # Slack files have direct URLs
                attachments.append(FileAttachment(
                    url=file_obj.get("url_private", ""),
                    mime_type=file_obj.get("mimetype", "application/octet-stream"),
                    filename=file_obj.get("name", "unknown"),
                    size_bytes=file_obj.get("size")
                ))
            except Exception as e:
                from ...utils.logger import logger
                logger.warning(f"⚠️ Failed to translate Slack file: {e}")
                continue
        
        return attachments
