"""
Content extraction contract of the drive indexer (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §9, §24.2-§24.5). Pure.

The chain table, the statuses and the text-or-scan router are source-neutral and live in `document_extraction`
(EXTERNAL_FILES_RFC §4.5); what stays here carries drive concepts (briefs, capture signals, triage, the fallback cost).

Public interface:
    TRANSCRIBE_LINK
    ExtractionRequest, ExtractionOutcome, PdfSplitFailed, ParserBusy, TriageImage, TriageResult, TriageBatchOutcome,
    DriveModelUnavailable
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from .document_extraction import ExtractionStatus
from .drive_capture import CaptureSignals
from .drive_index import DocumentBrief, TriageVerdict

TRANSCRIBE_LINK = "transcribe-gemini"   # the `extracted_by` link of a transcribed audio/video file (§27, G4)


@dataclass(frozen=True)
class ExtractionRequest:
    """One file on local disk. The caller fields configure and bill the reader; the parser ignores them."""
    path: str
    name: str
    kind: str
    mime_type: str
    user_id: str
    account_id: str
    max_model_pages: int = 0     # pages the reader may still send this month (§24.7); 0: the reader is not called
    item_id: str = ""            # the drive item, for the extractors' log lines only (final review F4-6)
    # The two request limits of an interactive open (EXTERNAL_FILES_RFC §4.4, C, D7). Both can only lower an
    # adapter's own ceilings; the indexer sets neither and keeps today's behaviour.
    deadline_at: Optional[float] = None   # absolute, epoch seconds (`time.time()`), as `PdfSplitterPort`'s
    text_budget: Optional[int] = None     # characters the reader may return; it stops reading once it has them


@dataclass(frozen=True)
class ExtractionOutcome:
    status: ExtractionStatus
    text: str = ""
    pages_total: int = 0         # PDFs: page count; images: 1; other kinds: 0
    pages_sent: int = 0          # pages the reader was sent (billing, monthly budget)
    truncated: bool = False
    brief: Optional[DocumentBrief] = None
    extracted_by: str = ""       # "id@version" of the link that produced `text`
    detail: str = ""             # for the log line only, never shown to the user
    model_calls: int = 0         # reader calls made for this file (tick summary, §24.9)
    model_cost: float = 0.0      # their cost in USD, already billed to the account (§24.9)
    model_fallback_cost: float = 0.0   # of it, what the fallback served (the fallback alert, §25.4.5, XC-5)
    pages_refused: int = 0       # PDF pages the provider refused even alone (413/400/422), skipped (R3-P1)
    # Parser@2 (§25.3): a `text` outcome of an HTML page whose text is under 1 % of its bytes. Its text is then
    # mostly the page's metadata lines; the profile is a `template` (doc_type web_page) and it gets no doc2query.
    script_rendered: bool = False
    # §26.3 (D34): parser@3's capture signals of a PDF (None for other kinds, or when the child could not read them),
    # and the file's capture with its source (`drive_capture.Capture` / `CaptureSource` values; "" for none). The
    # reader sets what its model said (`automatic`); the indexing service settles it against the parser's signals
    # (`drive_capture.settle`: a deterministic answer wins) before the record is written.
    capture_signals: Optional[CaptureSignals] = None
    capture: str = ""
    capture_source: str = ""
    # D7 (EXTERNAL_FILES_RFC §4.4): what a reader run read. `pages_read` counts the pages whose text is in `text` in
    # full; `skipped_pages` lists the 1-based pages refused or cut by the output limit; `cut_within_pages` the 1-based
    # pages of the one window whose text is in part, filled only when the request set `text_budget`.
    pages_read: int = 0
    skipped_pages: Tuple[int, ...] = ()
    cut_within_pages: Tuple[int, ...] = ()

    @property
    def read_by_fallback(self) -> bool:
        """A reader window was served by the fallback model (§26.4's reading problem R): the fallback cost is only
        ever charged for such a window, so it is the one fact the runner already reports."""
        return self.model_fallback_cost > 0


class PdfSplitFailed(Exception):
    """The sandboxed parser could not count or cut the PDF's pages: `status` is `encrypted`, `unsupported`,
    `timeout`, `memory`, `too_large` or `error`, and the reader returns it as its outcome (§24.3). The reader cuts
    one page window at a time, on demand, so at most MODEL_IN_FLIGHT windows exist at once."""

    def __init__(self, status: ExtractionStatus, detail: str = "") -> None:
        super().__init__(f"{status.value}: {detail}" if detail else status.value)
        self.status = status
        self.detail = detail


class ParserBusy(PdfSplitFailed):
    """The parser's slot did not come free before the request's deadline (EXTERNAL_FILES_RFC §4.4 "Parser slot"):
    status `timeout`, detail `busy`. Raised by the parser adapter only when a deadline was given (a request's
    `deadline_at` or a page op's) and it passed while waiting for the slot; it is systemic, not the file's (the
    document service maps it to `PARSER_UNAVAILABLE(busy)`). A `PdfSplitFailed`, so the reader already handles it; the
    indexer never sets a deadline, so it never sees it."""

    def __init__(self) -> None:
        super().__init__(ExtractionStatus.TIMEOUT, "busy")


class DriveModelUnavailable(Exception):
    """The reader or the triage model cannot be used now (429, 5xx, timeout, a missing prompt): a model outage,
    handled by deferring model work only (§24.8). It carries what the file or batch spent before the outage — the
    pages (reader) or images (triage) the model answered, the calls made and their cost, already billed — so the
    tick counts them against the month and the summary before it records the outage (§24.7, §24.8).

    `charges_file` (final review F1-1, arbiter R3-x2-a): the failure may be this file's — a provider 5xx, a timeout of
    a window that cannot be halved, an unexpected adapter error — so a reader outage that made a call for the file is
    charged to it. False for what is the model's for everyone: a rate limit (429), an account-level error (key,
    permission, model id, quota), a network failure, an open breaker."""

    def __init__(self, message: str = "", *, pages_sent: int = 0, images_sent: int = 0, model_calls: int = 0,
                 model_cost: float = 0.0, model_fallback_cost: float = 0.0, charges_file: bool = True) -> None:
        super().__init__(message)
        self.charges_file = charges_file
        self.pages_sent = pages_sent
        self.images_sent = images_sent
        self.model_calls = model_calls
        self.model_cost = model_cost
        self.model_fallback_cost = model_fallback_cost   # the fallback's share of model_cost (XC-5)


@dataclass(frozen=True)
class TriageImage:
    item_id: str
    thumbnail: bytes
    mime_type: str
    camera_prior: bool           # photo.takenDateTime or cameraMake present (§5.3): a hint, never a verdict
    place_name: str = ""         # D39: where the phone was ("Rome (Roma), Italy"), from the record; "" when none
    location: Optional[Tuple[float, float]] = None   # D39: latitude, longitude ALREADY rounded to 3 decimals


@dataclass(frozen=True)
class TriageResult:
    item_id: str
    verdict: TriageVerdict
    caption: str = ""            # photographs only
    capture: str = ""            # §26.3: what triage said of a text image (a `Capture` value); "" for a photograph


@dataclass(frozen=True)
class TriageBatchOutcome:
    """One triage call's answer: one result per image answered (an image whose call failed has none, for the
    caller's per-file retry), and what the model work cost (tick summary, §24.9)."""
    results: Tuple[TriageResult, ...]
    model_calls: int = 0         # triage calls made for this batch (a refused batch is split, §24.5)
    model_cost: float = 0.0      # their cost in USD, already billed to the account
