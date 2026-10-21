"""
BatchTranscriptionPort — hours-latency batch calls to a multimodal model (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.5).

Audio windows are uploaded once, referenced by name from the requests of one batch, and the answers are collected per
request key. Generic enough for text-only requests (no file). Provider outages and account problems raise
`LLMUnavailableError` / `LLMRateLimitError` / `LLMAccountError` (an uncharged outage for the caller); a request the
provider refuses or fails individually is that request's `BatchResult`, never an exception.

Public interface:
    BatchTranscriptionPort.upload_audio(path, mime) -> FileRef
    BatchTranscriptionPort.submit(requests, *, display_name) -> BatchRef
    BatchTranscriptionPort.find_batch(display_name) -> Optional[BatchRef]
    BatchTranscriptionPort.poll(batch) -> BatchStatus
    BatchTranscriptionPort.collect(batch) -> Mapping[str, BatchResult]
    BatchTranscriptionPort.delete_file(ref) -> None
"""
from abc import ABC, abstractmethod
from typing import Mapping, Optional, Sequence

from src.domain.batch_transcription import BatchRef, BatchRequest, BatchResult, BatchStatus, FileRef


class BatchTranscriptionPort(ABC):
    @abstractmethod
    async def upload_audio(self, path: str, mime: str) -> FileRef:
        """Uploads one audio file to the provider's file store."""

    @abstractmethod
    async def submit(self, requests: Sequence[BatchRequest], *, display_name: str) -> BatchRef:
        """Creates one batch job from `requests`. `display_name` is hashed by the adapter: the provider never sees it."""

    @abstractmethod
    async def find_batch(self, display_name: str) -> Optional[BatchRef]:
        """The provider's batch job that `submit` created with this `display_name` (matched by the adapter's hash of
        it, never by the raw argument), or None when the provider lists no such job (T-R28: a submit call that failed
        ambiguously — a timeout, a 5xx, a dropped connection — may still have created it). The returned ref has no
        `keys`; the caller has them. Outages raise as every call does; a scan has a bound, past which it is None."""

    @abstractmethod
    async def poll(self, batch: BatchRef) -> BatchStatus:
        """The job's current state."""

    @abstractmethod
    async def collect(self, batch: BatchRef) -> Mapping[str, BatchResult]:
        """The results of a succeeded job, one per request key (a missing or mismatched answer is an error result).
        Call it only after `poll` returned SUCCEEDED: a job in any other state, or a `batch` without `keys`, raises
        ValueError (a caller bug, not an outage). A per-response error never raises; it is that key's result."""

    @abstractmethod
    async def delete_file(self, ref: FileRef) -> None:
        """Deletes an uploaded file; an already-gone file is not an error."""
