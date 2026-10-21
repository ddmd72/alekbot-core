"""
MediaAudioPort — audio-only Opus windows of a drive recording (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.4).

The input is a short-lived download URL, not a file; the implementation reads it through a sandboxed decoder, so the
caller's process never decodes a byte of media. An unreadable file raises `MediaUnreadable` (a per-file transient); a
run cut by the wall or the memory limit is an outcome, mirroring `ExtractionStatus.TIMEOUT` / `MEMORY`.

Public interface:
    MediaAudioPort.probe(url) -> MediaProbeOutcome
    MediaAudioPort.extract_windows(url, windows, out_dir, *, wall_s, ...) -> MediaWindowsOutcome
"""
from abc import ABC, abstractmethod
from typing import Sequence

from src.domain.drive_transcript import MediaProbeOutcome, MediaWindowsOutcome, WindowSpec


class MediaAudioPort(ABC):
    @abstractmethod
    async def probe(self, url: str) -> MediaProbeOutcome:
        """Duration (`duration_known` False when the container has none), audio-stream presence and container.
        Raises MediaUnreadable."""

    @abstractmethod
    async def extract_windows(self, url: str, windows: Sequence[WindowSpec], out_dir: str, *, wall_s: float,
                              container: str, duration_known: bool = True) -> MediaWindowsOutcome:
        """Writes `window_<index>.opus` files into `out_dir` for the planned `windows`, stopping at `wall_s`.
        `container` is the probed format, pinned on the run; empty or invalid raises MediaUnreadable.
        Known duration: one `-ss/-t` run per window. `duration_known=False` (G35, T-R15): `windows` is a contiguous
        slice (at most `MEDIA_PASS_MAX_WINDOWS`) of the standard plan; one remote read of the span, the windows cut from
        it locally, the duration read from the result (`eof`). A slice that is too long or not contiguous raises ValueError
        (a caller bug). Raises MediaUnreadable for an unreadable file, a dropped connection that kept dropping, or a
        limit hit before the first window finished."""
