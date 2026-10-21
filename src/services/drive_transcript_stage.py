"""
DriveTranscriptStage — the transcript stage on the indexer. Part A: extraction, upload, submission
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.3-§27.6, §27.10; plan Task 12; G12, G18, G20, G31, G35, G39; rulings T-R8,
T-R9, T-R11, T-R15, T-R16, T-R18). Part B: poll, collect, settle, resubmit, finalize, orphan sweep (§27.5-§27.8; plan
Task 13; G9, G20, G32, G33, G37; rulings T-R11, T-R18, T-R20).

Public interface:
    DriveTranscriptStage(repo, drive, media, batch, runner, *, clock, monotonic, monthly_cap_usd, temp_root)
        async extract_step(user_id, host, *, stop_at) -> {counter: n}     one extraction step of one user's tick
        async cheap_steps(user_id, host, *, stop_at) -> {counter: n}      poll, collect, finalize, the orphan sweep
        async drop_jobs(user_id, host, item_ids) -> int                   jobs of records that left the pipeline
    TranscriptHost        what the tick lends the stage (a Protocol; DriveIndexingService implements it, Task 14)
    TranscriptRunner      the runner the stage submits through (a Protocol; DriveTranscriptRunner in composition/)
    TRANSCRIBER_AGENT     the model backoff key of this class (§25.4.6; defined in drive_transcript_plan, re-exported)

Several public classes in one module, by decision: the stage and the two narrow Protocols it is called and served
through (one contract; a service cannot import composition/, REQ-ARCH-22, so the runner's shape is declared here).

The stage holds no tick state of its own: the money, the lease and the record writes are the host's. It is called only
for a user with both the kill switch and the user's flag on (Task 14), so its plan context is always `enabled`.

`extract_step`, in this order, every file step starting only while `monotonic() < stop_at` (the host sets `stop_at` so a
step started before it ends before the tick deadline, G16):
1. in-flight jobs (`working` records, found by state: no queue returns them): a job whose windows are all at the provider
   joins the submission; otherwise its extraction resumes. A `working` record whose job is missing or holds no
   reservation (a crash between writes) starts again, one whose windows are all submitted is set `waiting`; an
   unreadable job document is dropped and charged one attempt.
2. copies waiting on another file (G12; `waiting` with no job of their own): finalized from the store once the other
   lands, back to `ready` when it is no longer in flight.
3. the queue, newest first (`transcript_queue`): a store hit finalizes with no ffmpeg and no model (Δ112; also a
   `current` record whose body or extraction version is no longer the code's, G18/G31: the plan made it `ready`); a
   copy in flight waits; otherwise a fresh download URL (never stored, never logged, Δ98/Δ134), the probe (its duration,
   never the facet), the stubs, the whole file's estimate reserved before any window (G39) or the file parked (Δ106).
   A container without a duration header reserves the worst case and learns its duration from the extraction (G35).
4. one submission per user per step (Δ103): every file whose windows are all uploaded, keys `{item_id}:{index}`; a
   re-purchase of a window collection sent back is reserved on its own (T-R8), its sum carried by the batch (T-R11).

Money (Δ106): the context is built from the usage document of `month_key(now)` and threaded through every decision of
the step, so two files never pass one cap (G39). Every reservation is written before what it pays for; every release
is issued exactly once, right after the document state change it follows (the job's or the batch's), never retried.

Charged attempts (`charge_attempt`, `failed` at MAX_ATTEMPTS, never a stub, Δ100): an unreadable probe or extraction, a
probe or run killed by its limits with nothing produced (a zero-window cut at the full wall, or the memory limit), a
download URL the drive refuses. A run cut by the wall after one or more windows is progress, not an attempt. A charged
attempt gives the file's reservation back; the windows already uploaded are kept for the next try.

An outage of the model provider (`drive_transcriber` backoff) stops extraction and submission for the step; everything
else the tick does goes on. Nothing from a transcript, no download URL and no provider file name or URI is logged.

`cheap_steps` (part B):
1. polls at most BATCH_POLLS_PER_TICK due batches, one poll each (first BATCH_POLL_FIRST_S after submission, set by part
   A; then every BATCH_POLL_EVERY_S, every BATCH_POLL_SLOW_S once older than BATCH_SLOW_AFTER_S). An open batch is always
   saved with its next poll. A provider outage on a poll or a collection backs the agent off and ends the polling; a
   batch the provider no longer knows (a client error) is settled as failed.
2. collects a succeeded batch: the runner parses and bills each answer (Δ105); the stage books the real cost first
   (`spend("transcribe", ...)`) in the month of the reservation that paid for each window (G9: the file's month for a
   first purchase, the batch's for a re-purchase; an answer with text but no priced cost at its window's estimate) and
   marks the batch `booked_at`, so a collection that fails later neither books nor bills again (T-R22); then each file takes its answers under its check's own rule (G37:
   invalid output, a response's own error (T-R18) or a short window → attempts to MAX_ATTEMPTS, then a gap marker; the
   time validator one resubmit, then a gap marker; the interpolation share one resubmit, then accepted with `~` stamps;
   a refusal a gap marker at once; a gone input file (rpc 5/7) re-extracted and re-uploaded once); an answered window's
   provider file is deleted; then the batch is closed and its own reservation (its re-purchases, T-R11) released once.
   A resubmitted window goes back `uploaded`: part A reserves it when it submits it (`resume_window`), never here.
   Answers for a record that is gone, out of the pipeline, finished otherwise or at another content version (G33, T-R20)
   are booked and dropped: the job goes, its reservation released once, its provider files deleted.
3. a batch that failed, expired or was cancelled whole sends its windows back once (a second time: gap markers),
   re-extracting once a window whose provider file will not outlive a batch; then it is closed and released.
4. finalizes a file whose windows are all answered (just collected, or a `waiting` record left by a crash or a full
   tick): stitch, index through `host.index_transcript` (a file slot, G32; only before `stop_at`), store, drop the job
   (its reservation released after every cost was booked), record `current`. No text is the `no_speech` stub (G20).
   Every window a gap marker and none refused is no transcript: the job is dropped and the file charged an attempt
   (T-R23). No slot left ends the finalizing; a run the §7.4 fence dropped goes on to the next file.
5. once a day per user, deletes stored transcripts unused for TRANSCRIPT_ORPHAN_TTL that no record carries (§27.8 b).
"""
from __future__ import annotations

import asyncio
import contextlib
import math
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Set, Tuple

from ..domain.batch_transcription import BatchRef, BatchStatus, FileRef
from ..domain.drive_enrichment import UsageClass
from ..domain.drive_extraction import TRANSCRIBE_LINK, DriveModelUnavailable
from ..domain.drive_index import TRANSCRIBE_VERSION, DriveIndexRecord
from ..domain.drive_index_params import (
    BATCH_POLL_FIRST_S, BATCH_POLLS_PER_TICK, TRANSCRIBE_EXTRACT_WALL_S, TRANSCRIBE_MAX_SECONDS,
    TRANSCRIBE_MIN_RUN_WALL_S, TRANSCRIBE_USD_PER_MONTH, TRANSCRIBE_WINDOW_S, TRANSCRIPT_ORPHAN_TTL,
)
from ..domain.drive_index_planner import month_key
from ..domain.drive_transcript import (
    JobWindow, MediaProbe, MediaStatus, MediaUnreadable, MediaWindowsOutcome, StoredTranscript, TranscriptBatch,
    TranscriptJob, TranscriptStub, WindowInput, WindowOutcome, WindowResult, WindowSpec, WindowTranscript,
)
from ..domain.drive_transcript_job import (
    DONE, SUBMITTED, after_failed_batch, all_done, check_window, file_gone, in_batch, is_repurchase, poll_after,
    ready_to_submit, request_key, resettled, resubmits, split_request_key, stitch_job, submitted, windows_to_extract,
    with_uploaded,
)
from ..domain.drive_transcript_plan import (
    CURRENT, FAILED, NONE, READY, TRANSCRIBER_AGENT, WAITING, WORKING, Reservation, StartOutcome, TranscriptContext,
    WindowAction, WindowCheck, charge_attempt, mark_current, park, release, resume_window, start_file, state_for_job,
    stub_record, wait_for_copy, window_estimate_usd, window_failed,
)
from ..domain.drive_transcript_plan import WindowResult as WindowDecision
from ..domain.drive_transcript_render import TRANSCRIPT_BODY_VERSION, render_body
from ..domain.drive_transcript_windows import content_key, plan_windows
from ..domain.exceptions import LLMAccountError, LLMClientError, LLMError, LLMRateLimitError
from ..domain.user_drive import (
    DriveAccessDenied, DriveItemNotFoundError, DriveNotConnectedError, DriveReauthRequired, DriveThrottled,
)
from ..ports.batch_transcription_port import BatchTranscriptionPort
from ..ports.drive_index_repository import DriveIndexRepository
from ..ports.media_audio_port import MediaAudioPort
from ..ports.user_drive_port import UserDrivePort
from ..utils.logger import logger

EXTRACTED_BY = f"{TRANSCRIBE_LINK}@{TRANSCRIBE_VERSION}"
WINDOW_MIME = "audio/ogg"          # the media child writes Ogg Opus (§27.4, T-R15)
_PAGE = 50                          # records read per list in one step
_ORPHAN_PAGE = 100                  # store entries the daily orphan sweep looks at (§27.8 b)
_DONE, _NO_SLOT, _FENCED = "done", "no_slot", "fenced"   # how one finalize ended (G32, §7.4)
_FILE_GONE_CODES = (5, 7)           # rpc NOT_FOUND / PERMISSION_DENIED on a file-ref request: the input file is gone (T-R18)
# A download URL the drive refuses for this file is that file's attempt (the reader's download_error class, §8.2);
# the connection, its throttling and a revoked grant are the tick's (re-raised).
_URL_ERRORS = (DriveItemNotFoundError, DriveAccessDenied, asyncio.TimeoutError, ValueError, OSError)
_DRIVE_SYSTEMIC = (DriveNotConnectedError, DriveThrottled, DriveReauthRequired)


class TranscriptHost(Protocol):
    """What the tick lends the stage, for one user. Every method is the tick's own rule, so the stage writes nothing
    the tick would not."""

    account_id: str   # the user's account: the runner bills collected windows to it (Δ105)

    def fence(self) -> None:
        """Raise `LeaseLost` once the tick's lease is about to end (§7.1). Called before every store write."""

    async def commit(self, records: Sequence[DriveIndexRecord], before: Mapping[str, DriveIndexRecord]) -> None:
        """Write records the way every tick write goes: fenced, `transcript_plan` and `enrich.planned` applied, and a
        write that drops a readable generation (`drops_readable` against `before`) collects its chunks — a stub
        demotes a record that was indexed before."""

    async def index_transcript(self, rec: DriveIndexRecord, body: str,
                               extracted_by: str) -> Optional[DriveIndexRecord]:
        """Index `body` as the file's text through the ordinary text path (gate, chunker, chunk cap, embeddings and
        the §7.4 fence) and commit it; the record after. None when the run did not start (it takes one of the tick's
        TICK_FILE_BUDGET slots, G32: none left) or its result was dropped (the record changed under the run): the
        record stays as it was and the next step finalizes it. A gate rejection is `machine_text`, never `no_text`
        (G20)."""

    async def spend(self, cls: str, usd: float, calls: int, month: str) -> None:
        """Book collected dollars of `cls` in `month` (G9; collection, Task 13)."""

    async def reserve(self, usd: float, month: str) -> None:
        """Add `usd` to the month's `transcribe_reserved_usd` (Δ106). Called before what it pays for."""

    async def release(self, usd: float, month: str) -> None:
        """Take `usd` off `month`'s open reservations. Issued exactly once, never retried (the port's contract)."""

    def agent_available(self, agent: str) -> bool:
        """The agent type is not backed off (§25.4.6)."""

    async def agent_outage(self, agent: str) -> None:
        """An uncharged outage of the agent type: its backoff, persisted with the tick's state."""


class TranscriptRunner(Protocol):
    async def submit(self, inputs: Sequence[WindowInput], *, display_name: str) -> BatchRef:
        """One batch of the given windows (raises the port's outages and DriveModelUnavailable, all uncharged)."""

    async def collect(self, batch: BatchRef, *, account_id: str, bill: bool = True) -> Mapping[str, WindowResult]:
        """The batch's results (Task 13). `bill=False`: the account is not billed again (a re-collection of a batch
        whose cost was booked already, T-R22); `usd` is still reported."""


class _JobUnwritable(Exception):
    """The store refused a job document (a plain ValueError of its codec): the file is charged, never the tick."""


@dataclass
class _Step:
    """One extraction step's own bookkeeping (not the tick's): the money context threaded through every decision
    (G39), the files ready to submit, and whether an outage ended the step."""
    ctx: TranscriptContext
    stop_at: float
    ready: Dict[str, Tuple[DriveIndexRecord, TranscriptJob]] = field(default_factory=dict)
    outage: bool = False
    charged: Set[str] = field(default_factory=set)   # files charged an attempt this step: not touched again (T-R21)

    def freed(self, usd: float, month: str) -> None:
        self.ctx = release(self.ctx, Reservation(usd=usd, month=month))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _inc(summary: Dict[str, int], key: str, by: int = 1) -> None:
    if by:
        summary[key] = summary.get(key, 0) + by


def _short(item_id: str) -> str:
    return item_id[:8]


class DriveTranscriptStage:
    def __init__(self, repo: DriveIndexRepository, drive: UserDrivePort, media: MediaAudioPort,
                 batch: BatchTranscriptionPort, runner: TranscriptRunner, *,
                 clock: Callable[[], datetime] = _utcnow, monotonic: Callable[[], float] = time.monotonic,
                 monthly_cap_usd: float = TRANSCRIBE_USD_PER_MONTH, temp_root: Optional[str] = None) -> None:
        """`monthly_cap_usd` is `effective_monthly_cap(DRIVE_TRANSCRIBE_USD_CAP)`, decided by the composition
        (T-R3); `temp_root` holds each step's window files, deleted when the step ends."""
        self._repo = repo
        self._drive = drive
        self._media = media
        self._batch = batch
        self._runner = runner
        self._clock = clock
        self._monotonic = monotonic
        self._cap = monthly_cap_usd
        self._temp_root = temp_root
        self._swept: Dict[str, date] = {}   # the orphan sweep's day per user: in process, no sync-state field (§27.8 b)

    # ------------------------------------------------------------------------------------------------------------
    # The step
    # ------------------------------------------------------------------------------------------------------------

    async def extract_step(self, user_id: str, host: TranscriptHost, *, stop_at: float) -> Dict[str, int]:
        summary: Dict[str, int] = {}
        started = self._monotonic()
        if not host.agent_available(TRANSCRIBER_AGENT):
            _inc(summary, "transcript_backed_off")   # §25.4.6: the backoff gates the stage's reads, records stay
            return summary
        step = _Step(ctx=await self._context(user_id, self._clock()), stop_at=stop_at)
        try:
            woken = await self._repo.transcript_wakeups(user_id, step.ctx.now, _PAGE)
            if woken:   # re-planned: a retry, park, cooldown or quiet hold ended (a woken mid-job park is `working`)
                await host.commit(woken, {r.item_id: r for r in woken})
                _inc(summary, "transcript_woken", len(woken))
            for rec in await self._repo.transcript_records(user_id, WORKING, _PAGE):
                if step.outage:
                    break
                await self._guarded(user_id, host, rec, step, summary, waiting=False)
            for rec in await self._repo.transcript_records(user_id, WAITING, _PAGE):
                if step.outage or not self._may_start(step):
                    break
                await self._guarded(user_id, host, rec, step, summary, waiting=True)
            for rec in await self._repo.transcript_queue(user_id, _PAGE):
                if step.outage or not self._may_start(step):
                    break
                await self._guarded(user_id, host, rec, step, summary, waiting=False)
            if not step.outage:
                await self._submit(user_id, host, step, summary)
        finally:
            if summary:
                logger.info(f"[DriveTranscript] extract user={user_id[:8]} {summary} "
                            f"duration={self._monotonic() - started:.1f}s")
        return summary

    async def drop_jobs(self, user_id: str, host: TranscriptHost, item_ids: Sequence[str]) -> int:
        """The jobs of records that left the pipeline (deleted, or `transcript_state` now `none`; the tick calls this
        for its deletes and for writes that took a record out, Task 14): each job's reservation is released once,
        its provider files deleted, the document removed. Returns how many jobs were dropped."""
        dropped = 0
        for item_id in item_ids:
            try:
                job = await self._repo.get_transcript_job(user_id, item_id)
            except ValueError:
                logger.error(f"[DriveTranscript] job document of {_short(item_id)} is unreadable: deleted, "
                             f"its reservation cannot be read")
                host.fence()
                await self._repo.delete_transcript_job(user_id, item_id)
                dropped += 1
                continue
            if job is not None and await self._abandon(user_id, host, job, None):
                dropped += 1
        return dropped

    async def cheap_steps(self, user_id: str, host: TranscriptHost, *, stop_at: float) -> Dict[str, int]:
        """Part B of the step (Task 13), in this order: (1) poll at most BATCH_POLLS_PER_TICK open batches that are due,
        one poll per batch; a succeeded batch is collected and settled, a failed, expired or cancelled one sends its
        windows back once; (2) finalize the files whose windows are all answered, each starting only while
        `monotonic() < stop_at` and taking one of the tick's file slots through `host.index_transcript` (G32);
        (3) once a day, the orphan sweep of the store (§27.8 b). Polling and collection take no share."""
        summary: Dict[str, int] = {}
        started = self._monotonic()
        finished: List[str] = []
        try:
            await self._poll_batches(user_id, host, summary, finished)
            await self._finalize_step(user_id, host, stop_at, summary, finished)
            await self._sweep_orphans(user_id, host, summary)
        finally:
            if summary:
                logger.info(f"[DriveTranscript] collect user={user_id[:8]} {summary} "
                            f"duration={self._monotonic() - started:.1f}s")
        return summary

    # ------------------------------------------------------------------------------------------------------------
    # One file
    # ------------------------------------------------------------------------------------------------------------

    async def _guarded(self, user_id: str, host: TranscriptHost, rec: DriveIndexRecord, step: _Step,
                       summary: Dict[str, int], *, waiting: bool) -> None:
        """A job document the store cannot read or write (plain ValueError of its codec) is this file's problem:
        dropped and charged one attempt, so neither the tick fails nor the file loops forever."""
        if rec.item_id in step.charged:
            return   # charged earlier in this step: its retry hold decides when it is tried again (T-R21)
        try:
            job = await self._repo.get_transcript_job(user_id, rec.item_id)
        except ValueError:
            await self._drop_unreadable(user_id, host, rec, step, summary, "unreadable")
            return
        try:
            if waiting:
                await self._waiting_copy(user_id, host, rec, job, step, summary)
            else:
                await self._file(user_id, host, rec, job, step, summary)
        except _JobUnwritable:
            await self._drop_unreadable(user_id, host, rec, step, summary, "refused by the store")

    async def _drop_unreadable(self, user_id: str, host: TranscriptHost, rec: DriveIndexRecord, step: _Step,
                               summary: Dict[str, int], why: str) -> None:
        logger.error(f"[DriveTranscript] job document of {_short(rec.item_id)} {why}: deleted, the file charged "
                     f"one attempt (an open reservation of it stays counted: the safe direction)")
        host.fence()
        await self._repo.delete_transcript_job(user_id, rec.item_id)
        latest = (await self._repo.get_records(user_id, [rec.item_id])).get(rec.item_id, rec)
        await self._commit(host, charge_attempt(latest, now=self._clock()), latest)
        step.charged.add(rec.item_id)
        _inc(summary, "transcript_jobs_dropped")
        _inc(summary, "transcript_attempts_charged")

    async def _file(self, user_id: str, host: TranscriptHost, rec: DriveIndexRecord, job: Optional[TranscriptJob],
                    step: _Step, summary: Dict[str, int]) -> None:
        key = content_key(rec)[0]
        if job is not None and job.content_key != key:
            # the plan reset the job on a new version: released once — unless a window of it is still in a batch,
            # which collection settles first (G33, T-R20); the file waits for that
            if not await self._abandon(user_id, host, job, step):
                _inc(summary, "transcript_waiting_for_settle")
                return
            _inc(summary, "transcript_jobs_abandoned")
            job = None
        if rec.transcript_state == WORKING:
            if job is not None and job.reserved_usd > 0:
                now = self._clock()
                if ready_to_submit(job, now):
                    step.ready[rec.item_id] = (rec, job)
                elif windows_to_extract(job, now, pass_mode=False):
                    if self._may_start(step):
                        await self._extract(user_id, host, rec, job, step, summary, url=None, probe=None)
                elif state_for_job(job) == WAITING:   # submitted, the record write lost (a crash): repaired
                    await self._commit(host, replace(rec, transcript_state=WAITING), rec)
                return
            logger.warning(f"[DriveTranscript] {_short(rec.item_id)} is working without a reserved job "
                           f"(a crash between writes): started again")
            _inc(summary, "transcript_restarted")
        if self._may_start(step):
            await self._start(user_id, host, rec, job, step, summary)

    async def _waiting_copy(self, user_id: str, host: TranscriptHost, rec: DriveIndexRecord,
                            job: Optional[TranscriptJob], step: _Step, summary: Dict[str, int]) -> None:
        """G12: a `waiting` record with no job of its own waits on another file with the same shared key. A waiting
        record WITH a job has its windows in a batch: collection's (Task 13)."""
        if job is not None:
            return
        key, shared = content_key(rec)
        stored = await self._repo.get_transcript(user_id, key)
        if stored is not None:
            await self._from_store(user_id, host, rec, stored, summary)
            return
        if shared and await self._copy_in_flight(user_id, rec, key):
            return
        await self._commit(host, replace(rec, transcript_state=READY), rec)   # the other is gone: on its own now
        _inc(summary, "transcript_copies_released")

    async def _start(self, user_id: str, host: TranscriptHost, rec: DriveIndexRecord, job: Optional[TranscriptJob],
                     step: _Step, summary: Dict[str, int]) -> None:
        key, shared = content_key(rec)
        stored = await self._repo.get_transcript(user_id, key)
        if stored is not None:
            await self._from_store(user_id, host, rec, stored, summary)
            if job is not None:
                await self._abandon(user_id, host, job, step)
            return
        if shared and await self._copy_in_flight(user_id, rec, key):
            await self._commit(host, wait_for_copy(rec), rec)
            _inc(summary, "transcript_copies_waiting")
            if job is not None:
                await self._abandon(user_id, host, job, step)
            return
        url = await self._url(user_id, host, rec, job, step, summary)
        if url is None:
            return
        probe = await self._probe(user_id, host, rec, job, step, summary, url)
        if probe is None:
            return
        start = start_file(rec, step.ctx, probe)
        step.ctx = start.ctx
        if start.outcome is StartOutcome.STUBBED:
            await self._stubbed(user_id, host, rec, start.rec, job, step, summary)
            return
        if start.outcome is not StartOutcome.STARTED or start.reservation is None:
            out = start.rec
            if start.outcome is StartOutcome.UNREADABLE:
                out = charge_attempt(rec, now=self._clock())   # the same charge, with its retry hold (T-R21)
                step.charged.add(rec.item_id)
                await self._job_after_charge(user_id, host, out, job, step)
                _inc(summary, "transcript_attempts_charged")
            await self._commit(host, out, rec)
            _inc(summary, f"transcript_{start.outcome.value}")
            return
        reservation = start.reservation
        host.fence()
        await host.reserve(reservation.usd, reservation.month)   # written before anything it pays for (G39)
        await self._commit(host, start.rec, rec)
        base = job if job is not None else TranscriptJob(user_id=user_id, item_id=rec.item_id, content_key=key)
        learned = base.duration_s if base.duration_s > 0 else 0.0   # a duration a run measured wins (T-R16)
        duration = learned or (probe.duration_s if probe.duration_known else 0.0)
        new_job = replace(base, content_key=key, content_version=TRANSCRIBE_VERSION, duration_s=duration,
                          reserved_usd=reservation.usd, reserved_month=reservation.month)
        await self._save_job(host, new_job)
        if job is not None and job.reserved_usd > 0:   # a reservation the old document still held: given back once
            await host.release(job.reserved_usd, job.reserved_month)
            step.freed(job.reserved_usd, job.reserved_month)
        _inc(summary, "transcript_started")
        logger.info(f"[DriveTranscript] {_short(rec.item_id)} started: "
                    f"{'duration unknown' if not duration else f'{duration:.0f}s'}, reserved ${reservation.usd:.4f}")
        working = start.rec
        if duration > 0:
            settled = await self._resettle(user_id, host, working, new_job, duration, step, summary)
            if settled is None:
                return
            working, new_job = settled
        await self._extract(user_id, host, working, new_job, step, summary, url=url, probe=probe)

    # ------------------------------------------------------------------------------------------------------------
    # Extraction and upload
    # ------------------------------------------------------------------------------------------------------------

    async def _extract(self, user_id: str, host: TranscriptHost, rec: DriveIndexRecord, job: TranscriptJob,
                       step: _Step, summary: Dict[str, int], *, url: Optional[str],
                       probe: Optional[MediaProbe]) -> None:
        """As many windows as the step's wall allows (§27.4): the first run gets the whole TRANSCRIBE_EXTRACT_WALL_S,
        a further one (the next pass of a file without a duration) starts only with TRANSCRIBE_MIN_RUN_WALL_S left.
        Each window is uploaded as the run returns it and recorded after the provider answered."""
        if url is None or probe is None:
            url = await self._url(user_id, host, rec, job, step, summary)   # fresh for every step (Δ98, Δ134)
            if url is None:
                return
            probe = await self._probe(user_id, host, rec, job, step, summary, url)
            if probe is None:
                return
        if not probe.has_audio:
            await self._charge(user_id, host, rec, job, step, summary, "the probe lost the audio stream")
            return
        pass_mode = job.duration_s <= 0 or not probe.duration_known
        out_dir = tempfile.mkdtemp(prefix="drive-transcript-", dir=self._temp_root)
        try:
            wall_left = float(TRANSCRIBE_EXTRACT_WALL_S)
            first = True
            while not step.outage:
                specs = windows_to_extract(job, self._clock(), pass_mode=pass_mode)
                if not specs or (not first and wall_left < TRANSCRIBE_MIN_RUN_WALL_S):
                    break
                began = self._monotonic()
                try:
                    outcome = await self._media.extract_windows(url, specs, out_dir, wall_s=wall_left,
                                                                container=probe.container,
                                                                duration_known=not pass_mode)
                except MediaUnreadable as exc:
                    await self._charge(user_id, host, rec, job, step, summary, f"unreadable ({exc.detail[:80]})")
                    return
                except ValueError:
                    logger.error(f"[DriveTranscript] {_short(rec.item_id)}: the extractor refused the window slice")
                    await self._charge(user_id, host, rec, job, step, summary, "window slice refused")
                    return
                wall_left -= self._monotonic() - began
                advanced = await self._after_run(user_id, host, rec, job, outcome, step, summary, full_wall=first)
                if advanced is None:
                    return
                rec, job = advanced
                if outcome.status is not MediaStatus.OK:
                    break   # cut by the wall after some windows: resumed at the first missing one next step
                first = False
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)
        if not step.outage and ready_to_submit(job, self._clock()):
            step.ready[rec.item_id] = (rec, job)

    async def _after_run(self, user_id: str, host: TranscriptHost, rec: DriveIndexRecord, job: TranscriptJob,
                         outcome: MediaWindowsOutcome, step: _Step, summary: Dict[str, int], *,
                         full_wall: bool) -> Optional[Tuple[DriveIndexRecord, TranscriptJob]]:
        """One run's outcome: the record and job after it, or None when the file's step is over (charged, stubbed,
        an outage)."""
        produced = list(outcome.windows)
        if not produced and outcome.status is not MediaStatus.OK:
            if outcome.status is MediaStatus.MEMORY or full_wall:
                await self._charge(user_id, host, rec, job, step, summary,
                                   f"{outcome.status.value} before the first window")
            return None   # a later, shorter run cut empty is no attempt: the next step starts with the whole wall
        if outcome.reached_bound:
            await self._too_long(user_id, host, rec, job, step, summary)
            return None
        if outcome.eof:
            if outcome.duration_s > TRANSCRIBE_MAX_SECONDS:
                await self._too_long(user_id, host, rec, job, step, summary)
                return None
            settled = await self._resettle(user_id, host, rec, job, outcome.duration_s, step, summary)
            if settled is None:
                return None
            rec, job = settled
            planned = {s.index for s in plan_windows(outcome.duration_s)}
            produced = [(spec, path) for spec, path in produced if spec.index in planned]
        elif not produced:
            await self._charge(user_id, host, rec, job, step, summary, "a run produced nothing")
            return None
        _inc(summary, "windows_extracted", len(produced))
        uploaded = await self._upload(user_id, host, rec, job, produced, step, summary)
        if uploaded is None:
            return None
        return rec, uploaded

    async def _upload(self, user_id: str, host: TranscriptHost, rec: DriveIndexRecord, job: TranscriptJob,
                      produced: Sequence[Tuple[WindowSpec, str]], step: _Step,
                      summary: Dict[str, int]) -> Optional[TranscriptJob]:
        for spec, path in produced:
            try:
                ref = await self._batch.upload_audio(path, WINDOW_MIME)
            except (LLMAccountError, LLMRateLimitError) as exc:
                await self._outage(host, step, summary, f"upload: {type(exc).__name__}")
                return None
            except LLMClientError as exc:
                await self._charge(user_id, host, rec, job, step, summary, f"upload refused ({type(exc).__name__})")
                return None
            except LLMError as exc:
                await self._outage(host, step, summary, f"upload: {type(exc).__name__}")
                return None
            except OSError as exc:
                await self._charge(user_id, host, rec, job, step, summary, f"window file ({type(exc).__name__})")
                return None
            job = with_uploaded(job, spec, ref.name, ref.expires_at)
            await self._save_job(host, job)
            _inc(summary, "windows_uploaded")
            with contextlib.suppress(OSError):
                os.unlink(path)
        return job

    async def _resettle(self, user_id: str, host: TranscriptHost, rec: DriveIndexRecord, job: TranscriptJob,
                        duration_s: float, step: _Step,
                        summary: Dict[str, int]) -> Optional[Tuple[DriveIndexRecord, TranscriptJob]]:
        """The real duration is known (`eof`, or a duration measured by an earlier run): the plan, the windows and the
        reservation follow it (T-R15, T-R16). The job is saved first; the dollars over the real estimate are released
        after it, once."""
        settled = resettled(job, duration_s)
        changed = (settled.job.duration_s != job.duration_s or settled.release_usd > 0 or settled.dropped)
        if not changed:
            return rec, job
        await self._save_job(host, settled.job)
        if settled.release_usd > 0:
            await host.release(settled.release_usd, job.reserved_month)
            step.freed(settled.release_usd, job.reserved_month)
            _inc(summary, "transcript_resettled")
        await self._delete_files(settled.dropped)
        out = rec
        if rec.transcript_seconds != int(duration_s):
            out = replace(rec, transcript_seconds=int(duration_s))
            await self._commit(host, out, rec)
        return out, settled.job

    # ------------------------------------------------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------------------------------------------------

    async def _submit(self, user_id: str, host: TranscriptHost, step: _Step, summary: Dict[str, int]) -> None:
        """One batch for every file whose windows are all uploaded (Δ103). A window collection sent back is a new
        purchase: reserved here (`resume_window`, T-R8), its sum carried by the batch document (T-R11); when it does
        not fit, its file is parked mid-job (`park(..., resubmit=True)`) and goes in no batch."""
        if not step.ready:
            return
        now = self._clock()
        inputs: List[WindowInput] = []
        files: List[Tuple[DriveIndexRecord, TranscriptJob]] = []
        held = 0.0
        for item_id in sorted(step.ready):
            rec, job = step.ready[item_id]
            ctx = step.ctx
            parked = False
            file_held = 0.0
            for window in resubmits(job):
                result = resume_window(window, ctx)
                if result.action is not WindowAction.RESUBMIT or result.reservation is None:
                    parked = True
                    break
                ctx = result.ctx
                file_held += result.reservation.usd
            if parked:
                await self._commit(host, park(rec, step.ctx, resubmit=True), rec)
                _inc(summary, "transcript_parked_mid_job")
                continue
            step.ctx = ctx
            held += file_held
            files.append((rec, job))
            inputs += [WindowInput(key=request_key(job.item_id, w.spec.index), window=w.spec, file_name=w.file_ref,
                                   file_mime=WINDOW_MIME) for w in job.windows if w.state == "uploaded"]
        if not inputs:
            return
        month = month_key(step.ctx.now)   # the month every reservation of this step was decided in (G9)
        host.fence()
        if held > 0:
            await host.reserve(held, month)
        try:
            ref = await self._runner.submit(inputs, display_name=f"{user_id}:{now.isoformat()}")
        except (LLMError, DriveModelUnavailable) as exc:
            if held > 0:
                await host.release(held, month)   # nothing was bought: given back once, before anything else
                step.freed(held, month)
            await self._outage(host, step, summary, f"submit: {type(exc).__name__}")
            return
        host.fence()
        batch = TranscriptBatch(name=ref.name, user_id=user_id, request_keys=tuple(i.key for i in inputs),
                                submitted_at=now, created_at=now, reserved_usd=held,
                                reserved_month=month if held > 0 else "", state="submitted",
                                next_poll_at=now + timedelta(seconds=BATCH_POLL_FIRST_S))
        if not await self._repo.create_batch(batch):   # first: the purchase is on record before the jobs name it
            logger.error(f"[DriveTranscript] batch document for user={user_id[:8]} already existed")
        records: List[DriveIndexRecord] = []
        before: Dict[str, DriveIndexRecord] = {}
        for rec, job in files:
            done = submitted(job, ref.name)
            await self._save_job(host, done)
            records.append(replace(rec, transcript_state=state_for_job(done)))
            before[rec.item_id] = rec
        await host.commit(records, before)
        _inc(summary, "windows_submitted", len(inputs))
        _inc(summary, "transcript_batches")
        logger.info(f"[DriveTranscript] submitted user={user_id[:8]} files={len(files)} windows={len(inputs)} "
                    f"reserved_resubmits=${held:.4f}")

    # ------------------------------------------------------------------------------------------------------------
    # Outcomes
    # ------------------------------------------------------------------------------------------------------------

    async def _from_store(self, user_id: str, host: TranscriptHost, rec: DriveIndexRecord, stored: StoredTranscript,
                          summary: Dict[str, int]) -> None:
        """Δ112, G18, G31: finalize from the stored result, embeddings only. A stub, or a transcript with no text (G20:
        a media `no_text` is always a stub), is a stub record."""
        now = self._clock()
        transcript = stored.transcript
        body = render_body(transcript) if transcript is not None else ""
        if transcript is None or not body.strip():
            stub = stored.stub if stored.stub in {s.value for s in TranscriptStub} else TranscriptStub.NO_SPEECH.value
            # the store keeps no duration for a stub: a re-finalize keeps the one the record already states
            seconds = rec.transcript_seconds if rec.transcript_key == stored.content_key else 0
            await self._commit(host, mark_current(rec, now, seconds=seconds, speakers=0, languages=(), stub=stub), rec)
        else:
            indexed = await host.index_transcript(rec, body, EXTRACTED_BY)
            if indexed is None:
                _inc(summary, "transcript_finalize_dropped")
                return
            done = mark_current(indexed, now, seconds=int(round(transcript.duration_s)), speakers=transcript.speakers,
                                languages=transcript.language_codes)
            await self._commit(host, done, indexed)
        host.fence()
        await self._repo.touch_transcript(user_id, stored.content_key, now)
        _inc(summary, "transcript_store_hits")

    async def _stubbed(self, user_id: str, host: TranscriptHost, before: DriveIndexRecord, stub_rec: DriveIndexRecord,
                       job: Optional[TranscriptJob], step: _Step, summary: Dict[str, int]) -> None:
        """A stub decided with no model call (`no_audio`, `too_long`): stored under the content key, so a copy finds
        it, and the record `current`; a readable generation it drops is collected by the host (`before`)."""
        host.fence()
        await self._repo.put_transcript(user_id, StoredTranscript(
            content_key=stub_rec.transcript_key, transcript=None, stub=stub_rec.transcript_stub,
            transcribe_version=TRANSCRIBE_VERSION, render_version=TRANSCRIPT_BODY_VERSION,
            last_used_at=self._clock()))
        await self._commit(host, stub_rec, before)
        if job is not None:
            await self._abandon(user_id, host, job, step)
        _inc(summary, f"transcript_stub_{stub_rec.transcript_stub}")

    async def _too_long(self, user_id: str, host: TranscriptHost, rec: DriveIndexRecord, job: TranscriptJob,
                        step: _Step, summary: Dict[str, int]) -> None:
        """A file without a duration header that runs past TRANSCRIBE_MAX_SECONDS (`reached_bound`, T-R15): the
        `too_long` stub; nothing was bought, the reservation goes back."""
        await self._stubbed(user_id, host, rec, stub_record(rec, TranscriptStub.TOO_LONG, self._clock()), job, step,
                            summary)

    async def _charge(self, user_id: str, host: TranscriptHost, rec: DriveIndexRecord, job: Optional[TranscriptJob],
                      step: _Step, summary: Dict[str, int], why: str) -> None:
        """A file-level failed attempt (Δ100): `failed` at MAX_ATTEMPTS with no stub, the job dropped; below it the
        reservation goes back and the uploaded windows stay for the next try."""
        out = charge_attempt(rec, now=self._clock())
        step.charged.add(rec.item_id)
        await self._job_after_charge(user_id, host, out, job, step)
        await self._commit(host, out, rec)
        _inc(summary, "transcript_attempts_charged")
        if out.transcript_state == FAILED:
            _inc(summary, "transcript_failed")
        logger.info(f"[DriveTranscript] {_short(rec.item_id)} attempt {out.transcript_attempts}: {why}")

    async def _job_after_charge(self, user_id: str, host: TranscriptHost, charged: DriveIndexRecord,
                                job: Optional[TranscriptJob], step: _Step) -> None:
        """A `failed` file's job is dropped (files deleted, reservation released); below MAX_ATTEMPTS it keeps its
        windows and gives its reservation back (the record is `ready`, and a `ready` file holds no money)."""
        if job is None:
            return
        if charged.transcript_state == FAILED:
            await self._abandon(user_id, host, job, step)
        else:
            await self._release_job(user_id, host, job, step)

    async def _release_job(self, user_id: str, host: TranscriptHost, job: Optional[TranscriptJob],
                           step: Optional[_Step]) -> None:
        """The job keeps its windows and gives its reservation back: the document first, the release once after it."""
        if job is None or job.reserved_usd <= 0:
            return
        usd, month = job.reserved_usd, job.reserved_month
        await self._save_job(host, replace(job, reserved_usd=0.0, reserved_month=""))
        await host.release(usd, month)
        if step is not None:
            step.freed(usd, month)

    async def _abandon(self, user_id: str, host: TranscriptHost, job: TranscriptJob, step: Optional[_Step]) -> bool:
        """The job ends without a transcript (a new content version, a stub, a failure, a record gone): the document
        goes first, its reservation is released once after it, and its provider files are deleted (§27.5). True when
        it was dropped.

        A job with a window still in a batch is left untouched (T-R20): its batch may still run and bill, and its
        reservation and files are collection's to settle (G33, Task 13), which drops the job afterwards. Releasing or
        deleting here would break the hard cap and fail the running batch; a new job would overwrite its keys."""
        if any(w.state == SUBMITTED for w in job.windows):
            logger.info(f"[DriveTranscript] job of {_short(job.item_id)} has windows in a batch: left for collection")
            return False
        host.fence()
        await self._repo.delete_transcript_job(user_id, job.item_id)
        if job.reserved_usd > 0:
            await host.release(job.reserved_usd, job.reserved_month)
            if step is not None:
                step.freed(job.reserved_usd, job.reserved_month)
        await self._delete_files(job.windows)
        return True

    async def _outage(self, host: TranscriptHost, step: _Step, summary: Dict[str, int], what: str) -> None:
        logger.warning(f"[DriveTranscript] provider outage ({what}): extraction and submission stop for this step")
        await host.agent_outage(TRANSCRIBER_AGENT)
        step.outage = True
        _inc(summary, "transcript_outages")

    # ------------------------------------------------------------------------------------------------------------
    # Part B: poll, collect, settle (Task 13)
    # ------------------------------------------------------------------------------------------------------------

    async def _poll_batches(self, user_id: str, host: TranscriptHost, summary: Dict[str, int],
                            finished: List[str]) -> None:
        """One poll per due batch, at most BATCH_POLLS_PER_TICK (Δ104). A provider outage ends the polling of this
        step and backs the agent off; the batch stays as it was and is polled again next step."""
        now = self._clock()
        try:
            due = await self._repo.get_open_batches(user_id, now, BATCH_POLLS_PER_TICK)
        except ValueError as exc:
            logger.error(f"[DriveTranscript] a batch document of user={user_id[:8]} is unreadable: "
                         f"{type(exc).__name__}; polling skipped this step")
            _inc(summary, "batch_unreadable")
            return
        for batch in due:
            if not batch.request_keys:   # nothing to collect, ever: closed, never rescheduled forever
                logger.error(f"[DriveTranscript] a batch of user={user_id[:8]} has no request keys: closed as failed")
                await self._close_batch(host, batch, "failed", now, summary)
                continue
            ref = BatchRef(name=batch.name, keys=batch.request_keys)   # keys: collect needs them (Task 10)
            try:
                status = await self._batch.poll(ref)
            except LLMAccountError as exc:   # a 403 on the batch is the account's, not the batch's (Task 10)
                await self._collect_outage(host, summary, f"poll: {type(exc).__name__}")
                return
            except LLMClientError as exc:   # the provider no longer knows the batch: nothing can be collected
                logger.error(f"[DriveTranscript] a batch of user={user_id[:8]} is gone at the provider "
                               f"({type(exc).__name__}, status={exc.http_status}): settled as failed")
                status = BatchStatus.FAILED
            except LLMError as exc:
                await self._collect_outage(host, summary, f"poll: {type(exc).__name__}")
                return
            _inc(summary, "batch_polls")
            if status in (BatchStatus.PENDING, BatchStatus.RUNNING):
                await self._reschedule(host, batch, now)
            elif status is BatchStatus.SUCCEEDED:
                if not await self._collect(user_id, host, batch, ref, summary, finished):
                    return
            else:
                await self._failed_batch(user_id, host, batch, status, summary, finished)

    async def _reschedule(self, host: TranscriptHost, batch: TranscriptBatch, now: datetime) -> None:
        """An open batch is always saved with its next poll (never None: it would never be polled again)."""
        host.fence()
        await self._repo.save_batch(replace(batch, last_polled_at=now, next_poll_at=poll_after(batch, now)))

    async def _collect(self, user_id: str, host: TranscriptHost, batch: TranscriptBatch, ref: BatchRef,
                       summary: Dict[str, int], finished: List[str]) -> bool:
        """A succeeded batch: the runner parses and bills every answer; the real cost is booked first, in the month of
        the reservation that paid for each window (G9); then each file takes its answers (G37, G33); then the batch is
        closed and its own reservation released, once, after that state change (Δ106, T-R11). False on an outage."""
        now = self._clock()
        booked = batch.booked_at is not None   # a collection that failed after booking: neither billed nor booked again
        try:
            results = await self._runner.collect(ref, account_id=host.account_id, bill=not booked)
        except LLMError as exc:
            await self._collect_outage(host, summary, f"collect: {type(exc).__name__}")
            return False
        except ValueError as exc:   # not succeeded when collected (a race with the poll): polled again
            logger.error(f"[DriveTranscript] collect refused for a batch of user={user_id[:8]}: {type(exc).__name__}")
            await self._reschedule(host, batch, now)
            return True
        answers = self._by_item(batch, results)
        files = await self._load_files(user_id, list(answers))
        if not booked:
            batch = await self._book(host, batch, answers, files, now)
        for item_id, by_index in answers.items():
            rec, job, unreadable = files[item_id]
            try:
                await self._take_answers(user_id, host, batch, rec, job, unreadable, item_id, by_index, now, summary,
                                         finished)
            except _JobUnwritable:
                logger.error(f"[DriveTranscript] job of {_short(item_id)} refused by the store at collection: "
                             f"its answers are booked, the document is stage A's to drop")
        _inc(summary, "windows_collected", sum(len(v) for v in answers.values()))
        await self._close_batch(host, batch, "done", now, summary)
        return True

    def _by_item(self, batch: TranscriptBatch,
                 results: Mapping[str, WindowResult]) -> Dict[str, Dict[int, WindowResult]]:
        """Every request key of the batch, by file and window; a key with no answer is a failed one (the port fills
        it in already: a missing answer is never a skipped window)."""
        out: Dict[str, Dict[int, WindowResult]] = {}
        for key in batch.request_keys:
            try:
                item_id, index = split_request_key(key)
            except ValueError:
                logger.error("[DriveTranscript] a batch carries a request key that is not one: ignored")
                continue
            out.setdefault(item_id, {})[index] = results.get(key) or WindowResult(key=key,
                                                                                  outcome=WindowOutcome.ERROR)
        return out

    async def _load_files(self, user_id: str, item_ids: Sequence[str]
                          ) -> Dict[str, Tuple[Optional[DriveIndexRecord], Optional[TranscriptJob], bool]]:
        """(record or None, job or None, job unreadable) per file."""
        records = await self._repo.get_records(user_id, list(item_ids)) if item_ids else {}
        out: Dict[str, Tuple[Optional[DriveIndexRecord], Optional[TranscriptJob], bool]] = {}
        for item_id in item_ids:
            try:
                out[item_id] = (records.get(item_id), await self._repo.get_transcript_job(user_id, item_id), False)
            except ValueError:
                out[item_id] = (records.get(item_id), None, True)
        return out

    async def _book(self, host: TranscriptHost, batch: TranscriptBatch, answers: Mapping[str, Mapping[int, WindowResult]],
                    files: Mapping[str, Tuple[Optional[DriveIndexRecord], Optional[TranscriptJob], bool]],
                    now: datetime) -> TranscriptBatch:
        """The real cost, booked before any reservation is released (Δ106), in the month of the reservation that paid
        for it (G9): a first purchase in its file's month (the job's pair, G39), a re-purchase in its batch's (T-R11).
        Booked for every answer, a dropped one included (G33): the provider billed it. An answer that carried text but
        no priced cost (usage missing or unpriced, nothing billed) is booked at its window's estimate, so the hard cap
        never sees a free window.

        T-R22: one fence, the spends, then the batch's `booked_at`, with no fence in between, so anything that raises
        later in the collection (a store write, the host's commit, the fence) re-collects without booking or billing
        again. Residual: a process killed between the spends and the `booked_at` write (or inside one of them) books
        once more on the re-collection (the safe direction for the cap)."""
        fallback = batch.reserved_month or month_key(now)
        buckets: Dict[str, Tuple[float, int]] = {}
        for item_id, by_index in answers.items():
            job = files[item_id][1]
            carried = {w.spec.index: w for w in in_batch(job, batch.name)} if job is not None else {}
            for index, result in by_index.items():
                window = carried.get(index)
                month = fallback
                if job is not None and window is not None and not is_repurchase(window) and job.reserved_month:
                    month = job.reserved_month
                cost = result.usd
                if (cost <= 0 and not result.billed
                        and result.outcome in (WindowOutcome.OK, WindowOutcome.INVALID)):   # text came, no price
                    cost = window_estimate_usd(window or JobWindow(WindowSpec(index, 0.0, float(TRANSCRIBE_WINDOW_S))))
                usd, calls = buckets.get(month, (0.0, 0))
                buckets[month] = (usd + cost, calls + 1)
        host.fence()
        for month, (usd, calls) in sorted(buckets.items()):
            await host.spend(UsageClass.TRANSCRIBE.value, usd, calls, month)
        out = replace(batch, booked_at=now)
        await self._repo.save_batch(out)
        return out

    async def _take_answers(self, user_id: str, host: TranscriptHost, batch: TranscriptBatch,
                            rec: Optional[DriveIndexRecord], job: Optional[TranscriptJob], unreadable: bool,
                            item_id: str, by_index: Mapping[int, WindowResult], now: datetime,
                            summary: Dict[str, int], finished: List[str]) -> None:
        if unreadable:
            logger.error(f"[DriveTranscript] job document of {_short(item_id)} is unreadable at collection: its "
                         f"answers are booked and dropped")
            if rec is None:
                host.fence()
                await self._repo.delete_transcript_job(user_id, item_id)
            return   # a record still there: stage A drops the document and charges the file (`_guarded`)
        if job is None:
            logger.warning(f"[DriveTranscript] answers for {_short(item_id)} have no job: booked and dropped")
            return
        carried = in_batch(job, batch.name)
        if not carried:
            return   # applied before (a collection repeated after a crash): booked again, the safe direction
        if rec is None or not self._live(rec, job):
            await self._settle_dropped(user_id, host, job, carried, summary)
            return
        saves: List[Tuple[int, WindowTranscript]] = []
        retired: List[JobWindow] = []
        new_windows: Dict[int, JobWindow] = {}
        for window in carried:
            result = by_index.get(window.spec.index) or WindowResult(key="", outcome=WindowOutcome.ERROR)
            new, transcript = self._answer(window, result, now, summary)
            if transcript is not None:
                saves.append((window.spec.index, transcript))
            if new.state == DONE:
                retired.append(window)   # answered or a gap: its provider file goes (§27.5)
                new = replace(new, file_ref="")
            new_windows[window.spec.index] = new
        for index, transcript in saves:   # the answers first: a job that says `done` always has its answer stored
            host.fence()
            await self._repo.save_window_result(user_id, item_id, index, transcript)
        windows = tuple(new_windows.get(w.spec.index, w) for w in job.windows)
        still = any(w.state == SUBMITTED for w in windows)
        out_job = replace(job, windows=windows, batch_name=job.batch_name if still else "", transcribed_at=now)
        await self._save_job(host, out_job)
        await self._delete_files(retired)
        await self._after_windows(host, rec, out_job, finished)

    def _answer(self, window: JobWindow, result: WindowResult, now: datetime,
                summary: Dict[str, int]) -> Tuple[JobWindow, Optional[WindowTranscript]]:
        """One window's answer under its check's own retry rule (G37, `window_failed`): (the window after, the
        transcript to store when it is accepted). A resubmit goes back `uploaded` with its file and request key, so
        stage A reserves it at the next submission (T-R8); the reservation `window_failed` computes is never made here."""
        if result.outcome is WindowOutcome.OK and result.transcript is not None:
            check = check_window(window.spec, result.transcript)
            if check is None:
                _inc(summary, "windows_done")
                return replace(window, state=DONE, batch_name=""), result.transcript
            decided = self._window_failed(window, check, now, summary, result.finish_reason)
            if decided.action is WindowAction.ACCEPT:
                _inc(summary, "windows_accepted_short" if check is WindowCheck.COVERAGE
                     else "windows_accepted_interpolated")
                return replace(decided.window, batch_name=""), result.transcript
            return replace(decided.window, batch_name=""), None
        if result.outcome is WindowOutcome.REFUSED:
            refused = self._window_failed(window, WindowCheck.REFUSED, now, summary).window
            return replace(refused, batch_name="", refused=True), None
        # invalid output, a response's own error, a missing answer: the window's charged attempt (T-R18, §27.5)
        decided = self._window_failed(window, WindowCheck.INVALID, now, summary)
        out = replace(decided.window, batch_name="")
        if (result.outcome is WindowOutcome.ERROR and result.code in _FILE_GONE_CODES
                and decided.action is not WindowAction.GAP):
            if window.reupload_used:   # its one re-upload is spent: the same request cannot succeed
                out = replace(out, state=DONE, gap=True)
                _inc(summary, "windows_gap")
            else:
                out = file_gone(out)
                _inc(summary, "windows_reupload")
        return out, None

    def _window_failed(self, window: JobWindow, check: WindowCheck, now: datetime,
                       summary: Dict[str, int], finish_reason: str = "") -> WindowDecision:
        """`window_failed` on a context that always fits: only the window it returns is used. The re-purchase is
        reserved by stage A when it submits the window (`resume_window`, T-R8), never a second time here."""
        decided = window_failed(window, check, TranscriptContext(now=now, enabled=True, month_usd=0.0,
                                                                 reserved_usd=0.0, cap_usd=math.inf),
                                finish_reason)
        _inc(summary, f"windows_{check.value}")
        if decided.action is WindowAction.GAP:
            _inc(summary, "windows_gap")
        return decided

    async def _after_windows(self, host: TranscriptHost, rec: DriveIndexRecord, job: TranscriptJob,
                             finished: List[str]) -> None:
        """The record follows its job: `working` while a window is to redo, `waiting` once every window is in a batch
        or answered. A record the stage did not leave in either (parked, held: a hold of the same key) keeps its state
        until its job is answered in full, then waits to be finalized: the audio is bought, finalizing costs nothing."""
        done = all_done(job)
        if done:
            finished.append(job.item_id)
        if rec.transcript_state in (WORKING, WAITING):
            state = state_for_job(job)
        elif done:
            state = WAITING
        else:
            return
        if state != rec.transcript_state or rec.transcript_key != job.content_key:
            await self._commit(host, replace(rec, transcript_state=state, transcript_key=job.content_key), rec)

    def _live(self, rec: Optional[DriveIndexRecord], job: TranscriptJob) -> bool:
        """The job still belongs to its record (G33, T-R20): the record exists, is in the pipeline, has not failed or
        finished otherwise (a copy finalized from the store), and is at the job's content version."""
        return (rec is not None and rec.transcript_state not in (NONE, FAILED, CURRENT)
                and content_key(rec)[0] == job.content_key)

    async def _settle_dropped(self, user_id: str, host: TranscriptHost, job: TranscriptJob,
                              carried: Sequence[JobWindow], summary: Dict[str, int]) -> None:
        """G33, T-R20: answers for a record that is gone, out of the pipeline, finished otherwise or at another content
        version. Their cost is booked already; the windows are dropped, and once no window of the job is in a batch the
        job goes: the document first, its reservation released once, its provider files deleted (`_abandon`)."""
        indexes = {w.spec.index for w in carried}
        windows = tuple(replace(w, state=DONE, gap=True, batch_name="") if w.spec.index in indexes else w
                        for w in job.windows)
        out = replace(job, windows=windows)
        _inc(summary, "transcript_results_dropped")
        if any(w.state == SUBMITTED for w in windows):   # another batch still carries some: it settles the rest
            await self._save_job(host, replace(out, windows=tuple(replace(w, file_ref="") if w.spec.index in indexes
                                                                  else w for w in windows)))
            await self._delete_files(carried)
            return
        await self._abandon(user_id, host, out, None)
        logger.info(f"[DriveTranscript] answers for {_short(job.item_id)} dropped (record gone or changed): "
                    f"settled, job deleted")

    async def _failed_batch(self, user_id: str, host: TranscriptHost, batch: TranscriptBatch, status: BatchStatus,
                            summary: Dict[str, int], finished: List[str]) -> None:
        """A batch that failed, expired or was cancelled whole (§27.5, Δ104): nothing to collect or book. Its windows
        go back once (`after_failed_batch`: re-extracted once when the provider file will not outlive a batch, a gap
        marker the second time), the batch is closed and its own reservation released once after that."""
        now = self._clock()
        item_ids: List[str] = []
        for key in batch.request_keys:
            with contextlib.suppress(ValueError):
                item_id = split_request_key(key)[0]
                if item_id not in item_ids:
                    item_ids.append(item_id)
        files = await self._load_files(user_id, item_ids)
        for item_id in item_ids:
            rec, job, unreadable = files[item_id]
            if unreadable or job is None:
                if unreadable and rec is None:
                    host.fence()
                    await self._repo.delete_transcript_job(user_id, item_id)
                continue
            carried = in_batch(job, batch.name)
            if not carried:
                continue
            try:
                if rec is None or not self._live(rec, job):
                    await self._settle_dropped(user_id, host, job, carried, summary)
                    continue
                retired: List[JobWindow] = []
                new_windows: Dict[int, JobWindow] = {}
                for window in carried:
                    new = after_failed_batch(window, now)
                    if new.file_ref != window.file_ref or new.state == DONE:
                        retired.append(window)
                        new = replace(new, file_ref="")
                    new_windows[window.spec.index] = new
                    _inc(summary, "windows_gap" if new.state == DONE else "windows_sent_back")
                windows = tuple(new_windows.get(w.spec.index, w) for w in job.windows)
                still = any(w.state == SUBMITTED for w in windows)
                out_job = replace(job, windows=windows, batch_name=job.batch_name if still else "")
                await self._save_job(host, out_job)
                await self._delete_files(retired)
                await self._after_windows(host, rec, out_job, finished)
            except _JobUnwritable:
                logger.error(f"[DriveTranscript] job of {_short(item_id)} refused by the store after a failed batch")
        logger.warning(f"[DriveTranscript] a batch of user={user_id[:8]} ended {status.value}: "
                       f"{len(batch.request_keys)} windows sent back")
        _inc(summary, f"batch_{status.value}")
        await self._close_batch(host, batch, "failed", now, summary)

    async def _close_batch(self, host: TranscriptHost, batch: TranscriptBatch, state: str, now: datetime,
                           summary: Dict[str, int]) -> None:
        """The batch's state change first (closed: never polled again), then its own reservation released, once, in its
        month (T-R11, Δ106). Never retried: a closed batch is not returned by the poll query."""
        host.fence()
        await self._repo.save_batch(replace(batch, state=state, last_polled_at=now, next_poll_at=None))
        if batch.reserved_usd > 0:
            await host.release(batch.reserved_usd, batch.reserved_month)
        _inc(summary, "batches_closed")

    async def _collect_outage(self, host: TranscriptHost, summary: Dict[str, int], what: str) -> None:
        logger.warning(f"[DriveTranscript] provider outage ({what}): polling stops for this step")
        await host.agent_outage(TRANSCRIBER_AGENT)
        _inc(summary, "transcript_outages")

    # ------------------------------------------------------------------------------------------------------------
    # Part B: finalize and the orphan sweep (Task 13)
    # ------------------------------------------------------------------------------------------------------------

    async def _finalize_step(self, user_id: str, host: TranscriptHost, stop_at: float, summary: Dict[str, int],
                             finished: Sequence[str]) -> None:
        """Files answered in full: those collected now first, then every `waiting` record whose job is complete (a
        finalize a crash or a full tick left, which is therefore idempotent). Each starts only before `stop_at`; no file
        slot left ends this step's finalizing, a run the §7.4 fence dropped goes on to the next file."""
        candidates: Dict[str, None] = dict.fromkeys(finished)
        for rec in await self._repo.transcript_records(user_id, WAITING, _PAGE):
            candidates.setdefault(rec.item_id, None)
        for item_id in candidates:
            if self._monotonic() >= stop_at:
                _inc(summary, "transcript_finalize_deferred")
                return
            try:
                job = await self._repo.get_transcript_job(user_id, item_id)
            except ValueError:
                continue   # stage A drops the document and charges the file
            if job is None or not all_done(job):
                continue
            found = (await self._repo.get_records(user_id, [item_id])).get(item_id)
            try:
                if found is None or not self._live(found, job):
                    if await self._abandon(user_id, host, job, None):
                        _inc(summary, "transcript_results_dropped")
                    continue
                if await self._finalize(user_id, host, found, job, summary) == _NO_SLOT:
                    return
            except _JobUnwritable:
                logger.error(f"[DriveTranscript] job of {_short(item_id)} refused by the store at finalize")

    async def _finalize(self, user_id: str, host: TranscriptHost, rec: DriveIndexRecord, job: TranscriptJob,
                        summary: Dict[str, int]) -> str:
        """Stitch (§27.6), index the body through the host (gate, chunks, embeddings, the §7.4 fence; one of the tick's
        file slots, G32), then store the structured result (sharded by the store), drop the job (its reservation
        released once, after every window's cost was booked at collection) and write the record `current`. A result
        with no text is the `no_speech` stub, never a plain `no_text` (G20), and has no chunks.

        The order makes a crash harmless: before the job is dropped, the next step finalizes again from the job; after
        it, the record is `waiting` with no job and stage A finalizes it from the store (Δ112).

        T-R23: a file whose every window is a gap marker and none of them a refusal (retries spent, batches failed) is
        not a transcript: the job is dropped (its cost booked at collection, its reservation released once) and the
        file charged an attempt (`charge_attempt`, the T-R21 retry hold, `failed` at MAX_ATTEMPTS with no stub). With a
        refusal among them it is final, `truncated` (§27.5: never dropped). Returns "done", "no_slot" or "fenced"."""
        now = self._clock()
        results = await self._repo.get_window_results(user_id, job.item_id)
        missing = [w.spec.index for w in job.windows if not w.gap and w.spec.index not in results]
        if missing:
            logger.error(f"[DriveTranscript] {_short(job.item_id)}: {len(missing)} answered windows have no stored "
                         f"answer: stitched as gap markers")
        if (all(w.gap or w.spec.index in missing for w in job.windows)
                and not any(w.refused for w in job.windows)):
            await self._abandon(user_id, host, job, None)
            charged = charge_attempt(rec, now=now)
            await self._commit(host, charged, rec)
            _inc(summary, "transcript_all_gaps_charged")
            if charged.transcript_state == FAILED:
                _inc(summary, "transcript_failed")
            logger.info(f"[DriveTranscript] {_short(job.item_id)}: every window is a gap marker with no refusal: "
                        f"attempt {charged.transcript_attempts} charged, nothing finalized")
            return _DONE
        stitched = stitch_job(job, results)
        body = render_body(stitched)
        if not body.strip():
            stored = StoredTranscript(content_key=job.content_key, transcript=None, stub=TranscriptStub.NO_SPEECH.value,
                                      transcribe_version=TRANSCRIBE_VERSION, render_version=TRANSCRIPT_BODY_VERSION,
                                      last_used_at=now)
            before = rec
            done = mark_current(rec, now, seconds=int(round(stitched.duration_s)), speakers=0, languages=(),
                                stub=TranscriptStub.NO_SPEECH.value)
        else:
            indexed = await host.index_transcript(rec, body, EXTRACTED_BY)
            if indexed is None:   # no file slot left (the record unchanged), or the §7.4 fence dropped the run
                latest = (await self._repo.get_records(user_id, [job.item_id])).get(job.item_id)
                if latest == rec:
                    _inc(summary, "transcript_finalize_no_slot")
                    return _NO_SLOT
                _inc(summary, "transcript_finalize_fenced")
                if not self._live(latest, job) and await self._abandon(user_id, host, job, None):
                    _inc(summary, "transcript_results_dropped")   # another version now: the job is settled at once
                return _FENCED
            stored = StoredTranscript(content_key=job.content_key, transcript=stitched,
                                      transcribe_version=TRANSCRIBE_VERSION, render_version=TRANSCRIPT_BODY_VERSION,
                                      last_used_at=now)
            before = indexed
            done = mark_current(indexed, now, seconds=int(round(stitched.duration_s)), speakers=stitched.speakers,
                                languages=stitched.language_codes)
        host.fence()
        await self._repo.put_transcript(user_id, stored)
        await self._abandon(user_id, host, job, None)
        await self._commit(host, done, before)
        interpolated = sum(1 for s in stitched.segments if s.interpolated)
        _inc(summary, "transcript_stub_no_speech" if stored.transcript is None else "transcripts_finalized")
        logger.info(f"[DriveTranscript] {_short(job.item_id)} finalized: {int(stitched.duration_s)}s, "
                    f"speakers={stitched.speakers}, segments={len(stitched.segments)}, interpolated={interpolated}, "
                    f"uncertain_seam={stitched.uncertain_seam}, truncated={stitched.truncated}")
        return _DONE

    async def _sweep_orphans(self, user_id: str, host: TranscriptHost, summary: Dict[str, int]) -> None:
        """§27.8 b, once a day per user: a stored transcript no record has used for TRANSCRIPT_ORPHAN_TTL is deleted.
        One a record still carries (`transcript_key`, any state) is kept and touched instead: a re-finalize (G18, G31)
        reads it with no model call, so sweeping it would re-buy the audio (Δ122); the touch also keeps it out of the
        next sweep's page, which is ordered by `last_used_at`."""
        now = self._clock()
        if self._swept.get(user_id) == now.date():
            return
        keys = await self._repo.orphan_transcripts(user_id, now - TRANSCRIPT_ORPHAN_TTL, _ORPHAN_PAGE)
        for key in keys:
            host.fence()
            if await self._repo.transcript_key_in_use(user_id, key):
                await self._repo.touch_transcript(user_id, key, now)
                _inc(summary, "transcript_orphans_kept")
            else:
                await self._repo.delete_transcript(user_id, key)
                _inc(summary, "transcript_orphans_deleted")
        self._swept[user_id] = now.date()

    # ------------------------------------------------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------------------------------------------------

    async def _context(self, user_id: str, now: datetime) -> TranscriptContext:
        """The hard cap's context from THIS month's usage document (Δ106, G9): what the cap paid, and the open
        reservations clamped at 0 (the port's reader rule)."""
        usage = await self._repo.get_model_usage(user_id, month_key(now))
        return TranscriptContext(now=now, enabled=True, month_usd=usage.cap_usd(UsageClass.TRANSCRIBE.value),
                                 reserved_usd=max(0.0, usage.transcribe_reserved_usd), cap_usd=self._cap)

    def _may_start(self, step: _Step) -> bool:
        return self._monotonic() < step.stop_at

    async def _url(self, user_id: str, host: TranscriptHost, rec: DriveIndexRecord, job: Optional[TranscriptJob],
                   step: _Step, summary: Dict[str, int]) -> Optional[str]:
        try:
            return await self._drive.download_url(user_id, rec.item_id)
        except _DRIVE_SYSTEMIC:
            raise
        except _URL_ERRORS as exc:
            await self._charge(user_id, host, rec, job, step, summary, f"download URL ({type(exc).__name__})")
            return None

    async def _probe(self, user_id: str, host: TranscriptHost, rec: DriveIndexRecord, job: Optional[TranscriptJob],
                     step: _Step, summary: Dict[str, int], url: str) -> Optional[MediaProbe]:
        try:
            outcome = await self._media.probe(url)
        except MediaUnreadable as exc:
            await self._charge(user_id, host, rec, job, step, summary, f"probe unreadable ({exc.detail[:80]})")
            return None
        if outcome.status is not MediaStatus.OK or outcome.probe is None:
            await self._charge(user_id, host, rec, job, step, summary, f"probe {outcome.status.value}")
            return None
        return outcome.probe

    async def _copy_in_flight(self, user_id: str, rec: DriveIndexRecord, key: str) -> bool:
        """G12: another file of the user with this shared key has a job and is `working` or `waiting` at it."""
        try:
            jobs = await self._repo.jobs_by_content_key(user_id, key)
        except ValueError:
            logger.error(f"[DriveTranscript] a job document with the key of {_short(rec.item_id)} is unreadable: "
                         f"no copy assumed in flight")
            return False
        others = [j.item_id for j in jobs if j.item_id != rec.item_id]
        if not others:
            return False
        recs = await self._repo.get_records(user_id, others)
        return any(r.transcript_state in (WORKING, WAITING) and r.transcript_key == key for r in recs.values())

    async def _save_job(self, host: TranscriptHost, job: TranscriptJob) -> None:
        host.fence()
        try:
            await self._repo.save_transcript_job(job)
        except ValueError as exc:
            logger.error(f"[DriveTranscript] the store refused the job of {_short(job.item_id)}: {type(exc).__name__}")
            raise _JobUnwritable() from exc

    async def _commit(self, host: TranscriptHost, rec: DriveIndexRecord, before: DriveIndexRecord) -> None:
        await host.commit([rec], {before.item_id: before})

    async def delete_job_files(self, windows: Sequence[JobWindow]) -> None:
        """The purge's best-effort delete of a job's provider files (G30: whatever stays expires within 48 h)."""
        await self._delete_files(windows)

    async def _delete_files(self, windows: Sequence[JobWindow]) -> None:
        """Best effort (§27.5): a file that cannot be deleted expires at the provider within 48 h."""
        now = self._clock()
        for window in windows:
            if not window.file_ref or (window.files_expire_at is not None and window.files_expire_at <= now):
                continue
            try:
                await self._batch.delete_file(FileRef(name=window.file_ref, uri="",
                                                      expires_at=window.files_expire_at or now))
            except Exception as exc:   # never the file's failure: it expires by itself
                logger.warning(f"[DriveTranscript] provider file not deleted: {type(exc).__name__}")
