"""
EmailEmbeddingRepairService — re-embeds IndexedEmail docs where embedding_pending=True.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §2.5.

Runs every 6h via Cloud Scheduler. Picks up emails that failed embedding during
initial indexing (transient API errors, quota exhaustion, etc.) and repairs them.
"""

from typing import List

from ..domain.email import IndexedEmail
from ..ports.embedding_service import EmbeddingService
from ..ports.indexed_email_repository import IndexedEmailRepository
from ..utils.logger import logger


class EmailEmbeddingRepairService:
    """
    Lightweight batch repair job.
    No LLM calls — only re-embeds existing text fields.
    """

    def __init__(
        self,
        email_repo: IndexedEmailRepository,
        embedding: EmbeddingService,
        batch_size: int = 100,
    ):
        self._email_repo = email_repo
        self._embedding = embedding
        self._batch_size = batch_size
        logger.info(
            f"🔧 EmailEmbeddingRepairService initialized. batch_size={batch_size}"
        )

    async def run(self) -> int:
        """
        Fetch pending emails, generate embeddings, write back vectors.
        Returns count of emails repaired.
        """
        pending = await self._email_repo.get_pending_embeddings(
            limit=self._batch_size
        )
        if not pending:
            logger.info("🔧 EmailEmbeddingRepairService: no pending embeddings")
            return 0

        logger.info(f"🔧 Repairing embeddings for {len(pending)} emails")
        repaired = 0

        for email in pending:
            try:
                vectors = await self._generate_vectors(email)
                await self._email_repo.update_vectors(email.email_id, vectors)
                repaired += 1
            except Exception as exc:
                logger.error(
                    f"💥 Repair failed for {email.email_id}: {exc}"
                )

        logger.info(f"🔧 Repair complete: {repaired}/{len(pending)} succeeded")
        return repaired

    async def _generate_vectors(self, email: IndexedEmail) -> dict:
        """Generate all 4 vectors for a single email. Returns partial dict on failure."""
        tags_text = " ".join(email.tags) if email.tags else email.text
        meta_text = " ".join(
            filter(
                None,
                [
                    email.subject,
                    email.from_address,
                    email.email_date.strftime("%Y-%m") if email.email_date else "",
                    email.text,
                ],
            )
        )

        # 3 vectors in one batch call
        batch_texts = [email.text, tags_text, meta_text]
        batch_vectors = await self._embedding.get_embeddings_batch(
            batch_texts, "RETRIEVAL_DOCUMENT"
        )

        vectors = {
            "vector": batch_vectors[0],
            "tags_vector": batch_vectors[1],
            "metadata_vector": batch_vectors[2],
        }

        # Attachments vector (optional)
        if email.attachments:
            attach_text = " ".join(email.attachments)
            vectors["attachments_vector"] = await self._embedding.get_embedding(
                attach_text, "RETRIEVAL_DOCUMENT"
            )

        return vectors
