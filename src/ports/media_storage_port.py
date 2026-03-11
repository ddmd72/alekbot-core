"""
MediaStoragePort — port for storing binary content and returning a public URL.

Used by RichContentService for non-PII public content (HTML weather widgets,
map images) that should be delivered as a shareable link rather than a file attachment.

Implementations:
  GcsMediaAdapter — uploads to a GCS bucket (public object), returns public URL.
"""
from abc import ABC, abstractmethod


class MediaStoragePort(ABC):
    """Store binary content and return a publicly accessible URL."""

    @abstractmethod
    async def store(self, data: bytes, key: str, content_type: str) -> str:
        """
        Upload content to storage and return its public URL.

        Args:
            data:         Raw bytes to store.
            key:          Object key / path within the storage (e.g. "html/uuid-name.html").
            content_type: MIME type (e.g. "text/html; charset=utf-8").

        Returns:
            Public URL string where the content can be accessed.
        """
