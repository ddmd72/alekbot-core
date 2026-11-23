"""In-memory `ExternalTaskStore` and `ExternalWorkerStore` with the ports' semantics (EXTERNAL_TASK_QUEUE_RFC §5.1, §5.2).

The transitions run the same rule functions as the Firestore adapter (`src/domain/external_task_rules.py`), with no
await between the read and the write, so of concurrent transitions on one event loop each sees the state the other
left, as a re-run Firestore transaction does. Creates never overwrite (`AlreadyExists`, like the backend); the result
record is created only by a submit that writes, and a second one raises before anything is written.

`get_key_reads` counts key lookups by digest, so a test can show that a malformed key costs no read.
"""
from datetime import datetime
from typing import Callable, Collection, Dict, List, Optional, Tuple

from google.api_core.exceptions import AlreadyExists

from src.domain import external_task_rules as rules
from src.domain.external_task import (
    OPEN_STATUSES,
    ExternalTask,
    ExternalTaskStatus,
    TaskDelivery,
    TaskResultMeta,
)
from src.domain.external_task_refusal import TaskRefusal
from src.domain.external_task_rules import TaskTransition
from src.domain.external_worker import ExternalWorker, WorkerStatus
from src.domain.external_worker_key import ExternalWorkerKey
from src.ports.external_task_store import ExternalTaskStore
from src.ports.external_worker_store import ExternalWorkerStore

Rule = Callable[[ExternalTask], TaskTransition]


class FakeExternalTaskStore(ExternalTaskStore):
    def __init__(self) -> None:
        self.tasks: Dict[str, ExternalTask] = {}
        self.results: Dict[str, Tuple[str, Optional[datetime]]] = {}     # task_id -> (text, purge_after)

    async def create(self, task: ExternalTask) -> None:
        if task.task_id in self.tasks:
            raise AlreadyExists(f"task {task.task_id} exists")
        self.tasks[task.task_id] = task

    async def get(self, user_id: str, task_id: str) -> Optional[ExternalTask]:
        task = self.tasks.get(task_id)
        return task if task is not None and task.user_id == user_id else None

    async def get_result(self, user_id: str, task_id: str) -> Optional[str]:
        if await self.get(user_id, task_id) is None or task_id not in self.results:
            return None
        return self.results[task_id][0]

    def _of(self, user_id: str) -> List[ExternalTask]:
        return [t for t in self.tasks.values() if t.user_id == user_id]

    async def list_for_user(self, user_id: str, limit: int) -> List[ExternalTask]:
        return sorted(self._of(user_id), key=lambda t: (t.created_at, t.task_id), reverse=True)[:limit]

    async def list_for_worker(self, user_id: str, worker_id: str,
                              statuses: Collection[ExternalTaskStatus]) -> List[ExternalTask]:
        return sorted((t for t in self._of(user_id) if t.worker_id == worker_id and t.status in statuses),
                      key=lambda t: (t.created_at, t.task_id))

    async def count_open(self, user_id: str) -> int:
        return sum(1 for t in self._of(user_id) if t.status in OPEN_STATUSES)

    async def find_held(self, user_id: str, code_digest: str) -> List[ExternalTask]:
        return [t for t in self._of(user_id) if t.status is ExternalTaskStatus.HELD
                and t.hold is not None and t.hold.code_digest == code_digest]

    async def queued_candidates(self, user_id: str, worker_id: str, limit: int) -> List[ExternalTask]:
        found = [t for t in self._of(user_id) if t.worker_id == worker_id and t.status is ExternalTaskStatus.QUEUED]
        return sorted(found, key=lambda t: (t.created_at, t.task_id))[:limit]

    async def lapsed_candidates(self, user_id: str, worker_id: str, now: datetime, limit: int) -> List[ExternalTask]:
        found = [t for t in self._of(user_id) if t.worker_id == worker_id and t.status is ExternalTaskStatus.LEASED
                 and t.lease.until is not None and t.lease.until <= now]
        return sorted(found, key=lambda t: (t.lease.until, t.task_id))[:limit]     # type: ignore[arg-type, return-value]

    def _transition(self, user_id: str, task_id: str, rule: Rule,
                    result_text: Optional[str] = None) -> TaskTransition:
        task = self.tasks.get(task_id)
        if task is None or task.user_id != user_id:
            return TaskTransition.refuse(None, TaskRefusal.NOT_FOUND)
        outcome = rule(task)
        if outcome.written and outcome.task is not None:
            if result_text is not None:
                if task_id in self.results:
                    raise AlreadyExists(f"result {task_id} exists")
                self.results[task_id] = (result_text, outcome.task.purge_after)
            self.tasks[task_id] = outcome.task
        return outcome

    async def claim(self, user_id: str, task_id: str, *, worker_id: str, expected_status: ExternalTaskStatus,
                    expected_generation: int, lease_id: str, now: datetime) -> TaskTransition:
        return self._transition(user_id, task_id, lambda t: rules.claim(
            t, worker_id=worker_id, expected_status=expected_status, expected_generation=expected_generation,
            lease_id=lease_id, now=now))

    async def renew(self, user_id: str, task_id: str, *, worker_id: str, lease_id: str,
                    now: datetime) -> TaskTransition:
        return self._transition(user_id, task_id, lambda t: rules.renew(t, worker_id=worker_id, lease_id=lease_id,
                                                                        now=now))

    async def submit(self, user_id: str, task_id: str, *, worker_id: str, lease_id: str, meta: TaskResultMeta,
                     result_text: str, now: datetime) -> TaskTransition:
        return self._transition(user_id, task_id, lambda t: rules.submit(
            t, worker_id=worker_id, lease_id=lease_id, meta=meta, now=now), result_text=result_text)

    async def release(self, user_id: str, task_id: str, *, worker_id: str, lease_id: str,
                      now: datetime) -> TaskTransition:
        return self._transition(user_id, task_id, lambda t: rules.release(t, worker_id=worker_id, lease_id=lease_id,
                                                                          now=now))

    async def fail(self, user_id: str, task_id: str, *, worker_id: str, lease_id: str, reason: Optional[str],
                   now: datetime) -> TaskTransition:
        return self._transition(user_id, task_id, lambda t: rules.fail(t, worker_id=worker_id, lease_id=lease_id,
                                                                       reason=reason, now=now))

    async def cancel(self, user_id: str, task_id: str, *, now: datetime) -> TaskTransition:
        return self._transition(user_id, task_id, lambda t: rules.cancel(t, now=now))

    async def confirm(self, user_id: str, task_id: str, *, code_digest: str, now: datetime) -> TaskTransition:
        return self._transition(user_id, task_id, lambda t: rules.confirm(t, code_digest=code_digest, now=now))

    async def expire_lease(self, user_id: str, task_id: str, *, generation: int, now: datetime) -> TaskTransition:
        return self._transition(user_id, task_id, lambda t: rules.expire_lease(t, generation=generation, now=now))

    async def expire_deadline(self, user_id: str, task_id: str, *, now: datetime) -> TaskTransition:
        return self._transition(user_id, task_id, lambda t: rules.expire_deadline(t, now=now))

    async def expire_hold(self, user_id: str, task_id: str, *, now: datetime) -> TaskTransition:
        return self._transition(user_id, task_id, lambda t: rules.expire_hold(t, now=now))

    async def record_delivery(self, user_id: str, task_id: str, *, delivery: TaskDelivery) -> TaskTransition:
        return self._transition(user_id, task_id, lambda t: rules.record_delivery(t, delivery=delivery))


class FakeExternalWorkerStore(ExternalWorkerStore):
    def __init__(self) -> None:
        self.workers: Dict[Tuple[str, str], ExternalWorker] = {}
        self.keys: Dict[str, ExternalWorkerKey] = {}
        self.get_key_reads = 0

    async def create_worker(self, worker: ExternalWorker, max_active: int) -> Optional[TaskRefusal]:
        active = [w for w in self.workers.values() if w.user_id == worker.user_id and w.is_active]
        if any(w.label == worker.label for w in active):
            return TaskRefusal.LABEL_TAKEN
        if len(active) >= max_active:
            return TaskRefusal.WORKERS_CAP
        if (worker.user_id, worker.worker_id) in self.workers:
            raise AlreadyExists(f"worker {worker.worker_id} exists")
        self.workers[(worker.user_id, worker.worker_id)] = worker
        return None

    async def get_worker(self, user_id: str, worker_id: str) -> Optional[ExternalWorker]:
        return self.workers.get((user_id, worker_id))

    async def list_workers(self, user_id: str) -> List[ExternalWorker]:
        return sorted((w for w in self.workers.values() if w.user_id == user_id),
                      key=lambda w: (w.created_at, w.worker_id))

    async def find_active_by_label(self, user_id: str, label: str) -> Optional[ExternalWorker]:
        for w in self.workers.values():
            if w.user_id == user_id and w.label == label and w.is_active:
                return w
        return None

    async def revoke_worker(self, user_id: str, worker_id: str, now: datetime) -> bool:
        worker = self.workers.get((user_id, worker_id))
        if worker is None or not worker.is_active:
            return False
        self.workers[(user_id, worker_id)] = worker.model_copy(update={"status": WorkerStatus.REVOKED,
                                                                        "updated_at": now})
        return True

    async def create_key(self, key: ExternalWorkerKey, max_active: int) -> Optional[TaskRefusal]:
        live = [k for k in self.keys.values()
                if k.user_id == key.user_id and k.worker_id == key.worker_id and not k.is_revoked]
        if len(live) >= max_active:
            return TaskRefusal.KEYS_CAP
        if key.key_digest in self.keys:
            raise AlreadyExists("key exists")
        self.keys[key.key_digest] = key
        return None

    async def get_key(self, key_digest: str) -> Optional[ExternalWorkerKey]:
        self.get_key_reads += 1
        return self.keys.get(key_digest)

    async def list_keys(self, user_id: str, worker_id: str) -> List[ExternalWorkerKey]:
        return sorted((k for k in self.keys.values() if k.user_id == user_id and k.worker_id == worker_id),
                      key=lambda k: (k.created_at, k.key_digest))

    async def revoke_key(self, user_id: str, worker_id: str, key_digest: str, now: datetime) -> bool:
        key = self.keys.get(key_digest)
        if key is None or key.user_id != user_id or key.worker_id != worker_id or key.is_revoked:
            return False
        self.keys[key_digest] = key.model_copy(update={"revoked_at": now})
        return True

    async def touch_key(self, key_digest: str, now: datetime) -> None:
        key = self.keys.get(key_digest)
        if key is not None:
            self.keys[key_digest] = key.model_copy(update={"last_used_at": now})
