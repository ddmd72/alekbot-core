"""Firestore storage of external tasks and their results (EXTERNAL_TASK_QUEUE_RFC §5.1, §5.2).

Collections (`EnvironmentConfig`): `{prefix}external_tasks` (doc id `task_id`; fields: every `ExternalTask` field but
the id, nested value objects as maps, enums as their values) and `{prefix}external_task_results` (doc id `task_id`;
fields `text`, `purge_after`). Both carry `purge_after` once the task is final; a TTL policy on it deletes them (the
runbook entry is written with the TTL step of the RFC).

Every transition is one transaction over the task document: read it, check the user, apply the rule of
`src/domain/external_task_rules.py`, write the whole task when the rule says so. Submit creates the result document in
the same transaction with `create`, so an existing result aborts the commit and is never overwritten. Of two concurrent
transitions the second commit aborts and re-runs its rule on the state the first left.

Queries and their indexes (`config/firestore.indexes.json`):
- claim candidates: `user_id ==, worker_id ==, status == queued`, order `created_at` ASC (composite);
- lapsed leases: `user_id ==, worker_id ==, status == leased, lease.until <=` (composite);
- listing: `user_id ==`, order `created_at` DESC (composite);
- worker listing, open count, held lookup: equality only (single-field indexes merge).
"""
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Collection, Dict, List, Optional

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from ..config.environment import EnvironmentConfig
from ..domain import external_task_rules as rules
from ..domain.external_task import (
    OPEN_STATUSES,
    ExternalTask,
    ExternalTaskStatus,
    TaskDelivery,
    TaskResultMeta,
)
from ..domain.external_task_refusal import TaskRefusal
from ..domain.external_task_rules import TaskTransition
from ..ports.external_task_store import ExternalTaskStore

Rule = Callable[[ExternalTask], TaskTransition]


def _encode(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_encode(v) for v in value]
    return value


def _decode(value: Any) -> Any:
    """Plain tz-aware UTC datetimes from Firestore's DatetimeWithNanoseconds, through nested maps."""
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        aware = aware.astimezone(timezone.utc)
        return datetime(aware.year, aware.month, aware.day, aware.hour, aware.minute, aware.second,
                        aware.microsecond, tzinfo=timezone.utc)
    if isinstance(value, dict):
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


def _to_doc(task: ExternalTask) -> Dict[str, Any]:
    data: Dict[str, Any] = _encode(task.model_dump())
    del data["task_id"]
    return data


def _from_doc(task_id: str, data: Dict[str, Any]) -> ExternalTask:
    return ExternalTask.model_validate({**_decode(data), "task_id": task_id})


class FirestoreExternalTaskStore(ExternalTaskStore):
    def __init__(self, db_client: Any, env_config: EnvironmentConfig) -> None:
        self._db = db_client
        self._tasks = db_client.collection(env_config.external_tasks_collection)
        self._results = db_client.collection(env_config.external_task_results_collection)

    # -- reads ------------------------------------------------------------------------------------------------------

    async def create(self, task: ExternalTask) -> None:
        await self._tasks.document(task.task_id).create(_to_doc(task))

    async def get(self, user_id: str, task_id: str) -> Optional[ExternalTask]:
        if not task_id:
            return None
        snap = await self._tasks.document(task_id).get()
        if not snap.exists:
            return None
        data = snap.to_dict()
        if data.get("user_id") != user_id:
            return None
        return _from_doc(task_id, data)

    async def get_result(self, user_id: str, task_id: str) -> Optional[str]:
        if await self.get(user_id, task_id) is None:
            return None
        snap = await self._results.document(task_id).get()
        if not snap.exists:
            return None
        text = snap.to_dict().get("text")
        return text if isinstance(text, str) else None

    async def list_for_user(self, user_id: str, limit: int) -> List[ExternalTask]:
        query = (self._tasks.where(filter=FieldFilter("user_id", "==", user_id))
                 .order_by("created_at", direction=firestore.Query.DESCENDING).limit(limit))
        return [_from_doc(s.id, s.to_dict()) for s in await query.get()]

    async def list_for_worker(self, user_id: str, worker_id: str,
                              statuses: Collection[ExternalTaskStatus]) -> List[ExternalTask]:
        if not statuses:
            return []
        query = (self._tasks.where(filter=FieldFilter("user_id", "==", user_id))
                 .where(filter=FieldFilter("worker_id", "==", worker_id))
                 .where(filter=FieldFilter("status", "in", sorted(s.value for s in statuses))))
        tasks = [_from_doc(s.id, s.to_dict()) for s in await query.get()]
        return sorted(tasks, key=lambda t: (t.created_at, t.task_id))

    async def count_open(self, user_id: str) -> int:
        query = (self._tasks.where(filter=FieldFilter("user_id", "==", user_id))
                 .where(filter=FieldFilter("status", "in", sorted(s.value for s in OPEN_STATUSES)))
                 .select(["status"]))
        return len(await query.get())

    async def find_held(self, user_id: str, code_digest: str) -> List[ExternalTask]:
        query = (self._tasks.where(filter=FieldFilter("user_id", "==", user_id))
                 .where(filter=FieldFilter("hold.code_digest", "==", code_digest)))
        return [t for t in (_from_doc(s.id, s.to_dict()) for s in await query.get())
                if t.status is ExternalTaskStatus.HELD]

    async def queued_candidates(self, user_id: str, worker_id: str, limit: int) -> List[ExternalTask]:
        query = (self._tasks.where(filter=FieldFilter("user_id", "==", user_id))
                 .where(filter=FieldFilter("worker_id", "==", worker_id))
                 .where(filter=FieldFilter("status", "==", ExternalTaskStatus.QUEUED.value))
                 .order_by("created_at").limit(limit))
        return [_from_doc(s.id, s.to_dict()) for s in await query.get()]

    async def lapsed_candidates(self, user_id: str, worker_id: str, now: datetime, limit: int) -> List[ExternalTask]:
        query = (self._tasks.where(filter=FieldFilter("user_id", "==", user_id))
                 .where(filter=FieldFilter("worker_id", "==", worker_id))
                 .where(filter=FieldFilter("status", "==", ExternalTaskStatus.LEASED.value))
                 .where(filter=FieldFilter("lease.until", "<=", now))
                 .limit(limit))
        return [_from_doc(s.id, s.to_dict()) for s in await query.get()]

    # -- transitions ------------------------------------------------------------------------------------------------

    async def _transition(self, user_id: str, task_id: str, rule: Rule,
                          result_text: Optional[str] = None) -> TaskTransition:
        if not task_id:
            return TaskTransition.refuse(None, TaskRefusal.NOT_FOUND)
        ref = self._tasks.document(task_id)
        result_ref = self._results.document(task_id)

        @firestore.async_transactional
        async def _run(transaction: Any) -> TaskTransition:
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                return TaskTransition.refuse(None, TaskRefusal.NOT_FOUND)
            data = snap.to_dict()
            if data.get("user_id") != user_id:
                return TaskTransition.refuse(None, TaskRefusal.NOT_FOUND)
            outcome = rule(_from_doc(task_id, data))
            if outcome.written and outcome.task is not None:
                transaction.set(ref, _to_doc(outcome.task))
                if result_text is not None:
                    transaction.create(result_ref, {"text": result_text, "purge_after": outcome.task.purge_after})
            return outcome

        return await _run(self._db.transaction())

    async def claim(self, user_id: str, task_id: str, *, worker_id: str, expected_status: ExternalTaskStatus,
                    expected_generation: int, lease_id: str, now: datetime) -> TaskTransition:
        return await self._transition(user_id, task_id, lambda t: rules.claim(
            t, worker_id=worker_id, expected_status=expected_status, expected_generation=expected_generation,
            lease_id=lease_id, now=now))

    async def renew(self, user_id: str, task_id: str, *, worker_id: str, lease_id: str,
                    now: datetime) -> TaskTransition:
        return await self._transition(user_id, task_id, lambda t: rules.renew(
            t, worker_id=worker_id, lease_id=lease_id, now=now))

    async def submit(self, user_id: str, task_id: str, *, worker_id: str, lease_id: str, meta: TaskResultMeta,
                     result_text: str, now: datetime) -> TaskTransition:
        return await self._transition(user_id, task_id, lambda t: rules.submit(
            t, worker_id=worker_id, lease_id=lease_id, meta=meta, now=now), result_text=result_text)

    async def release(self, user_id: str, task_id: str, *, worker_id: str, lease_id: str,
                      now: datetime) -> TaskTransition:
        return await self._transition(user_id, task_id, lambda t: rules.release(
            t, worker_id=worker_id, lease_id=lease_id, now=now))

    async def fail(self, user_id: str, task_id: str, *, worker_id: str, lease_id: str, reason: Optional[str],
                   now: datetime) -> TaskTransition:
        return await self._transition(user_id, task_id, lambda t: rules.fail(
            t, worker_id=worker_id, lease_id=lease_id, reason=reason, now=now))

    async def cancel(self, user_id: str, task_id: str, *, now: datetime) -> TaskTransition:
        return await self._transition(user_id, task_id, lambda t: rules.cancel(t, now=now))

    async def confirm(self, user_id: str, task_id: str, *, code_digest: str, now: datetime) -> TaskTransition:
        return await self._transition(user_id, task_id, lambda t: rules.confirm(t, code_digest=code_digest, now=now))

    async def expire_lease(self, user_id: str, task_id: str, *, generation: int, now: datetime) -> TaskTransition:
        return await self._transition(user_id, task_id, lambda t: rules.expire_lease(
            t, generation=generation, now=now))

    async def expire_deadline(self, user_id: str, task_id: str, *, now: datetime) -> TaskTransition:
        return await self._transition(user_id, task_id, lambda t: rules.expire_deadline(t, now=now))

    async def expire_hold(self, user_id: str, task_id: str, *, now: datetime) -> TaskTransition:
        return await self._transition(user_id, task_id, lambda t: rules.expire_hold(t, now=now))

    async def record_delivery(self, user_id: str, task_id: str, *, delivery: TaskDelivery) -> TaskTransition:
        return await self._transition(user_id, task_id, lambda t: rules.record_delivery(t, delivery=delivery))
