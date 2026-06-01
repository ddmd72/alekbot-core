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
from uuid import uuid4

from ..ports.media_storage_port import MediaStoragePort
from .file_link_service import FileLinkService
from ..utils.logger import logger

# storage_class → object-key prefix. email_review is gated + short TTL
# (FileLinkService derives gating/TTL from the prefix).
_PREFIX = {
    "document": "docs",
    "email_review": "email_review",
}


class DocumentDeliveryService:
    """Stores document artifacts privately and returns a capability link."""

    def __init__(self, storage: MediaStoragePort, link_service: FileLinkService) -> None:
        self._storage = storage
        self._links = link_service

    async def store(
        self,
        content: bytes,
        filename: str,
        content_type: str,
        user_id: str,
        storage_class: str = "document",
    ) -> str:
        """
        Upload a document privately and return a capability link.

        Args:
            content:       Raw document bytes.
            filename:      Full filename with extension, e.g. "q1_report.pdf".
            content_type:  MIME type, e.g. "application/pdf".
            user_id:       Owner — embedded in the capability token.
            storage_class: "document" (default) or "email_review" (gated, short TTL).

        Returns:
            A `<base>/f/<token>` capability link.
        """
        prefix = _PREFIX.get(storage_class, "docs")
        key = f"{prefix}/{uuid4()}-{filename}"
        await self._storage.store(data=content, key=key, content_type=content_type)
        link = self._links.build_link(key=key, user_id=user_id)
        logger.info(
            "DocumentDeliveryService: stored '%s' (class=%s) → %s", filename, storage_class, key
        )
        return link
