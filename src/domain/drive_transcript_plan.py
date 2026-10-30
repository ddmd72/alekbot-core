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
    file_estimate_usd(duration_s) -> float                                  the sum over the planned windows (overlaps paid)
    reserve_if_fits(estimate, ctx) -> (Reservation | None, ctx);  release(ctx, reservation) -> ctx      (G39, G9)
    transcript_plan(rec, ctx) -> rec                                        the state table below
    reset_on_new_version(rec) -> rec                                        a new content version clears the job (D42)
    leaves_job(written, planned) -> bool                                    the write takes the record off its job (Task 14)
    start_file(rec, ctx, probe) -> FileStart                                the stage's step after the probe (Task 12 step 4)
    wait_for_copy(rec) -> rec                                               a copy waits for another file's result (G12)
    stub_from_probe(probe) / stub_record(rec, stub, now, seconds) / mark_current(rec, now, ...)
    charge_attempt(rec, *, now=None) -> rec                                 a file-level failed attempt (`failed` at MAX_ATTEMPTS;
                                                                            with `now`, `held`/retry with a backoff, T-R21)
    state_for_job(job) -> "working" | "waiting"
    window_failed(window, check, ctx) -> WindowResult                       the per-check retry rules (G37); a resubmit
                                                                            is reserved like any purchase (T-R8)
    queue_blocked(now, backoff_until) -> bool                               a model backoff gates the stage's queue read
    TRANSCRIBER_AGENT                                                       the class's model backoff key (§25.4.6)

The state table, first match wins (`DriveIndexRecord.transcript_state`, plain strings, G21):
    none      not a TRANSCRIBE-admitted kind, not `normal` scope, a folder, card-capped (T-R24), not resolved or being
              deleted, a never-download reason (dot name, secret, OS artefact, remote item, package, malware, dense
              tree, kind not yet admitted), no content version, or inaccessible (§26.2).
    current   a transcript or stub exists at this content key and TRANSCRIPT_BODY_VERSION (the stage wrote `current`; the
              plan never looks in the store). A stale body version, a record read under another EXTRACTION_VERSION
              (G31), or a record `pending` again (its reading lost: a chunk-cap release, a re-read; T-R24), is not
              current: the record is `ready` and the stage finds the store hit. A TRANSCRIBE_VERSION bump changes NOTHING by itself (Δ122: re-buying is the requeue
              script's job): it is not part of the key, and records stay as they are.
    current   (too_large) `size_bytes > TRANSCRIBE_MAX_BYTES`: the plan writes the stub itself from metadata (record
              `metadata_only / too_large` through `demoted`, no probe, no model). Only while `enabled`.
    failed    `transcript_attempts >= MAX_ATTEMPTS` for this content version (ffmpeg cannot read it, or the probe's
              duration is unusable): NO stub (Δ100).
    waiting   windows submitted, batches open (written by the stage). Kept while the content key is unchanged, ahead of
    working   every hold below: a same-key edit (new cTag, same bytes) must not demote an in-flight job, which would buy
              it twice. `working`: some windows to extract or upload; the file's estimate is reserved from the moment it
              fit, before its first window (G39).
    parked    the whole file's estimate does not fit the month's cap after pending reservations; `transcript_wake_at` is
              the next month's start, never cut (Δ106). Entered by `start_file` after the probe (an audio file has no
              duration before it); the plan only keeps it until the wake-up.
    held      a hold for this content version, with `transcript_wake_at` set: `cooldown` (an edit within
              TRANSCRIBE_REREAD_COOLDOWN of `model_read_at`, set when the file was last bought: at `start_file` and again
              at `mark_current`; it applies on a free cache hit too, by design: the plan cannot see the store) or `quiet`
              (dirty and not `quiet_over`, a recording still uploading), or `retry` (a charged attempt backing off until
              its wake-up, T-R21; kept ahead of the cooldown and quiet holds).
    ready     due: the queue predicate (`transcript_state == "ready"`, newest `effective_day` first).
A model backoff is NOT a record state (§25.4.6, `JobState`'s docstring): the `drive_transcriber` backoff gates the stage's
queue read, records stay `ready`, an outage writes no record (G21).

`transcript_hold_version` tags everything that belongs to one content version of the file: its content key. A tag that is not the current one means the content changed: attempts, holds and the wake-up are cleared (D42).

Several public classes in one module, by decision: one contract (the job's decisions) with its value types, as
`drive_transcript` and `drive_enrichment` group theirs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import FrozenSet, Optional, Tuple

from src.domain.billing import batch_transcription_input_usd, batch_transcription_output_usd
from src.domain.drive_index import (
    EXTRACTION_VERSION, TRANSCRIBE_VERSION, DriveIndexRecord, Reason, RecordState, Scope, inaccessible,
)
from src.domain.drive_index_params import (
    MAX_ATTEMPTS, TRANSCRIBE_AUDIO_TOKENS_PER_S, TRANSCRIBE_HOUR_USD_ESTIMATE, TRANSCRIBE_MIN_OUTPUT_TOKENS, TRANSCRIBE_WINDOW_PROMPT_TOKENS, TRANSCRIBE_MAX_BYTES, TRANSCRIBE_MAX_SECONDS,
    TRANSCRIBE_REREAD_COOLDOWN, TRANSCRIBE_SHORT_ACCEPT_MAX_S, TRANSCRIBE_USD_PER_MONTH,
)
from src.domain.drive_index_planner import backoff, content_due_at, demoted, month_key, next_month_start, quiet_over
from src.domain.drive_index_policy import Admission, admission
from src.domain.drive_transcript import JobWindow, MediaProbe, TranscriptJob, TranscriptStub
from src.domain.drive_transcript_render import TRANSCRIPT_BODY_VERSION
from src.domain.drive_transcript_windows import plan_windows, record_content_key

NONE, READY, WORKING, WAITING = "none", "ready", "working", "waiting"
TRANSCRIBER_AGENT = "drive_transcriber"   # the model backoff key of this class (§25.4.6): the stage's and the tick's
CURRENT, HELD, PARKED, FAILED = "current", "held", "parked", "failed"
HOLD_COOLDOWN, HOLD_QUIET, HOLD_RESUBMIT, HOLD_RETRY = "cooldown", "quiet", "resubmit", "retry"

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
    THIS month's open reservations (Δ106; one made in an earlier month is not in it, G9); `cap_usd` the effective monthly cap (`effective_monthly_cap`). `enabled` is the kill
    switch AND the user's flag, decided by the caller."""
    now: datetime
    enabled: bool
    month_usd: float
    reserved_usd: float
    cap_usd: float


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
    UNREADABLE = "unreadable"  # the probe's duration is unusable (0, negative, NaN, infinite): a charged attempt


@dataclass(frozen=True)
class FileStart:
    rec: DriveIndexRecord
    outcome: StartOutcome
    reservation: Optional[Reservation]
    ctx: TranscriptContext


@dataclass(frozen=True)
class WindowResult:
    """`window_failed`'s answer. `reservation` is held for a RESUBMIT (release it on abandon); `ctx` carries it."""
    window: JobWindow
    action: "WindowAction"
    reservation: Optional[Reservation]
    ctx: TranscriptContext


FINISH_STOP = "STOP"   # the provider's clean finish (T-R27)


class WindowCheck(str, Enum):
    """Why a collected window answer is not usable (G37)."""
    INVALID = "invalid"              # request error, invalid JSON, an unparseable stamp (`parse_stamp` ValueError)
    COVERAGE = "coverage"            # the answer stops short of the window (Δ129)
    TIME = "time"                    # the time validator rejected the stamps
    INTERPOLATION = "interpolation"  # more than INTERPOLATED_MAX_SHARE of the stamps are computed
    REFUSED = "refused"              # a content-policy block


class WindowAction(str, Enum):
    RESUBMIT = "resubmit"
    PARK = "park"        # a resubmit is due but its estimate does not fit the month: the file waits for it (see below)
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
    """The audio part of an estimate: seconds at the hour rate. A window also pays `window_prompt_usd()`."""
    return max(0.0, seconds) * TRANSCRIBE_HOUR_USD_ESTIMATE / 3600.0


def window_prompt_usd() -> float:
    """The fixed per-window allowance for the text input (system instruction + task) at the batch input rate."""
    return batch_transcription_input_usd(TRANSCRIBE_WINDOW_PROMPT_TOKENS)


def window_cost_usd(duration_s: float) -> float:
    """One window's reservation (T-R19, T-R26): the larger of the hour-rate estimate and its worst case, audio input +
    text input + the output cap (`max(TRANSCRIBE_MIN_OUTPUT_TOKENS, 1000 per minute)`, as the agent sets it) at the
    batch rates. The floor matters for a short window, whose cap is the minimum, not the per-minute figure."""
    seconds = max(0.0, duration_s)
    hour_rate = estimate_usd(seconds) + window_prompt_usd()
    output_cap = max(TRANSCRIBE_MIN_OUTPUT_TOKENS, math.ceil(1_000 * seconds / 60))
    worst = (batch_transcription_input_usd(math.ceil(TRANSCRIBE_AUDIO_TOKENS_PER_S * seconds)
                                           + TRANSCRIBE_WINDOW_PROMPT_TOKENS)
             + batch_transcription_output_usd(output_cap))
    return max(hour_rate, worst)


def available_usd(ctx: TranscriptContext) -> float:
    """What the month's cap has left after what was collected and what is reserved (no initial-build allowance)."""
    return max(0.0, ctx.cap_usd - ctx.month_usd - ctx.reserved_usd)


def fits(estimate: float, ctx: TranscriptContext) -> bool:
    return estimate <= available_usd(ctx) + 1e-9


def file_estimate_usd(duration_s: float) -> float:
    """The file's estimate: the SUM over its planned windows, overlaps paid for (the cost is per audio sent)."""
    return sum(window_cost_usd(w.duration_s) for w in plan_windows(duration_s))


def reserve_if_fits(estimate: float, ctx: TranscriptContext) -> Tuple[Optional[Reservation], TranscriptContext]:
    """G39: the fit check and the reservation are ONE step. The returned context carries the reservation, so the next
    file checked in the same tick sees it; a second file can never pass a check the cap only holds for one."""
    if not fits(estimate, ctx):
        return None, ctx
    return Reservation(usd=estimate, month=month_key(ctx.now)), replace(ctx, reserved_usd=ctx.reserved_usd + estimate)


def release(ctx: TranscriptContext, reservation: Reservation) -> TranscriptContext:
    """Park, failure and abandon give the reservation back. `ctx.reserved_usd` holds THIS month's reservations only: one
    made in an earlier month was dropped from it at the turn, so releasing it now must not lower this month's (G9)."""
    if reservation.month != month_key(ctx.now):
        return ctx
    return replace(ctx, reserved_usd=max(0.0, ctx.reserved_usd - reservation.usd))


def queue_blocked(now: datetime, backoff_until: Optional[datetime]) -> bool:
    """The `drive_transcriber` model backoff (§25.4.6) gates the stage's queue read; it is never a record state."""
    return backoff_until is not None and now < backoff_until


# -- keys and versions --------------------------------------------------------------------------------------------

def hold_key(rec: DriveIndexRecord) -> str:
    """The tag of everything that belongs to this content version of the file: its content key. Deliberately not the
    TRANSCRIBE_VERSION: a bump re-queues nothing (Δ122)."""
    return record_content_key(rec)[0]


def reset_on_new_version(rec: DriveIndexRecord) -> DriveIndexRecord:
    """D42: a new content version clears attempts, holds, the wake-up and a stale stub, and
    re-keys: the file re-transcribes, or hits the cache through its content key. A job in a state made for another key
    goes back to `ready` (the stage drops the job document whose key no longer matches)."""
    key = record_content_key(rec)[0]
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


def leaves_job(written: DriveIndexRecord, planned: DriveIndexRecord) -> bool:
    """The write takes the record off the job document it may hold, so the tick drops that job at once (its reservation
    released, Task 12 carry) instead of when the stage reaches the record: the plan took it out of the pipeline
    (`none`: not eligible any more), or a new content version reset it (`reset_on_new_version`). `written` is the record
    as the caller wrote it, `planned` the plan's result. A retry, park or quiet hold that ends at the same content
    version is not a reset: its job keeps the windows uploaded for the next try. A `current` or `failed` record holds
    no job (dropped when it finished), nor does a `none` one."""
    if written.transcript_state in (NONE, CURRENT, FAILED):
        return False
    if planned.transcript_state == NONE:
        return True
    return reset_on_new_version(written) != written


# -- the plan -----------------------------------------------------------------------------------------------------

def _eligible(rec: DriveIndexRecord) -> bool:
    """A card-capped record is not (T-R24): its reading would never be found (§5.4, search never shows content
    without a card), so it is not bought; its job is dropped (`leaves_job`). When the cap releases it, it is `pending`
    again and re-planned, and a transcript already stored is a store hit with no model cost."""
    return (admission(rec.kind) is Admission.TRANSCRIBE and rec.scope is Scope.NORMAL and not rec.is_folder
            and not inaccessible(rec) and not rec.card_capped
            and rec.state not in (RecordState.UNRESOLVED, RecordState.DELETING)
            and bool(rec.content_version) and rec.reason not in _NEVER_TRANSCRIBED)


def _is_current(rec: DriveIndexRecord, key: str) -> bool:
    """A record read under another EXTRACTION_VERSION is not current either (G31): its outcome no longer stands, the
    upgrade re-plan would leave it `pending` forever, and the stage's store path re-finalizes it with no model call.
    `extraction_version == 0` (never stamped) is not stale: the planner's own rule (`stale_versions`).

    Nor is a record that is `pending` again (T-R24): its reading is gone while the transcript stands — the chunk cap
    released it (`chunk_released`), a failed re-embed sent it to a re-read, a new cTag with the same bytes re-planned it
    — and no index run reads a media file (`content_due` is false for TRANSCRIBE kinds), so it would stay `pending` for
    good; the stage's store path indexes it again with no model call. A finished file is never `pending`: a transcript
    leaves it `indexed`, a stub or a gate or cap refusal `metadata_only`."""
    stale_extraction = rec.extraction_version > 0 and rec.extraction_version != EXTRACTION_VERSION
    return (rec.transcript_state == CURRENT and rec.transcript_key == key
            and rec.transcript_body_version == TRANSCRIPT_BODY_VERSION and not stale_extraction
            and rec.state is not RecordState.PENDING)


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
    key = record_content_key(rec)[0]
    if _is_current(rec, key):
        return _settle(rec, CURRENT)
    if ctx.enabled and rec.size_bytes > TRANSCRIBE_MAX_BYTES:
        return stub_record(rec, TranscriptStub.TOO_LARGE, ctx.now)
    if rec.transcript_attempts >= MAX_ATTEMPTS:
        return _settle(rec, FAILED)
    # an in-flight job for this very content key stands ahead of every hold: a same-key edit (new cTag, same bytes)
    # must not demote it to held/ready, which would start it, and pay for it, a second time
    if rec.transcript_state in (WORKING, WAITING) and rec.transcript_key == key:
        return _settle(rec, rec.transcript_state)
    if (rec.transcript_state == PARKED and rec.transcript_wake_at is not None
            and rec.transcript_wake_at > ctx.now):
        return _settle(rec, PARKED, hold=rec.transcript_hold if rec.transcript_hold == HOLD_RESUBMIT else None,
                       wake=rec.transcript_wake_at)
    if rec.transcript_state == PARKED and rec.transcript_hold == HOLD_RESUBMIT and rec.transcript_key == key:
        return _settle(rec, WORKING)     # woken mid-job: resume the parked window, never re-reserve the whole file
    if (rec.transcript_state == HELD and rec.transcript_hold == HOLD_RETRY and rec.transcript_wake_at is not None
            and rec.transcript_wake_at > ctx.now):
        return _settle(rec, HELD, hold=HOLD_RETRY, wake=rec.transcript_wake_at)   # a charged attempt backs off (T-R21)
    # a purchase of another content version exists (done, or in flight when the edit came) and is younger than the
    # cooldown: an edited recording is re-bought at most once per TRANSCRIBE_REREAD_COOLDOWN (T-R9)
    if rec.transcript_key not in ("", key) and rec.model_read_at is not None:
        until = rec.model_read_at + TRANSCRIBE_REREAD_COOLDOWN
        if ctx.now < until:
            return _settle(rec, HELD, hold=HOLD_COOLDOWN, wake=until)
    if not quiet_over(rec, ctx.now):
        return _settle(rec, HELD, hold=HOLD_QUIET, wake=content_due_at(rec, ctx.now))
    return _settle(rec, READY)


# -- what the stage writes ----------------------------------------------------------------------------------------

def stub_from_probe(probe: MediaProbe) -> Optional[TranscriptStub]:
    """Decided from what ffprobe says (§27.4): no audio stream, or longer than TRANSCRIBE_MAX_SECONDS. No model call."""
    if not probe.has_audio:
        return TranscriptStub.NO_AUDIO
    if probe.duration_s > TRANSCRIBE_MAX_SECONDS:
        return TranscriptStub.TOO_LONG
    return None


def stub_record(rec: DriveIndexRecord, stub: TranscriptStub, now: datetime, seconds: int = 0) -> DriveIndexRecord:
    """A stub is a result: `current` at this content key, the record `metadata_only` with the stub's reason, built through
    `demoted` so the content outcome stands (`extraction_version`), the quiet period ends and the card is scheduled: a
    rename, rescope or upgrade re-observes it as the same outcome, never as `pending` again."""
    done = demoted(rec, _STUB_RECORD_REASON[stub], now)
    return replace(done, transcript_state=CURRENT, transcript_key=record_content_key(rec)[0],
                   transcript_version=TRANSCRIBE_VERSION, transcript_body_version=TRANSCRIPT_BODY_VERSION,
                   transcript_stub=stub.value, transcript_seconds=seconds, transcript_hold=None,
                   transcript_wake_at=None, transcript_hold_version=hold_key(rec) if rec.transcript_attempts > 0 else "")


def _usable_duration(duration_s: float) -> bool:
    return math.isfinite(duration_s) and duration_s > 0


def start_file(rec: DriveIndexRecord, ctx: TranscriptContext, probe: MediaProbe) -> FileStart:
    """The stage's step after the probe (Task 12 step 4): a stub from the probe, or the whole file's estimate reserved in
    the same step as the fit check (G39) and the record `working`, or the whole file parked until the next month
    (Δ106: never cut to the dollars left). With the feature off nothing changes.

    The probe is untrusted input. A file with no audio stream is a true stub whatever its duration says. Otherwise a
    duration that is 0, negative, NaN or infinite cannot be planned (no windows, a $0 estimate that fits any cap): it is a
    charged attempt (`charge_attempt`, `failed` at MAX_ATTEMPTS, no stub), not a stub, because a stub states a fact
    about the recording and this is a probe that did not answer sensibly (same class as ffmpeg failing to read it).

    A container with no duration header (`duration_known=False`, a browser-recorded webm, G35/T-R15) is not unreadable:
    its duration is learned while it is extracted. The reservation is the worst case, `file_estimate_usd` of
    TRANSCRIBE_MAX_SECONDS, re-settled to the real estimate when the extraction reaches the end of the audio
    (`drive_transcript_job.resettled`); `transcript_seconds` stays 0 until then."""
    if not ctx.enabled:
        return FileStart(rec, StartOutcome.DISABLED, None, ctx)
    if not probe.has_audio:
        return FileStart(stub_record(rec, TranscriptStub.NO_AUDIO, ctx.now), StartOutcome.STUBBED, None, ctx)
    if not probe.duration_known:
        return _reserve_start(rec, ctx, TRANSCRIBE_MAX_SECONDS, seconds=0)
    if not _usable_duration(probe.duration_s):
        return FileStart(charge_attempt(rec), StartOutcome.UNREADABLE, None, ctx)
    stub = stub_from_probe(probe)
    if stub is not None:
        return FileStart(stub_record(rec, stub, ctx.now, int(probe.duration_s)), StartOutcome.STUBBED, None, ctx)
    return _reserve_start(rec, ctx, probe.duration_s, seconds=int(probe.duration_s))


def _reserve_start(rec: DriveIndexRecord, ctx: TranscriptContext, planned_s: float, *, seconds: int) -> FileStart:
    reservation, held = reserve_if_fits(file_estimate_usd(planned_s), ctx)
    if reservation is None:
        return FileStart(park(rec, ctx), StartOutcome.PARKED, None, ctx)
    working = replace(_settle(rec, WORKING), transcript_key=record_content_key(rec)[0],
                      transcript_seconds=seconds, model_read_at=ctx.now)   # the purchase time (T-R9)
    return FileStart(working, StartOutcome.STARTED, reservation, held)


def wait_for_copy(rec: DriveIndexRecord) -> DriveIndexRecord:
    """G12: another file with the same shared content key is being transcribed; this one waits for its stored result
    (`waiting` at this key, with no job of its own, nothing reserved). Kept by the plan while the key stands."""
    return replace(_settle(rec, WAITING), transcript_key=record_content_key(rec)[0])


def park(rec: DriveIndexRecord, ctx: TranscriptContext, *, resubmit: bool = False) -> DriveIndexRecord:
    """The whole file waits for the next month's cap (Δ106). `resubmit=True`: parked MID-JOB because a window's resubmit
    did not fit (`WindowAction.PARK`); the record keeps its job key and the hold `resubmit`, and wakes as `working`."""
    return _settle(rec, PARKED, hold=HOLD_RESUBMIT if resubmit else None, wake=next_month_start(ctx.now))


def resume_window(window: JobWindow, ctx: TranscriptContext) -> WindowResult:
    """The wake-up of a mid-job park: reserve ONLY the parked window's estimate (the file's original reservation is still
    held). `RESUBMIT` with the reservation, or `PARK` again when it still does not fit."""
    return _resubmit(window, ctx)


def mark_current(rec: DriveIndexRecord, now: datetime, *, seconds: int, speakers: int, languages: Tuple[str, ...],
                 stub: str = "", truncated: bool = False) -> DriveIndexRecord:
    """The job finished: a transcript (or the model's `no_speech` stub) is stored at this content key. Attempts and holds
    are spent. `model_read_at` anchors the edit cooldown."""
    out = replace(rec, transcript_state=CURRENT, transcript_key=record_content_key(rec)[0],
                  transcript_version=TRANSCRIBE_VERSION, transcript_body_version=TRANSCRIPT_BODY_VERSION,
                  transcript_stub=stub, transcript_seconds=seconds, transcript_speakers=speakers,
                  transcript_languages=languages, transcript_truncated=truncated, model_read_at=now, transcript_attempts=0,
                  transcript_hold=None, transcript_hold_version="", transcript_wake_at=None)
    if stub:
        out = demoted(out, _STUB_RECORD_REASON[TranscriptStub(stub)], now)
    return out


def charge_attempt(rec: DriveIndexRecord, *, now: Optional[datetime] = None) -> DriveIndexRecord:
    """A file-level failed attempt (ffmpeg cannot read it): counted for this content version; `failed` at MAX_ATTEMPTS,
    with no stub (Δ100). Below it the file is `ready` again — or, given `now` (the stage always passes it, T-R21),
    `held` with the hold `retry` until `now + backoff`, the planner's per-item backoff (`drive_index_planner.backoff`,
    first retry after RETRY_BASE, doubling, capped at RETRY_CAP), so a transient failure cannot spend every attempt
    within minutes. The plan keeps it held while the wake-up is in the future; a due wake-up makes it `ready`."""
    attempts = rec.transcript_attempts + 1
    out = replace(rec, transcript_attempts=attempts, transcript_hold_version=hold_key(rec), transcript_hold=None,
                  transcript_wake_at=None)
    if attempts >= MAX_ATTEMPTS:
        return replace(out, transcript_state=FAILED)
    if now is None:
        return replace(out, transcript_state=READY)
    return replace(out, transcript_state=HELD, transcript_hold=HOLD_RETRY, transcript_wake_at=now + backoff(attempts - 1))


def state_for_job(job: TranscriptJob) -> str:
    """`waiting` once every window is submitted or done; `working` while any is still to extract, upload or redo."""
    if job.windows and all(w.state in ("submitted", "done") for w in job.windows):
        return WAITING
    return WORKING


def window_failed(window: JobWindow, check: WindowCheck, ctx: TranscriptContext,
                  finish_reason: str = "") -> WindowResult:
    """G37, per check as §27.5/§27.6 word them. `INVALID`: MAX_ATTEMPTS answers, then a gap marker (the
    declared choice, so one bad window does not fail a long file). `TIME`: one resubmit, then the gap marker.
    `INTERPOLATION`: one resubmit, then accepted with `~` stamps. `REFUSED`: a gap marker at once. A model stamp that
    `parse_stamp` rejects (ValueError) is `INVALID`, never a crash.

    A resubmit is a purchase (T-R8): the window's estimate is reserved in the same step as its fit check, on the context
    the caller threads. When it does not fit the action is `PARK`: the attempt stays counted, the window stays
    `uploaded`, nothing new is reserved, and the caller parks the file MID-JOB with `park(rec, ctx, resubmit=True)`. The
    file's ORIGINAL reservation stays held for as long as any window of the file is in flight (it is released only when
    the file settles, fails or is abandoned). On the wake-up the plan returns the record to `working` (never `ready`: that
    would reserve the whole file again), and the caller reserves only the parked window with `resume_window`.
    `COVERAGE` (T-R27): a short answer that finished cleanly (`finish_reason` STOP) is resubmitted ONCE, uncharged
    (`coverage_resubmits`); if the second is short again and also STOP, the audio ends in silence: ACCEPT. A short answer
    that did not finish cleanly (MAX_TOKENS, none) keeps the MAX_ATTEMPTS rule. T-R31: that acceptance stands as it is
    only for a window up to TRANSCRIBE_SHORT_ACCEPT_MAX_S; a longer one is accepted `accepted_short`, so its transcript
    is stored `truncated` (the model may have stopped early, Δ129) and never silently.
    A resubmitted window goes back to `uploaded`."""
    if check is WindowCheck.REFUSED:
        return WindowResult(replace(window, state="done", gap=True), WindowAction.GAP, None, ctx)
    if check is WindowCheck.COVERAGE and finish_reason == FINISH_STOP:
        if window.coverage_resubmits < 1:
            return _resubmit(replace(window, coverage_resubmits=window.coverage_resubmits + 1, state="uploaded"), ctx)
        marked = window.spec.duration_s > TRANSCRIBE_SHORT_ACCEPT_MAX_S
        return WindowResult(replace(window, state="done", accepted_short=marked), WindowAction.ACCEPT, None, ctx)
    if check in (WindowCheck.INVALID, WindowCheck.COVERAGE):
        attempts = window.attempts + 1
        if attempts >= MAX_ATTEMPTS:
            return WindowResult(replace(window, attempts=attempts, state="done", gap=True), WindowAction.GAP, None, ctx)
        return _resubmit(replace(window, attempts=attempts, state="uploaded"), ctx)
    if check is WindowCheck.TIME:
        if window.time_resubmits < 1:
            return _resubmit(replace(window, time_resubmits=window.time_resubmits + 1, state="uploaded"), ctx)
        return WindowResult(replace(window, state="done", gap=True), WindowAction.GAP, None, ctx)
    if window.interp_resubmits < 1:
        return _resubmit(replace(window, interp_resubmits=window.interp_resubmits + 1, state="uploaded"), ctx)
    return WindowResult(replace(window, state="done"), WindowAction.ACCEPT, None, ctx)


def window_estimate_usd(window: JobWindow) -> float:
    return window_cost_usd(window.spec.duration_s)


def _resubmit(window: JobWindow, ctx: TranscriptContext) -> WindowResult:
    reservation, held = reserve_if_fits(window_estimate_usd(window), ctx)
    if reservation is None:
        return WindowResult(window, WindowAction.PARK, None, ctx)
    return WindowResult(window, WindowAction.RESUBMIT, reservation, held)
