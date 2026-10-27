"""
TaskSearchIndex — abstract interface for the Firestore-backed task search index.

Port is justified: testable substitution + external system boundary (Firestore).
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from ..domain.task import TaskSearchEntry


class TaskSearchIndex(ABC):
    """Vector search index of one user's To Do tasks (thin TaskSearchEntry records), one entry per (user_id, task_id).

    Holds for every method: tenancy is by user_id (a method never reads or removes another user's entry); backend
    errors propagate unless a method says otherwise.
    """

    @abstractmethod
    async def upsert(self, entry: TaskSearchEntry) -> None:
        """Store `entry` under (entry.user_id, entry.task_id), replacing any earlier entry.

        Idempotency: a repeat stores the same single entry.
        """
        ...

    @abstractmethod
    async def delete(self, user_id: str, task_id: str) -> None:
        """Remove the entry of (user_id, task_id).

        Idempotency: a missing entry is a no-op.
        """
        ...

    @abstractmethod
    async def delete_by_list(self, user_id: str, list_id: str) -> None:
        """Remove every entry of `user_id` in `list_id`. No caller in src/.

        Atomicity: removed in batches, so a failure midway leaves the rest; a repeat finishes the job.
        """
        ...

    @abstractmethod
    async def find_nearest(
        self,
        user_id: str,
        vectors: Dict[str, List[float]],
        limit: int = 10,
        show_completed: bool = False,
        list_id: Optional[str] = None,
    ) -> List[TaskSearchEntry]:
        """Up to `limit` entries of `user_id` nearest to `vectors` (field name -> query vector; a None vector is
        skipped, no vector at all -> []). The caller fetches the full tasks from the provider.

        Order: best match first, the fields' rankings fused. Results past a fixed similarity bound are dropped.
        Filter: `show_completed=False` excludes completed entries and entries with no status.
        `list_id`: restricts the search to one list; not supported by the current backend, and no caller passes it.
        Legacy: an entry that does not parse (missing indexed_at or status, unknown status or importance) is skipped.
        Errors: a failing field query is logged and the other fields still answer. When every
        field query fails the result should be the error (P11-G3-1: today it is [], which the caller reports as
        "no tasks").
        """
        ...

    @abstractmethod
    async def get_by_short_id(self, user_id: str, short_id: str) -> Optional[TaskSearchEntry]:
        """The entry of `user_id` whose short_id is `short_id`, or None.

        Legacy: an entry that does not parse reads as None. A short_id is not guaranteed unique: on a clash
        one of the matching entries is returned, which one is unspecified.
        """
        ...

    @abstractmethod
    async def delete_all_for_user(self, user_id: str) -> None:
        """Remove every entry of `user_id`, nobody else's. Called on disconnect.

        Atomicity: removed in batches; a repeat finishes the job.
        """
        ...
