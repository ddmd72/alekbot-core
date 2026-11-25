"""The `memory_inbox_triage` `/worker` task: one user's triage of the memory inbox (MCP_MEMORY_SUBMISSIONS_RFC §10.2, §11).

A triage turns what outside assistants sent (`received` submissions) into candidates the owner checks in the Cabinet. The
model work is the injected `TriagePass`; this runner is only the rhythm around it: the payload and the account, the
interval recheck (a redelivered or hand-sent task must not triage twice in a day), the account's memory-write lease
(the one consolidation and the review hold, so no two memory-writing runs overlap), the deferral when that lease is
busy, the state marks and the one notice to the owner. The sweep (`MemoryInboxSweepTask`) is what asks for a triage
each day; rhythm lives on the state document, never in a task name.

The account comes from the user's profile only, never from the payload (a payload is a hint of who, not of where memory
is written). Logs carry ids and counts, never submission or candidate text.
"""
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, Tuple

from ..domain.fact_review_rules import interval_elapsed
from ..domain.memory_inbox import triage_interval
from ..domain.memory_inbox_runs import PassRequest, TriageOutcome, TriagePass
from ..domain.memory_inbox_task_keys import (
    INBOX_DEADLINE_S,
    TRIAGE_TASK,
    task_payload,
    triage_defer_key,
)
from ..domain.memory_write_lease import LEASE_BUSY_RETRY_DELAY_S, lease_until
from ..domain.ui_messages import UIMessage
from ..ports.account_repository import AccountRepository
from ..ports.memory_inbox_repository import MemoryInboxRepository
from ..ports.memory_write_lease import MemoryWriteLeasePort
from ..ports.task_queue import TaskQueue
from ..ports.user_repository import UserRepository
from ..utils.logger import logger

if TYPE_CHECKING:
    from .owner_timezone_resolver import OwnerTimezoneResolver


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MemoryInboxTriageTask:
    """Runs one delivery of `memory_inbox_triage`; a `WorkerTask`.

    `owner_zones` resolves the account owner's zone (`resolve(account_id).zone`), `languages` the owner's UI language
    (`resolve_ui_language(user_id)`, the candidates' language), `notifications` posts the notice
    (`notify_ui_notice(user_id, account_id, message, **fields)`). `account_repo` gives the account's default interval.
    """

    def __init__(self, *, store: MemoryInboxRepository, triage_pass: TriagePass, lease: MemoryWriteLeasePort,
                 task_queue: TaskQueue, user_repo: UserRepository, account_repo: AccountRepository,
                 owner_zones: "OwnerTimezoneResolver", languages: Any, notifications: Any, run_timeout_s: float,
                 clock: Callable[[], datetime] = _utcnow) -> None:
        self._store = store
        self._pass = triage_pass
        self._lease = lease
        self._task_queue = task_queue
        self._user_repo = user_repo
        self._account_repo = account_repo
        self._owner_zones = owner_zones
        self._languages = languages
        self._notifications = notifications
        self._run_timeout_s = run_timeout_s
        self._clock = clock

    async def __call__(self, payload: Dict[str, Any], headers: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
        """400 for a payload without a user; else 200 with `status` `interval` (not due yet), `deferred` (the lease was
        busy; one named task retries), `completed`, `empty` (nothing was waiting) or `failed` (the pass failed, or the
        user has no account). A lease, store or queue error raises (Cloud Tasks retries a 5xx; the interval recheck and
        the `received` status keep a rerun safe)."""
        user_id = payload.get("user_id")
        if not user_id or not isinstance(user_id, str):
            return {"error": "missing user_id"}, 400
        profile = await self._user_repo.get_user(user_id)
        account_id = profile.account_id if profile is not None else None
        if profile is None or not account_id:
            logger.warning(f"⚠️ [MemoryInbox] triage user={user_id[:8]} has no profile or no account; nothing done")
            return {"status": "failed"}, 200
        now = self._clock()
        zone = (await self._owner_zones.resolve(account_id)).zone
        state = await self._store.state(user_id)
        account = await self._account_repo.get_account(account_id)
        interval = triage_interval(profile.config, account.account_defaults if account is not None else None)
        if not interval_elapsed(state.last_triage_at, now, zone, interval):
            logger.info(f"[MemoryInbox] triage user={user_id[:8]} account={account_id[:8]} status=interval")
            return {"status": "interval"}, 200
        owner = uuid.uuid4().hex
        until = lease_until(now, self._run_timeout_s)
        if not await self._lease.acquire(account_id, owner, now, until):
            await self._defer(user_id, account_id, now)
            return {"status": "deferred"}, 200
        try:
            language = await self._language(user_id)
            outcome = await self._run_pass(PassRequest(user_id=user_id, account_id=account_id, zone=zone,
                                                       language=language, now=now, deadline=until))
            await self._store.record_triage(user_id, outcome.status != "failed", now)
        finally:
            await self._release(account_id, owner)
        logger.info(f"[MemoryInbox] triage user={user_id[:8]} account={account_id[:8]} submissions={outcome.submissions} "
                    f"candidates={outcome.candidates} discarded={outcome.discarded} left={outcome.left} "
                    f"passes={outcome.passes} status={outcome.status}")
        if outcome.candidates > 0:
            await self._notice(user_id, account_id, outcome)
        return {"status": outcome.status}, 200

    async def _run_pass(self, request: PassRequest) -> TriageOutcome:
        """The pass answers its own failures as `failed`; a raise past it is the same answer, logged."""
        try:
            return await self._pass.run(request)
        except Exception as exc:
            logger.error(f"❌ [MemoryInbox] triage pass of user {request.user_id[:8]} raised {type(exc).__name__}: {exc}",
                         exc_info=True)
            return TriageOutcome(status="failed", submissions=0, candidates=0, discarded=0, left=0, passes=0, sources=())

    async def _defer(self, user_id: str, account_id: str, now: datetime) -> None:
        await self._task_queue.enqueue_worker_task(
            TRIAGE_TASK, task_payload(user_id), delay_seconds=LEASE_BUSY_RETRY_DELAY_S,
            deadline_seconds=INBOX_DEADLINE_S, dedup_key=triage_defer_key(account_id, now),
        )
        logger.info(f"⏳ [MemoryInbox] memory-write lease busy: deferred the triage of account {account_id[:8]}")

    async def _release(self, account_id: str, owner: str) -> None:
        try:
            await self._lease.release(account_id, owner)
        except Exception as exc:
            logger.warning(f"[MemoryInbox] releasing the memory-write lease failed for account {account_id[:8]} "
                           f"(it expires on its own): {exc}")

    async def _language(self, user_id: str) -> str:
        """The owner's UI language code; English when the lookup fails (the candidates are still made)."""
        try:
            return str((await self._languages.resolve_ui_language(user_id)).value)
        except Exception as exc:
            logger.warning(f"[MemoryInbox] UI language lookup failed for user {user_id[:8]}: {exc}")
            return "en"

    async def _notice(self, user_id: str, account_id: str, outcome: TriageOutcome) -> None:
        """One literal line to the owner: how many candidates wait, naming the client when all came from one. A failed
        post is logged, not retried: the candidates wait in the Cabinet either way."""
        clients = set(outcome.sources)
        single = next(iter(clients)) if len(clients) == 1 else ""
        try:
            if single:
                posted = await self._notifications.notify_ui_notice(
                    user_id, account_id, UIMessage.MEMORY_INBOX_CANDIDATES_FROM,
                    count=str(outcome.candidates), source=single)
            else:
                posted = await self._notifications.notify_ui_notice(
                    user_id, account_id, UIMessage.MEMORY_INBOX_CANDIDATES, count=str(outcome.candidates))
            if not posted:
                logger.warning(f"⚠️ [MemoryInbox] triage notice to user {user_id[:8]} was not delivered")
        except Exception as exc:
            logger.warning(f"⚠️ [MemoryInbox] triage notice to user {user_id[:8]} failed: {type(exc).__name__}: {exc}")
