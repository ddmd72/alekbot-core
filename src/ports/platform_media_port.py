"""
PlatformMediaPort — abstract interface for uploading media to the user's messaging platform.

Separates "where to fetch/generate content" (application layer) from
"how to deliver it" (adapter layer: Slack, Telegram, etc.).

Implementations:
  SlackMediaAdapter   — files_upload_v2
  TelegramMediaAdapter — sendPhoto / sendDocument (future)
"""
from abc import ABC, abstractmethod


class PlatformMediaPort(ABC):
    """Delivers binary media (images, files) to the messaging platform."""

    @abstractmethod
    async def upload_image(
        self,
        image_bytes: bytes,
        alt_text: str,
        channel_id: str,
    ) -> None:
        """
        Upload an image and post it to the channel.

        Args:
            image_bytes: Raw image bytes (PNG, JPEG, etc.)
            alt_text:    Accessible description / notification text
            channel_id:  Platform-specific channel identifier
        """
        ...

    @abstractmethod
    async def upload_file(
        self,
        file_bytes: bytes,
        filename: str,
        title: str,
        channel_id: str,
    ) -> None:
        """
        Upload a file and post it to the channel.

        Args:
            file_bytes: Raw file content
            filename:   Filename with extension (e.g. "summary-2026-02-25.md")
            title:      Human-readable title shown in the platform UI
            channel_id: Platform-specific channel identifier
        """
        ...
