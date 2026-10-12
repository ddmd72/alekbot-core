"""
DriveSyncSchedulerService — the `start_drive_sync` fan-out on the main service
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §7.1 "Trigger", §7.7, §12.1; plan Δ5, Δ6).

Every TICK_INTERVAL Cloud Scheduler calls it once. It enqueues one `drive_sync` tick per user with
indexing on, or with a stored sync state (so an orphaned index always gets purged), and skips a user
whose continuation is already queued. It also runs the two checks that must work while the drive
queue is stuck: the staleness alert (§12.1) and the one reconnect notice per lost grant (§7.7, Δ6).
It writes nothing but the two fan-out flags, through `update_sync_flags`.

Two passes: every user's tick is enqueued first, then the checks run. A notice goes through the
formatter agent (up to its 120 s SLA), so running checks first would let one slow notice delay every
later user's tick past the scheduler's attempt deadline.

Public interface:
    DriveSyncSchedulerService(repo, queue, drive, users, connected_users, notifier, alert_sink, *, clock,
                              ensure_agents)
    await fan_out() -> Dict[str, int]
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Tuple

from ..domain.drive_index import DriveSyncState
from ..domain.drive_index_params import TICK_DEADLINE_S
from ..domain.drive_index_planner import is_stale
from ..domain.drive_task_keys import DRIVE_SYNC_TASK, tick_key, tick_payload
from ..domain.notification_kind import NotificationKind
from ..domain.notify_result import NotifyResult
from ..domain.user_drive import DriveConnection
from ..ports.alert_sink import AlertSinkPort
from ..ports.drive_index_repository import DriveIndexRepository
from ..ports.task_queue import TaskQueue
from ..ports.user_drive_port import UserDrivePort
from ..ports.user_repository import UserRepository
from ..utils.logger import logger

if TYPE_CHECKING:
    from .user_notification_service import UserNotificationService

# The notice Smart (through the notification formatter) turns into a message in the user's own
# language. Wording follows the Gmail precedent (WorkerHandler._flag_gmail_reconnect); the provider is
# never named to the model (Step 1 §4.3).
RECONNECT_NOTICE = (
    "The connection to the user's drive expired: the drive provider rejected Alek's saved access "
    "(usually a password change, or access removed in the account's app settings). Until the user "
    "reconnects, the drive index is not updated; searching it still works from the last update. Tell "
    "them, and that the fix is Cabinet → Drive → Reconnect."
)
_CONTINUATION_FRESH = timedelta(seconds=TICK_DEADLINE_S)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _nothing_sent(result: NotifyResult) -> bool:
    """True only when `notify` stopped before any agent call or send: no channel to deliver to. Any
    other undelivered outcome may already have reached the user (text sent, then rich content failed)."""
    error = result.error or ""
    return error == "no_channel" or error.startswith("channel_factory_returned_none")


class DriveSyncSchedulerService:
    def __init__(self, repo: DriveIndexRepository, queue: TaskQueue, drive: UserDrivePort,
                 users: UserRepository, connected_users: Callable[[], Awaitable[List[str]]],
                 notifier: Optional["UserNotificationService"], alert_sink: Optional[AlertSinkPort], *,
                 clock: Callable[[], datetime] = _utcnow,
                 ensure_agents: Optional[Callable[[str], Awaitable[Any]]] = None) -> None:
        self._repo = repo
        self._queue = queue
        self._drive = drive
        self._users = users
        self._connected_users = connected_users
        self._notifier = notifier
        self._alert_sink = alert_sink
        self._clock = clock
        self._ensure_agents = ensure_agents

    async def fan_out(self) -> Dict[str, int]:
        """One pass over every candidate. A failure for one user is logged and never stops the rest."""
        now = self._clock()
        candidates = sorted(set(await self._connected_users()) | set(await self._repo.list_sync_user_ids()))
        summary = {"candidates": len(candidates), "enqueued": 0, "skipped": 0, "continuing": 0,
                   "alerts": 0, "notices": 0, "errors": 0}
        to_check: List[Tuple[str, Any, DriveSyncState]] = []
        for user_id in candidates:
            try:
                checked = await self._visit(user_id, now, summary)
                if checked is not None:
                    to_check.append(checked)
            except Exception as exc:
                summary["errors"] += 1
                logger.error(f"[DriveSync] fan-out failed for {user_id[:8]}: {exc}", exc_info=True)
        for user_id, profile, state in to_check:
            try:
                connection = await self._drive.connection(user_id)
                summary["notices"] += await self._reconnect_notice(user_id, profile, state, connection)
                summary["alerts"] += await self._staleness(user_id, state, now)
            except Exception as exc:   # a check never costs anyone their tick: all are enqueued already
                summary["errors"] += 1
                logger.error(f"[DriveSync] checks failed for {user_id[:8]}: {exc}", exc_info=True)
        logger.info(f"[DriveSync] fan-out {summary}")
        return summary

    async def _visit(self, user_id: str, now: datetime,
                     summary: Dict[str, int]) -> Optional[Tuple[str, Any, DriveSyncState]]:
        """Enqueue the user's tick if due; return what the checks need, for users with indexing on."""
        profile = await self._users.get_user(user_id)
        enabled = bool(profile is not None and profile.config.drive_indexing_enabled)
        state = await self._repo.get_sync_state(user_id)
        if not enabled and state is None:
            summary["skipped"] += 1   # nothing stored, nothing to purge (§7.1)
            return None
        if state is not None and state.continuation_enqueued_at is not None \
                and now - state.continuation_enqueued_at < _CONTINUATION_FRESH:
            summary["continuing"] += 1   # a continuation is already queued (§7.1)
        else:
            await self._queue.enqueue_worker_task(DRIVE_SYNC_TASK, tick_payload(user_id),
                                                  deadline_seconds=TICK_DEADLINE_S,
                                                  dedup_key=tick_key(user_id, now))
            summary["enqueued"] += 1
        return (user_id, profile, state) if enabled and state is not None else None

    async def _staleness(self, user_id: str, state: DriveSyncState, now: datetime) -> int:
        """Alert on the False->True transition only; clear when the criterion is false again (§12.1)."""
        stale = is_stale(state, await self._repo.oldest_due_at(user_id, 0), now)
        if stale and not state.stale_alerted:
            if self._alert_sink is None:
                logger.warning(f"[DriveSync] drive index stale for {user_id[:8]}; no alert sink configured")
                return 0
            since = state.last_success_at
            await self._alert_sink.post(
                f"⚠️ Drive index stale for user {user_id[:8]}: no clean tick or an overdue edit "
                f"(last clean tick: {since.isoformat() if since else 'never'}, "
                f"last error: {state.last_error.value if state.last_error else 'none'})")
            await self._repo.update_sync_flags(user_id, stale_alerted=True)
            logger.warning(f"[DriveSync] drive index stale for {user_id[:8]}; alerted")
            return 1
        if not stale and state.stale_alerted:
            await self._repo.update_sync_flags(user_id, stale_alerted=False)
            logger.info(f"[DriveSync] drive index fresh again for {user_id[:8]}")
        return 0

    async def _reconnect_notice(self, user_id: str, profile: Any, state: DriveSyncState,
                                connection: DriveConnection) -> int:
        """One notice per lost grant (§7.7, plan Δ6). Retried on the next fan-out only when nothing can
        have been sent (no channel) or `notify` raised before sending; any other attempt sets the flag,
        delivered or not, because a partial delivery would otherwise repeat every 10 minutes. A good
        grant again clears the flag."""
        if connection.connected and not connection.needs_reconnect:
            if state.reconnect_notified:
                await self._repo.update_sync_flags(user_id, reconnect_notified=False)
            return 0
        if not connection.needs_reconnect or state.reconnect_notified:
            return 0
        if self._notifier is None:
            logger.warning(f"[DriveSync] drive needs reconnect for {user_id[:8]}; no notifier configured")
            return 0
        # The formatter agent is only registered while the user's agents are loaded (cold start or idle
        # eviction otherwise answers "Unknown recipient"), like every other /worker path that notifies.
        # Before the flag: a failed load leaves it down, `fan_out` logs it and the next fan-out retries.
        if self._ensure_agents is not None:
            await self._ensure_agents(user_id)
        # The flag goes up BEFORE the send (Gmail precedent, WorkerHandler._flag_gmail_reconnect): a request
        # cancelled after the message left (scheduler attempt deadline, deploy SIGTERM) must not repeat it.
        # It comes down again only when nothing can have been sent. CancelledError is not an Exception.
        await self._repo.update_sync_flags(user_id, reconnect_notified=True)
        try:
            result = await self._notifier.notify(user_id=user_id, account_id=profile.account_id,
                                                 system_alert=RECONNECT_NOTICE,
                                                 kind=NotificationKind.DOCUMENT_DELIVERY)
        except Exception as exc:   # notify raises only before any send (channel lookup)
            logger.error(f"[DriveSync] reconnect notice for {user_id[:8]} raised ({exc}); retried on the "
                         f"next fan-out")
            await self._repo.update_sync_flags(user_id, reconnect_notified=False)
            raise
        if not result.delivered and _nothing_sent(result):
            await self._repo.update_sync_flags(user_id, reconnect_notified=False)
            logger.warning(f"[DriveSync] reconnect notice for {user_id[:8]} had no channel ({result.error}); "
                           f"retried on the next fan-out")
            return 0
        if not result.delivered:
            logger.warning(f"[DriveSync] reconnect notice for {user_id[:8]} not confirmed delivered "
                           f"({result.agent_status.value}, {result.error}); not retried")
            return 0
        logger.info(f"[DriveSync] reconnect notice sent to {user_id[:8]}")
        return 1
