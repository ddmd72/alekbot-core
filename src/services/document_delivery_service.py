"""
DocumentDeliveryService — uploads generated documents to cloud storage.

Provides a uniform interface for storing document artifacts (PDF, HTML, DOCX)
to GCS and returning their public URLs.

Used by ConversationHandler and AgentWorkerHandler when processing
DeliveryItem(type="document") — the unified document delivery type.
See docs/10_rfcs/DOCUMENT_DELIVERY_RFC.md.
"""
from uuid import uuid4

from ..ports.media_storage_port import MediaStoragePort
from ..utils.logger import logger


class DocumentDeliveryService:
    """Stores binary document artifacts to cloud storage and returns public URLs."""

    def __init__(self, storage: MediaStoragePort) -> None:
        self._storage = storage

    async def store(self, content: bytes, filename: str, content_type: str) -> str:
        """
        Upload document to GCS under docs/{uuid}-{filename}.

        Args:
            content:      Raw document bytes.
            filename:     Full filename with extension, e.g. "q1_report.pdf".
            content_type: MIME type, e.g. "application/pdf".

        Returns:
            Public GCS URL.
        """
        key = f"docs/{uuid4()}-{filename}"
        url = await self._storage.store(data=content, key=key, content_type=content_type)
        logger.info("DocumentDeliveryService: stored '%s' → %s", filename, url)
        return url
