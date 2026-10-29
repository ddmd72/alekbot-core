"""
TaskConfigPort — abstract interface for per-user tasks integration config.

Port is justified: testable substitution + external system boundary (Firestore).
Single implementation: FirestoreTaskConfigRepository.
"""

from abc import ABC, abstractmethod
from typing import List

from ..domain.task import TaskUserConfig


class TaskConfigPort(ABC):
    """Per-user To Do integration config, one document per user_id: the primary list id and the webhook subscriptions.

    Holds for every method: tenancy is the user_id's document only; backend errors propagate.
    """

    @abstractmethod
    async def get_config(self, user_id: str) -> TaskUserConfig:
        """The user's config, or an empty TaskUserConfig when none is stored.

        Every subscription's expires_at is naive UTC. A missing primary_list_id or subscriptions reads empty. A
        subscription without sub_id or list_id: no writer produces one; behaviour pending the data audit. A
        subscription without expires_at reads as naive UTC too (P11-G3-2: today it reads as an aware "now", which
        every caller's naive comparison rejects with TypeError).
        """
        ...

    @abstractmethod
    async def save_config(self, user_id: str, config: TaskUserConfig) -> None:
        """Replace the whole stored config with `config` (primary_list_id included: a None erases a stored list id).

        Atomicity: a blind overwrite, last writer wins; two read-modify-write callers lose each other's changes.
        Idempotency: a repeat stores the same document.
        """
        ...

    @abstractmethod
    async def set_primary_list_id_if_absent(self, user_id: str, list_id: str) -> str:
        """Return the stored primary_list_id when one is set; otherwise store `list_id` and return it.

        Atomicity: of concurrent callers the first commit wins and every caller returns that value. The stored
        subscriptions are kept.
        """
        ...

    @abstractmethod
    async def list_user_ids(self) -> List[str]:
        """Every user with a stored config, sorted (R1 lists users from the feature store, SECRETS_AT_REST_RFC §6.2).

        A listed user's config may be empty (a disconnect stores an empty config).
        """
        ...
