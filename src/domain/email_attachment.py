"""
Email attachment outcome — what the email index v2 learned about one LISTED file of a mail (docs: email index v2 RFC
§2.1 "Attachments", §3). Pure, mypy --strict.

Two public types in one module on purpose (CLAUDE.md's one-class rule is waived here): the enum and the record are one
contract, read together by the attachment service and the card core (precedent `document_read`).

Public interface:
    AttachmentRead                 how a listed file was read: text | read_by_reader | scan_not_read | locked | skipped
    SKIP_* / SKIP_REASONS          the closed set of `skipped:<reason>` reasons
    EmailAttachment                one listed file and its outcome; `.outcome` is the stored `read` value
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, Optional


class AttachmentRead(str, Enum):
    TEXT = "text"                      # read; text below
    READ_BY_READER = "read_by_reader"  # reserved for a later slice (scans); never produced yet
    SCAN_NOT_READ = "scan_not_read"    # a PDF with no usable text layer (the router would hand it to the reader)
    LOCKED = "locked"                  # ENCRYPTED: password-protected; no retry, no failure count
    SKIPPED = "skipped"                # see `EmailAttachment.reason`


SKIP_IMAGE = "image"
SKIP_TOO_LARGE = "too_large"
SKIP_OVER_CAP = "over_cap"
SKIP_UNSUPPORTED = "unsupported"
SKIP_NO_TEXT = "no_text"
SKIP_UNREADABLE = "unreadable"
SKIP_TIMEOUT = "timeout"
SKIP_EMPTY = "empty"
SKIP_DUPLICATE = "duplicate"
SKIP_REASONS: FrozenSet[str] = frozenset({
    SKIP_IMAGE, SKIP_TOO_LARGE, SKIP_OVER_CAP, SKIP_UNSUPPORTED, SKIP_NO_TEXT, SKIP_UNREADABLE, SKIP_TIMEOUT,
    SKIP_EMPTY, SKIP_DUPLICATE,
})


@dataclass(frozen=True)
class EmailAttachment:
    """One LISTED file of a mail (noise parts are never listed). `text`, `head`, `tail` live in memory only:
    bodies and document text are never stored."""
    name: str                    # as the sender wrote it (EmailPart.filename)
    kind: str                    # sniffed from the bytes when downloaded; else the declared kind
    size: int                    # EmailPart.size (bytes)
    read: AttachmentRead
    reason: str = ""             # SKIPPED only, one of SKIP_REASONS; "" otherwise
    text: Optional[str] = None   # TEXT only: the document service's full raw text = grading evidence
    head: str = ""               # TEXT only: model input head
    tail: str = ""               # TEXT only: model input tail ("" when the text fits or for sheets)

    def __post_init__(self) -> None:
        if self.read is AttachmentRead.SKIPPED:
            if self.reason not in SKIP_REASONS:
                raise ValueError(f"a skipped attachment needs one of {sorted(SKIP_REASONS)}, got {self.reason!r}")
        elif self.reason:
            raise ValueError("reason is only for a skipped attachment")
        if self.text is not None and self.read is not AttachmentRead.TEXT:
            raise ValueError("text is only for a read attachment")

    @property
    def outcome(self) -> str:
        """The stored `read` value: the enum value, or `skipped:<reason>`."""
        if self.read is AttachmentRead.SKIPPED:
            return f"skipped:{self.reason}"
        return str(self.read.value)
