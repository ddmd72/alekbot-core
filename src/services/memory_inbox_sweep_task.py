"""The `memory_inbox_sweep` `/worker` task: the memory inbox's daily tick (MCP_MEMORY_SUBMISSIONS_RFC §10.1).

Sent once a day by Cloud Scheduler (08:30 Europe/Madrid). No model, no lease, idempotent: every task it enqueues has a
dedup name, and the records it moves move once. Three steps:

1. Expire the candidates the owner did not answer within their window.
2. For each user holding `received` submissions whose triage interval elapsed (owner-local calendar days since the last
   triage), enqueue one `memory_inbox_triage` task named by the owner-local date.
3. Backstop: for each user whose `final_due_at` passed more than FINAL_BACKSTOP_GRACE ago and who still holds `confirmed`
   candidates, enqueue one `memory_inbox_final` task (a lost enqueue, a failed pass).

One user's failure is logged and does not stop the others; the next day's sweep tries again. Logs carry ids and counts.
"""
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Tuple

from ..domain.fact_review_rules import interval_elapsed
from ..domain.memory_inbox import FINAL_BACKSTOP_GRACE, CandidateStatus, triage_interval
from ..domain.memory_inbox_task_keys import (
    FINAL_TASK,
    INBOX_DEADLINE_S,
    TRIAGE_TASK,
    final_key,
    task_payload,
    triage_key,
)
from ..ports.account_repository import AccountRepository
from ..ports.memory_inbox_repository import MemoryInboxRepository
from ..ports.task_queue import TaskQueue
from ..ports.user_repository import UserRepository
from ..utils.logger import logger

if TYPE_CHECKING:
    from .owner_timezone_resolver import OwnerTimezoneResolver


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MemoryInboxSweepTask:
    """Runs one delivery of `memory_inbox_sweep`; a `WorkerTask`. `account_repo` gives each account's default interval."""

    def __init__(self, *, store: MemoryInboxRepository, task_queue: TaskQueue, user_repo: UserRepository,
                 account_repo: AccountRepository, owner_zones: "OwnerTimezoneResolver",
                 clock: Callable[[], datetime] = _utcnow) -> None:
        self._store = store
        self._task_queue = task_queue
        self._user_repo = user_repo
        self._account_repo = account_repo
        self._owner_zones = owner_zones
        self._clock = clock

    async def __call__(self, payload: Dict[str, Any], headers: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
        """200 with how many candidates expired, triage tasks enqueued and final backstop tasks enqueued (a task whose
        name already exists is not counted). A store error of the expiry or of the user lists raises; a failure of one
        user's step is logged and skipped."""
        now = self._clock()
        expired = await self._store.expire_due(now)
        triage_enqueued = await self._triage_due(now)
        final_backstop = await self._final_backstop(now)
        logger.info(f"[MemoryInbox] sweep expired={expired} triage_enqueued={triage_enqueued} "
                    f"final_backstop={final_backstop}")
        return {"expired": expired, "triage_enqueued": triage_enqueued, "final_backstop": final_backstop}, 200

    async def _triage_due(self, now: datetime) -> int:
        enqueued = 0
        for user_id, _ in await self._store.users_with_received():
            try:
                account_id = await self._account_of(user_id)
                if account_id is None:
                    continue
                if await self._triage_one(user_id, account_id, now):
                    enqueued += 1
            except Exception as exc:
                logger.error(f"❌ [MemoryInbox] sweep: triage of user {user_id[:8]} not enqueued: "
                             f"{type(exc).__name__}: {exc}", exc_info=True)
        return enqueued

    async def _triage_one(self, user_id: str, account_id: str, now: datetime) -> bool:
        zone = (await self._owner_zones.resolve(account_id)).zone
        profile = await self._user_repo.get_user(user_id)
        account = await self._account_repo.get_account(account_id)
        interval = triage_interval(profile.config if profile is not None else None,
                                   account.account_defaults if account is not None else None)
        if not interval_elapsed((await self._store.state(user_id)).last_triage_at, now, zone, interval):
            return False
        name = await self._task_queue.enqueue_worker_task(
            TRIAGE_TASK, task_payload(user_id), deadline_seconds=INBOX_DEADLINE_S,
            dedup_key=triage_key(user_id, now, zone))
        return name is not None

    async def _final_backstop(self, now: datetime) -> int:
        enqueued = 0
        for user_id in await self._store.users_final_overdue(now - FINAL_BACKSTOP_GRACE):
            try:
                account_id = await self._account_of(user_id)
                if account_id is None:
                    continue
                due = (await self._store.state(user_id)).final_due_at
                if due is None or await self._store.count_candidates(user_id, CandidateStatus.CONFIRMED) == 0:
                    continue
                name = await self._task_queue.enqueue_worker_task(
                    FINAL_TASK, task_payload(user_id), deadline_seconds=INBOX_DEADLINE_S,
                    dedup_key=final_key(user_id, due))
                if name is not None:
                    enqueued += 1
            except Exception as exc:
                logger.error(f"❌ [MemoryInbox] sweep: final backstop of user {user_id[:8]} not enqueued: "
                             f"{type(exc).__name__}: {exc}", exc_info=True)
        return enqueued

    async def _account_of(self, user_id: str) -> Optional[str]:
        """The user's account from the profile only; a user without one is skipped with a WARNING."""
        profile = await self._user_repo.get_user(user_id)
        if profile is None or not profile.account_id:
            logger.warning(f"⚠️ [MemoryInbox] sweep: user {user_id[:8]} has no profile or no account; skipped")
            return None
        return profile.account_id
