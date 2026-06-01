"""
MediaStoragePort — port for storing binary content in private object storage.

Objects are stored PRIVATELY (no public ACL). Two access paths:
  - Users open files via a capability link built by FileLinkService (token →
    /f/<token> route → short-lived signed URL). The adapter never returns a
    user-facing URL — it returns the storage key, and the service layer mints
    the link (the adapter must not depend on services/ per REQ-ARCH).
  - Agents re-read delivered files server-side via fetch(key) — no external HTTP,
    no dependency on the bucket being public.

Implementations:
  GcsMediaAdapter — uploads to a private GCS bucket; fetch() reads via the SDK.
"""
from abc import ABC, abstractmethod


class MediaStoragePort(ABC):
    """Store binary content privately, addressable by object key."""

    @abstractmethod
    async def store(self, data: bytes, key: str, content_type: str) -> str:
        """
        Upload content to private storage.

        Args:
            data:         Raw bytes to store.
            key:          Object key / path within the storage (e.g. "html/uuid-name.html").
            content_type: MIME type (e.g. "text/html; charset=utf-8").

        Returns:
            The stored object key (NOT a URL). The service layer turns this into a
            capability link via FileLinkService.
        """

    @abstractmethod
    async def fetch(self, key: str) -> bytes:
        """
        Read a stored object's bytes server-side (service-account credentials).

        Used by agents to re-read a previously delivered file without going
        through an external URL fetch. Raises on missing object.

        Args:
            key: Object key as returned by store().

        Returns:
            Raw object bytes.
        """
