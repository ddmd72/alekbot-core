"""
The extraction chain table and the text-or-scan router (docs/10_rfcs/EXTERNAL_FILES_RFC.md §4.5; the rules are
docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §9, §24.2-§24.3). Pure and source-neutral: the drive indexer and the document
service read the same chains, statuses and routing. The router's two thresholds stay drive parameters
(`drive_index_params`) and are imported.

Public interface:
    PARSER, PARSER_VERSION, READER, EXTRACTOR_CHAINS, chain_for(kind) -> Tuple[str, ...]
    PARSER_VERSION_UNREPORTED, READER_FAMILIES, reported_identity(result) -> Tuple[str, str]
    ExtractionStatus
    text_digest(text) -> str, failure_label(exc) -> str
    garbage_share(text) -> float
    model_route(outcome, kind) -> Optional[str]
"""
from __future__ import annotations

import hashlib
import os
import re
from enum import Enum
from typing import Any, FrozenSet, Mapping, Optional, Protocol, Tuple

from .drive_index_params import MODEL_EXTRACT_DENSITY, MODEL_EXTRACT_GARBAGE

PARSER = "parser"   # the child-process parser adapter (§24.3)
# 2: parser@2's HTML policy (§25.3, D23); 3: a PDF's capture signals (§26.3, D34);
# 4: U5's reader fixes; 5: PDF table pages no longer repeat the table's edge rows as loose text. A bump re-extracts every
# parser-decided record (§25.4.8). Every parser adapter (in-process and sandbox) reports it, so switching adapters
# never re-extracts the drive index.
PARSER_VERSION = 5
# U5 (A-U5-2): the parser child reports the PARSER_VERSION it ran (`parser_version` in its extract result) and the
# adapters stamp `extracted_by` from that report, never from their own constant, so a record never carries one
# version's stamp over another version's text. A result without the key comes from a child older than the report:
# version 3, the last version that did not report itself.
PARSER_VERSION_UNREPORTED = 3
# U5: the reader inside the parser that produced a text (`reader_family` of the outcome and the record). "" is unknown
# or not the parser (the reader agent, transcripts, a record read before U5). `text_html` is a text or Markdown file
# read through the HTML converter (§25.3); `epub_package` an Apple Books tar package repacked as an EPUB. A test ties
# the set to the readers' table (`drive_parser/readers.py`).
READER_FAMILIES: FrozenSet[str] = frozenset({
    "pdf", "docx", "pptx", "epub", "epub_package", "msg", "xlsx", "xls", "odf", "html", "text", "text_html", "csv",
    "subtitles", "rtf", "eml",
})
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


class _ParsedText(Protocol):
    """What the router reads of a parser outcome (the drive's `ExtractionOutcome` is one): this module cannot import
    it, the drive module imports `ExtractionStatus` from here."""

    @property
    def status(self) -> ExtractionStatus: ...

    @property
    def text(self) -> str: ...

    @property
    def pages_total(self) -> int: ...


# (cid:N) runs from fonts without a Unicode map, replacement characters, and UTF-8 read as Latin-1 (Ð/Ñ/Ã + a C1 or
# Latin-1 punctuation character) or as CP1251 (Р/С + a CP1251 high-half character). Only CP1251 characters that never
# follow Р or С in real Cyrillic text are listed: Ukrainian і/ї/є/ґ and ё are left out, so "РІК" is not garbage
# (§9 signal 3).
_CP1251_TAIL = ("ЂЃ‚ѓ„…†‡€‰Љ‹ЊЌЋ"
                "Џђ‘’“”•–—™љ›њќћ"
                "џ°±µ¶·")
_GARBAGE = re.compile(r"\(cid:\d+\)|�|[ÐÑÃ][\x80-\xbf]|[РС][" + _CP1251_TAIL + "]")


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


def reported_identity(result: Mapping[str, Any]) -> Tuple[str, str]:
    """(`extracted_by`, `reader_family`) of a parser child's extract result, from what the child reported: `parser@N`
    with N its `parser_version` (PARSER_VERSION_UNREPORTED when absent), and its `reader_family` ("" when absent or
    not a family this code knows). ValueError when either key is present with a bad type or a version below 1: the
    child is untrusted, the caller maps that to an invalid result."""
    version = result.get("parser_version", PARSER_VERSION_UNREPORTED)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("bad parser_version")
    family = result.get("reader_family", "")
    if not isinstance(family, str):
        raise ValueError("bad reader_family")
    return f"{PARSER}@{version}", family if family in READER_FAMILIES else ""


def garbage_share(text: str) -> float:
    """Share of characters inside unreadable sequences, over non-space characters."""
    visible = sum(1 for ch in text if not ch.isspace())
    if visible == 0:
        return 0.0
    bad = sum(len(m.group(0)) for m in _GARBAGE.finditer(text))
    return min(1.0, bad / visible)


def model_route(outcome: _ParsedText, kind: str) -> Optional[str]:
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
