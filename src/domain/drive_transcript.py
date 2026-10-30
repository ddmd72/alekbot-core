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

from .connection_scope import ConnectionScope

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
    duration_known: bool = True       # False: ffprobe said N/A (a browser-recorded webm/mkv, G35); `duration_s` is 0.0


@dataclass(frozen=True)
class WindowSpec:
    """One window of media time: starts at `start_s` of the file's clock and lasts `duration_s`."""
    index: int
    start_s: float
    duration_s: float


class MediaUnreadable(Exception):
    """ffprobe/ffmpeg could not read the file (per-file transient: `extract_error`, §8.2). `detail` carries lengths and
    hashes only, never the URL or ffmpeg's text."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class MediaStatus(str, Enum):
    """How a media child run ended when it was not an unreadable file: mirrors `ExtractionStatus` timeout/memory."""
    OK = "ok"
    TIMEOUT = "timeout"
    MEMORY = "memory"


@dataclass(frozen=True)
class MediaProbeOutcome:
    """`probe` result: `probe` is set only when `status` is OK."""
    status: MediaStatus
    probe: Optional[MediaProbe] = None
    detail: str = ""


@dataclass(frozen=True)
class MediaWindowsOutcome:
    """`extract_windows` result. `windows`: (spec, local path) of every window finished before the wall, in order, each
    with the plan's index and start and its real length; on TIMEOUT a prefix of the request (the caller resumes at the
    first missing one). `eof` is True when the audio ended inside the run (a cue-less pass, G35/T-R15, or a known-duration
    window that came out short because the header duration overstated it, T-R16), and `duration_s` is then the real
    duration; every window carries its MEASURED length; `reached_bound` is True when the pass ended at
    `TRANSCRIBE_MAX_SECONDS` with the file still going (the `too_long` stub)."""
    status: MediaStatus
    windows: Tuple[Tuple[WindowSpec, str], ...] = ()
    duration_s: float = 0.0
    reached_bound: bool = False
    eof: bool = False
    detail: str = ""


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
class WindowInput:
    """One window handed to the transcript runner: its batch request `key`, its place in the file, and the uploaded
    provider file by NAME (`files/<id>`) and mime type. Never a URL (§27.4)."""
    key: str
    window: WindowSpec
    file_name: str
    file_mime: str


class WindowOutcome(str, Enum):
    """How one collected window ended, for the stage to charge (§27.5, T-R18): `ok` parsed; `invalid` the answer was not
    the schema (a failed attempt); `refused` the provider refused the audio (a gap marker); `error` the response held
    its own provider error (`transient` says a later attempt can clear it)."""
    OK = "ok"
    INVALID = "invalid"
    REFUSED = "refused"
    ERROR = "error"


@dataclass(frozen=True)
class WindowResult:
    """One collected window: the parsed `transcript` when `outcome` is ok, `usd` the priced cost of the result (0 when it
    came without usage or its model is unpriced; independent of the account write), `billed` whether that cost reached
    the account."""
    key: str
    outcome: WindowOutcome
    transcript: Optional[WindowTranscript] = None
    transient: bool = False
    usd: float = 0.0
    billed: bool = False
    finish_reason: str = ""           # the provider's finish reason of an `ok`/`invalid` answer ("STOP", "MAX_TOKENS"); T-R27
    code: int = 0                     # an `error` response's rpc code (0 when none): 5/7 is the input file gone (T-R18)


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
    coverage_resubmits: int = 0       # the coverage check's one resubmit of a clean (STOP) short answer (T-R27)
    gap: bool = False                 # done as a gap marker: the provider refused it or the retries ran out
    batch_name: str = ""              # the provider batch carrying it while submitted (T-R12)
    files_expire_at: Optional[datetime] = None   # when the provider's input file expires (§27.5)
    reupload_used: bool = False       # the one re-extract and re-upload of an expired file is spent
    batch_resubmits: int = 0          # the one resubmit after its batch failed, expired or was cancelled whole (§27.5)
    refused: bool = False             # its gap marker stands for a provider refusal (an all-gap file is final only then, T-R23)
    lost_purchases: int = 0           # batches it rode whose outcome is unknown, booked as bought (T-R29): a re-purchase since
    accepted_short: bool = False      # a window over TRANSCRIBE_SHORT_ACCEPT_MAX_S accepted short (T-R31): the transcript is `truncated`


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
    """One provider batch: one user, windows of several files as independent requests (Δ103).

    T-R28: a submission writes its document BEFORE the provider is asked, in state `submitting`, under a name of its
    own (`submitting/<id>`, never a provider name) with the `display_name` the adapter hashes, the request keys and the
    reservation, and the windows are marked in it; the provider's batch name is recorded on it (`provider_name`) the
    moment the call returns, then the document is moved to that name (the batch everybody polls). A call that failed
    ambiguously leaves it `submitting`: the poll looks the provider batch up by its display name (`lookups` counts the
    misses) and either moves it, or after BATCH_SUBMIT_LOOKUPS misses hands its windows and reservation back."""
    name: str
    user_id: str
    request_keys: Tuple[str, ...] = ()
    submitted_at: Optional[datetime] = None
    last_polled_at: Optional[datetime] = None
    reserved_usd: float = 0.0         # the sum of this submission's reservations (T-R11), not the file's
    reserved_month: str = ""          # "YYYY-MM" of that reservation (G9)
    state: str = "submitted"          # submitting | submitted | done | failed; a closed batch has no `next_poll_at`
    created_at: Optional[datetime] = None
    next_poll_at: Optional[datetime] = None   # None = closed; set = open and due then (§27.5)
    kind: str = BATCH_KIND_TRANSCRIBE
    booked_at: Optional[datetime] = None      # when its collected cost was booked: a re-collection books nothing (T-R22)
    display_name: str = ""            # `submitting` only: the name the adapter hashes into the provider's display name (T-R28)
    provider_name: str = ""           # `submitting` only: the provider's batch name, once the submit call returned it (T-R28)
    lookups: int = 0                  # `submitting` only: lookups by display name that found nothing (T-R28)


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


def transcript_job_doc_id(scope: ConnectionScope, item_id: str) -> str:
    """A transcript job's document id, per connection (MULTI_DRIVE_RFC §8): `{connection_id}:{item_id}`, escaped."""
    return safe_doc_id(f"{scope.connection_id}:{item_id}")


def transcript_batch_doc_id(scope: ConnectionScope, name: str) -> str:
    """A transcript batch's document id, per connection (§8): `{connection_id}:{name}`, escaped (the provider's batch
    name is a path)."""
    return safe_doc_id(f"{scope.connection_id}:{name}")


BATCH_CLOSED_STATES: Tuple[str, ...] = ("done", "failed")
BATCH_SUBMITTING = "submitting"       # written before the provider is asked (T-R28); open, polled by its lookup


def normalized_batch(batch: TranscriptBatch) -> TranscriptBatch:
    """A closed batch (done, failed) has no `next_poll_at`, so it is never polled again (§27.5)."""
    if batch.state in BATCH_CLOSED_STATES and batch.next_poll_at is not None:
        return replace(batch, next_poll_at=None)
    return batch


def url_fields(**fields: str) -> List[str]:
    """Names of the given string fields that hold a URL (a `http:` / `https:` scheme or a `://`):
    §27.4 keeps every URL, a Graph download URL or a Gemini file URI, out of any persisted document. A scheme, not the
    letters `http`: a content key is base64 and may begin with them (`:` is not in that alphabet)."""
    return [name for name, value in fields.items() if _is_url(value)]


def _is_url(value: str) -> bool:
    lowered = value.lstrip().lower()
    return lowered.startswith(("http:", "https:")) or "://" in lowered


def job_url_fields(job: TranscriptJob) -> List[str]:
    found = url_fields(content_key=job.content_key, batch_name=job.batch_name, reserved_month=job.reserved_month)
    for w in job.windows:
        found += [f"windows[{w.spec.index}].{n}" for n in
                  url_fields(file_ref=w.file_ref, request_key=w.request_key, batch_name=w.batch_name, state=w.state)]
    return found


def batch_url_fields(batch: TranscriptBatch) -> List[str]:
    found = url_fields(name=batch.name, reserved_month=batch.reserved_month, state=batch.state, kind=batch.kind,
                       display_name=batch.display_name, provider_name=batch.provider_name)
    return found + [f"request_keys[{i}]" for i, k in enumerate(batch.request_keys) if url_fields(k=k)]
