"""The `memory_inbox_final` `/worker` task: the final pass over one user's confirmed candidates (MCP_MEMORY_SUBMISSIONS_RFC
§9.4, §10.3).

The final pass is the only writer of facts from the inbox; the model work is the injected `FinalPass`. This runner is the
rhythm around it. Each "True" of the owner moves `final_due_at` on the state document an hour ahead and enqueues one task
for it, so a burst of confirmations enqueues N tasks and only the last finds `now >= final_due_at`; the others answer
`not_due`. A due task takes the account's memory-write lease (busy: one named deferral), runs the pass under it, runs the
profile step inside the lease after a pass that wrote (as the review and the consolidation do), and clears `final_due_at`
only when it is still the value this run read: a confirmation made during the run keeps its own pending task.

A pass that failed (or ended only partly) leaves its candidates `confirmed` (the pass counts the attempt on each) and
`final_due_at` set, and asks for one more run an hour later while the state's retry count is below its limit; after it the
sweep's backstop takes over the next day.

The account comes from the user's profile only. Logs carry ids and counts, never candidate text.
"""
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple, TYPE_CHECKING

from ..domain.memory_inbox import FINAL_DELAY, FINAL_MAX_ATTEMPTS
from ..domain.memory_inbox_runs import FinalOutcome, FinalPass, PassRequest
from ..domain.memory_inbox_task_keys import FINAL_TASK, INBOX_DEADLINE_S, final_defer_key, task_payload
from ..domain.memory_write_lease import LEASE_BUSY_RETRY_DELAY_S, lease_until
from ..ports.memory_inbox_repository import MemoryInboxRepository
from ..ports.memory_write_lease import MemoryWriteLeasePort
from ..ports.task_queue import TaskQueue
from ..ports.user_repository import UserRepository
from ..utils.logger import logger

if TYPE_CHECKING:
    from .owner_timezone_resolver import OwnerTimezoneResolver


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _retry_key(user_id: str, due: datetime, attempts: int) -> str:
    """The one more run after a failed one: the confirmation's own key is taken by the task that just ran, so the retry is
    named by the failed-run count too (one name per user, due time and attempt)."""
    return f"memory-inbox-final-retry:{user_id}:{int(due.timestamp())}:{attempts}"


class MemoryInboxFinalTask:
    """Runs one delivery of `memory_inbox_final`; a `WorkerTask`.

    `profile_step` is the fact profile step (`run(account_id, deadline)`, never raises) or None; `owner_zones` resolves
    the owner's zone for the pass's date rules.
    """

    def __init__(self, *, store: MemoryInboxRepository, final_pass: FinalPass, lease: MemoryWriteLeasePort,
                 task_queue: TaskQueue, user_repo: UserRepository, owner_zones: "OwnerTimezoneResolver",
                 profile_step: Optional[Any], run_timeout_s: float, clock: Callable[[], datetime] = _utcnow) -> None:
        self._store = store
        self._pass = final_pass
        self._lease = lease
        self._task_queue = task_queue
        self._user_repo = user_repo
        self._owner_zones = owner_zones
        self._profile_step = profile_step
        self._run_timeout_s = run_timeout_s
        self._clock = clock

    async def __call__(self, payload: Dict[str, Any], headers: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
        """400 for a payload without a user; else 200 with `status` `not_due` (a later confirmation moved the run, or
        nothing is due), `deferred` (the lease was busy), `completed`, `partial` or `failed` (a retry may follow),
        `empty` (nothing was confirmed any more). A lease, store or queue error raises."""
        user_id = payload.get("user_id")
        if not user_id or not isinstance(user_id, str):
            return {"error": "missing user_id"}, 400
        profile = await self._user_repo.get_user(user_id)
        account_id = profile.account_id if profile is not None else None
        if profile is None or not account_id:
            logger.warning(f"⚠️ [MemoryInbox] final user={user_id[:8]} has no profile or no account; nothing done")
            return {"status": "failed"}, 200
        now = self._clock()
        due = (await self._store.state(user_id)).final_due_at
        if due is None or now < due:
            logger.info(f"[MemoryInbox] final user={user_id[:8]} status=not_due")
            return {"status": "not_due"}, 200
        owner = uuid.uuid4().hex
        until = lease_until(now, self._run_timeout_s)
        if not await self._lease.acquire(account_id, owner, now, until):
            await self._defer(user_id, account_id, now)
            return {"status": "deferred"}, 200
        try:
            zone = (await self._owner_zones.resolve(account_id)).zone
            # The final pass reads no owner-language text: its candidates are already worded (the field is not used).
            outcome = await self._run_pass(PassRequest(user_id=user_id, account_id=account_id, zone=zone, language="",
                                                       now=now, deadline=until))
            if outcome.wrote > 0:
                await self._profile(account_id, until)
        finally:
            await self._release(account_id, owner)
        logger.info(f"[MemoryInbox] final user={user_id[:8]} candidates={outcome.candidates} wrote={outcome.wrote} "
                    f"status={outcome.status}")
        if outcome.status in ("completed", "empty"):
            if not await self._store.clear_final_due(user_id, due, self._clock()):
                logger.info(f"[MemoryInbox] final user={user_id[:8]}: a confirmation moved final_due_at during the run; "
                            f"its own task runs it")
        else:
            await self._retry(user_id, account_id, due)
        return {"status": outcome.status}, 200

    async def _run_pass(self, request: PassRequest) -> FinalOutcome:
        """The pass answers its own failures as `failed`; a raise past it is the same answer, logged."""
        try:
            return await self._pass.run(request)
        except Exception as exc:
            logger.error(f"❌ [MemoryInbox] final pass of user {request.user_id[:8]} raised {type(exc).__name__}: {exc}",
                         exc_info=True)
            return FinalOutcome(status="failed", candidates=0, wrote=0, gave_up=0)

    async def _retry(self, user_id: str, account_id: str, due: datetime) -> None:
        """One more run an hour later while the failed runs of this burst are fewer than FINAL_MAX_ATTEMPTS."""
        attempts = await self._store.bump_final_retry(user_id)
        if attempts >= FINAL_MAX_ATTEMPTS:
            logger.warning(f"⚠️ [MemoryInbox] final user={user_id[:8]} failed {attempts} times; the sweep's backstop "
                           f"takes over")
            return
        await self._task_queue.enqueue_worker_task(
            FINAL_TASK, task_payload(user_id), delay_seconds=int(FINAL_DELAY.total_seconds()),
            deadline_seconds=INBOX_DEADLINE_S, dedup_key=_retry_key(user_id, due, attempts),
        )
        logger.info(f"[MemoryInbox] final user={user_id[:8]} retry {attempts} enqueued")

    async def _profile(self, account_id: str, until: datetime) -> None:
        """The profile step, inside the lease after a pass that wrote; it never changes the result."""
        if self._profile_step is None:
            return
        try:
            await self._profile_step.run(account_id, until)
        except Exception:
            logger.error(f"[FactProfile] step failed account={account_id[:8]}", exc_info=True)

    async def _defer(self, user_id: str, account_id: str, now: datetime) -> None:
        await self._task_queue.enqueue_worker_task(
            FINAL_TASK, task_payload(user_id), delay_seconds=LEASE_BUSY_RETRY_DELAY_S,
            deadline_seconds=INBOX_DEADLINE_S, dedup_key=final_defer_key(account_id, now),
        )
        logger.info(f"⏳ [MemoryInbox] memory-write lease busy: deferred the final pass of account {account_id[:8]}")

    async def _release(self, account_id: str, owner: str) -> None:
        try:
            await self._lease.release(account_id, owner)
        except Exception as exc:
            logger.warning(f"[MemoryInbox] releasing the memory-write lease failed for account {account_id[:8]} "
                           f"(it expires on its own): {exc}")
