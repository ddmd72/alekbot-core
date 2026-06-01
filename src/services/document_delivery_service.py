"""
DocumentDeliveryService — uploads generated documents to private cloud storage
and returns a user-facing capability link.

Provides a uniform interface for storing document artifacts (PDF, HTML, DOCX)
to a PRIVATE GCS bucket and turning the stored key into a `/f/<token>` capability
link via FileLinkService — handlers never touch storage keys or tokens.

Used by ConversationHandler and AgentWorkerHandler when processing
DeliveryItem(type="document") — the unified document delivery type.
See docs/10_rfcs/DOCUMENT_DELIVERY_RFC.md.
"""
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from ..ports.media_storage_port import MediaStoragePort
from ..utils.logger import logger

if TYPE_CHECKING:
    # Type-only import (REQ-ARCH-22: services must not import services at runtime).
    # The concrete instance is injected via the constructor.
    from .file_link_service import FileLinkService

# storage_class → object-key prefix. email_review is gated + short TTL
# (FileLinkService derives gating/TTL from the prefix).
_PREFIX = {
    "document": "docs",
    "email_review": "email_review",
}


@dataclass(frozen=True)
class DeliveredDocument:
    """Result of storing a document.

    link — user-facing capability link (`/f/<token>`), sent to the channel.
    key  — internal object key, written to conversation history so an agent can
           re-read the document later via open_file (server-side, no TTL).
    """
    link: str
    key: str


class DocumentDeliveryService:
    """Stores document artifacts privately and returns a capability link."""

    def __init__(self, storage: MediaStoragePort, link_service: "FileLinkService") -> None:
        self._storage = storage
        self._links = link_service

    async def store(
        self,
        content: bytes,
        filename: str,
        content_type: str,
        user_id: str,
        storage_class: str = "document",
    ) -> DeliveredDocument:
        """
        Upload a document privately and return its link + internal key.

        Args:
            content:       Raw document bytes.
            filename:      Full filename with extension, e.g. "q1_report.pdf".
            content_type:  MIME type, e.g. "application/pdf".
            user_id:       Owner — embedded in the capability token + object key.
            storage_class: "document" (default) or "email_review" (gated, short TTL).

        Returns:
            DeliveredDocument(link, key) — link for the channel, key for history.
        """
        # Key always carries user_id so the open_file resolver can verify ownership
        # before serving a delivered document: {prefix}/{user_id}/{uuid}-{filename}.
        prefix = _PREFIX.get(storage_class, "docs")
        key = f"{prefix}/{user_id}/{uuid4()}-{filename}"
        await self._storage.store(data=content, key=key, content_type=content_type)
        link = self._links.build_link(key=key, user_id=user_id)
        logger.info(
            "DocumentDeliveryService: stored '%s' (class=%s) → %s", filename, storage_class, key
        )
        return DeliveredDocument(link=link, key=key)
