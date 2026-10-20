"""
Transcript value types (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27). Pure data: no I/O, no clock. States and stubs are
plain strings on records and job documents (Δ47's rollback invariant); the enum below only names the allowed stubs.

Several public classes in one module, by decision: one contract (the transcript pipeline) with several value types, as
`drive_enrichment` and `drive_extraction` group theirs (CLAUDE.md's one-class rule is waived here on purpose).
"""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple


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
    once stitched. `speaker` is None for a single speaker."""
    start_s: float
    speaker: Optional[int]
    text: str


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
    """One window inside a job document. `state`: extracted, uploaded, submitted, done or failed (§27.5)."""
    spec: WindowSpec
    state: str = "extracted"
    file_ref: str = ""                # the provider's file handle; never a URL
    request_key: str = ""             # `{item_id}:{index}` in a batch
    attempts: int = 0
    reserved_usd: float = 0.0


@dataclass(frozen=True)
class TranscriptJob:
    """The job document `{prefix}drive_transcript_jobs/{user_id}:{item_id}` (§27.3). Holds no URL (§27.4)."""
    user_id: str
    item_id: str
    content_key: str
    duration_s: float = 0.0
    windows: Tuple[JobWindow, ...] = ()
    batch_name: str = ""
    files_expire_at: Optional[datetime] = None


@dataclass(frozen=True)
class TranscriptBatch:
    """One provider batch: one user, windows of several files as independent requests (Δ103)."""
    name: str
    user_id: str
    request_keys: Tuple[str, ...] = ()
    submitted_at: Optional[datetime] = None
    last_polled_at: Optional[datetime] = None
    reserved_usd: float = 0.0
