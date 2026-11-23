"""Storage contract for external tasks and their results (EXTERNAL_TASK_QUEUE_RFC §5.1, §5.2).

Implemented by FirestoreExternalTaskStore; an in-memory fake in tests/support/external_task_stores.py. Used by
ExternalTaskService.
"""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Collection, List, Optional

from ..domain.external_task import ExternalTask, ExternalTaskStatus, TaskDelivery, TaskResultMeta
from ..domain.external_task_rules import TaskTransition


class ExternalTaskStore(ABC):
    """Tasks keyed by `task_id`; results keyed by the same id, kept apart so listings never read bodies.

    Holds for every method:
    - Tenancy: every method takes the user; a task of another user reads as absent and is never changed (a transition
      on it, like one on a missing task, is `TaskTransition(task=None, refusal=not_found)`). Worker tenancy is the rule's.
    - Transitions: each state change is ONE transaction that reads the task and applies the named function of
      `src/domain/external_task_rules.py` to what it read; the task is written only when that function says so
      (`written`), whole. A refused or unchanged transition writes nothing. Of concurrent transitions on one task each
      runs its rule on the state the other left (the loser re-reads), so a precondition never passes on stale state.
    - The result record is create-only, written in the submit transaction only, and never overwritten or deleted here.
    - Datetimes read back tz-aware UTC.
    - Errors: backend errors propagate.
    """

    @abstractmethod
    async def create(self, task: ExternalTask) -> None:
        """Store a new task; never overwrites (an existing id raises the backend's conflict)."""

    @abstractmethod
    async def get(self, user_id: str, task_id: str) -> Optional[ExternalTask]:
        """The user's task, or None when absent or foreign."""

    @abstractmethod
    async def get_result(self, user_id: str, task_id: str) -> Optional[str]:
        """The stored result text of the user's task, or None when there is none or the task is foreign."""

    @abstractmethod
    async def list_for_user(self, user_id: str, limit: int) -> List[ExternalTask]:
        """The user's newest tasks, newest `created_at` first, at most `limit`."""

    @abstractmethod
    async def list_for_worker(self, user_id: str, worker_id: str,
                              statuses: Collection[ExternalTaskStatus]) -> List[ExternalTask]:
        """The user's tasks of that worker whose status is in `statuses`, oldest `created_at` first. No limit (the
        open-tasks cap bounds non-final statuses)."""

    @abstractmethod
    async def count_open(self, user_id: str) -> int:
        """How many of the user's tasks are `held`, `queued` or `leased`."""

    @abstractmethod
    async def find_held(self, user_id: str, code_digest: str) -> List[ExternalTask]:
        """The user's `held` tasks whose hold carries `code_digest` (no time check)."""

    @abstractmethod
    async def queued_candidates(self, user_id: str, worker_id: str, limit: int) -> List[ExternalTask]:
        """The worker's `queued` tasks, oldest `created_at` first, at most `limit` (index: user_id, worker_id, status,
        created_at)."""

    @abstractmethod
    async def lapsed_candidates(self, user_id: str, worker_id: str, now: datetime, limit: int) -> List[ExternalTask]:
        """The worker's `leased` tasks with `lease.until <= now`, earliest `until` first, at most `limit` (index:
        user_id, worker_id, status, lease.until)."""

    # -- transitions: one transaction each, the rule of the same name -----------------------------------------------

    @abstractmethod
    async def claim(self, user_id: str, task_id: str, *, worker_id: str, expected_status: ExternalTaskStatus,
                    expected_generation: int, lease_id: str, now: datetime) -> TaskTransition:
        """`rules.claim`."""

    @abstractmethod
    async def renew(self, user_id: str, task_id: str, *, worker_id: str, lease_id: str,
                    now: datetime) -> TaskTransition:
        """`rules.renew`."""

    @abstractmethod
    async def submit(self, user_id: str, task_id: str, *, worker_id: str, lease_id: str, meta: TaskResultMeta,
                     result_text: str, now: datetime) -> TaskTransition:
        """`rules.submit`; when it writes, the same transaction creates the result record `{text: result_text,
        purge_after: <the task's>}`. A result record that already exists aborts the whole transaction (the backend's
        conflict propagates, nothing is written)."""

    @abstractmethod
    async def release(self, user_id: str, task_id: str, *, worker_id: str, lease_id: str,
                      now: datetime) -> TaskTransition:
        """`rules.release`."""

    @abstractmethod
    async def fail(self, user_id: str, task_id: str, *, worker_id: str, lease_id: str, reason: Optional[str],
                   now: datetime) -> TaskTransition:
        """`rules.fail`."""

    @abstractmethod
    async def cancel(self, user_id: str, task_id: str, *, now: datetime) -> TaskTransition:
        """`rules.cancel`."""

    @abstractmethod
    async def confirm(self, user_id: str, task_id: str, *, code_digest: str, now: datetime) -> TaskTransition:
        """`rules.confirm`."""

    @abstractmethod
    async def expire_lease(self, user_id: str, task_id: str, *, generation: int, now: datetime) -> TaskTransition:
        """`rules.expire_lease`."""

    @abstractmethod
    async def expire_deadline(self, user_id: str, task_id: str, *, now: datetime) -> TaskTransition:
        """`rules.expire_deadline`."""

    @abstractmethod
    async def expire_hold(self, user_id: str, task_id: str, *, now: datetime) -> TaskTransition:
        """`rules.expire_hold`."""

    @abstractmethod
    async def record_delivery(self, user_id: str, task_id: str, *, delivery: TaskDelivery) -> TaskTransition:
        """`rules.record_delivery`."""
