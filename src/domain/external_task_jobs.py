"""The `/worker` task types the external task queue enqueues, and their payloads (EXTERNAL_TASK_QUEUE_RFC §5.8, §5.9).

One module on purpose: the three task types, their payload shapes, their dedup keys and what a wake run tells its
runner are one contract between the service that enqueues them and the `/worker` runners that consume them. The service
writes each body as a dict literal at its enqueue site (the `/worker` payload walk reads literals); the runner parses it
with `from_payload`.

`external_task_timer` body: `{"task_type", "user_id", "account_id", "task_id", "kind", "generation", "renewals"}`;
`kind` is `lease`, `deadline` or `hold`; `generation` and `renewals` are the lease's at enqueue time for a lease timer
and 0 otherwise. The runner calls `ExternalTaskService.on_timer(TimerPayload.from_payload(body))`. Dedup keys:
`lease:<task_id>:<generation>:<renewals>` (RFC §5.8), `deadline:<task_id>:<enqueue epoch s>` (the enqueue instant
keeps a deadline timer that re-enqueues itself from meeting its own, still running, name; a second deadline timer of
one task is harmless, its rule finds the task final), `hold:<task_id>`.

`external_task_deliver` body: `{"task_type", "user_id", "account_id", "task_id", "notice"}`; `notice` is `done`,
`failed` or `expired`. Dedup key `deliver:<task_id>` (RFC §5.9): a task reaches one final state, so it has one notice.
The runner reads the task and its result through `ExternalTaskService.load_for_delivery` and records the outcome with
`ExternalTaskService.record_delivery`. Its delivery is a Smart run (a system alert, kind `EXTERNAL_TASK`), so it is
enqueued with the dispatch deadline `DELIVER_DEADLINE_S`: without one Cloud Tasks cuts the run at its 600 s default,
reads the cut as a failure and runs the turn again.

`external_task_wake` body: `{"task_type", "user_id", "account_id", "worker_id", "generation"}`, never task content.
`generation` is the worker's `wake_generation` the enqueue moved it to; dedup key `external_task_wake:<worker_id>:
<generation>` (RFC §5.8): rhythm comes from the stored generation, a stale generation makes the run a no-op. The runner
calls `ExternalTaskService.on_wake(WakePayload.from_payload(body))` and answers by the `WakeRun` it gets back.

Every body names the user, so `/worker` refuses a locked user's timer, notice and wake-up (§6.4).
"""
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional

from .external_task import TaskOrigin
from .wake_refusal import WakeRefusal

EXTERNAL_TASK_TIMER = "external_task_timer"
EXTERNAL_TASK_DELIVER = "external_task_deliver"
EXTERNAL_TASK_WAKE = "external_task_wake"

# `dispatch_deadline_s(NotificationKind.EXTERNAL_TASK)` (`infrastructure/notification_sla.py`): the kind's 600 s budget
# plus the handler's 120 s. Written here because a service may not import the infrastructure layer and the service's
# constructor is at its parameter cap; a test pins it to the SLA table, so a changed budget fails there.
DELIVER_DEADLINE_S = 720


class TimerKind(str, Enum):
    LEASE = "lease"
    DEADLINE = "deadline"
    HOLD = "hold"


class TaskNotice(str, Enum):
    DONE = "done"
    FAILED = "failed"
    EXPIRED = "expired"


@dataclass(frozen=True)
class TimerPayload:
    user_id: str
    account_id: str
    task_id: str
    kind: TimerKind
    generation: int = 0
    renewals: int = 0

    @classmethod
    def from_payload(cls, body: Mapping[str, Any]) -> "TimerPayload":
        """Raises KeyError or ValueError on a body that is not a timer payload."""
        return cls(user_id=str(body["user_id"]), account_id=str(body["account_id"]), task_id=str(body["task_id"]),
                   kind=TimerKind(body["kind"]), generation=int(body.get("generation", 0)),
                   renewals=int(body.get("renewals", 0)))


@dataclass(frozen=True)
class DeliverPayload:
    user_id: str
    account_id: str
    task_id: str
    notice: TaskNotice

    @classmethod
    def from_payload(cls, body: Mapping[str, Any]) -> "DeliverPayload":
        """Raises KeyError or ValueError on a body that is not a delivery payload."""
        return cls(user_id=str(body["user_id"]), account_id=str(body["account_id"]), task_id=str(body["task_id"]),
                   notice=TaskNotice(body["notice"]))


@dataclass(frozen=True)
class WakePayload:
    user_id: str
    account_id: str
    worker_id: str
    generation: int

    @classmethod
    def from_payload(cls, body: Mapping[str, Any]) -> "WakePayload":
        """Raises KeyError, ValueError or TypeError on a body that is not a wake payload."""
        return cls(user_id=str(body["user_id"]), account_id=str(body["account_id"]),
                   worker_id=str(body["worker_id"]), generation=int(body["generation"]))


class WakeOutcome(str, Enum):
    SKIPPED = "skipped"     # stale generation, no target, no queued work, a removed worker: nothing sent
    SENT = "sent"           # the target answered 2xx
    RETRY = "retry"         # the target is down or slow: Cloud Tasks retries
    REFUSED = "refused"     # final: the guard refused the target, or it answered 3xx/4xx


@dataclass(frozen=True)
class WakeRun:
    """What one wake run did. For `refused`, the worker's label and the origin channel of its oldest queued task, so
    the runner can post the owner's notice; for `retry` and `refused`, the closed code."""
    outcome: WakeOutcome
    refusal: Optional[WakeRefusal] = None
    label: Optional[str] = None
    origin: Optional[TaskOrigin] = None


def lease_timer_key(task_id: str, generation: int, renewals: int) -> str:
    return f"lease:{task_id}:{generation}:{renewals}"


def deadline_timer_key(task_id: str, enqueue_epoch_s: int) -> str:
    return f"deadline:{task_id}:{enqueue_epoch_s}"


def hold_timer_key(task_id: str) -> str:
    return f"hold:{task_id}"


def deliver_key(task_id: str) -> str:
    return f"deliver:{task_id}"


def wake_key(worker_id: str, generation: int) -> str:
    return f"external_task_wake:{worker_id}:{generation}"
