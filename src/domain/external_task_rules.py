"""The external task queue's transition rules, as pure functions (EXTERNAL_TASK_QUEUE_RFC §5.1, §5.2, §5.8).

Each function takes a task as one store transaction read it and returns a `TaskTransition`: the task to write, or the
refusal and the task as found (nothing written), or the task unchanged (an idempotent replay). The stores
(`ExternalTaskStore` implementations) call exactly these functions inside their transaction, so a precondition is
evaluated on the state the commit is conditioned on, and the Firestore adapter and the test fake share one rule set.

Tenancy by user is the store's (an absent or foreign task is `not_found` before any rule runs); tenancy by worker is
here: a task of another worker, and a held task, are `not_found` to a worker (§5.4, §5.7).

The worker precondition (§5.2) is `status = leased ∧ lease.lease_id = <presented>`. A lease id is minted fresh by each
claim and each claim moves `lease.generation` on, so a matching lease id pins the generation stored at that claim.
A lapsed lease that nobody re-claimed still satisfies it (status still `leased`), until the lease timer or a claim ends
it. Every final transition sets `purge_after = now + RETENTION_S`.

Public interface:
    TaskTransition
    lease_refusal(task, worker_id, lease_id) -> Optional[TaskRefusal]
    file_refusal(task, *, worker_id, lease_id, now) -> Optional[TaskRefusal]      (a read: a link to a task's file)
    claim(task, *, worker_id, expected_status, expected_generation, lease_id, now)
    renew(task, *, worker_id, lease_id, now);  submit(task, *, worker_id, lease_id, meta, now)
    release(task, *, worker_id, lease_id, now);  fail(task, *, worker_id, lease_id, reason, now)
    cancel(task, *, now);  confirm(task, *, code_digest, now)
    expire_lease(task, *, generation, now);  expire_deadline(task, *, now);  expire_hold(task, *, now)
    record_delivery(task, *, delivery)
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from .external_task import (
    LEASE_S,
    MAX_CLAIMS_PER_TASK,
    MAX_RENEWALS_PER_TASK,
    RETENTION_S,
    DeliveryState,
    ExternalTask,
    ExternalTaskStatus,
    TaskDelivery,
    TaskFailure,
    TaskFailureCode,
    TaskLease,
    TaskResultMeta,
)
from .external_task_refusal import TaskRefusal

_S = ExternalTaskStatus


@dataclass(frozen=True)
class TaskTransition:
    """`written`: the store wrote `task` (the state after). Otherwise `task` is the state as found and `refusal` says
    why nothing was written; no refusal and nothing written is an idempotent replay. `task` is None only for an absent
    or foreign task (`not_found`)."""
    task: Optional[ExternalTask]
    written: bool = False
    refusal: Optional[TaskRefusal] = None

    @property
    def ok(self) -> bool:
        return self.refusal is None

    @classmethod
    def write(cls, task: ExternalTask) -> "TaskTransition":
        return cls(task=task, written=True)

    @classmethod
    def refuse(cls, task: Optional[ExternalTask], reason: TaskRefusal) -> "TaskTransition":
        return cls(task=task, refusal=reason)

    @classmethod
    def unchanged(cls, task: ExternalTask) -> "TaskTransition":
        return cls(task=task)


def _update(task: ExternalTask, now: datetime, **changes: Any) -> ExternalTask:
    return task.model_copy(update={**changes, "updated_at": now})


def _finalise(task: ExternalTask, status: ExternalTaskStatus, now: datetime, **changes: Any) -> ExternalTask:
    return _update(task, now, status=status, purge_after=now + timedelta(seconds=RETENTION_S), **changes)


def _worker_sees(task: ExternalTask, worker_id: str) -> bool:
    return task.worker_id == worker_id and task.status is not _S.HELD


def lease_refusal(task: ExternalTask, worker_id: str, lease_id: str) -> Optional[TaskRefusal]:
    """The worker precondition: None when it holds, else `not_found` (another worker's or a held task) or
    `stale_lease` (any other status, or another lease)."""
    if not _worker_sees(task, worker_id):
        return TaskRefusal.NOT_FOUND
    if task.status is not _S.LEASED or not lease_id or task.lease.lease_id != lease_id:
        return TaskRefusal.STALE_LEASE
    return None


def _lease_lapsed(task: ExternalTask, now: datetime) -> bool:
    return task.status is _S.LEASED and task.lease.until is not None and task.lease.until <= now


def file_refusal(task: ExternalTask, *, worker_id: str, lease_id: str, now: datetime) -> Optional[TaskRefusal]:
    """Whether the worker holding `lease_id` may have a link to one of the task's files (§5.12): None when it may. The
    lease is the right to the files, checked as `renew` checks it: a final task is `not_found` (as for a missing task:
    the files end with the task), then the worker precondition (`not_found` for another worker's or a held task,
    `stale_lease`), then a lapsed lease is `lease_expired`. A read: nothing is written either way."""
    if task.is_final:
        return TaskRefusal.NOT_FOUND
    refusal = lease_refusal(task, worker_id, lease_id)
    if refusal is not None:
        return refusal
    if _lease_lapsed(task, now):
        return TaskRefusal.LEASE_EXPIRED
    return None


def claim(task: ExternalTask, *, worker_id: str, expected_status: ExternalTaskStatus, expected_generation: int,
          lease_id: str, now: datetime) -> TaskTransition:
    """A claim of one candidate. Precondition: the status and lease generation the candidate query saw are unchanged
    and the task is still claimable (`queued`, or `leased` with `until <= now`); else `contended`. A candidate past its
    `deadline_at` is expired, not claimed; one whose `claims` reached `MAX_CLAIMS_PER_TASK` fails with `claim_loop`.
    Otherwise: `leased`, the new lease id, `until = now + LEASE_S`, generation + 1, renewals 0, claims + 1."""
    if not _worker_sees(task, worker_id):
        return TaskTransition.refuse(task, TaskRefusal.NOT_FOUND)
    if task.status is not expected_status or task.lease.generation != expected_generation:
        return TaskTransition.refuse(task, TaskRefusal.CONTENDED)
    if not (task.status is _S.QUEUED or _lease_lapsed(task, now)):
        return TaskTransition.refuse(task, TaskRefusal.CONTENDED)
    if task.deadline_at <= now:
        return TaskTransition.write(_finalise(task, _S.EXPIRED, now))
    if task.claims >= MAX_CLAIMS_PER_TASK:
        return TaskTransition.write(_finalise(task, _S.FAILED, now,
                                              failure=TaskFailure(code=TaskFailureCode.CLAIM_LOOP)))
    lease = TaskLease(lease_id=lease_id, until=now + timedelta(seconds=LEASE_S),
                      generation=task.lease.generation + 1, renewals=0)
    return TaskTransition.write(_update(task, now, status=_S.LEASED, lease=lease, claims=task.claims + 1))


def renew(task: ExternalTask, *, worker_id: str, lease_id: str, now: datetime) -> TaskTransition:
    """The worker precondition, plus `until > now` (`lease_expired`: a lapsed lease is not revived) and
    `renewals < MAX_RENEWALS_PER_TASK` (`renew_cap`). Sets `until = now + LEASE_S`, renewals + 1."""
    refusal = lease_refusal(task, worker_id, lease_id)
    if refusal is not None:
        return TaskTransition.refuse(task, refusal)
    if _lease_lapsed(task, now):
        return TaskTransition.refuse(task, TaskRefusal.LEASE_EXPIRED)
    if task.lease.renewals >= MAX_RENEWALS_PER_TASK:
        return TaskTransition.refuse(task, TaskRefusal.RENEW_CAP)
    lease = task.lease.model_copy(update={"until": now + timedelta(seconds=LEASE_S),
                                          "renewals": task.lease.renewals + 1})
    return TaskTransition.write(_update(task, now, lease=lease))


def submit(task: ExternalTask, *, worker_id: str, lease_id: str, meta: TaskResultMeta,
           now: datetime) -> TaskTransition:
    """Accepted when the worker precondition holds (a lapsed lease nobody re-claimed included): `done` with `meta`.
    A `done` task whose stored sha256 equals `meta.sha256` is an idempotent replay (unchanged, no refusal); a `done`
    task with another body is `already_submitted`; anything else is the worker precondition's refusal."""
    if not _worker_sees(task, worker_id):
        return TaskTransition.refuse(task, TaskRefusal.NOT_FOUND)
    if task.status is _S.DONE:
        if task.result_meta is not None and task.result_meta.sha256 == meta.sha256:
            return TaskTransition.unchanged(task)
        return TaskTransition.refuse(task, TaskRefusal.ALREADY_SUBMITTED)
    refusal = lease_refusal(task, worker_id, lease_id)
    if refusal is not None:
        return TaskTransition.refuse(task, refusal)
    return TaskTransition.write(_finalise(task, _S.DONE, now, result_meta=meta))


def _end_lease(task: ExternalTask) -> TaskLease:
    return TaskLease(generation=task.lease.generation + 1, renewals=0)


def release(task: ExternalTask, *, worker_id: str, lease_id: str, now: datetime) -> TaskTransition:
    """Under the worker precondition: back to `queued`, the lease cleared and its generation moved on. The release
    counts toward `MAX_CLAIMS_PER_TASK` through the claim before it."""
    refusal = lease_refusal(task, worker_id, lease_id)
    if refusal is not None:
        return TaskTransition.refuse(task, refusal)
    return TaskTransition.write(_update(task, now, status=_S.QUEUED, lease=_end_lease(task)))


def fail(task: ExternalTask, *, worker_id: str, lease_id: str, reason: Optional[str],
         now: datetime) -> TaskTransition:
    """Under the worker precondition: `failed` with code `worker_failed` and the (already prepared) reason."""
    refusal = lease_refusal(task, worker_id, lease_id)
    if refusal is not None:
        return TaskTransition.refuse(task, refusal)
    return TaskTransition.write(_finalise(task, _S.FAILED, now,
                                          failure=TaskFailure(code=TaskFailureCode.WORKER_FAILED, reason=reason)))


def cancel(task: ExternalTask, *, now: datetime) -> TaskTransition:
    """A `held`, `queued` or `leased` task becomes `cancelled`; a final one is `already_final`."""
    if task.is_final:
        return TaskTransition.refuse(task, TaskRefusal.ALREADY_FINAL)
    return TaskTransition.write(_finalise(task, _S.CANCELLED, now))


def confirm(task: ExternalTask, *, code_digest: str, now: datetime) -> TaskTransition:
    """`$task send`: a `held` task whose code digest matches and whose hold has not run out becomes `queued`; anything
    else is `hold_not_found` (one answer for a wrong and an expired code)."""
    hold = task.hold
    if task.status is not _S.HELD or hold is None or hold.code_digest != code_digest or hold.hold_until <= now:
        return TaskTransition.refuse(task, TaskRefusal.HOLD_NOT_FOUND)
    return TaskTransition.write(_update(task, now, status=_S.QUEUED, hold=None))


def expire_lease(task: ExternalTask, *, generation: int, now: datetime) -> TaskTransition:
    """The lease timer: still `leased` under `generation` and `until <= now` → back to `queued`, the lease cleared and
    its generation moved on. Anything else (renewed, submitted, released, failed, re-claimed) is `not_due`."""
    if not (_lease_lapsed(task, now) and task.lease.generation == generation):
        return TaskTransition.refuse(task, TaskRefusal.NOT_DUE)
    return TaskTransition.write(_update(task, now, status=_S.QUEUED, lease=_end_lease(task)))


def expire_deadline(task: ExternalTask, *, now: datetime) -> TaskTransition:
    """The deadline timer: a non-final task at or past `deadline_at` becomes `expired`. Final: `already_final`;
    before the deadline: `not_due` (the caller re-enqueues the timer)."""
    if task.is_final:
        return TaskTransition.refuse(task, TaskRefusal.ALREADY_FINAL)
    if task.deadline_at > now:
        return TaskTransition.refuse(task, TaskRefusal.NOT_DUE)
    return TaskTransition.write(_finalise(task, _S.EXPIRED, now))


def expire_hold(task: ExternalTask, *, now: datetime) -> TaskTransition:
    """The hold timer: a `held` task at or past `hold_until` becomes `expired` (silently). Anything else: `not_due`."""
    if task.status is not _S.HELD or task.hold is None or task.hold.hold_until > now:
        return TaskTransition.refuse(task, TaskRefusal.NOT_DUE)
    return TaskTransition.write(_finalise(task, _S.EXPIRED, now))


def record_delivery(task: ExternalTask, *, delivery: TaskDelivery) -> TaskTransition:
    """The delivery fields of a final task, and nothing else (not even `updated_at`: a final task never changes again
    but for these). A task not final is `not_due`; a task already `delivered` is unchanged."""
    if not task.is_final:
        return TaskTransition.refuse(task, TaskRefusal.NOT_DUE)
    if task.delivery is not None and task.delivery.state is DeliveryState.DELIVERED:
        return TaskTransition.unchanged(task)
    return TaskTransition.write(task.model_copy(update={"delivery": delivery}))
