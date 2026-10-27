"""
IndexedEmailRepository — store and search indexed email facts.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §2.1.2.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, List, Optional

from src.domain.email import IndexedEmail, IndexingState


class IndexedEmailRepository(ABC):
    """Port for indexed email facts (one record per user and email, id `{user_id}_{email_id}`; the `email_id` field
    keeps the provider's message id) and the per-provider indexing cursors.

    Holds for every method:
    - Tenancy: user_id scopes every read except get_pending_embeddings (cross-user, for the repair job); account_id is
      stored but never filtered.
    - Dates: datetimes come back naive, in UTC; date filters take naive (UTC) or aware datetimes.
    - Legacy: a stored fact that fails IndexedEmail validation is logged and left out of a result. Such a fact is never
      marked consolidated, so get_unconsolidated_batch reads it again each time; no writer produces one, behaviour
      pending the data audit (as for a fact stored without a `consolidated_at` field).
    """

    @abstractmethod
    async def save_batch(self, emails: List[IndexedEmail]) -> int:
        """Upsert each email (the whole record) under `{user_id}_{email_id}`. Returns the number written.

        Atomicity: written in atomic chunks; a failed chunk writes none of its emails.
        Idempotency: re-saving an email keeps its consolidated_at (P11-G2-1: today it is cleared, so a re-indexed
        email is consolidated again).
        """

    @abstractmethod
    async def find_nearest(
        self,
        user_id: str,
        vectors: Dict[str, List[float]],
        limit: int = 10,
        state: str = "current",
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> List[IndexedEmail]:
        """Multi-vector search: one similarity search per non-None vector key ("vector" | "tags_vector" |
        "metadata_vector" | "attachments_vector"), each limited to user_id, `state` and the optional email_date range
        (inclusive), fused by rank; at most `limit` facts, best fused rank first, nothing past the similarity floor.

        No vectors (all None or none given): [] without a search.
        Errors: a failing channel is logged and skipped; when every channel fails the error is raised
        (P11-G2-2: today [] is returned, which the search service reports as "no emails found").
        """

    @abstractmethod
    async def get_indexing_state(
        self, user_id: str, provider: str
    ) -> Optional[IndexingState]:
        """The cursors, or None if the user has never indexed this provider."""

    @abstractmethod
    async def update_indexing_state(self, state: IndexingState) -> None:
        """Write all three cursors (an upsert); last writer wins.

        Called only after each chunk completes successfully (idempotent retry guarantee).
        """

    @abstractmethod
    async def clear_indexing_state(self, user_id: str, provider: str) -> None:
        """Delete the cursors of this user and provider. No caller in src/."""

    @abstractmethod
    async def count_by_user(
        self, user_id: str, provider: Optional[str] = None
    ) -> int:
        """Count indexed email facts; provider=None counts across all providers. No caller in src/ (scripts)."""

    @abstractmethod
    async def delete_by_user(self, user_id: str) -> None:
        """Delete every fact of user_id, across all providers, until none is left; other users and the cursors are
        untouched. Called on Gmail disconnect. Does not affect biographical facts."""

    @abstractmethod
    async def get_unconsolidated_batch(
        self, user_id: str, limit: int = 200
    ) -> List[IndexedEmail]:
        """Facts of user_id whose consolidated_at is null, oldest indexed_at first, at most `limit`.

        Used by the ConsolidationAgent post-processing hook to feed email facts into biographical memory (§13.1).
        """

    @abstractmethod
    async def mark_consolidated(
        self, user_id: str, email_ids: List[str], consolidated_at: datetime
    ) -> None:
        """Set consolidated_at on the given email ids of user_id; they are not returned by get_unconsolidated_batch
        again.

        Atomicity: in atomic chunks. Errors: the backend's not-found error (NotFound today) when an id is gone (its chunk is not applied).
        """

    @abstractmethod
    async def get_pending_embeddings(self, limit: int = 100) -> List[IndexedEmail]:
        """Facts with embedding_pending true, any user, at most `limit`; a fact that stays pending is returned again.

        Used by EmailEmbeddingRepairService.
        """

    @abstractmethod
    async def update_vectors(
        self, user_id: str, email_id: str, vectors: Dict[str, List[float]]
    ) -> None:
        """Write the given non-None vectors and set embedding_pending false (also when some vectors are None).

        Errors: the backend's not-found error (NotFound today) on a missing fact.
        """
