"""Storage contract for external workers and their keys (EXTERNAL_TASK_QUEUE_RFC §5.1, §5.6).

Implemented by FirestoreExternalWorkerStore; an in-memory fake in tests/support/external_task_stores.py. Used by
ExternalTaskService.
"""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional

from ..domain.external_task_refusal import TaskRefusal
from ..domain.external_worker import ExternalWorker
from ..domain.external_worker_key import ExternalWorkerKey


class ExternalWorkerStore(ABC):
    """Workers keyed by `(user_id, worker_id)`; keys keyed by `key_digest` (the keyed digest of the raw key: the raw
    key never reaches the store).

    Holds for every method:
    - Tenancy: every worker read and write takes the user; a foreign worker reads as absent and is never changed. A
      key is read by digest only (`get_key`, the one lookup a presented key makes); the caller checks the record's
      user and worker.
    - Datetimes read back tz-aware UTC.
    - Errors: backend errors propagate.
    """

    @abstractmethod
    async def create_worker(self, worker: ExternalWorker, max_active: int) -> Optional[TaskRefusal]:
        """Store a new active worker in one transaction with its checks: `label_taken` when an active worker of the
        user has the label, `workers_cap` when the user already has `max_active` active workers; None when stored.
        A refused create writes nothing."""

    @abstractmethod
    async def get_worker(self, user_id: str, worker_id: str) -> Optional[ExternalWorker]:
        """The user's worker (active or revoked), or None."""

    @abstractmethod
    async def list_workers(self, user_id: str) -> List[ExternalWorker]:
        """Every worker of the user, active and revoked, oldest `created_at` first."""

    @abstractmethod
    async def find_active_by_label(self, user_id: str, label: str) -> Optional[ExternalWorker]:
        """The user's active worker with that label, or None."""

    @abstractmethod
    async def revoke_worker(self, user_id: str, worker_id: str, now: datetime) -> bool:
        """Set an active worker `revoked` (`updated_at = now`) in one transaction; False when absent, foreign or
        already revoked (nothing written)."""

    @abstractmethod
    async def set_wake(self, user_id: str, worker_id: str, *, wake_host: Optional[str], wake_secret: Optional[str],
                       now: datetime) -> bool:
        """Replace an active worker's wake-up target in one transaction: `wake_host`, `wake_secret` (the ciphertext,
        stored as given), `has_wake = wake_secret is not None`, `updated_at = now`; both None clears the target.
        `wake_generation` is left alone. False when absent, foreign or revoked (nothing written)."""

    @abstractmethod
    async def bump_wake_generation(self, user_id: str, worker_id: str) -> Optional[int]:
        """Add one to an active worker's `wake_generation` in one transaction, when it has a target; the new value.
        None when absent, foreign, revoked or without a target (nothing written)."""

    @abstractmethod
    async def create_key(self, key: ExternalWorkerKey, max_active: int) -> Optional[TaskRefusal]:
        """Store a new key in one transaction with its check: `keys_cap` when the worker already has `max_active`
        unrevoked keys; None when stored. Never overwrites (an existing digest raises the backend's conflict)."""

    @abstractmethod
    async def get_key(self, key_digest: str) -> Optional[ExternalWorkerKey]:
        """The key record under that digest (revoked or not), or None."""

    @abstractmethod
    async def list_keys(self, user_id: str, worker_id: str) -> List[ExternalWorkerKey]:
        """Every key of the user's worker, revoked included, oldest `created_at` first."""

    @abstractmethod
    async def revoke_key(self, user_id: str, worker_id: str, key_digest: str, now: datetime) -> bool:
        """Set `revoked_at = now` on an unrevoked key of that user and worker, in one transaction; False when absent,
        foreign or already revoked (nothing written)."""

    @abstractmethod
    async def touch_key(self, key_digest: str, now: datetime) -> None:
        """Set `last_used_at = now` on the key; an absent key is a no-op."""
