"""The `/worker` task types the external task queue enqueues, and their payloads (EXTERNAL_TASK_QUEUE_RFC §5.8, §5.9).

One module on purpose: the two task types, their payload shapes and their dedup keys are one contract between the
service that enqueues them and the `/worker` runners that consume them (built by later parts). The service writes each
body as a dict literal at its enqueue site (the `/worker` payload walk reads literals); the runner parses it with
`from_payload`.

`external_task_timer` body: `{"task_type", "user_id", "account_id", "task_id", "kind", "generation", "renewals"}`;
`kind` is `lease`, `deadline` or `hold`; `generation` and `renewals` are the lease's at enqueue time for a lease timer
and 0 otherwise. The runner calls `ExternalTaskService.on_timer(TimerPayload.from_payload(body))`. Dedup keys:
`lease:<task_id>:<generation>:<renewals>` (RFC §5.8), `deadline:<task_id>:<enqueue epoch s>` (the enqueue instant
keeps a deadline timer that re-enqueues itself from meeting its own, still running, name; a second deadline timer of
one task is harmless, its rule finds the task final), `hold:<task_id>`.

`external_task_deliver` body: `{"task_type", "user_id", "account_id", "task_id", "notice"}`; `notice` is `done`,
`failed` or `expired`. Dedup key `deliver:<task_id>` (RFC §5.9): a task reaches one final state, so it has one notice.
The runner reads the task and its result through `ExternalTaskService.load_for_delivery` and records the outcome with
`ExternalTaskService.record_delivery`.

Both bodies name the user, so `/worker` refuses a locked user's timer and notice (§6.4).
"""
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

EXTERNAL_TASK_TIMER = "external_task_timer"
EXTERNAL_TASK_DELIVER = "external_task_deliver"


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


def lease_timer_key(task_id: str, generation: int, renewals: int) -> str:
    return f"lease:{task_id}:{generation}:{renewals}"


def deadline_timer_key(task_id: str, enqueue_epoch_s: int) -> str:
    return f"deadline:{task_id}:{enqueue_epoch_s}"


def hold_timer_key(task_id: str) -> str:
    return f"hold:{task_id}"


def deliver_key(task_id: str) -> str:
    return f"deliver:{task_id}"
