"""
PdfSplitterPort — a PDF's page operations, run where the PDF is parsed (docs/10_rfcs/EXTERNAL_FILES_RFC.md §4.4,
§4.5; USER_DRIVE_INDEXING_RFC §24.3).

The document reader counts a routed PDF's pages and reads it in page windows; it never parses the PDF itself. The
sandboxed parser (`SubprocessContentExtractor` today, the sandbox's parse engine later) does both.

Every method raises `drive_extraction.PdfSplitFailed` with the outcome status (`encrypted`, `unsupported`, `timeout`,
`memory`, `too_large` or `error`). `deadline_at` is an absolute time in epoch seconds (`time.time()`), so it survives
serialization; it can only lower the adapter's own wall clock, never raise it. A deadline already passed raises
`PdfSplitFailed(TIMEOUT, detail="deadline")` and starts no work; one that passes while the call waits for the parser's
slot raises its subclass `drive_extraction.ParserBusy` (TIMEOUT, detail "busy"). Without it, the adapter's own wall
applies alone.
"""
from abc import ABC, abstractmethod
from typing import Optional, Sequence, Tuple


class PdfSplitterPort(ABC):

    @abstractmethod
    async def pdf_page_count(self, path: str, *, deadline_at: Optional[float] = None) -> int:
        """Pages of the PDF at `path`."""

    @abstractmethod
    async def cut_pdf_window(self, path: str, first_page: int, page_count: int, out_path: str,
                             *, deadline_at: Optional[float] = None) -> None:
        """One window PDF at `out_path`: pages [first_page, first_page + page_count), 0-based, clamped to the end.
        The caller owns `out_path` and removes it."""

    @abstractmethod
    async def cut_pdf_windows(self, path: str, windows: Sequence[Tuple[int, int]], out_paths: Sequence[str],
                              *, deadline_at: Optional[float] = None) -> None:
        """Several windows, `(first_page, page_count)` each, written to the matching `out_paths` (same length), all
        under the one deadline. All or nothing: when any window fails, no window file is left."""
