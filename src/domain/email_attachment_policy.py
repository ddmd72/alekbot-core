"""
Email attachment policy — which parts of a mail are noise, which are read as text, and how a read text is cut into
the model's input (email index v2 RFC §3 "Attachments"). Pure, mypy --strict: no I/O, no clock.

Public interface:
    MIN_ATTACHMENT_BYTES, MAX_READ_ATTACHMENTS, ATTACHMENT_HEAD_CHARS, ATTACHMENT_TAIL_CHARS
    PARSE_CONCURRENCY, BUSY_RETRY_DELAYS_S                         the parser's load limits
    NOISE_EXTS, NOISE_MIMES, NOISE_KINDS, MAIL_READ_KINDS, SHEET_KINDS
    declared_kind(part), declared_mime(part)                       what the name and the listing say
    is_noise(part) -> bool, noise_reason(part) -> str              never listed, never downloaded
    skip_before_download(part) -> str                              "" = download; else the skip reason
    sniffed_kind(content, part) -> str                             what the bytes prove
    input_slices(kind, text) -> (head, tail)                       the model-input cut of a read text
"""
from __future__ import annotations

import mimetypes
from typing import FrozenSet, Tuple

from .document_kind import SNIFF_HEAD_BYTES, kind_of, sniff_kind
from .document_limits import parse_input_cap
from .drive_profile import head_and_tail, sheet_head
from .email import EmailPart
from .email_attachment import SKIP_IMAGE, SKIP_TOO_LARGE

MIN_ATTACHMENT_BYTES = 2_048                     # under it: noise
MAX_READ_ATTACHMENTS = 3                         # document-service reads per mail
ATTACHMENT_HEAD_CHARS = 6_000
ATTACHMENT_TAIL_CHARS = 1_500
PARSE_CONCURRENCY = 2                            # parser calls in flight per service instance
BUSY_RETRY_DELAYS_S: Tuple[float, ...] = (2.0, 5.0, 10.0)
NOISE_EXTS: FrozenSet[str] = frozenset({".vcf", ".ics", ".p7s", ".p7m", ".pkpass"})
NOISE_MIMES: FrozenSet[str] = frozenset({
    "text/calendar", "text/vcard", "text/x-vcard", "application/pkcs7-signature", "application/x-pkcs7-signature",
    "application/pkcs7-mime", "application/x-pkcs7-mime", "application/vnd.apple.pkpass", "text/html",
})
NOISE_KINDS: FrozenSet[str] = frozenset({"html", "archive", "audio", "video"})
# No "doc": the parser has no legacy Word reader, so a .doc is listed `skipped:unsupported`.
MAIL_READ_KINDS: FrozenSet[str] = frozenset({"pdf", "docx", "odt", "rtf", "xlsx", "xls", "csv"})
SHEET_KINDS: FrozenSet[str] = frozenset({"xlsx", "xls", "csv"})


def declared_kind(part: EmailPart) -> str:
    return kind_of(part.filename, part.mime, False)


def declared_mime(part: EmailPart) -> str:
    """The open path's rule: the part's own type without parameters, else a guess from the name."""
    own = part.mime.split(";")[0].strip().lower() if part.mime else ""
    return own or (mimetypes.guess_type(part.filename)[0] or "application/octet-stream")


def _extension(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot >= 0 else ""


def noise_reason(part: EmailPart) -> str:
    """Why a part is noise ("" = not noise): inline_image | extension | mime | kind | small. Only the name, the
    declared type and the listed size are known here (no bytes yet). An inline part is dropped only when it is an
    image: an inline document is a document."""
    kind = declared_kind(part)
    if part.displayed_inline and kind == "image":
        return "inline_image"
    if _extension(part.filename) in NOISE_EXTS:
        return "extension"
    if declared_mime(part) in NOISE_MIMES:
        return "mime"
    if kind in NOISE_KINDS:
        return "kind"
    if part.size < MIN_ATTACHMENT_BYTES:
        return "small"
    return ""


def is_noise(part: EmailPart) -> bool:
    return bool(noise_reason(part))


def skip_before_download(part: EmailPart) -> str:
    """"" = download it; else the `skipped:<reason>` the listing alone decides."""
    if declared_kind(part) == "image":
        return SKIP_IMAGE
    if part.size > parse_input_cap("email"):
        return SKIP_TOO_LARGE
    return ""


def sniffed_kind(content: bytes, part: EmailPart) -> str:
    return sniff_kind(content[:SNIFF_HEAD_BYTES], declared_kind(part))


def input_slices(kind: str, text: str) -> Tuple[str, str]:
    """(head, tail) of a read text. A sheet uses the drive's sheet rule (names, headers, first rows) and no tail; any
    other kind the drive's paragraph/sentence cut. No gap marker: the input builder adds it."""
    if kind in SHEET_KINDS:
        return sheet_head(text), ""
    return head_and_tail(text, ATTACHMENT_HEAD_CHARS, ATTACHMENT_TAIL_CHARS)
