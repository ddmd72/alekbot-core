"""
TaskLifecyclePort — abstract interface for Graph API integration lifecycle operations.

Separated from TasksProviderPort to keep task CRUD distinct from subscription
management and initial setup concerns. MicrosoftToDoAdapter implements both ports.

Config persistence is the CALLER'S responsibility (TaskSetupService) — these methods
only call Graph API and return results without writing to Firestore.
"""

from abc import ABC, abstractmethod

from ..domain.task import TaskSubscriptionConfig


class SubscriptionNotFoundError(Exception):
    """
    Raised when a provider reports that a subscription no longer exists
    (e.g. MS Graph 404 ResourceNotFound on PATCH). Signals the caller to
    drop the orphan from local state and register a replacement.
    """


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

        Raises SubscriptionNotFoundError if the provider reports the
        subscription does not exist (e.g. expired past provider retention).
        Callers should treat this as a signal to drop the orphan and
        register a fresh subscription for the same list.
        """
        ...

    @abstractmethod
    async def delete_subscription(self, user_id: str, sub_id: str) -> None:
        """DELETE /subscriptions/{sub_id}."""
        ...
