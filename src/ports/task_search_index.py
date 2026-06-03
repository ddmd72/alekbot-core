"""
TaskSearchIndex — abstract interface for the Firestore-backed task search index.

Port is justified: testable substitution + external system boundary (Firestore).
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from ..domain.task import TaskSearchEntry


class TaskSearchIndex(ABC):
    """Abstract vector search index for tasks."""

    @abstractmethod
    async def upsert(self, entry: TaskSearchEntry) -> None:
        """Insert or replace search index entry for a task."""
        ...

    @abstractmethod
    async def delete(self, user_id: str, task_id: str) -> None:
        """Remove task from search index."""
        ...

    @abstractmethod
    async def delete_by_list(self, user_id: str, list_id: str) -> None:
        """Remove all tasks in a list from the index (used when list is deleted)."""
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
        """
        RRF vector search. Returns TaskSearchEntry list (caller fetches full tasks from Graph).
        show_completed=False filters out status==COMPLETED.
        list_id if set restricts search to one list.
        """
        ...

    @abstractmethod
    async def get_by_short_id(self, user_id: str, short_id: str) -> Optional[TaskSearchEntry]:
        """Look up a single index entry by its stable short_id. Returns None if not found."""
        ...

    @abstractmethod
    async def delete_all_for_user(self, user_id: str) -> None:
        """Remove all task index entries for a user. Called on disconnect."""
        ...
