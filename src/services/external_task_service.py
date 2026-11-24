"""ExternalTaskService — the one place where the external task queue's rules are applied (EXTERNAL_TASK_QUEUE_RFC §5.2).

Public interface:
    ExternalTaskService(tasks, workers, queue, users, inactive_users, *, digest_key, clock, new_id, random_bytes,
                        cipher=None, webhook=None, own_hosts=())

    Owner side (Smart tools, `$task send`), user id closed over by the caller:
        .create(user_id, worker_label, title, text, origin, *, entry, note) -> CreatedTask
        .confirm(user_id, code) -> TaskSummary
        .is_owner(user_id, task_id) -> bool                              (the Slack Send button's owner check)
        .list_tasks(user_id, status=None, worker_label=None) -> List[TaskSummary]
        .get_task(user_id, task_id) -> TaskView
        .cancel(user_id, task_id) -> TaskSummary
    Worker side, every operation on a `WorkerPrincipal` from a verified credential:
        .resolve_key(raw_key) -> WorkerPrincipal
        .list_for_worker(principal) -> List[WorkerTaskSummary]
        .claim(principal) -> Optional[ClaimedTask]                       (None: no task)
        .renew(principal, task_id, lease_id) -> LeaseGrant
        .submit(principal, task_id, lease_id, text) -> SubmitReceipt
        .release(principal, task_id, lease_id) -> None
        .fail(principal, task_id, lease_id, reason) -> None
    `/worker` runners:
        .on_timer(payload: TimerPayload) -> bool                         (True: the timer changed the task)
        .load_for_delivery(user_id, task_id) -> Optional[DeliveryLoad]
        .record_delivery(user_id, task_id, delivery) -> bool
        .on_wake(payload: WakePayload) -> WakeRun
    Worker registry (Cabinet):
        .create_worker(user_id, label, description=None, destination=WORKSPACE) -> ExternalWorker
        .list_workers(user_id) -> List[ExternalWorker]                   (active only)
        .revoke_worker(user_id, worker_id) -> int                        (tasks cancelled)
        .mint_key(user_id, worker_id) -> MintedKey
        .list_keys(user_id, worker_id) -> List[ExternalWorkerKey]
        .revoke_key(user_id, worker_id, key_digest) -> None
        .save_wake_target(user_id, worker_id, url, headers) -> ExternalWorker   (WakeTargetRefused: the guard's code)
        .clear_wake_target(user_id, worker_id) -> ExternalWorker

Every refusal raises `ExternalTaskRefused(reason)` and writes nothing. Checks, all fresh on every call: the account lock
(`InactiveUserCheck`) then the flag (`UserBotConfig.external_tasks_enabled`, §5.10) for every owner, worker and registry
operation; for a worker operation first the worker record (active, the principal's). Timers and delivery check neither:
`/worker` refuses a locked user, and a task outlives a switched-off flag until its deadline (§5.10). Nor does
`is_owner`, a read of ownership only (its docstring says why).

Every state change is one store transaction applying a rule of `src/domain/external_task_rules.py` (§5.2). Timers and
notices are Cloud Tasks through `TaskQueue.enqueue_worker_task` (payloads: `src/domain/external_task_jobs.py`):
- create / confirm enqueue the deadline or hold timer BEFORE the write: a failed enqueue writes nothing, and a timer
  whose task was never written finds nothing;
- submit enqueues the delivery after the commit and raises if that fails; the worker's retry is an idempotent replay,
  which enqueues it again (dedup `deliver:<task_id>`);
- claim / renew enqueue the lease timer, and claim / fail enqueue their notices, after the commit; a failed enqueue
  there is logged at error level and not raised: the claim's lazy recovery and the deadline bound a lost lease timer,
  and the state change has already happened for the worker.

Wake-up (§5.8). When a task becomes `queued` (a reminder create, a confirm) and it is the oldest queued task of a
worker that has a target and holds no live lease, the worker's `wake_generation` moves on and an `external_task_wake`
Cloud Task is enqueued for that generation (dedup `external_task_wake:<worker_id>:<generation>`); a lease timer that
re-queues a task pings the same way, whether or not other tasks are queued. The generation moves on in its own worker
transaction after the task write, not in the task write's transaction (the two records sit behind two stores): a crash
between the two loses one ping, which the RFC already tolerates for a failed ping (the task stays queued for polling or
the next ping). A failed check or enqueue is logged at error level and never raised: the task is already written.
`on_wake` re-reads the worker: a changed generation, a removed target or worker, or no queued work sends nothing. The
target is decrypted (AAD `external_workers/{user_id}:{worker_id}`) and posted through `OutboundWebhookPort` with the body
`{"event": "tasks_available", "worker": <worker_id>}`: no task text, title, label or count. A target is stored only
after the guard's name-level rules pass (`check_target`, no DNS); the sender runs them again with DNS on every ping.

Logs: `[TaskQueue] <op> user=<8> worker=<fingerprint> task=<id>`; never a task text, a title, a note, a result, a
reason, a key, a code, a wake-up URL or a header.

A task's note is the owner's side only: no worker operation returns it (`ClaimedTask`, `WorkerTaskSummary`).
"""
from __future__ import annotations

import base64
import secrets
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Collection, List, Optional, Sequence, Tuple

from ..domain.external_task import (
    CLAIM_BATCH,
    CLAIM_ROUNDS,
    HOLD_CODE_BYTES,
    HOLD_CODE_DIGEST_PURPOSE,
    HOLD_S,
    KEY_TOUCH_S,
    LIST_MAX,
    MAX_KEYS_PER_WORKER,
    MAX_OPEN_TASKS_PER_USER,
    MAX_WORKERS_PER_USER,
    OPEN_STATUSES,
    TASK_MAX_AGE_S,
    TIMER_MARGIN_S,
    TIMER_MAX_DELAY_S,
    WORKER_VISIBLE_STATUSES,
    DeliveryState,
    ExternalTask,
    ExternalTaskStatus,
    SubmitVia,
    TaskDelivery,
    TaskFailureCode,
    TaskHold,
    TaskOrigin,
    TaskResultMeta,
    WorkerDestination,
)
from ..domain.external_task_entry import TaskEntry
from ..domain.external_task_jobs import (
    DELIVER_DEADLINE_S,
    DeliverPayload,
    TaskNotice,
    TimerKind,
    TimerPayload,
    WakeOutcome,
    WakePayload,
    WakeRun,
    deadline_timer_key,
    deliver_key,
    hold_timer_key,
    lease_timer_key,
    wake_key,
)
from ..domain.external_task_refusal import ExternalTaskRefused, TaskRefusal
from ..domain.external_task_rules import TaskTransition
from ..domain.external_task_text import (
    is_valid_label,
    prepare_description,
    prepare_note,
    prepare_reason,
    prepare_result,
    prepare_task_text,
    prepare_title,
)
from ..domain.external_task_views import (
    ClaimedTask,
    CreatedTask,
    DeliveryLoad,
    LeaseGrant,
    MintedKey,
    SubmitReceipt,
    TaskSummary,
    TaskView,
    WorkerTaskSummary,
)
from ..domain.external_worker import ExternalWorker
from ..domain.external_worker_key import (
    WORKER_KEY_DIGEST_PURPOSE,
    WORKER_KEY_PREFIX,
    WORKER_KEY_RANDOM_BYTES,
    ExternalWorkerKey,
    has_key_shape,
    key_hint,
)
from ..domain.external_worker_principal import WorkerPrincipal
from ..domain.secret_digest import fingerprint, keyed_digest
from ..domain.user import UserProfile
from ..domain.wake_refusal import WakeRefusal, WakeTargetRefused
from ..domain.wake_target import WakeTarget, check_target, wake_aad
from ..ports.cipher_port import CipherPort
from ..ports.external_task_store import ExternalTaskStore
from ..ports.external_worker_store import ExternalWorkerStore
from ..ports.outbound_webhook_port import OutboundWebhookPort
from ..ports.task_queue import TaskQueue
from ..ports.user_repository import UserRepository
from ..utils.logger import logger

if TYPE_CHECKING:
    from .inactive_user_check import InactiveUserCheck

_S = ExternalTaskStatus
_LEASE_ID_BYTES = 16
_CODE_MAX_CHARS = 64


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return secrets.token_hex(8)


def _urlsafe(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _raise_on(outcome: TaskTransition) -> ExternalTask:
    """The task of a transition that was not refused; a refusal raises."""
    if outcome.refusal is not None:
        raise ExternalTaskRefused(outcome.refusal)
    if outcome.task is None:     # an absent task always comes with not_found; kept for the type
        raise ExternalTaskRefused(TaskRefusal.NOT_FOUND)
    return outcome.task


class ExternalTaskService:
    def __init__(self, tasks: ExternalTaskStore, workers: ExternalWorkerStore, queue: TaskQueue,
                 users: UserRepository, inactive_users: "InactiveUserCheck", *, digest_key: bytes,
                 clock: Callable[[], datetime] = _utcnow, new_id: Callable[[], str] = _new_id,
                 random_bytes: Callable[[int], bytes] = secrets.token_bytes, cipher: Optional[CipherPort] = None,
                 webhook: Optional[OutboundWebhookPort] = None, own_hosts: Collection[str] = ()) -> None:
        """`cipher`, `webhook` and `own_hosts` serve the wake-up: without the cipher or the webhook a target cannot be
        stored (`wake_unavailable`) and no ping is sent; `own_hosts` are the bot's own host names (lowercase)."""
        if not digest_key:
            raise ValueError("the external task queue needs a digest key")
        self._tasks = tasks
        self._workers = workers
        self._queue = queue
        self._users = users
        self._inactive_users = inactive_users
        self._digest_key = digest_key
        self._clock = clock
        self._new_id = new_id
        self._random_bytes = random_bytes
        self._cipher = cipher
        self._webhook = webhook
        self._own_hosts = frozenset(h.lower() for h in own_hosts)

    # -- checks ---------------------------------------------------------------------------------------------------

    async def _require_enabled(self, user_id: str) -> UserProfile:
        """The account lock, then the flag; both read fresh. A user that is not stored has no flag."""
        if await self._inactive_users.is_inactive(user_id):
            raise ExternalTaskRefused(TaskRefusal.ACCOUNT_LOCKED)
        user = await self._users.get_user(user_id)
        if user is None or not user.config.external_tasks_enabled:
            raise ExternalTaskRefused(TaskRefusal.NOT_ENABLED)
        return user

    async def _require_worker(self, principal: WorkerPrincipal) -> ExternalWorker:
        """The principal's worker is stored, active and of the principal's account; then the user checks."""
        worker = await self._workers.get_worker(principal.user_id, principal.worker_id)
        if worker is None or not worker.is_active or worker.account_id != principal.account_id:
            raise ExternalTaskRefused(TaskRefusal.WORKER_REVOKED)
        await self._require_enabled(principal.user_id)
        return worker

    async def _own_worker(self, user_id: str, worker_id: str) -> ExternalWorker:
        worker = await self._workers.get_worker(user_id, worker_id)
        if worker is None:
            raise ExternalTaskRefused(TaskRefusal.NOT_FOUND)
        return worker

    def _code_digest(self, code: str) -> str:
        return keyed_digest(self._digest_key, HOLD_CODE_DIGEST_PURPOSE, code)

    def _key_digest(self, key: str) -> str:
        return keyed_digest(self._digest_key, WORKER_KEY_DIGEST_PURPOSE, key)

    def key_digest(self, raw_key: str) -> str:
        """The digest a worker key is stored under; pure (no store read), so a surface can name a key in memory."""
        return self._key_digest(raw_key)

    # -- enqueueing -----------------------------------------------------------------------------------------------

    async def _enqueue_timer(self, payload: TimerPayload, fire_at: datetime, dedup_key: str) -> None:
        delay = max(0, int((fire_at - self._clock()).total_seconds()) + 1)
        # Literals on purpose: the `/worker` trigger and payload walks (tests/support/worker_triggers.py,
        # worker_payload_fields.py) read the task type and the body keys from the call site. The type is
        # EXTERNAL_TASK_TIMER and the body is the one `TimerPayload.from_payload` reads.
        await self._queue.enqueue_worker_task(
            "external_task_timer",
            {"user_id": payload.user_id, "account_id": payload.account_id, "task_id": payload.task_id,
             "kind": payload.kind.value, "generation": payload.generation, "renewals": payload.renewals},
            delay_seconds=min(delay, TIMER_MAX_DELAY_S), dedup_key=dedup_key)

    async def _enqueue_deadline(self, task: ExternalTask) -> None:
        """The deadline timer, at most `TIMER_MAX_DELAY_S` ahead; a timer that fires before the deadline re-enqueues
        itself. The enqueue instant is in the dedup key, so a re-enqueue never meets its own (still running) name."""
        now = self._clock()
        fire_at = min(task.deadline_at + timedelta(seconds=TIMER_MARGIN_S), now + timedelta(seconds=TIMER_MAX_DELAY_S))
        payload = TimerPayload(task.user_id, task.account_id, task.task_id, TimerKind.DEADLINE)
        await self._enqueue_timer(payload, fire_at, deadline_timer_key(task.task_id, int(now.timestamp())))

    async def _enqueue_lease_timer(self, task: ExternalTask) -> None:
        lease = task.lease
        if lease.until is None:
            return
        payload = TimerPayload(task.user_id, task.account_id, task.task_id, TimerKind.LEASE,
                               generation=lease.generation, renewals=lease.renewals)
        try:
            await self._enqueue_timer(payload, lease.until + timedelta(seconds=TIMER_MARGIN_S),
                                      lease_timer_key(task.task_id, lease.generation, lease.renewals))
        except Exception as exc:
            logger.error(f"[TaskQueue] lease timer not enqueued task={task.task_id} error={type(exc).__name__}; "
                         "the next claim recovers the lease lazily")

    async def _enqueue_notice(self, task: ExternalTask, notice: TaskNotice) -> None:
        payload = DeliverPayload(task.user_id, task.account_id, task.task_id, notice)
        # EXTERNAL_TASK_DELIVER and the body `DeliverPayload.from_payload` reads, as literals (see `_enqueue_timer`)
        await self._queue.enqueue_worker_task(
            "external_task_deliver",
            {"user_id": payload.user_id, "account_id": payload.account_id, "task_id": payload.task_id,
             "notice": payload.notice.value},
            dedup_key=deliver_key(task.task_id), deadline_seconds=DELIVER_DEADLINE_S)

    async def _enqueue_notice_logged(self, task: ExternalTask, notice: TaskNotice) -> None:
        try:
            await self._enqueue_notice(task, notice)
        except Exception as exc:
            logger.error(f"[TaskQueue] {notice.value} notice not enqueued task={task.task_id} "
                         f"error={type(exc).__name__}")

    async def _wake_if_due(self, task: ExternalTask, *, oldest_only: bool) -> None:
        """A ping for a task that just became `queued` (§5.8): none while any task of the worker holds an unexpired
        lease (the worker is draining), none when the worker has no target; with `oldest_only`, none unless this task
        is the worker's oldest queued one (an earlier queued task already pinged). Never raises."""
        user_id, worker_id = task.user_id, task.worker_id
        try:
            worker = await self._workers.get_worker(user_id, worker_id)
            if worker is None or not worker.is_active or not worker.has_wake:
                return
            now = self._clock()
            leased = await self._tasks.list_for_worker(user_id, worker_id, (_S.LEASED,))
            if any(t.lease.until is not None and t.lease.until > now for t in leased):
                return
            if oldest_only:
                oldest = await self._tasks.queued_candidates(user_id, worker_id, 1)
                if not oldest or oldest[0].task_id != task.task_id:
                    return
            generation = await self._workers.bump_wake_generation(user_id, worker_id)
            if generation is None:
                return
            # EXTERNAL_TASK_WAKE and the body `WakePayload.from_payload` reads, as literals (see `_enqueue_timer`)
            await self._queue.enqueue_worker_task(
                "external_task_wake",
                {"user_id": user_id, "account_id": task.account_id, "worker_id": worker_id,
                 "generation": generation},
                dedup_key=wake_key(worker_id, generation))
            logger.info(f"[TaskQueue] wake enqueued user={user_id[:8]} worker={fingerprint(worker_id)} "
                        f"generation={generation}")
        except Exception as exc:
            logger.error(f"[TaskQueue] wake not started worker={fingerprint(worker_id)} task={task.task_id} "
                         f"error={type(exc).__name__}; the task stays queued for polling or the next ping")

    # -- owner side -----------------------------------------------------------------------------------------------

    async def create(self, user_id: str, worker_label: str, title: str, text: str, origin: TaskOrigin, *,
                     entry: TaskEntry, note: str) -> CreatedTask:
        """`TaskEntry.CHAT`: `held` with a fresh confirmation code (stored as its keyed digest, returned once) and
        `hold_until = now + HOLD_S`, with the hold timer only; the owner confirms with `$task send`.
        `TaskEntry.REMINDER`: `queued`, with the deadline timer, and no code. The account is the worker's (bound at
        registration). `note` (the bot's return instructions) is required and stored with the entry; checked after the
        title and the text."""
        await self._require_enabled(user_id)
        clean_title = prepare_title(title)
        clean_text = prepare_task_text(text)
        clean_note = prepare_note(note)
        worker = (await self._workers.find_active_by_label(user_id, worker_label)
                  if is_valid_label(worker_label) else None)
        if worker is None:
            raise ExternalTaskRefused(TaskRefusal.UNKNOWN_WORKER)
        if await self._tasks.count_open(user_id) >= MAX_OPEN_TASKS_PER_USER:
            raise ExternalTaskRefused(TaskRefusal.OPEN_TASKS_CAP)
        now = self._clock()
        task = ExternalTask(task_id=self._new_id(), user_id=user_id, account_id=worker.account_id,
                            worker_id=worker.worker_id, label=worker.label, title=clean_title, text=clean_text,
                            origin=origin, status=_S.QUEUED, note=clean_note, entry=entry, created_at=now,
                            updated_at=now,
                            deadline_at=now + timedelta(seconds=TASK_MAX_AGE_S))
        code: Optional[str] = None
        held = entry is TaskEntry.CHAT
        if held:
            code = self._random_bytes(HOLD_CODE_BYTES).hex()
            hold = TaskHold(code_digest=self._code_digest(code), hold_until=now + timedelta(seconds=HOLD_S))
            task = task.model_copy(update={"status": _S.HELD, "hold": hold})
            await self._enqueue_timer(TimerPayload(user_id, task.account_id, task.task_id, TimerKind.HOLD),
                                      hold.hold_until + timedelta(seconds=TIMER_MARGIN_S),
                                      hold_timer_key(task.task_id))
        else:
            await self._enqueue_deadline(task)
        await self._tasks.create(task)
        logger.info(f"[TaskQueue] create user={user_id[:8]} worker={fingerprint(worker.worker_id)} "
                    f"task={task.task_id} status={task.status.value}")
        if not held:
            await self._wake_if_due(task, oldest_only=True)
        return CreatedTask(task_id=task.task_id, status=task.status, label=task.label, title=task.title,
                           text=task.text, note=clean_note, code=code)

    async def confirm(self, user_id: str, code: str) -> TaskSummary:
        """`$task send <code>`: the user's held task with that code, hold not run out, becomes `queued` and gets its
        deadline timer. A wrong or expired code: `hold_not_found`, nothing changed."""
        await self._require_enabled(user_id)
        typed = code.strip().lower()
        if not typed or len(typed) > _CODE_MAX_CHARS:
            raise ExternalTaskRefused(TaskRefusal.HOLD_NOT_FOUND)
        digest = self._code_digest(typed)
        now = self._clock()
        for held in await self._tasks.find_held(user_id, digest):
            if held.hold is None or held.hold.hold_until <= now:
                continue
            await self._enqueue_deadline(held)
            outcome = await self._tasks.confirm(user_id, held.task_id, code_digest=digest, now=now)
            if outcome.written and outcome.task is not None:
                logger.info(f"[TaskQueue] confirm user={user_id[:8]} task={held.task_id}")
                await self._wake_if_due(outcome.task, oldest_only=True)
                return TaskSummary.of(outcome.task)
        raise ExternalTaskRefused(TaskRefusal.HOLD_NOT_FOUND)

    async def is_owner(self, user_id: str, task_id: str) -> bool:
        """Whether `task_id` names a task of `user_id`, in any status. The one owner-side read without the lock and flag
        checks: it says nothing about the task but that it is this user's, and serves the Slack Send button, whose click
        must be known to be the owner's before anything is shown on the owner's message (a click of another user in a
        shared channel changes nothing). The click was authorised by IAM first (the lock), and the release itself is
        `confirm`, with both checks."""
        return await self._tasks.get(user_id, task_id) is not None

    async def list_tasks(self, user_id: str, status: Optional[ExternalTaskStatus] = None,
                         worker_label: Optional[str] = None) -> List[TaskSummary]:
        """The user's newest tasks (at most `LIST_MAX` read), newest first, filtered; never a result or a reason."""
        await self._require_enabled(user_id)
        tasks = await self._tasks.list_for_user(user_id, LIST_MAX)
        return [TaskSummary.of(t) for t in tasks
                if (status is None or t.status is status) and (worker_label is None or t.label == worker_label)]

    async def get_task(self, user_id: str, task_id: str) -> TaskView:
        """One task of the user; a missing and a foreign task are both `not_found`. A done task carries its result and
        a worker-failed task its reason, raw and flagged untrusted (the caller wraps them)."""
        await self._require_enabled(user_id)
        task = await self._tasks.get(user_id, task_id)
        if task is None:
            raise ExternalTaskRefused(TaskRefusal.NOT_FOUND)
        result = await self._tasks.get_result(user_id, task_id) if task.status is _S.DONE else None
        failure = task.failure
        reason = failure.reason if failure is not None and failure.code is TaskFailureCode.WORKER_FAILED else None
        return TaskView(summary=TaskSummary.of(task), failure_code=failure.code if failure is not None else None,
                        result=result, failure_reason=reason)

    async def cancel(self, user_id: str, task_id: str) -> TaskSummary:
        """A non-final task becomes `cancelled`; a final one is `already_final`, a missing or foreign one `not_found`."""
        await self._require_enabled(user_id)
        task = _raise_on(await self._tasks.cancel(user_id, task_id, now=self._clock()))
        logger.info(f"[TaskQueue] cancel user={user_id[:8]} task={task_id}")
        return TaskSummary.of(task)

    # -- worker side ----------------------------------------------------------------------------------------------

    async def resolve_key(self, raw_key: str) -> WorkerPrincipal:
        """A presented worker key → its principal. Shape first (no read for a malformed key), then one digest lookup,
        not revoked, worker active, not locked, flag on; then `last_used_at`, written at most every `KEY_TOUCH_S`.
        A malformed, unknown or revoked key is `key_invalid`."""
        if not isinstance(raw_key, str) or not has_key_shape(raw_key):
            raise ExternalTaskRefused(TaskRefusal.KEY_INVALID)
        digest = self._key_digest(raw_key)
        record = await self._workers.get_key(digest)
        if record is None or record.is_revoked:
            raise ExternalTaskRefused(TaskRefusal.KEY_INVALID)
        principal = WorkerPrincipal(user_id=record.user_id, account_id=record.account_id,
                                    worker_id=record.worker_id, via=SubmitVia.KEY)
        await self._require_worker(principal)
        now = self._clock()
        if record.last_used_at is None or now - record.last_used_at >= timedelta(seconds=KEY_TOUCH_S):
            await self._workers.touch_key(digest, now)
        return principal

    async def list_for_worker(self, principal: WorkerPrincipal) -> List[WorkerTaskSummary]:
        """This worker's `queued` and `leased` tasks, oldest first; a held task is never listed."""
        await self._require_worker(principal)
        tasks = await self._tasks.list_for_worker(principal.user_id, principal.worker_id, WORKER_VISIBLE_STATUSES)
        return [WorkerTaskSummary(task_id=t.task_id, title=t.title, status=t.status, created_at=t.created_at,
                                  lease_until=t.lease.until if t.status is _S.LEASED else None) for t in tasks]

    async def claim(self, principal: WorkerPrincipal) -> Optional[ClaimedTask]:
        """The oldest `queued` task of this worker, else the oldest lapsed lease. A candidate past its deadline is
        expired (notice), one at `MAX_CLAIMS_PER_TASK` fails with `claim_loop` (notice), one another claim took is
        skipped; the next candidate is tried. None when nothing is claimable."""
        await self._require_worker(principal)
        for _ in range(CLAIM_ROUNDS):
            now = self._clock()
            candidates = await self._tasks.queued_candidates(principal.user_id, principal.worker_id, CLAIM_BATCH)
            if not candidates:
                candidates = await self._tasks.lapsed_candidates(principal.user_id, principal.worker_id, now,
                                                                 CLAIM_BATCH)
            if not candidates:
                return None
            for candidate in candidates:
                outcome = await self._tasks.claim(
                    principal.user_id, candidate.task_id, worker_id=principal.worker_id,
                    expected_status=candidate.status, expected_generation=candidate.lease.generation,
                    lease_id=_urlsafe(self._random_bytes(_LEASE_ID_BYTES)), now=now)
                task = outcome.task
                if not outcome.written or task is None:
                    continue
                if task.status is _S.LEASED and task.lease.lease_id is not None and task.lease.until is not None:
                    await self._enqueue_lease_timer(task)
                    logger.info(f"[TaskQueue] claim user={principal.user_id[:8]} "
                                f"worker={fingerprint(principal.worker_id)} task={task.task_id} claims={task.claims}")
                    return ClaimedTask(task_id=task.task_id, created_at=task.created_at, lease_id=task.lease.lease_id,
                                       lease_until=task.lease.until, title=task.title, text=task.text)
                notice = TaskNotice.EXPIRED if task.status is _S.EXPIRED else TaskNotice.FAILED
                logger.info(f"[TaskQueue] claim ended task={task.task_id} status={task.status.value}")
                await self._enqueue_notice_logged(task, notice)
        return None

    async def renew(self, principal: WorkerPrincipal, task_id: str, lease_id: str) -> LeaseGrant:
        """E1: the lease precondition, a live lease, under the renewal cap; `until = now + LEASE_S`."""
        await self._require_worker(principal)
        task = _raise_on(await self._tasks.renew(principal.user_id, task_id, worker_id=principal.worker_id,
                                                 lease_id=lease_id, now=self._clock()))
        await self._enqueue_lease_timer(task)
        until = task.lease.until
        if until is None:      # a renewed lease always has an expiry; kept for the type
            raise ExternalTaskRefused(TaskRefusal.STALE_LEASE)
        return LeaseGrant(task_id=task.task_id, lease_id=lease_id, lease_until=until)

    async def submit(self, principal: WorkerPrincipal, task_id: str, lease_id: str, text: str) -> SubmitReceipt:
        """Normalise and cap the text (§5.9), then one transaction: `done` and the create-only result record. The same
        body again is a replay (same answer, nothing written); another body after done is `already_submitted`.
        Delivery is enqueued on acceptance and on every replay."""
        await self._require_worker(principal)
        prepared = prepare_result(text)
        now = self._clock()
        meta = TaskResultMeta(bytes=prepared.bytes, sha256=prepared.sha256, submitted_at=now,
                              submitted_via=principal.via, client_id=principal.client_id)
        outcome = await self._tasks.submit(principal.user_id, task_id, worker_id=principal.worker_id,
                                           lease_id=lease_id, meta=meta, result_text=prepared.text, now=now)
        task = _raise_on(outcome)
        await self._enqueue_notice(task, TaskNotice.DONE)
        logger.info(f"[TaskQueue] submit user={principal.user_id[:8]} worker={fingerprint(principal.worker_id)} "
                    f"task={task_id} bytes={prepared.bytes} replay={not outcome.written}")
        return SubmitReceipt(task_id=task_id, bytes=prepared.bytes, sha256=prepared.sha256,
                             replay=not outcome.written)

    async def release(self, principal: WorkerPrincipal, task_id: str, lease_id: str) -> None:
        """Under the lease precondition, back to `queued` (counts toward the claim cap through its claim)."""
        await self._require_worker(principal)
        _raise_on(await self._tasks.release(principal.user_id, task_id, worker_id=principal.worker_id,
                                            lease_id=lease_id, now=self._clock()))
        logger.info(f"[TaskQueue] release worker={fingerprint(principal.worker_id)} task={task_id}")

    async def fail(self, principal: WorkerPrincipal, task_id: str, lease_id: str, reason: Optional[str]) -> None:
        """Under the lease precondition, `failed` with the reason stored capped and untrusted; a notice follows."""
        await self._require_worker(principal)
        task = _raise_on(await self._tasks.fail(principal.user_id, task_id, worker_id=principal.worker_id,
                                                lease_id=lease_id, reason=prepare_reason(reason), now=self._clock()))
        logger.info(f"[TaskQueue] fail worker={fingerprint(principal.worker_id)} task={task_id}")
        await self._enqueue_notice_logged(task, TaskNotice.FAILED)

    # -- timers and delivery (/worker) ------------------------------------------------------------------------------

    async def on_timer(self, payload: TimerPayload) -> bool:
        """One `external_task_timer`. Each kind's precondition makes a stale timer a no-op (False):
        - lease: still leased under the payload's generation and lapsed → `queued`, then a ping when due (§5.8);
        - deadline: not final and past `deadline_at` → `expired` with a notice; early → re-enqueued; an expired task
          whose notice is not delivered yet gets it again (a retried timer after a failed enqueue);
        - hold: still held and past `hold_until` → `expired`, silently."""
        now = self._clock()
        user_id, task_id = payload.user_id, payload.task_id
        if payload.kind is TimerKind.LEASE:
            outcome = await self._tasks.expire_lease(user_id, task_id, generation=payload.generation, now=now)
            if outcome.written and outcome.task is not None:
                await self._wake_if_due(outcome.task, oldest_only=False)
        elif payload.kind is TimerKind.HOLD:
            outcome = await self._tasks.expire_hold(user_id, task_id, now=now)
        else:
            outcome = await self._tasks.expire_deadline(user_id, task_id, now=now)
            task = outcome.task
            if task is not None and outcome.written:
                await self._enqueue_notice(task, TaskNotice.EXPIRED)
            elif task is not None and outcome.refusal is TaskRefusal.NOT_DUE:
                await self._enqueue_deadline(task)
            elif (task is not None and task.status is _S.EXPIRED and task.hold is None
                  and (task.delivery is None or task.delivery.state is not DeliveryState.DELIVERED)):
                await self._enqueue_notice(task, TaskNotice.EXPIRED)
        if outcome.written:
            logger.info(f"[TaskQueue] timer kind={payload.kind.value} task={task_id} "
                        f"status={outcome.task.status.value if outcome.task else '-'}")
        return outcome.written

    async def load_for_delivery(self, user_id: str, task_id: str) -> Optional[DeliveryLoad]:
        """A final task of the user and, when done, its stored result; None when absent, foreign or not final."""
        task = await self._tasks.get(user_id, task_id)
        if task is None or not task.is_final:
            return None
        result = await self._tasks.get_result(user_id, task_id) if task.status is _S.DONE else None
        return DeliveryLoad(task=task, result=result)

    async def record_delivery(self, user_id: str, task_id: str, delivery: TaskDelivery) -> bool:
        """The delivery fields of a final task; True when written, False when already delivered (or not final)."""
        outcome = await self._tasks.record_delivery(user_id, task_id, delivery=delivery)
        return outcome.written

    async def on_wake(self, payload: WakePayload) -> WakeRun:
        """One `external_task_wake`. Re-reads the worker: a changed generation, another account, a removed worker or
        target, or no queued work is `skipped` and sends nothing. Otherwise the target is decrypted and pinged:
        `sent` (2xx), `retry` (a retryable code) or `refused` (a final code, with the label and the origin of the oldest
        queued task for the owner's notice). A decrypt or store failure raises (the runner answers 5xx)."""
        user_id, worker_id = payload.user_id, payload.worker_id
        worker = await self._workers.get_worker(user_id, worker_id)
        if (worker is None or not worker.is_active or worker.account_id != payload.account_id
                or not worker.has_wake or worker.wake_secret is None
                or worker.wake_generation != payload.generation):
            return WakeRun(WakeOutcome.SKIPPED)
        queued = await self._tasks.queued_candidates(user_id, worker_id, 1)
        if not queued:
            return WakeRun(WakeOutcome.SKIPPED)
        if self._cipher is None or self._webhook is None:
            logger.error(f"[TaskQueue] wake skipped worker={fingerprint(worker_id)}: no cipher or outbound guard wired")
            return WakeRun(WakeOutcome.SKIPPED)
        target = WakeTarget.from_json(await self._cipher.decrypt(worker.wake_secret, aad=wake_aad(user_id, worker_id)))
        refusal = await self._webhook.post(target, {"event": "tasks_available", "worker": worker_id})
        logger.info(f"[TaskQueue] wake user={user_id[:8]} worker={fingerprint(worker_id)} "
                    f"generation={payload.generation} outcome={refusal.value if refusal else 'sent'}")
        if refusal is None:
            return WakeRun(WakeOutcome.SENT)
        if not refusal.is_final:
            return WakeRun(WakeOutcome.RETRY, refusal=refusal)
        return WakeRun(WakeOutcome.REFUSED, refusal=refusal, label=worker.label, origin=queued[0].origin)

    # -- worker registry (Cabinet) ----------------------------------------------------------------------------------

    async def create_worker(self, user_id: str, label: str, description: Optional[str] = None,
                            destination: WorkerDestination = WorkerDestination.WORKSPACE) -> ExternalWorker:
        """A new active worker of the user's account. The label must be valid and unique among active workers; the
        drive destination is refused until the drive step is built (§5.6)."""
        user = await self._require_enabled(user_id)
        if not is_valid_label(label):
            raise ExternalTaskRefused(TaskRefusal.LABEL_INVALID)
        if destination is not WorkerDestination.WORKSPACE:
            raise ExternalTaskRefused(TaskRefusal.DESTINATION_UNAVAILABLE)
        if not user.account_id:
            raise ExternalTaskRefused(TaskRefusal.NOT_ENABLED)
        now = self._clock()
        worker = ExternalWorker(worker_id=self._new_id(), user_id=user_id, account_id=user.account_id, label=label,
                                description=prepare_description(description), destination=destination,
                                created_at=now, updated_at=now)
        refusal = await self._workers.create_worker(worker, MAX_WORKERS_PER_USER)
        if refusal is not None:
            raise ExternalTaskRefused(refusal)
        logger.info(f"[TaskQueue] worker created user={user_id[:8]} worker={fingerprint(worker.worker_id)}")
        return worker

    async def list_workers(self, user_id: str) -> List[ExternalWorker]:
        await self._require_enabled(user_id)
        return [w for w in await self._workers.list_workers(user_id) if w.is_active]

    async def revoke_worker(self, user_id: str, worker_id: str) -> int:
        """Remove a worker: revoke it, revoke its keys, cancel its non-final tasks. Safe to repeat (a retry after a
        partial failure finishes the cleanup). Returns how many tasks it cancelled."""
        await self._require_enabled(user_id)
        await self._own_worker(user_id, worker_id)
        now = self._clock()
        await self._workers.revoke_worker(user_id, worker_id, now)
        for key in await self._workers.list_keys(user_id, worker_id):
            if not key.is_revoked:
                await self._workers.revoke_key(user_id, worker_id, key.key_digest, now)
        cancelled = 0
        for task in await self._tasks.list_for_worker(user_id, worker_id, OPEN_STATUSES):
            if (await self._tasks.cancel(user_id, task.task_id, now=now)).written:
                cancelled += 1
        logger.info(f"[TaskQueue] worker revoked user={user_id[:8]} worker={fingerprint(worker_id)} "
                    f"cancelled={cancelled}")
        return cancelled

    async def mint_key(self, user_id: str, worker_id: str) -> MintedKey:
        """A new key for an active worker of the user: the raw key is returned once; the store keeps its keyed digest
        and a hint."""
        await self._require_enabled(user_id)
        worker = await self._own_worker(user_id, worker_id)
        if not worker.is_active:
            raise ExternalTaskRefused(TaskRefusal.WORKER_REVOKED)
        raw = WORKER_KEY_PREFIX + _urlsafe(self._random_bytes(WORKER_KEY_RANDOM_BYTES))
        record = ExternalWorkerKey(key_digest=self._key_digest(raw), user_id=user_id, account_id=worker.account_id,
                                   worker_id=worker_id, hint=key_hint(raw), created_at=self._clock())
        refusal = await self._workers.create_key(record, MAX_KEYS_PER_WORKER)
        if refusal is not None:
            raise ExternalTaskRefused(refusal)
        logger.info(f"[TaskQueue] key minted user={user_id[:8]} worker={fingerprint(worker_id)}")
        return MintedKey(key=raw, key_digest=record.key_digest, hint=record.hint, worker_id=worker_id)

    async def list_keys(self, user_id: str, worker_id: str) -> List[ExternalWorkerKey]:
        await self._require_enabled(user_id)
        await self._own_worker(user_id, worker_id)
        return await self._workers.list_keys(user_id, worker_id)

    async def revoke_key(self, user_id: str, worker_id: str, key_digest: str) -> None:
        await self._require_enabled(user_id)
        if not await self._workers.revoke_key(user_id, worker_id, key_digest, self._clock()):
            raise ExternalTaskRefused(TaskRefusal.NOT_FOUND)
        logger.info(f"[TaskQueue] key revoked user={user_id[:8]} worker={fingerprint(worker_id)}")

    async def save_wake_target(self, user_id: str, worker_id: str, url: str,
                              headers: Sequence[Tuple[str, str]]) -> ExternalWorker:
        """Store an active worker's wake-up target (§5.6, E3), replacing any earlier one whole. The guard's name-level
        rules run first (no DNS): a refused target raises `WakeTargetRefused` with its code and stores nothing. The URL
        and headers are stored only as one ciphertext bound to the record; `wake_host` is the plaintext part."""
        await self._require_enabled(user_id)
        worker = await self._own_worker(user_id, worker_id)
        if not worker.is_active:
            raise ExternalTaskRefused(TaskRefusal.WORKER_REVOKED)
        if self._cipher is None or self._webhook is None:
            raise ExternalTaskRefused(TaskRefusal.WAKE_UNAVAILABLE)
        target = WakeTarget(url=url.strip(), headers=tuple(headers))
        checked = check_target(target, self._own_hosts)
        if isinstance(checked, WakeRefusal):
            logger.info(f"[TaskQueue] wake target refused user={user_id[:8]} worker={fingerprint(worker_id)} "
                        f"code={checked.value}")
            raise WakeTargetRefused(checked)
        secret = await self._cipher.encrypt(target.to_json(), aad=wake_aad(user_id, worker_id))
        if not await self._workers.set_wake(user_id, worker_id, wake_host=checked.host, wake_secret=secret,
                                            now=self._clock()):
            raise ExternalTaskRefused(TaskRefusal.WORKER_REVOKED)
        logger.info(f"[TaskQueue] wake target set user={user_id[:8]} worker={fingerprint(worker_id)}")
        return await self._own_worker(user_id, worker_id)

    async def clear_wake_target(self, user_id: str, worker_id: str) -> ExternalWorker:
        """Remove an active worker's wake-up target; the worker then polls. A pending ping finds no target and sends
        nothing."""
        await self._require_enabled(user_id)
        worker = await self._own_worker(user_id, worker_id)
        if not worker.is_active or not await self._workers.set_wake(user_id, worker_id, wake_host=None,
                                                                    wake_secret=None, now=self._clock()):
            raise ExternalTaskRefused(TaskRefusal.WORKER_REVOKED)
        logger.info(f"[TaskQueue] wake target cleared user={user_id[:8]} worker={fingerprint(worker_id)}")
        return await self._own_worker(user_id, worker_id)
