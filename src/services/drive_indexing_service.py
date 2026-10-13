"""
DriveIndexingService — the per-user `drive_sync` tick (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §7, §8).

Gate -> lease -> observe -> work off -> continue. Every decision is the planner's (domain); this class
fetches, calls it and writes the result. Runs only on the indexer service (D5).

Write-order rule: inside a step, a progress marker (a cursor, a cleared flag) is always the LAST write,
so a crash between writes repeats the step instead of skipping work.

Public interface:
    DriveIndexingService(drive, repo, embeddings, queue, users, place_of, embedding_space, *, extractors=None,
                         triage=None, clock, monotonic)
    await tick(user_id, nudge=None) -> TickResult

`extractors` maps `drive_extraction.PARSER` / `READER` to their ContentExtractorPort and `triage` is the
ImageTriagePort (§24.2). Both may be left out where nothing is admitted; a record whose content or triage is due
without them ends the tick as an internal error (loud, retried with backoff, never a demotion).
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from ..domain import drive_index_policy as policy
from ..domain.drive_card import card_eligible, card_hash, card_text, repository_root
from ..domain.drive_chunker import chunk_text
from ..domain.drive_content_terms import content_terms
from ..domain.drive_extraction import (
    PARSER, READER, DriveModelUnavailable, ExtractionOutcome, ExtractionRequest, ExtractionStatus, TriageImage,
    TriageResult, chain_for, model_route,
)
from ..domain.drive_index import (
    CURRENT_VERSIONS, DriveChange, DriveChanges, DriveChunk, DriveIndexRecord, DriveIndexStoreUnavailable,
    DriveSyncState, EpochPhase, ErrorCategory, FolderProbe, LeaseLost, Nudge, Priority, Reason, RecordState, Scope,
)
from ..domain.drive_index_params import (
    CASCADE_OPS_PER_TICK, EMBED_FILE_BUDGET_S, EMBED_IN_FLIGHT, ITEM_CALLS_PER_TICK, LEASE_MARGIN, MAINTENANCE_SHARE,
    MAX_ATTEMPTS, MAX_CARD_RECORDS_PER_USER, MAX_CHUNKS_PER_USER, MODEL_EXTRACT_PAGES_PER_MONTH,
    NUDGE_RETRY_DELAY_S, PROBE_CHILDREN, READER_RENDITION_SIZE, RECORD_WRITE_BATCH, RELIST_PAGE, RETRY_CAP,
    SYSTEMIC_BACKOFF_BASE_S, SYSTEMIC_BACKOFF_CAP_S, TICK_DEADLINE_S, TICK_FILE_BUDGET, TICK_START_BUDGET,
    TRIAGE_BATCH, TRIAGE_IMAGES_PER_MONTH, TRIAGE_THUMBNAIL_SIZE,
)
from ..domain.drive_index_planner import (
    AccessAction, ObservationPlan, PlaceOf, PlanContext, access_action, attempt_crashed, can_start_file, capped,
    card_refused, change_from_record, chunks_reembedded, content_due, content_retry, deferred, demoted,
    extractor_stale, generation_started, indexed, model_available, model_outage, model_recovered, needs_probe,
    next_month_start, quiet_over,
    parent_scope, plan_observation, readme_read, reembed_in_space, reextraction_due, released, schedule,
    stale_versions, triage_due, triaged, usage_now, with_card, with_retry, with_usage, without_card,
)
from ..domain.drive_index_policy import (
    Admission, admission, is_marker_child, kind_of, probe_from_children, size_cap,
)
from ..domain.drive_index_run import (
    READER_IMAGE_MAX_BYTES, README_MAX_BYTES, RunStep, camera_prior, chunk_cap_refuses, chunk_released, file_suffix,
    gate_exempt, image_mime, no_rendition, original_fallback, outcome_step, reader_pages, readme_choice, releasable,
    routed,
)
from ..domain.drive_search import UNREAD_COUNTER
from ..domain.drive_sniff import SNIFF_HEAD_BYTES, sniff_kind
from ..domain.drive_task_keys import DRIVE_SYNC_TASK, continuation_key, nudge_key, tick_payload
from ..domain.drive_text_gate import is_machine_text
from ..domain.exceptions import LLMClientError, LLMError, LLMRateLimitError, LLMUnavailableError
from ..domain.user_drive import (
    DriveAccessDenied, DriveFileTooLarge, DriveItemNotFoundError, DriveNotConnectedError, DriveReauthRequired,
    DriveResyncRequired, DriveThrottled,
)
from ..ports.content_extractor_port import ContentExtractorPort
from ..ports.drive_index_repository import DriveIndexRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.image_triage_port import ImageTriagePort
from ..ports.task_queue import TaskQueue
from ..ports.user_drive_port import UserDrivePort
from ..ports.user_repository import UserRepository
from ..utils.logger import logger

_DUE_PAGE = 100
# Errors about ONE item (a probe, a parent lookup, a re-listing page, a nudge read): defer that item, never
# the user (§8.2). ValueError is the adapter's 5xx; OSError covers connection resets.
_PER_ITEM_ERRORS = (DriveAccessDenied, DriveItemNotFoundError, asyncio.TimeoutError, ValueError, OSError)
_STEP_ASIDE = timedelta(seconds=1)   # a deleting folder waiting for its children comes back after them
_GATE_RECHECK_S = 5.0   # the opt-out flag is re-read before a write batch at most this often


@dataclass
class TickResult:
    status: str                               # disabled | busy | purging | purged | access_lost | done | throttled |
                                              # error | lease_lost
    summary: Dict[str, int] = field(default_factory=dict)
    model_cost: float = 0.0                   # USD of the reader and triage calls of this tick, as the runners priced
                                              # them (§24.9)


class _OptedOut(Exception):
    """Indexing was turned off while the tick ran: drop the in-flight result (§7.1)."""


class _DriveSwitched(Exception):
    """A page came from another drive than the stored one (§7.1 step 1): purge and start fresh."""


class _NotWired(RuntimeError):
    """A content port the work needs is not wired: an internal error of the tick, never a per-file failure."""


class _ItemGone(Exception):
    """The provider has no such item any more (a download or a rendition said so): re-observe it, never read it as
    "no content" or "no thumbnail"."""


# What ends the tick when it escapes an index run or a re-embed (§8.2): the drive connection, a provider's throttling or
# outage, the embedding provider's errors, a transient index-store error and a wiring error. Anything else is this
# file's: a counted per-file retry.
_RUN_SYSTEMIC = (DriveNotConnectedError, DriveThrottled, DriveReauthRequired, LLMError, DriveIndexStoreUnavailable,
                 _NotWired)


@dataclass
class _RunMark:
    """Set once an index run's outcome is written: an error after it never rolls the outcome back."""
    committed: bool = False


@dataclass
class _Saved:
    """The sync state as last written during this tick. A systemic error writes its fields onto this, never
    onto the tick-start copy, so the cursor and epoch observe already saved survive (§8.2)."""
    state: DriveSyncState


@dataclass
class _Continuation:
    """The continuation a tick asks for. It is enqueued only after the lease is released, so it can never be
    dispatched while its own parent still holds the lease (§7.1)."""
    delay_s: Optional[int] = None

    def request(self, delay_s: int = 0) -> None:
        self.delay_s = delay_s


@dataclass(frozen=True)
class _Lease:
    """The lease this tick holds. Every sync-state write names `owner`; no write starts once the clock reaches
    `until - LEASE_MARGIN` (§7.1 fencing)."""
    owner: str
    until: datetime
    clock: Callable[[], datetime]

    def check(self) -> None:
        if self.clock() >= self.until - LEASE_MARGIN:
            raise LeaseLost(f"own lease ends at {self.until.isoformat()}")


# Per tick (each Cloud Tasks request runs in its own task, hence its own context), so a write deep in a step is
# fenced by the lease of the tick that runs it.
_TICK_LEASE: ContextVar[Optional[_Lease]] = ContextVar("drive_tick_lease", default=None)


@dataclass
class _Budget:
    started: float
    monotonic: Callable[[], float]
    item_calls: int = field(default_factory=lambda: ITEM_CALLS_PER_TICK)
    cascade_ops: int = field(default_factory=lambda: CASCADE_OPS_PER_TICK)
    maintenance_s: float = 0.0
    files: int = field(default_factory=lambda: TICK_FILE_BUDGET)

    def time_left(self) -> bool:
        return self.monotonic() - self.started < TICK_START_BUDGET.total_seconds()

    def file_allowed(self, *, routed: bool) -> bool:
        """An index run starts only within the file budget and the start budget, and only if its worst case still
        ends before the tick deadline (§7.1, §24.3)."""
        return (self.files > 0 and self.time_left()
                and can_start_file(self.monotonic() - self.started, routed=routed))

    def item_call_allowed(self) -> bool:
        """An item call (probe, parent lookup, re-listing page) starts only within both budgets: a slow provider
        must not push the tick past its deadline (RFC Δ9). The item waits for the next tick."""
        return self.item_calls > 0 and self.time_left()

    def maintenance_allowed(self) -> bool:
        return self.maintenance_s < MAINTENANCE_SHARE * TICK_START_BUDGET.total_seconds()


@dataclass
class _Models:
    """Model availability and monthly usage during one tick (§24.7, §24.8). Every sync-state save of the tick, the
    error paths included, carries them, so pages already sent are counted even when the tick ends on an error."""
    state: DriveSyncState
    calls: int = 0
    cost: float = 0.0
    saved: Optional[_Saved] = None   # the tick's last written sync state, which a usage write extends (F1-4)

    def available(self, now: datetime) -> bool:
        return model_available(self.state, now)

    def pages_left(self, now: datetime) -> int:
        return max(0, MODEL_EXTRACT_PAGES_PER_MONTH - usage_now(self.state, now)[0])

    def images_left(self, now: datetime) -> int:
        return max(0, TRIAGE_IMAGES_PER_MONTH - usage_now(self.state, now)[1])

    def outage(self, now: datetime) -> None:
        self.state = model_outage(self.state, now)

    def used(self, now: datetime, *, calls: int, cost: float, pages: int = 0, images: int = 0,
             recovered: bool = True) -> None:
        """Model work spent: count it as the runner reports it. `recovered`: it went through, so an outage is over;
        the part spent before an outage ends nothing, and the outage's backoff keeps doubling (§24.8)."""
        state = with_usage(self.state, now, pages=pages, images=images)
        self.state = model_recovered(state) if recovered else state
        self.calls += calls
        self.cost += cost

    def onto(self, state: DriveSyncState) -> DriveSyncState:
        m = self.state
        return replace(state, model_backoff_until=m.model_backoff_until, model_failures=m.model_failures,
                       usage_month=m.usage_month, model_pages_used=m.model_pages_used,
                       triage_images_used=m.triage_images_used)


def _count_reader(models: _Models, summary: Dict[str, int], now: datetime, *, calls: int, cost: float,
                  pages: int, outage: bool = False) -> None:
    """What the reader spent on one file — all of it, or the part before an `outage` (§24.7, §24.9). The part before
    an outage never ends it: the doubling backoff the outage then extends stands."""
    models.used(now, calls=calls, cost=cost, pages=pages, recovered=not outage)
    summary["model_calls"] = summary.get("model_calls", 0) + calls
    summary["model_pages"] = summary.get("model_pages", 0) + pages


def _count_triage(models: _Models, summary: Dict[str, int], now: datetime, *, calls: int, cost: float,
                  images: int, outage: bool = False) -> None:
    """What triage spent on one batch — all of it, or the part before an `outage`, which never ends it (§24.7-§24.9)."""
    models.used(now, calls=calls, cost=cost, images=images, recovered=not outage)
    summary["triage_calls"] = summary.get("triage_calls", 0) + calls
    summary["triage_images"] = summary.get("triage_images", 0) + images


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _lease_span() -> timedelta:
    return timedelta(seconds=TICK_DEADLINE_S) + LEASE_MARGIN


def _backoff_s(failures: int) -> int:
    return int(min(SYSTEMIC_BACKOFF_BASE_S * (2 ** failures), SYSTEMIC_BACKOFF_CAP_S))


class DriveIndexingService:
    def __init__(self, drive: UserDrivePort, repo: DriveIndexRepository, embeddings: EmbeddingService,
                 queue: TaskQueue, users: UserRepository, place_of: PlaceOf, embedding_space: str, *,
                 extractors: Optional[Mapping[str, ContentExtractorPort]] = None,
                 triage: Optional[ImageTriagePort] = None,
                 clock: Callable[[], datetime] = _utcnow, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._drive = drive
        self._repo = repo
        self._embeddings = embeddings
        self._queue = queue
        self._users = users
        self._place_of = place_of
        self._space = embedding_space
        self._extractors: Mapping[str, ContentExtractorPort] = dict(extractors or {})
        # each wired link's version: a new one re-extracts what that link decided (§9, §12.3)
        self._extractor_versions: Dict[str, int] = {k: e.version for k, e in self._extractors.items()}
        self._triage = triage
        self._clock = clock
        self._monotonic = monotonic
        self._gate_checked: Dict[str, float] = {}
        self._account_ids: Dict[str, str] = {}   # read with the opt-in flag; the reader and triage bill by it (§24.9)

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
            else:
                await self._requeue_busy(user_id, tick_id)
            return TickResult("busy")
        summary: Dict[str, int] = {}
        cont = _Continuation()
        token = _TICK_LEASE.set(_Lease(tick_id, lease.lease_until or now + _lease_span(), self._clock))
        try:
            state = lease if lease.created_at is not None else replace(lease, created_at=now)
            if state.epoch == 0 and state.cursor is None and not state.applied_versions:
                # nothing stored yet: onboarding builds every record under the current versions (§12.3)
                state = replace(state, applied_versions=self._target_versions(), applied_space=self._space)
            action = access_action(enabled, state, connection, now)   # facts may have moved before the lease
            if action in (AccessAction.PURGE_OPT_OUT, AccessAction.PURGE_EXPIRED,
                          AccessAction.PURGE_DRIVE_SWITCH, AccessAction.PURGE_THEN_RESTART):
                return await self._purge(user_id, state, action, enabled, summary, cont)
            if action is AccessAction.RECORD_LOSS:
                if state.access_lost_at is None:
                    await self._save_state(replace(state, access_lost_at=now))
                    logger.warning(f"[DriveIndex] access lost for {user_id[:8]}; grace period started")
                return TickResult("access_lost")
            if action is AccessAction.RESUME_AFTER_REGAIN:
                state = replace(state, access_lost_at=None)
                logger.info(f"[DriveIndex] access regained for {user_id[:8]}; continuing from the cursor")
            return await self._run(user_id, state, nudge, now, enabled, summary, cont)
        except LeaseLost as exc:
            # another tick holds the lease, or this one's is ending: it carries on from the last saved state
            logger.info(f"[DriveIndex] tick for {user_id[:8]} stopped, its lease is gone: {exc}")
            return TickResult("lease_lost", summary)
        finally:
            _TICK_LEASE.reset(token)
            await self._repo.release_lease(user_id, tick_id)
            if cont.delay_s is not None:
                await self._continue(user_id, tick_id, delay_seconds=cont.delay_s)

    async def _run(self, user_id: str, state: DriveSyncState, nudge: Optional[Nudge], now: datetime,
                   enabled: bool, summary: Dict[str, int], cont: _Continuation) -> TickResult:
        budget = _Budget(started=self._monotonic(), monotonic=self._monotonic)
        self._gate_checked[user_id] = budget.started   # the gate just read the flag
        state = replace(state, continuation_enqueued_at=None)
        saved = _Saved(state)
        models = _Models(state, saved=saved)
        try:
            if nudge is not None:
                await self._observe_nudge(user_id, state, nudge, now, budget, summary)
            state = await self._observe(user_id, state, now, budget, summary, saved)
            if state.round_complete:
                state = await self._work_off(user_id, state, now, budget, summary, models)
            more = await self._has_more(user_id, state, now, upgrade_wrote=summary.get("upgraded", 0) > 0)
            if more:
                cont.request()
            else:
                state = replace(state, counters=await self._counters(user_id))   # settled: refresh
            progressed = any(v for k, v in summary.items() if k != "pages")
            state = replace(models.onto(state), continuation_enqueued_at=now if more else None, last_error=None,
                            systemic_failures=0,
                            last_success_at=now if (progressed or not more) else state.last_success_at)
            await self._save_state(state)
            self._log(user_id, "continued" if more else "done", summary, budget, models)
            return TickResult("done", summary, models.cost)
        except LeaseLost:
            raise   # nothing more may be written, not even the error
        except _OptedOut:
            logger.info(f"[DriveIndex] indexing turned off mid-tick for {user_id[:8]}; result dropped")
            # The next tick's gate purges. Without it a lost toggle task leaves the purge to a fan-out that skips
            # the user on the stale continuation marker for up to 30 min (§7.1).
            cont.request()
            return TickResult("disabled", summary, models.cost)
        except _DriveSwitched:
            logger.warning(f"[DriveIndex] a page came from another drive for {user_id[:8]}; purging")
            return await self._purge(user_id, state, AccessAction.PURGE_DRIVE_SWITCH, enabled, summary, cont)
        except DriveReauthRequired:
            flagged = await self._drive.flag_reconnect(user_id)
            logger.warning(f"[DriveIndex] refresh rejected for {user_id[:8]} (flag set now: {flagged})")
            await self._save_state(replace(models.onto(saved.state), last_error=ErrorCategory.RECONNECT_NEEDED))
            return TickResult("access_lost", summary, models.cost)
        except (DriveThrottled, LLMRateLimitError, LLMUnavailableError) as exc:
            delay = _backoff_s(state.systemic_failures)
            if isinstance(exc, DriveThrottled):   # the provider's Retry-After first; a repeat backs off as well
                retry_after = int(exc.retry_after_s)
                delay = retry_after if state.systemic_failures == 0 else max(retry_after, delay)
            logger.warning(f"[DriveIndex] throttled for {user_id[:8]}: {exc}; continuing in {delay}s")
            cont.request(delay)
            await self._save_state(replace(models.onto(saved.state), last_error=ErrorCategory.THROTTLED,
                                           continuation_enqueued_at=now,
                                           systemic_failures=state.systemic_failures + 1))
            return TickResult("throttled", summary, models.cost)
        except Exception as exc:
            category = ErrorCategory.PROVIDER_ERROR if isinstance(exc, (DriveAccessDenied, ValueError)) \
                else ErrorCategory.INTERNAL
            delay = _backoff_s(state.systemic_failures)
            logger.error(f"[DriveIndex] tick failed for {user_id[:8]}: {exc}; continuing in {delay}s", exc_info=True)
            cont.request(delay)
            await self._save_state(replace(models.onto(saved.state), last_error=category,
                                           continuation_enqueued_at=now,
                                           systemic_failures=state.systemic_failures + 1))
            return TickResult("error", summary, models.cost)

    async def _counters(self, user_id: str) -> Dict[str, int]:
        """Records per state and carded (§3.5), readable chunks and `unread` (§10.2, §12.1). A zero content counter
        is left out: the Cabinet lists only non-zero counters and every reader defaults to 0."""
        counters = dict(await self._repo.count_by_state(user_id))
        content = {"chunks": await self._repo.readable_chunk_total(user_id),
                   UNREAD_COUNTER: await self._repo.count_unread(user_id, policy.ADMITTED_KINDS,
                                                                   policy.TRIAGED_KINDS)}
        counters.update({k: v for k, v in content.items() if v})
        return counters

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
                await self._save_state(state)
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
            await self._save_state(state)   # the cursor is the page's progress marker: written last
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
        collects: Set[str] = set()
        for change in changes:
            try:
                existing = cache.get(change.item_id)
                parent = cache.get(change.parent_id) if not change.deleted else None
                previous_parent = cache.get(existing.parent_id) if existing is not None else None
                probe = await self._maybe_probe(user_id, change, parent, existing, ctx, budget)
                plan = plan_observation(change, existing, parent, probe, ctx, previous_parent, nudged=nudged)
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
            self._fold(plan, cache, writes, deletes, collects)
        await self._commit(user_id, list(writes.values()), deletes, collects=collects)
        summary["observed"] = summary.get("observed", 0) + len(changes)

    @staticmethod
    def _fold(plan: ObservationPlan, cache: Dict[str, Optional[DriveIndexRecord]],
              writes: Dict[str, DriveIndexRecord], deletes: Set[str], collects: Optional[Set[str]] = None) -> None:
        if plan.collect_chunks is not None and collects is not None:
            collects.add(plan.collect_chunks)
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
        except Exception as exc:   # any other provider failure is a failed probe: the folder waits (§8.2)
            logger.warning(f"[DriveIndex] density probe failed for {user_id[:8]}: {type(exc).__name__}: {exc}")
            return None
        return probe_from_children(page.changes)

    async def _commit(self, user_id: str, writes: List[DriveIndexRecord], deletes: Set[str],
                      card_vectors: Optional[Dict[str, List[float]]] = None, *,
                      collects: Optional[Set[str]] = None) -> None:
        """`collects`: items whose chunks (every generation) go before the records are written (§3.4, §7.6)."""
        if not writes and not deletes and not collects:
            return
        _fence()
        await self._recheck_gate(user_id)
        for item_id in sorted(collects or ()):
            await self._repo.delete_chunks_except(user_id, item_id, None)
        for start in range(0, len(writes), RECORD_WRITE_BATCH):
            await self._repo.upsert_records(writes[start:start + RECORD_WRITE_BATCH], card_vectors)
        if deletes:
            await self._repo.delete_records(user_id, sorted(deletes))

    # ------------------------------------------------------------------------------------------
    # Work off (only after a complete round, RFC Δ15)
    # ------------------------------------------------------------------------------------------

    async def _work_off(self, user_id: str, state: DriveSyncState, now: datetime, budget: _Budget,
                        summary: Dict[str, int], models: _Models) -> DriveSyncState:
        if state.epoch_phase is EpochPhase.SWEEPING:
            state = await self._sweep(user_id, state, now, budget, summary)
        stopped_class, worked = await self._due_loop(user_id, state, now, budget, summary, models)
        if worked and _upgrade_waiting(state):
            state = replace(state, upgrade_found_stale=False)   # the work may have cleared what the pass left
        if stopped_class is None:
            state = await self._upgrade_step(user_id, state, now, budget, summary)
            await self._release_card_cap(user_id, now, summary)
            await self._release_chunk_cap(user_id, now, summary)
        return state

    async def _sweep(self, user_id: str, state: DriveSyncState, now: datetime, budget: _Budget,
                     summary: Dict[str, int]) -> DriveSyncState:
        """Delete what the epoch's enumeration did not see (§7.5). A full enumeration carries no tombstones,
        so a swept marker child is handled as a deletion: its folder re-opens (§5.2)."""
        ctx = self._ctx(user_id, state, now)
        while budget.cascade_ops > 0 and budget.time_left():
            stale = await self._repo.records_seen_before(user_id, state.epoch, min(budget.cascade_ops, RECORD_WRITE_BATCH))
            if not stale:
                if not await self._sweep_chunks(user_id, budget, summary):
                    return state   # the chunk backstop starts again next tick
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
            await self._commit(user_id, list(writes.values()), deletes, collects=_ever_read(stale))
            budget.cascade_ops -= len(stale)
            summary["swept"] = summary.get("swept", 0) + len(stale)
        return state

    async def _sweep_chunks(self, user_id: str, budget: _Budget, summary: Dict[str, int]) -> bool:
        """The backstop for every crash window (§7.5): chunks whose record is missing or whose generation is not the
        record's `indexed_generation`. One pass from the first chunk; True when it reached the end. A pass the budget
        cuts starts again on the next tick: its position is not stored, and one pass reads at most the user's
        readable chunks (MAX_CHUNKS_PER_USER) plus the debris it removes."""
        after: Optional[Tuple[str, int, int]] = None
        while budget.time_left():
            page = await self._repo.chunks_page(user_id, after, RECORD_WRITE_BATCH)
            if not page:
                return True
            recs = await self._repo.get_records(user_id, sorted({c.item_id for c in page}))
            debris = sorted({c.item_id for c in page
                             if c.item_id not in recs or c.generation != recs[c.item_id].indexed_generation})
            for item_id in debris:
                _fence()
                keep = recs[item_id].indexed_generation if item_id in recs else None
                deleted = await self._repo.delete_chunks_except(user_id, item_id, keep)
                summary["swept_chunks"] = summary.get("swept_chunks", 0) + deleted
            if len(page) < RECORD_WRITE_BATCH:
                return True
            after = (page[-1].item_id, page[-1].generation, page[-1].index)
        return False

    async def _due_loop(self, user_id: str, state: DriveSyncState, now: datetime, budget: _Budget,
                        summary: Dict[str, int], models: _Models) -> Tuple[Optional[int], bool]:
        """Work due records class by class (§7.8). Returns the class a budget stopped at (or None), and whether
        any record was worked.

        A class that runs out of its budget stops the loop: lower classes wait for the next tick. That is
        what keeps a repository's files from being carded while its rescope is unfinished (RFC Δ15)."""
        worked = False
        while budget.time_left():
            batch = await self._repo.due_records(user_id, now, _DUE_PAGE)
            if not batch:
                return None, worked
            cls = batch[0].priority if batch[0].priority is not None else int(Priority.RETRY)
            same = [r for r in batch if r.priority == cls]
            if cls >= Priority.MAINTENANCE and not budget.maintenance_allowed():
                return cls, worked
            started = self._monotonic()
            more_beyond = len(same) == _DUE_PAGE   # the page holds only this class: more of it may follow
            progressed = await self._work_class(user_id, state, same, now, budget, summary, models, more_beyond)
            worked |= progressed
            if cls >= Priority.MAINTENANCE:
                budget.maintenance_s += self._monotonic() - started
            if not progressed:
                return cls, worked
        return int(Priority.RETRY), worked

    async def _work_class(self, user_id: str, state: DriveSyncState, recs: List[DriveIndexRecord], now: datetime,
                          budget: _Budget, summary: Dict[str, int], models: _Models, more_beyond: bool) -> bool:
        """Content and triage come before the card of the same record, so a read file is carded once, with its
        lead text, brief or caption (§8.5); a card whose content cannot start now is embedded at once."""
        ctx = self._ctx(user_id, state, now)
        cards: List[DriveIndexRecord] = []
        images: List[DriveIndexRecord] = []
        progressed = False
        page_ids = {r.item_id for r in recs}
        for stale in recs:
            # An earlier step of this batch may have changed or removed it: always work the stored version.
            rec_or_none = (await self._repo.get_records(user_id, [stale.item_id])).get(stale.item_id)
            if rec_or_none is None or rec_or_none.due_at is None or rec_or_none.due_at > now:
                progressed = True
                continue
            rec = rec_or_none
            # its content may start now: due for other work (a card, a re-embed), it still keeps its quiet period
            content_now = content_due(rec, now) and quiet_over(rec, now)
            if rec.state is RecordState.DELETING:
                progressed |= await self._cascade_step(user_id, rec, now, budget, summary, page_ids)
            elif rec.probe_pending:
                progressed |= await self._probe_step(user_id, rec, ctx, budget)
            elif rec.state is RecordState.UNRESOLVED:
                progressed |= await self._resolve_step(user_id, rec, state, ctx, budget)
            elif rec.rescope_pending:
                progressed |= await self._rescope_step(user_id, rec, ctx, budget, summary)
            elif rec.reobserve_pending:
                progressed |= await self._reobserve_step(user_id, rec, ctx, budget, summary)
            elif rec.readme_pending:
                progressed |= await self._readme_step(user_id, rec, ctx, budget, summary)
            elif rec.has_card and not card_eligible(rec):
                await self._commit(user_id, [without_card(rec, now)], set())
                progressed = True
            elif content_now and await self._index_step(user_id, rec, ctx, budget, summary, models):
                progressed = True
            elif rec.chunk_reembed_pending and rec.indexed_generation is not None and not content_now:
                # any content state: a failed or a waiting record serves its readable generation too (§3.4, §7.4)
                progressed |= await self._reembed_step(user_id, rec, ctx, budget, summary)
            elif triage_due(rec, now) and quiet_over(rec, now):
                images.append(rec)
            elif card_eligible(rec) and rec.card_hash != card_hash(rec):
                cards.append(rec)
            elif not content_now:
                await self._commit(user_id, [schedule(rec, now, first_seen=False)], set())
                progressed = True
        if images:
            # a partial last batch waits for the next page only when this pass progresses anyway: no starvation
            others = progressed or bool(cards) or len(images) >= TRIAGE_BATCH
            progressed |= await self._triage_step(user_id, images, ctx, budget, summary, models,
                                                  hold_partial=more_beyond and others)
        if cards:
            await self._embed_cards(user_id, cards, now, summary)
            progressed = True
        return progressed

    async def _cascade_step(self, user_id: str, folder: DriveIndexRecord, now: datetime, budget: _Budget,
                            summary: Dict[str, int], page_ids: Set[str]) -> bool:
        """Folder delete, post-order: files go, child folders become deleting, the folder goes last (§7.6).

        `page_ids`: the due page being worked. A folder whose children are all deleting folders waits for them;
        when none of them is in this page it steps aside behind them, or a page full of such folders would keep
        their children from ever being worked."""
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
        if not files and not dying:   # its children are folders already deleting: their own steps do the work
            if any(k.item_id in page_ids for k in kids):
                return False   # worked in this page; the folder comes back in the next one
            await self._commit(user_id, [replace(folder, due_at=now + _STEP_ASIDE)], set())
            return True
        await self._commit(user_id, dying, files, collects=_ever_read([k for k in kids if not k.is_folder]))
        budget.cascade_ops -= len(files) + len(dying)
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
            rec = replace(rec, attempts=0)   # resolved: the next step (a probe) counts its own attempts (§8.2)
            if parent is not None and parent.scope is Scope.UNRESOLVED:
                # parked: waits for its parent's own resolution or probe, whose rescope re-classifies it. An
                # unresolved record is not card-eligible, and parked it is never due to drop its card (§3.4).
                await self._commit(user_id, [replace(rec, has_card=False, card_hash=None, due_at=None, priority=None)],
                                   set())
                return True
            await self._commit_plan(user_id, plan_observation(change_from_record(rec), rec, parent, None, ctx, parent,
                                                              observed=False))
            return True
        if not budget.item_call_allowed():
            return False
        budget.item_calls -= 1
        try:
            parent_change = await self._drive.get_change(user_id, rec.parent_id)
        except DriveItemNotFoundError:
            parent_change = DriveChange(item_id=rec.parent_id, deleted=True)   # definite: the parent is gone
        except _PER_ITEM_ERRORS as exc:
            if rec.attempts + 1 >= MAX_ATTEMPTS:
                # Transient failures prove nothing about the parent, so the record is never deleted on them; in delta
                # mode no later enumeration would settle it either, so it keeps retrying at the backoff cap.
                logger.warning(f"[DriveIndex] parent lookup failed {MAX_ATTEMPTS} times for {user_id[:8]}: {exc}; "
                               f"retrying in {RETRY_CAP}")
                await self._commit(user_id, [replace(rec, attempts=MAX_ATTEMPTS, due_at=ctx.now + RETRY_CAP,
                                                     priority=int(Priority.RETRY))], set())
            else:
                logger.warning(f"[DriveIndex] parent lookup failed for {user_id[:8]}: {exc}; backing off")
                await self._commit(user_id, [with_retry(rec, ctx.now)], set())
            return True
        if parent_change.deleted:
            await self._remove_orphan(user_id, rec, ctx)
            return True
        grandparent = (await self._repo.get_records(user_id, [parent_change.parent_id])).get(parent_change.parent_id)
        await self._commit_plan(user_id, plan_observation(parent_change, None, grandparent, None, ctx))
        return True

    async def _remove_orphan(self, user_id: str, rec: DriveIndexRecord, ctx: PlanContext) -> None:
        """A folder goes through the cascade, so children parked under it go with it (§3.4)."""
        if rec.is_folder:
            await self._commit(user_id, [_dying(rec, ctx.now)], set())
        else:
            await self._commit(user_id, [], {rec.item_id}, collects=_ever_read([rec]))

    async def _rescope_step(self, user_id: str, folder: DriveIndexRecord, ctx: PlanContext, budget: _Budget,
                            summary: Dict[str, int]) -> bool:
        """Re-classify one page of a folder's children with the folder's new scope (§5.2)."""
        if budget.cascade_ops <= 0:
            return False
        limit = min(budget.cascade_ops, RECORD_WRITE_BATCH)
        kids = await self._repo.children(user_id, folder.item_id, folder.cascade_cursor, limit)
        writes: Dict[str, DriveIndexRecord] = {}
        deletes: Set[str] = set()
        collects: Set[str] = set()
        cache: Dict[str, Optional[DriveIndexRecord]] = {folder.item_id: folder}
        for kid in kids:
            if kid.state is RecordState.DELETING:
                continue   # on its way out; re-observing a stored record must never revive it
            current = cache.get(folder.item_id) or folder   # a marker child may have changed the folder already
            plan = plan_observation(change_from_record(kid), kid, current, None, ctx, current, observed=False)
            self._fold(plan, cache, writes, deletes, collects)
        budget.cascade_ops -= max(len(kids), 1)
        current = cache.get(folder.item_id) or folder
        writes.pop(folder.item_id, None)
        await self._commit(user_id, list(writes.values()), deletes, collects=collects)    # the children first ...
        done = len(kids) < limit
        if current.scope is folder.scope:
            current = replace(current, rescope_pending=not done, cascade_cursor=None if done else kids[-1].item_id)
        await self._commit(user_id, [schedule(current, ctx.now, first_seen=not current.has_card)], set())   # ... marker last
        summary["rescoped"] = summary.get("rescoped", 0) + len(kids)
        return True

    async def _reobserve_step(self, user_id: str, folder: DriveIndexRecord, ctx: PlanContext, budget: _Budget,
                              summary: Dict[str, int]) -> bool:
        """One page of a never-stored subtree (§5.2): bounded per item call, resumable by a page cursor."""
        if folder.scope in (Scope.OPAQUE_ROOT, Scope.INSIDE_OPAQUE):
            await self._commit(user_id, [_relisted(folder, None, ctx.now)], set())   # nothing of it is stored
            return True
        if not budget.item_call_allowed():
            return False
        budget.item_calls -= 1
        try:
            page = await self._drive.list_children_changes(user_id, folder.item_id, limit=RELIST_PAGE,
                                                           cursor=folder.reobserve_cursor)
        except DriveItemNotFoundError:
            logger.info(f"[DriveIndex] re-listing target gone for {user_id[:8]}; its tombstone follows")
            await self._commit(user_id, [_relisted(folder, None, ctx.now)], set())
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
        collects: Set[str] = set()
        self._fold(plan, {}, writes, deletes, collects)
        await self._commit(user_id, list(writes.values()), deletes, collects=collects)

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
        systemic: Optional[BaseException] = None
        for rec, outcome in zip(to_embed, outcomes):
            if isinstance(outcome, LLMClientError) and outcome.http_status == 400:
                # A definite bad-input refusal of this card text: the record backs off alone (§8.2, RFC Δ24).
                logger.warning(f"[DriveIndex] card embedding refused for item {rec.item_id} of {user_id[:8]}: "
                               f"{outcome}; backing off")
                refused.append(rec)
                continue
            if isinstance(outcome, BaseException):
                # Anything else — quota, 5xx, other 4xx, network, unknown, cancellation — is systemic: the tick
                # ends and backs off with `attempts` untouched (§8.2), after the batch's other outcomes are kept.
                systemic = systemic or outcome
                continue
            results.append(outcome)
        fresh = await self._repo.get_records(user_id, [rec.item_id for rec in to_embed])
        written: List[DriveIndexRecord] = []
        vectors: Dict[str, List[float]] = {}
        for rec in refused:
            now_rec = fresh.get(rec.item_id)
            if now_rec is None:
                continue
            if now_rec.attempts + 1 >= MAX_ATTEMPTS and card_text(now_rec) == card_text(rec):
                logger.warning(f"[DriveIndex] card for item {rec.item_id} of {user_id[:8]} refused {MAX_ATTEMPTS} "
                               f"times; no card until its card text changes")
                written.append(card_refused(now_rec, now))
            else:
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
        if systemic is not None:
            logger.warning(f"[DriveIndex] card embedding failed for {user_id[:8]}: "
                           f"{type(systemic).__name__}: {systemic}; {len(vectors)} cards kept, ending the tick")
            raise systemic

    async def _release_card_cap(self, user_id: str, now: datetime, summary: Dict[str, int]) -> None:
        """Headroom releases capped records best rank first (§5.4: a document before a non-document)."""
        headroom = MAX_CARD_RECORDS_PER_USER - await self._repo.count_carded(user_id)
        if headroom <= 0:
            return
        capped_recs = await self._repo.card_capped(user_id, min(headroom, _DUE_PAGE))
        if not capped_recs:
            return
        # the structural reason was never touched by the cap, so lifting the flag is the whole decision
        await self._commit(user_id, [_maintenance(released(rec, now)) for rec in capped_recs], set())
        summary["released"] = summary.get("released", 0) + len(capped_recs)

    # -- the repository root's README (§5.2) -----------------------------------------------------

    async def _readme_step(self, user_id: str, rec: DriveIndexRecord, ctx: PlanContext, budget: _Budget,
                           summary: Dict[str, int]) -> bool:
        """One listing of the repository root (an item call) finds its README; the parser reads it (never the
        reader) and its first CARD_LEAD_CHARS become the root's lead text. A transient failure backs off; after
        MAX_ATTEMPTS the root keeps what it had."""
        now = ctx.now
        if not repository_root(rec):
            await self._commit(user_id, [readme_read(rec, None, "", "", now)], set())
            return True
        if not budget.item_call_allowed():
            return False
        budget.item_calls -= 1
        try:
            page = await self._drive.list_children_changes(user_id, rec.item_id, limit=RELIST_PAGE)
        except DriveItemNotFoundError:
            logger.info(f"[DriveIndex] repository {rec.item_id} of {user_id[:8]} is gone; its tombstone follows")
            await self._commit(user_id, [readme_read(rec, None, "", "", now)], set())
            return True
        except (DriveNotConnectedError, DriveThrottled) as exc:
            logger.warning(f"[DriveIndex] README listing hit a systemic error for {user_id[:8]}: {type(exc).__name__}")
            raise
        except _PER_ITEM_ERRORS as exc:
            logger.warning(f"[DriveIndex] README listing failed for {user_id[:8]}: {exc}")
            await self._commit(user_id, [self._readme_retry(rec, now)], set())
            return True
        readme = readme_choice(page.changes)
        if readme is None:
            await self._commit(user_id, [readme_read(rec, None, "", "", now)], set())
            return True
        if readme.item_id == rec.readme_item_id and readme.content_version == rec.readme_version:
            await self._commit(user_id, [readme_read(rec, readme.item_id, readme.content_version, rec.lead_text,
                                                     now)], set())
            return True
        kind = kind_of(readme.name, readme.mime_type, False)
        if admission(kind) is not Admission.CONTENT or readme.size_bytes > README_MAX_BYTES:
            await self._commit(user_id, [readme_read(rec, readme.item_id, readme.content_version, "", now)], set())
            return True
        if not budget.file_allowed(routed=False):
            return False
        budget.files -= 1
        text = await self._read_readme(user_id, readme, kind)
        if text is None:
            await self._commit(user_id, [self._readme_retry(rec, now)], set())
            return True
        await self._commit(user_id, [readme_read(rec, readme.item_id, readme.content_version, text, now)], set())
        summary["readmes"] = summary.get("readmes", 0) + 1
        return True

    @staticmethod
    def _readme_retry(rec: DriveIndexRecord, now: datetime) -> DriveIndexRecord:
        if rec.attempts + 1 >= MAX_ATTEMPTS:
            return readme_read(rec, rec.readme_item_id, rec.readme_version, rec.lead_text, now)   # keeps what it had
        return with_retry(rec, now)

    async def _read_readme(self, user_id: str, readme: DriveChange, kind: str) -> Optional[str]:
        """The README's text ("" when it has none, deterministically), or None on a transient failure."""
        with tempfile.TemporaryDirectory(prefix="drive-readme-") as tmp:
            path = os.path.join(tmp, "source" + file_suffix(readme.name))
            try:
                await self._drive.download_to_file(user_id, readme.item_id, path, README_MAX_BYTES)
            except (DriveFileTooLarge, DriveItemNotFoundError) as exc:
                logger.info(f"[DriveIndex] README {readme.item_id} of {user_id[:8]} not read: {type(exc).__name__}")
                return ""
            except (DriveNotConnectedError, DriveThrottled) as exc:
                logger.warning(f"[DriveIndex] README download hit a systemic error for {user_id[:8]}: "
                               f"{type(exc).__name__}")
                raise
            except _PER_ITEM_ERRORS as exc:
                logger.warning(f"[DriveIndex] README download failed for {user_id[:8]}: {exc}")
                return None
            sniffed = sniff_kind(await asyncio.to_thread(_read_head, path), kind)
            if admission(sniffed) is not Admission.CONTENT:
                return ""
            extractor = self._extractor(PARSER)
            try:
                outcome = await extractor.extract(ExtractionRequest(
                    path=path, name=readme.name, kind=sniffed, mime_type=readme.mime_type, user_id=user_id,
                    account_id=self._account_ids.get(user_id, ""), item_id=readme.item_id))
            except Exception as exc:   # the parser reports failures as outcomes; anything else is this file's
                logger.error(f"[DriveIndex] README parse failed for {user_id[:8]}: {exc}", exc_info=True)
                return None
        step, _ = outcome_step(outcome.status)
        if step is RunStep.RETRY:
            return None
        return outcome.text if outcome.status is ExtractionStatus.TEXT else ""

    # -- content: index runs (§8.1, §8.2, §7.4, §24.3) ------------------------------------------

    async def _index_step(self, user_id: str, rec: DriveIndexRecord, ctx: PlanContext, budget: _Budget,
                          summary: Dict[str, int], models: _Models) -> bool:
        """One due index run, or its deferral. False: it cannot start in this tick (the file budget, the start
        budget, or the deadline rule of §7.1/§24.3); the record stays due for the next tick."""
        now = ctx.now
        if attempt_crashed(rec, now):
            logger.warning(f"[DriveIndex] index run of item {rec.item_id} for {user_id[:8]} never ended; "
                           f"counted as a crashed attempt")
            await self._end_run(user_id, rec, content_retry(rec, Reason.EXTRACT_ERROR, now), rec.priority, summary)
            return True
        if chain_for(rec.kind)[0] == READER:
            held = self._model_hold(rec, now, models)
            if held is not None:
                await self._end_run(user_id, rec, held, rec.priority, summary)
                return True
        if not budget.file_allowed(routed=routed(rec.kind)):
            return False
        self._extractor(chain_for(rec.kind)[0])   # a wiring error ends the tick before anything is written
        budget.files -= 1
        run = generation_started(rec, now)
        await self._commit(user_id, [run], set())   # persisted before the download (§8.1 step 2)
        mark = _RunMark()
        try:
            await self._index_run(user_id, run, rec.priority, ctx, budget, summary, models, mark)
        except (LeaseLost, _OptedOut):
            raise   # nothing more may be written; an opt-out purges anyway
        except _RUN_SYSTEMIC as exc:
            if mark.committed:
                logger.warning(f"[DriveIndex] index run of item {rec.item_id} for {user_id[:8]} committed its "
                               f"outcome, then hit {type(exc).__name__}")
                raise
            logger.warning(f"[DriveIndex] index run of item {rec.item_id} for {user_id[:8]} ended by "
                           f"{type(exc).__name__}; the attempt is not counted")
            await self._abandon_run(user_id, run, rec.priority, now)
            raise
        except Exception as exc:
            if mark.committed:   # its debris is the sweep's (§7.5); the outcome stands and the tick goes on
                logger.error(f"[DriveIndex] index run of item {rec.item_id} for {user_id[:8]} failed after its "
                             f"outcome was committed: {type(exc).__name__}: {exc}", exc_info=True)
                return True
            logger.error(f"[DriveIndex] index run of item {rec.item_id} for {user_id[:8]} failed: "
                         f"{type(exc).__name__}: {exc}; counted as a per-file failure", exc_info=True)
            await self._end_run(user_id, run, content_retry(run, Reason.EXTRACT_ERROR, now), rec.priority, summary)
        return True

    async def _index_run(self, user_id: str, run: DriveIndexRecord, cls: Optional[int], ctx: PlanContext,
                         budget: _Budget, summary: Dict[str, int], models: _Models, mark: _RunMark) -> None:
        """§8.1 steps 3-9 for a record whose generation is persisted. Every outcome is one record write through
        `_end_run`, which sets `mark`; `cls` is the class the run started from."""
        now = ctx.now
        with tempfile.TemporaryDirectory(prefix="drive-index-") as tmp:   # the temp file goes in any case (§8.1)
            path = os.path.join(tmp, "source" + file_suffix(run.name))
            try:
                kind, final = await self._fetch(user_id, run, path, now)
            except _ItemGone:
                await self._item_gone(user_id, run, cls, ctx, budget, summary, mark)
                return
            if final is None and kind is not None and routed(kind) and not routed(run.kind) \
                    and not can_start_file(budget.monotonic() - budget.started, routed=True):
                # its bytes say it may go to the reader, whose worst case no longer fits this tick (§7.1): the run
                # waits for the next tick, which starts it first
                final = deferred(_cleared(run), now + _STEP_ASIDE, now, priority=cls)
            outcome: Optional[ExtractionOutcome] = None
            if final is None and kind is not None:
                outcome, final = await self._extract(user_id, run, path, kind, now, summary, models, cls)
        if final is None and outcome is not None and kind is not None:
            final = await self._read_text(user_id, run, outcome, kind, now, summary, mark)
        if final is not None:
            await self._end_run(user_id, run, final, cls, summary, mark, outcome=outcome)

    async def _fetch(self, user_id: str, run: DriveIndexRecord, path: str,
                     now: datetime) -> Tuple[Optional[str], Optional[DriveIndexRecord]]:
        """§8.1 steps 3-4: the file on local disk (an image: the provider's large rendition, §24.5) and the kind its
        bytes show; or, instead, the record a deterministic or per-item outcome leaves."""
        try:
            if chain_for(run.kind)[0] == READER:
                data = await self._drive.get_thumbnail(user_id, run.item_id, READER_RENDITION_SIZE)
                if data is not None:
                    await asyncio.to_thread(_write_file, path, data)
                    return run.kind, None   # a provider rendition (JPEG or PNG): nothing to sniff
                if not original_fallback(run):
                    return None, demoted(run, Reason.NO_TEXT, now)   # never decoded by us (§24.5)
                await self._drive.download_to_file(user_id, run.item_id, path, READER_IMAGE_MAX_BYTES)
            else:
                await self._drive.download_to_file(user_id, run.item_id, path, size_cap(run.kind))
        except DriveFileTooLarge:
            logger.info(f"[DriveIndex] item {run.item_id} of {user_id[:8]} is over its download cap")
            return None, demoted(run, Reason.TOO_LARGE, now)
        except DriveItemNotFoundError as exc:
            logger.info(f"[DriveIndex] item {run.item_id} of {user_id[:8]} is gone from the drive; re-observing it")
            raise _ItemGone() from exc
        except (DriveNotConnectedError, DriveThrottled) as exc:
            logger.warning(f"[DriveIndex] download of item {run.item_id} hit a systemic error for {user_id[:8]}: "
                           f"{type(exc).__name__}")
            raise   # systemic (§8.2)
        except _PER_ITEM_ERRORS as exc:
            logger.warning(f"[DriveIndex] download of item {run.item_id} failed for {user_id[:8]}: "
                           f"{type(exc).__name__}: {exc}")
            return None, content_retry(run, Reason.DOWNLOAD_ERROR, now)
        kind = sniff_kind(await asyncio.to_thread(_read_head, path), run.kind)   # part A's contract (§8.1 step 4)
        if kind != run.kind and admission(kind) is not Admission.CONTENT:
            logger.info(f"[DriveIndex] item {run.item_id} of {user_id[:8]} is {kind}, not {run.kind}")
            return None, demoted(run, Reason.KIND_NOT_ADMITTED, now)   # a mismatch goes back to the policy
        return kind, None

    async def _extract(self, user_id: str, run: DriveIndexRecord, path: str, kind: str, now: datetime,
                       summary: Dict[str, int], models: _Models,
                       cls: Optional[int]) -> Tuple[Optional[ExtractionOutcome], Optional[DriveIndexRecord]]:
        """§9 / §24.3 router: the kind's chain, the next link only on the parser-failed signals. The reader runs
        only while the model is available and the month has the pages the file needs left (§24.7, §24.8); a
        deferral keeps `cls`, the class the run started from. Returns the last link's outcome (for the run's log
        line, F4-6) and, when that outcome is not text to index, the record to write instead."""
        outcome: Optional[ExtractionOutcome] = None
        for position, extractor_id in enumerate(chain_for(kind)):
            if position > 0:
                why = model_route(outcome, kind) if outcome is not None else None
                if why is None:
                    break
                logger.info(f"[DriveIndex] item {run.item_id} of {user_id[:8]} goes to the {extractor_id}: {why}")
            max_pages = 0
            if extractor_id == READER:
                pages = reader_pages(kind, outcome if position > 0 else None)
                held = self._model_hold(run, now, models, cls=cls, pages=pages)
                if held is not None:
                    return None, held
                # The store before the model (final review F4-2): a store that cannot count the user's chunks ends
                # the tick before anything is billed, and a user with no chunk headroom costs no reader call. The
                # real count is checked again on the text (_read_text); one chunk is the least a read can add.
                if chunk_cap_refuses(await self._repo.readable_chunk_total(user_id), run, 1):
                    logger.info(f"[DriveIndex] item {run.item_id} of {user_id[:8]} refused by the chunk cap before "
                                f"the reader")
                    return None, demoted(run, Reason.USER_CHUNK_CAP, now, chunk_count=1)
                max_pages = models.pages_left(now)
            extractor = self._extractor(extractor_id)
            request = ExtractionRequest(path=path, name=run.name, kind=kind, mime_type=run.mime_type,
                                        user_id=user_id, account_id=self._account_ids.get(user_id, ""),
                                        max_model_pages=max_pages, item_id=run.item_id)
            try:
                outcome = await extractor.extract(request)
                if not outcome.extracted_by:   # the link that decided, whatever the outcome (§9 upgrade compares it)
                    outcome = replace(outcome, extracted_by=f"{extractor_id}@{extractor.version}")
            except DriveModelUnavailable as exc:
                logger.warning(f"[DriveIndex] reader unavailable for {user_id[:8]}: {exc}; model work deferred")
                _count_reader(models, summary, now, calls=exc.model_calls, cost=exc.model_cost, pages=exc.pages_sent,
                              outage=True)
                models.outage(now)   # after the usage, which leaves the backoff to this outage (C-M1, T-M2)
                await self._persist_usage(models)
                return None, deferred(_cleared(run), _backoff_until(models), now, priority=cls)
            except Exception as exc:   # the port reports per-file failures as outcomes; anything else is this file's
                logger.error(f"[DriveIndex] {extractor_id} failed on item {run.item_id} of {user_id[:8]}: {exc}",
                             exc_info=True)
                return None, content_retry(run, Reason.EXTRACT_ERROR, now)
            if extractor_id == READER:
                _count_reader(models, summary, now, calls=outcome.model_calls, cost=outcome.model_cost,
                              pages=outcome.pages_sent)
                await self._persist_usage(models)
        if outcome is None:   # an empty chain: no kind is admitted without an extractor (§5.1)
            raise _NotWired(f"no extractor chain for kind {kind}")
        step, reason = outcome_step(outcome.status)
        if step is RunStep.WAIT_MONTH:
            return outcome, deferred(_cleared(run), next_month_start(now), now, maintenance=True)
        if step is RunStep.DEMOTE and reason is not None:
            return outcome, demoted(run, reason, now, outcome=outcome)
        if step is RunStep.RETRY and reason is not None:
            retry = content_retry(run, reason, now)
            if retry.state is RecordState.FAILED:   # failed under this link: a new version of it gets a fresh try
                retry = replace(retry, extracted_by=outcome.extracted_by)
            return outcome, retry
        return outcome, None

    async def _read_text(self, user_id: str, run: DriveIndexRecord, outcome: ExtractionOutcome, kind: str,
                         now: datetime, summary: Dict[str, int], mark: _RunMark) -> Optional[DriveIndexRecord]:
        """§8.1 steps 6-9: gate, chunks, the chunk cap, embeddings, chunk writes, the fence and the commit. Returns
        the record to write, or None when the fence dropped the result."""
        # CPU work on up to MAX_TEXT_CHARS: in a worker thread, so the event loop keeps serving (lease, embeddings)
        if not gate_exempt(kind) and await asyncio.to_thread(is_machine_text, outcome.text):
            return demoted(run, Reason.MACHINE_TEXT, now, outcome=outcome)
        chunked = await asyncio.to_thread(chunk_text, outcome.text)
        if not chunked.chunks:
            return demoted(run, Reason.NO_TEXT, now, outcome=outcome)
        if chunk_cap_refuses(await self._repo.readable_chunk_total(user_id), run, len(chunked.chunks)):
            logger.info(f"[DriveIndex] item {run.item_id} of {user_id[:8]} refused by the chunk cap "
                        f"({len(chunked.chunks)} chunks)")
            return demoted(run, Reason.USER_CHUNK_CAP, now, outcome=outcome, chunk_count=len(chunked.chunks))
        started = self._monotonic()
        try:
            vectors = await asyncio.wait_for(self._embed_texts([c.text for c in chunked.chunks]),
                                             timeout=EMBED_FILE_BUDGET_S)
        except asyncio.TimeoutError:
            logger.warning(f"[DriveIndex] embeddings of item {run.item_id} for {user_id[:8]} passed "
                           f"{EMBED_FILE_BUDGET_S}s")
            return content_retry(run, Reason.EMBED_TIMEOUT, now)
        except LLMClientError as exc:
            if exc.http_status != 400:
                logger.warning(f"[DriveIndex] chunk embeddings of item {run.item_id} failed for {user_id[:8]}: {exc}")
                raise   # systemic (§8.2)
            logger.warning(f"[DriveIndex] a chunk of item {run.item_id} for {user_id[:8]} was refused: {exc}")
            return content_retry(run, Reason.EXTRACT_ERROR, now)
        summary["embed_ms"] = summary.get("embed_ms", 0) + int((self._monotonic() - started) * 1000)
        generation = run.index_generation
        terms = await asyncio.to_thread(_terms_of, [c.text for c in chunked.chunks])
        chunks = [DriveChunk(user_id=user_id, item_id=run.item_id, generation=generation, index=i, text=c.text,
                             char_start=c.char_start, char_end=c.char_end, kind=run.kind, content_terms=t)
                  for i, (c, t) in enumerate(zip(chunked.chunks, terms))]
        _fence()
        await self._recheck_gate(user_id)
        for start in range(0, len(chunks), RECORD_WRITE_BATCH):
            await self._repo.write_chunks(chunks[start:start + RECORD_WRITE_BATCH],
                                          vectors[start:start + RECORD_WRITE_BATCH])
        # The read fence (§7.4): commit only over the record this run started from, at the same content version.
        fresh = (await self._repo.get_records(user_id, [run.item_id])).get(run.item_id)
        if fresh is None or fresh.content_version != run.content_version or fresh.index_generation != generation:
            logger.info(f"[DriveIndex] item {run.item_id} of {user_id[:8]} changed during its run; result dropped")
            mark.committed = True   # the record is newer than the run: nothing may write the run back over it
            _fence()
            await self._repo.delete_chunks_except(user_id, run.item_id,
                                                  fresh.indexed_generation if fresh is not None else None)
            summary["dropped"] = summary.get("dropped", 0) + 1
            if fresh is not None and fresh.attempt_started_at == run.attempt_started_at:
                await self._commit(user_id, [replace(fresh, attempt_started_at=None)], set())
            return None
        summary["chunks"] = summary.get("chunks", 0) + len(chunks)
        return indexed(fresh, outcome=replace(outcome, truncated=outcome.truncated or chunked.truncated),
                       generation=generation, chunk_count=len(chunks), now=now, chunk_space=self._space)

    async def _embed_texts(self, texts: List[str]) -> List[List[float]]:
        """At most EMBED_IN_FLIGHT calls at once (§8.1 step 8). Any error but a 400 wins, so a quota error is never
        taken for this file's refusal."""
        semaphore = asyncio.Semaphore(EMBED_IN_FLIGHT)

        async def one(text: str) -> List[float]:
            async with semaphore:
                return await self._embeddings.get_embedding(text, "RETRIEVAL_DOCUMENT")

        results = await asyncio.gather(*(one(t) for t in texts), return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise next((e for e in errors if not (isinstance(e, LLMClientError) and e.http_status == 400)), errors[0])
        return [r for r in results if not isinstance(r, BaseException)]

    async def _reembed_step(self, user_id: str, rec: DriveIndexRecord, ctx: PlanContext, budget: _Budget,
                            summary: Dict[str, int]) -> bool:
        """§12.3: the readable chunks are in another embedding space. Their stored text is embedded again into a new
        generation, with no download and no extraction; the old generation serves until the new one commits under
        the same fence as an index run (§7.4). Any content state: a failed record's previous generation and a waiting
        record's current one are served too (§3.4), and their content state, attempts and deferral are left as they
        are (T-M1). Errors are classified like an index run's (§8.2): a systemic one ends the tick with the re-embed
        still due; chunks that cannot be read back as stored, a per-file embedding failure or any other error send the
        file to a full index run, which counts it."""
        if not budget.file_allowed(routed=False):
            return False
        readable = rec.indexed_generation
        if readable is None:
            return True
        stored = sorted(await self._repo.get_chunks(user_id, rec.item_id, readable, range(rec.chunk_count)),
                        key=lambda c: c.index)
        if rec.chunk_count == 0 or [c.index for c in stored] != list(range(rec.chunk_count)):
            logger.warning(f"[DriveIndex] chunks of item {rec.item_id} for {user_id[:8]} cannot be re-embedded as "
                           f"stored; the file is read again")
            await self._reread(user_id, rec, ctx)
            return True
        budget.files -= 1
        generation = rec.index_generation + 1
        await self._commit(user_id, [replace(rec, index_generation=generation)], set())   # never reused (§8.1 step 2)
        mark = _RunMark()
        try:
            await self._reembed_run(user_id, rec, stored, generation, ctx, summary, mark)
        except (LeaseLost, _OptedOut):
            raise   # nothing more may be written; an opt-out purges anyway
        except _RUN_SYSTEMIC as exc:
            logger.warning(f"[DriveIndex] re-embedding item {rec.item_id} for {user_id[:8]} ended by "
                           f"{type(exc).__name__}; it stays due")
            raise
        except Exception as exc:
            if mark.committed:   # its debris is the sweep's (§7.5); the outcome stands and the tick goes on
                logger.error(f"[DriveIndex] re-embedding item {rec.item_id} for {user_id[:8]} failed after its "
                             f"outcome was committed: {type(exc).__name__}: {exc}", exc_info=True)
                return True
            logger.error(f"[DriveIndex] re-embedding item {rec.item_id} for {user_id[:8]} failed: "
                         f"{type(exc).__name__}: {exc}; the file is read again", exc_info=True)
            await self._reread(user_id, rec, ctx)
        return True

    async def _reembed_run(self, user_id: str, rec: DriveIndexRecord, stored: List[DriveChunk], generation: int,
                           ctx: PlanContext, summary: Dict[str, int], mark: _RunMark) -> None:
        """The re-embed once its generation is persisted; `mark` is set once its outcome is written."""
        now = ctx.now
        readable = rec.indexed_generation
        started = self._monotonic()
        try:
            vectors = await asyncio.wait_for(self._embed_texts([c.text for c in stored]), timeout=EMBED_FILE_BUDGET_S)
        except asyncio.TimeoutError:
            logger.warning(f"[DriveIndex] re-embedding item {rec.item_id} for {user_id[:8]} passed "
                           f"{EMBED_FILE_BUDGET_S}s; the file is read again")
            await self._reread(user_id, rec, ctx)
            mark.committed = True
            return
        except LLMClientError as exc:
            if exc.http_status != 400:
                raise   # systemic (§8.2)
            logger.warning(f"[DriveIndex] a chunk of item {rec.item_id} for {user_id[:8]} was refused: {exc}")
            await self._reread(user_id, rec, ctx)
            mark.committed = True
            return
        summary["embed_ms"] = summary.get("embed_ms", 0) + int((self._monotonic() - started) * 1000)
        terms = await asyncio.to_thread(_terms_of, [c.text for c in stored])
        chunks = [replace(c, generation=generation, content_terms=t) for c, t in zip(stored, terms)]
        _fence()
        await self._recheck_gate(user_id)
        for start in range(0, len(chunks), RECORD_WRITE_BATCH):
            await self._repo.write_chunks(chunks[start:start + RECORD_WRITE_BATCH],
                                          vectors[start:start + RECORD_WRITE_BATCH])
        fresh = (await self._repo.get_records(user_id, [rec.item_id])).get(rec.item_id)
        if (fresh is None or fresh.content_version != rec.content_version or fresh.indexed_generation != readable
                or fresh.index_generation != generation):
            logger.info(f"[DriveIndex] item {rec.item_id} of {user_id[:8]} changed while re-embedding; dropped")
            mark.committed = True   # the record is newer than the re-embed: nothing may write over it
            _fence()
            await self._repo.delete_chunks_except(user_id, rec.item_id,
                                                  fresh.indexed_generation if fresh is not None else None)
            return
        done = chunks_reembedded(fresh, generation, self._space, now)
        if not (content_due(done) or triage_due(done)):   # waiting content keeps its own class and time (§7.8)
            done = _maintenance_kept(done, rec.priority)
        await self._commit(user_id, [done], set())
        mark.committed = True
        _fence()
        await self._repo.delete_chunks_except(user_id, rec.item_id, generation)
        summary["reembedded"] = summary.get("reembedded", 0) + 1

    async def _reread(self, user_id: str, rec: DriveIndexRecord, ctx: PlanContext) -> None:
        """The content no longer stands: planned again, the file is pending for a full index run (§9, §12.3)."""
        fresh = (await self._repo.get_records(user_id, [rec.item_id])).get(rec.item_id) or rec
        parent = await self._parent_of(user_id, fresh, ctx)
        writes: Dict[str, DriveIndexRecord] = {}
        deletes: Set[str] = set()
        collects: Set[str] = set()
        current = replace(reextraction_due(fresh), chunk_reembed_pending=False)
        self._fold(_as_maintenance(plan_observation(change_from_record(fresh), current, parent, None, ctx, parent,
                                                    observed=False)), {}, writes, deletes, collects)
        if fresh.item_id not in writes and fresh.item_id not in deletes:
            writes[fresh.item_id] = _maintenance(schedule(current, ctx.now, first_seen=False))
        await self._commit(user_id, list(writes.values()), deletes, collects=collects)

    async def _end_run(self, user_id: str, before: DriveIndexRecord, final: DriveIndexRecord, cls: Optional[int],
                       summary: Dict[str, int], mark: Optional[_RunMark] = None, *,
                       outcome: Optional[ExtractionOutcome] = None) -> None:
        """The run's one record write, then its debris: every generation but the readable one goes (§7.4), a crashed
        run's partial chunks included. Terminal outcomes of maintenance work keep class 3 for the card (§7.8). One INFO
        line names the outcome (F4-6); `outcome` is what the last extractor link reported, when one ran."""
        if final.state is not RecordState.PENDING:
            final = _maintenance_kept(final, cls)
        await self._commit(user_id, [final], set())
        if mark is not None:
            mark.committed = True
        _fence()
        await self._repo.delete_chunks_except(user_id, final.item_id, final.indexed_generation)
        key = _outcome_key(before, final)
        summary[key] = summary.get(key, 0) + 1
        logger.info(_run_line(user_id, key, final, outcome))

    async def _abandon_run(self, user_id: str, run: DriveIndexRecord, cls: Optional[int], now: datetime) -> None:
        """A systemic error ends the tick mid-run (§8.2): the attempt is cleared with `attempts` untouched and the
        content due again; the started generation is never reused. Best effort: the error itself is re-raised."""
        back = _maintenance_kept(schedule(_cleared(replace(run, defer_until=None, defer_priority=None)), now,
                                          first_seen=not run.has_card), cls)
        try:
            _fence()
            await self._repo.upsert_records([back])
        except Exception as exc:
            logger.warning(f"[DriveIndex] could not clear the run of item {run.item_id} for {user_id[:8]}: {exc}; "
                           f"it counts as a crashed attempt")

    async def _persist_usage(self, models: _Models) -> None:
        """The month's usage (and the model's outage state) written as soon as it is spent: after each reader file and
        each triage batch (final review F1-4), so a tick the instance kills, or whose lease is lost, still counts the
        pages and images it sent. One fenced write over the tick's last saved state; it raises LeaseLost once the lease
        is gone, like every other write of the tick."""
        if models.saved is None:
            return
        state = models.onto(models.saved.state)
        await self._save_state(state)
        models.saved.state = state

    def _model_hold(self, rec: DriveIndexRecord, now: datetime, models: _Models, *, cls: Optional[int] = None,
                    pages: int = 1) -> Optional[DriveIndexRecord]:
        """Model work waits, still pending, while the model is out (§24.8) or until a month has the `pages` the file
        needs (reader_pages): it is never cut to what is left of this month (§24.7)."""
        if not models.available(now):
            return deferred(_cleared(rec), _backoff_until(models), now, priority=cls)
        if models.pages_left(now) < pages:
            return deferred(_cleared(rec), next_month_start(now), now, maintenance=True)
        return None

    def _extractor(self, extractor_id: str) -> ContentExtractorPort:
        extractor = self._extractors.get(extractor_id)
        if extractor is None:
            raise _NotWired(f"content extractor {extractor_id!r} is not wired into the indexing service")
        return extractor

    # -- content: image triage (§5.3, §24.5) ---------------------------------------------------

    async def _triage_step(self, user_id: str, recs: List[DriveIndexRecord], ctx: PlanContext, budget: _Budget,
                           summary: Dict[str, int], models: _Models, *, hold_partial: bool) -> bool:
        """Due images in batches of TRIAGE_BATCH thumbnails per call. `hold_partial`: more of this class follows
        and the pass already progressed, so a partial last batch waits for the next page and N images cost
        ⌈N / TRIAGE_BATCH⌉ calls (§16)."""
        now = ctx.now
        if not models.available(now):
            await self._commit(user_id, [deferred(r, _backoff_until(models), now) for r in recs], set())
            summary["deferred"] = summary.get("deferred", 0) + len(recs)
            return True
        left = models.images_left(now)
        over = [deferred(r, next_month_start(now), now, maintenance=True) for r in recs[left:]]
        if over:
            await self._commit(user_id, over, set())
            summary["deferred"] = summary.get("deferred", 0) + len(over)
        recs = recs[:left]
        if hold_partial and len(recs) % TRIAGE_BATCH:
            recs = recs[:len(recs) - len(recs) % TRIAGE_BATCH]
        if not recs:
            return bool(over)
        port = self._triage_port()
        for start in range(0, len(recs), TRIAGE_BATCH):
            if not budget.time_left():
                break
            batch = recs[start:start + TRIAGE_BATCH]
            if not models.available(now):   # an outage earlier in this pass
                await self._commit(user_id, [deferred(r, _backoff_until(models), now) for r in batch], set())
                summary["deferred"] = summary.get("deferred", 0) + len(batch)
                continue
            await self._triage_batch(user_id, port, batch, ctx, budget, summary, models)
            await self._persist_usage(models)
        return True

    async def _triage_batch(self, user_id: str, port: ImageTriagePort, batch: List[DriveIndexRecord],
                            ctx: PlanContext, budget: _Budget, summary: Dict[str, int], models: _Models) -> None:
        now = ctx.now
        writes: List[DriveIndexRecord] = []
        images: List[TriageImage] = []
        waiting: Dict[str, DriveIndexRecord] = {}
        gone: List[DriveIndexRecord] = []
        for rec in batch:
            try:
                thumbnail = await self._drive.get_thumbnail(user_id, rec.item_id, TRIAGE_THUMBNAIL_SIZE)
            except DriveItemNotFoundError:
                logger.info(f"[DriveIndex] image {rec.item_id} of {user_id[:8]} is gone from the drive; re-observing it")
                gone.append(rec)   # not "no thumbnail": the item itself is gone
                continue
            except (DriveNotConnectedError, DriveThrottled) as exc:
                logger.warning(f"[DriveIndex] thumbnail of item {rec.item_id} hit a systemic error for "
                               f"{user_id[:8]}: {type(exc).__name__}")
                await self._commit(user_id, writes, set())   # the verdicts already taken stay
                raise
            except _PER_ITEM_ERRORS as exc:
                logger.warning(f"[DriveIndex] thumbnail of item {rec.item_id} failed for {user_id[:8]}: "
                               f"{type(exc).__name__}: {exc}")
                writes.append(content_retry(rec, Reason.DOWNLOAD_ERROR, now))
                continue
            if thumbnail is None:   # no rendition yet: retried; a photograph without a caption at MAX_ATTEMPTS (§24.5)
                after = no_rendition(rec, now)
                writes.append(_maintenance_kept(after, rec.priority) if after.triage_verdict is not None else after)
                continue
            images.append(TriageImage(item_id=rec.item_id, thumbnail=thumbnail, mime_type=image_mime(thumbnail[:8]),
                                      camera_prior=camera_prior(rec.facets)))
            waiting[rec.item_id] = rec
        if images:
            try:
                batch_outcome = await port.triage(user_id, self._account_ids.get(user_id, ""), images)
            except DriveModelUnavailable as exc:
                logger.warning(f"[DriveIndex] triage unavailable for {user_id[:8]}: {exc}; model work deferred")
                _count_triage(models, summary, now, calls=exc.model_calls, cost=exc.model_cost,
                              images=exc.images_sent, outage=True)
                models.outage(now)   # after the usage, which leaves the backoff to this outage (C-M1, T-M2)
                writes.extend(deferred(r, _backoff_until(models), now) for r in waiting.values())
                summary["deferred"] = summary.get("deferred", 0) + len(waiting)
            except Exception as exc:   # a refusal or unreadable output: per-file transient for the batch (§24.8)
                logger.error(f"[DriveIndex] triage failed for {user_id[:8]}: {exc}", exc_info=True)
                writes.extend(content_retry(r, Reason.EXTRACT_ERROR, now) for r in waiting.values())
            else:
                _count_triage(models, summary, now, calls=batch_outcome.model_calls, cost=batch_outcome.model_cost,
                              images=len(images))
                by_id = {r.item_id: r for r in batch_outcome.results}
                for item_id, rec in waiting.items():
                    result = by_id.get(item_id)
                    writes.append(_verdict(rec, result, now) if result is not None
                                  else content_retry(rec, Reason.EXTRACT_ERROR, now))
        await self._commit(user_id, writes, set())
        summary["triaged"] = summary.get("triaged", 0) + sum(1 for w in writes if w.triage_verdict is not None)
        for rec in gone:
            await self._item_gone(user_id, rec, rec.priority, ctx, budget, summary)

    async def _item_gone(self, user_id: str, rec: DriveIndexRecord, cls: Optional[int], ctx: PlanContext,
                         budget: _Budget, summary: Dict[str, int], mark: Optional[_RunMark] = None) -> None:
        """A download or a rendition found no item (§7.6): it is observed again at once instead of waiting for its
        tombstone. A deletion removes the record and its chunks; an item that is there after all costs a per-file
        attempt (§8.2), so a provider that keeps contradicting itself cannot loop."""
        try:
            change = await self._drive.get_change(user_id, rec.item_id)
        except DriveItemNotFoundError:
            change = DriveChange(item_id=rec.item_id, deleted=True)
        except (DriveNotConnectedError, DriveThrottled) as exc:
            logger.warning(f"[DriveIndex] re-reading item {rec.item_id} hit a systemic error for {user_id[:8]}: "
                           f"{type(exc).__name__}")
            raise
        except _PER_ITEM_ERRORS as exc:
            logger.warning(f"[DriveIndex] re-reading item {rec.item_id} failed for {user_id[:8]}: {exc}")
            await self._end_run(user_id, rec, content_retry(rec, Reason.DOWNLOAD_ERROR, ctx.now), cls, summary, mark)
            return
        await self._apply(user_id, ctx, [change], budget, summary)
        if mark is not None:
            mark.committed = True   # the re-observation is written: the run's snapshot is outdated
        summary["gone"] = summary.get("gone", 0) + int(change.deleted)
        fresh = (await self._repo.get_records(user_id, [rec.item_id])).get(rec.item_id)
        if fresh is None:
            return   # deleted, its chunks with it (§7.6)
        if fresh.content_version == rec.content_version and (content_due(fresh) or triage_due(fresh)):
            await self._end_run(user_id, rec, content_retry(fresh, Reason.DOWNLOAD_ERROR, ctx.now), cls, summary, mark)
        elif fresh.attempt_started_at is not None and fresh.attempt_started_at == rec.attempt_started_at:
            await self._commit(user_id, [replace(fresh, attempt_started_at=None)], set())   # a new version: no crash

    def _triage_port(self) -> ImageTriagePort:
        if self._triage is None:
            raise _NotWired("image triage is not wired into the indexing service")
        return self._triage

    async def _release_chunk_cap(self, user_id: str, now: datetime, summary: Dict[str, int]) -> None:
        """§5.4: a file the chunk cap refused is due again only when the headroom holds its recorded chunk_count,
        best rank first, then newest; never re-extracted before it can fit."""
        headroom = MAX_CHUNKS_PER_USER - await self._repo.readable_chunk_total(user_id)
        if headroom <= 0:
            return
        fits = releasable(await self._repo.chunk_capped(user_id, _DUE_PAGE), headroom)
        if fits:
            await self._commit(user_id, [chunk_released(rec, now) for rec in fits], set())
            summary["released_chunks"] = summary.get("released_chunks", 0) + len(fits)

    # -- version upgrades (§12.3) --------------------------------------------------------------

    async def _upgrade_step(self, user_id: str, state: DriveSyncState, now: datetime, budget: _Budget,
                            summary: Dict[str, int]) -> DriveSyncState:
        """Pages through records while the maintenance share and the tick budget allow, at most to the end of
        one pass: the work a pass schedules runs in the due loop of a later tick, so the next pass waits.

        A pass that left stale records (scheduled, backing off, or parked) is not repeated until due work has run
        or the target versions change: a record stuck in backoff must not cost a full read of the index per tick."""
        target = self._target_versions()
        if _upgrade_waiting(state) and dict(state.target_versions) == target and state.target_space == self._space:
            return state
        while self._upgrade_due(state) and budget.time_left() and budget.maintenance_allowed():
            started = self._monotonic()
            state = await self._upgrade_page(user_id, state, target, now, summary)
            budget.maintenance_s += self._monotonic() - started
            if state.upgrade_cursor is None:
                break   # a pass ended (applied, or restarted to verify)
        return state

    async def _upgrade_page(self, user_id: str, state: DriveSyncState, target: Dict[str, int], now: datetime,
                            summary: Dict[str, int]) -> DriveSyncState:
        if dict(state.target_versions) != target or state.target_space != self._space:
            state = replace(state, target_versions=target, target_space=self._space, upgrade_cursor=None,
                            upgrade_found_stale=False)
        ctx = self._ctx(user_id, state, now)
        page = await self._repo.records_page(user_id, state.upgrade_cursor, _DUE_PAGE)
        writes: Dict[str, DriveIndexRecord] = {}
        deletes: Set[str] = set()
        collects: Set[str] = set()
        found = state.upgrade_found_stale   # a stale record this pass left stale (its work is due, or blocked)
        for rec in page:
            if rec.state is RecordState.DELETING or not self._stale(rec):
                continue
            if rec.state is RecordState.UNRESOLVED and rec.due_at is None:
                # parked under an unresolved parent, whose rescope re-plans it under the current versions; scheduling
                # it only re-parks it, and it holds no card, so it does not keep the versions from applying
                continue
            parent = await self._parent_of(user_id, rec, ctx)
            current = reembed_in_space(rec, self._space)
            if extractor_stale(rec, self._extractor_versions):
                current = reextraction_due(current)   # that link's outcome no longer stands: read again (§9)
            self._fold(_as_maintenance(plan_observation(change_from_record(rec), current, parent, None, ctx, parent,
                                                        observed=False)),
                       {}, writes, deletes, collects)
            if rec.item_id not in writes and rec.item_id not in deletes and current != rec:
                # nothing observable moved and the record is already scheduled (a quiet period, a deferral): the
                # planner keeps it as it is, so the re-embed or re-read this pass decided is written here (§12.3)
                writes[rec.item_id] = _maintenance(schedule(current, now, first_seen=False))
            if rec.item_id not in deletes and self._stale(writes.get(rec.item_id, rec)):
                found = True
        await self._commit(user_id, list(writes.values()), deletes, collects=collects)
        summary["upgraded"] = summary.get("upgraded", 0) + len(writes)
        if len(page) == _DUE_PAGE:
            return replace(state, upgrade_cursor=page[-1].item_id, upgrade_found_stale=found)
        if found:   # scheduling is not completion: the next pass verifies once due work has run
            return replace(state, upgrade_cursor=None, upgrade_found_stale=True)
        logger.info(f"[DriveIndex] versions {target} in {self._space} applied for {user_id[:8]}")
        return replace(state, applied_versions=target, applied_space=self._space, upgrade_cursor=None,
                       upgrade_found_stale=False)

    # ------------------------------------------------------------------------------------------
    # Purge, continuation, helpers
    # ------------------------------------------------------------------------------------------

    async def _purge(self, user_id: str, state: DriveSyncState, action: AccessAction, enabled: bool,
                     summary: Dict[str, int], cont: _Continuation) -> TickResult:
        """Durable: `purging` is written before the first delete, so a purge always finishes (§7.6)."""
        if not state.purging:
            await self._save_state(replace(state, purging=True))
        budget = _Budget(started=self._monotonic(), monotonic=self._monotonic)
        while budget.time_left():
            _fence()
            chunks = await self._repo.delete_user_chunks(user_id, RECORD_WRITE_BATCH)   # chunks first (§7.6)
            if chunks:
                summary["purged_chunks"] = summary.get("purged_chunks", 0) + chunks
                continue
            deleted = await self._repo.delete_user_records(user_id, RECORD_WRITE_BATCH)
            summary["purged"] = summary.get("purged", 0) + deleted
            if deleted == 0:
                _fence()
                await self._repo.delete_sync_state(user_id, _owner())
                logger.info(f"[DriveIndex] index purged for {user_id[:8]} ({action.value})")
                if enabled and action is not AccessAction.PURGE_EXPIRED:
                    cont.request()   # onboarding of the connected drive starts now
                return TickResult("purged", summary)
        cont.request()
        return TickResult("purging", summary)

    async def _has_more(self, user_id: str, state: DriveSyncState, now: datetime, *,
                        upgrade_wrote: bool = False) -> bool:
        if not state.round_complete or state.epoch_phase is EpochPhase.SWEEPING:
            return True
        if self._upgrade_due(state) and (state.upgrade_cursor is not None or upgrade_wrote):
            # a pass is mid-way, or this tick's pass wrote (re-stamped or scheduled) and the verification pass
            # runs in-chain; a restarted pass that wrote nothing waits for due work or the fan-out (RFC Δ30)
            return True
        # backed-off work waits for its due_at; a folder that stepped aside is due right after this tick
        return bool(await self._repo.due_records(user_id, now + _STEP_ASIDE, 1))

    async def _save_state(self, state: DriveSyncState) -> None:
        _fence()
        await self._repo.save_sync_state(state, _owner())

    def _upgrade_due(self, state: DriveSyncState) -> bool:
        """The version constants or the embedding space differ from what the index was last verified under."""
        return dict(state.applied_versions) != self._target_versions() or state.applied_space != self._space

    def _target_versions(self) -> Dict[str, int]:
        """The version map an index is verified under: the domain constants and each wired extractor's version
        (`extractor:<id>`), so a new parser or reader starts an upgrade pass (§9, §12.3)."""
        return {**dict(CURRENT_VERSIONS), **{f"extractor:{k}": v for k, v in self._extractor_versions.items()}}

    def _stale(self, rec: DriveIndexRecord) -> bool:
        return (stale_versions(rec, embedding_space=self._space)
                or extractor_stale(rec, self._extractor_versions))

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

    async def _requeue_busy(self, user_id: str, tick_id: str) -> None:
        """A tick that finds the lease busy comes back once per holder: a continuation can be dispatched while the
        tick that enqueued it still holds the lease, and that tick has already saved its continuation marker, so
        the fan-out skips the user (§7.1). One key per holder dedups redeliveries and every other busy tick."""
        holder = await self._repo.get_sync_state(user_id)
        owner = holder.lease_owner if holder is not None and holder.lease_owner else tick_id
        name = await self._queue.enqueue_worker_task(DRIVE_SYNC_TASK, tick_payload(user_id),
                                                     delay_seconds=NUDGE_RETRY_DELAY_S,
                                                     deadline_seconds=TICK_DEADLINE_S,
                                                     dedup_key=continuation_key(user_id, f"busy-{owner}"))
        logger.info(f"[DriveIndex] lease busy for {user_id[:8]}; "
                    f"{'retrying in ' + str(NUDGE_RETRY_DELAY_S) + 's' if name else 'a retry is already queued'}")

    async def _recheck_gate(self, user_id: str) -> None:
        """Before a write batch (§7.1): an opt-out during the tick drops the in-flight result."""
        last = self._gate_checked.get(user_id)
        if last is not None and self._monotonic() - last < _GATE_RECHECK_S:
            return
        if not await self._enabled(user_id):
            raise _OptedOut()
        self._gate_checked[user_id] = self._monotonic()

    async def _enabled(self, user_id: str) -> bool:
        """The opt-in flag, read uncached (§7.1). The account id comes with it: model calls bill to it (§24.9)."""
        profile = await self._users.get_user(user_id)
        if profile is not None:
            self._account_ids[user_id] = str(getattr(profile, "account_id", "") or "")
        return bool(profile is not None and profile.config.drive_indexing_enabled)

    @staticmethod
    def _log(user_id: str, end: str, summary: Dict[str, int], budget: _Budget, models: _Models) -> None:
        elapsed = budget.monotonic() - budget.started
        cost = f" model_cost=${models.cost:.4f}" if models.calls or models.cost else ""
        logger.info(f"[DriveIndex] tick user={user_id[:8]} end={end} {summary}{cost} duration={elapsed:.1f}s")


def _fence() -> None:
    """No write once this tick's lease is about to end (§7.1): a tick that outlives it stops instead."""
    lease = _TICK_LEASE.get()
    if lease is not None:
        lease.check()


def _owner() -> Optional[str]:
    lease = _TICK_LEASE.get()
    return lease.owner if lease is not None else None


def _ever_read(recs: Sequence[DriveIndexRecord]) -> Set[str]:
    """Records whose deletion must take their chunks along: any that ever started an index run (§7.6)."""
    return {r.item_id for r in recs if r.index_generation > 0}


def _upgrade_waiting(state: DriveSyncState) -> bool:
    """A finished pass left stale records: no new pass before due work has run (or the target changes)."""
    return state.upgrade_cursor is None and state.upgrade_found_stale


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
    if plan.upsert is None:
        return plan
    return replace(plan, upsert=_maintenance(plan.upsert))


def _maintenance(rec: DriveIndexRecord) -> DriveIndexRecord:
    return rec if rec.due_at is None else replace(rec, priority=int(Priority.MAINTENANCE))


def _maintenance_kept(rec: DriveIndexRecord, cls: Optional[int]) -> DriveIndexRecord:
    return _maintenance(rec) if cls == int(Priority.MAINTENANCE) else rec


def _verdict(rec: DriveIndexRecord, result: TriageResult, now: datetime) -> DriveIndexRecord:
    """A stored verdict (§24.5); an upgrade's image keeps class 3 for what follows (§7.8)."""
    return _maintenance_kept(triaged(rec, result, now), rec.priority)


def _cleared(rec: DriveIndexRecord) -> DriveIndexRecord:
    """No run in progress: a cleared attempt is never counted as a crash (§8.2)."""
    return replace(rec, attempt_started_at=None)


def _backoff_until(models: _Models) -> datetime:
    until = models.state.model_backoff_until
    if until is None:
        raise RuntimeError("model work deferred without a model backoff")
    return until


def _outcome_key(before: DriveIndexRecord, final: DriveIndexRecord) -> str:
    """The tick summary's count for a run's outcome (§12.1, §24.9): a retry is the one pending outcome that counts
    an attempt."""
    if final.state is RecordState.INDEXED:
        return "indexed"
    if final.state is RecordState.FAILED:
        return "failed"
    if final.state is RecordState.METADATA_ONLY:
        return "demoted"
    return "retried" if final.attempts > before.attempts else "deferred"


def _run_line(user_id: str, key: str, final: DriveIndexRecord, outcome: Optional[ExtractionOutcome]) -> str:
    """One index run's outcome for on-call (final review F4-6): ids, state, reason, the deciding link and the pages
    — never document text or the file name."""
    reason = final.reason.value if final.reason is not None else "-"
    extractor = (outcome.extracted_by if outcome is not None and outcome.extracted_by else final.extracted_by) or "-"
    total = outcome.pages_total if outcome is not None else final.pages_total
    sent = outcome.pages_sent if outcome is not None else 0
    refused = outcome.pages_refused if outcome is not None else 0
    truncated = final.truncated or (outcome is not None and outcome.truncated)
    return (f"[DriveIndex] run user={user_id[:8]} item={final.item_id} outcome={key} state={final.state.value} "
            f"reason={reason} extractor={extractor} attempts={final.attempts} pages={sent}/{total} "
            f"refused={refused} truncated={truncated}")


def _terms_of(texts: Sequence[str]) -> List[Tuple[str, ...]]:
    return [content_terms(t) for t in texts]


def _write_file(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


def _read_head(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(SNIFF_HEAD_BYTES)
