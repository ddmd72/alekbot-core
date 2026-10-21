"""
Content extraction contract (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §9, §24.2-§24.5). Pure.

Public interface:
    ExtractionStatus, ExtractionRequest, ExtractionOutcome, PdfSplitFailed, TriageImage, TriageResult,
    TriageBatchOutcome, DriveModelUnavailable
    PARSER, READER, EXTRACTOR_CHAINS, chain_for(kind) -> Tuple[str, ...]
    garbage_share(text) -> float
    model_route(outcome, kind) -> Optional[str]
    text_digest(text) -> str, failure_label(exc) -> str
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional, Tuple

from .drive_capture import CaptureSignals
from .drive_index import DocumentBrief, TriageVerdict
from .drive_index_params import MODEL_EXTRACT_DENSITY, MODEL_EXTRACT_GARBAGE

PARSER = "parser"   # the child-process parser adapter (§24.3)
READER = "reader"   # the document reader agent (§24.2)
TRANSCRIBE_LINK = "transcribe-gemini"   # the `extracted_by` link of a transcribed audio/video file (§27, G4)

# Which extractor reads which kind, in order; the next link runs only when model_route says so (§9).
EXTRACTOR_CHAINS: Mapping[str, Tuple[str, ...]] = {"pdf": (PARSER, READER), "image": (READER,)}


def chain_for(kind: str) -> Tuple[str, ...]:
    return EXTRACTOR_CHAINS.get(kind, (PARSER,))


class ExtractionStatus(str, Enum):
    TEXT = "text"
    NO_TEXT = "no_text"
    UNSUPPORTED = "unsupported"
    TOO_LARGE = "too_large"
    ENCRYPTED = "encrypted"
    TIMEOUT = "timeout"
    MEMORY = "memory"
    ERROR = "error"
    BUDGET_EXHAUSTED = "budget_exhausted"   # routed to the reader with no monthly pages left: wait, never demote (§24.8)


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


# (cid:N) runs from fonts without a Unicode map, replacement characters, and UTF-8 read as Latin-1 (Ð/Ñ/Ã + a C1 or
# Latin-1 punctuation character) or as CP1251 (Р/С + a CP1251 high-half character). Only CP1251 characters that never
# follow Р or С in real Cyrillic text are listed: Ukrainian і/ї/є/ґ and ё are left out, so "РІК" is not garbage
# (§9 signal 3).
_CP1251_TAIL = ("\u0402\u0403\u201a\u0453\u201e\u2026\u2020\u2021\u20ac\u2030\u0409\u2039\u040a\u040c\u040b"
                "\u040f\u0452\u2018\u2019\u201c\u201d\u2022\u2013\u2014\u2122\u0459\u203a\u045a\u045c\u045b"
                "\u045f\u00b0\u00b1\u00b5\u00b6\u00b7")
_GARBAGE = re.compile(r"\(cid:\d+\)|\ufffd|[\u00d0\u00d1\u00c3][\x80-\xbf]|[\u0420\u0421][" + _CP1251_TAIL + "]")


def text_digest(text: str | bytes) -> str:
    """A short, stable hash of `text`: tells two failures apart in logs without quoting them."""
    data = text.encode("utf-8", errors="replace") if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()[:8]


def failure_label(exc: BaseException) -> str:
    """`Type at file.py:line #hash` — the exception type, the code position that raised it and a hash of its message,
    never the message itself: a parser's message can quote the document it choked on, and these labels reach
    WARNING logs and outcome details (R3 F3-5)."""
    tb = exc.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    where = f" at {os.path.basename(tb.tb_frame.f_code.co_filename)}:{tb.tb_lineno}" if tb is not None else ""
    return f"{type(exc).__name__}{where} #{text_digest(str(exc))}"


def garbage_share(text: str) -> float:
    """Share of characters inside unreadable sequences, over non-space characters."""
    visible = sum(1 for ch in text if not ch.isspace())
    if visible == 0:
        return 0.0
    bad = sum(len(m.group(0)) for m in _GARBAGE.finditer(text))
    return min(1.0, bad / visible)


def model_route(outcome: ExtractionOutcome, kind: str) -> Optional[str]:
    """Why the parser's outcome on this kind goes to the next link (the reader), or None (§9 router). `timeout`
    and `memory` are bombs and never routed; encryption is deterministic and never routed."""
    if READER not in chain_for(kind) or chain_for(kind)[0] == READER:
        return None
    if outcome.status is ExtractionStatus.NO_TEXT:
        return "no_text"
    if outcome.status in (ExtractionStatus.ERROR, ExtractionStatus.UNSUPPORTED):
        return "parser_error"
    if outcome.status is not ExtractionStatus.TEXT:
        return None
    if outcome.pages_total > 0 and len(outcome.text.strip()) / outcome.pages_total < MODEL_EXTRACT_DENSITY:
        return "low_density"
    if garbage_share(outcome.text) > MODEL_EXTRACT_GARBAGE:
        return "garbage_text"
    return None
