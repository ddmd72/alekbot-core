"""The external task record, its embedded value objects and the queue's limits (EXTERNAL_TASK_QUEUE_RFC §5.1, §5.11).

One module on purpose: `ExternalTask` and the value objects it embeds (`TaskOrigin`, `TaskHold`, `TaskLease`,
`TaskResultMeta`, `TaskFailure`, `TaskDelivery`) and their enums are one stored record, read and written whole; splitting
them would only scatter one document's shape over seven files.

Life of a task (§5.1): `held` (a chat create waits for the owner's `$task send`) or `queued` (a reminder create, or the confirmed `$task send`), `leased` by a claim, then
one of the final states `done`, `failed`, `cancelled`, `expired`. A final task never changes again except for its
`delivery` fields. The transitions themselves are the pure functions of `external_task_rules`.

The limits are named constants (E7). Every number below is a round placeholder, set at implementation review; none was
measured anywhere.
"""
from datetime import datetime
from enum import Enum
from typing import FrozenSet, Optional

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------------------------------- limits (§5.11)

TITLE_MAX_CHARS = 120                   # set at implementation review
TASK_TEXT_MAX_CHARS = 3_000             # set at implementation review; the whole text must fit one literal receipt
RESULT_MAX_BYTES = 512_000              # set at implementation review; below the 1 MiB Firestore document limit
FAIL_REASON_MAX_CHARS = 1_000           # set at implementation review
DESCRIPTION_MAX_CHARS = 200             # set at implementation review; a worker's one-line description
MAX_OPEN_TASKS_PER_USER = 20            # set at implementation review; held tasks count
MAX_WORKERS_PER_USER = 10               # set at implementation review; active workers
MAX_KEYS_PER_WORKER = 3                 # set at implementation review; unrevoked keys
LABEL_MAX_CHARS = 32                    # set at implementation review
LABEL_PATTERN = r"[a-z0-9][a-z0-9_-]*"  # set at implementation review; labels are also the Smart tool's enum
LEASE_S = 900                           # set at implementation review; one lease, and one renewal's extension
MAX_RENEWALS_PER_TASK = 8               # set at implementation review; within one lease
MAX_CLAIMS_PER_TASK = 5                 # set at implementation review; releases and expiries count through their claims
HOLD_S = 86_400                         # set at implementation review; how long a held task waits for `$task send`
TASK_MAX_AGE_S = 7 * 86_400             # set at implementation review; deadline_at = created_at + this
RETENTION_S = 30 * 86_400               # set at implementation review; purge_after = final time + this
CLAIMS_PER_WINDOW = 60                  # set at implementation review; per key / per grant, read by the surfaces
SUBMITS_PER_WINDOW = 60                 # set at implementation review
CALLS_PER_WINDOW = 600                  # set at implementation review
RATE_WINDOW_S = 3_600                   # set at implementation review
UNAUTHORIZED_PER_ADDRESS_PER_WINDOW = 20    # set at implementation review
UNAUTHORIZED_GLOBAL_PER_WINDOW = 500        # set at implementation review
WAKE_TIMEOUT_S = 10                     # set at implementation review; one wake-up send, DNS included
WAKE_HEADERS_MAX = 8                    # set at implementation review
WAKE_HEADER_BYTES_MAX = 1_024           # set at implementation review; names and values of all headers together

# Engineering constants of this implementation (not in the RFC's table).
TIMER_MARGIN_S = 30                     # set at implementation review; a timer fires this long after its instant
TIMER_MAX_DELAY_S = 29 * 86_400         # Cloud Tasks schedules at most 30 days ahead; a later timer re-enqueues itself
KEY_TOUCH_S = 300                       # set at implementation review; `last_used_at` is written at most this often
LIST_MAX = 50                           # set at implementation review; the newest tasks a listing reads
CLAIM_BATCH = 10                        # set at implementation review; candidates one claim query reads
CLAIM_ROUNDS = 3                        # set at implementation review; candidate queries one claim may run
HOLD_CODE_BYTES = 4                     # set at implementation review; the code is 2 × this many hex characters
WAKE_URL_MAX_CHARS = 2_048              # set at implementation review; a wake-up URL
WAKE_RESPONSE_READ_MAX = 4_096          # set at implementation review; bytes of a wake-up answer read, then discarded
WAKE_MAX_ATTEMPTS = 5                   # set at implementation review; deliveries of one wake task, retries included

HOLD_CODE_DIGEST_PURPOSE = "external_task_hold"


class ExternalTaskStatus(str, Enum):
    HELD = "held"
    QUEUED = "queued"
    LEASED = "leased"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


FINAL_STATUSES: FrozenSet[ExternalTaskStatus] = frozenset({
    ExternalTaskStatus.DONE, ExternalTaskStatus.FAILED, ExternalTaskStatus.CANCELLED, ExternalTaskStatus.EXPIRED})
OPEN_STATUSES: FrozenSet[ExternalTaskStatus] = frozenset({
    ExternalTaskStatus.HELD, ExternalTaskStatus.QUEUED, ExternalTaskStatus.LEASED})
# What a worker may see: a held task is invisible to every worker surface (§5.4).
WORKER_VISIBLE_STATUSES: FrozenSet[ExternalTaskStatus] = frozenset({
    ExternalTaskStatus.QUEUED, ExternalTaskStatus.LEASED})


class TaskFailureCode(str, Enum):
    WORKER_FAILED = "worker_failed"     # the worker called `fail`
    CLAIM_LOOP = "claim_loop"           # the claim after MAX_CLAIMS_PER_TASK


class SubmitVia(str, Enum):
    KEY = "key"
    OAUTH = "oauth"


class DeliveryState(str, Enum):
    PENDING = "pending"
    DELIVERED = "delivered"


class WorkerDestination(str, Enum):
    WORKSPACE = "workspace"
    DRIVE = "drive"


class TaskOrigin(BaseModel):
    """The creating turn's session and channel; notices go there."""
    model_config = ConfigDict(frozen=True)

    platform: str
    channel_id: str
    session_id: str


class TaskHold(BaseModel):
    """A held task's confirmation: the code's keyed digest (never the code) and when the hold runs out."""
    model_config = ConfigDict(frozen=True)

    code_digest: str
    hold_until: datetime


class TaskLease(BaseModel):
    """`generation` moves on at every claim and every lease end (release, lease expiry); `lease_id` and `until` are set
    only while the task is leased; `renewals` counts the renewals of the current lease."""
    model_config = ConfigDict(frozen=True)

    lease_id: Optional[str] = None
    until: Optional[datetime] = None
    generation: int = 0
    renewals: int = 0


class TaskResultMeta(BaseModel):
    """What is known of the stored result without reading its body."""
    model_config = ConfigDict(frozen=True)

    bytes: int
    sha256: str
    submitted_at: datetime
    submitted_via: SubmitVia
    client_id: Optional[str] = None


class TaskFailure(BaseModel):
    """`reason` is the worker's text: normalised, capped and untrusted; never quoted in a notice."""
    model_config = ConfigDict(frozen=True)

    code: TaskFailureCode
    reason: Optional[str] = None


class TaskDelivery(BaseModel):
    """Filled by the delivery task (§5.9); the only fields a final task may still change."""
    model_config = ConfigDict(frozen=True)

    state: DeliveryState = DeliveryState.PENDING
    destination: Optional[WorkerDestination] = None
    path: Optional[str] = None
    delivered_at: Optional[datetime] = None


class ExternalTask(BaseModel):
    """One task (`{prefix}external_tasks`, doc id `task_id`). The result body lives apart, in the result record."""
    model_config = ConfigDict(frozen=True)

    task_id: str
    user_id: str
    account_id: str
    worker_id: str
    label: str
    title: str
    text: str
    origin: TaskOrigin
    status: ExternalTaskStatus
    hold: Optional[TaskHold] = None
    lease: TaskLease = TaskLease()
    claims: int = 0
    result_meta: Optional[TaskResultMeta] = None
    failure: Optional[TaskFailure] = None
    delivery: Optional[TaskDelivery] = None
    created_at: datetime
    updated_at: datetime
    deadline_at: datetime
    purge_after: Optional[datetime] = None

    @property
    def is_final(self) -> bool:
        return self.status in FINAL_STATUSES
