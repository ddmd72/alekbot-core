"""
IndexedEmailRepository — store and search indexed email facts.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §2.1.2.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, List, Optional, Union

from src.domain.email import IndexedEmail, IndexingState
from src.domain.reconnect_policy import Cleared


class IndexedEmailRepository(ABC):
    """Port for indexed email facts (one record per user and email, id `{user_id}_{email_id}`; the `email_id` field
    keeps the provider's message id) and the per-provider indexing cursors.

    Holds for every method:
    - Tenancy: user_id scopes every read; account_id is stored but never filtered.
    - Dates: datetimes come back naive, in UTC; date filters take naive (UTC) or aware datetimes.
    - Legacy: a stored fact that fails IndexedEmail validation is logged and left out of a result. Such a fact is never
      marked consolidated, so get_unconsolidated_batch reads it again each time; no writer produces one, behaviour
      pending the data audit (as for a fact stored without a `consolidated_at` field).
    """

    @abstractmethod
    async def save_batch(self, emails: List[IndexedEmail]) -> int:
        """Upsert each email (the whole record) under `{user_id}_{email_id}`. Returns the number written.

        Atomicity: written in atomic chunks; a failed chunk writes none of its emails.
        Idempotency: re-saving an email sent without consolidated_at keeps the stored one, so a re-indexed email is
        not consolidated again; an email sent with one writes it.
        """

    @abstractmethod
    async def get_indexing_state(
        self, user_id: str, provider: str
    ) -> Optional[IndexingState]:
        """The cursors, or None if the user has never indexed this provider."""

    @abstractmethod
    async def update_indexing_state(self, state: IndexingState) -> None:
        """Write all three cursors (an upsert); last writer wins. The R1 fields are never written here.

        Called only after each chunk completes successfully (idempotent retry guarantee).
        """

    @abstractmethod
    async def set_reconnect_flags(
        self, user_id: str, provider: str, *,
        reconnect_notified: Optional[bool] = None,
        disconnected_by_user_at: Union[datetime, Cleared, None] = None,
    ) -> None:
        """Field-level update of the R1 fields of the indexing state (SECRETS_AT_REST_RFC §6.2, F1/F2): None leaves a
        field as stored, `Cleared.MARK` empties the mark. The cursors are untouched; a missing state is left missing
        (no past connection, nothing for R1 to see). The only writer of these two fields."""

    @abstractmethod
    async def list_indexing_user_ids(self, provider: str) -> List[str]:
        """The users that have an indexing state for `provider`, sorted (R1 lists users from the feature store, never
        from the credential store)."""

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
    async def get_consolidation_marks(
        self, user_id: str, email_ids: List[str]
    ) -> Dict[str, Optional[datetime]]:
        """The ids among `email_ids` already stored for user_id, each mapped to its consolidated_at (None while not
        yet consolidated). An id that is not stored is absent from the result; no ids: {}.

        Used by the indexing pipeline to skip emails already in the index and to keep the mark of one it re-processes.
        """
