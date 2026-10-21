"""
DriveTranscriptStage — the transcript stage on the indexer, part A: extraction, upload, submission
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.3-§27.6, §27.10; plan Task 12; G12, G18, G20, G31, G35, G39; rulings T-R8,
T-R9, T-R11, T-R15, T-R16, T-R18). Collection, settlement and finalize after a batch are part B (Task 13).

Public interface:
    DriveTranscriptStage(repo, drive, media, batch, runner, *, clock, monotonic, monthly_cap_usd, temp_root)
        async extract_step(user_id, host, *, stop_at) -> {counter: n}     one extraction step of one user's tick
        async drop_jobs(user_id, host, item_ids) -> int                   jobs of records that left the pipeline
    TranscriptHost        what the tick lends the stage (a Protocol; DriveIndexingService implements it, Task 14)
    TranscriptRunner      the runner the stage submits through (a Protocol; DriveTranscriptRunner in composition/)
    TRANSCRIBER_AGENT     the model backoff key of this class (§25.4.6)

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
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Set, Tuple

from ..domain.batch_transcription import BatchRef, FileRef
from ..domain.drive_enrichment import UsageClass
from ..domain.drive_extraction import TRANSCRIBE_LINK, DriveModelUnavailable
from ..domain.drive_index import TRANSCRIBE_VERSION, DriveIndexRecord
from ..domain.drive_index_params import (
    BATCH_POLL_FIRST_S, TRANSCRIBE_EXTRACT_WALL_S, TRANSCRIBE_MAX_SECONDS, TRANSCRIBE_MIN_RUN_WALL_S,
    TRANSCRIBE_USD_PER_MONTH,
)
from ..domain.drive_index_planner import month_key
from ..domain.drive_transcript import (
    JobWindow, MediaProbe, MediaStatus, MediaUnreadable, MediaWindowsOutcome, StoredTranscript, TranscriptBatch,
    TranscriptJob, TranscriptStub, WindowInput, WindowResult, WindowSpec,
)
from ..domain.drive_transcript_job import (
    SUBMITTED, ready_to_submit, request_key, resettled, resubmits, submitted, windows_to_extract, with_uploaded,
)
from ..domain.drive_transcript_plan import (
    FAILED, READY, WAITING, WORKING, Reservation, StartOutcome, TranscriptContext, WindowAction, charge_attempt,
    mark_current, park, release, resume_window, start_file, state_for_job, stub_record, wait_for_copy,
)
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

TRANSCRIBER_AGENT = "drive_transcriber"
EXTRACTED_BY = f"{TRANSCRIBE_LINK}@{TRANSCRIBE_VERSION}"
WINDOW_MIME = "audio/ogg"          # the media child writes Ogg Opus (§27.4, T-R15)
_PAGE = 50                          # records read per list in one step
# A download URL the drive refuses for this file is that file's attempt (the reader's download_error class, §8.2);
# the connection, its throttling and a revoked grant are the tick's (re-raised).
_URL_ERRORS = (DriveItemNotFoundError, DriveAccessDenied, asyncio.TimeoutError, ValueError, OSError)
_DRIVE_SYSTEMIC = (DriveNotConnectedError, DriveThrottled, DriveReauthRequired)


class TranscriptHost(Protocol):
    """What the tick lends the stage, for one user. Every method is the tick's own rule, so the stage writes nothing
    the tick would not."""

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

    async def collect(self, batch: BatchRef, *, account_id: str) -> Mapping[str, WindowResult]:
        """The batch's results (Task 13)."""


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
