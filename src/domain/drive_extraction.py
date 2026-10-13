"""
Content extraction contract (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §9, §24.2-§24.5). Pure.

Public interface:
    ExtractionStatus, ExtractionRequest, ExtractionOutcome, PdfSplitFailed, TriageImage, TriageResult,
    TriageBatchOutcome, DriveModelUnavailable
    PARSER, READER, EXTRACTOR_CHAINS, chain_for(kind) -> Tuple[str, ...]
    garbage_share(text) -> float
    model_route(outcome, kind) -> Optional[str]
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional, Tuple

from .drive_index import DocumentBrief, TriageVerdict
from .drive_index_params import MODEL_EXTRACT_DENSITY, MODEL_EXTRACT_GARBAGE

PARSER = "parser"   # the child-process parser adapter (§24.3)
READER = "reader"   # the document reader agent (§24.2)

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
    pages_refused: int = 0       # PDF pages the provider refused even alone (413/400/422), skipped (R3-P1)


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
    tick counts them against the month and the summary before it records the outage (§24.7, §24.9)."""

    def __init__(self, message: str = "", *, pages_sent: int = 0, images_sent: int = 0, model_calls: int = 0,
                 model_cost: float = 0.0) -> None:
        super().__init__(message)
        self.pages_sent = pages_sent
        self.images_sent = images_sent
        self.model_calls = model_calls
        self.model_cost = model_cost


@dataclass(frozen=True)
class TriageImage:
    item_id: str
    thumbnail: bytes
    mime_type: str
    camera_prior: bool           # photo.takenDateTime or cameraMake present (§5.3): a hint, never a verdict


@dataclass(frozen=True)
class TriageResult:
    item_id: str
    verdict: TriageVerdict
    caption: str = ""            # photographs only


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
