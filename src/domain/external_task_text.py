"""Text that enters or leaves the external task queue, made safe to store (EXTERNAL_TASK_QUEUE_RFC §5.9, §5.1).

Pure functions only. A worker's result and failure reason are hostile by assumption; the task's title and text are
written by Smart in a turn that may quote outside content. All of them go through the same normalisation before they
are stored:

- Unicode NFC;
- control characters stripped, except newline and tab (a CR is dropped, so CRLF becomes LF);
- invisible format characters stripped (category Cf: bidi overrides and isolates, zero-width characters, the BOM) and
  lone surrogates;
- the system tag word neutralised (`neutralize_system_tag`), so stored text can never forge a system note.

A result is refused, never repaired: invalid UTF-8, nothing left after normalisation, or more than `RESULT_MAX_BYTES`.
A failure reason is stored capped (truncated to `FAIL_REASON_MAX_CHARS`). A title or a task text over its cap is
refused, so the owner's receipt never cuts what leaves the system.

Public interface:
    normalise_text(text) -> str
    decode_result(raw) -> str                        raises ExternalTaskRefused(invalid_text)
    prepare_result(text) -> PreparedResult           raises ExternalTaskRefused(too_large | invalid_text)
    prepare_reason(text) -> Optional[str]
    prepare_title(text) -> str;  prepare_task_text(text) -> str;  prepare_description(text) -> Optional[str]
    is_valid_label(label) -> bool
"""
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

from .external_task import (
    DESCRIPTION_MAX_CHARS,
    FAIL_REASON_MAX_CHARS,
    LABEL_MAX_CHARS,
    LABEL_PATTERN,
    RESULT_MAX_BYTES,
    TASK_TEXT_MAX_CHARS,
    TITLE_MAX_CHARS,
)
from .external_task_refusal import ExternalTaskRefused, TaskRefusal
from .untrusted_text import neutralize_system_tag

_KEPT_CONTROLS = frozenset("\n\t")
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})
_LABEL = re.compile(LABEL_PATTERN)
_WHITESPACE_RUN = re.compile(r"\s+")


@dataclass(frozen=True)
class PreparedResult:
    """A result ready to store: the normalised text, its UTF-8 size and the sha256 hex of those bytes."""
    text: str
    bytes: int
    sha256: str


def normalise_text(text: str) -> str:
    """NFC, controls (but newline and tab) and invisible format characters stripped, system tag word neutralised."""
    composed = unicodedata.normalize("NFC", text)
    kept = "".join(ch for ch in composed
                   if ch in _KEPT_CONTROLS or unicodedata.category(ch) not in _DROPPED_CATEGORIES)
    return neutralize_system_tag(kept)[0]


def decode_result(raw: bytes) -> str:
    """A submitted body as text; invalid UTF-8 is refused, not repaired."""
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ExternalTaskRefused(TaskRefusal.INVALID_TEXT) from exc


def prepare_result(text: str) -> PreparedResult:
    """The stored form of a result. Over the cap (checked before and after normalisation) is `too_large`; nothing left
    but whitespace is `invalid_text`."""
    if len(text) > RESULT_MAX_BYTES:     # every character is at least one byte: an early refusal of an oversized input
        raise ExternalTaskRefused(TaskRefusal.TOO_LARGE)
    clean = normalise_text(text)
    if not clean.strip():
        raise ExternalTaskRefused(TaskRefusal.INVALID_TEXT)
    data = clean.encode("utf-8")
    if len(data) > RESULT_MAX_BYTES:
        raise ExternalTaskRefused(TaskRefusal.TOO_LARGE)
    return PreparedResult(text=clean, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())


def prepare_reason(text: Optional[str]) -> Optional[str]:
    """A worker's failure reason, normalised, stripped and truncated to the cap; None when nothing is left."""
    if text is None:
        return None
    clean = normalise_text(text[: FAIL_REASON_MAX_CHARS * 4]).strip()[:FAIL_REASON_MAX_CHARS]
    return clean or None


def _one_line(text: str) -> str:
    return _WHITESPACE_RUN.sub(" ", normalise_text(text)).strip()


def prepare_title(text: str) -> str:
    """A task title: one line, at most `TITLE_MAX_CHARS`; empty or over the cap is refused."""
    title = _one_line(text)
    if not title:
        raise ExternalTaskRefused(TaskRefusal.INVALID_TEXT)
    if len(title) > TITLE_MAX_CHARS:
        raise ExternalTaskRefused(TaskRefusal.TOO_LARGE)
    return title


def prepare_task_text(text: str) -> str:
    """A task's full brief, at most `TASK_TEXT_MAX_CHARS`; empty or over the cap is refused, never cut."""
    brief = normalise_text(text).strip()
    if not brief:
        raise ExternalTaskRefused(TaskRefusal.INVALID_TEXT)
    if len(brief) > TASK_TEXT_MAX_CHARS:
        raise ExternalTaskRefused(TaskRefusal.TOO_LARGE)
    return brief


def prepare_description(text: Optional[str]) -> Optional[str]:
    """A worker's optional one-line description, at most `DESCRIPTION_MAX_CHARS`; over the cap is refused."""
    if text is None:
        return None
    line = _one_line(text)
    if len(line) > DESCRIPTION_MAX_CHARS:
        raise ExternalTaskRefused(TaskRefusal.TOO_LARGE)
    return line or None


def is_valid_label(label: str) -> bool:
    """A label as stored: `LABEL_PATTERN` in full, at most `LABEL_MAX_CHARS`. Nothing is normalised."""
    return 0 < len(label) <= LABEL_MAX_CHARS and _LABEL.fullmatch(label) is not None
