"""
The transcript job state machine and the budget decisions (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.3, §27.5,
§27.10; plan Part C G8, G9, G12, G21, G37, G39). Pure: no I/O, no clock (time comes in `TranscriptContext.now`), no store.

`transcript_plan(rec, ctx)` runs on every record write next to `enrich.planned` (Task 14 wires it into `_commit`). It
reads the record only: a store hit is found by the stage (Task 12 step 1), which then writes `current`. The states
`working` and `waiting` are written by the stage as the job advances (`start_file`, `state_for_job`); the plan keeps them
while the content key they were made for still stands.

Public interface:
    TranscriptContext(now, enabled, month_usd, reserved_usd, cap_usd)       what the decisions are made with
    effective_monthly_cap(raw) -> float                                     `min(20, DRIVE_TRANSCRIBE_USD_CAP)`, T-R3
    estimate_usd(seconds) / available_usd(ctx) / fits(estimate, ctx)         the whole file's estimate against the cap
    reserve_if_fits(estimate, ctx) -> (Reservation | None, ctx);  release(ctx, reservation) -> ctx      (G39)
    transcript_plan(rec, ctx) -> rec                                        the state table below
    reset_on_new_version(rec) -> rec                                        a new content version clears the job (D42)
    start_file(rec, ctx, probe) -> FileStart                                the stage's step after the probe (Task 12 step 4)
    stub_from_probe(probe) / stub_record(rec, stub, seconds) / mark_current(rec, now, ...)
    charge_attempt(rec) -> rec                                              a file-level failed attempt (`failed` at MAX_ATTEMPTS)
    state_for_job(job) -> "working" | "waiting"
    window_failed(window, check) -> (window, WindowAction)                  the per-check retry rules (G37)
    queue_blocked(now, backoff_until) -> bool                               a model backoff gates the stage's queue read

The state table, first match wins (`DriveIndexRecord.transcript_state`, plain strings, G21):
    none      not a TRANSCRIBE-admitted kind, not `normal` scope, a folder, not resolved or being deleted, a never-download
              reason (dot name, secret, OS artefact, remote item, package, malware, dense tree, kind not yet admitted),
              no content version, or inaccessible (§26.2).
    current   a transcript or stub exists at this content key, TRANSCRIBE_VERSION and TRANSCRIPT_BODY_VERSION (the stage
              wrote `current`; the plan never looks in the store). A stale body or transcribe version is not current: the
              record is `ready` and the stage finds the store hit.
    current   (too_large) `size_bytes > TRANSCRIBE_MAX_BYTES`: the plan writes the stub itself from metadata (record
              `metadata_only / too_large`, no probe, no model). Only while `enabled`: a switched-off feature writes no stub.
    failed    `transcript_attempts >= MAX_ATTEMPTS` for this content version (ffmpeg cannot read it): NO stub (Δ100).
    held      a hold for this content version, with `transcript_wake_at` set: `cooldown` (an edit within
              TRANSCRIBE_REREAD_COOLDOWN of `model_read_at`, the last transcription) or `quiet` (dirty and not
              `quiet_over`, a recording still uploading).
    parked    the whole file's estimate does not fit the month's cap after pending reservations; `transcript_wake_at` is
              the next month's start, never cut (Δ106). Entered by `start_file` after the probe (an audio file has no
              duration before it); the plan only keeps it until the wake-up.
    waiting   windows submitted, batches open (written by the stage).
    working   some windows extracted or uploaded, not all submitted (written by the stage); the file's estimate is
              reserved from the moment it fit, before its first window (G39).
    ready     due: the queue predicate (`transcript_state == "ready"`, newest `effective_day` first).
A model backoff is NOT a record state (§25.4.6, `JobState`'s docstring): the `drive_transcriber` backoff gates the stage's
queue read, records stay `ready`, an outage writes no record (G21).

`transcript_hold_version` tags everything that belongs to one content version of the file: `"<TRANSCRIBE_VERSION>:<content
key>"`. A tag that is not the current one means the content changed: attempts, holds and the wake-up are cleared (D42).

Several public classes in one module, by decision: one contract (the job's decisions) with its value types, as
`drive_transcript` and `drive_enrichment` group theirs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import FrozenSet, Optional, Tuple

from src.domain.drive_index import TRANSCRIBE_VERSION, DriveIndexRecord, Reason, RecordState, Scope, inaccessible
from src.domain.drive_index_params import (
    MAX_ATTEMPTS, TRANSCRIBE_HOUR_USD_ESTIMATE, TRANSCRIBE_MAX_BYTES, TRANSCRIBE_MAX_SECONDS,
    TRANSCRIBE_REREAD_COOLDOWN, TRANSCRIBE_USD_PER_MONTH,
)
from src.domain.drive_index_planner import _content_due_at, quiet_over
from src.domain.drive_index_policy import Admission, admission
from src.domain.drive_transcript import JobWindow, MediaProbe, TranscriptJob, TranscriptStub
from src.domain.drive_transcript_render import TRANSCRIPT_BODY_VERSION
from src.domain.drive_transcript_windows import content_key

NONE, READY, WORKING, WAITING = "none", "ready", "working", "waiting"
CURRENT, HELD, PARKED, FAILED = "current", "held", "parked", "failed"
HOLD_COOLDOWN, HOLD_QUIET = "cooldown", "quiet"

# Reasons a media record carries when the policy never downloads it, or has not admitted it yet (policy 3 re-classifies
# the latter to `pending`, §27.3). TOO_LARGE, NO_TEXT and the extraction errors are this feature's own outcomes.
_NEVER_TRANSCRIBED: FrozenSet[Reason] = frozenset({
    Reason.FOLDER, Reason.KIND_NOT_ADMITTED, Reason.OPAQUE_ROOT, Reason.DOT_NAME, Reason.SECRET_NAME,
    Reason.OS_ARTIFACT, Reason.REMOTE_ITEM, Reason.PACKAGE, Reason.MALWARE, Reason.DENSE_TREE,
})
_STUB_RECORD_REASON = {TranscriptStub.TOO_LARGE: Reason.TOO_LARGE, TranscriptStub.TOO_LONG: Reason.TOO_LARGE,
                       TranscriptStub.NO_AUDIO: Reason.NO_TEXT, TranscriptStub.NO_SPEECH: Reason.NO_TEXT}


@dataclass(frozen=True)
class TranscriptContext:
    """What the decisions are made with. `month_usd` is the month's collected `transcribe` dollars, **without any
    initial-build share** (G8: the build never pays for transcription, so there is no initial field here); `reserved_usd`
    the pending reservations (Δ106); `cap_usd` the effective monthly cap (`effective_monthly_cap`). `enabled` is the kill
    switch AND the user's flag, decided by the caller."""
    now: datetime
    enabled: bool
    month_usd: float
    reserved_usd: float
    cap_usd: float = TRANSCRIBE_USD_PER_MONTH


@dataclass(frozen=True)
class Reservation:
    """The file's estimate, held from the moment it fit. `month` ("YYYY-MM", UTC) is the month it was made in: released
    there, and the real cost is booked there too, so a batch collected after a month turn cannot hand the next month
    dollars it never reserved (G9)."""
    usd: float
    month: str


class StartOutcome(str, Enum):
    STARTED = "started"      # fits: reserved, the record is `working`
    PARKED = "parked"        # does not fit: the whole file waits for the next month
    STUBBED = "stubbed"      # decided from the probe, no model call, nothing reserved
    DISABLED = "disabled"    # kill switch or per-user flag off: nothing changes


@dataclass(frozen=True)
class FileStart:
    rec: DriveIndexRecord
    outcome: StartOutcome
    reservation: Optional[Reservation]
    ctx: TranscriptContext


class WindowCheck(str, Enum):
    """Why a collected window answer is not usable (G37)."""
    INVALID = "invalid"              # request error, invalid JSON, an unparseable stamp (`parse_stamp` ValueError)
    COVERAGE = "coverage"            # the answer stops short of the window (Δ129)
    TIME = "time"                    # the time validator rejected the stamps
    INTERPOLATION = "interpolation"  # more than INTERPOLATED_MAX_SHARE of the stamps are computed
    REFUSED = "refused"              # a content-policy block


class WindowAction(str, Enum):
    RESUBMIT = "resubmit"
    GAP = "gap"          # the window is done as a gap marker; the transcript is `truncated`
    ACCEPT = "accept"    # stitched as it is, `~` stamps and all


# -- budget -------------------------------------------------------------------------------------------------------

def effective_monthly_cap(raw: Optional[str]) -> float:
    """T-R3: `min(TRANSCRIBE_USD_PER_MONTH, DRIVE_TRANSCRIBE_USD_CAP)`. The optional knob can only lower the cap:
    empty or absent is the default, a larger value is clamped, and a value that is not a finite non-negative number is
    treated as absent (a typo must never crash the indexer, nor silently remove the cap)."""
    if raw is None or not raw.strip():
        return TRANSCRIBE_USD_PER_MONTH
    try:
        value = float(raw.strip())
    except ValueError:
        return TRANSCRIBE_USD_PER_MONTH
    if not math.isfinite(value) or value < 0:
        return TRANSCRIBE_USD_PER_MONTH
    return min(TRANSCRIBE_USD_PER_MONTH, value)


def estimate_usd(seconds: float) -> float:
    return max(0.0, seconds) * TRANSCRIBE_HOUR_USD_ESTIMATE / 3600.0


def available_usd(ctx: TranscriptContext) -> float:
    """What the month's cap has left after what was collected and what is reserved (no initial-build allowance)."""
    return max(0.0, ctx.cap_usd - ctx.month_usd - ctx.reserved_usd)


def fits(estimate: float, ctx: TranscriptContext) -> bool:
    return estimate <= available_usd(ctx) + 1e-9


def _month(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m")


def _next_month(now: datetime) -> datetime:
    utc = now.astimezone(timezone.utc)
    year, month = (utc.year + 1, 1) if utc.month == 12 else (utc.year, utc.month + 1)
    return datetime(year, month, 1, tzinfo=timezone.utc)


def reserve_if_fits(estimate: float, ctx: TranscriptContext) -> Tuple[Optional[Reservation], TranscriptContext]:
    """G39: the fit check and the reservation are ONE step. The returned context carries the reservation, so the next
    file checked in the same tick sees it; a second file can never pass a check the cap only holds for one."""
    if not fits(estimate, ctx):
        return None, ctx
    return Reservation(usd=estimate, month=_month(ctx.now)), replace(ctx, reserved_usd=ctx.reserved_usd + estimate)


def release(ctx: TranscriptContext, reservation: Reservation) -> TranscriptContext:
    """Park, failure and abandon give the reservation back."""
    return replace(ctx, reserved_usd=max(0.0, ctx.reserved_usd - reservation.usd))


def queue_blocked(now: datetime, backoff_until: Optional[datetime]) -> bool:
    """The `drive_transcriber` model backoff (§25.4.6) gates the stage's queue read; it is never a record state."""
    return backoff_until is not None and now < backoff_until


# -- keys and versions --------------------------------------------------------------------------------------------

def hold_key(rec: DriveIndexRecord) -> str:
    """The tag of everything that belongs to this content version of the file."""
    return f"{TRANSCRIBE_VERSION}:{content_key(rec)[0]}"


def reset_on_new_version(rec: DriveIndexRecord) -> DriveIndexRecord:
    """D42: a new content version (or transcribe version) clears attempts, holds, the wake-up and a stale stub, and
    re-keys: the file re-transcribes, or hits the cache through its content key. A job in a state made for another key
    goes back to `ready` (the stage drops the job document whose key no longer matches)."""
    key = content_key(rec)[0]
    tag_stale = rec.transcript_hold_version not in ("", hold_key(rec))
    key_stale = rec.transcript_state in (WORKING, WAITING, CURRENT) and rec.transcript_key != key
    if not (tag_stale or key_stale):
        return rec
    out = replace(rec, transcript_attempts=0, transcript_hold=None, transcript_hold_version="",
                  transcript_wake_at=None)
    if key_stale or rec.transcript_state in (HELD, PARKED, FAILED):
        out = replace(out, transcript_state=READY)
    if key_stale:
        out = replace(out, transcript_stub="")
    return out


# -- the plan -----------------------------------------------------------------------------------------------------

def _eligible(rec: DriveIndexRecord) -> bool:
    return (admission(rec.kind) is Admission.TRANSCRIBE and rec.scope is Scope.NORMAL and not rec.is_folder
            and not inaccessible(rec) and rec.state not in (RecordState.UNRESOLVED, RecordState.DELETING)
            and bool(rec.content_version) and rec.reason not in _NEVER_TRANSCRIBED)


def _is_current(rec: DriveIndexRecord, key: str) -> bool:
    return (rec.transcript_state == CURRENT and rec.transcript_key == key
            and rec.transcript_version == TRANSCRIBE_VERSION
            and rec.transcript_body_version == TRANSCRIPT_BODY_VERSION)


def _settle(rec: DriveIndexRecord, state: str, *, hold: Optional[str] = None,
            wake: Optional[datetime] = None) -> DriveIndexRecord:
    """The record in `state`, with the hold and wake-up it names and nothing else left over. What belongs to this content
    version (attempts, a hold, a park) carries its tag."""
    tagged = rec.transcript_attempts > 0 or hold is not None or state == PARKED
    return replace(rec, transcript_state=state, transcript_hold=hold, transcript_wake_at=wake,
                   transcript_hold_version=hold_key(rec) if tagged else "")


def transcript_plan(rec: DriveIndexRecord, ctx: TranscriptContext) -> DriveIndexRecord:
    if not _eligible(rec):
        return replace(rec, transcript_state=NONE, transcript_hold=None, transcript_wake_at=None)
    rec = reset_on_new_version(rec)
    key = content_key(rec)[0]
    if _is_current(rec, key):
        return _settle(rec, CURRENT)
    if ctx.enabled and rec.size_bytes > TRANSCRIBE_MAX_BYTES:
        return stub_record(rec, TranscriptStub.TOO_LARGE)
    if rec.transcript_attempts >= MAX_ATTEMPTS:
        return _settle(rec, FAILED)
    # an earlier transcript of another content version exists and is younger than the cooldown: an edited recording is
    # re-bought at most once per TRANSCRIBE_REREAD_COOLDOWN
    if rec.transcript_key not in ("", key) and rec.model_read_at is not None:
        until = rec.model_read_at + TRANSCRIBE_REREAD_COOLDOWN
        if ctx.now < until:
            return _settle(rec, HELD, hold=HOLD_COOLDOWN, wake=until)
    if not quiet_over(rec, ctx.now):
        return _settle(rec, HELD, hold=HOLD_QUIET, wake=_content_due_at(rec, ctx.now))
    if (rec.transcript_state == PARKED and rec.transcript_wake_at is not None
            and rec.transcript_wake_at > ctx.now):
        return _settle(rec, PARKED, wake=rec.transcript_wake_at)
    if rec.transcript_state in (WORKING, WAITING) and rec.transcript_key == key:
        return _settle(rec, rec.transcript_state)
    return _settle(rec, READY)


# -- what the stage writes ----------------------------------------------------------------------------------------

def stub_from_probe(probe: MediaProbe) -> Optional[TranscriptStub]:
    """Decided from what ffprobe says (§27.4): no audio stream, or longer than TRANSCRIBE_MAX_SECONDS. No model call."""
    if not probe.has_audio:
        return TranscriptStub.NO_AUDIO
    if probe.duration_s > TRANSCRIBE_MAX_SECONDS:
        return TranscriptStub.TOO_LONG
    return None


def stub_record(rec: DriveIndexRecord, stub: TranscriptStub, seconds: int = 0) -> DriveIndexRecord:
    """A stub is a result: `current` at this content key, the record `metadata_only` with the stub's reason."""
    return replace(rec, state=RecordState.METADATA_ONLY, reason=_STUB_RECORD_REASON[stub], transcript_state=CURRENT,
                   transcript_key=content_key(rec)[0], transcript_version=TRANSCRIBE_VERSION,
                   transcript_body_version=TRANSCRIPT_BODY_VERSION, transcript_stub=stub.value,
                   transcript_seconds=seconds, transcript_hold=None, transcript_wake_at=None,
                   transcript_hold_version=hold_key(rec) if rec.transcript_attempts > 0 else "")


def start_file(rec: DriveIndexRecord, ctx: TranscriptContext, probe: MediaProbe) -> FileStart:
    """The stage's step after the probe (Task 12 step 4): a stub from the probe, or the whole file's estimate reserved in
    the same step as the fit check (G39) and the record `working`, or the whole file parked until the next month
    (Δ106: never cut to the dollars left). With the feature off nothing changes."""
    if not ctx.enabled:
        return FileStart(rec, StartOutcome.DISABLED, None, ctx)
    stub = stub_from_probe(probe)
    if stub is not None:
        return FileStart(stub_record(rec, stub, int(probe.duration_s)), StartOutcome.STUBBED, None, ctx)
    reservation, held = reserve_if_fits(estimate_usd(probe.duration_s), ctx)
    if reservation is None:
        return FileStart(_settle(rec, PARKED, wake=_next_month(ctx.now)), StartOutcome.PARKED, None, ctx)
    working = replace(_settle(rec, WORKING), transcript_key=content_key(rec)[0],
                      transcript_seconds=int(probe.duration_s))
    return FileStart(working, StartOutcome.STARTED, reservation, held)


def mark_current(rec: DriveIndexRecord, now: datetime, *, seconds: int, speakers: int, languages: Tuple[str, ...],
                 stub: str = "") -> DriveIndexRecord:
    """The job finished: a transcript (or the model's `no_speech` stub) is stored at this content key. Attempts and holds
    are spent. `model_read_at` anchors the edit cooldown."""
    out = replace(rec, transcript_state=CURRENT, transcript_key=content_key(rec)[0],
                  transcript_version=TRANSCRIBE_VERSION, transcript_body_version=TRANSCRIPT_BODY_VERSION,
                  transcript_stub=stub, transcript_seconds=seconds, transcript_speakers=speakers,
                  transcript_languages=languages, model_read_at=now, transcript_attempts=0,
                  transcript_hold=None, transcript_hold_version="", transcript_wake_at=None)
    if stub:
        out = replace(out, state=RecordState.METADATA_ONLY, reason=_STUB_RECORD_REASON[TranscriptStub(stub)])
    return out


def charge_attempt(rec: DriveIndexRecord) -> DriveIndexRecord:
    """A file-level failed attempt (ffmpeg cannot read it): counted for this content version; `failed` at MAX_ATTEMPTS,
    with no stub (Δ100). Below it the file is `ready` again."""
    attempts = rec.transcript_attempts + 1
    out = replace(rec, transcript_attempts=attempts, transcript_hold_version=hold_key(rec), transcript_hold=None,
                  transcript_wake_at=None)
    return replace(out, transcript_state=FAILED if attempts >= MAX_ATTEMPTS else READY)


def state_for_job(job: TranscriptJob) -> str:
    """`waiting` once every window is submitted or done; `working` while any is still to extract, upload or redo."""
    if job.windows and all(w.state in ("submitted", "done") for w in job.windows):
        return WAITING
    return WORKING


def window_failed(window: JobWindow, check: WindowCheck) -> Tuple[JobWindow, WindowAction]:
    """G37, per check as §27.5/§27.6 word them. `INVALID` and `COVERAGE`: MAX_ATTEMPTS answers, then a gap marker (the
    declared choice, so one bad window does not fail a long file). `TIME`: one resubmit, then the gap marker.
    `INTERPOLATION`: one resubmit, then accepted with `~` stamps. `REFUSED`: a gap marker at once. A model stamp that
    `parse_stamp` rejects (ValueError) is `INVALID`, never a crash. A resubmitted window goes back to `uploaded`."""
    if check is WindowCheck.REFUSED:
        return replace(window, state="done", gap=True), WindowAction.GAP
    if check in (WindowCheck.INVALID, WindowCheck.COVERAGE):
        attempts = window.attempts + 1
        if attempts >= MAX_ATTEMPTS:
            return replace(window, attempts=attempts, state="done", gap=True), WindowAction.GAP
        return replace(window, attempts=attempts, state="uploaded"), WindowAction.RESUBMIT
    if check is WindowCheck.TIME:
        if window.time_resubmits < 1:
            return replace(window, time_resubmits=window.time_resubmits + 1, state="uploaded"), WindowAction.RESUBMIT
        return replace(window, state="done", gap=True), WindowAction.GAP
    if window.interp_resubmits < 1:
        return replace(window, interp_resubmits=window.interp_resubmits + 1, state="uploaded"), WindowAction.RESUBMIT
    return replace(window, state="done"), WindowAction.ACCEPT
