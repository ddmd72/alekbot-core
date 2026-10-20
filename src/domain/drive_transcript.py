"""
Transcript value types (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27). Pure data: no I/O, no clock. States and stubs are
plain strings on records and job documents (Δ47's rollback invariant); the enum below only names the allowed stubs.

Several public classes in one module, by decision: one contract (the transcript pipeline) with several value types, as
`drive_enrichment` and `drive_extraction` group theirs (CLAUDE.md's one-class rule is waived here on purpose).
"""
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import List, Optional, Tuple

WINDOW_STATES: Tuple[str, ...] = ("extracted", "uploaded", "submitted", "done", "failed")   # JobWindow.state (§27.5)
BATCH_KIND_TRANSCRIBE = "transcribe"      # TranscriptBatch.kind; "summary" joins it with T3


class TranscriptStub(str, Enum):
    """The four stub values of `DriveIndexRecord.transcript_stub` (§27.3); the empty string means "not a stub"."""
    NO_SPEECH = "no_speech"
    TOO_LONG = "too_long"
    TOO_LARGE = "too_large"
    NO_AUDIO = "no_audio"


@dataclass(frozen=True)
class MediaProbe:
    """What ffprobe says about a file (§27.4). The duration here, not the facet, is trusted."""
    duration_s: float
    has_audio: bool
    container: str = ""


@dataclass(frozen=True)
class WindowSpec:
    """One window of media time: starts at `start_s` of the file's clock and lasts `duration_s`."""
    index: int
    start_s: float
    duration_s: float


@dataclass(frozen=True)
class Segment:
    """One utterance. `start_s` is seconds from the window's start inside a `WindowTranscript`, from the file's start
    once stitched. `speaker` is None for a single speaker. `interpolated`: the stamp was computed by code (character
    proportion, §27.6), not measured by the model; `gap`: a marker for a span the provider refused (text is unused)."""
    start_s: float
    speaker: Optional[int]
    text: str
    interpolated: bool = False
    gap: bool = False


@dataclass(frozen=True)
class WindowTranscript:
    """The parsed model answer for one window (§27.5)."""
    language_codes: Tuple[str, ...]
    speakers: int
    no_speech: bool
    segments: Tuple[Segment, ...]


@dataclass(frozen=True)
class StitchedTranscript:
    """The windows of one file on the file's clock with speakers aligned (§27.6)."""
    segments: Tuple[Segment, ...]
    language_codes: Tuple[str, ...]
    speakers: int
    duration_s: float
    uncertain_seam: bool = False      # a seam mapped on fewer than STITCH_MIN_MATCHED_WORDS words
    truncated: bool = False           # a gap marker stands for a window the provider refused


@dataclass(frozen=True)
class StoredTranscript:
    """The stored structured result under the content key (§27.8 b); never the rendered Markdown. The plan calls it
    `TranscriptStore`; renamed because REQ-ARCH-07 bars a `*Store` class in domain/."""
    content_key: str
    transcript: Optional[StitchedTranscript]
    stub: str = ""
    summary: str = ""
    summary_language: str = ""
    transcribe_version: int = 0
    render_version: int = 0
    summary_version: int = 0
    last_used_at: Optional[datetime] = None


@dataclass(frozen=True)
class JobWindow:
    """One window inside a job document. `state`: extracted, uploaded, submitted, done or failed (§27.5). `failed` is the
    stage's: the window cannot be redone as it stands (e.g. its provider file expired and re-extraction failed);
    `state_for_job` counts it as work still to do. The money is reserved per FILE (and per resubmission, held by the
    caller), never on the window."""
    spec: WindowSpec
    state: str = "extracted"
    file_ref: str = ""                # the provider's file handle; never a URL
    request_key: str = ""             # `{item_id}:{index}` in a batch
    attempts: int = 0                 # failed answers (invalid JSON, short coverage), toward MAX_ATTEMPTS (§27.5)
    time_resubmits: int = 0           # the time validator's one resubmit (§27.6, G37)
    interp_resubmits: int = 0         # the interpolation-share check's one resubmit (§27.6, G37)
    gap: bool = False                 # done as a gap marker: the provider refused it or the retries ran out
    batch_name: str = ""              # the provider batch carrying it while submitted (T-R12)
    files_expire_at: Optional[datetime] = None   # when the provider's input file expires (§27.5)
    reupload_used: bool = False       # the one re-extract and re-upload of an expired file is spent


@dataclass(frozen=True)
class TranscriptJob:
    """The job document `{prefix}drive_transcript_jobs/{user_id}:{item_id}` (§27.3). Holds no URL (§27.4)."""
    user_id: str
    item_id: str
    content_key: str
    content_version: int = 0          # the record's `transcript_version` the job was made for (T-R12)
    duration_s: float = 0.0
    windows: Tuple[JobWindow, ...] = ()
    batch_name: str = ""
    files_expire_at: Optional[datetime] = None
    reserved_usd: float = 0.0         # the file's estimate, reserved the moment it fit (G39)
    reserved_month: str = ""          # "YYYY-MM" the reservation was made in; its cost is booked there (G9)
    transcribed_at: Optional[datetime] = None   # when the last window was collected (T-R12)


@dataclass(frozen=True)
class TranscriptBatch:
    """One provider batch: one user, windows of several files as independent requests (Δ103)."""
    name: str
    user_id: str
    request_keys: Tuple[str, ...] = ()
    submitted_at: Optional[datetime] = None
    last_polled_at: Optional[datetime] = None
    reserved_usd: float = 0.0         # the sum of this submission's reservations (T-R11), not the file's
    reserved_month: str = ""          # "YYYY-MM" of that reservation (G9)
    state: str = "submitted"          # submitted | done | failed; a closed batch has no `next_poll_at`
    created_at: Optional[datetime] = None
    next_poll_at: Optional[datetime] = None   # None = closed; set = open and due then (§27.5)
    kind: str = BATCH_KIND_TRANSCRIBE


def safe_doc_id(raw: str) -> str:
    """A Firestore document id from any string: `%` -> `%25` and `/` -> `%2F` (injective: a `%` in the output always
    starts one of the two escapes or the guard below), and an id Firestore reserves (`.`, `..`, `__x__`) has its first
    character percent-escaped (`%2E`, `%5F`), which no unguarded output can start with."""
    out = raw.replace("%", "%25").replace("/", "%2F")
    if out in (".", "..") or (len(out) >= 4 and out.startswith("__") and out.endswith("__")):
        out = f"%{ord(out[0]):02X}{out[1:]}"
    return out


def job_doc_id(user_id: str, item_id: str) -> str:
    return safe_doc_id(f"{user_id}:{item_id}")


def batch_doc_id(user_id: str, name: str) -> str:
    """A provider batch name is a path (`batches/<id>`); the id carries it escaped."""
    return safe_doc_id(f"{user_id}:{name}")


def transcript_doc_id(user_id: str, content_key: str) -> str:
    """The content key is `quickXorHash:size` (base64, which has `/`) or the fallback `item:{id}:{cTag}`; the raw key
    stays a field of the document."""
    return safe_doc_id(f"{user_id}:{content_key}")


BATCH_CLOSED_STATES: Tuple[str, ...] = ("done", "failed")


def normalized_batch(batch: TranscriptBatch) -> TranscriptBatch:
    """A closed batch (done, failed) has no `next_poll_at`, so it is never polled again (§27.5)."""
    if batch.state in BATCH_CLOSED_STATES and batch.next_poll_at is not None:
        return replace(batch, next_poll_at=None)
    return batch


def url_fields(**fields: str) -> List[str]:
    """Names of the given string fields that hold a URL (start with `http`): §27.4 keeps every URL, a Graph download
    URL or a Gemini file URI, out of any persisted document."""
    return [name for name, value in fields.items() if value.lstrip().lower().startswith("http")]


def job_url_fields(job: TranscriptJob) -> List[str]:
    found = url_fields(content_key=job.content_key, batch_name=job.batch_name, reserved_month=job.reserved_month)
    for w in job.windows:
        found += [f"windows[{w.spec.index}].{n}" for n in
                  url_fields(file_ref=w.file_ref, request_key=w.request_key, batch_name=w.batch_name, state=w.state)]
    return found


def batch_url_fields(batch: TranscriptBatch) -> List[str]:
    found = url_fields(name=batch.name, reserved_month=batch.reserved_month, state=batch.state, kind=batch.kind)
    return found + [f"request_keys[{i}]" for i, k in enumerate(batch.request_keys) if url_fields(k=k)]
