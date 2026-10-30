"""
DriveSyncSchedulerService — the `start_drive_sync` fan-out on the main service
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §7.1 "Trigger", §7.7, §12.1; RFC Δ5, Δ6).

Every TICK_INTERVAL Cloud Scheduler calls it once. Per connection (MULTI_DRIVE_RFC §8): it enqueues one `drive_sync`
tick (payload `{user_id, connection_id}`, `scope_payload`) for every ACTIVE connection with indexing on (listed from the
connection repository, never from the users) and for every connection that has a stored sync state, whatever its state
(so an orphaned index always gets purged: indexing off, NEEDS_RECONNECT past the grace, DISCONNECTED), and skips one whose
continuation is already queued. It also runs the checks that must work while the drive queue is stuck, per connection
with indexing on: the staleness alert (§12.1) and the one reconnect notice per lost grant (§7.7, RFC Δ6), which also
covers R1 (SECRETS_AT_REST_RFC §6.2, `decisions/reconnect_notice_r1.md`) reading connections: a NEEDS_RECONNECT connection,
or an ACTIVE one whose credential is gone (`DriveConnectionService.reconnect_needed`), gets the one notice; a
DISCONNECTED one never does (a deliberate disconnect ends the connection).
It writes the fan-out flags, through `update_sync_flags`, and a minimal sync state for an ACTIVE connection with
indexing on that has none (`create_sync_state_if_absent`): its `created_at` is the staleness baseline, so a connection
whose ticks never land is alerted too (§12.1). The tick onboards from it as from no state at all. The queue keeps one
dispatch at a time, so a user's connections tick in turn.

Search v2 (§25.4.5, §25.8 as amended by D30) adds three ops alerts to the checks, each posted once through its own flag:
an index built by v1 that awaits the owner's from-scratch rebuild (its ticks do no work); the fallback model serving
more than FALLBACK_ALERT_SHARE of a user's model dollars in a day (once that day); enrichment that has not moved for
DESCRIBED_STALL_AFTER while jobs are ready and budget is left (once, cleared when it moves again). "Moved" is
`described_grew_at`, which the tick refreshes on any enrichment progress or when no job is ready (review rv_sv2_p5
MINOR 2), so a quiet day followed by one new file is not a stall.

The cap alert (§5.4; drive_cap_alert): files held by the card cap (`card_capped`) or the chunk cap (`metadata_only /
user_chunk_cap`), one ops alert per cap with its count when it goes from none held to some, through its own flag, lowered
when the cap holds none again. Counted with two count aggregations (equality filters only) per pass for a user with
indexing on; the tick keeps no count of held files, only a per-tick event count on its log line.

Two passes: every user's tick is enqueued first, then the checks run. A notice goes through the
formatter agent (up to its 120 s SLA), so running checks first would let one slow notice delay every
later user's tick past the scheduler's attempt deadline.

Files the bot cannot open (§26.2, D33, Δ49, Δ53, Δ54): one notice per pass for the files the tick marked, sent here
because the indexer has no chat access (RFC Δ6). Read only while the tick's `inaccessible_unnotified` counter is
non-zero, so a quiet drive costs no query; a file is in it once a delta round that started after its marking has
completed (`drive_access.notice_due`), and once per `inaccessible_since`. The chat text is a fixed localized UI string
with the count and the fix only, through `notify_raw` (no formatter model, no history pair); the paths always go in an
attached text file through `notify_file_bytes`, sent first. The records are marked notified before the send and
unmarked when the list was not sent; this fan-out is the only writer of `inaccessible_notified_since`.

The initial build is keyed by the connection's provider account (MULTI_DRIVE_RFC §10, D8), read from the connection's
`provider_account`. A connection stored before that field gets it here: `account_of(user_id, credential_id)`
(composition) decrypts the credential once and the account is written back; without an account (the credential is
gone) the stall check counts no initial build.

Public interface:
    DriveSyncSchedulerService(repo, queue, connections, notifier, alert_sink, *, connection_service, account_of,
                              clock, ensure_agents, localization, language)
    await fan_out() -> Dict[str, int]
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Tuple

from ..domain import drive_enrich_plan as enrich
from ..domain.connection_scope import ConnectionScope
from ..domain.drive_cap_alert import CARD_CAP, cap_flag_changes
from ..domain.drive_access import NOTICE_FILENAME, notice_batch, notice_file, notice_paths
from ..domain.drive_connection import DriveConnection
from ..domain.drive_connection_state import ConnectionState
from ..domain.drive_enrichment import EnrichJob, InitialBuild, JobState, UsageClass
from ..domain.drive_index import MIGRATION_GATE_KEY, DriveIndexRecord, DriveSyncState
from ..domain.drive_index_params import (
    DESCRIBED_STALL_AFTER, FALLBACK_ALERT_SHARE, INACCESSIBLE_NOTICE_BATCH, INACCESSIBLE_SCAN_MAX,
    MAX_CARD_RECORDS_PER_USER, MAX_CHUNKS_PER_USER, TICK_DEADLINE_S,
)
from ..domain.drive_index_planner import is_stale, month_key
from ..domain.drive_profile import PROFILE_POLICY
from ..domain.drive_search import INACCESSIBLE_UNNOTIFIED_COUNTER, MAX_CHAIN_DEPTH
from ..domain.drive_task_keys import DRIVE_SYNC_TASK, scope_payload, tick_task_key
from ..domain.language import LanguageCode
from ..domain.notification_kind import NotificationKind
from ..domain.notify_result import empty_answer_alert, is_empty_answer
from ..domain.integration import Integration
from ..domain.provider_account import ProviderAccount
from ..domain.reconnect_policy import R1_NOTICES
from ..domain.ui_messages import UIMessage
from ..domain.drive_usage import closed_initial, new_initial_build
from ..ports.alert_sink import AlertSinkPort
from ..ports.drive_connection_repository import DriveConnectionRepository
from ..ports.drive_index_repository import DriveIndexRepository
from ..ports.task_queue import TaskQueue
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..ports.language_service_port import LanguageServicePort
    from .drive_connection_service import DriveConnectionService
    from .localization_service import LocalizationService
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
# The provider account of a connection's grant (realm, subject): the initial build's key (MULTI_DRIVE_RFC §10)
AccountOf = Callable[[str, str], Awaitable[Optional[ProviderAccount]]]


def _tag(scope: ConnectionScope) -> str:
    """The user and connection in a log line or an ops alert: id prefixes only."""
    return f"{scope.user_id[:8]}/{scope.connection_id[:8]}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DriveSyncSchedulerService:
    def __init__(self, repo: DriveIndexRepository, queue: TaskQueue, connections: DriveConnectionRepository,
                 notifier: Optional["UserNotificationService"], alert_sink: Optional[AlertSinkPort], *,
                 connection_service: Optional["DriveConnectionService"] = None,
                 account_of: Optional[AccountOf] = None,
                 clock: Callable[[], datetime] = _utcnow,
                 ensure_agents: Optional[Callable[[str], Awaitable[Any]]] = None,
                 localization: Optional["LocalizationService"] = None,
                 language: Optional["LanguageServicePort"] = None) -> None:
        self._repo = repo
        self._queue = queue
        self._connections = connections
        self._connection_service = connection_service
        self._account_of = account_of
        self._notifier = notifier
        self._alert_sink = alert_sink
        self._clock = clock
        self._ensure_agents = ensure_agents
        self._localization = localization
        self._language = language
        # connection -> (UTC day, `inaccessible_unnotified` then): a notice that could not be delivered (no channel, no
        # file upload for the user's platform) is not read again that day unless the tick counts more files (review of
        # S1). Per process: a new instance tries once more, which costs one read and posts nothing that cannot arrive.
        self._notice_held: Dict[str, Tuple[str, int]] = {}

    async def fan_out(self) -> Dict[str, int]:
        """One pass over every candidate connection. A failure for one is logged and never stops the rest."""
        now = self._clock()
        indexing = await self._connections.list_by_state([ConnectionState.ACTIVE], indexing_enabled=True)
        known: Dict[str, Optional[DriveConnection]] = {c.connection_id: c for c in indexing}
        scopes: Dict[str, ConnectionScope] = {c.connection_id: c.scope for c in indexing}
        for scope in await self._repo.list_sync_scopes():
            scopes.setdefault(scope.connection_id, scope)
        candidates = sorted(scopes.values(), key=lambda sc: (sc.user_id, sc.connection_id))
        summary = {"candidates": len(candidates), "enqueued": 0, "skipped": 0, "continuing": 0,
                   "alerts": 0, "notices": 0, "errors": 0}
        to_check: List[Tuple[DriveConnection, DriveSyncState]] = []
        for scope in candidates:
            try:
                connection = known[scope.connection_id] if scope.connection_id in known \
                    else await self._connections.get(scope.user_id, scope.connection_id)
                checked = await self._visit(scope, connection, now, summary)
                if checked is not None:
                    to_check.append(checked)
            except Exception as exc:
                summary["errors"] += 1
                logger.error(f"[DriveSync] fan-out failed for {_tag(scope)}: {exc}", exc_info=True)
        for connection, state in to_check:
            scope = connection.scope
            try:
                summary["notices"] += await self._reconnect_notice(connection, state)
                summary["notices"] += await self._inaccessible_notice(connection, state)
                summary["alerts"] += await self._staleness(scope, state, now)
                summary["alerts"] += await self._search_v2_alerts(connection, state, now)
                summary["alerts"] += await self._cap_alerts(scope, state)
            except Exception as exc:   # a check never costs anyone their tick: all are enqueued already
                summary["errors"] += 1
                logger.error(f"[DriveSync] checks failed for {_tag(scope)}: {exc}", exc_info=True)
        logger.info(f"[DriveSync] fan-out {summary}")
        return summary

    async def _visit(self, scope: ConnectionScope, connection: Optional[DriveConnection], now: datetime,
                     summary: Dict[str, int]) -> Optional[Tuple[DriveConnection, DriveSyncState]]:
        """Enqueue the connection's tick if due; return what the checks need, for a live connection with indexing
        on."""
        enabled = (connection is not None and connection.state is not ConnectionState.DISCONNECTED
                   and connection.indexing_enabled)
        state = await self._repo.get_sync_state(scope)
        if not enabled and state is None:
            summary["skipped"] += 1   # nothing stored, nothing to purge (§7.1)
            return None
        if state is not None and state.continuation_enqueued_at is not None \
                and now - state.continuation_enqueued_at < _CONTINUATION_FRESH:
            summary["continuing"] += 1   # a continuation is already queued (§7.1)
        else:
            await self._queue.enqueue_worker_task(DRIVE_SYNC_TASK, scope_payload(scope),
                                                  deadline_seconds=TICK_DEADLINE_S,
                                                  dedup_key=tick_task_key(scope, now))
            summary["enqueued"] += 1
        if not enabled or connection is None:
            return None
        if state is None and connection.state is ConnectionState.ACTIVE:
            # after the enqueue, so a failed create never costs the connection a tick; never under a lost grant, where
            # the gate treats no state as nothing to do: a state then would restart the grace period and its notice
            minimal = DriveSyncState(user_id=scope.user_id, connection_id=scope.connection_id, created_at=now)
            state = minimal if await self._repo.create_sync_state_if_absent(minimal) \
                else await self._repo.get_sync_state(scope)
        return (connection, state) if state is not None else None

    async def _initial_build(self, connection: DriveConnection) -> InitialBuild:
        """The initial build of the connection's provider account (D8); a closed one when the account is unknown (no
        `account_of`, or the credential is gone), so only the month's caps count."""
        account = connection.provider_account
        if account is None and self._account_of:
            # a connection stored before the field: one decrypt, then the account is kept on the connection
            account = await self._account_of(connection.user_id, connection.credential_id)
            if account is not None:
                await self._connections.update(connection.user_id, connection.connection_id,
                                               provider_account=ProviderAccount(account.realm, account.subject, ""))
        if account is None:
            return closed_initial(new_initial_build(), self._clock())
        return await self._repo.get_initial_build(connection.user_id, account.realm, account.subject)

    async def _staleness(self, scope: ConnectionScope, state: DriveSyncState, now: datetime) -> int:
        """Alert on the False->True transition only; clear when the criterion is false again (§12.1)."""
        stale = is_stale(state, await self._repo.oldest_due_at(scope, 0), now)
        if stale and not state.stale_alerted:
            if self._alert_sink is None:
                logger.warning(f"[DriveSync] drive index stale for {_tag(scope)}; no alert sink configured")
                return 0
            since = state.last_success_at
            await self._alert_sink.post(
                f"⚠️ Drive index stale for {_tag(scope)}: no clean tick or an overdue edit "
                f"(last clean tick: {since.isoformat() if since else 'never'}, "
                f"last error: {state.last_error.value if state.last_error else 'none'})")
            await self._repo.update_sync_flags(scope, stale_alerted=True)
            logger.warning(f"[DriveSync] drive index stale for {_tag(scope)}; alerted")
            return 1
        if not stale and state.stale_alerted:
            await self._repo.update_sync_flags(scope, stale_alerted=False)
            logger.info(f"[DriveSync] drive index fresh again for {_tag(scope)}")
        return 0

    async def _search_v2_alerts(self, connection: DriveConnection, state: DriveSyncState, now: datetime) -> int:
        """The three Search v2 ops alerts (module docstring); each flag is written only after its post."""
        if self._alert_sink is None:
            return 0
        scope = connection.scope
        posted = 0
        if state.applied_versions and MIGRATION_GATE_KEY not in state.applied_versions and not state.purging:
            if not state.migration_alerted:
                await self._alert_sink.post(
                    f"⚠️ Drive index of {_tag(scope)} was built under an older index version and awaits its "
                    f"rebuild (D30, §26.7): its ticks do no work until `drive_index_rebuild.py --user … --apply`")
                await self._repo.update_sync_flags(scope, migration_alerted=True)
                logger.warning(f"[DriveSync] index of {_tag(scope)} awaits its v2 rebuild; alerted")
                posted += 1
            return posted   # a v1 index has no v2 usage or rebuild to watch
        day = now.astimezone(timezone.utc).date().isoformat()
        if state.fallback_alerted_day != day:
            usage = await self._repo.get_model_usage(scope.user_id, month_key(now))
            total, fallback = usage.daily.get(day, (0.0, 0.0))
            if total > 0 and fallback > FALLBACK_ALERT_SHARE * total:
                await self._alert_sink.post(
                    f"⚠️ Drive models for {_tag(scope)}: the fallback served ${fallback:.4f} of ${total:.4f} "
                    f"today (over {FALLBACK_ALERT_SHARE:.0%}); the primary provider may be failing")
                await self._repo.update_sync_flags(scope, fallback_alerted_day=day)
                logger.warning(f"[DriveSync] drive model fallback share high for {_tag(scope)}; alerted")
                posted += 1
        posted += await self._stall(connection, state, now)
        return posted

    async def _stall(self, connection: DriveConnection, state: DriveSyncState, now: datetime) -> int:
        """§25.8: enrichment has not moved (`described_grew_at`: the described count grew, a run changed a record, or
        nothing was ready) for DESCRIBED_STALL_AFTER while profile or passage jobs are ready and the initial build or a
        month's cap has dollars left."""
        scope = connection.scope
        stalled = state.described_grew_at is not None and now - state.described_grew_at > DESCRIBED_STALL_AFTER
        if not stalled:
            if state.stall_alerted:
                await self._repo.update_sync_flags(scope, stall_alerted=False)
            return 0
        if state.stall_alerted or self._alert_sink is None:
            return 0
        counts = await self._repo.count_by_job_state(scope, frozenset(PROFILE_POLICY) | {"image"})
        ready = sum(counts.get(job.value, {}).get(JobState.READY.value, 0) for job in EnrichJob)
        if not ready:
            return 0
        ctx = enrich.enrich_context(now, await self._repo.get_model_usage(scope.user_id, month_key(now)),
                                    await self._initial_build(connection))
        if not any(enrich.available_usd(ctx, cls) > 0 for cls in (UsageClass.PROFILE.value,
                                                                   UsageClass.PASSAGE.value)):
            return 0
        await self._alert_sink.post(
            f"⚠️ Drive index rebuild stalled for {_tag(scope)}: {state.described_count} files described, none "
            f"more since {state.described_grew_at.isoformat() if state.described_grew_at else 'never'}, with "
            f"{ready} jobs ready and budget left")
        await self._repo.update_sync_flags(scope, stall_alerted=True)
        logger.warning(f"[DriveSync] drive index rebuild stalled for {_tag(scope)}; alerted")
        return 1

    async def _cap_alerts(self, scope: ConnectionScope, state: DriveSyncState) -> int:
        """One ops alert per cap that starts holding files; its flag is written only after the post, and lowered when
        the cap holds none again (module docstring; drive_cap_alert)."""
        if self._alert_sink is None:
            return 0
        counts = await self._repo.count_capped(scope)
        posted = 0
        for cap, raised in cap_flag_changes(counts, card_alerted=state.card_cap_alerted,
                                            chunk_alerted=state.chunk_cap_alerted).items():
            flag = "card_cap_alerted" if cap == CARD_CAP else "chunk_cap_alerted"
            if not raised:
                await self._repo.update_sync_flags(scope, **{flag: False})
                logger.info(f"[DriveSync] drive {cap} cap holds no file of {_tag(scope)} again")
                continue
            if cap == CARD_CAP:
                text = (f"⚠️ Drive index of {_tag(scope)} is at the card cap: {counts.card} files held without a "
                        f"card (`card_capped`, {MAX_CARD_RECORDS_PER_USER} carded records per user); their content "
                        f"and triage wait until headroom returns")
            else:
                text = (f"⚠️ Drive index of {_tag(scope)} is at the chunk cap: {counts.chunk} files not read "
                        f"(`user_chunk_cap`, {MAX_CHUNKS_PER_USER} readable chunks per user); their content is not "
                        f"searchable until headroom holds them")
            await self._alert_sink.post(text)
            await self._repo.update_sync_flags(scope, **{flag: True})
            logger.warning(f"[DriveSync] drive {cap} cap holds files of {_tag(scope)}; alerted")
            posted += 1
        return posted

    async def _reconnect_notice(self, connection: DriveConnection, state: DriveSyncState) -> int:
        """One notice per lost grant (§7.7, RFC Δ6), and R1's one notice when an ACTIVE connection's grant is gone
        altogether (SECRETS_AT_REST_RFC §6.2, F1; MULTI_DRIVE_RFC §4.4; same flag, on the connection's sync state): the
        connections owed it are `DriveConnectionService.reconnect_needed`'s (NEEDS_RECONNECT, or ACTIVE with no
        credential, checked without a decrypt; never DISCONNECTED). Retried on the next fan-out when no send was
        attempted (no channel, a formatter failure, `notify` raising); once a send was attempted the flag stays up,
        delivered or not, because a partial delivery would otherwise repeat every 10 minutes. Except `empty_answer`,
        which is final: the flag stays up (one ERROR line + one ops alert). A good grant again clears the flag."""
        scope = connection.scope
        owed = await self._owed_notice(connection)
        if not owed:
            if state.reconnect_notified:
                await self._repo.update_sync_flags(scope, reconnect_notified=False)
            return 0
        if state.reconnect_notified:
            return 0
        notice = (RECONNECT_NOTICE if connection.state is ConnectionState.NEEDS_RECONNECT
                  else R1_NOTICES[Integration.DRIVE])
        user_id = scope.user_id
        if self._notifier is None:
            logger.warning(f"[DriveSync] drive needs reconnect for {_tag(scope)}; no notifier configured")
            return 0
        # The formatter agent is only registered while the user's agents are loaded (cold start or idle
        # eviction otherwise answers "Unknown recipient"), like every other /worker path that notifies.
        # Before the flag: a failed load leaves it down, `fan_out` logs it and the next fan-out retries.
        if self._ensure_agents is not None:
            await self._ensure_agents(user_id)
        # The flag goes up BEFORE the send (Gmail precedent, WorkerHandler._flag_gmail_reconnect): a request
        # cancelled after the message left (scheduler attempt deadline, deploy SIGTERM) must not repeat it.
        # It comes down again only when nothing can have been sent. CancelledError is not an Exception.
        await self._repo.update_sync_flags(scope, reconnect_notified=True)
        try:
            result = await self._notifier.notify(user_id=user_id, account_id=connection.account_id,
                                                 system_alert=notice,
                                                 kind=NotificationKind.DOCUMENT_DELIVERY)
        except Exception as exc:   # notify raises only before any send (channel lookup)
            logger.error(f"[DriveSync] reconnect notice for {_tag(scope)} raised ({exc}); retried on the "
                         f"next fan-out")
            await self._repo.update_sync_flags(scope, reconnect_notified=False)
            raise
        if is_empty_answer(result):
            # Final (owner decision 2026-10-23): the flag stays up, so the next fan-out does not re-ask the formatter
            # for the same empty answer every 10 minutes.
            logger.error(f"[DriveSync] background_empty_answer: reconnect notice for {_tag(scope)}; not retried",
                         extra={"event": "background_empty_answer", "path": "drive_reconnect_notice",
                                "user_id": user_id[:8]})
            if self._alert_sink is not None:
                try:
                    await self._alert_sink.post(empty_answer_alert("drive_reconnect_notice", user_id))
                except Exception as exc:
                    logger.warning(f"[DriveSync] background_empty_answer alert failed: {exc}")
            return 0
        if not result.delivered and not result.sent:
            await self._repo.update_sync_flags(scope, reconnect_notified=False)
            logger.warning(f"[DriveSync] reconnect notice for {_tag(scope)} not sent ({result.agent_status.value}, "
                           f"{result.error}); retried on the next fan-out")
            return 0
        if not result.delivered:
            logger.warning(f"[DriveSync] reconnect notice for {_tag(scope)} sent but not confirmed delivered "
                           f"({result.agent_status.value}, {result.error}); not retried")
            return 0
        logger.info(f"[DriveSync] reconnect notice sent to {_tag(scope)}")
        return 1

    async def _owed_notice(self, connection: DriveConnection) -> bool:
        """The connection is owed the reconnect notice: NEEDS_RECONNECT always; an ACTIVE one only when the connection
        service lists it (its credential is gone). Without the service only NEEDS_RECONNECT is known."""
        if connection.state is ConnectionState.NEEDS_RECONNECT:
            return True
        if connection.state is not ConnectionState.ACTIVE or self._connection_service is None:
            return False
        owed = await self._connection_service.reconnect_needed(connection.user_id)
        return any(c.connection_id == connection.connection_id for c in owed)

    async def _inaccessible_notice(self, connection: DriveConnection, state: DriveSyncState) -> int:
        """One notice for the files the bot cannot open (module docstring; §26.2). Returns 1 when the list was sent."""
        scope = connection.scope
        user_id = scope.user_id
        waiting = int(state.counters.get(INACCESSIBLE_UNNOTIFIED_COUNTER, 0))
        if not waiting or state.observed_as_of is None:
            return 0
        day = self._clock().astimezone(timezone.utc).date().isoformat()
        held = self._notice_held.get(scope.connection_id)
        if held is not None and held[0] == day and waiting <= held[1]:
            return 0   # undeliverable earlier today and nothing new marked since
        if self._notifier is None or self._localization is None:
            logger.warning(f"[DriveSync] files of {_tag(scope)} the bot cannot open; no notifier or localization")
            return 0
        # lean (ACCESS_MARK_FIELDS): the notice reads the name, the parent, the two inaccessible_* fields and what the
        # path needs to see the record (scope, state); the ancestors are read in full (`_ancestors`)
        marked = await self._repo.inaccessible_records(scope, INACCESSIBLE_SCAN_MAX, lean=True)
        due = notice_batch(marked, state.observed_as_of, INACCESSIBLE_NOTICE_BATCH)
        if not due:
            return 0
        paths = notice_paths(due, await self._ancestors(scope, due, state.root_item_id), state.root_item_id)
        lang = await self._ui_language(user_id)
        account_id = connection.account_id
        previous = {r.item_id: r.inaccessible_notified_since for r in due}
        # Up BEFORE the send (the reconnect notice's rule): a request cancelled after the list left must not repeat it.
        await self._repo.set_inaccessible_notified(scope, {r.item_id: r.inaccessible_since for r in due})
        sent = await self._notifier.notify_file_bytes(
            user_id=user_id, account_id=account_id, file_bytes=notice_file(paths), filename=NOTICE_FILENAME,
            title=self._localization.get_ui_string(lang, UIMessage.DRIVE_INACCESSIBLE_LIST_TITLE))
        if not sent:
            await self._repo.set_inaccessible_notified(scope, previous)   # nothing was sent: retried later
            self._notice_held[scope.connection_id] = (day, waiting)
            logger.warning(f"[DriveSync] list of {len(due)} files of {_tag(scope)} the bot cannot open was not sent; "
                           f"retried tomorrow or when more are marked")
            return 0
        self._notice_held.pop(scope.connection_id, None)
        text = self._localization.get_ui_string(lang, UIMessage.DRIVE_INACCESSIBLE_NOTICE).format(count=len(due))
        if not await self._notifier.notify_raw(user_id, account_id, text):
            logger.warning(f"[DriveSync] the list went to {_tag(scope)} but its text was not sent; not retried")
        logger.info(f"[DriveSync] notice of {len(due)} files the bot cannot open sent to {_tag(scope)}")
        return 1

    async def _ancestors(self, scope: ConnectionScope, recs: List[DriveIndexRecord],
                         root_item_id: str) -> Callable[[str], Optional[DriveIndexRecord]]:
        """The parent chains of `recs`, read level by level in batches, as a lookup for the paths (§3.1)."""
        known: Dict[str, Optional[DriveIndexRecord]] = {r.item_id: r for r in recs}
        frontier = {r.parent_id for r in recs if r.parent_id and r.parent_id != root_item_id} - set(known)
        for _ in range(MAX_CHAIN_DEPTH):
            if not frontier:
                break
            found = await self._repo.get_records(scope, sorted(frontier))
            for item_id in frontier:
                known[item_id] = found.get(item_id)
            frontier = {r.parent_id for r in found.values()
                        if r.parent_id and r.parent_id != root_item_id and r.parent_id not in known}
        return known.get

    async def _ui_language(self, user_id: str) -> LanguageCode:
        if self._language is None:
            return LanguageCode.EN
        try:
            return await self._language.resolve_ui_language(user_id)
        except Exception:
            logger.warning(f"[DriveSync] UI language lookup failed for {user_id[:8]}", exc_info=True)
            return LanguageCode.EN
