"""
TasksProviderPort — abstract interface for task management providers.

Port is justified: 2+ implementations
  (1) MicrosoftToDoAdapter — Graph API + OAuth
  (2) GoogleTasksAdapter — frozen, retained for reference

Design note: OAuthCredentials are NOT passed via method parameters.
Each adapter resolves auth internally by user_id, using OAuthCredentialsPort
injected at construction time.

Note on search_tasks: removed from port. Semantic search is handled by
TaskSearchIndex (vector search in Firestore), not by the provider.
GoogleTasksAdapter retained its own client-side implementation internally,
but it is no longer exposed via this port.
"""

from abc import ABC, abstractmethod
from typing import List, Optional, Tuple

from ..domain.task import Task, TaskCreate, TaskList, TaskUpdate


class TasksProviderPort(ABC):
    """Abstract interface for task management providers."""

    @abstractmethod
    async def list_task_lists(self, user_id: str) -> List[TaskList]:
        """Return all task lists for the user."""
        ...

    @abstractmethod
    async def list_tasks(
        self,
        user_id: str,
        list_id: Optional[str] = None,
        show_completed: bool = False,
    ) -> List[Task]:
        """
        List tasks. list_id=None returns tasks from the user's primary list.

        Implementation note: adapters resolve the primary list via task config
        when list_id is not provided. Pass an explicit list_id to query a specific list.
        """
        ...

    @abstractmethod
    async def get_task(self, user_id: str, list_id: str, task_id: str) -> Task:
        """Fetch single task by ID. Raises ValueError if not found."""
        ...

    @abstractmethod
    async def batch_get_tasks(
        self, user_id: str, task_refs: List[Tuple[str, str]]
    ) -> List[Task]:
        """
        Fetch multiple tasks across lists. Used after search_index lookup.
        task_refs: list of (list_id, task_id) tuples.
        """
        ...

    @abstractmethod
    async def create_task(self, user_id: str, task: TaskCreate) -> Task:
        """
        Create a new task in the user's dedicated list.
        Returns the created Task with provider-assigned task_id.
        """
        ...

    @abstractmethod
    async def update_task(
        self,
        user_id: str,
        list_id: str,
        task_id: str,
        updates: TaskUpdate,
    ) -> Task:
        """
        Update an existing task.
        Raises ValueError if task_id not found.
        Returns the updated Task.
        """
        ...

    @abstractmethod
    async def delete_task(
        self,
        user_id: str,
        list_id: str,
        task_id: str,
    ) -> None:
        """
        Delete a task by ID.
        Raises ValueError if task_id not found.
        """
        ...
