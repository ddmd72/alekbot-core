"""
TaskConfigPort — abstract interface for per-user tasks integration config.

Port is justified: testable substitution + external system boundary (Firestore).
Single implementation: FirestoreTaskConfigRepository.
"""

from abc import ABC, abstractmethod

from ..domain.task import TaskUserConfig


class TaskConfigPort(ABC):
    """Stores and retrieves per-user tasks integration config."""

    @abstractmethod
    async def get_config(self, user_id: str) -> TaskUserConfig:
        """Load user's task config. Returns empty TaskUserConfig if not found."""
        ...

    @abstractmethod
    async def save_config(self, user_id: str, config: TaskUserConfig) -> None:
        """Overwrite user's task config."""
        ...

    @abstractmethod
    async def set_primary_list_id_if_absent(self, user_id: str, list_id: str) -> str:
        """
        Atomic create-if-not-exists for primary_list_id.
        If primary_list_id is already set, returns the existing value unchanged.
        If not set, writes list_id and returns it.
        Implemented as a Firestore transaction — safe under concurrent calls.
        """
        ...
