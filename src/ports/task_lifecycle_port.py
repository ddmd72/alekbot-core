"""
TaskLifecyclePort — abstract interface for Graph API integration lifecycle operations.

Separated from TasksProviderPort to keep task CRUD distinct from subscription
management and initial setup concerns. MicrosoftToDoAdapter implements both ports.

Config persistence is the CALLER'S responsibility (TaskSetupService) — these methods
only call Graph API and return results without writing to Firestore.
"""

from abc import ABC, abstractmethod

from ..domain.task import TaskSubscriptionConfig


class TaskLifecyclePort(ABC):
    """Graph API operations for subscription management and initial list setup."""

    @abstractmethod
    async def ensure_primary_list(self, user_id: str) -> str:
        """
        GET /me/todo/lists → find "Alek Bot Tasks" → if absent POST to create.
        Returns list_id. Does NOT persist — caller persists via TaskConfigPort.
        """
        ...

    @abstractmethod
    async def register_subscription(
        self, user_id: str, list_id: str, notification_url_base: str
    ) -> TaskSubscriptionConfig:
        """
        POST /subscriptions for the given list.
        Returns TaskSubscriptionConfig(sub_id, list_id, expires_at).
        Does NOT persist — caller persists via TaskConfigPort.
        """
        ...

    @abstractmethod
    async def renew_subscription(
        self, user_id: str, sub_id: str
    ) -> TaskSubscriptionConfig:
        """
        PATCH /subscriptions/{sub_id} with new expirationDateTime.
        Returns updated TaskSubscriptionConfig.
        Does NOT persist — caller persists via TaskConfigPort.
        """
        ...

    @abstractmethod
    async def delete_subscription(self, user_id: str, sub_id: str) -> None:
        """DELETE /subscriptions/{sub_id}."""
        ...
