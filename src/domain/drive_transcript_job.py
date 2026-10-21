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
"""
from dataclasses import dataclass, replace
from datetime import datetime
from typing import List, Optional, Tuple

from src.domain.drive_index_params import MEDIA_PASS_MAX_WINDOWS, TRANSCRIBE_FILE_LIFE_AT_SUBMIT, TRANSCRIBE_MAX_SECONDS
from src.domain.drive_transcript import JobWindow, TranscriptJob, WindowSpec
from src.domain.drive_transcript_plan import file_estimate_usd
from src.domain.drive_transcript_windows import plan_windows

UPLOADED, SUBMITTED, DONE = "uploaded", "submitted", "done"


@dataclass(frozen=True)
class Resettled:
    """`resettled`'s answer: the job at its real duration, the windows that fell outside the new plan (their provider
    files are to be deleted) and the dollars of the file's reservation to give back (>= 0, in `job.reserved_month`)."""
    job: TranscriptJob
    dropped: Tuple[JobWindow, ...]
    release_usd: float


def request_key(item_id: str, index: int) -> str:
    return f"{item_id}:{index}"


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
