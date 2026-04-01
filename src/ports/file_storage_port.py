"""
FileStoragePort — store and retrieve user file attachments.

Implementations:
  GcsFileStorageAdapter — uploads to a GCS bucket under {user_id}/files/ prefix.

Distinction from MediaStoragePort: MediaStoragePort stores public non-PII content
(HTML pages, widgets) with public URLs. FileStoragePort stores private user file
attachments with TTL, download, and duplicate name resolution.
"""
from abc import ABC, abstractmethod


class FileStoragePort(ABC):
    """Store and retrieve user file attachments by filename."""

    @abstractmethod
    async def upload(self, data: bytes, filename: str, user_id: str, content_type: str) -> str:
        """
        Upload file to storage with Finder-style duplicate name resolution.

        Args:
            data:         Raw file bytes.
            filename:     Original filename (e.g. "report.docx"). Sanitized internally.
            user_id:      Owner. Used to build storage key: {user_id}/files/{filename}.
            content_type: MIME type.

        Returns:
            The deduplicated filename (e.g. "report (1).docx" if name was taken).
        """

    @abstractmethod
    async def download(self, filename: str, user_id: str) -> bytes:
        """
        Download file from storage.

        Args:
            filename: Deduplicated filename as returned by upload().
            user_id:  Owner.

        Returns:
            Raw file bytes.
        """

    @abstractmethod
    async def delete(self, filename: str, user_id: str) -> None:
        """Delete a file from storage."""

    @abstractmethod
    async def exists(self, filename: str, user_id: str) -> bool:
        """Check if a file exists (used for duplicate name resolution)."""

    @abstractmethod
    async def get_url(self, filename: str, user_id: str) -> str:
        """Assemble a full URL from filename + user_id. For external sharing only."""
