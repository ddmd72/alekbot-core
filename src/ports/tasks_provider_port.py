"""
TasksProviderPort — abstract interface for task management providers.

Port is justified: 2+ planned implementations
  (1) GoogleTasksAdapter — REST API + OAuth
  (2) Things3Adapter — local HTTP API with token (future)

Design note: OAuthCredentials are NOT passed via method parameters.
Each adapter resolves auth internally by user_id, using OAuthCredentialsPort
injected at construction time. This makes the port compatible with both
OAuth-based (Google) and token-based (Things 3) providers.
"""

from abc import ABC, abstractmethod
from typing import List

from ..domain.task import Task, TaskCreate, TaskUpdate


class TasksProviderPort(ABC):
    """Abstract interface for task management providers."""

    @abstractmethod
    async def list_tasks(
        self,
        user_id: str,
        show_completed: bool = False,
    ) -> List[Task]:
        """
        List all tasks in the user's dedicated tasklist.

        Args:
            user_id: User identifier (used to fetch credentials internally).
            show_completed: If True, include completed tasks.

        Returns:
            List of Task objects, ordered by due_date ascending (null last).
        """
        ...

    @abstractmethod
    async def create_task(
        self,
        user_id: str,
        task: TaskCreate,
    ) -> Task:
        """
        Create a new task in the user's dedicated tasklist.

        Returns the created Task with provider-assigned task_id.
        """
        ...

    @abstractmethod
    async def update_task(
        self,
        user_id: str,
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
        task_id: str,
    ) -> None:
        """
        Delete a task by ID.

        Raises ValueError if task_id not found.
        """
        ...

    @abstractmethod
    async def search_tasks(
        self,
        user_id: str,
        query: str,
    ) -> List[Task]:
        """
        Search tasks by keyword in title and notes.

        Implementation note: Google Tasks API has no full-text search.
        Implementations should call list_tasks() and filter client-side.

        Returns tasks where query appears in title or notes (case-insensitive).
        """
        ...
