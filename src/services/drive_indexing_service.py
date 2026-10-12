"""
DriveIndexingService — the per-user `drive_sync` tick (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §7, §8).

Gate -> lease -> observe -> work off -> continue. Every decision is the planner's (domain); this class
fetches, calls it and writes the result. Runs only on the indexer service (D5).

Write-order rule: inside a step, a progress marker (a cursor, a cleared flag) is always the LAST write,
so a crash between writes repeats the step instead of skipping work.

Public interface:
    DriveIndexingService(drive, repo, embeddings, queue, users, place_of, embedding_space, *, clock, monotonic)
    await tick(user_id, nudge=None) -> TickResult
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from ..domain.drive_card import card_eligible, card_hash, card_text
from ..domain.drive_index import (
    CURRENT_VERSIONS, DriveChange, DriveChanges, DriveIndexRecord, DriveSyncState, EpochPhase, ErrorCategory,
    FolderProbe, Nudge, Priority, RecordState, Scope,
)
from ..domain.drive_index_params import (
    CASCADE_OPS_PER_TICK, EMBED_IN_FLIGHT, ITEM_CALLS_PER_TICK, LEASE_MARGIN, MAINTENANCE_SHARE,
    MAX_ATTEMPTS, MAX_CARD_RECORDS_PER_USER, NUDGE_RETRY_DELAY_S, PROBE_CHILDREN, RECORD_WRITE_BATCH,
    RELIST_PAGE, SYSTEMIC_BACKOFF_BASE_S, SYSTEMIC_BACKOFF_CAP_S, TICK_DEADLINE_S, TICK_START_BUDGET,
)
from ..domain.drive_index_planner import (
    AccessAction, ObservationPlan, PlaceOf, PlanContext, access_action, capped, change_from_record,
    needs_probe, parent_scope, plan_observation, schedule, stale_versions, with_card, with_retry, without_card,
)
from ..domain.drive_index_policy import is_marker_child, probe_from_children
from ..domain.drive_task_keys import DRIVE_SYNC_TASK, continuation_key, nudge_key, tick_payload
from ..domain.exceptions import LLMClientError, LLMRateLimitError, LLMUnavailableError
from ..domain.user_drive import (
    DriveAccessDenied, DriveItemNotFoundError, DriveNotConnectedError, DriveReauthRequired, DriveResyncRequired,
    DriveThrottled,
)
from ..ports.drive_index_repository import DriveIndexRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.task_queue import TaskQueue
from ..ports.user_drive_port import UserDrivePort
from ..ports.user_repository import UserRepository
from ..utils.logger import logger

_DUE_PAGE = 100
# Errors about ONE item (a probe, a parent lookup, a re-listing page, a nudge read): defer that item, never
# the user (§8.2). ValueError is the adapter's 5xx; OSError covers connection resets.
_PER_ITEM_ERRORS = (DriveAccessDenied, DriveItemNotFoundError, asyncio.TimeoutError, ValueError, OSError)
_GATE_RECHECK_S = 5.0   # the opt-out flag is re-read before a write batch at most this often


@dataclass
class TickResult:
    status: str                               # disabled | busy | purging | purged | access_lost | done | throttled | error
    summary: Dict[str, int] = field(default_factory=dict)


class _OptedOut(Exception):
    """Indexing was turned off while the tick ran: drop the in-flight result (§7.1)."""


class _DriveSwitched(Exception):
    """A page came from another drive than the stored one (§7.1 step 1): purge and start fresh."""


@dataclass
class _Saved:
    """The sync state as last written during this tick. A systemic error writes its fields onto this, never
    onto the tick-start copy, so the cursor and epoch observe already saved survive (retro_tick F1)."""
    state: DriveSyncState


@dataclass
class _Budget:
    started: float
    monotonic: Callable[[], float]
    item_calls: int = field(default_factory=lambda: ITEM_CALLS_PER_TICK)
    cascade_ops: int = field(default_factory=lambda: CASCADE_OPS_PER_TICK)
    maintenance_s: float = 0.0

    def time_left(self) -> bool:
        return self.monotonic() - self.started < TICK_START_BUDGET.total_seconds()

    def item_call_allowed(self) -> bool:
        """An item call (probe, parent lookup, re-listing page) starts only within both budgets: a slow provider
        must not push the tick past its deadline (premise_audit P8). The item waits for the next tick."""
        return self.item_calls > 0 and self.time_left()

    def maintenance_allowed(self) -> bool:
        return self.maintenance_s < MAINTENANCE_SHARE * TICK_START_BUDGET.total_seconds()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _lease_span() -> timedelta:
    return timedelta(seconds=TICK_DEADLINE_S) + LEASE_MARGIN


def _backoff_s(failures: int) -> int:
    return int(min(SYSTEMIC_BACKOFF_BASE_S * (2 ** failures), SYSTEMIC_BACKOFF_CAP_S))


class DriveIndexingService:
    def __init__(self, drive: UserDrivePort, repo: DriveIndexRepository, embeddings: EmbeddingService,
                 queue: TaskQueue, users: UserRepository, place_of: PlaceOf, embedding_space: str, *,
                 clock: Callable[[], datetime] = _utcnow, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._drive = drive
        self._repo = repo
        self._embeddings = embeddings
        self._queue = queue
        self._users = users
        self._place_of = place_of
        self._space = embedding_space
        self._clock = clock
        self._monotonic = monotonic
        self._gate_checked: Dict[str, float] = {}

    # ------------------------------------------------------------------------------------------
    # The tick
    # ------------------------------------------------------------------------------------------

    async def tick(self, user_id: str, nudge: Optional[Nudge] = None) -> TickResult:
        now = self._clock()
        tick_id = uuid.uuid4().hex
        enabled = await self._enabled(user_id)
        state = await self._repo.get_sync_state(user_id)
        connection = await self._drive.connection(user_id)
        action = access_action(enabled, state, connection, now)
        if action is AccessAction.NOOP_DISABLED:
            return TickResult("disabled")
        lease = await self._repo.acquire_lease(user_id, tick_id, now, now + _lease_span(), create=True)
        if lease is None:
            if nudge is not None:
                await self._requeue_nudge(user_id, nudge)
            return TickResult("busy")
        summary: Dict[str, int] = {}
        try:
            state = lease if lease.created_at is not None else replace(lease, created_at=now)
            action = access_action(enabled, state, connection, now)   # facts may have moved before the lease
            if action in (AccessAction.PURGE_OPT_OUT, AccessAction.PURGE_EXPIRED,
                          AccessAction.PURGE_DRIVE_SWITCH, AccessAction.PURGE_THEN_RESTART):
                return await self._purge(user_id, state, tick_id, action, enabled, summary)
            if action is AccessAction.RECORD_LOSS:
                if state.access_lost_at is None:
                    await self._repo.save_sync_state(replace(state, access_lost_at=now))
                    logger.warning(f"[DriveIndex] access lost for {user_id[:8]}; grace period started")
                return TickResult("access_lost")
            if action is AccessAction.RESUME_AFTER_REGAIN:
                state = replace(state, access_lost_at=None)
                logger.info(f"[DriveIndex] access regained for {user_id[:8]}; continuing from the cursor")
            return await self._run(user_id, state, tick_id, nudge, now, enabled, summary)
        finally:
            await self._repo.release_lease(user_id, tick_id)

    async def _run(self, user_id: str, state: DriveSyncState, tick_id: str, nudge: Optional[Nudge],
                   now: datetime, enabled: bool, summary: Dict[str, int]) -> TickResult:
        budget = _Budget(started=self._monotonic(), monotonic=self._monotonic)
        self._gate_checked[user_id] = budget.started   # the gate just read the flag
        state = replace(state, continuation_enqueued_at=None)
        saved = _Saved(state)
        try:
            if nudge is not None:
                await self._observe_nudge(user_id, state, nudge, now, budget, summary)
            state = await self._observe(user_id, state, now, budget, summary, saved)
            if state.round_complete:
                state = await self._work_off(user_id, state, now, budget, summary)
            more = await self._has_more(user_id, state, now, upgrade_wrote=summary.get("upgraded", 0) > 0)
            if more:
                await self._continue(user_id, tick_id)
            else:
                state = replace(state, counters=await self._repo.count_by_state(user_id))   # settled: refresh
            progressed = any(v for k, v in summary.items() if k != "pages")
            state = replace(state, continuation_enqueued_at=now if more else None, last_error=None,
                            systemic_failures=0,
                            last_success_at=now if (progressed or not more) else state.last_success_at)
            await self._repo.save_sync_state(state)
            self._log(user_id, "continued" if more else "done", summary, budget)
            return TickResult("done", summary)
        except _OptedOut:
            logger.info(f"[DriveIndex] indexing turned off mid-tick for {user_id[:8]}; result dropped")
            # The next tick's gate purges. Without it a lost toggle task leaves the purge to a fan-out that skips
            # the user on the stale continuation marker for up to 30 min (premise_audit P2).
            await self._continue(user_id, tick_id)
            return TickResult("disabled", summary)
        except _DriveSwitched:
            logger.warning(f"[DriveIndex] a page came from another drive for {user_id[:8]}; purging")
            return await self._purge(user_id, state, tick_id, AccessAction.PURGE_DRIVE_SWITCH, enabled, summary)
        except DriveReauthRequired:
            flagged = await self._drive.flag_reconnect(user_id)
            logger.warning(f"[DriveIndex] refresh rejected for {user_id[:8]} (flag set now: {flagged})")
            await self._repo.save_sync_state(replace(saved.state, last_error=ErrorCategory.RECONNECT_NEEDED))
            return TickResult("access_lost", summary)
        except (DriveThrottled, LLMRateLimitError, LLMUnavailableError) as exc:
            delay = int(exc.retry_after_s) if isinstance(exc, DriveThrottled) else _backoff_s(state.systemic_failures)
            logger.warning(f"[DriveIndex] throttled for {user_id[:8]}: {exc}; continuing in {delay}s")
            await self._continue(user_id, tick_id, delay_seconds=delay)
            await self._repo.save_sync_state(replace(saved.state, last_error=ErrorCategory.THROTTLED,
                                                     continuation_enqueued_at=now,
                                                     systemic_failures=state.systemic_failures + 1))
            return TickResult("throttled", summary)
        except Exception as exc:
            category = ErrorCategory.PROVIDER_ERROR if isinstance(exc, (DriveAccessDenied, ValueError)) \
                else ErrorCategory.INTERNAL
            delay = _backoff_s(state.systemic_failures)
            logger.error(f"[DriveIndex] tick failed for {user_id[:8]}: {exc}; continuing in {delay}s", exc_info=True)
            await self._continue(user_id, tick_id, delay_seconds=delay)
            await self._repo.save_sync_state(replace(saved.state, last_error=category, continuation_enqueued_at=now,
                                                     systemic_failures=state.systemic_failures + 1))
            return TickResult("error", summary)

    # ------------------------------------------------------------------------------------------
    # Observe
    # ------------------------------------------------------------------------------------------

    async def _observe_nudge(self, user_id: str, state: DriveSyncState, nudge: Nudge, now: datetime,
                             budget: _Budget, summary: Dict[str, int]) -> None:
        """A bot write is applied before delta has caught up (§7.1 step 1, S0d)."""
        if not state.root_item_id:
            return   # onboarding has not seen the root yet; the enumeration will see the item anyway
        changes: List[DriveChange] = []
        for item_id in nudge.item_ids:
            try:
                changes.append(await self._drive.get_change(user_id, item_id))
            except _PER_ITEM_ERRORS as exc:
                logger.warning(f"[DriveIndex] nudge read failed for {user_id[:8]}: {exc}")
        if changes:
            await self._apply(user_id, self._ctx(user_id, state, now), changes, budget, summary, nudged=True)

    async def _observe(self, user_id: str, state: DriveSyncState, now: datetime, budget: _Budget,
                       summary: Dict[str, int], saved: _Saved) -> DriveSyncState:
        if state.round_complete or state.round_started_at is None:
            state = replace(state, round_complete=False, round_started_at=now)
        resynced = False
        while budget.time_left():
            try:
                page = await self._drive.get_changes(user_id, state.cursor)
            except DriveResyncRequired as exc:
                if resynced:   # a restart cursor that expires again is the adapter's problem: systemic
                    raise ValueError("drive change cursor expired twice in one tick") from exc
                resynced = True
                logger.warning(f"[DriveIndex] cursor expired for {user_id[:8]}; epoch {state.epoch + 1}")
                state = replace(state, cursor=exc.restart_cursor, epoch=state.epoch + 1,
                                epoch_phase=EpochPhase.ENUMERATING, round_complete=False, round_started_at=now)
                await self._repo.save_sync_state(state)
                saved.state = state
                continue
            if state.drive_id and page.drive_id and page.drive_id != state.drive_id:
                raise _DriveSwitched()
            state = self._enter_epoch(state, page)
            state = replace(state, drive_id=page.drive_id or state.drive_id, root_item_id=page.root_item_id)
            await self._apply(user_id, self._ctx(user_id, state, now), list(page.changes), budget, summary)
            state = replace(state, cursor=page.cursor)
            if page.complete:
                phase = EpochPhase.SWEEPING if state.epoch_phase is EpochPhase.ENUMERATING else state.epoch_phase
                state = replace(state, round_complete=True, observed_as_of=state.round_started_at, epoch_phase=phase)
            await self._repo.save_sync_state(state)   # the cursor is the page's progress marker: written last
            saved.state = state
            summary["pages"] = summary.get("pages", 0) + 1
            if page.complete:
                break
        return state

    @staticmethod
    def _enter_epoch(state: DriveSyncState, page: DriveChanges) -> DriveSyncState:
        if page.full_enumeration and state.epoch_phase is not EpochPhase.ENUMERATING:
            return replace(state, epoch=state.epoch + 1, epoch_phase=EpochPhase.ENUMERATING)
        return state

    def _ctx(self, user_id: str, state: DriveSyncState, now: datetime) -> PlanContext:
        return PlanContext(user_id=user_id, root_item_id=state.root_item_id, epoch=state.epoch,
                           phase=state.epoch_phase, now=now, place_of=self._place_of)

    async def _apply(self, user_id: str, ctx: PlanContext, changes: Sequence[DriveChange], budget: _Budget,
                     summary: Dict[str, int], *, relisting: bool = False, nudged: bool = False) -> None:
        """Apply observations in feed order against an in-page view; one write batch at the end.

        `relisting`: the changes are a re-listing of a never-stored subtree, so every new normal folder
        among them needs its own re-listing (§5.2). `nudged`: the items are named in a nudge, so a card
        they are due for is class 0 even when first seen (§7.8)."""
        ids = [c.item_id for c in changes]
        cache: Dict[str, Optional[DriveIndexRecord]] = dict(await self._repo.get_records(user_id, ids))
        parent_ids = {c.parent_id for c in changes if c.parent_id} | {
            r.parent_id for r in cache.values() if r is not None and r.parent_id}
        missing = [p for p in parent_ids - set(cache) if p != ctx.root_item_id]
        cache.update(await self._repo.get_records(user_id, missing))
        writes: Dict[str, DriveIndexRecord] = {}
        deletes: Set[str] = set()
        for change in changes:
            try:
                existing = cache.get(change.item_id)
                parent = cache.get(change.parent_id) if not change.deleted else None
                previous_parent = cache.get(existing.parent_id) if existing is not None else None
                probe = await self._maybe_probe(user_id, change, parent, existing, ctx, budget)
                plan = plan_observation(change, existing, parent, probe, ctx, previous_parent)
            except (DriveNotConnectedError, DriveThrottled, _OptedOut) as exc:   # systemic: ends the tick (§8.2)
                logger.warning(f"[DriveIndex] observation of item {change.item_id} hit a systemic error for "
                               f"{user_id[:8]}: {type(exc).__name__}; the page is not applied")
                raise
            except Exception as exc:   # one bad item never stalls the user's index (§8.2)
                logger.error(f"[DriveIndex] observation of item {change.item_id} failed for {user_id[:8]}: {exc}",
                             exc_info=True)
                summary["skipped"] = summary.get("skipped", 0) + 1
                continue
            if (relisting and existing is None and plan.upsert is not None and plan.upsert.is_folder
                    and plan.upsert.scope is Scope.NORMAL):
                plan = replace(plan, upsert=schedule(replace(plan.upsert, reobserve_pending=True), ctx.now,
                                                     first_seen=True))
            if (nudged and plan.upsert is not None and plan.upsert.due_at is not None
                    and card_eligible(plan.upsert) and plan.upsert.card_hash != card_hash(plan.upsert)):
                plan = replace(plan, upsert=schedule(plan.upsert, ctx.now, first_seen=False))
            self._fold(plan, cache, writes, deletes)
        await self._commit(user_id, list(writes.values()), deletes)
        summary["observed"] = summary.get("observed", 0) + len(changes)

    @staticmethod
    def _fold(plan: ObservationPlan, cache: Dict[str, Optional[DriveIndexRecord]],
              writes: Dict[str, DriveIndexRecord], deletes: Set[str]) -> None:
        for update in plan.parent_updates:
            cache[update.item_id] = update
            writes[update.item_id] = update
        if plan.delete_item_id is not None:
            cache[plan.delete_item_id] = None
            writes.pop(plan.delete_item_id, None)
            deletes.add(plan.delete_item_id)
        if plan.upsert is not None:
            cache[plan.upsert.item_id] = plan.upsert
            writes[plan.upsert.item_id] = plan.upsert
            deletes.discard(plan.upsert.item_id)

    async def _maybe_probe(self, user_id: str, change: DriveChange, parent: Optional[DriveIndexRecord],
                           existing: Optional[DriveIndexRecord], ctx: PlanContext,
                           budget: _Budget) -> Optional[FolderProbe]:
        if not needs_probe(change, parent_scope(change.parent_id, parent, ctx.root_item_id), existing):
            return None
        if not budget.item_call_allowed():
            return None   # the folder waits as probe_pending; its children are held unresolved (§3.6)
        return await self._probe(user_id, change.item_id, budget)

    async def _probe(self, user_id: str, folder_id: str, budget: _Budget) -> Optional[FolderProbe]:
        budget.item_calls -= 1
        try:
            page = await self._drive.list_children_changes(user_id, folder_id, limit=PROBE_CHILDREN)
        except _PER_ITEM_ERRORS as exc:
            logger.warning(f"[DriveIndex] density probe failed for {user_id[:8]}: {exc}")
            return None
        except (DriveNotConnectedError, DriveThrottled) as exc:
            logger.warning(f"[DriveIndex] density probe hit a systemic error for {user_id[:8]}: {type(exc).__name__}")
            raise   # the caller ends the tick
        except Exception as exc:   # any other provider failure is a failed probe: the folder waits (retro_tick F2)
            logger.warning(f"[DriveIndex] density probe failed for {user_id[:8]}: {type(exc).__name__}: {exc}")
            return None
        return probe_from_children(page.changes)

    async def _commit(self, user_id: str, writes: List[DriveIndexRecord], deletes: Set[str],
                      card_vectors: Optional[Dict[str, List[float]]] = None) -> None:
        if not writes and not deletes:
            return
        await self._recheck_gate(user_id)
        for start in range(0, len(writes), RECORD_WRITE_BATCH):
            await self._repo.upsert_records(writes[start:start + RECORD_WRITE_BATCH], card_vectors)
        if deletes:
            await self._repo.delete_records(user_id, sorted(deletes))

    # ------------------------------------------------------------------------------------------
    # Work off (only after a complete round, plan Δ15)
    # ------------------------------------------------------------------------------------------

    async def _work_off(self, user_id: str, state: DriveSyncState, now: datetime, budget: _Budget,
                        summary: Dict[str, int]) -> DriveSyncState:
        if state.epoch_phase is EpochPhase.SWEEPING:
            state = await self._sweep(user_id, state, now, budget, summary)
        stopped_class = await self._due_loop(user_id, state, now, budget, summary)
        if stopped_class is None:
            state = await self._upgrade_step(user_id, state, now, budget, summary)
            await self._release_card_cap(user_id, state, now, summary)
        return state

    async def _sweep(self, user_id: str, state: DriveSyncState, now: datetime, budget: _Budget,
                     summary: Dict[str, int]) -> DriveSyncState:
        """Delete what the epoch's enumeration did not see (§7.5). A full enumeration carries no tombstones,
        so a swept marker child is handled as a deletion: its folder re-opens (§5.2)."""
        ctx = self._ctx(user_id, state, now)
        while budget.cascade_ops > 0 and budget.time_left():
            stale = await self._repo.records_seen_before(user_id, state.epoch, min(budget.cascade_ops, RECORD_WRITE_BATCH))
            if not stale:
                logger.info(f"[DriveIndex] epoch {state.epoch} swept for {user_id[:8]}")
                return replace(state, epoch_phase=EpochPhase.IDLE, initial_enumeration_done=True)
            writes: Dict[str, DriveIndexRecord] = {}
            deletes: Set[str] = {r.item_id for r in stale}
            for rec in stale:
                if is_marker_child(rec.name):
                    parent = await self._parent_of(user_id, rec, ctx)
                    plan = plan_observation(DriveChange(item_id=rec.item_id, deleted=True), rec, None, None, ctx, parent)
                    for update in plan.parent_updates:
                        if update.item_id not in deletes:
                            writes[update.item_id] = update   # re-opened, with its re-listing due (durable)
            await self._commit(user_id, list(writes.values()), deletes)
            budget.cascade_ops -= len(stale)
            summary["swept"] = summary.get("swept", 0) + len(stale)
        return state

    async def _due_loop(self, user_id: str, state: DriveSyncState, now: datetime, budget: _Budget,
                        summary: Dict[str, int]) -> Optional[int]:
        """Work due records class by class (§7.8). Returns the class a budget stopped at, or None.

        A class that runs out of its budget stops the loop: lower classes wait for the next tick. That is
        what keeps a repository's files from being carded while its rescope is unfinished (plan Δ15)."""
        while budget.time_left():
            batch = await self._repo.due_records(user_id, now, _DUE_PAGE)
            if not batch:
                return None
            cls = batch[0].priority if batch[0].priority is not None else int(Priority.RETRY)
            same = [r for r in batch if r.priority == cls]
            if cls >= Priority.MAINTENANCE and not budget.maintenance_allowed():
                return cls
            started = self._monotonic()
            progressed = await self._work_class(user_id, state, same, now, budget, summary)
            if cls >= Priority.MAINTENANCE:
                budget.maintenance_s += self._monotonic() - started
            if not progressed:
                return cls
        return int(Priority.RETRY)

    async def _work_class(self, user_id: str, state: DriveSyncState, recs: List[DriveIndexRecord], now: datetime,
                          budget: _Budget, summary: Dict[str, int]) -> bool:
        ctx = self._ctx(user_id, state, now)
        cards: List[DriveIndexRecord] = []
        progressed = False
        for stale in recs:
            # An earlier step of this batch may have changed or removed it: always work the stored version.
            rec_or_none = (await self._repo.get_records(user_id, [stale.item_id])).get(stale.item_id)
            if rec_or_none is None or rec_or_none.due_at is None or rec_or_none.due_at > now:
                progressed = True
                continue
            rec = rec_or_none
            if rec.state is RecordState.DELETING:
                progressed |= await self._cascade_step(user_id, rec, now, budget, summary)
            elif rec.probe_pending:
                progressed |= await self._probe_step(user_id, rec, ctx, budget)
            elif rec.state is RecordState.UNRESOLVED:
                progressed |= await self._resolve_step(user_id, rec, state, ctx, budget)
            elif rec.rescope_pending:
                progressed |= await self._rescope_step(user_id, rec, ctx, budget, summary)
            elif rec.reobserve_pending:
                progressed |= await self._reobserve_step(user_id, rec, ctx, budget, summary)
            elif rec.has_card and not card_eligible(rec):
                await self._commit(user_id, [without_card(rec, now)], set())
                progressed = True
            elif card_eligible(rec) and rec.card_hash != card_hash(rec):
                cards.append(rec)
            else:
                await self._commit(user_id, [schedule(rec, now, first_seen=False)], set())
                progressed = True
        if cards:
            await self._embed_cards(user_id, cards, now, summary)
            progressed = True
        return progressed

    async def _cascade_step(self, user_id: str, folder: DriveIndexRecord, now: datetime, budget: _Budget,
                            summary: Dict[str, int]) -> bool:
        """Folder delete, post-order: files go, child folders become deleting, the folder goes last (§7.6)."""
        if budget.cascade_ops <= 0:
            return False
        kids = await self._repo.children(user_id, folder.item_id, None, min(budget.cascade_ops, RECORD_WRITE_BATCH))
        if not kids:
            await self._commit(user_id, [], {folder.item_id})
            budget.cascade_ops -= 1
            summary["deleted"] = summary.get("deleted", 0) + 1
            return True
        files = {k.item_id for k in kids if not k.is_folder}
        dying = [_dying(k, now) for k in kids if k.is_folder and k.state is not RecordState.DELETING]
        await self._commit(user_id, dying, files)
        budget.cascade_ops -= len(kids)
        summary["deleted"] = summary.get("deleted", 0) + len(files)
        return True

    async def _probe_step(self, user_id: str, rec: DriveIndexRecord, ctx: PlanContext, budget: _Budget) -> bool:
        if not budget.item_call_allowed():
            return False
        probe = await self._probe(user_id, rec.item_id, budget)
        if probe is None and rec.attempts + 1 < MAX_ATTEMPTS:
            await self._commit(user_id, [with_retry(rec, ctx.now)], set())
            return True
        if probe is None:   # out of attempts: classify without a probe, i.e. not dense (§8.2)
            probe = FolderProbe(marker_found=False, class_counts={}, sampled=0)
        parent = await self._parent_of(user_id, rec, ctx)
        plan = plan_observation(change_from_record(rec), replace(rec, attempts=0), parent, probe, ctx, parent,
                                observed=False)
        await self._commit_plan(user_id, plan)
        return True

    async def _resolve_step(self, user_id: str, rec: DriveIndexRecord, state: DriveSyncState, ctx: PlanContext,
                            budget: _Budget) -> bool:
        """An unresolved record: its parent may be stored by now; else ask the provider once (§3.4)."""
        parent = await self._parent_of(user_id, rec, ctx)
        if parent is not None or rec.parent_id == state.root_item_id:
            if parent is not None and parent.scope is Scope.UNRESOLVED:
                await self._commit(user_id, [replace(rec, due_at=None, priority=None)], set())
                return True   # waits for its parent's own resolution or probe; the parent's rescope re-classifies it
            await self._commit_plan(user_id, plan_observation(change_from_record(rec), rec, parent, None, ctx, parent,
                                                              observed=False))
            return True
        if not budget.item_call_allowed():
            return False
        budget.item_calls -= 1
        try:
            parent_change = await self._drive.get_change(user_id, rec.parent_id)
        except _PER_ITEM_ERRORS as exc:
            logger.warning(f"[DriveIndex] parent lookup failed for {user_id[:8]}: {exc}")
            if rec.attempts + 1 >= MAX_ATTEMPTS:
                await self._remove_orphan(user_id, rec, ctx)   # an orphan after the retry budget (§8.2)
            else:
                await self._commit(user_id, [with_retry(rec, ctx.now)], set())
            return True
        if parent_change.deleted:
            await self._remove_orphan(user_id, rec, ctx)
            return True
        grandparent = (await self._repo.get_records(user_id, [parent_change.parent_id])).get(parent_change.parent_id)
        await self._commit_plan(user_id, plan_observation(parent_change, None, grandparent, None, ctx))
        return True

    async def _remove_orphan(self, user_id: str, rec: DriveIndexRecord, ctx: PlanContext) -> None:
        """A folder goes through the cascade, so children parked under it go with it (§3.4, review A M6)."""
        if rec.is_folder:
            await self._commit(user_id, [_dying(rec, ctx.now)], set())
        else:
            await self._commit(user_id, [], {rec.item_id})

    async def _rescope_step(self, user_id: str, folder: DriveIndexRecord, ctx: PlanContext, budget: _Budget,
                            summary: Dict[str, int]) -> bool:
        """Re-classify one page of a folder's children with the folder's new scope (§5.2)."""
        if budget.cascade_ops <= 0:
            return False
        limit = min(budget.cascade_ops, RECORD_WRITE_BATCH)
        kids = await self._repo.children(user_id, folder.item_id, folder.cascade_cursor, limit)
        writes: Dict[str, DriveIndexRecord] = {}
        deletes: Set[str] = set()
        cache: Dict[str, Optional[DriveIndexRecord]] = {folder.item_id: folder}
        for kid in kids:
            if kid.state is RecordState.DELETING:
                continue   # on its way out; re-observing a stored record must never revive it
            current = cache.get(folder.item_id) or folder   # a marker child may have changed the folder already
            plan = plan_observation(change_from_record(kid), kid, current, None, ctx, current, observed=False)
            self._fold(plan, cache, writes, deletes)
        budget.cascade_ops -= max(len(kids), 1)
        current = cache.get(folder.item_id) or folder
        writes.pop(folder.item_id, None)
        await self._commit(user_id, list(writes.values()), deletes)    # the children first ...
        done = len(kids) < limit
        if current.scope is folder.scope:
            current = replace(current, rescope_pending=not done, cascade_cursor=None if done else kids[-1].item_id)
        await self._commit(user_id, [schedule(current, ctx.now, first_seen=not current.has_card)], set())   # ... marker last
        summary["rescoped"] = summary.get("rescoped", 0) + len(kids)
        return True

    async def _reobserve_step(self, user_id: str, folder: DriveIndexRecord, ctx: PlanContext, budget: _Budget,
                              summary: Dict[str, int]) -> bool:
        """One page of a never-stored subtree (§5.2): bounded per item call, resumable by a page cursor."""
        if not budget.item_call_allowed():
            return False
        budget.item_calls -= 1
        try:
            page = await self._drive.list_children_changes(user_id, folder.item_id, limit=RELIST_PAGE,
                                                           cursor=folder.reobserve_cursor)
        except DriveItemNotFoundError:
            await self._commit(user_id, [_relisted(folder, None, ctx.now)], set())   # gone: its tombstone follows
            return True
        except _PER_ITEM_ERRORS as exc:
            if folder.attempts + 1 >= MAX_ATTEMPTS:
                logger.warning(f"[DriveIndex] re-listing dropped for {user_id[:8]} after {MAX_ATTEMPTS} attempts: {exc}")
                await self._commit(user_id, [_relisted(folder, None, ctx.now)], set())
            else:
                logger.warning(f"[DriveIndex] re-listing failed for {user_id[:8]}: {exc}; backing off")
                await self._commit(user_id, [with_retry(folder, ctx.now)], set())
            return True
        await self._apply(user_id, ctx, list(page.changes), budget, summary, relisting=True)   # the children first ...
        fresh = (await self._repo.get_records(user_id, [folder.item_id])).get(folder.item_id)
        if fresh is not None:
            await self._commit(user_id, [_relisted(fresh, page.next_cursor, ctx.now)], set())   # ... marker last
        return True

    async def _parent_of(self, user_id: str, rec: DriveIndexRecord, ctx: PlanContext) -> Optional[DriveIndexRecord]:
        if not rec.parent_id or rec.parent_id == ctx.root_item_id:
            return None
        return (await self._repo.get_records(user_id, [rec.parent_id])).get(rec.parent_id)

    async def _commit_plan(self, user_id: str, plan: ObservationPlan) -> None:
        writes: Dict[str, DriveIndexRecord] = {}
        deletes: Set[str] = set()
        self._fold(plan, {}, writes, deletes)
        await self._commit(user_id, list(writes.values()), deletes)

    # -- cards and the card cap (§5.4, §8.5) ---------------------------------------------------

    async def _embed_cards(self, user_id: str, recs: List[DriveIndexRecord], now: datetime,
                           summary: Dict[str, int]) -> None:
        carded = await self._repo.count_carded(user_id)
        to_embed: List[DriveIndexRecord] = []
        evicted = 0
        for rec in sorted(recs, key=lambda r: r.card_rank):
            if not rec.has_card and carded >= MAX_CARD_RECORDS_PER_USER:
                victim = await self._repo.lowest_ranked_carded(user_id)
                if victim is not None and victim.card_rank > rec.card_rank:
                    await self._commit(user_id, [capped(victim, now)], set())
                    evicted += 1
                else:
                    await self._commit(user_id, [capped(rec, now)], set())
                    continue
            elif not rec.has_card:
                carded += 1
            to_embed.append(rec)
        semaphore = asyncio.Semaphore(EMBED_IN_FLIGHT)

        async def embed(rec: DriveIndexRecord) -> Tuple[DriveIndexRecord, List[float]]:
            async with semaphore:
                return rec, await self._embeddings.get_embedding(card_text(rec), "RETRIEVAL_DOCUMENT")

        outcomes = await asyncio.gather(*(embed(r) for r in to_embed), return_exceptions=True)
        results: List[Tuple[DriveIndexRecord, List[float]]] = []
        refused: List[DriveIndexRecord] = []
        for rec, outcome in zip(to_embed, outcomes):
            if isinstance(outcome, LLMClientError) and outcome.http_status == 400:
                # A definite bad-input refusal of this card text: the record backs off alone (premise_audit P9).
                logger.warning(f"[DriveIndex] card embedding refused for item {rec.item_id} of {user_id[:8]}: "
                               f"{outcome}; backing off")
                refused.append(rec)
                continue
            if isinstance(outcome, BaseException):
                # Anything else — quota, 5xx, other 4xx, network, unknown, cancellation — is systemic: the tick
                # ends and backs off with `attempts` untouched (§8.2).
                logger.warning(f"[DriveIndex] card embedding failed for {user_id[:8]}: "
                               f"{type(outcome).__name__}: {outcome}; ending the tick")
                raise outcome
            results.append(outcome)
        fresh = await self._repo.get_records(user_id, [rec.item_id for rec in to_embed])
        written: List[DriveIndexRecord] = []
        vectors: Dict[str, List[float]] = {}
        for rec in refused:
            now_rec = fresh.get(rec.item_id)
            if now_rec is not None:
                written.append(with_retry(now_rec, now))
        for rec, vec in results:
            now_rec = fresh.get(rec.item_id)
            # Written only over a record whose card is still the one embedded; anything else is rescheduled anyway.
            if now_rec is None or not card_eligible(now_rec) or card_text(now_rec) != card_text(rec):
                continue
            written.append(with_card(now_rec, card_hash(now_rec), self._space, now))
            vectors[rec.item_id] = vec
        await self._commit(user_id, written, set(), vectors)
        summary["cards"] = summary.get("cards", 0) + len(vectors)
        summary["evicted"] = summary.get("evicted", 0) + evicted

    async def _release_card_cap(self, user_id: str, state: DriveSyncState, now: datetime,
                                summary: Dict[str, int]) -> None:
        """Headroom releases capped records best rank first (§5.4: a document before a non-document)."""
        headroom = MAX_CARD_RECORDS_PER_USER - await self._repo.count_carded(user_id)
        if headroom <= 0:
            return
        released = await self._repo.card_capped(user_id, min(headroom, _DUE_PAGE))
        if not released:
            return
        ctx = self._ctx(user_id, state, now)
        for rec in released:
            parent = await self._parent_of(user_id, rec, ctx)
            plan = plan_observation(change_from_record(rec), replace(rec, reason=None), parent, None, ctx, parent,
                                    observed=False)
            await self._commit_plan(user_id, _as_maintenance(plan))
        summary["released"] = summary.get("released", 0) + len(released)

    # -- version upgrades (§12.3) --------------------------------------------------------------

    async def _upgrade_step(self, user_id: str, state: DriveSyncState, now: datetime, budget: _Budget,
                            summary: Dict[str, int]) -> DriveSyncState:
        """Pages through records while the maintenance share and the tick budget allow, at most to the end of
        one pass: the work a pass schedules runs in the due loop of a later tick, so the next pass waits."""
        target = dict(CURRENT_VERSIONS)
        while dict(state.applied_versions) != target and budget.time_left() and budget.maintenance_allowed():
            started = self._monotonic()
            state = await self._upgrade_page(user_id, state, target, now, summary)
            budget.maintenance_s += self._monotonic() - started
            if state.upgrade_cursor is None:
                break   # a pass ended (applied, or restarted to verify)
        return state

    async def _upgrade_page(self, user_id: str, state: DriveSyncState, target: Dict[str, int], now: datetime,
                            summary: Dict[str, int]) -> DriveSyncState:
        if dict(state.target_versions) != target:
            state = replace(state, target_versions=target, upgrade_cursor=None, upgrade_found_stale=False)
        ctx = self._ctx(user_id, state, now)
        page = await self._repo.records_page(user_id, state.upgrade_cursor, _DUE_PAGE)
        writes: Dict[str, DriveIndexRecord] = {}
        deletes: Set[str] = set()
        found = state.upgrade_found_stale
        for rec in page:
            if rec.state is RecordState.DELETING or not stale_versions(rec):
                continue
            found = True
            if rec.state is RecordState.UNRESOLVED and rec.due_at is None:
                continue   # parked under an unresolved parent, whose rescope re-plans it; scheduling only re-parks it
            parent = await self._parent_of(user_id, rec, ctx)
            self._fold(_as_maintenance(plan_observation(change_from_record(rec), rec, parent, None, ctx, parent,
                                                        observed=False)),
                       {}, writes, deletes)
        await self._commit(user_id, list(writes.values()), deletes)
        summary["upgraded"] = summary.get("upgraded", 0) + len(writes)
        if len(page) == _DUE_PAGE:
            return replace(state, upgrade_cursor=page[-1].item_id, upgrade_found_stale=found)
        if found:   # scheduling is not completion: verify with another full pass
            return replace(state, upgrade_cursor=None, upgrade_found_stale=False)
        logger.info(f"[DriveIndex] versions {target} applied for {user_id[:8]}")
        return replace(state, applied_versions=target, upgrade_cursor=None, upgrade_found_stale=False)

    # ------------------------------------------------------------------------------------------
    # Purge, continuation, helpers
    # ------------------------------------------------------------------------------------------

    async def _purge(self, user_id: str, state: DriveSyncState, tick_id: str, action: AccessAction,
                     enabled: bool, summary: Dict[str, int]) -> TickResult:
        """Durable: `purging` is written before the first delete, so a purge always finishes (review A M1)."""
        if not state.purging:
            await self._repo.save_sync_state(replace(state, purging=True))
        budget = _Budget(started=self._monotonic(), monotonic=self._monotonic)
        while budget.time_left():
            deleted = await self._repo.delete_user_records(user_id, RECORD_WRITE_BATCH)
            summary["purged"] = summary.get("purged", 0) + deleted
            if deleted == 0:
                await self._repo.delete_sync_state(user_id)
                logger.info(f"[DriveIndex] index purged for {user_id[:8]} ({action.value})")
                if enabled and action is not AccessAction.PURGE_EXPIRED:
                    await self._continue(user_id, tick_id)   # onboarding of the connected drive starts now
                return TickResult("purged", summary)
        await self._continue(user_id, tick_id)
        return TickResult("purging", summary)

    async def _has_more(self, user_id: str, state: DriveSyncState, now: datetime, *,
                        upgrade_wrote: bool = False) -> bool:
        if not state.round_complete or state.epoch_phase is EpochPhase.SWEEPING:
            return True
        if dict(state.applied_versions) != dict(CURRENT_VERSIONS) and (state.upgrade_cursor is not None
                                                                         or upgrade_wrote):
            # a pass is mid-way, or this tick's pass wrote (re-stamped or scheduled) and the verification pass
            # runs in-chain; a restarted pass that wrote nothing waits for due work or the fan-out (retro_tick F6)
            return True
        return bool(await self._repo.due_records(user_id, now, 1))   # backed-off work waits for its due_at

    async def _continue(self, user_id: str, tick_id: str, delay_seconds: int = 0) -> None:
        name = await self._queue.enqueue_worker_task(DRIVE_SYNC_TASK, tick_payload(user_id),
                                                     delay_seconds=delay_seconds, deadline_seconds=TICK_DEADLINE_S,
                                                     dedup_key=continuation_key(user_id, tick_id))
        if name is None:
            logger.warning(f"[DriveIndex] continuation for {user_id[:8]} dropped as a duplicate (tick {tick_id})")

    async def _requeue_nudge(self, user_id: str, nudge: Nudge) -> None:
        """A nudge never drops on a live lease: it comes back later under a fresh key (§7.1)."""
        again = replace(nudge, attempt=nudge.attempt + 1)
        await self._queue.enqueue_worker_task(DRIVE_SYNC_TASK, tick_payload(user_id, again),
                                              delay_seconds=NUDGE_RETRY_DELAY_S, deadline_seconds=TICK_DEADLINE_S,
                                              dedup_key=nudge_key(user_id, nudge.mutation_id, again.attempt))

    async def _recheck_gate(self, user_id: str) -> None:
        """Before a write batch (§7.1): an opt-out during the tick drops the in-flight result."""
        last = self._gate_checked.get(user_id)
        if last is not None and self._monotonic() - last < _GATE_RECHECK_S:
            return
        if not await self._enabled(user_id):
            raise _OptedOut()
        self._gate_checked[user_id] = self._monotonic()

    async def _enabled(self, user_id: str) -> bool:
        profile = await self._users.get_user(user_id)
        return bool(profile is not None and profile.config.drive_indexing_enabled)

    @staticmethod
    def _log(user_id: str, end: str, summary: Dict[str, int], budget: _Budget) -> None:
        elapsed = budget.monotonic() - budget.started
        logger.info(f"[DriveIndex] tick user={user_id[:8]} end={end} {summary} duration={elapsed:.1f}s")


def _dying(rec: DriveIndexRecord, now: datetime) -> DriveIndexRecord:
    return schedule(replace(rec, state=RecordState.DELETING, rescope_pending=False, probe_pending=False,
                            reobserve_pending=False, reobserve_cursor=None, has_card=False, card_hash=None),
                    now, first_seen=False)   # invariant: a deleting record carries no card


def _relisted(folder: DriveIndexRecord, next_cursor: Optional[str], now: datetime) -> DriveIndexRecord:
    """Progress of a re-listing: the next page, or done."""
    return schedule(replace(folder, reobserve_pending=next_cursor is not None, reobserve_cursor=next_cursor,
                            attempts=0), now, first_seen=not folder.has_card)


def _as_maintenance(plan: ObservationPlan) -> ObservationPlan:
    """Upgrade and release work runs in class 3 (§7.8, §12.3)."""
    if plan.upsert is None or plan.upsert.due_at is None:
        return plan
    return replace(plan, upsert=replace(plan.upsert, priority=int(Priority.MAINTENANCE)))
