"""
The document service's contract (docs/10_rfcs/EXTERNAL_FILES_RFC.md §4.4, D1): what an open hands the service and what
it gets back. Pure, mypy --strict.

Three public classes in one module on purpose (CLAUDE.md's one-class rule is waived here): they are one contract, read
together by the open path, the service and a later remote adapter (precedent `sandbox_contract`). The contract is shaped
as if the service were remote (D1): bytes and plain values in, a closed-schema outcome out, one absolute deadline, and a
`STILL_WORKING` status that the in-process service never returns (D7).

`ReadOutcome` is the RFC's, plus one field, `cut_within_pages`: the 1-based pages of the one window whose text is shown
in part, which D7's note needs ("text cut within pages N+1–N+10"). Every outcome field but `status` has a neutral
default.

Public interface:
    ReadStatus                     the eight per-file `ExtractionStatus` values (same strings) plus four
    ReadRequest                    one file's bytes and how to read them
    ReadOutcome                    what the read gave, raw and unmarked (§4.6)
    OPEN_MARGIN_S, INTERACTIVE_OPEN_S, PREFETCH_OPEN_S     the deadlines (§4.4 "Long scans: Deadline")
    CALLER_OPEN, CALLER_PREFETCH, CALLER_CONNECTOR         `ReadRequest.caller` values
    READER_SKIPPED, reader_skipped(outcome) -> bool         a connector read the reader would have taken (no paid call)
    IN_PROCESS_TEXT_KINDS          the kinds with no converter, read as UTF-8 in the caller (sandbox S-Δ2, A3)
    PARSE_KINDS                    the child readers' kinds that are not in IN_PROCESS_TEXT_KINDS
    TRANSCRIPT_CACHE_DAYS          how long a chat upload's transcript is kept (P21)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, Tuple

from .native_format import NativeFormat

# §4.4 "Deadline": each open keeps its agent's timeout minus this margin, left for presentation.
OPEN_MARGIN_S = 30.0
# FileManagement's agent timeout (120 s, src/composition/user_agent_factory.py, USER_DRIVE_RFC §4.13) minus the margin.
INTERACTIVE_OPEN_S = 120.0 - OPEN_MARGIN_S
# A prefetch in the async worker (P18): under Cloud Tasks' 600 s default dispatch deadline
# (src/adapters/gcp_task_queue.py, `enqueue_worker_task`), with the specialist's own run after it.
PREFETCH_OPEN_S = 270.0

CALLER_OPEN = "open"
CALLER_PREFETCH = "prefetch"
# The drive connector's open (an outside client): never runs the paid model reader. A file the router would hand to
# the reader (a scan, a thin or garbled text layer, a parser error on a PDF; any image) answers NO_TEXT with a
# `READER_SKIPPED` detail instead, and the parser's links. Chat and prefetch never pass it.
CALLER_CONNECTOR = "connector"
READER_SKIPPED = "reader_skipped"

# A3: the kinds that have no converter; reading UTF-8 is not parsing, so the caller reads them in process.
IN_PROCESS_TEXT_KINDS: FrozenSet[str] = frozenset({"text", "markdown", "json", "code", "config", "log"})
# The parser child's reader keys (src/adapters/drive_parser/readers.py `_READERS`) without the in-process kinds. A test
# ties the two: PARSE_KINDS | {"text", "markdown", "json", "config"} == the child's keys.
PARSE_KINDS: FrozenSet[str] = frozenset({
    "pdf", "docx", "pptx", "epub", "msg", "xlsx", "xls", "odt", "ods", "odp", "html", "csv", "subtitles", "rtf",
    "eml",
})

# P21: a chat upload lives 90 days (DriveOperations' not-found line, "files are kept for 90 days"), so its cached
# transcript does too.
TRANSCRIPT_CACHE_DAYS = 90


class ReadStatus(str, Enum):
    """The eight per-file values carry `ExtractionStatus`'s strings, so the status map (§4.5) is one to one."""
    TEXT = "text"
    NO_TEXT = "no_text"
    UNSUPPORTED = "unsupported"
    TOO_LARGE = "too_large"
    ENCRYPTED = "encrypted"
    TIMEOUT = "timeout"
    MEMORY = "memory"
    ERROR = "error"
    NATIVE = "native"                          # attach the bytes you sent, as `native_mime`
    MODEL_UNAVAILABLE = "model_unavailable"    # the reader cannot run now (429, 5xx, open breaker); spend so far reported
    PARSER_UNAVAILABLE = "parser_unavailable"  # detail: unavailable | misconfigured | busy; systemic, not the file's
    STILL_WORKING = "still_working"            # reserved for a remote adapter (D1); the in-process service never returns it


@dataclass(frozen=True)
class ReadRequest:
    """One file, staged by the open path: the service never fetches (P2). Plain values only, so it serializes."""
    content: bytes
    name: str                                  # for log lines and the sandbox's `ext`; never addresses anything
    declared_mime: str
    store: str                                 # a `FileRefKind` value: keys the caps (§4.5)
    caller: str                                # CALLER_OPEN | CALLER_PREFETCH | CALLER_CONNECTOR: keys the deadline,
                                               # the presentation and whether the reader may run
    native_formats: Tuple[NativeFormat, ...]   # what the consumer reads natively (§4.3); empty: text only
    user_id: str                               # the reader bills and configures by these two
    account_id: str
    deadline_at: float                         # one absolute deadline (epoch seconds, `time.time()`), from the open's start
    request_id: str


@dataclass(frozen=True)
class ReadOutcome:
    """What a read gave. `text` is raw and unmarked (§4.6): marking and the presentation cut are the presenter's."""
    status: ReadStatus
    kind: str = ""                             # sniffed
    native_mime: str = ""                      # set when NATIVE
    text: str = ""
    pages_total: int = 0
    pages_read: int = 0                        # pages shown in full (D7)
    skipped_pages: Tuple[int, ...] = ()        # 1-based pages the provider refused or that hit the output limit
    truncated: bool = False
    may_be_incomplete: bool = False
    links: Tuple[str, ...] = ()                # `extracted_by` of every link that ran, e.g. ("parser@3", "reader@2")
    model_calls: int = 0
    model_cost_usd: float = 0.0                # already billed to the account; reported, not booked
    detail: str = ""                           # a failure label, never document text
    cut_within_pages: Tuple[int, ...] = ()     # 1-based pages of the one window whose text is shown in part (D7)


def reader_skipped(outcome: ReadOutcome) -> bool:
    """A connector read the reader would have taken and did not (`CALLER_CONNECTOR`): NO_TEXT, `READER_SKIPPED` detail."""
    return outcome.status is ReadStatus.NO_TEXT and outcome.detail.split(":", 1)[0] == READER_SKIPPED
