"""
IndexedEmailRepository — store and search indexed email facts.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §2.1.2.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, List, Optional

from src.domain.email import IndexedEmail, IndexingState


class IndexedEmailRepository(ABC):

    @abstractmethod
    async def save_batch(self, emails: List[IndexedEmail]) -> int:
        """
        Upsert batch. email_id is document ID — idempotent on retry.
        Returns count of documents written.
        Firestore max: 500 writes per batch transaction.
        """

    @abstractmethod
    async def find_nearest(
        self,
        user_id: str,
        vectors: Dict[str, List[float]],
        limit: int = 10,
        state: str = "current",
    ) -> List[IndexedEmail]:
        """
        Multi-vector RRF search across provided vector fields.
        vectors keys: "vector" | "tags_vector" | "metadata_vector" | "attachments_vector"
        Absent keys are skipped (e.g., attachments_vector absent → skip that query).
        Returns top-N by RRF score, filtered by user_id and state.
        """

    @abstractmethod
    async def get_indexing_state(
        self, user_id: str, provider: str
    ) -> Optional[IndexingState]:
        """Returns None if user has never indexed this provider."""

    @abstractmethod
    async def update_indexing_state(self, state: IndexingState) -> None:
        """
        Advance indexed_through cursor.
        Called only after each chunk completes successfully (idempotent retry guarantee).
        """

    @abstractmethod
    async def count_by_user(
        self, user_id: str, provider: Optional[str] = None
    ) -> int:
        """Count indexed email facts. provider=None counts across all providers."""

    @abstractmethod
    async def delete_by_user(self, user_id: str) -> None:
        """
        Delete ALL indexed facts for user across all providers.
        Called on Gmail disconnect. Does not affect biographical facts in domain_facts_v2.
        """

    @abstractmethod
    async def get_unconsolidated_batch(
        self, user_id: str, limit: int = 200
    ) -> List[IndexedEmail]:
        """
        WHERE consolidated_at IS NULL AND user_id = X ORDER BY indexed_at ASC LIMIT N.
        Used by ConsolidationAgent post-processing hook to feed email facts
        into biographical memory (§13.1). Default limit=200 matches UAT optimal batch size.
        """

    @abstractmethod
    async def mark_consolidated(
        self, email_ids: List[str], consolidated_at: datetime
    ) -> None:
        """
        Batch update: set consolidated_at = now() on processed IDs.
        Called after ConsolidationAgent completes email triage.
        Re-runs are safe — deduplication in ConsolidationAgent prevents double-writes.
        """

    @abstractmethod
    async def get_pending_embeddings(self, limit: int = 100) -> List[IndexedEmail]:
        """
        WHERE embedding_pending=True LIMIT N.
        Used by EmailEmbeddingRepairService (Cloud Scheduler, every 6h).
        """

    @abstractmethod
    async def update_vectors(
        self, email_id: str, vectors: Dict[str, List[float]]
    ) -> None:
        """
        Partial update: write computed vectors dict, set embedding_pending=False.
        Called by repair service after successful re-embedding.
        """
