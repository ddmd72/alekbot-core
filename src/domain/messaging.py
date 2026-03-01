"""
Platform-agnostic messaging domain entities.
These DTOs allow adapters to translate platform-specific events into a common format.
"""
from dataclasses import dataclass, field
from typing import List, Optional, Any, Protocol, Dict
from abc import abstractmethod
from .ui_messages import StatusType


@dataclass
class FileAttachment:
    """Platform-agnostic file attachment representation."""
    url: str
    mime_type: str
    filename: str
    size_bytes: Optional[int] = None


@dataclass
class MessageContext:
    """
    Platform-agnostic message context.
    Driving adapters (Slack, Telegram, Web) translate their events into this DTO.

    SESSION_26: Added account_id for multi-tenant billing and prompt hierarchy.
    """
    text: str
    session_id: str
    user_id: str
    account_id: str  # SESSION_26: Required for 4-level prompt resolution
    attachments: List[FileAttachment] = field(default_factory=list)
    thread_id: Optional[str] = None
    metadata: dict = field(default_factory=dict)  # Platform-specific extras


@dataclass
class RichContent:
    """
    Platform-agnostic container for structured (rich) responses.

    Adapters decide how to render specific content types.
    Always include a fallback_text for platforms without rich support.
    """
    content_type: str
    data: Dict[str, Any]
    fallback_text: str


@dataclass
class SmartResponse:
    """
    Smart Pass response payload.

    If structured_data is present, ConversationHandler may route via
    ResponseChannel.send_rich_content().
    """
    text: str
    structured_data: Optional[RichContent] = None


class ResponseChannel(Protocol):
    """
    Interface for sending responses back to the user.
    Each adapter implements this protocol for their specific platform.

    This abstraction allows ConversationHandler to be completely platform-agnostic.
    """

    platform: str       # "slack" | "telegram" — used by UserNotificationService
    channel_id: str     # platform channel identifier — used by UserNotificationService

    @property
    @abstractmethod
    def max_message_length(self) -> int:
        """Return platform's max message length."""
        ...
    
    @property
    @abstractmethod
    def supports_message_editing(self) -> bool:
        """Return whether platform supports editing messages."""
        ...
    
    @abstractmethod
    async def send_message(self, text: str, thread_id: Optional[str] = None) -> Any:
        """
        Send a text message to the user.
        
        Args:
            text: Message content
            thread_id: Optional thread/conversation identifier
            
        Returns:
            Platform-specific message object
        """
        ...
    
    @abstractmethod
    async def update_message(self, message_id: str, text: str) -> None:
        """
        Update an existing message (for streaming/status updates).
        
        Args:
            message_id: Platform-specific message identifier
            text: New message content
        """
        ...

    @abstractmethod
    async def send_chunked_message(self, text: str, message_id: str, thread_id: Optional[str] = None) -> None:
        """
        Send a long message by updating the first message and posting the rest as thread replies.

        Args:
            text: Full message content
            message_id: Message to update with the first chunk
            thread_id: Optional thread identifier
        """
        ...

    @abstractmethod
    async def send_rich_content(self, content: RichContent, thread_id: Optional[str] = None) -> Any:
        """
        Send structured rich content to the user.

        Adapters must render if supported, otherwise fallback to content.fallback_text.

        Args:
            content: Structured payload + fallback text
            thread_id: Optional thread/conversation identifier

        Returns:
            Platform-specific message object
        """
        ...

    @abstractmethod
    async def send_status(self, status_type: StatusType, thread_id: Optional[str] = None) -> str:
        """
        Send a status/thinking message using semantic status type.
        
        Args:
            status_type: Semantic status type (THINKING, SEARCHING_MEMORY, etc.)
            thread_id: Optional thread identifier
            
        Returns:
            Message ID for future updates
        """
        ...
    
    @abstractmethod
    async def send_status_with_phrase(self, status_type: StatusType, thread_id: Optional[str] = None) -> tuple[str, str]:
        """
        Send a status message and return both message ID and the chosen phrase.
        
        Args:
            status_type: Semantic status type
            thread_id: Optional thread identifier
            
        Returns:
            Tuple of (message_id, phrase)
        """
        ...
    
    @abstractmethod
    async def get_status_phrase(self, status_type: StatusType) -> str:
        """
        Get a localized phrase for a status type.
        
        Args:
            status_type: Semantic status type
            
        Returns:
            Localized phrase (without emoji or dots)
        """
        ...

    @abstractmethod
    async def get_entertainment_intro(self) -> str:
        """
        Get a localized intro phrase for entertainment messages.

        Returns:
            Localized intro phrase
        """
        ...

    @abstractmethod
    async def send_entertainment_message(self, text: str, thread_id: Optional[str] = None) -> Any:
        """
        Send a dedicated entertainment message (for web search).

        Args:
            text: Entertainment message body
            thread_id: Optional thread identifier

        Returns:
            Platform-specific message object
        """
        ...
    
    @abstractmethod
    async def update_status_with_phrase_and_dots(self, message_id: str, phrase: str, dots_count: int) -> None:
        """
        Update status message with fixed phrase and animated dots.
        
        Args:
            message_id: Platform-specific message identifier
            phrase: Fixed phrase (without dots)
            dots_count: Number of dots to display (1-5)
        """
        ...
    
    @abstractmethod
    async def update_status(self, message_id: str, status_type: StatusType) -> None:
        """
        Update an existing status message with new status type.
        
        Args:
            message_id: Platform-specific message identifier
            status_type: New semantic status type
        """
        ...
    
    @abstractmethod
    async def update_status_with_dots(self, message_id: str, status_type: StatusType, dots_count: int) -> None:
        """
        Update an existing status message with animated dots.
        
        Args:
            message_id: Platform-specific message identifier
            status_type: Semantic status type
            dots_count: Number of dots to display (1-5)
        """
        ...
    
    @abstractmethod
    async def download_file(self, url: str, mime_type: str) -> Optional[str]:
        """
        Download a file attachment.
        
        Args:
            url: File URL
            mime_type: File MIME type
            
        Returns:
            Local file path or None if download failed
        """
        ...
