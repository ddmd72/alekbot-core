"""What `ExternalTaskService` returns to its callers (EXTERNAL_TASK_QUEUE_RFC §5.2, §5.3, §5.4, §5.6).

One module on purpose: these are the read shapes of one service, each a few fields, consumed by the Smart tools, the
`$task send` command, the worker surfaces and the Cabinet.

What each shape leaves out is the contract:
- `TaskSummary` (owner listings) never carries a result or a failure reason;
- `TaskView` (owner `get`) carries the result and the failure reason as raw stored text: the
  Smart tool wraps them (`wrap_untrusted(store=workspace)` after `EXTERNAL_REMINDER`); nothing here wraps;
- `WorkerTaskSummary` (worker listings) carries the lease's expiry but never its id;
- `MintedKey` carries the raw key once; nothing stores or returns it again.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .external_task import ExternalTask, ExternalTaskStatus, TaskFailureCode


@dataclass(frozen=True)
class CreatedTask:
    """`code` is set for a held task only: the raw confirmation code for `$task send <code>`, returned this once.
    `title`, `text` and `note` are the stored (normalised) forms the receipt quotes in full."""
    task_id: str
    status: ExternalTaskStatus
    label: str
    title: str
    text: str
    note: str
    code: Optional[str] = None


@dataclass(frozen=True)
class TaskSummary:
    task_id: str
    label: str
    title: str
    status: ExternalTaskStatus
    created_at: datetime
    updated_at: datetime
    deadline_at: datetime

    @classmethod
    def of(cls, task: ExternalTask) -> "TaskSummary":
        return cls(task_id=task.task_id, label=task.label, title=task.title, status=task.status,
                   created_at=task.created_at, updated_at=task.updated_at, deadline_at=task.deadline_at)


@dataclass(frozen=True)
class TaskView:
    """One task for its owner. `result` (done) and `failure_reason` (failed by the worker) are worker text, untrusted
    by assumption: the Smart tool wraps them."""
    summary: TaskSummary
    failure_code: Optional[TaskFailureCode] = None
    result: Optional[str] = None
    failure_reason: Optional[str] = None


@dataclass(frozen=True)
class WorkerTaskSummary:
    task_id: str
    title: str
    status: ExternalTaskStatus
    created_at: datetime
    lease_until: Optional[datetime] = None


@dataclass(frozen=True)
class ClaimedTask:
    """What a claim hands the worker: provenance fields first, then the text as its own field (§5.4)."""
    task_id: str
    created_at: datetime
    lease_id: str
    lease_until: datetime
    title: str
    text: str


@dataclass(frozen=True)
class LeaseGrant:
    task_id: str
    lease_id: str
    lease_until: datetime


@dataclass(frozen=True)
class SubmitReceipt:
    """`replay`: the same body was already stored for this task; nothing changed."""
    task_id: str
    bytes: int
    sha256: str
    replay: bool = False


@dataclass(frozen=True)
class MintedKey:
    """`key` is shown once; `key_digest` addresses the key afterwards (revoke), `hint` is what the Cabinet shows."""
    key: str
    key_digest: str
    hint: str
    worker_id: str


@dataclass(frozen=True)
class DeliveryLoad:
    """A final task and, when done, its stored result: what the delivery runner writes and announces."""
    task: ExternalTask
    result: Optional[str] = None
