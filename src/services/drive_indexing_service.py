"""
DriveIndexingService — the per-user `drive_sync` tick (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §7, §8).

Gate -> lease -> observe -> work off -> continue. Every decision is the planner's (domain); this class
fetches, calls it and writes the result. Runs only on the indexer service (D5).

Write-order rule: inside a step, a progress marker (a cursor, a cleared flag) is always the LAST write,
so a crash between writes repeats the step instead of skipping work.

Public interface:
    DriveIndexingService(drive, repo, embeddings, queue, users, place_of, embedding_space, *, extractors=None,
                         triage=None, profiler=None, passages=None, upgrade_pass=True, transcripts=None,
                         transcribe_enabled=False, transcribe_cap_usd=TRANSCRIBE_USD_PER_MONTH, clock, monotonic)
    await tick(user_id, nudge=None) -> TickResult

`extractors` maps `drive_extraction.PARSER` / `READER` to their ContentExtractorPort and `triage` is the
ImageTriagePort (§24.2). Both may be left out where nothing is admitted; a record whose content or triage is due
without them ends the tick as an internal error (loud, retried with backoff, never a demotion). `profiler` and
`passages` are Search v2's DocumentProfilerPort and PassageHintPort (§25.4.4): without both the enrichment phase does
not run (the jobs wait, planned as usual).

Search v2 (§25.4, P5):
- the migration gate (§25.8 as amended by D30): a sync state whose `applied_versions` lacks the `keys` key was built
  by v1 and is never upgraded: its tick does no work until the owner's from-scratch rebuild (a purge, then an
  onboarding that stamps every version). The fan-out raises one ops alert for it;
- every record write runs `drive_enrich_plan.planned` (`_commit`): the profile and passage jobs, the keys work,
  `about_keys` and the queue fields;
- the tick's order: content classes 0-2 within `1 - ENRICH_SHARE` of the start budget; enrichment (the queue, newest
  first, ENRICH_IN_FLIGHT files at once, model calls under MODEL_IN_FLIGHT, each file inside ENRICH_FILE_BUDGET_S);
  classes 3-4 (keys work included) within the maintenance share of what is left; then the upgrade pass;
- model dollars per class are written to the usage documents a purge keeps, the initial build first (§25.4.5); the
  reader's pages and triage's images are cost estimates against them, no longer monthly counters.

Transcripts of audio and video (§27.3, §27.10-§27.13; plan Task 14): `transcripts` is the DriveTranscriptStage (injected;
REQ-ARCH-22) and `transcribe_enabled` the kill switch. Only for a user with the switch AND `drive_transcribe_enabled` on
(read with the opt-in flag at the tick's gate, kept for the tick) does any transcript step run, any transcript query go
out, or a recording count as waiting; otherwise the tick is today's. Every record write runs `transcript_plan` (with it
off the job state is still planned, never acted on). The tick lends the stage a per-user `_TickHost`: its cheap steps
(poll, collect, finalize) run first in the work-off with no share, a finalize taking a file slot (G32); the extraction
step runs right after the due loop in its own start window (`_Budget.extraction_stop`), and while it has work the content
classes and enrichment leave it TRANSCRIBE_SHARE (G16). Ready extraction work continues the chain; a future wake-up or
open batches alone do not (the fan-out polls, Δ104).

Files the bot cannot open (§26.2, D33): existence is decided by delta alone. A 404 on an id-addressed call while working a
known item marks it inaccessible (`marked_inaccessible`), never deletes it: a download, a rendition or a thumbnail (after
one re-read by id), a folder's re-listing, README listing or density probe. Marking a file checks its parent with one
item call, and a 404 marks the parent (one level per marking, never the root, Δ50). A marked record is read again by id
in class 3 (`_recheck_step`): on an observation of it or of its parent folder at most once per INACCESSIBLE_RETRY_MIN,
otherwise every INACCESSIBLE_RETRY_SLOW (Δ51, OQ-C1); a readable answer clears the marking silently and the content path
resumes. A parent lookup's 404 still removes an orphan (RFC Δ29) and a nudge's 404 writes nothing. The tick counts the
marked files for the coverage line and the fan-out's notice (`_unread_counters`, Δ56); it never writes the fan-out's
`inaccessible_notified_since` (Δ54).
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from ..domain import drive_enrich_plan as enrich
from ..domain.drive_accuracy import accuracy_settled, scored
from ..domain import drive_index_policy as policy
from ..domain.drive_capture import CaptureSignals, CaptureSource, file_capture
from ..domain.drive_card import (
    card_eligible, card_hash, card_text, coarse_location, collapsed_folder, structurally_card_eligible,
)
from ..domain.drive_chunker import chunk_text
from ..domain.drive_enrich_plan import COVERAGE_COUNTERS, PASSAGES_AGENT, PROFILER_AGENT
from ..domain.drive_enrichment import (
    DriveHintDoc, EnrichContext, EnrichJob, HintItem, InitialBuild, JobState, ModelUsageMonth, ProfileSource,
    UsageClass,
)
from ..domain.drive_enrichment_calls import (
    HintReply, HintRequest, ModelReplyStatus, ProfileLabels, ProfileRequest, TriageResultV2,
)
from ..domain.drive_hints import chunk_text_hash, drop_if_far, ground, old_chunk, reuse_source
from ..domain.drive_extraction import (
    PARSER, READER, DriveModelUnavailable, ExtractionOutcome, ExtractionRequest, ExtractionStatus, TriageImage,
    TriageResult, chain_for, model_route,
)
from ..domain.drive_access import access_counts
from ..domain.drive_index import (
    ACCURACY_VERSION, CURRENT_VERSIONS, KEYS_VERSION, MIGRATION_GATE_KEY, PASSAGES_VERSION, DriveChange, DriveChanges, DriveChunk,
    DriveIndexRecord, DriveIndexStoreUnavailable, DriveSyncState, EpochPhase, ErrorCategory, FolderProbe, LeaseLost,
    Nudge, Priority, Reason, RecordState, Scope, TriageVerdict, inaccessible,
)
from ..domain.drive_index_params import (
    CASCADE_OPS_PER_TICK, EMBED_FILE_BUDGET_S, EMBED_IN_FLIGHT, ENRICH_FILE_BUDGET_S, ENRICH_IN_FLIGHT,
    ENRICH_QUEUE_PAGE, ENRICH_SHARE, ENRICH_WAKEUPS_PAGE, FILE_START_MARGIN_S, INACCESSIBLE_SCAN_MAX, SUBFOLDER_SCAN_MAX,
    ITEM_CALLS_PER_TICK, LEASE_MARGIN,
    MAINTENANCE_SHARE, MAX_ATTEMPTS, MAX_CARD_RECORDS_PER_USER, MAX_CHUNKS_PER_USER, MODEL_EXTRACT_PAGES_PER_MONTH,
    MODEL_IN_FLIGHT,
    NUDGE_RETRY_DELAY_S, PASSAGE_CHUNK_USD_ESTIMATE, PASSAGE_CHUNKS_PER_CALL, PASSAGE_IN_FLIGHT, PROBE_CHILDREN,
    README_CHILD_NAMES, README_SCAN_CHILDREN, READER_PAGE_USD_ESTIMATE, READER_RENDITION_SIZE, RECORD_WRITE_BATCH,
    RELIST_PAGE, RETRY_CAP, SYSTEMIC_BACKOFF_BASE_S, SYSTEMIC_BACKOFF_CAP_S, TICK_DEADLINE_S, TICK_FILE_BUDGET,
    TICK_START_BUDGET, TRANSCRIBE_EXTRACT_WALL_S, TRANSCRIBE_SHARE, TRANSCRIBE_UPLOAD_S, TRANSCRIBE_USD_PER_MONTH,
    TRIAGE_BATCH, TRIAGE_IMAGE_USD_ESTIMATE, TRIAGE_THUMBNAIL_SIZE,
)
from ..domain.drive_index_planner import (
    AccessAction, ObservationPlan, PlaceOf, PlanContext, access_action, access_observed, access_recheck_due,
    access_rechecked, access_regained, attempt_crashed, can_start_file, capped,
    card_refused, change_from_record, chunks_reembedded, chunks_refused, content_due, content_retry, deferred, demoted,
    drops_readable, extractor_stale, generation_started, indexed, marked_inaccessible, model_available, model_outage,
    model_recovered, month_key, month_parked, needs_probe, next_month_start, outage_charged, quiet_over,
    parent_scope, plan_observation, readme_read, reembed_in_space, reextraction_due, released, schedule,
    stale_versions, structure_stale, triage_due, triaged, with_card, with_retry, without_card,
)
from ..domain.drive_keys import word_form_keys, word_keys_capped
from ..domain.drive_profile import (
    PROFILE_POLICY, ProfileOutput, TemplateReason, input_fingerprint, profile_input, record_policy, stored_text,
    template_profile, grade, GradeCounts,
)
from ..domain.drive_index_policy import (
    Admission, admission, is_marker_child, kind_of, probe_from_children, size_cap,
)
from ..domain.drive_index_run import (
    READER_IMAGE_MAX_BYTES, README_MAX_BYTES, RunStep, camera_prior, chunk_cap_refuses, chunk_released, file_suffix,
    gate_exempt, image_mime, no_rendition, original_fallback, outcome_step, reader_pages, readme_choice, releasable,
    reread_cooldown_until, routed,
)
from ..domain.drive_search import (
    INACCESSIBLE_COUNTER, INACCESSIBLE_UNNOTIFIED_COUNTER, MAX_CHAIN_DEPTH, NOT_READ_YET_COUNTER, UNREAD_COUNTER,
)
from ..domain.drive_sniff import SNIFF_HEAD_BYTES, sniff_kind
from ..domain.drive_task_keys import DRIVE_SYNC_TASK, continuation_key, nudge_key, tick_payload
from ..domain.drive_text_gate import is_machine_text
from ..domain.drive_transcript_plan import (
    CURRENT as TRANSCRIPT_CURRENT, FAILED as TRANSCRIPT_FAILED, HELD as TRANSCRIPT_HELD, NONE as TRANSCRIPT_NONE,
    PARKED as TRANSCRIPT_PARKED, TRANSCRIBER_AGENT, WAITING as TRANSCRIPT_WAITING,
    WORKING as TRANSCRIPT_WORKING, TranscriptContext, available_usd, leaves_job, transcript_plan,
)
from ..domain.drive_transcript_job import SUBMITTED
from ..domain.drive_usage import added_usage
from ..domain.exceptions import LLMClientError, LLMError, LLMRateLimitError, LLMUnavailableError
from ..domain.user_drive import (
    DriveAccessDenied, DriveFileTooLarge, DriveItemNotFoundError, DriveNotConnectedError, DriveReauthRequired,
    DriveResyncRequired, DriveThrottled,
)
from ..ports.content_extractor_port import ContentExtractorPort
from ..ports.document_profiler_port import DocumentProfilerPort
from ..ports.drive_index_repository import DriveIndexRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.image_triage_port import ImageTriagePort
from ..ports.passage_hint_port import PassageHintPort
from ..ports.task_queue import TaskQueue
from ..ports.user_drive_port import UserDrivePort
from ..ports.user_repository import UserRepository
from ..utils.logger import logger

if TYPE_CHECKING:   # REQ-ARCH-22: a service never imports another at run time; the stage is injected
    from .drive_transcript_stage import DriveTranscriptStage

_DUE_PAGE = 100
def _last_months(now: datetime, count: int) -> List[str]:
    """`count` month keys, this month first, stepping through month starts (never `now - 31 days`, which skips a month)."""
    first = now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    out: List[str] = []
    for _ in range(count):
        out.append(month_key(first))
        first = (first - timedelta(days=1)).replace(day=1)
    return out


_PURGE_JOB_SCAN = 1000   # the purge's read of records and open batches that may carry a job (no cursor: a full page is logged)
# Errors about ONE item (a probe, a parent lookup, a re-listing page, a nudge read): defer that item, never
# the user (§8.2). ValueError is the adapter's 5xx; OSError covers connection resets.
_PER_ITEM_ERRORS = (DriveAccessDenied, DriveItemNotFoundError, asyncio.TimeoutError, ValueError, OSError)
_STEP_ASIDE = timedelta(seconds=1)   # a deleting folder waiting for its children comes back after them
_GATE_RECHECK_S = 5.0   # the opt-out flag is re-read before a write batch at most this often
# The kinds a profile or passage job can exist for (§25.3): the job counts read only these (`kind in` batches)
_ENRICH_KINDS = frozenset(PROFILE_POLICY) | {"image"}
_READY = JobState.READY.value


@dataclass
class TickResult:
    status: str                               # disabled | busy | purging | purged | access_lost | done | throttled |
                                              # error | lease_lost | awaiting_migration (D30)
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
    """A download or a rendition answered "not found": the item is re-read by id once, never read as "no content" or "no
    thumbnail". A 404 again means the bot cannot open it (§26.2): it is marked, never deleted."""


class _FolderClosed(Exception):
    """A density probe of a folder answered "not found" (§26.2): the folder is marked and classified without a probe."""


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
    enrich_s: float = 0.0          # time spent on enrichment this tick (§25.4.5), the inline first-seen profiles too
    enriched: Set[str] = field(default_factory=set)   # files the enrichment phase ran this tick: never twice
    profiled_inline: Set[str] = field(default_factory=set)   # first-seen files profiled inline: passages still due
    enrich_idle: Set[str] = field(default_factory=set)   # files whose run wrote nothing: not waited for this tick
    enrich_progressed: bool = False   # an enrichment run changed a record this tick (the stall alert)
    # G16: TRANSCRIBE_SHARE while the user has ready extraction work (decided once, before the due loop), else 0: the
    # content classes and enrichment then leave that share of the start budget to the extraction step
    transcribe_share: float = 0.0

    def elapsed(self) -> float:
        return self.monotonic() - self.started

    def time_left(self) -> bool:
        return self.elapsed() < TICK_START_BUDGET.total_seconds()

    def content_allowed(self) -> bool:
        """Content classes 0-2 use at most `1 - ENRICH_SHARE` of the start budget, enrichment time not counted
        (§25.4.5): enrichment keeps at least its share. While the user has ready extraction work they also leave
        `transcribe_share` (G16); without it the rule is unchanged."""
        elapsed = self.elapsed()
        total = TICK_START_BUDGET.total_seconds()
        return elapsed < total and elapsed - self.enrich_s < (1 - ENRICH_SHARE - self.transcribe_share) * total

    def enrichment_limit(self) -> float:
        """Where enrichment's start window ends at the latest (seconds into the tick): the start budget, less the
        extraction step's share while the user has ready extraction work (G16)."""
        total = TICK_START_BUDGET.total_seconds()
        return total - self.transcribe_share * total

    def extraction_stop(self) -> Optional[float]:
        """The extraction step's start window (§27.3, G16), as an absolute `monotonic()` value, or None when no step
        may start: TRANSCRIBE_SHARE of the start budget from where the step begins, whatever the due loop used, cut so a
        file step started inside it still ends before the tick deadline — its worst case (one extraction wall, an
        upload, the start margin) joins the §7.1 sum, as `enrichment_allowed`'s does."""
        at = self.elapsed()
        last_start = TICK_DEADLINE_S - (TRANSCRIBE_EXTRACT_WALL_S + TRANSCRIBE_UPLOAD_S + FILE_START_MARGIN_S)
        stop = min(at + TRANSCRIBE_SHARE * TICK_START_BUDGET.total_seconds(), last_start)
        return self.started + stop if stop > at else None

    def enrichment_allowed(self, stop_at: float) -> bool:
        """A file's enrichment starts before `stop_at` (the phase's share) and only if its whole ENRICH_FILE_BUDGET_S
        still ends before the tick deadline (§25.4.5: it joins the §7.1 deadline sum)."""
        elapsed = self.elapsed()
        return (elapsed < stop_at and self.time_left()
                and elapsed + ENRICH_FILE_BUDGET_S + FILE_START_MARGIN_S < TICK_DEADLINE_S)

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
class _CardSlots:
    """Card headroom during one pass of a due class (§5.4; R3 F1-2), decided before a record's content or triage
    work. `carded` is read once per pass; `admitted` counts the cardless records this pass let through, whose cards
    come at the end of the pass."""
    carded: Optional[int] = None
    admitted: int = 0


@dataclass
class _Models:
    """Model availability during one tick (§24.8, §25.4.6): the reader's and triage's shared backoff (the v1 fields)
    and Search v2's backoff per agent type. Every sync-state save of the tick, the error paths included, carries them.
    The dollars spent are the usage documents' (`_Spend`)."""
    state: DriveSyncState
    calls: int = 0
    cost: float = 0.0
    saved: Optional[_Saved] = None   # the tick's last written sync state, which a backoff write extends (F1-4)

    def available(self, now: datetime) -> bool:
        return model_available(self.state, now)

    def outage(self, now: datetime) -> None:
        self.state = model_outage(self.state, now)

    def used(self, now: datetime, *, calls: int, cost: float, recovered: bool = True) -> None:
        """Model work spent: count it as the runner reports it. `recovered`: it went through, so an outage is over;
        the part spent before an outage ends nothing, and the outage's backoff keeps doubling (§24.8)."""
        if recovered:
            self.state = model_recovered(self.state)
        self.calls += calls
        self.cost += cost

    def agent_available(self, agent: str, now: datetime) -> bool:
        return enrich.agent_available(self.state.model_backoff_by_agent, agent, now)

    def agent_outage(self, agent: str, now: datetime) -> None:
        self.state = replace(self.state, model_backoff_by_agent=enrich.agent_outage(
            self.state.model_backoff_by_agent, agent, now))

    def agent_recovered(self, agent: str) -> bool:
        """True when a backoff was lifted (the state needs a write)."""
        before = dict(self.state.model_backoff_by_agent)
        after = enrich.agent_recovered(before, agent)
        if after == before:
            return False
        self.state = replace(self.state, model_backoff_by_agent=after)
        return True

    def onto(self, state: DriveSyncState) -> DriveSyncState:
        m = self.state
        return replace(state, model_backoff_until=m.model_backoff_until, model_failures=m.model_failures,
                       model_backoff_by_agent=dict(m.model_backoff_by_agent))


@dataclass
class _Spend:
    """The user's model dollars during one tick (§25.4.5): the month's usage document and the initial build, read when
    the tick starts and kept current by every spend, so the plans and the budget checks see what was spent. `usd` is
    this tick's spending per class, plus "fallback" and "initial_build" (the tick line)."""
    now: datetime
    usage: ModelUsageMonth
    build: InitialBuild
    usd: Dict[str, float] = field(default_factory=dict)
    reserved_usd: float = 0.0   # this tick's net transcription reservations (reserved less released, any month)

    def ctx(self) -> EnrichContext:
        return enrich.enrich_context(self.now, self.usage, self.build)

    def available(self, cls: str) -> float:
        return enrich.available_usd(self.ctx(), cls)


# Per tick, like the lease: the spend every plan and budget check of the tick reads.
_TICK_SPEND: ContextVar[Optional[_Spend]] = ContextVar("drive_tick_spend", default=None)


@dataclass
class _Transcription:
    """The tick's transcript stage (§27.3): `on` is the kill switch AND the user's flag, read at the tick's gate and
    kept for the whole tick (a flag flipped mid-tick takes effect on the next one); `host` the side the tick lends the
    stage; whether the extraction step ran and moved anything (the continuation's loop guard, `_has_more`); `commits`
    the record writes the stage made through the host."""
    on: bool
    host: Optional["_TickHost"] = None
    extract_ran: bool = False
    extract_progressed: bool = False
    commits: int = 0


# Per tick: None outside a tick (the plan then runs with transcription off and no job is dropped).
_TICK_TRANSCRIPTION: ContextVar[Optional[_Transcription]] = ContextVar("drive_tick_transcription", default=None)


@dataclass
class _Marks:
    """The marked records (§26.2) as the tick read them at its start, lean (Δ51, Δ71): parent folder id -> item ids, so
    an observation of the folder makes them due for a re-read, and the same list for the counters at the end when the
    tick changed no marking."""
    records: List[DriveIndexRecord]
    by_parent: Dict[str, Tuple[str, ...]]


# Per tick: None when the last tick counted no marked file (nothing read at the start).
_TICK_CLOSED: ContextVar[Optional[_Marks]] = ContextVar("drive_tick_closed", default=None)
# What makes the tick read the marked records again for its counters: it changed a marking itself (review of S1)
_MARK_CHANGES = ("inaccessible", "inaccessible_folders", "inaccessible_rechecked", "access_regained")


def _count_reader(models: _Models, summary: Dict[str, int], now: datetime, *, calls: int, cost: float,
                  pages: int, outage: bool = False) -> None:
    """What the reader spent on one file — all of it, or the part before an `outage` (§24.9). The part before an
    outage never ends it: the doubling backoff the outage then extends stands. Its dollars go to `_spend_usd`."""
    models.used(now, calls=calls, cost=cost, recovered=not outage)
    summary["model_calls"] = summary.get("model_calls", 0) + calls
    summary["model_pages"] = summary.get("model_pages", 0) + pages


def _count_triage(models: _Models, summary: Dict[str, int], now: datetime, *, calls: int, cost: float,
                  images: int, outage: bool = False) -> None:
    """What triage spent on one batch — all of it, or the part before an `outage`, which never ends it (§24.8, §24.9)."""
    models.used(now, calls=calls, cost=cost, recovered=not outage)
    summary["triage_calls"] = summary.get("triage_calls", 0) + calls
    summary["triage_images"] = summary.get("triage_images", 0) + images


def _count_grades(summary: Dict[str, int], counts: GradeCounts) -> None:
    """The grader's tally onto the tick summary: values the text does not hold, per field (§26.4; was
    `verify_dropped_*`), and the enum labels it relabelled or dropped."""
    for gfield, missing in counts.not_in_text.items():
        _inc(summary, f"graded_not_in_text_{gfield}", missing)
    _inc(summary, "graded_labels_normalised", counts.labels)


def _inc(summary: Dict[str, int], key: str, by: int = 1) -> None:
    if by:
        summary[key] = summary.get(key, 0) + by


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
                 profiler: Optional[DocumentProfilerPort] = None,
                 passages: Optional[PassageHintPort] = None, upgrade_pass: bool = True,
                 transcripts: Optional["DriveTranscriptStage"] = None, transcribe_enabled: bool = False,
                 transcribe_cap_usd: float = TRANSCRIBE_USD_PER_MONTH,
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
        self._profiler = profiler
        self._passages = passages
        self._upgrade_pass = upgrade_pass   # False: the §25.8 emergency switch stops every upgrade pass
        self._transcripts = transcripts     # the transcript stage (§27.3); None: not built (the kill switch off)
        self._transcribe_enabled = transcribe_enabled   # the kill switch (DRIVE_TRANSCRIBE_ENABLED, §27.13)
        self._transcribe_flags: Dict[str, bool] = {}    # the per-user flag, read with the opt-in flag (Δ125)
        # the month's transcription cap: `effective_monthly_cap(DRIVE_TRANSCRIBE_USD_CAP)`, decided by the composition
        # (T-R3; REQ-ARCH-17: a service reads no environment), the same value the stage is built with
        self._transcribe_cap = transcribe_cap_usd
        self._clock = clock
        self._monotonic = monotonic
        self._gate_checked: Dict[str, float] = {}
        self._migration_warned: Set[str] = set()   # gated users warned about by this instance (D30)
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
            if MIGRATION_GATE_KEY not in state.applied_versions:
                # D30: built by v1, never upgraded. No tick work until the owner's rebuild purges it; the fan-out
                # alerts once. A purge (above) and a fresh onboarding (stamped just above) are never caught here.
                # WARNING until the fan-out's ops alert went out, once per instance; INFO on every later tick (a gated
                # user is ticked every fan-out until rebuilt; review rv_sv2_p5 MINOR 10)
                line = f"[DriveIndex] index of {user_id[:8]} was built under an older index version, awaiting its rebuild (D30); no tick work"
                if state.migration_alerted or user_id in self._migration_warned:
                    logger.info(line)
                else:
                    self._migration_warned.add(user_id)
                    logger.warning(line)
                return TickResult("awaiting_migration")
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
        spend = _Spend(now, await self._repo.get_model_usage(user_id, month_key(now)),
                       await self._repo.get_initial_build(user_id))
        spend_token = _TICK_SPEND.set(spend)
        closed_token = _TICK_CLOSED.set(None)   # read inside the tick's error handling (_run_spending)
        transcription = _Transcription(on=self._transcription_on(user_id))   # as the gate read it: kept for the tick
        if transcription.on:
            transcription.host = _TickHost(self, user_id, budget, models, summary, transcription)
        transcription_token = _TICK_TRANSCRIPTION.set(transcription)
        try:
            return await self._run_spending(user_id, state, nudge, now, enabled, budget, summary, cont, saved,
                                            models, spend)
        finally:
            _TICK_TRANSCRIPTION.reset(transcription_token)
            _TICK_CLOSED.reset(closed_token)
            _TICK_SPEND.reset(spend_token)

    async def _read_marks(self, user_id: str) -> List[DriveIndexRecord]:
        """The marked records, lean (the fields the counters and the parent map read), at most INACCESSIBLE_SCAN_MAX."""
        marked = await self._repo.inaccessible_records(user_id, INACCESSIBLE_SCAN_MAX, lean=True)
        if len(marked) >= INACCESSIBLE_SCAN_MAX:
            logger.warning(f"[DriveIndex] {user_id[:8]} has at least {INACCESSIBLE_SCAN_MAX} records the bot cannot "
                           f"open; counted up to that")
        return marked

    async def _start_marks(self, user_id: str, state: DriveSyncState) -> Optional[_Marks]:
        """Read at the tick's start only while the last tick counted a marked file (§26.2, Δ51)."""
        if not state.counters.get(INACCESSIBLE_COUNTER):
            return None
        records = await self._read_marks(user_id)
        by_parent: Dict[str, List[str]] = {}
        for rec in records:
            if rec.parent_id:
                by_parent.setdefault(rec.parent_id, []).append(rec.item_id)
        return _Marks(records, {parent: tuple(kids) for parent, kids in by_parent.items()})

    async def _end_marks(self, user_id: str, state: DriveSyncState, summary: Mapping[str, int], *,
                         settled: bool) -> Optional[List[DriveIndexRecord]]:
        """The marked records the counters are made from (Δ56, Δ71), or None to leave them uncounted. A settled tick
        always counts them, so a tick interrupted after a marking (its counters unsaved) cannot hide the marking from
        the notice for good; otherwise only while the last tick counted some or this one changed a marking. The start's
        read is reused when this tick changed none."""
        changed = any(summary.get(key) for key in _MARK_CHANGES)
        if not (settled or changed or state.counters.get(INACCESSIBLE_COUNTER)
                or state.counters.get(INACCESSIBLE_UNNOTIFIED_COUNTER)):
            return None
        marks = _TICK_CLOSED.get()
        if marks is not None and not changed:
            return marks.records
        return await self._read_marks(user_id)

    async def _run_spending(self, user_id: str, state: DriveSyncState, nudge: Optional[Nudge], now: datetime,
                            enabled: bool, budget: _Budget, summary: Dict[str, int], cont: _Continuation,
                            saved: _Saved, models: _Models, spend: _Spend) -> TickResult:
        try:
            _TICK_CLOSED.set(await self._start_marks(user_id, state))
            if nudge is not None:
                await self._observe_nudge(user_id, state, nudge, now, budget, summary)
            state = await self._observe(user_id, state, now, budget, summary, saved)
            if state.round_complete:
                state = await self._work_off(user_id, state, now, budget, summary, models)
            more = await self._has_more(user_id, state, now, upgrade_wrote=summary.get("upgraded", 0) > 0,
                                        models=models, idle=budget.enrich_idle)
            job_counts: Dict[str, int] = {}
            if state.round_complete and self._enrich_wired():
                state, job_counts = await self._rebuild_progress(user_id, state, now, spend, settled=not more,
                                                                 progressed=budget.enrich_progressed)
            # the files the bot cannot open (§26.2, Δ56): always on a settled tick, else while any may exist
            marked = await self._end_marks(user_id, state, summary, settled=not more)
            transcribing = _transcribing()   # recordings count as waiting only for a user whose stage runs (G3)
            if more:
                cont.request()
                # unread is what search tells Smart: refreshed every tick, not only once the index settles (F4-1)
                state = replace(state, counters=await self._unread_counters(user_id, state.counters, marked=marked,
                                                                            transcription_on=transcribing))
            else:   # settled: refresh
                state = replace(state, counters=await self._counters(user_id, marked=marked,
                                                                     transcription_on=transcribing))
            if job_counts:
                state = replace(state, counters={**{k: v for k, v in state.counters.items()
                                                    if not k.startswith(("profile_", "passages_", "coverage_"))},
                                                 **job_counts})
            progressed = any(v for k, v in summary.items() if k != "pages")
            state = replace(models.onto(state), continuation_enqueued_at=now if more else None, last_error=None,
                            systemic_failures=0,
                            last_success_at=now if (progressed or not more) else state.last_success_at)
            await self._save_state(state)
            self._log(user_id, "continued" if more else "done", summary, budget, models, spend)
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

    async def _counters(self, user_id: str, *, marked: Optional[Sequence[DriveIndexRecord]] = None,
                        transcription_on: bool = False) -> Dict[str, int]:
        """Records per state and carded (§3.5), readable chunks and `unread` (§10.2, §12.1). A zero content counter
        is left out: the Cabinet lists only non-zero counters and every reader defaults to 0. While transcription is
        off for the user, his recordings (admitted `pending` by the global policy, G3) are taken out of `pending`:
        no stage will run for them, so they would read "waiting" for good. Counted with the not-read-yet class
        (same filters as `_unread_counters`, no new index)."""
        counters = dict(await self._repo.count_by_state(user_id))
        if not transcription_on and counters.get(RecordState.PENDING.value):
            idle_media = await self._repo.count_unread(user_id, frozenset(), frozenset(), pending=True,
                                                       transcribed=policy.TRANSCRIBED_KINDS)
            left = max(0, counters[RecordState.PENDING.value] - idle_media)
            if left:
                counters[RecordState.PENDING.value] = left
            else:
                del counters[RecordState.PENDING.value]
        content = {"chunks": await self._repo.readable_chunk_total(user_id)}
        counters.update({k: v for k, v in content.items() if v})
        return await self._unread_counters(user_id, counters, marked=marked, transcription_on=transcription_on)

    async def _unread_counters(self, user_id: str, counters: Mapping[str, int], *,
                               marked: Optional[Sequence[DriveIndexRecord]] = None,
                               transcription_on: bool = False) -> Dict[str, int]:
        """`counters` with `unread` and `not_read_yet` counted now (§10.2, final review F5-2): the documents still
        waiting and those that could not be read, two count passes; a zero is left out. With `marked` (the marked
        records, `_end_marks`), the files the bot cannot open are counted on their own (`inaccessible`, and
        `inaccessible_unnotified` for the fan-out's notice); they are taken out of `not_read_yet`, which they would
        otherwise stay in for good (§26.2, Δ56). The count queries are unchanged: no new composite index.
        `transcription_on` (§27.11 "Coverage", G2, G3): only for a user whose transcription is on do the recordings
        count too (a third kind set, `transcribed`); otherwise the calls are exactly the ones without it, so a user
        who never transcribes is not told his recordings are waiting for a stage that will not run."""
        media = {"transcribed": policy.TRANSCRIBED_KINDS} if transcription_on else {}
        waiting, unreadable = await asyncio.gather(
            self._repo.count_unread(user_id, policy.ADMITTED_KINDS, policy.TRIAGED_KINDS, pending=True, **media),
            self._repo.count_unread(user_id, policy.ADMITTED_KINDS, policy.TRIAGED_KINDS, pending=False, **media))
        closed = access_counts(())
        if marked is not None:
            closed = access_counts(marked, frozenset(policy.ADMITTED_KINDS), frozenset(policy.TRIAGED_KINDS),
                                   frozenset(policy.TRANSCRIBED_KINDS) if transcription_on else None)
        waiting = max(0, waiting - closed.waiting)
        out = {k: v for k, v in counters.items()
               if k not in (UNREAD_COUNTER, NOT_READ_YET_COUNTER, INACCESSIBLE_COUNTER, INACCESSIBLE_UNNOTIFIED_COUNTER)}
        out.update({k: v for k, v in ((UNREAD_COUNTER, waiting + unreadable), (NOT_READ_YET_COUNTER, waiting),
                                      (INACCESSIBLE_COUNTER, closed.files),
                                      (INACCESSIBLE_UNNOTIFIED_COUNTER, closed.unnotified)) if v})
        return out

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
        await self._observe_closed_children(user_id, ctx, changes, cache, writes, deletes)
        await self._commit(user_id, list(writes.values()), deletes, collects=collects)
        summary["observed"] = summary.get("observed", 0) + len(changes)

    async def _observe_closed_children(self, user_id: str, ctx: PlanContext, changes: Sequence[DriveChange],
                                       cache: Dict[str, Optional[DriveIndexRecord]],
                                       writes: Dict[str, DriveIndexRecord], deletes: Set[str]) -> None:
        """An observed folder makes its marked children due for a re-read (§26.2, Δ51): a permission change on a folder
        may re-emit the folder alone. The children come from the tick's map (`_TICK_CLOSED`)."""
        marks = _TICK_CLOSED.get()
        if marks is None or not marks.by_parent:
            return
        kids = [k for c in changes if c.is_folder and not c.deleted for k in marks.by_parent.get(c.item_id, ())
                if k not in deletes]
        if not kids:
            return
        missing = [k for k in kids if k not in cache and k not in writes]
        cache.update(await self._repo.get_records(user_id, missing))
        for kid in kids:
            rec = writes.get(kid) or cache.get(kid)
            if rec is not None and inaccessible(rec) and not rec.inaccessible_reobserved:
                writes[kid] = access_observed(rec, ctx.now)
                cache[kid] = writes[kid]

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

    async def _probe(self, user_id: str, folder_id: str, budget: _Budget, *,
                     report_closed: bool = False) -> Optional[FolderProbe]:
        """`report_closed`: a "not found" raises _FolderClosed (the probe step marks the folder, §26.2); otherwise it is
        a failed probe, as on observation, where the folder waits as probe_pending for its probe step."""
        budget.item_calls -= 1
        try:
            page = await self._drive.list_children_changes(user_id, folder_id, limit=PROBE_CHILDREN)
        except DriveItemNotFoundError as exc:
            logger.info(f"[DriveIndex] density probe of folder {folder_id} for {user_id[:8]}: not found")
            if report_closed:
                raise _FolderClosed() from exc
            return None
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
                      collects: Optional[Set[str]] = None,
                      before: Optional[Mapping[str, DriveIndexRecord]] = None) -> None:
        """`collects`: items whose chunks (every generation) go before the records are written (§3.4, §7.6).
        `before`: the records the writes were derived from; a write that drops its readable generation collects it
        (drops_readable, R3 F2-2), so a caller holding the record never has to remember to.

        Every write runs the transcript plan (`transcript_plan`, §27.3; with transcription off it still plans the
        state, it only never stubs) and then the enrichment plan. While transcription is on for the user, the job of a
        record the write takes off it (`leaves_job`) or deletes is dropped first (`drop_jobs`: its reservation released
        once, T-R20: a job with a window in a batch is left to collection), so a crash between the two writes leaves a
        record without a job (restarted by the stage), never a job holding money for a record that left."""
        if before:
            dropped = {w.item_id for w in writes if drops_readable(before.get(w.item_id), w)}
            collects = (collects or set()) | dropped if dropped else collects
        if not writes and not deletes and not collects:
            return
        _fence()
        await self._recheck_gate(user_id)
        for item_id in sorted(collects or ()):
            await self._collect(user_id, item_id, None)
        ctx = self._enrich_ctx()
        tctx = self._transcript_ctx()
        # every write's plans: the transcript job's (§27.3), then the keys, enrichment jobs and queue fields (§25.4.9)
        planned = [enrich.planned(transcript_plan(w, tctx), ctx) for w in writes]
        await self._drop_transcript_jobs(user_id, writes, planned, deletes)
        writes = planned
        for start in range(0, len(writes), RECORD_WRITE_BATCH):
            await self._repo.upsert_records(writes[start:start + RECORD_WRITE_BATCH], card_vectors)
        if deletes:
            await self._repo.delete_records(user_id, sorted(deletes))

    # ------------------------------------------------------------------------------------------
    # Work off (only after a complete round, RFC Δ15)
    # ------------------------------------------------------------------------------------------

    async def _work_off(self, user_id: str, state: DriveSyncState, now: datetime, budget: _Budget,
                        summary: Dict[str, int], models: _Models) -> DriveSyncState:
        """§7.1 step 2 as Search v2 orders it (§25.4.5): content classes 0-2 within their share, then enrichment with at
        least ENRICH_SHARE, then classes 3-4 (keys work included) within the maintenance share of what is left, then
        the upgrade pass and the cap releases. With transcription on for the user (§27.3): the transcript stage's
        cheap steps right after the sweep, and its extraction step right after the due loop."""
        if state.epoch_phase is EpochPhase.SWEEPING:
            state = await self._sweep(user_id, state, now, budget, summary)
        if _transcribing():
            # §27.3: poll, collect and finalize first, with no share (a finalize takes a file slot, G32); then the
            # shares the content classes and enrichment leave to extraction while it has work (G16)
            await self._transcript_cheap(user_id, budget, summary, models)
            if await self._extraction_ready(user_id, models):
                budget.transcribe_share = TRANSCRIBE_SHARE
        stopped_class, worked = await self._due_loop(user_id, state, now, budget, summary, models)
        if _transcribing():
            # after the enrichment phase (inside the due loop) whatever class it stopped at, before the upgrade pass
            await self._transcript_extract(user_id, budget, summary, models)
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
                if not (await self._sweep_hints(user_id, budget, summary)
                        and await self._sweep_chunks(user_id, budget, summary)):
                    return state   # the hint and chunk backstops start again next tick
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

    async def _sweep_hints(self, user_id: str, budget: _Budget, summary: Dict[str, int]) -> bool:
        """The hint documents' backstop (§25.2.2), run before the chunks': hint documents whose record is missing or
        whose generation is not the record's `indexed_generation` — found here even when their chunks are already
        gone. Same pass rules as `_sweep_chunks`."""
        after: Optional[Tuple[str, int, int]] = None
        while budget.time_left():
            page = await self._repo.hints_page(user_id, after, RECORD_WRITE_BATCH)
            if not page:
                return True
            recs = await self._repo.get_records(user_id, sorted({h.item_id for h in page}))
            debris = sorted({h.item_id for h in page
                             if h.item_id not in recs or h.generation != recs[h.item_id].indexed_generation})
            for item_id in debris:
                _fence()
                keep = recs[item_id].indexed_generation if item_id in recs else None
                deleted = await self._repo.delete_hints_except(user_id, item_id, keep)
                summary["swept_hints"] = summary.get("swept_hints", 0) + deleted
            if len(page) < RECORD_WRITE_BATCH:
                return True
            after = (page[-1].item_id, page[-1].generation, page[-1].index)
        return False

    async def _collect(self, user_id: str, item_id: str, keep_generation: Optional[int]) -> None:
        """Every generation of the item but `keep_generation` (None: all) goes: its hint documents first, then its
        chunks (§7.4, §25.2.2), so no hint document outlives the chunks it was written for."""
        await self._repo.delete_hints_except(user_id, item_id, keep_generation)
        await self._repo.delete_chunks_except(user_id, item_id, keep_generation)

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
                hints = await self._repo.delete_hints_except(user_id, item_id, keep)   # hints first (§25.2.2)
                if hints:
                    summary["swept_hints"] = summary.get("swept_hints", 0) + hints
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
        what keeps a repository's files from being carded while its rescope is unfinished (RFC Δ15). Search v2's
        order (§25.4.5): content classes 0-2 stop at their share of the start budget (checked once work was done, so
        every tick progresses); the enrichment phase runs once, after them — before classes 3-4, or when the content
        share or the due work ends; classes 3-4 then have the maintenance share."""
        worked = False
        enriched = False

        async def enrich_once() -> None:
            nonlocal worked, enriched
            if not enriched:
                enriched = True
                worked |= await self._enrich_phase(user_id, now, budget, summary, models)

        while budget.time_left():
            batch = await self._repo.due_records(user_id, now, _DUE_PAGE)
            if not batch:
                await enrich_once()
                return None, worked
            cls = batch[0].priority if batch[0].priority is not None else int(Priority.RETRY)
            same = [r for r in batch if r.priority == cls]
            if cls <= Priority.FIRST_SEEN and worked and not budget.content_allowed():
                await enrich_once()
                return cls, worked   # the content share is spent: the rest waits for the next tick
            if cls >= Priority.MAINTENANCE:
                await enrich_once()
            if cls >= Priority.MAINTENANCE and not budget.maintenance_allowed():
                return cls, worked
            started = self._monotonic()
            more_beyond = len(same) == _DUE_PAGE   # the page holds only this class: more of it may follow
            progressed = await self._work_class(user_id, state, same, now, budget, summary, models, more_beyond)
            worked |= progressed
            if cls >= Priority.MAINTENANCE:
                budget.maintenance_s += self._monotonic() - started
            if not progressed:
                if cls <= Priority.FIRST_SEEN:
                    await enrich_once()
                return cls, worked
        await enrich_once()
        return int(Priority.RETRY), worked

    async def _work_class(self, user_id: str, state: DriveSyncState, recs: List[DriveIndexRecord], now: datetime,
                          budget: _Budget, summary: Dict[str, int], models: _Models, more_beyond: bool) -> bool:
        """Content and triage come before the card of the same record, so a read file is carded once, with its
        lead text, brief or caption (§8.5); a card whose content cannot start now is embedded at once. Card headroom
        is decided before both: a record the cap would refuse a card is capped instead of read (§5.4; R3 F1-2)."""
        ctx = self._ctx(user_id, state, now)
        cards: List[DriveIndexRecord] = []
        images: List[DriveIndexRecord] = []
        progressed = False
        page_ids = {r.item_id for r in recs}
        slots = _CardSlots()
        content_class = (recs[0].priority if recs and recs[0].priority is not None else 0) <= Priority.FIRST_SEEN
        runs = 0
        for stale in recs:
            # An earlier step of this batch may have changed or removed it: always work the stored version.
            rec_or_none = (await self._repo.get_records(user_id, [stale.item_id])).get(stale.item_id)
            if rec_or_none is None or rec_or_none.due_at is None or rec_or_none.due_at > now:
                progressed = True
                continue
            rec = rec_or_none
            # its content may start now: due for other work (a card, a re-embed), it still keeps its quiet period
            content_now = content_due(rec, now) and quiet_over(rec, now)
            if content_now and content_class and runs and not budget.content_allowed():
                break   # the content classes' share is spent (§25.4.5): the rest waits for the next tick
            if rec.state is RecordState.DELETING:
                progressed |= await self._cascade_step(user_id, rec, now, budget, summary, page_ids)
            elif rec.probe_pending:
                progressed |= await self._probe_step(user_id, rec, ctx, budget)
            elif rec.state is RecordState.UNRESOLVED:
                progressed |= await self._resolve_step(user_id, rec, state, ctx, budget)
            elif rec.rescope_pending:
                progressed |= await self._rescope_step(user_id, rec, ctx, budget, summary)
            elif access_recheck_due(rec, now):
                progressed |= await self._recheck_step(user_id, rec, ctx, budget, summary)
            elif rec.reobserve_pending and not inaccessible(rec):
                progressed |= await self._reobserve_step(user_id, rec, ctx, budget, summary)
            elif rec.readme_pending and not inaccessible(rec):
                progressed |= await self._readme_step(user_id, rec, ctx, budget, summary)
            elif rec.has_card and not card_eligible(rec):
                await self._commit(user_id, [without_card(rec, now)], set())
                progressed = True
            elif ((content_now or (triage_due(rec, now) and quiet_over(rec, now)))
                  and not await self._card_slot(user_id, rec, slots)):
                # no card headroom for it: its reading would never be found, so it waits for a release (R3 F1-2)
                await self._commit(user_id, [capped(rec, now)], set(), before={rec.item_id: rec})
                summary["capped"] = summary.get("capped", 0) + 1
                progressed = True
            elif content_now and await self._index_step(user_id, rec, ctx, budget, summary, models):
                progressed = True
                runs += 1
                if rec.priority == int(Priority.FIRST_SEEN):
                    await self._enrich_inline(user_id, rec.item_id, now, budget, summary, models)
            elif rec.chunk_reembed_pending and rec.indexed_generation is not None and not content_now:
                # any content state: a failed or a waiting record serves its readable generation too (§3.4, §7.4)
                progressed |= await self._reembed_step(user_id, rec, ctx, budget, summary)
            elif rec.keys_pending and rec.indexed_generation is not None and not content_now:
                progressed |= await self._keys_step(user_id, rec, ctx, budget, summary)
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
        try:
            probe = await self._probe(user_id, rec.item_id, budget, report_closed=True)
        except _FolderClosed:
            # the bot cannot list it (§26.2): marked, and classified without a probe, as after MAX_ATTEMPTS (§8.2)
            logger.info(f"[DriveIndex] folder {rec.item_id} of {user_id[:8]} cannot be opened; marked")
            rec, probe = marked_inaccessible(rec, ctx.now), FolderProbe(marker_found=False, class_counts={}, sampled=0)
            await self._mark_subfolders(user_id, rec, ctx.now)
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
            # RFC Δ29 stands (§26.2): delta lists the items the bot cannot open, so a parent a complete round never
            # listed is not in the App Folder; the orphan goes
            parent_change = DriveChange(item_id=rec.parent_id, deleted=True)
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
            # §26.2: delta decides existence; the bot cannot list it. The re-listing waits for access (not due while
            # marked) and restarts from its first page
            logger.info(f"[DriveIndex] folder {folder.item_id} of {user_id[:8]} cannot be listed; marked")
            await self._commit(user_id, [marked_inaccessible(folder, ctx.now)], set())
            _inc(summary, "inaccessible_folders")
            await self._mark_subfolders(user_id, folder, ctx.now)
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
                    # the evicted record's chunks go with its card (drops_readable, R3 F1-2)
                    await self._commit(user_id, [capped(victim, now)], set(), before={victim.item_id: victim})
                    evicted += 1
                else:
                    await self._commit(user_id, [capped(rec, now)], set(), before={rec.item_id: rec})
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

    async def _card_slot(self, user_id: str, rec: DriveIndexRecord, slots: _CardSlots) -> bool:
        """Whether `rec` will hold a card once its content or triage is done (§5.4): it has one, there is headroom
        (counting the cardless records this pass already let through), or it outranks the lowest-ranked carded record,
        which _embed_cards then evicts. Decided before any model or embedding work (R3 F1-2)."""
        if rec.has_card or not structurally_card_eligible(rec):
            return True
        if slots.carded is None:
            slots.carded = await self._repo.count_carded(user_id)
        if slots.carded + slots.admitted < MAX_CARD_RECORDS_PER_USER:
            slots.admitted += 1
            return True
        victim = await self._repo.lowest_ranked_carded(user_id)
        return victim is not None and victim.card_rank > rec.card_rank

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

    # -- a collapsed folder's README (§5.2, §25.3) -----------------------------------------------

    async def _readme_step(self, user_id: str, rec: DriveIndexRecord, ctx: PlanContext, budget: _Budget,
                           summary: Dict[str, int]) -> bool:
        """Every collapsed folder (any opaque_root reason) finds its README by listing its children page by page
        (RELIST_PAGE per item call) until `readme_choice` finds one or README_SCAN_CHILDREN were listed; the parser
        reads it (never the reader); its head and tail and the first README_CHILD_NAMES child names become the
        folder's `readme_excerpt`, the profile's input. A transient failure backs off; after MAX_ATTEMPTS the folder
        keeps what it had. The scan starts only when the tick has the item calls it needs (at most
        ITEM_CALLS_PER_TICK) and is not resumed across ticks: one the budget still cuts decides on what it listed (a
        README further on is found when the child count next moves)."""
        now = ctx.now
        if not collapsed_folder(rec):
            await self._commit(user_id, [readme_read(rec, None, "", "", now)], set())
            return True
        pages = max(1, -(-min(max(rec.child_count, 1), README_SCAN_CHILDREN) // RELIST_PAGE))
        if budget.item_calls < min(pages, ITEM_CALLS_PER_TICK) or not budget.item_call_allowed():
            return False
        names: List[str] = []
        seen: List[DriveChange] = []
        cursor: Optional[str] = None
        readme: Optional[DriveChange] = None
        while True:
            if not budget.item_call_allowed():
                break   # cut: decided on what was listed
            budget.item_calls -= 1
            try:
                page = await self._drive.list_children_changes(user_id, rec.item_id, limit=RELIST_PAGE, cursor=cursor)
            except DriveItemNotFoundError:
                # §26.2: the bot cannot list it; the README waits for access (not due while marked)
                logger.info(f"[DriveIndex] folder {rec.item_id} of {user_id[:8]} cannot be listed; marked")
                await self._commit(user_id, [marked_inaccessible(rec, now)], set())
                _inc(summary, "inaccessible_folders")
                await self._mark_subfolders(user_id, rec, now)
                return True
            except (DriveNotConnectedError, DriveThrottled) as exc:
                logger.warning(f"[DriveIndex] README listing hit a systemic error for {user_id[:8]}: "
                               f"{type(exc).__name__}")
                raise
            except _PER_ITEM_ERRORS as exc:
                logger.warning(f"[DriveIndex] README listing failed for {user_id[:8]}: {exc}")
                await self._commit(user_id, [self._readme_retry(rec, now)], set())
                return True
            seen.extend(page.changes)
            names.extend(c.name for c in page.changes if not c.deleted and c.name)
            readme = readme_choice(seen)
            cursor = page.next_cursor
            if readme is not None or cursor is None or len(seen) >= README_SCAN_CHILDREN:
                break
        child_names = tuple(names[:README_CHILD_NAMES])
        if readme is None:
            await self._commit(user_id, [readme_read(rec, None, "", "", now)], set())
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
        await self._commit(user_id, [readme_read(rec, readme.item_id, readme.content_version, text, now,
                                                 child_names=child_names)], set())
        summary["readmes"] = summary.get("readmes", 0) + 1
        return True

    @staticmethod
    def _readme_retry(rec: DriveIndexRecord, now: datetime) -> DriveIndexRecord:
        if rec.attempts + 1 >= MAX_ATTEMPTS:
            return readme_read(rec, rec.readme_item_id, rec.readme_version, None, now)   # keeps what it had
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
        if final is not None and outcome is not None and outcome.model_calls > 0 \
                and final.state is not RecordState.PENDING:
            final = replace(final, model_read_at=now)   # the reader settled it: its cooldown starts (F1-3)
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
        signals: Optional[CaptureSignals] = None   # parser@3's, kept past the reader's outcome (§26.3)
        for position, extractor_id in enumerate(chain_for(kind)):
            if position > 0:
                why = model_route(outcome, kind) if outcome is not None else None
                if why is None:
                    break
                logger.info(f"[DriveIndex] item {run.item_id} of {user_id[:8]} goes to the {extractor_id}: {why}")
            max_pages = 0
            if extractor_id == READER:
                pages = reader_pages(kind, outcome if position > 0 else None)
                held = self._model_hold(_with_detected_capture(run, kind, signals), now, models, cls=cls, pages=pages)
                if held is not None:
                    return None, held
                # The store before the model (final review F4-2): a store that cannot count the user's chunks ends
                # the tick before anything is billed, and a user with no chunk headroom costs no reader call. The
                # real count is checked again on the text (_read_text); one chunk is the least a read can add.
                if chunk_cap_refuses(await self._repo.readable_chunk_total(user_id), run, 1):
                    logger.info(f"[DriveIndex] item {run.item_id} of {user_id[:8]} refused by the chunk cap before "
                                f"the reader")
                    return None, demoted(run, Reason.USER_CHUNK_CAP, now, chunk_count=1)
                max_pages = self._affordable(UsageClass.READER.value, READER_PAGE_USD_ESTIMATE)
                if pages >= MODEL_EXTRACT_PAGES_PER_MONTH:
                    # cut to what a whole month's reader cap pays (reader_pages): asked for exactly that (MINOR 4)
                    max_pages = min(max_pages, pages)
            extractor = self._extractor(extractor_id)
            request = ExtractionRequest(path=path, name=run.name, kind=kind, mime_type=run.mime_type,
                                        user_id=user_id, account_id=self._account_ids.get(user_id, ""),
                                        max_model_pages=max_pages, item_id=run.item_id)
            try:
                outcome = await extractor.extract(request)
                if not outcome.extracted_by:   # the link that decided, whatever the outcome (§9 upgrade compares it)
                    outcome = replace(outcome, extracted_by=f"{extractor_id}@{extractor.version}")
                if position == 0:
                    signals = outcome.capture_signals
            except DriveModelUnavailable as exc:
                logger.warning(f"[DriveIndex] reader unavailable for {user_id[:8]}: {exc}; model work deferred")
                _count_reader(models, summary, now, calls=exc.model_calls, cost=exc.model_cost, pages=exc.pages_sent,
                              outage=True)
                await self._spend_usd(user_id, UsageClass.READER.value, exc.model_cost, exc.model_fallback_cost,
                                      exc.model_calls)   # the fallback's share reaches the fallback alert (XC-5)
                models.outage(now)   # after the usage, which leaves the backoff to this outage (C-M1, T-M2)
                await self._persist_usage(models)
                if exc.model_calls > 0 and exc.charges_file:
                    # the reader called the model for this file and it failed in a way that may be the file's (a 5xx,
                    # a timeout, an unexpected error; never a rate limit or an account error): charged to the file,
                    # which waits behind other due work and fails at MAX_ATTEMPTS (F1-1)
                    charged = outage_charged(run, _backoff_until(models), now, f"{extractor_id}@{extractor.version}")
                    logger.warning(f"[DriveIndex] outage charged to item {run.item_id} of {user_id[:8]} "
                                   f"({charged.model_outages or MAX_ATTEMPTS}/{MAX_ATTEMPTS})")
                    return None, charged
                return None, deferred(_cleared(run), _backoff_until(models), now, priority=cls)
            except Exception as exc:   # the port reports per-file failures as outcomes; anything else is this file's
                logger.error(f"[DriveIndex] {extractor_id} failed on item {run.item_id} of {user_id[:8]}: {exc}",
                             exc_info=True)
                return None, content_retry(run, Reason.EXTRACT_ERROR, now)
            if extractor_id == READER:
                _count_reader(models, summary, now, calls=outcome.model_calls, cost=outcome.model_cost,
                              pages=outcome.pages_sent)
                await self._spend_usd(user_id, UsageClass.READER.value, outcome.model_cost,
                                      outcome.model_fallback_cost, outcome.model_calls)   # XC-5
                await self._persist_usage(models)
        if outcome is None:   # an empty chain: no kind is admitted without an extractor (§5.1)
            raise _NotWired(f"no extractor chain for kind {kind}")
        # §26.3: a deterministic answer (the parser's signals, the camera facet) wins over the reader's model
        triaged_here = (run.triage_verdict is TriageVerdict.TEXT and run.triaged_content_version == run.content_version)
        capture, source = "", ""
        if outcome.status in (ExtractionStatus.TEXT, ExtractionStatus.NO_TEXT):   # only a file that was read (Δ79)
            capture, source = file_capture(kind, signals=signals, facets=run.facets, model=outcome.capture,
                                           triaged=run.capture if triaged_here else "")
        outcome = replace(outcome, capture_signals=signals, capture=capture, capture_source=source)
        step, reason = outcome_step(outcome.status)
        if step is RunStep.WAIT_MONTH:
            return outcome, deferred(_cleared(_with_detected_capture(run, kind, signals)), next_month_start(now), now,
                                     maintenance=True)
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
        keys = [_text_key(c.text) for c in chunked.chunks]
        reused = await self._reusable_vectors(user_id, run)
        missing = {k: c.text for k, c in zip(keys, chunked.chunks) if k not in reused}   # one call per distinct text
        started = self._monotonic()
        try:
            embedded = await asyncio.wait_for(self._embed_texts(list(missing.values())), timeout=EMBED_FILE_BUDGET_S)
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
        fresh_vectors = dict(zip(missing, embedded))
        vectors = [reused[k] if k in reused else fresh_vectors[k] for k in keys]
        if len(keys) > len(missing):
            summary["vectors_reused"] = summary.get("vectors_reused", 0) + sum(1 for k in keys if k in reused)
        generation = run.index_generation
        keyed = await asyncio.to_thread(_keys_of, [c.text for c in chunked.chunks])
        chunks = [DriveChunk(user_id=user_id, item_id=run.item_id, generation=generation, index=i, text=c.text,
                             char_start=c.char_start, char_end=c.char_end, kind=run.kind, word_keys=k,
                             keys_version=KEYS_VERSION)
                  for i, (c, (k, _)) in enumerate(zip(chunked.chunks, keyed))]
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
            await self._collect(user_id, run.item_id, fresh.indexed_generation if fresh is not None else None)
            summary["dropped"] = summary.get("dropped", 0) + 1
            if fresh is not None and fresh.attempt_started_at == run.attempt_started_at:
                await self._commit(user_id, [replace(fresh, attempt_started_at=None)], set())
            return None
        summary["chunks"] = summary.get("chunks", 0) + len(chunks)
        keys_cut = any(cut for _, cut in keyed)
        _inc(summary, "keys_capped", int(keys_cut))
        done = indexed(fresh, outcome=replace(outcome, truncated=outcome.truncated or chunked.truncated),
                       generation=generation, chunk_count=len(chunks), now=now, chunk_space=self._space,
                       keys_capped=keys_cut)
        return await self._v2_content(user_id, fresh, done, chunks, now, summary)

    async def _v2_content(self, user_id: str, before: DriveIndexRecord, done: DriveIndexRecord,
                          chunks: List[DriveChunk], now: datetime, summary: Dict[str, int]) -> DriveIndexRecord:
        """Search v2's part of an index run, between the fence re-read and the commit (§25.4.7): the profiler input's
        fingerprint from the new text, the stored profile re-verified against it at once (before any model call), a
        short note's template, and the passage hints reused into the new generation."""
        fingerprint = enrich.input_fingerprint_of(done, chunks)
        done = replace(done, profile_input_fingerprint=fingerprint)
        if done.profile is not None and done.profile_source == ProfileSource.MODEL.value:
            policy_ = record_policy(done, text_chars=done.text_chars, script_rendered=done.script_rendered)
            verified, counts = await asyncio.to_thread(grade, done.profile, chunks, done.name,
                                                       key_values=policy_.output is ProfileOutput.FULL)
            _count_grades(summary, counts)
            done = enrich.reverified(done, verified, now)
            _inc(summary, "reverified")
        if enrich.template_reason(done) is TemplateReason.SHORT:
            text = stored_text(chunks)
            done = enrich.profiled(done, template_profile(TemplateReason.SHORT, done.kind, text),
                                   source=ProfileSource.TEMPLATE, fingerprint=fingerprint, now=now)
        done = enrich.brief_graded(done, chunks, now)   # a brief date the text does not hold is no file date (Δ60)
        done = scored(done, chunks)   # the new brief, capture and reading facts, and the re-graded profile (§26.4)
        return await self._reuse_hints(user_id, before, done, chunks, summary)

    async def _reuse_hints(self, user_id: str, before: DriveIndexRecord, done: DriveIndexRecord,
                           chunks: List[DriveChunk], summary: Dict[str, int]) -> DriveIndexRecord:
        """§25.4.7 (N4): a new chunk takes an old chunk's hint items when its text hash matches, or when at least
        PASSAGE_REUSE_SHARE of its paragraphs came from that one old chunk; the items are grounded again against the
        new text and written as hint documents of the NEW generation (fenced: invisible until it commits) with the old
        hint vector. Chunks left without hints wait for passage enrichment (the plan's cooldown)."""
        old_gen, new_gen = before.indexed_generation, done.indexed_generation
        if (old_gen is None or new_gen is None or old_gen == new_gen or before.chunk_space != self._space
                or enrich.chunks_to_hint(done) == 0):
            return done
        old_hints = {h.index: (h, v) for h, v in await self._repo.get_hints_with_vectors(user_id, done.item_id, old_gen)
                     if h.passages_version == PASSAGES_VERSION}
        if not old_hints:
            return done
        olds = [old_chunk(c.index, c.text)
                for c in await self._repo.get_chunks(user_id, done.item_id, old_gen, sorted(old_hints))]
        to_hint = enrich.chunks_to_hint(done)
        docs: List[DriveHintDoc] = []
        vectors: List[List[float]] = []
        for chunk in chunks[:to_hint]:
            source = reuse_source(chunk.text, olds)
            if source is None or source not in old_hints:
                continue
            hint, vector = old_hints[source]
            grounded = ground(chunk.text, hint.items, chunk_word_keys=chunk.word_keys, keyword_keys=word_form_keys)
            if not grounded.items:
                continue
            docs.append(DriveHintDoc(user_id=user_id, item_id=done.item_id, generation=new_gen, index=chunk.index,
                                     kind=done.kind, chunk_text_hash=chunk_text_hash(chunk.text),
                                     items=grounded.items, hint_keys=grounded.hint_keys,
                                     passages_version=PASSAGES_VERSION))
            vectors.append(list(vector))
        if not docs:
            return done
        _fence()
        await self._recheck_gate(user_id)
        await self._repo.write_hints(docs, vectors)
        _inc(summary, "hints_reused", len(docs))
        reused = {d.index for d in docs}
        complete = all(i in reused for i in range(to_hint))
        return replace(done, passages_generation=new_gen, passages_version=PASSAGES_VERSION,
                       passages_next_index=to_hint if complete else 0, passages_attempts=0)

    async def _reusable_vectors(self, user_id: str, run: DriveIndexRecord) -> Dict[str, List[float]]:
        """The readable generation's vectors by chunk-text hash, when it is in this tick's embedding space (final
        review F1-3): an edit embeds only its new or changed chunks. A vector from another space is never mixed in."""
        readable = run.indexed_generation
        if readable is None or run.chunk_count <= 0 or run.chunk_space != self._space:
            return {}
        stored = await self._repo.get_chunks_with_vectors(user_id, run.item_id, readable, range(run.chunk_count),
                                                          terms=False)
        return {_text_key(c.text): vector for c, vector in stored if c.text and vector}

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
        still due; a chunk this space refuses (HTTP 400) is terminal for the space (chunks_refused, R3 F2-3); chunks that
        cannot be read back as stored, an embedding timeout or any other error send the file to a full index run,
        which counts it."""
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
            # a definite refusal by this space's model: terminal here, never re-read (a reader call would buy the
            # same refusal); the old-space chunks go, so the space change completes (R3 F2-3)
            logger.warning(f"[DriveIndex] a chunk of item {rec.item_id} for {user_id[:8]} was refused in "
                           f"{self._space}: {exc}; no chunks in this space")
            fresh = (await self._repo.get_records(user_id, [rec.item_id])).get(rec.item_id)
            mark.committed = True
            if (fresh is None or fresh.content_version != rec.content_version or fresh.indexed_generation != readable
                    or fresh.index_generation != generation):
                return   # the record moved on: its own work decides (the sweep removes any debris, §7.5)
            done = _maintenance_kept(chunks_refused(fresh, self._space, now), rec.priority)
            await self._commit(user_id, [done], set(), before={fresh.item_id: fresh})
            summary["reembed_refused"] = summary.get("reembed_refused", 0) + 1
            return
        summary["embed_ms"] = summary.get("embed_ms", 0) + int((self._monotonic() - started) * 1000)
        keyed = await asyncio.to_thread(_keys_of, [c.text for c in stored])
        chunks = [replace(c, generation=generation, content_terms=(), word_keys=k, keys_version=KEYS_VERSION)
                  for c, (k, _) in zip(stored, keyed)]
        # its hint documents move with it, their vectors embedded in this space too (§25.2.2: fenced like chunks)
        hints = await self._repo.get_hints(user_id, rec.item_id, readable)
        hint_vectors: List[List[float]] = []
        if hints:
            try:
                hint_vectors = await asyncio.wait_for(self._embed_texts([_hint_text(h.items) for h in hints]),
                                                      timeout=EMBED_FILE_BUDGET_S)
            except LLMClientError as exc:
                if exc.http_status != 400:
                    raise   # systemic (§8.2)
                logger.warning(f"[DriveIndex] hint text of item {rec.item_id} for {user_id[:8]} refused in "
                               f"{self._space}: {exc}; its passage hints are written again")
                hints = []
        _fence()
        await self._recheck_gate(user_id)
        for start in range(0, len(chunks), RECORD_WRITE_BATCH):
            await self._repo.write_chunks(chunks[start:start + RECORD_WRITE_BATCH],
                                          vectors[start:start + RECORD_WRITE_BATCH])
        if hints:
            await self._repo.write_hints([replace(h, generation=generation) for h in hints], hint_vectors)
        fresh = (await self._repo.get_records(user_id, [rec.item_id])).get(rec.item_id)
        if (fresh is None or fresh.content_version != rec.content_version or fresh.indexed_generation != readable
                or fresh.index_generation != generation):
            logger.info(f"[DriveIndex] item {rec.item_id} of {user_id[:8]} changed while re-embedding; dropped")
            mark.committed = True   # the record is newer than the re-embed: nothing may write over it
            _fence()
            await self._collect(user_id, rec.item_id, fresh.indexed_generation if fresh is not None else None)
            return
        done = chunks_reembedded(fresh, generation, self._space, now)
        done = replace(done, keys_version=KEYS_VERSION, keys_pending=False,
                       keys_capped=any(cut for _, cut in keyed),
                       passages_generation=(generation if fresh.passages_generation == readable and hints
                                            else None if fresh.passages_generation == readable
                                            else fresh.passages_generation))
        if not (content_due(done) or triage_due(done)):   # waiting content keeps its own class and time (§7.8)
            done = _maintenance_kept(done, rec.priority)
        await self._commit(user_id, [done], set())
        mark.committed = True
        _fence()
        await self._collect(user_id, rec.item_id, generation)
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
        await self._collect(user_id, final.item_id, final.indexed_generation)
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
            await self._repo.upsert_records([enrich.planned(transcript_plan(back, self._transcript_ctx()),
                                                            self._enrich_ctx())])
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
        """Model work waits, still pending, while the model is out (§24.8) or until the dollars left for the reader
        (the initial build, then the month's cap, §25.4.5) pay for the `pages` the file needs at the per-page estimate
        (reader_pages): it is never cut to what is left of this month (§24.7). A file the reader settled less
        than MODEL_REREAD_COOLDOWN ago waits for it to end, in its class: the edits made meanwhile are read once, at
        their newest version (final review F1-3)."""
        cooled = reread_cooldown_until(rec)
        if cooled is not None and now < cooled:
            return deferred(_cleared(rec), cooled, now, priority=cls)
        if not models.available(now):
            return deferred(_cleared(rec), _backoff_until(models), now, priority=cls)
        if self._affordable(UsageClass.READER.value, READER_PAGE_USD_ESTIMATE) < pages:
            # the reader's dollars this month and the initial build cannot pay for the pages it needs (§25.4.5)
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
        left = self._affordable(UsageClass.TRIAGE.value, TRIAGE_IMAGE_USD_ESTIMATE)
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
                await self._commit(user_id, writes, set(), before={r.item_id: r for r in batch})   # verdicts stay
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
                                      camera_prior=camera_prior(rec.facets),
                                      place_name=rec.place_name or "", location=coarse_location(rec.facets)))
            waiting[rec.item_id] = rec
        if images:
            try:
                reply = await port.triage(user_id, self._account_ids.get(user_id, ""), images)
            except Exception as exc:   # the port reports outcomes as a status; anything raised is a per-file transient
                logger.error(f"[DriveIndex] triage failed for {user_id[:8]}: {exc}", exc_info=True)
                writes.extend(content_retry(r, Reason.EXTRACT_ERROR, now) for r in waiting.values())
            else:
                # Triage v2 (§25.4.3, §25.13.1): a verdict is stored with the photo fields, a refused image is a
                # photograph without a description, a capped image is a per-file retry (a text scan must not become a
                # permanent photograph), an outage defers what it left unsettled; an image left unanswered after the
                # correction turn is a per-file retry; in an outage the images answered before it keep their verdicts.
                # The dollars go to the triage class (the fallback's share for the daily alert).
                outage = reply.status in (ModelReplyStatus.UNAVAILABLE, ModelReplyStatus.FAILED)
                _count_triage(models, summary, now, calls=reply.model_calls, cost=reply.usd,
                              images=len(reply.results) if outage else len(images), outage=outage)
                await self._spend_usd(user_id, UsageClass.TRIAGE.value, reply.usd, reply.fallback_usd,
                                      reply.model_calls)
                if outage:
                    logger.warning(f"[DriveIndex] triage unavailable for {user_id[:8]} ({reply.status.value}); "
                                   f"model work deferred")
                    models.outage(now)   # after the usage, which leaves the backoff to this outage (C-M1, T-M2)
                by_id: Dict[str, TriageResultV2] = {r.item_id: r for r in reply.results}
                for item_id in reply.refused_item_ids:
                    by_id.setdefault(item_id, TriageResultV2(item_id=item_id, verdict=TriageVerdict.PHOTO))
                _inc(summary, "triage_capped", len(reply.capped_item_ids))
                _inc(summary, "triage_refused", len(reply.refused_item_ids))
                for item_id, rec in waiting.items():
                    result = by_id.get(item_id)
                    if result is not None:
                        writes.append(_verdict(rec, result, now))
                    elif outage:
                        writes.append(deferred(rec, _backoff_until(models), now))
                        summary["deferred"] = summary.get("deferred", 0) + 1
                    else:
                        writes.append(content_retry(rec, Reason.EXTRACT_ERROR, now))
        await self._commit(user_id, writes, set(), before={r.item_id: r for r in batch})
        summary["triaged"] = summary.get("triaged", 0) + sum(1 for w in writes if w.triage_verdict is not None)
        for rec in gone:
            await self._item_gone(user_id, rec, rec.priority, ctx, budget, summary)

    async def _item_gone(self, user_id: str, rec: DriveIndexRecord, cls: Optional[int], ctx: PlanContext,
                         budget: _Budget, summary: Dict[str, int], mark: Optional[_RunMark] = None) -> None:
        """A download or a rendition answered "not found" (§26.2): the item is re-read by id once. A 404 again means the
        bot cannot open it: marked, never deleted (Δ48) — existence is decided by delta, whose tombstone removes a file
        that was really deleted. An item that is readable after all costs a per-file attempt (§8.2), so a provider
        that keeps contradicting itself cannot loop."""
        try:
            change: Optional[DriveChange] = await self._drive.get_change(user_id, rec.item_id)
        except DriveItemNotFoundError:
            change = None
        except (DriveNotConnectedError, DriveThrottled) as exc:
            logger.warning(f"[DriveIndex] re-reading item {rec.item_id} hit a systemic error for {user_id[:8]}: "
                           f"{type(exc).__name__}")
            raise
        except _PER_ITEM_ERRORS as exc:
            logger.warning(f"[DriveIndex] re-reading item {rec.item_id} failed for {user_id[:8]}: {exc}")
            await self._end_run(user_id, rec, content_retry(rec, Reason.DOWNLOAD_ERROR, ctx.now), cls, summary, mark)
            return
        if change is None or change.deleted:   # a port still reporting a deletion is read as the 404 it was
            await self._mark_inaccessible(user_id, rec, ctx, budget, summary, mark)
            return
        await self._apply(user_id, ctx, [change], budget, summary)
        if mark is not None:
            mark.committed = True   # the re-observation is written: the run's snapshot is outdated
        fresh = (await self._repo.get_records(user_id, [rec.item_id])).get(rec.item_id)
        if fresh is None:
            return   # deleted, its chunks with it (§7.6)
        if fresh.content_version == rec.content_version and (content_due(fresh) or triage_due(fresh)):
            await self._end_run(user_id, rec, content_retry(fresh, Reason.DOWNLOAD_ERROR, ctx.now), cls, summary, mark)
        elif fresh.attempt_started_at is not None and fresh.attempt_started_at == rec.attempt_started_at:
            await self._commit(user_id, [replace(fresh, attempt_started_at=None)], set())   # a new version: no crash

    async def _mark_inaccessible(self, user_id: str, rec: DriveIndexRecord, ctx: PlanContext, budget: _Budget,
                                 summary: Dict[str, int], mark: Optional[_RunMark] = None) -> None:
        """The stored record is marked (a started run ends without an attempt), then its parent is checked (Δ50)."""
        fresh = (await self._repo.get_records(user_id, [rec.item_id])).get(rec.item_id)
        if fresh is None:
            return   # its tombstone was applied meanwhile
        marked = marked_inaccessible(fresh, ctx.now)
        await self._commit(user_id, [marked], set())
        if mark is not None:
            mark.committed = True
        _inc(summary, "inaccessible_folders" if marked.is_folder else "inaccessible")
        logger.info(f"[DriveIndex] item {rec.item_id} of {user_id[:8]} cannot be opened by the bot (404 by id, listed "
                    f"by delta); marked, not deleted")
        await self._mark_parent(user_id, marked, ctx, budget, summary)

    async def _mark_parent(self, user_id: str, rec: DriveIndexRecord, ctx: PlanContext, budget: _Budget,
                           summary: Dict[str, int]) -> None:
        """Folders are never downloaded, so a folder is checked when one of its files is marked (Δ50): the nearest
        unmarked ancestor gets one item call (the marked ones are climbed through, so each marking climbs one level
        more) and a 404 marks it. The root is never marked."""
        parent_id: str = rec.parent_id
        parent: Optional[DriveIndexRecord] = None
        for _ in range(MAX_CHAIN_DEPTH):
            if not parent_id or parent_id == ctx.root_item_id:
                return
            parent = (await self._repo.get_records(user_id, [parent_id])).get(parent_id)
            if parent is None or not inaccessible(parent):
                break
            parent_id = parent.parent_id
        if parent is None or inaccessible(parent) or not budget.item_call_allowed():
            return
        budget.item_calls -= 1
        try:
            await self._drive.get_change(user_id, parent.item_id)
        except DriveItemNotFoundError:
            await self._commit(user_id, [marked_inaccessible(parent, ctx.now)], set())
            _inc(summary, "inaccessible_folders")
            await self._mark_subfolders(user_id, parent, ctx.now)
            logger.info(f"[DriveIndex] folder {parent.item_id} of {user_id[:8]} cannot be opened by the bot; marked")
        except (DriveNotConnectedError, DriveThrottled) as exc:
            logger.warning(f"[DriveIndex] parent check of item {rec.item_id} hit a systemic error for {user_id[:8]}: "
                           f"{type(exc).__name__}")
            raise
        except _PER_ITEM_ERRORS as exc:   # the parent stays as it is; a later marking checks it again
            logger.warning(f"[DriveIndex] parent check of item {rec.item_id} failed for {user_id[:8]}: {exc}")

    async def _subfolders(self, user_id: str, folder: DriveIndexRecord) -> List[DriveIndexRecord]:
        """The stored folders below `folder`, breadth first, reading at most SUBFOLDER_SCAN_MAX records (Firestore
        reads only, no provider call). The budget counts every child read, files included: `children` has no folder
        filter, and one would need a new composite index (user_id, parent_id, is_folder, item_id). A folder with more
        direct files than the budget, in item-id order before its sub-folders, may leave sub-folders unmarked (RFC
        Δ78): they are marked by their own 404 when they are next listed, or answer "No folder" on a path listing."""
        found: List[DriveIndexRecord] = []
        level, left = [folder.item_id], SUBFOLDER_SCAN_MAX
        while level and left > 0:
            below: List[str] = []
            for parent_id in level:
                after: Optional[str] = None
                while left > 0:
                    want = min(left, RECORD_WRITE_BATCH)
                    page = await self._repo.children(user_id, parent_id, after, want)
                    left -= len(page)
                    kids = [k for k in page if k.is_folder and k.state is not RecordState.DELETING]
                    found.extend(kids)
                    below.extend(k.item_id for k in kids)
                    if len(page) < want:
                        break
                    after = page[-1].item_id
            level = below
        return found

    async def _mark_subfolders(self, user_id: str, folder: DriveIndexRecord, now: datetime) -> None:
        """A folder the bot cannot open marks its stored sub-folders as well (review of S1): they inherit its
        permissions, and a sub-folder listed by path must answer the access sentence. Marked by inference, not by its
        own 404, each is due for its own re-read within INACCESSIBLE_RETRY_MIN (second review of S1), so one that has
        its own permissions for the app shows the access line for at most an hour."""
        subs = [access_observed(marked_inaccessible(r, now), now) for r in await self._subfolders(user_id, folder)
                if not inaccessible(r)]
        if subs:
            await self._commit(user_id, subs, set())
            logger.info(f"[DriveIndex] {len(subs)} sub-folders of folder {folder.item_id} of {user_id[:8]} marked "
                        f"with it")

    async def _recheck_step(self, user_id: str, rec: DriveIndexRecord, ctx: PlanContext, budget: _Budget,
                            summary: Dict[str, int]) -> bool:
        """One id read of a marked record (Δ51): "not found" again restarts its retry clock; a readable answer clears
        the marking silently (no notice) and the item is observed again, so the ordinary content path resumes."""
        if not budget.item_call_allowed():
            return False
        budget.item_calls -= 1
        try:
            change = await self._drive.get_change(user_id, rec.item_id)
        except DriveItemNotFoundError:
            await self._commit(user_id, [access_rechecked(rec, ctx.now)], set())
            _inc(summary, "inaccessible_rechecked")
            return True
        except (DriveNotConnectedError, DriveThrottled) as exc:
            logger.warning(f"[DriveIndex] re-check of item {rec.item_id} hit a systemic error for {user_id[:8]}: "
                           f"{type(exc).__name__}")
            raise
        except _PER_ITEM_ERRORS as exc:   # proves nothing: tried, the next read waits for the retry clock
            logger.warning(f"[DriveIndex] re-check of item {rec.item_id} failed for {user_id[:8]}: {exc}")
            await self._commit(user_id, [access_rechecked(rec, ctx.now)], set())
            return True
        if change.deleted:   # a port still reporting a deletion: delta decides existence (§26.2)
            await self._commit(user_id, [access_rechecked(rec, ctx.now)], set())
            return True
        regained = access_regained(rec, ctx.now)
        if extractor_stale(regained, self._extractor_versions):
            regained = reextraction_due(regained)   # an upgrade pass skipped it while it could not be read
        await self._commit(user_id, [regained], set())
        await self._apply(user_id, ctx, [change], budget, summary)
        if regained.is_folder:   # its marked sub-folders are due for their own re-read (Δ51)
            observed = [access_observed(r, ctx.now) for r in await self._subfolders(user_id, regained)
                        if inaccessible(r) and not r.inaccessible_reobserved]
            if observed:
                await self._commit(user_id, observed, set())
        _inc(summary, "access_regained")
        logger.info(f"[DriveIndex] item {rec.item_id} of {user_id[:8]} can be opened again")
        return True

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

    # -- Search v2: model dollars (§25.4.5) -----------------------------------------------------

    def _enrich_ctx(self) -> EnrichContext:
        spend = _TICK_SPEND.get()
        return spend.ctx() if spend is not None else enrich.default_context(self._clock())

    def _affordable(self, cls: str, unit_usd: float) -> int:
        """Units (pages, images) the dollars left for `cls` pay for at the per-unit estimate: the open initial build,
        then the month's rest of the class cap (§25.4.5). Outside a tick: no limit."""
        spend = _TICK_SPEND.get()
        if spend is None:
            return 1 << 30
        return int(spend.available(cls) / unit_usd + 1e-9)

    async def _spend_usd(self, user_id: str, cls: str, usd: float, fallback_usd: float, calls: int,
                         month: Optional[str] = None) -> None:
        """Dollars a model call cost, priced by the runner at the model that ran (failover included): the initial
        build covers what it can first (D27), and the month's document keeps every dollar per class, the fallback's
        share and the day's totals (the fallback alert), with the build's share apart, which the month's cap does not
        count (review rv_sv2_p5 M1). Never retried, never fenced: the call was made (§25.13.1).

        Shielded (review rv_sv2_p5 MINOR 5): the file's ENRICH_FILE_BUDGET_S cut, or a sibling's fatal error, may
        cancel the caller right after a paid reply; the write still completes, whole (the build's share and the
        month's document together), and the cancellation reaches the caller.

        `month` ("YYYY-MM") books the dollars in that month's document instead of the tick's: a transcription batch's
        real cost goes to its RESERVATION's month when collected, so a month turn never puts dollars into a month
        that did not reserve them (G9)."""
        if usd <= 0 and calls <= 0:
            return
        await asyncio.shield(self._spend_write(user_id, cls, usd, fallback_usd, calls, _TICK_SPEND.get(),
                                               month=month))

    async def _spend_write(self, user_id: str, cls: str, usd: float, fallback_usd: float, calls: int,
                           spend: Optional[_Spend], month: Optional[str] = None) -> None:
        now = spend.now if spend is not None else self._clock()
        covered = 0.0
        # G8: the initial build never pays for transcription (§27.10: no allowance)
        if usd > 0 and cls != UsageClass.TRANSCRIBE.value and (spend is None or spend.build.closed_at is None):
            build, covered = await self._repo.spend_initial_build(user_id, usd, now)
            if spend is not None:
                spend.build = build
        day = now.astimezone(timezone.utc).date().isoformat()
        args = (cls, max(usd, 0.0), max(fallback_usd, 0.0), max(calls, 0), covered)
        booked = month or month_key(now)
        # A booking into another month than today's (a late-collected batch, G9) gets no daily entry: today's day key
        # does not belong in the old month's document ("" = skip, see add_model_usage)
        await self._repo.add_model_usage(user_id, booked, day if booked == month_key(now) else "", *args)
        if spend is not None:
            if booked == month_key(spend.now):   # the tick holds this month's document only
                spend.usage = added_usage(spend.usage, day, *args)
            for key, value in ((cls, usd), ("fallback", fallback_usd), ("initial_build", covered)):
                if value > 0:
                    spend.usd[key] = spend.usd.get(key, 0.0) + value

    # -- Search v2: enrichment (§25.4.1-§25.4.7) ------------------------------------------------

    def _enrich_wired(self) -> bool:
        return self._profiler is not None and self._passages is not None

    @staticmethod
    def _jobs_now(rec: DriveIndexRecord, models: _Models, now: datetime) -> Tuple[bool, bool]:
        """The record's ready jobs whose agent type is not backed off (§25.4.6: a backoff gates the queue read, it
        is not a job state). A template profile calls no model."""
        profile = rec.profile_state == _READY and (enrich.template_reason(rec) is not None
                                                   or models.agent_available(PROFILER_AGENT, now))
        passages = rec.passages_state == _READY and models.agent_available(PASSAGES_AGENT, now)
        return profile, passages

    async def _enrich_todo(self, user_id: str, now: datetime, models: _Models, skip: Set[str],
                           profiled: Set[str]) -> List[DriveIndexRecord]:
        """The queue's records with a job an available agent can do now, newest first, leaving out `skip` and the
        profile job of `profiled`. A full page with nothing to do now may hide the other agent type's work further
        down (one agent backed off, its records first in the queue): each available agent type's own queue is read
        then (review rv_sv2_p5 MINOR 6)."""

        def wanted(rec: DriveIndexRecord) -> bool:
            if rec.item_id in skip:
                return False
            profile, passages = self._jobs_now(rec, models, now)
            return (profile and rec.item_id not in profiled) or passages

        page = await self._repo.enrich_queue(user_id, ENRICH_QUEUE_PAGE)
        todo = [r for r in page if wanted(r)]
        if todo or len(page) < ENRICH_QUEUE_PAGE:
            return todo
        for job, agent in ((EnrichJob.PASSAGES, PASSAGES_AGENT), (EnrichJob.PROFILE, PROFILER_AGENT)):
            if models.agent_available(agent, now):
                todo = [r for r in await self._repo.enrich_queue(user_id, ENRICH_QUEUE_PAGE, job=job.value)
                        if wanted(r)]
                if todo:
                    return todo
        return []

    async def _enrichment_waiting(self, user_id: str, now: datetime, models: _Models,
                                  idle: Optional[Set[str]] = None) -> bool:
        """The tick continues while the queue holds a record with a job an available agent can do now, or a deferred
        or parked job is due to wake. A record whose run this tick wrote nothing (`idle`) is not waited for: it is
        held by its run's outcome, or tried again by the next fan-out, never by a zero-delay chain (review rv_sv2_p5
        M4)."""
        if not self._enrich_wired():
            return False
        if await self._repo.enrich_wakeups(user_id, now, 1):
            return True
        return bool(await self._enrich_todo(user_id, now, models, idle or set(), set()))

    async def _enrich_phase(self, user_id: str, now: datetime, budget: _Budget, summary: Dict[str, int],
                            models: _Models) -> bool:
        """The enrichment share of the tick (§25.4.5): wake-ups re-planned, then the queue (newest first), up to
        ENRICH_IN_FLIGHT files at once, all model calls under MODEL_IN_FLIGHT. It gets a start window of at least
        ENRICH_SHARE of the start budget, or `1 - MAINTENANCE_SHARE` of what the content classes left, whichever is
        more (a start window: a file started in it runs to its own ENRICH_FILE_BUDGET_S). A file's fatal error
        (the lease, the gate, a systemic store or embedding error) cancels its siblings at once (review rv_sv2_p5
        MINOR 5); what they spent is recorded (`_spend_usd` is shielded). Returns whether any record was enriched or
        re-planned."""
        if not self._enrich_wired():
            return False
        started = self._monotonic()
        total = TICK_START_BUDGET.total_seconds()
        at = budget.elapsed()
        limit = budget.enrichment_limit()   # the start budget, less the extraction share while it has work (G16)
        stop_at = min(limit, at + max(ENRICH_SHARE * total - budget.enrich_s, (1 - MAINTENANCE_SHARE) * (limit - at)))
        worked = False
        try:
            woken = await self._repo.enrich_wakeups(user_id, now, ENRICH_WAKEUPS_PAGE)
            if woken:
                await self._commit(user_id, woken, set())   # re-planned: a cooldown or a month turn ended
                _inc(summary, "enrich_woken", len(woken))
                worked = True
            processed = budget.enriched
            model_slots = asyncio.Semaphore(MODEL_IN_FLIGHT)
            while budget.enrichment_allowed(stop_at):
                todo = await self._enrich_todo(user_id, now, models, processed | budget.enrich_idle,
                                               budget.profiled_inline)
                if not todo:
                    break
                files = asyncio.Semaphore(ENRICH_IN_FLIGHT)

                async def one(rec: DriveIndexRecord) -> bool:
                    async with files:
                        if not budget.enrichment_allowed(stop_at):
                            return False
                        processed.add(rec.item_id)
                        await self._enrich_tracked(user_id, rec.item_id, now, budget, summary, models, model_slots,
                                                   profile=rec.item_id not in budget.profiled_inline)
                        return True

                tasks = [asyncio.create_task(one(r)) for r in todo]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
                finally:
                    for task in tasks:
                        task.cancel()   # a no-op on a finished file; a fatal error stops the others now
                    await asyncio.gather(*tasks, return_exceptions=True)
                errors = [t.exception() for t in tasks if not t.cancelled() and t.exception() is not None]
                if errors:
                    raise next((e for e in errors if isinstance(e, (LeaseLost, _OptedOut))), errors[0])
                if not any(t.result() is True for t in tasks):
                    break
                worked = True
        finally:
            budget.enrich_s += self._monotonic() - started
        return worked

    async def _enrich_inline(self, user_id: str, item_id: str, now: datetime, budget: _Budget,
                             summary: Dict[str, int], models: _Models) -> None:
        """A first-seen file whose profile is ready when its content commits is profiled in the same run, charged to
        the enrichment share, so its card is embedded once (§25.4.5). The profile only (review rv_sv2_p5 M3): its
        passages go through the enrichment phase, in parallel with other files and within its start window."""
        if not self._enrich_wired():
            return
        rec = (await self._repo.get_records(user_id, [item_id])).get(item_id)
        if rec is None or rec.profile_state != _READY or not self._jobs_now(rec, models, now)[0]:
            return
        if not budget.enrichment_allowed(TICK_START_BUDGET.total_seconds()):
            return
        budget.profiled_inline.add(item_id)
        started = self._monotonic()
        try:
            await self._enrich_tracked(user_id, item_id, now, budget, summary, models,
                                       asyncio.Semaphore(MODEL_IN_FLIGHT), passages=False, inline=True)
        finally:
            budget.enrich_s += self._monotonic() - started

    async def _enrich_tracked(self, user_id: str, item_id: str, now: datetime, budget: _Budget,
                              summary: Dict[str, int], models: _Models, slots: asyncio.Semaphore, *,
                              profile: bool = True, passages: bool = True, inline: bool = False) -> None:
        """`_enrich_file` with its progress check (review rv_sv2_p5 M4, MINOR 2): a run that left the record as it was
        is idle for the rest of the tick; one that changed it is enrichment progress (the stalled-rebuild alert)."""
        before = (await self._repo.get_records(user_id, [item_id])).get(item_id)
        await self._enrich_file(user_id, item_id, now, summary, models, slots, profile=profile, passages=passages)
        after = (await self._repo.get_records(user_id, [item_id])).get(item_id)
        if after != before:
            budget.enrich_progressed = True
        elif not inline:
            budget.enrich_idle.add(item_id)
            if after is not None and after.enrich_ready:
                logger.warning(f"[DriveIndex] enrichment of item {item_id} for {user_id[:8]} wrote nothing; it waits "
                               f"for the next fan-out")
                _inc(summary, "enrich_idle")

    async def _enrich_file(self, user_id: str, item_id: str, now: datetime, summary: Dict[str, int],
                           models: _Models, slots: asyncio.Semaphore, *, profile: bool = True,
                           passages: bool = True) -> None:
        """One file's enrichment inside ENRICH_FILE_BUDGET_S (a hard wait_for, §25.4.5): its profile, its passage
        batches with their hint embeddings, and its card. Progress is saved per batch, so a cut file resumes. A
        model outcome is never a content failure (§25.4.6); a systemic error (the store, the embeddings, the lease)
        ends the tick."""
        try:
            await asyncio.wait_for(self._enrich_run(user_id, item_id, now, summary, models, slots, profile=profile,
                                                    passages=passages),
                                   timeout=ENRICH_FILE_BUDGET_S)
        except asyncio.TimeoutError:
            logger.warning(f"[DriveIndex] enrichment of item {item_id} for {user_id[:8]} passed "
                           f"{ENRICH_FILE_BUDGET_S}s; it resumes from its saved progress")
            _inc(summary, "enrich_timeouts")
        except (LeaseLost, _OptedOut):
            raise
        except _RUN_SYSTEMIC as exc:
            logger.warning(f"[DriveIndex] enrichment of item {item_id} for {user_id[:8]} ended by "
                           f"{type(exc).__name__}: {exc}")
            raise
        except Exception as exc:   # one file never stalls the queue; it is ready again next tick
            logger.error(f"[DriveIndex] enrichment of item {item_id} for {user_id[:8]} failed: "
                         f"{type(exc).__name__}: {exc}", exc_info=True)
            _inc(summary, "enrich_errors")

    async def _enrich_run(self, user_id: str, item_id: str, now: datetime, summary: Dict[str, int],
                          models: _Models, slots: asyncio.Semaphore, *, profile: bool = True,
                          passages: bool = True) -> None:
        rec = (await self._repo.get_records(user_id, [item_id])).get(item_id)
        if rec is None or not rec.enrich_ready:
            return
        do_profile, do_passages = self._jobs_now(rec, models, now)
        do_profile, do_passages = do_profile and profile, do_passages and passages
        if not (do_profile or do_passages):
            return
        chunks: List[DriveChunk] = []
        vectors: Dict[int, List[float]] = {}
        if rec.indexed_generation is not None and rec.chunk_count > 0:
            rows = await self._repo.get_chunks_with_vectors(user_id, item_id, rec.indexed_generation,
                                                            range(rec.chunk_count), vectors=do_passages)
            chunks = sorted((c for c, _ in rows), key=lambda c: c.index)
            vectors = {c.index: v for c, v in rows if v}
        if do_profile:
            after = await self._profile_job(user_id, rec, chunks, now, summary, models, slots)
            if after is None:
                return
            if card_eligible(after) and after.card_hash != card_hash(after):
                await self._embed_cards(user_id, [after], now, summary)   # its card carries the profile (§25.2.7)
            rec = (await self._repo.get_records(user_id, [item_id])).get(item_id)
            if rec is None:
                return
        if do_passages and rec.passages_state == _READY:
            if not chunks:
                # its readable generation has no stored chunk to send: held for it, never ready forever (M4)
                logger.warning(f"[DriveIndex] item {item_id} of {user_id[:8]} has no stored chunk for its passage "
                               f"hints; held until its next generation")
                _inc(summary, "passages_unusable")
                await self._commit_enriched(user_id, rec, enrich.passages_unusable)
                return
            await self._passages_job(user_id, rec, chunks, vectors, now, summary, models, slots)

    async def _commit_enriched(self, user_id: str, started: DriveIndexRecord,
                               apply: Callable[[DriveIndexRecord], DriveIndexRecord]) -> Optional[DriveIndexRecord]:
        """An enrichment result written onto the record as it is now (a rename meanwhile is kept), only while the text
        it was made from is still the readable one (§7.4's fence for enrichment): same content version, readable
        generation and README excerpt. Returns the planned record, or None when the result was dropped."""
        fresh = (await self._repo.get_records(user_id, [started.item_id])).get(started.item_id)
        if (fresh is None or fresh.indexed_generation != started.indexed_generation
                or fresh.content_version != started.content_version
                or fresh.readme_excerpt_version != started.readme_excerpt_version):
            logger.info(f"[DriveIndex] item {started.item_id} of {user_id[:8]} changed during its enrichment; "
                        f"result dropped")
            return None
        done = apply(fresh)
        await self._commit(user_id, [done], set())
        return enrich.planned(done, self._enrich_ctx())

    async def _profile_job(self, user_id: str, rec: DriveIndexRecord, chunks: List[DriveChunk], now: datetime,
                           summary: Dict[str, int], models: _Models,
                           slots: asyncio.Semaphore) -> Optional[DriveIndexRecord]:
        """One profile (§25.4.1): a short note's template from its stored text, or a profiler call over the policy's
        input, verified against the whole stored text; the outcome per §25.4.6. The skip rule (§25.4.7) is the plan's:
        the job is ready only when the input fingerprint or the version moved and the cooldown is over."""
        reason = enrich.template_reason(rec)
        if reason is TemplateReason.SHORT:
            text = stored_text(chunks)
            if not text.strip():
                _inc(summary, "profile_unusable")   # held, never ready forever (review rv_sv2_p5 M4)
                return await self._commit_enriched(user_id, rec, enrich.profile_unusable)
            fingerprint = enrich.input_fingerprint_of(rec, chunks)
            profile = template_profile(reason, rec.kind, text)
            _inc(summary, "profile_templates")
            return await self._commit_enriched(user_id, rec, lambda f: scored(enrich.profiled(
                f, profile, source=ProfileSource.TEMPLATE, fingerprint=fingerprint, now=now), chunks))
        if self._profiler is None:
            raise _NotWired("the profiler is not wired into the indexing service")
        inp = profile_input(rec, chunks, rec.kind)
        if inp is None:
            _inc(summary, "profile_unusable")   # nothing to send: held, never ready forever (review rv_sv2_p5 M4)
            return await self._commit_enriched(user_id, rec, enrich.profile_unusable)
        if enrich.available_usd(self._enrich_ctx(), UsageClass.PROFILE.value) < enrich.profile_estimate(rec):
            return await self._commit_enriched(user_id, rec, lambda f: f)   # re-planned: parked
        fingerprint = input_fingerprint(rec.kind, inp)
        previous = None
        if rec.profile is not None and rec.profile_source == ProfileSource.MODEL.value:
            previous = ProfileLabels(doc_type=rec.profile.doc_type, life_domains=rec.profile.life_domains,
                                     valid_until=rec.valid_until.date() if rec.valid_until is not None else None)
        async with slots:
            reply = await self._profiler.profile(ProfileRequest(
                user_id=user_id, account_id=self._account_ids.get(user_id, ""), item_id=rec.item_id, kind=rec.kind,
                name=rec.name, input=inp, previous=previous))
        status = reply.status
        await self._spend_usd(user_id, UsageClass.PROFILE.value, reply.usd, reply.fallback_usd,
                              0 if status is ModelReplyStatus.UNAVAILABLE and not reply.usd else 1)
        _inc(summary, f"profile_{status.value}")
        if status is ModelReplyStatus.UNAVAILABLE:
            # uncharged: the job stays ready behind this agent type's backoff; content work and keys go on
            models.agent_outage(PROFILER_AGENT, now)
            await self._persist_usage(models)
            return rec
        if status is ModelReplyStatus.OK and reply.profile is not None:
            policy_ = record_policy(rec, text_chars=rec.text_chars, script_rendered=rec.script_rendered)
            texts: Sequence[object] = chunks if chunks else [_Text(rec.readme_excerpt)]
            verified, counts = grade(reply.profile, texts, rec.name,   # type: ignore[arg-type]
                                     key_values=policy_.output is ProfileOutput.FULL)
            _count_grades(summary, counts)
            _inc(summary, "profiles")
            if models.agent_recovered(PROFILER_AGENT):
                await self._persist_usage(models)
            return await self._commit_enriched(user_id, rec, lambda f: scored(enrich.profiled(
                f, verified, source=ProfileSource.MODEL, fingerprint=fingerprint, now=now), chunks))
        if status is ModelReplyStatus.FAILED:
            models.agent_outage(PROFILER_AGENT, now)   # charged AND a backoff (§25.4.6); a fallback refusal too
            await self._persist_usage(models)
        if status is ModelReplyStatus.STOPPED_AT_CAP:
            _inc(summary, "stopped_at_cap")
        return await self._commit_enriched(user_id, rec, lambda f: enrich.profile_charged(f, status, now))

    async def _passages_job(self, user_id: str, rec: DriveIndexRecord, chunks: List[DriveChunk],
                            vectors: Mapping[int, List[float]], now: datetime, summary: Dict[str, int],
                            models: _Models, slots: asyncio.Semaphore) -> Optional[DriveIndexRecord]:
        """Passage hints for the chunks still without them (§25.4.2): PASSAGE_CHUNKS_PER_CALL chunks a call, up to
        PASSAGE_IN_FLIGHT calls at once; each batch's grounded hint documents written (fenced) before the next, the
        record re-read after each (a moved generation deletes this run's documents and stops it). Outcomes per
        §25.4.6: a refused or still-capped chunk gets no hints and never holds the file; `invalid`/`failed` chunks
        cost one attempt per run and stay due; `failed` and `unavailable` stop the run behind a backoff."""
        if self._passages is None or rec.indexed_generation is None:
            return rec
        gen = rec.indexed_generation
        started = enrich.passages_started(rec)
        to_hint = enrich.chunks_to_hint(started)
        have = {h.index for h in await self._repo.get_hints(user_id, rec.item_id, gen)
                if h.passages_version == PASSAGES_VERSION}
        by_index = {c.index: c for c in chunks}
        settled: Set[int] = set(have)
        missing = {i for i in range(started.passages_next_index, to_hint) if i not in have and i not in by_index}
        if missing:
            # a chunk the readable generation lost: skipped, so the run moves past it (review rv_sv2_p5 M4)
            logger.warning(f"[DriveIndex] item {rec.item_id} of {user_id[:8]}: {len(missing)} chunk(s) of generation "
                           f"{gen} missing; no hints for them")
            _inc(summary, "passage_missing_chunks", len(missing))
            settled |= missing
        pending = [i for i in range(started.passages_next_index, to_hint) if i not in have and i in by_index]
        title, what = (rec.profile.title, rec.profile.what_it_is) if rec.profile is not None else ("", "")
        batches = [pending[i:i + PASSAGE_CHUNKS_PER_CALL] for i in range(0, len(pending), PASSAGE_CHUNKS_PER_CALL)]
        charged = halted = failed = answered = False
        for at in range(0, len(batches), PASSAGE_IN_FLIGHT):
            wave = batches[at:at + PASSAGE_IN_FLIGHT]
            # the wave shrinks to the batches the dollars left pay for; none: the plan parks the rest (one batch is
            # what the plan's estimate asks for, so a wave never stops a job the plan keeps ready, M4)
            left = enrich.available_usd(self._enrich_ctx(), UsageClass.PASSAGE.value)
            while wave and left < PASSAGE_CHUNK_USD_ESTIMATE * sum(len(b) for b in wave):
                wave = wave[:-1]
            if not wave:
                break
            trimmed = len(wave) < len(batches[at:at + PASSAGE_IN_FLIGHT])
            replies = await asyncio.gather(*(self._hint_call(user_id, rec, batch, by_index, title, what, slots)
                                             for batch in wave))
            docs: List[DriveHintDoc] = []
            doc_vectors: List[List[float]] = []
            for batch, reply in zip(wave, replies):
                halted |= reply.status is ModelReplyStatus.UNAVAILABLE
                failed |= reply.status is ModelReplyStatus.FAILED
                for index in batch:
                    outcome = reply.outcome_by_index.get(index)
                    if outcome is None:
                        continue   # not attempted: an outage cut the batch; retried
                    _inc(summary, f"passage_{outcome.value}")
                    if outcome in (ModelReplyStatus.REFUSED, ModelReplyStatus.STOPPED_AT_CAP):
                        settled.add(index)
                        if outcome is ModelReplyStatus.STOPPED_AT_CAP:
                            _inc(summary, "stopped_at_cap")
                        continue
                    if outcome is not ModelReplyStatus.OK:
                        charged = True
                        continue
                    answered = True
                    settled.add(index)
                    _inc(summary, "passage_chunks")
                    made = await self._hint_doc(user_id, rec, gen, by_index[index],
                                                reply.items_by_index.get(index, ()), vectors.get(index), summary)
                    if made is not None:
                        docs.append(made[0])
                        doc_vectors.append(made[1])
            if docs:
                _fence()
                await self._recheck_gate(user_id)
                await self._repo.write_hints(docs, doc_vectors)
            fresh = (await self._repo.get_records(user_id, [rec.item_id])).get(rec.item_id)
            if fresh is None or fresh.indexed_generation != gen:
                if docs:   # the generation moved meanwhile: this run's documents go (§25.2.2)
                    _fence()
                    await self._repo.delete_hints_except(user_id, rec.item_id,
                                                         fresh.indexed_generation if fresh is not None else None)
                return None
            if halted or failed or trimmed:
                break
            if at + PASSAGE_IN_FLIGHT < len(batches):   # progress saved per batch: a cut file resumes (§25.4.5)
                done_to = _first_unsettled(started.passages_next_index, to_hint, settled)
                if await self._commit_enriched(user_id, rec, _passage_progress(started, done_to, now)) is None:
                    return None
        next_index = _first_unsettled(started.passages_next_index, to_hint, settled)
        if halted or failed:
            models.agent_outage(PASSAGES_AGENT, now)
            await self._persist_usage(models)
        elif answered and models.agent_recovered(PASSAGES_AGENT):
            await self._persist_usage(models)
        return await self._commit_enriched(user_id, rec, _passage_progress(
            started, next_index, now, finished=next_index >= to_hint, charged=charged))

    async def _hint_call(self, user_id: str, rec: DriveIndexRecord, batch: Sequence[int],
                         by_index: Mapping[int, DriveChunk], title: str, what: str,
                         slots: asyncio.Semaphore) -> HintReply:
        port = self._passages
        if port is None:
            raise _NotWired("passage hints are not wired into the indexing service")
        async with slots:
            reply = await port.hints(HintRequest(
                user_id=user_id, account_id=self._account_ids.get(user_id, ""), item_id=rec.item_id,
                chunks=[(i, by_index[i].text) for i in batch], title=title, what_it_is=what))
        # spent as soon as the reply is in (shielded): a cut of the wave never loses a paid call (MINOR 5)
        await self._spend_usd(user_id, UsageClass.PASSAGE.value, reply.usd, reply.fallback_usd,
                              0 if reply.status is ModelReplyStatus.UNAVAILABLE and not reply.usd else 1)
        return reply

    async def _hint_doc(self, user_id: str, rec: DriveIndexRecord, gen: int, chunk: DriveChunk,
                        items: Sequence[HintItem], chunk_vector: Optional[List[float]],
                        summary: Dict[str, int]) -> Optional[Tuple[DriveHintDoc, List[float]]]:
        """Grounding (§25.4.2): rules 1-3 over the chunk, its hint vector embedded, rule 4 against the chunk's vector.
        Drops are counted per rule; the hint-to-chunk similarity by deciles."""
        word_keys = chunk.word_keys or word_keys_capped(chunk.text)[0]
        grounded = ground(chunk.text, items, chunk_word_keys=word_keys, keyword_keys=word_form_keys)
        for key in ("evidence", "numbers", "plain", "invalid"):
            _inc(summary, f"hints_dropped_{key}", getattr(grounded.drops, key))
        if not grounded.items:
            return None
        try:
            vector = await self._embeddings.get_embedding(_hint_text(grounded.items), "RETRIEVAL_DOCUMENT")
        except LLMClientError as exc:
            if exc.http_status != 400:
                raise   # systemic (§8.2)
            logger.warning(f"[DriveIndex] hint text of item {rec.item_id} for {user_id[:8]} refused: {exc}")
            _inc(summary, "hints_dropped_embed")
            return None
        if chunk_vector:
            score = enrich.similarity(vector, chunk_vector)
            _inc(summary, f"hint_sim_d{min(9, max(0, int(score * 10)))}")
            grounded = drop_if_far(grounded, score)
            _inc(summary, "hints_dropped_far", grounded.drops.far)
            if not grounded.items:
                return None
        doc = DriveHintDoc(user_id=user_id, item_id=rec.item_id, generation=gen, index=chunk.index, kind=rec.kind,
                           chunk_text_hash=chunk_text_hash(chunk.text), items=grounded.items,
                           hint_keys=grounded.hint_keys, passages_version=PASSAGES_VERSION)
        return doc, list(vector)

    # -- Search v2: the keys pass (§25.4.8) ----------------------------------------------------

    async def _keys_step(self, user_id: str, rec: DriveIndexRecord, ctx: PlanContext, budget: _Budget,
                         summary: Dict[str, int]) -> bool:
        """Keys made under another KEYS_VERSION: the readable generation is re-keyed from its stored text into a NEW
        generation, the way `_reembed_run` re-embeds (no in-place writes): each chunk's text, offsets, kind, vector and
        `user_id` with new `word_keys`, its hint documents with re-keyed `hint_keys`; committed under the §7.4 fence
        (dropped if the content version or the generation moved), then the other generations' hints and chunks go. No
        model call, never budgeted."""
        if not budget.file_allowed(routed=False):
            return False
        readable = rec.indexed_generation
        if readable is None:
            return True
        rows = sorted(await self._repo.get_chunks_with_vectors(user_id, rec.item_id, readable, range(rec.chunk_count),
                                                               terms=False), key=lambda r: r[0].index)
        if rec.chunk_count == 0 or [c.index for c, _ in rows] != list(range(rec.chunk_count)):
            logger.warning(f"[DriveIndex] chunks of item {rec.item_id} for {user_id[:8]} cannot be re-keyed as "
                           f"stored; the file is read again")
            await self._reread(user_id, rec, ctx)
            return True
        budget.files -= 1
        now = ctx.now
        generation = rec.index_generation + 1
        await self._commit(user_id, [replace(rec, index_generation=generation)], set())   # never reused (§8.1 step 2)
        keyed = await asyncio.to_thread(_keys_of, [c.text for c, _ in rows])
        chunks = [replace(c, generation=generation, content_terms=(), word_keys=k, keys_version=KEYS_VERSION)
                  for (c, _), (k, _) in zip(rows, keyed)]
        chunk_keys = {c.index: c for c in chunks}
        hints: List[DriveHintDoc] = []
        hint_vectors: List[List[float]] = []
        for hint, vector in await self._repo.get_hints_with_vectors(user_id, rec.item_id, readable):
            chunk = chunk_keys.get(hint.index)
            if chunk is None:
                continue
            regrounded = ground(chunk.text, hint.items, chunk_word_keys=chunk.word_keys, keyword_keys=word_form_keys)
            hints.append(replace(hint, generation=generation, hint_keys=regrounded.hint_keys))
            hint_vectors.append(list(vector))
        _fence()
        await self._recheck_gate(user_id)
        for start in range(0, len(chunks), RECORD_WRITE_BATCH):
            await self._repo.write_chunks(chunks[start:start + RECORD_WRITE_BATCH],
                                          [v for _, v in rows[start:start + RECORD_WRITE_BATCH]])
        if hints:
            await self._repo.write_hints(hints, hint_vectors)
        fresh = (await self._repo.get_records(user_id, [rec.item_id])).get(rec.item_id)
        if (fresh is None or fresh.content_version != rec.content_version or fresh.indexed_generation != readable
                or fresh.index_generation != generation):
            logger.info(f"[DriveIndex] item {rec.item_id} of {user_id[:8]} changed while re-keying; dropped")
            _fence()
            await self._collect(user_id, rec.item_id, fresh.indexed_generation if fresh is not None else None)
            return True
        cut = any(c for _, c in keyed)
        done = replace(fresh, indexed_generation=generation, keys_version=KEYS_VERSION, keys_pending=False,
                       keys_capped=cut,
                       passages_generation=(generation if fresh.passages_generation == readable
                                            else fresh.passages_generation))
        done = schedule(done, now, first_seen=not done.has_card)
        if not (content_due(done) or triage_due(done)):
            done = _maintenance_kept(done, rec.priority)
        await self._commit(user_id, [done], set())
        _fence()
        await self._collect(user_id, rec.item_id, generation)
        _inc(summary, "rekeyed")
        _inc(summary, "keys_capped", int(cut))
        return True

    async def _rescore(self, user_id: str, rec: DriveIndexRecord, now: datetime,
                       summary: Dict[str, int]) -> DriveIndexRecord:
        """The accuracy upgrade of one record (§26.4 "Recomputing"): the stored model profile graded again against the
        stored chunk text, the score computed again, both written. Firestore only; the keys follow on the commit
        (`planned` runs `about_keys_of`), and the card text does not carry the score, so nothing is re-embedded. Chunks
        that cannot be read whole leave the stored grades as they are (grading against a partial text would call real
        values missing); the record is still scored under this version, from them."""
        chunks: List[DriveChunk] = []
        if rec.indexed_generation is not None and rec.chunk_count > 0:
            chunks = sorted(await self._repo.get_chunks(user_id, rec.item_id, rec.indexed_generation,
                                                        range(rec.chunk_count)), key=lambda c: c.index)
            if [c.index for c in chunks] != list(range(rec.chunk_count)):
                logger.warning(f"[DriveIndex] chunks of item {rec.item_id} for {user_id[:8]} are incomplete; "
                               f"its score is computed from the stored grades")
                _inc(summary, "rescore_incomplete")
                chunks = []
        done = rec
        if chunks and rec.profile is not None and rec.profile_source == ProfileSource.MODEL.value:
            policy_ = record_policy(rec, text_chars=rec.text_chars, script_rendered=rec.script_rendered)
            regraded, _counts = await asyncio.to_thread(grade, rec.profile, chunks, rec.name,
                                                        key_values=policy_.output is ProfileOutput.FULL)
            done = enrich.regraded(done, regraded, now)
        if not chunks and not (rec.profile is not None and rec.profile_source == ProfileSource.MODEL.value):
            # a brief-only card is graded against the text, and there is none whole: keep the stored score and stamp
            # the version, never grade the brief against nothing
            done = replace(done, accuracy_version=ACCURACY_VERSION)
        else:
            if chunks:
                done = enrich.brief_graded(done, chunks, now)
            done = scored(done, chunks)
        await self._commit(user_id, [done], set())
        _inc(summary, "rescored")
        return done

    # -- Search v2: rebuild progress and the initial build (§25.4.5, §25.8) ---------------------------------

    async def _rebuild_progress(self, user_id: str, state: DriveSyncState, now: datetime, spend: _Spend, *,
                                settled: bool, progressed: bool = False) -> Tuple[DriveSyncState, Dict[str, int]]:
        """The job states per job (the tick line and the Cabinet), the described count (profiles `current`), when
        enrichment last moved (`described_grew_at`, the fan-out's stalled-rebuild alert), and the initial build's end.

        `described_grew_at` is refreshed when the described count grows, when an enrichment run of this tick changed a
        record (`progressed`: a profile, passage progress, a charge or a hold), or when no job is ready (nothing to
        stall). Only ready jobs left untouched for DESCRIBED_STALL_AFTER are a stall: not a quiet day followed by one
        new file, nor long passage-only progress, nor a delete followed by an add (review rv_sv2_p5 MINOR 2).

        The build closes once the first onboarding has completed and no record has a profile or passages job ready or
        parked (D27) — and the index has settled (`settled`: no due work, nothing to enrich). Right after the
        enumeration no file has been read yet, so no record has a job at all: D27's own test would close the build
        before the content it describes was read (RFC §25.4.5, the note on D27)."""
        counts, gaps = await asyncio.gather(self._repo.count_by_job_state(user_id, _ENRICH_KINDS),
                                            self._repo.count_describe_gaps(user_id))
        flat = {f"{job}_{st}": n for job, states in counts.items() for st, n in states.items() if n}
        # the coverage line's counts, read by search from the state (XC-9: no count query per search)
        flat.update({COVERAGE_COUNTERS[gap]: n for gap, n in gaps.items() if n and gap in COVERAGE_COUNTERS})
        described = counts.get(EnrichJob.PROFILE.value, {}).get(JobState.CURRENT.value, 0)
        text_records = sum(n for st, n in counts.get(EnrichJob.PROFILE.value, {}).items()
                           if st != JobState.NONE.value)
        ready = any(counts.get(job.value, {}).get(JobState.READY.value, 0) for job in EnrichJob)
        grew = state.described_grew_at
        if grew is None or described > state.described_count or progressed or not ready:
            grew = now
        if described != state.described_count:
            logger.info(f"[DriveIndex] rebuild progress user={user_id[:8]} described={described}/{text_records}")
        state = replace(state, described_count=described, described_grew_at=grew)
        waiting = any(counts.get(job.value, {}).get(st, 0) for job in EnrichJob
                      for st in (JobState.READY.value, JobState.PARKED.value))
        if spend.build.closed_at is None and state.initial_enumeration_done and settled and not waiting:
            await self._repo.close_initial_build(user_id, now)
            spend.build = replace(spend.build, closed_at=now)
            logger.info(f"[DriveIndex] initial build closed for {user_id[:8]} "
                        f"(${spend.build.spent_usd:.2f} of ${spend.build.allowance_usd:.2f})")
        return state, flat

    # -- transcripts (§27.3, §27.10; plan Task 14) ----------------------------------------------------------------

    def _transcript_ctx(self) -> TranscriptContext:
        """The transcript plan's context for a record write: `enabled` is the tick's transcription (False outside a
        tick); the dollars are the tick's usage document of this month (the plan itself reads only `now` and
        `enabled`; the cap math is the stage's and `_extraction_ready`'s)."""
        transcription = _TICK_TRANSCRIPTION.get()
        spend = _TICK_SPEND.get()
        now = self._clock()
        usage = spend.usage if spend is not None and month_key(spend.now) == month_key(now) else ModelUsageMonth()
        return TranscriptContext(now=now, enabled=bool(transcription is not None and transcription.on),
                                 month_usd=usage.cap_usd(UsageClass.TRANSCRIBE.value),
                                 reserved_usd=max(0.0, usage.transcribe_reserved_usd), cap_usd=self._transcribe_cap)

    async def _transcribe_available(self, user_id: str) -> float:
        """The dollars the transcription cap has left this month (`available_usd`): the effective cap (T-R3, given by the
        composition) less what was collected and what is reserved (clamped at 0) in the usage document of
        `month_key(now)`. The tick's copy when it is that month's (kept current by every reservation through the host),
        else read."""
        now = self._clock()
        spend = _TICK_SPEND.get()
        if spend is not None and month_key(spend.now) == month_key(now):
            usage = spend.usage
        else:
            usage = await self._repo.get_model_usage(user_id, month_key(now))
        return available_usd(TranscriptContext(now=now, enabled=True,
                                               month_usd=usage.cap_usd(UsageClass.TRANSCRIBE.value),
                                               reserved_usd=max(0.0, usage.transcribe_reserved_usd),
                                               cap_usd=self._transcribe_cap))

    async def _drop_transcript_jobs(self, user_id: str, written: Sequence[DriveIndexRecord],
                                    planned: Sequence[DriveIndexRecord], deletes: Set[str]) -> None:
        """The jobs of records a write takes out of the pipeline or resets for a new content version (`leaves_job`),
        and of deleted recordings that were not finished, go through the stage's `drop_jobs` (Task 12 carry): each
        reservation released once, the provider files deleted, the document removed; a job with a window still in a
        batch is left to collection (T-R20). Only while transcription is on: with it off nothing is read."""
        transcription = _TICK_TRANSCRIPTION.get()
        if transcription is None or not transcription.on or transcription.host is None or self._transcripts is None:
            return
        # Re-entrancy (review 14A, Minor 4): this also runs for the stage's own writes (host.commit -> _commit). Those
        # never change a record's eligibility nor its content version within a tick — the stage writes only job states
        # at the key it read — so `leaves_job` is false for them, and `drop_jobs` from inside a stage step never drops
        # (nor releases a second time) a job the step is working; only observation, cascade, sweep and cap writes do.
        leaving = [p.item_id for w, p in zip(written, planned) if leaves_job(w, p)]
        if deletes:
            gone = await self._repo.get_records(user_id, sorted(deletes))
            leaving += sorted(item_id for item_id, rec in gone.items()
                              if rec.kind in policy.TRANSCRIBED_KINDS
                              and rec.transcript_state not in (TRANSCRIPT_NONE, TRANSCRIPT_CURRENT, TRANSCRIPT_FAILED))
        if leaving:
            dropped = await self._transcripts.drop_jobs(user_id, transcription.host, leaving)
            _inc(transcription.host.summary, "transcript_jobs_dropped", dropped)

    async def _reserve_transcribe(self, user_id: str, delta_usd: float, month: str) -> None:
        """A transcription reservation (`delta_usd` > 0) or its release (< 0) in the RESERVATION's month (Δ106, G9),
        through the usage document's server-side increment. Shielded like a spend: once issued it completes whole, and
        a release is never retried (the port's contract). The tick's copy of this month's document follows it."""
        await asyncio.shield(self._repo.reserve_model_usd(user_id, month, UsageClass.TRANSCRIBE.value, delta_usd))
        spend = _TICK_SPEND.get()
        if spend is None:
            return
        spend.reserved_usd += delta_usd
        if month == month_key(spend.now):
            spend.usage = replace(spend.usage,
                                  transcribe_reserved_usd=spend.usage.transcribe_reserved_usd + delta_usd)

    async def _index_transcript(self, user_id: str, rec: DriveIndexRecord, body: str, extracted_by: str,
                                budget: _Budget, summary: Dict[str, int]) -> Optional[DriveIndexRecord]:
        """`TranscriptHost.index_transcript` (§27.8 a): the transcript is the file's text, indexed by the ordinary
        path — one of the tick's TICK_FILE_BUDGET slots under the start budget and the deadline rule (G32), a new
        generation persisted first (§8.1 step 2), then `_read_text` (the gate, the chunker, the chunk cap, embeddings,
        the §7.4 fence) and `_end_run`; `indexed`/`demoted` stamp EXTRACTION_VERSION (else G31 would re-finalize it on
        every pass). The enrichment jobs then profile and hint it as any text (`enrich.planned` in `_commit`).

        None, and the record untouched, when no slot is left: the stage finalizes it in a later step. None too when the
        fence dropped the run, or when the run ended in a transient per-file failure (an embedding timeout or refusal,
        an unexpected error: counted on the record like any run, the stage finalizes again next step); from
        MAX_ATTEMPTS on the `failed` record is returned, so a file the embeddings keep refusing stops. A gate
        rejection is `metadata_only / machine_text` (G20), returned."""
        if not budget.file_allowed(routed=False):
            return None
        now = self._clock()
        budget.files -= 1
        run = generation_started(rec, now)
        await self._commit(user_id, [run], set())   # persisted before the first chunk write (§8.1 step 2)
        mark = _RunMark()
        outcome = ExtractionOutcome(status=ExtractionStatus.TEXT, text=body, extracted_by=extracted_by)
        try:
            final = await self._read_text(user_id, run, outcome, run.kind, now, summary, mark)
        except (LeaseLost, _OptedOut):
            raise   # nothing more may be written; an opt-out purges anyway
        except _RUN_SYSTEMIC as exc:
            logger.warning(f"[DriveIndex] transcript of item {rec.item_id} for {user_id[:8]} ended by "
                           f"{type(exc).__name__}; the attempt is not counted")
            if not mark.committed:
                await self._abandon_run(user_id, run, rec.priority, now)
            raise
        except Exception as exc:
            if mark.committed:
                logger.error(f"[DriveIndex] transcript of item {rec.item_id} for {user_id[:8]} failed after its "
                             f"outcome was committed: {type(exc).__name__}: {exc}", exc_info=True)
                return None
            logger.error(f"[DriveIndex] transcript of item {rec.item_id} for {user_id[:8]} failed: "
                         f"{type(exc).__name__}: {exc}; counted as a per-file failure", exc_info=True)
            final = content_retry(run, Reason.EXTRACT_ERROR, now)
        if final is None:
            return None   # the record changed under the run (§7.4): the stage sees it changed and goes on
        await self._end_run(user_id, run, final, rec.priority, summary, mark, outcome=outcome)
        if final.state is RecordState.PENDING:
            return None   # a transient per-file failure: finalized again in a later step
        return (await self._repo.get_records(user_id, [rec.item_id])).get(rec.item_id)

    async def _transcript_step(self, user_id: str, step: str, run: Awaitable[Dict[str, int]],
                               summary: Dict[str, int], models: _Models) -> Dict[str, int]:
        """One transcript stage call, isolated like an index run (§8.2): the lease, an opt-out and the systemic errors
        end the tick as anywhere else; any other error is logged and costs this tick's transcript step only, never the
        user's index. The tick line takes the stage's window counters; a provider answer ends the agent's backoff
        streak (§25.4.6)."""
        try:
            out = await run
        except (LeaseLost, _OptedOut):
            raise
        except _RUN_SYSTEMIC:
            raise
        except Exception as exc:
            logger.error(f"[DriveIndex] transcript {step} step failed for {user_id[:8]}: {type(exc).__name__}: {exc}",
                         exc_info=True)
            _inc(summary, "transcript_step_errors")
            return {}
        for key in ("windows_extracted", "windows_submitted", "windows_collected"):
            _inc(summary, key, out.get(key, 0))
        answered = any(out.get(k) for k in ("windows_uploaded", "windows_submitted", "windows_collected", "batch_polls"))
        if answered and not out.get("transcript_outages"):
            models.agent_recovered(TRANSCRIBER_AGENT)
        return out

    async def _transcript_cheap(self, user_id: str, budget: _Budget, summary: Dict[str, int], models: _Models) -> None:
        """§27.3: poll, collect, finalize and the orphan sweep, first in the work-off and with no share; a finalize
        starts within the start budget and takes a file slot (G32, `_index_transcript`)."""
        transcription = _TICK_TRANSCRIPTION.get()
        if self._transcripts is None or transcription is None or transcription.host is None:
            return
        stop_at = budget.started + TICK_START_BUDGET.total_seconds()
        await self._transcript_step(user_id, "collect", self._transcripts.cheap_steps(
            user_id, transcription.host, stop_at=stop_at), summary, models)

    async def _transcript_extract(self, user_id: str, budget: _Budget, summary: Dict[str, int],
                                  models: _Models) -> None:
        """§27.3, G16: the extraction step after the due loop, in its own start window (`extraction_stop`). The stage
        re-plans due wake-ups at its start itself (Task 12), so the tick has no wake handling of its own."""
        transcription = _TICK_TRANSCRIPTION.get()
        if self._transcripts is None or transcription is None or transcription.host is None:
            return
        stop_at = budget.extraction_stop()
        if stop_at is None:
            return   # no step could end before the tick deadline: the next tick runs it
        transcription.extract_ran = True
        commits = transcription.commits
        out = await self._transcript_step(user_id, "extract", self._transcripts.extract_step(
            user_id, transcription.host, stop_at=stop_at), summary, models)
        transcription.extract_progressed = (transcription.commits > commits or any(
            out.get(k) for k in ("windows_extracted", "windows_uploaded", "windows_submitted")))

    async def _extraction_ready(self, user_id: str, models: _Models) -> bool:
        """The user has extraction work the stage can do now (G16): the `drive_transcriber` agent is not backed off,
        and a job is in flight (`working`: its money is reserved already) or a recording is `ready` while the month's
        cap has dollars left (`_transcribe_available`). Parked, held and waiting records, open batches and a
        wake-up in the future are not."""
        if not models.agent_available(TRANSCRIBER_AGENT, self._clock()):
            return False
        if await self._repo.transcript_records(user_id, TRANSCRIPT_WORKING, 1):
            return True
        if await self._transcribe_available(user_id) <= 0:
            return False
        return bool(await self._repo.transcript_queue(user_id, 1))

    async def _transcripts_waiting(self, user_id: str, models: _Models) -> bool:
        """The chain continues for transcription only while there is extraction work (`_extraction_ready`) or a due
        wake-up the stage re-plans into it — never for a wake-up in the future or open batches alone (the 10-minute
        fan-out polls them, Δ104) — and only if this tick's extraction step did not run, or moved something: a step
        that found work it could not touch (a file whose old batch must settle first, T-R20) is tried again by the
        fan-out, never by a zero-delay chain."""
        transcription = _TICK_TRANSCRIPTION.get()
        if transcription is None or not transcription.on:
            return False
        if transcription.extract_ran and not transcription.extract_progressed:
            return False
        if await self._extraction_ready(user_id, models):
            return True
        now = self._clock()
        return (models.agent_available(TRANSCRIBER_AGENT, now)
                and bool(await self._repo.transcript_wakeups(user_id, now, 1)))

    # -- version upgrades (§12.3) --------------------------------------------------------------

    async def _upgrade_step(self, user_id: str, state: DriveSyncState, now: datetime, budget: _Budget,
                            summary: Dict[str, int]) -> DriveSyncState:
        """Pages through records while the maintenance share and the tick budget allow, at most to the end of
        one pass: the work a pass schedules runs in the due loop of a later tick, so the next pass waits.

        A pass that left stale records (scheduled, backing off, or parked) is not repeated until due work has run
        or the target versions change: a record stuck in backoff must not cost a full read of the index per tick. A
        pass that left only records held for next month's budget is parked until the month turns (R3 F1-5)."""
        if not self._upgrade_pass:
            return state   # the emergency switch (§25.8, DRIVE_UPGRADE_PASS): no upgrade pass at all
        target = self._target_versions()
        parked = state.upgrade_cursor is None and state.upgrade_parked_month == month_key(now)
        if ((_upgrade_waiting(state) or parked) and dict(state.target_versions) == target
                and state.target_space == self._space):
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
                            upgrade_found_stale=False, upgrade_parked_month="")
        ctx = self._ctx(user_id, state, now)
        page = await self._repo.records_page(user_id, state.upgrade_cursor, _DUE_PAGE)
        writes: Dict[str, DriveIndexRecord] = {}
        deletes: Set[str] = set()
        collects: Set[str] = set()
        found = state.upgrade_found_stale   # a stale record this pass left stale (its work is due, or blocked)
        # a record held for next month seen earlier in this pass (a new pass starts with none)
        held = state.upgrade_cursor is not None and state.upgrade_parked_month == month_key(now)
        pending = self._pending_keys(state, target)
        for rec in page:
            if rec.state is RecordState.DELETING or not self._stale(rec, pending):
                continue
            if "accuracy" in pending and not accuracy_settled(rec):
                # §26.4 "Recomputing": Firestore reads and one write, no download, no model call, no budget; done
                # before the parent and month checks below because none of them can hold it back
                rec = await self._rescore(user_id, rec, now, summary)
                if not self._stale(rec, pending):
                    continue
            if rec.state is RecordState.UNRESOLVED and rec.due_at is None:
                # parked under an unresolved parent, whose rescope re-plans it under the current versions; scheduling
                # it only re-parks it, and it holds no card, so it does not keep the versions from applying
                continue
            if month_parked(rec, now, embedding_space=self._space):
                # its re-read is already scheduled for the month's turn: no parent read, no re-plan (R3 F1-5)
                held = True
                if "keys" in pending and not enrich.keys_settled(rec):
                    # but a keys bump needs no budget: re-planned, its keys work is due now (review rv_sv2_p5
                    # MINOR 9), and the pass verifies again once it ran
                    writes[rec.item_id] = rec
                    found = True
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
            if rec.item_id not in writes and rec.item_id not in deletes and self._enrich_stale(rec, pending):
                writes[rec.item_id] = rec   # re-planned by the commit: its job is ready, or its keys pending (§25.4.5)
            if rec.item_id not in deletes and self._stale(enrich.planned(writes.get(rec.item_id, rec),
                                                                         self._enrich_ctx()), pending):
                found = True
        await self._commit(user_id, list(writes.values()), deletes, collects=collects)
        summary["upgraded"] = summary.get("upgraded", 0) + len(writes)
        month = month_key(now) if held else ""
        if len(page) == _DUE_PAGE:
            return replace(state, upgrade_cursor=page[-1].item_id, upgrade_found_stale=found,
                           upgrade_parked_month=month)
        if found:   # scheduling is not completion: the next pass verifies once due work has run
            return replace(state, upgrade_cursor=None, upgrade_found_stale=True, upgrade_parked_month="")
        if held:    # only month-held records are left: the next pass runs when the month turns (R3 F1-5)
            logger.info(f"[DriveIndex] upgrade pass for {user_id[:8]} parked until the month turns")
            return replace(state, upgrade_cursor=None, upgrade_found_stale=False, upgrade_parked_month=month)
        logger.info(f"[DriveIndex] versions {target} in {self._space} applied for {user_id[:8]}")
        return replace(state, applied_versions=target, applied_space=self._space, upgrade_cursor=None,
                       upgrade_found_stale=False, upgrade_parked_month="")

    # ------------------------------------------------------------------------------------------
    # Purge, continuation, helpers
    # ------------------------------------------------------------------------------------------

    async def _purge(self, user_id: str, state: DriveSyncState, action: AccessAction, enabled: bool,
                     summary: Dict[str, int], cont: _Continuation) -> TickResult:
        """Durable: `purging` is written before the first delete, so a purge always finishes (§7.6)."""
        if not state.purging:
            await self._save_state(replace(state, purging=True))
        budget = _Budget(started=self._monotonic(), monotonic=self._monotonic)
        keep = state.purge_keep_transcripts   # read at the purge's start: a rebuild keeps the store (§27.8 b)
        released = swept = docs_done = False
        while budget.time_left():
            _fence()
            if not released:
                await self._release_transcript_reservations(user_id, summary)
                released = True
            if not docs_done:
                if await self._purge_transcript_documents(user_id, keep, summary):
                    continue
                docs_done = True
            if not swept:   # T-R14: only after the three helpers returned 0, never failing or holding the purge
                swept = await self._sweep_transcript_orphans(user_id, keep, summary)
                if not swept:
                    continue
            hints = await self._repo.delete_user_hints(user_id, RECORD_WRITE_BATCH)   # hints first (§25.2.2)
            if hints:
                summary["purged_hints"] = summary.get("purged_hints", 0) + hints
                continue
            chunks = await self._repo.delete_user_chunks(user_id, RECORD_WRITE_BATCH)   # then chunks (§7.6)
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

    async def _purge_transcript_documents(self, user_id: str, keep: bool, summary: Dict[str, int]) -> bool:
        """One round of the purge's transcript deletes: the user's jobs (with their window results), then batches, then
        the store unless `keep` (a rebuild keeps it). Repository only, with or without the stage. True when it deleted."""
        for key, deleter in (("purged_transcript_jobs", self._repo.delete_user_transcript_jobs),
                             ("purged_transcript_batches", self._repo.delete_user_batches)):
            deleted = await deleter(user_id, RECORD_WRITE_BATCH)
            if deleted:
                summary[key] = summary.get(key, 0) + deleted
                return True
        if not keep:
            stored = await self._repo.delete_user_transcripts(user_id, RECORD_WRITE_BATCH)
            if stored:
                summary["purged_transcripts"] = summary.get("purged_transcripts", 0) + stored
                return True
        return False

    async def _sweep_transcript_orphans(self, user_id: str, keep: bool, summary: Dict[str, int]) -> bool:
        """T-R14: the collection-group sweep of window and part documents, one round. True when it is finished (0 left, or
        its index is missing: logged ERROR naming the index, retried at the next purge or daily pass)."""
        try:
            deleted = await self._repo.delete_user_transcript_orphans(user_id, RECORD_WRITE_BATCH, keep_transcripts=keep)
        except DriveIndexStoreUnavailable as exc:
            logger.error(f"[DriveIndex] transcript orphan sweep of {user_id[:8]} skipped, its collection-group index is "
                         f"unavailable ({exc}); the purge goes on")
            return True
        if deleted:
            summary["purged_transcript_orphans"] = summary.get("purged_transcript_orphans", 0) + deleted
            return False
        return True

    async def _release_transcript_reservations(self, user_id: str, summary: Dict[str, int]) -> None:
        """Before the jobs and batches go, the user's OPEN reservations are settled, each exactly once, in its own month
        (G9; the usage documents survive a purge, §25.4.5). T-R25: money that may still be spent is BOOKED as transcribe
        spend, not released, because the port cannot cancel a provider batch and a running one bills when it finishes
        (Δ106, never under-report): every open batch's reservation, and a job's reservation when any window is
        `submitted`. Only a reservation with nothing in flight (no submitted window) is released. The document is saved
        zeroed BEFORE the spend or release, so a purge that stops in between never settles twice. A job's provider files
        are deleted best-effort when the stage is built (G30). Gate: only a user whose usage documents (this month and the
        two before) show an open reservation is read at all, so a user who never transcribed triggers no transcript query."""
        now = self._clock()
        months = _last_months(now, 3)
        usages = [await self._repo.get_model_usage(user_id, month) for month in months]
        if all(u.transcribe_reserved_usd <= 0 for u in usages):
            return
        item_ids: Set[str] = set()
        for stage_state in (TRANSCRIPT_WORKING, TRANSCRIPT_WAITING, TRANSCRIPT_PARKED, TRANSCRIPT_HELD):
            page = await self._repo.transcript_records(user_id, stage_state, _PURGE_JOB_SCAN)
            if len(page) >= _PURGE_JOB_SCAN:
                logger.warning(f"[DriveIndex] purge of {user_id[:8]}: {stage_state} recordings fill the scan page; "
                               f"reservations past it are not settled")
            item_ids.update(rec.item_id for rec in page)
        batches = await self._repo.get_open_batches(user_id, now + timedelta(days=3650), _PURGE_JOB_SCAN)
        for batch in batches:   # a job whose record is gone is left for collection: its batch names it
            item_ids.update(key.rsplit(":", 1)[0] for key in batch.request_keys)
        released = booked = 0.0
        for item_id in sorted(item_ids):
            try:
                job = await self._repo.get_transcript_job(user_id, item_id)
            except ValueError:
                logger.error(f"[DriveIndex] transcript job of {item_id[:8]} is unreadable in the purge: deleted unsettled")
                continue
            if job is None:
                continue
            if job.reserved_usd > 0:
                if not job.reserved_month:
                    logger.error(f"[DriveIndex] transcript job of {item_id[:8]} holds ${job.reserved_usd:.4f} with no "
                                 f"reservation month: left unsettled")
                else:
                    in_flight = any(w.state == SUBMITTED for w in job.windows)
                    _fence()
                    await self._repo.save_transcript_job(replace(job, reserved_usd=0.0, reserved_month=""))
                    await self._settle_reservation(user_id, job.reserved_usd, job.reserved_month, book=in_flight)
                    booked, released = (booked + job.reserved_usd, released) if in_flight else (
                        booked, released + job.reserved_usd)
            if self._transcripts is not None:
                await self._transcripts.delete_job_files(job.windows)
        for batch in batches:   # open: it may still run and bill
            if batch.reserved_usd <= 0:
                continue
            if not batch.reserved_month:
                logger.error(f"[DriveIndex] open batch holds ${batch.reserved_usd:.4f} with no reservation month: "
                             f"left unsettled")
                continue
            _fence()
            await self._repo.save_batch(replace(batch, reserved_usd=0.0, reserved_month=""))
            await self._settle_reservation(user_id, batch.reserved_usd, batch.reserved_month, book=True)
            booked += batch.reserved_usd
        if released or booked:
            summary["purged_released_usd_milli"] = summary.get("purged_released_usd_milli", 0) + round(released * 1000)
            summary["purged_booked_usd_milli"] = summary.get("purged_booked_usd_milli", 0) + round(booked * 1000)
            logger.info(f"[DriveIndex] purge of {user_id[:8]} released ${released:.4f} and booked ${booked:.4f} "
                        f"(runs in flight) of open transcription reservations")

    async def _settle_reservation(self, user_id: str, usd: float, month: str, *, book: bool) -> None:
        """One zeroed reservation, in its own month: `book` counts it as transcribe spend (the initial build is never
        touched, G8) and takes it out of the open counter; otherwise it is only released."""
        if book:
            await self._spend_usd(user_id, UsageClass.TRANSCRIBE.value, usd, 0.0, 0, month=month)
        await self._reserve_transcribe(user_id, -usd, month)

    async def _has_more(self, user_id: str, state: DriveSyncState, now: datetime, *,
                        upgrade_wrote: bool = False, models: Optional[_Models] = None,
                        idle: Optional[Set[str]] = None) -> bool:
        if not state.round_complete or state.epoch_phase is EpochPhase.SWEEPING:
            return True
        if models is not None and await self._enrichment_waiting(user_id, now, models, idle):
            return True
        if self._upgrade_due(state) and (state.upgrade_cursor is not None or upgrade_wrote):
            # a pass is mid-way, or this tick's pass wrote (re-stamped or scheduled) and the verification pass
            # runs in-chain; a restarted pass that wrote nothing waits for due work or the fan-out (RFC Δ30)
            return True
        if models is not None and await self._transcripts_waiting(user_id, models):
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

    def _stale(self, rec: DriveIndexRecord, pending: Optional[Set[str]] = None) -> bool:
        """A record the bot cannot open (§26.2) is stale only for what a re-plan applies without reading it: its card,
        name keys and embedding space; its content waits for access, like a record under an unresolved parent, and
        never keeps the versions from applying (access regained re-plans it)."""
        if inaccessible(rec):
            return (structure_stale(rec, embedding_space=self._space)
                    or (pending is not None and "keys" in pending and not enrich.keys_settled(rec))
                    or (pending is not None and "accuracy" in pending and not accuracy_settled(rec)))
        return (stale_versions(rec, embedding_space=self._space)
                or extractor_stale(rec, self._extractor_versions)
                or (pending is not None and self._enrich_stale(rec, pending)))

    def _enrich_stale(self, rec: DriveIndexRecord, pending: Set[str]) -> bool:
        """Settled per version key (§25.4.5): a pending `profile` or `passages` key waits for every record's job to be
        current or none, or held/parked for the current version; a pending `keys` key for every record's keys; a
        pending `accuracy` key for every record's score (§26.4)."""
        ctx = self._enrich_ctx()
        return (("profile" in pending and not enrich.profile_settled(rec, ctx))
                or ("passages" in pending and not enrich.passages_settled(rec, ctx))
                or ("keys" in pending and not enrich.keys_settled(rec))
                or ("accuracy" in pending and not accuracy_settled(rec)))

    @staticmethod
    def _pending_keys(state: DriveSyncState, target: Mapping[str, int]) -> Set[str]:
        return {key for key, version in target.items() if state.applied_versions.get(key) != version}

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
        """The opt-in flag, read uncached (§7.1). The account id comes with it: model calls bill to it (§24.9); and
        the user's transcription flag (`drive_transcribe_enabled`, Δ125), which the tick keeps as read at its gate."""
        profile = await self._users.get_user(user_id)
        if profile is not None:
            self._account_ids[user_id] = str(getattr(profile, "account_id", "") or "")
        self._transcribe_flags[user_id] = bool(
            profile is not None and getattr(profile.config, "drive_transcribe_enabled", False))
        return bool(profile is not None and profile.config.drive_indexing_enabled)

    def _transcription_on(self, user_id: str) -> bool:
        """The kill switch AND the user's flag (§27.12-§27.13), with the stage built: only then does any transcript
        step run, any transcript query go out, or a recording count as waiting."""
        return (self._transcribe_enabled and self._transcripts is not None
                and self._transcribe_flags.get(user_id, False))

    @staticmethod
    def _log(user_id: str, end: str, summary: Dict[str, int], budget: _Budget, models: _Models,
             spend: Optional[_Spend] = None) -> None:
        """The tick line (§12.1, §25.7 telemetry): the summary counts, and the dollars per class spent this tick (the
        fallback's share and what the initial build covered), the enrichment time and the rebuild progress. The
        transcript stage adds `windows_extracted`, `windows_submitted`, `windows_collected` and `transcripts` (files made
        `current`) to the counts, `transcribe_usd` (collected dollars booked) to the classes and
        `transcribe_reserved_usd` (this tick's net reservations) after them (§27.10)."""
        elapsed = budget.monotonic() - budget.started
        cost = f" model_cost=${models.cost:.4f}" if models.calls or models.cost else ""
        usd = ""
        if spend is not None and spend.usd:
            usd = " " + " ".join(f"{k}_usd=${v:.4f}" for k, v in sorted(spend.usd.items()) if v)
        if spend is not None and spend.reserved_usd:   # this tick's net transcription reservations (§27.10)
            usd += f" transcribe_reserved_usd=${spend.reserved_usd:+.4f}"
        enriched = f" enrich_s={budget.enrich_s:.1f}" if budget.enrich_s else ""
        logger.info(f"[DriveIndex] tick user={user_id[:8]} end={end} {summary}{cost}{usd}{enriched} "
                    f"duration={elapsed:.1f}s")


class _TickHost:
    """The `TranscriptHost` (drive_transcript_stage) one tick lends the transcript stage for one user (§27.3; plan
    Task 14). Every method is the tick's own rule, so the stage writes nothing the tick would not: the lease fence, a
    record write through `_commit` (the plans, drops_readable), an index run through the ordinary text path under the
    tick's file budget (G32), the usage documents (G8, G9, Δ106) and the per-agent backoff (§25.4.6)."""

    def __init__(self, svc: "DriveIndexingService", user_id: str, budget: _Budget, models: _Models,
                 summary: Dict[str, int], transcription: _Transcription) -> None:
        self._svc = svc
        self._user_id = user_id
        self._budget = budget
        self._models = models
        self.summary = summary
        self._transcription = transcription

    @property
    def account_id(self) -> str:
        return self._svc._account_ids.get(self._user_id, "")

    def fence(self) -> None:
        _fence()

    async def commit(self, records: Sequence[DriveIndexRecord], before: Mapping[str, DriveIndexRecord]) -> None:
        """`_commit`, with the card of a record the stage changed scheduled again: a transcript or stub line changes
        the card text after the run's own scheduling (§27.11), so the due loop embeds it once more."""
        if not records:
            return
        now = self._svc._clock()
        writes = [_card_rescheduled(r, now) for r in records]
        made_current = sum(1 for r in writes if r.transcript_state == TRANSCRIPT_CURRENT
                           and (r.item_id not in before or before[r.item_id].transcript_state != TRANSCRIPT_CURRENT))
        await self._svc._commit(self._user_id, writes, set(), before=before)
        self._transcription.commits += 1
        _inc(self.summary, "transcripts", made_current)

    async def index_transcript(self, rec: DriveIndexRecord, body: str,
                               extracted_by: str) -> Optional[DriveIndexRecord]:
        return await self._svc._index_transcript(self._user_id, rec, body, extracted_by, self._budget, self.summary)

    async def spend(self, cls: str, usd: float, calls: int, month: str) -> None:
        await self._svc._spend_usd(self._user_id, cls, usd, 0.0, calls, month=month)

    async def reserve(self, usd: float, month: str) -> None:
        await self._svc._reserve_transcribe(self._user_id, usd, month)

    async def release(self, usd: float, month: str) -> None:
        await self._svc._reserve_transcribe(self._user_id, -usd, month)

    def agent_available(self, agent: str) -> bool:
        return self._models.agent_available(agent, self._svc._clock())

    async def agent_outage(self, agent: str) -> None:
        """The agent's backoff, written with the tick's state at once (a killed tick keeps it, as the reader's)."""
        self._models.agent_outage(agent, self._svc._clock())
        await self._svc._persist_usage(self._models)


def _transcribing() -> bool:
    """Transcription is on for the user of the running tick (the kill switch and the user's flag, read at its gate)."""
    transcription = _TICK_TRANSCRIPTION.get()
    return transcription is not None and transcription.on


def _card_rescheduled(rec: DriveIndexRecord, now: datetime) -> DriveIndexRecord:
    """A record whose card text no longer matches its stored card is due for it now, if nothing has it due sooner."""
    if card_eligible(rec) and rec.card_hash != card_hash(rec) and (rec.due_at is None or rec.due_at > now):
        return schedule(rec, now, first_seen=not rec.has_card)
    return rec


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


def _verdict(rec: DriveIndexRecord, result: TriageResultV2, now: datetime) -> DriveIndexRecord:
    """A stored verdict with its photo fields (§24.5, §25.4.3); an upgrade's image keeps class 3 for what follows
    (§7.8)."""
    verdict = TriageResult(item_id=result.item_id, verdict=result.verdict, capture=result.capture)
    return _maintenance_kept(triaged(rec, verdict, now, photo=result.photo), rec.priority)


def _with_detected_capture(rec: DriveIndexRecord, kind: str,
                           signals: Optional[CaptureSignals]) -> DriveIndexRecord:
    """A record held for the reader keeps a capture the parser's signals or the camera facet decided (A4, §26.3): a
    scan waiting for the monthly cap is still `scan (detected)` and found by "scans". A model's word never rides a
    hold (it is not known yet), and a held record keeps the capture it had when nothing was detected."""
    capture, source = file_capture(kind, signals=signals, facets=rec.facets)
    if source != CaptureSource.DETECTED.value:
        return rec
    return replace(rec, capture=capture, capture_source=source)


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


def _text_key(text: str) -> str:
    """A chunk's text hash: equal texts embed to the same vector in one space (F1-3)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _keys_of(texts: Sequence[str]) -> List[Tuple[Tuple[str, ...], bool]]:
    """Each chunk's `word_keys` (§25.2.2) and whether the cap cut them; CPU work, run in a worker thread."""
    return [word_keys_capped(t) for t in texts]


def _first_unsettled(start: int, end: int, settled: Set[int]) -> int:
    return next((i for i in range(start, end) if i not in settled), end)


def _passage_progress(started: DriveIndexRecord, next_index: int, now: datetime, *, finished: bool = False,
                      charged: bool = False) -> Callable[[DriveIndexRecord], DriveIndexRecord]:
    """A passage run's progress written onto the record as it is now: the run's generation and version, the next chunk
    to send, a finished run's time, and one charged attempt per run (§25.4.6)."""
    def apply(fresh: DriveIndexRecord) -> DriveIndexRecord:
        out = replace(fresh, passages_generation=started.passages_generation,
                      passages_version=started.passages_version, passages_attempts=started.passages_attempts,
                      passages_next_index=started.passages_next_index)
        out = enrich.passages_advanced(out, next_index, finished=finished, now=now)
        return enrich.passages_charged(out) if charged else out
    return apply


@dataclass(frozen=True)
class _Text:
    """A text verification reads (a folder's README excerpt stands in for chunks, §25.4.1)."""
    text: str


def _hint_text(items: Sequence[HintItem]) -> str:
    """The text a hint document's `hint_vector` embeds: its kept items (§25.2.2)."""
    return "\n".join(item.text for item in items)


def _write_file(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


def _read_head(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(SNIFF_HEAD_BYTES)
