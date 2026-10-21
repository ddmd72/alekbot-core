"""
The window bookkeeping of one transcript job document (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.4-§27.5; plan Task 12,
G35/T-R15, T-R16). Pure: no clock (time comes in as `now`), no I/O, no logging.

A job lists a window only once its audio is at the provider (`uploaded`) or later (`submitted`, `done`), or when collection
sent it back (`extracted`, `failed`: its audio must be cut again). A window is never persisted as planned: its local file
does not outlive the extraction step, so "planned" is derived from the job's duration (`job_plan`). A duration of 0 means
"not known yet" (a container without a duration header): the plan is then the worst case, TRANSCRIBE_MAX_SECONDS, cut in
passes of at most MEDIA_PASS_MAX_WINDOWS contiguous windows, until a run reaches the end of the audio (`resettled`).

Public interface:
    job_plan(job) -> windows                       the planned windows (the worst case while the duration is unknown)
    windows_to_extract(job, now, *, pass_mode)     what the next extraction run asks for
    with_uploaded(job, spec, file_name, expires)   one window's audio reached the provider (counters kept)
    ready_to_submit(job, now) / resubmits(job)     the file's windows can go in a batch; which of them are re-purchases
    submitted(job, batch_name)                     the uploaded windows are in `batch_name`
    resettled(job, duration_s) -> Resettled        the real duration is known: plan, windows and reservation follow it
    request_key(item_id, index)                    the batch request key `{item_id}:{index}` (Δ103)
    split_request_key(key) -> (item_id, index)     its inverse; ValueError for a key that is not one

Collection (plan Task 13, §27.5-§27.6, G37):
    poll_after(batch, now) -> datetime             the next poll of an open batch (Δ104, Δ133)
    is_repurchase(window) -> bool                  collection sent it back before: a submission of it is a new purchase
    check_window(spec, transcript) -> WindowCheck | None    the window checks, in order: time, coverage, interpolation
    interpolated_share(spec, transcript) -> float  the share of the window's stamps interpolation would compute
    in_batch(job, batch_name) -> windows           the job's windows carried by that batch
    file_gone(window) -> window                    its provider file is gone: re-extracted and re-uploaded once
    after_failed_batch(window, now) -> window      its batch failed whole: resubmitted once, re-extracted once if expired
    all_done(job) -> bool                          every window answered (a gap marker counts): the file can be finalized
    stitch_job(job, results) -> StitchedTranscript the windows on the file's clock, speakers aligned, stamps interpolated
"""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Dict, List, Mapping, Optional, Tuple

from src.domain.drive_index_params import (
    BATCH_POLL_EVERY_S, BATCH_POLL_SLOW_S, BATCH_SLOW_AFTER_S, INTERPOLATED_MAX_SHARE, MEDIA_PASS_MAX_WINDOWS,
    TRANSCRIBE_FILE_LIFE_AT_SUBMIT, TRANSCRIBE_MAX_SECONDS,
)
from src.domain.drive_transcript import (
    JobWindow, Segment, StitchedTranscript, TranscriptBatch, TranscriptJob, WindowSpec, WindowTranscript,
)
from src.domain.drive_transcript_plan import WindowCheck, file_estimate_usd
from src.domain.drive_transcript_stitch import align_speakers, interpolate, rebase, seam_cuts
from src.domain.drive_transcript_windows import coverage_ok, plan_windows, times_ok

UPLOADED, SUBMITTED, DONE, EXTRACTED = "uploaded", "submitted", "done", "extracted"


@dataclass(frozen=True)
class Resettled:
    """`resettled`'s answer: the job at its real duration, the windows that fell outside the new plan (their provider
    files are to be deleted) and the dollars of the file's reservation to give back (>= 0, in `job.reserved_month`)."""
    job: TranscriptJob
    dropped: Tuple[JobWindow, ...]
    release_usd: float


def request_key(item_id: str, index: int) -> str:
    return f"{item_id}:{index}"


def split_request_key(key: str) -> Tuple[str, int]:
    """`{item_id}:{index}` back to its parts (the index after the LAST colon); ValueError for anything else."""
    item_id, sep, index = key.rpartition(":")
    if not sep or not item_id or not index.isdigit():
        raise ValueError("not a transcript request key")
    return item_id, int(index)


def job_plan(job: TranscriptJob) -> Tuple[WindowSpec, ...]:
    return plan_windows(job.duration_s if job.duration_s > 0 else float(TRANSCRIBE_MAX_SECONDS))


def _usable_upload(window: JobWindow, now: datetime) -> bool:
    """Its provider file lives long enough for a batch to run (§27.5: the API answers within 24 h)."""
    return window.files_expire_at is None or window.files_expire_at > now + TRANSCRIBE_FILE_LIFE_AT_SUBMIT


def _present(window: JobWindow, now: datetime) -> bool:
    """Needs no extraction: in a batch or answered, or uploaded with enough file life left."""
    if window.state in (SUBMITTED, DONE):
        return True
    return window.state == UPLOADED and _usable_upload(window, now)


def windows_to_extract(job: TranscriptJob, now: datetime, *, pass_mode: bool) -> Tuple[WindowSpec, ...]:
    """The planned windows not yet at the provider. `pass_mode` (no duration header, or a duration not known yet): the
    first contiguous run of them, at most MEDIA_PASS_MAX_WINDOWS (the port's slice rule, T-R15); otherwise all of them
    (one `-ss/-t` run per window, the run stops at its wall)."""
    present = {w.spec.index for w in job.windows if _present(w, now)}
    missing = [s for s in job_plan(job) if s.index not in present]
    if not pass_mode:
        return tuple(missing)
    run: List[WindowSpec] = []
    for spec in missing:
        if len(run) == MEDIA_PASS_MAX_WINDOWS or (run and spec.index != run[-1].index + 1):
            break
        run.append(spec)
    return tuple(run)


def _find(job: TranscriptJob, index: int) -> Optional[JobWindow]:
    return next((w for w in job.windows if w.spec.index == index), None)


def with_uploaded(job: TranscriptJob, spec: WindowSpec, file_name: str, expires_at: datetime) -> TranscriptJob:
    """The window's audio is at the provider under `file_name` (a NAME, `files/<id>`, never a URI). A window collection
    sent back keeps its counters and its request key (a resubmission is a re-purchase, `resubmits`)."""
    old = _find(job, spec.index)
    if old is None:
        new = JobWindow(spec=spec, state=UPLOADED, file_ref=file_name, files_expire_at=expires_at)
    else:
        new = replace(old, spec=spec, state=UPLOADED, file_ref=file_name, files_expire_at=expires_at, batch_name="")
    others = [w for w in job.windows if w.spec.index != spec.index]
    return replace(job, windows=tuple(sorted(others + [new], key=lambda w: w.spec.index)))


def ready_to_submit(job: TranscriptJob, now: datetime) -> bool:
    """Every window of the file is at the provider (the duration known, so "every" is known) and at least one is not
    in a batch yet: the file's windows go into the user's next submission (§27.5)."""
    if job.duration_s <= 0 or not job.windows:
        return False
    present = {w.spec.index for w in job.windows if _present(w, now)}
    if any(s.index not in present for s in job_plan(job)):
        return False
    return any(w.state == UPLOADED for w in job.windows)


def resubmits(job: TranscriptJob) -> Tuple[JobWindow, ...]:
    """Uploaded windows that were in a batch before (their request key is set): each is a new purchase, reserved on its
    own when it is submitted (T-R8), never covered by the file's first reservation (G39)."""
    return tuple(w for w in job.windows if w.state == UPLOADED and w.request_key)


def submitted(job: TranscriptJob, batch_name: str) -> TranscriptJob:
    windows = tuple(replace(w, state=SUBMITTED, request_key=request_key(job.item_id, w.spec.index), batch_name=batch_name)
                    if w.state == UPLOADED else w for w in job.windows)
    return replace(job, windows=windows, batch_name=batch_name)


def resettled(job: TranscriptJob, duration_s: float) -> Resettled:
    """A run reached the end of the audio (`eof`, T-R15/T-R16): the plan follows the real duration, a window outside
    it is dropped, and the file's reservation comes down to the real estimate (never up: the worst case or the header's
    duration was reserved, and the audio cannot be longer than what was read)."""
    plan = {s.index for s in plan_windows(duration_s)}
    kept = tuple(w for w in job.windows if w.spec.index in plan)
    dropped = tuple(w for w in job.windows if w.spec.index not in plan)
    estimate = file_estimate_usd(duration_s)
    release = max(0.0, job.reserved_usd - estimate)
    out = replace(job, duration_s=float(duration_s), windows=kept, reserved_usd=min(job.reserved_usd, estimate))
    return Resettled(out, dropped, release)


# -- collection (plan Task 13) --------------------------------------------------------------------------------------

def poll_after(batch: TranscriptBatch, now: datetime) -> datetime:
    """The next poll of a batch that is still running (Δ104, Δ133): every BATCH_POLL_EVERY_S, every BATCH_POLL_SLOW_S
    once the batch is older than BATCH_SLOW_AFTER_S (the API's contract is 24 h). The first poll, BATCH_POLL_FIRST_S
    after the submission, is set when the batch is created. Never None: an open batch without it is never polled."""
    since = batch.submitted_at or batch.created_at or now
    slow = now - since >= timedelta(seconds=BATCH_SLOW_AFTER_S)
    return now + timedelta(seconds=BATCH_POLL_SLOW_S if slow else BATCH_POLL_EVERY_S)


def is_repurchase(window: JobWindow) -> bool:
    """Collection sent the window back at least once (a failed answer, a gone file, the time or the interpolation check,
    each counted on the window): every later submission of it is a new purchase, reserved at submit (T-R8) and carried
    by its batch (T-R11), so its cost is booked in the batch's month. A window resubmitted only because its batch failed
    whole is not one: nothing was bought, and the file's reservation still covers it (G39)."""
    return window.attempts > 0 or window.time_resubmits > 0 or window.interp_resubmits > 0


def interpolated_share(spec: WindowSpec, transcript: WindowTranscript) -> float:
    """The share of the window's stamps that interpolation computes rather than the model measured (§27.6). Called WITH
    the window's spec: without one, `interpolate` returns no shares and the check never fires."""
    _, shares = interpolate(rebase(spec, transcript.segments), (spec,))
    return shares[0] if shares else 0.0


def check_window(spec: WindowSpec, transcript: WindowTranscript) -> Optional[WindowCheck]:
    """The first check a parsed window answer fails, or None (G37 gives each its own retry rule, `window_failed`):
    the time validator (§27.6), the coverage check (Δ129), the interpolation share (§27.6). A `no_speech` answer has
    nothing to check."""
    if transcript.no_speech:
        return None
    if not times_ok(spec, transcript.segments):
        return WindowCheck.TIME
    if not coverage_ok(spec, transcript.segments, transcript.no_speech):
        return WindowCheck.COVERAGE
    if interpolated_share(spec, transcript) > INTERPOLATED_MAX_SHARE:
        return WindowCheck.INTERPOLATION
    return None


def in_batch(job: TranscriptJob, batch_name: str) -> Tuple[JobWindow, ...]:
    return tuple(w for w in job.windows if w.state == SUBMITTED and w.batch_name == batch_name)


def file_gone(window: JobWindow) -> JobWindow:
    """The provider no longer has the window's input file (rpc 5/7 on its request, T-R18): its audio is cut and
    uploaded again, once (`reupload_used`). The caller has already charged the answer (`window_failed`)."""
    return replace(window, state=EXTRACTED, file_ref="", files_expire_at=None, batch_name="", reupload_used=True)


def after_failed_batch(window: JobWindow, now: datetime) -> JobWindow:
    """The window's batch failed, expired or was cancelled whole (§27.5, Δ104): resubmitted once (`batch_resubmits`),
    then a gap marker. Nothing was bought, so a window that was a first purchase loses its request key and stays covered
    by the file's reservation (`is_repurchase`); a re-purchase keeps it and is reserved again at submit. A window whose
    provider file will not outlive a batch (TRANSCRIBE_FILE_LIFE_AT_SUBMIT, stage A's rule) is re-extracted and
    re-uploaded, once per window; when that is spent it is a gap marker too."""
    if window.batch_resubmits >= 1:
        return replace(window, state=DONE, gap=True, batch_name="")
    out = replace(window, state=UPLOADED, batch_name="", batch_resubmits=window.batch_resubmits + 1,
                  request_key=window.request_key if is_repurchase(window) else "")
    if _usable_upload(window, now):
        return out
    if window.reupload_used:
        return replace(out, state=DONE, gap=True)
    return replace(out, state=EXTRACTED, file_ref="", files_expire_at=None, reupload_used=True)


def all_done(job: TranscriptJob) -> bool:
    """Every planned window answered, a gap marker included (the duration known, so "every" is known)."""
    if job.duration_s <= 0 or not job.windows:
        return False
    done = {w.spec.index for w in job.windows if w.state == DONE}
    return all(s.index in done for s in job_plan(job))


def stitch_job(job: TranscriptJob, results: Mapping[int, WindowTranscript]) -> StitchedTranscript:
    """The file's transcript from its windows' stored answers (§27.6): a gap marker for a window done as a gap (or
    whose answer is missing), placed at its seam cut so the cut keeps it; speakers aligned by code (the count is the
    aligned one, any uncertain seam marks the transcript); stamps interpolated WITH the specs; `truncated` when any gap
    stands in it. Languages in order of first appearance."""
    windows = sorted(job.windows, key=lambda w: w.spec.index)
    specs = [w.spec for w in windows]
    cuts = seam_cuts(specs)
    pairs: List[Tuple[WindowSpec, WindowTranscript]] = []
    languages: Dict[str, None] = {}
    truncated = False
    for k, window in enumerate(windows):
        answer = None if window.gap else results.get(window.spec.index)
        if answer is None:
            truncated = True
            start = cuts[k - 1] - window.spec.start_s if k > 0 else 0.0
            answer = WindowTranscript(language_codes=(), speakers=0, no_speech=False,
                                      segments=(Segment(start_s=max(0.0, start), speaker=None, text="", gap=True),))
        for code in answer.language_codes:
            languages.setdefault(code, None)
        pairs.append((window.spec, answer))
    segments, seams, speakers = align_speakers(pairs)
    segments, _ = interpolate(segments, specs)
    duration = job.duration_s if job.duration_s > 0 else (specs[-1].start_s + specs[-1].duration_s if specs else 0.0)
    return StitchedTranscript(segments=segments, language_codes=tuple(languages), speakers=speakers,
                              duration_s=duration, uncertain_seam=any(seams), truncated=truncated)
