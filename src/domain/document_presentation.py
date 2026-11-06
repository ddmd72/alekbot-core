"""
What the model gets for a read (docs/10_rfcs/EXTERNAL_FILES_RFC.md §4.4 "Presentation", §4.5 caps, §4.6 marking, D7,
D20). Pure.

`note_for` turns a status into its `[Tool note: …]` note. Every per-file failure (NO_TEXT, UNSUPPORTED, ENCRYPTED, ERROR,
MEMORY and a TIMEOUT that is not the open's deadline) gives today's conversion alert, so a chat file that fails reads
as today; TOO_LARGE gives today's size alert. An image the consumer cannot take gets one of the two image notes (too
large, unreadable format) and an audio file left unsupported today's audio note.

`present_text` presents read text: the tag's reminder, the text marked by `wrap_untrusted` and cut at a paragraph to
the store's presentation cap (D20), then OUTSIDE the wrapper the cut note and D7's line on what was shown. Drive opens
keep their own presentation in `DriveOperations` (§29's bound and cut line); this is the other stores'.

Public interface:
    note_for(outcome, name, declared_mime, *, size_bytes=0, limit_bytes=MAX_FILE_BYTES) -> str
    present_text(body, *, store, ref, label, nonce, outcome) -> str
"""
from __future__ import annotations

from typing import List, Mapping

from .document_limits import MAX_FILE_BYTES, presentation_cap
from .document_open_texts import (
    audio_unavailable_note, conversion_alert, cut_note, deadline_note, image_format_note, image_too_large_note,
    model_unavailable_note, parser_unavailable_note, shown_line, size_alert, still_working_note,
)
from .document_read import ReadOutcome, ReadStatus
from .drive_open_cut import head_at_paragraph
from .untrusted_text import DRIVE_TAG, EXTERNAL_REMINDER, EXTERNAL_TAG, REFERENCE_REMINDER, tag_for, wrap_untrusted

# The detail of a TIMEOUT that hit the open's deadline (the parser's and the reader's label for it).
_DEADLINE = "deadline"

_REMINDERS: Mapping[str, str] = {DRIVE_TAG: REFERENCE_REMINDER, EXTERNAL_TAG: EXTERNAL_REMINDER}


def _is_image(outcome: ReadOutcome, declared_mime: str) -> bool:
    return outcome.kind == "image" or (not outcome.kind and declared_mime.startswith("image/"))


def _is_audio(outcome: ReadOutcome, declared_mime: str) -> bool:
    return outcome.kind == "audio" or (not outcome.kind and declared_mime.startswith("audio/"))


def note_for(outcome: ReadOutcome, name: str, declared_mime: str, *, size_bytes: int = 0,
             limit_bytes: int = MAX_FILE_BYTES) -> str:
    """The `[Tool note: …]` note of an outcome that carries no text to show. `size_bytes` and `limit_bytes` word the
    size alert of a TOO_LARGE file (the open path knows both; the outcome carries neither). TEXT and NATIVE are not
    notes: asking for one is a caller bug (ValueError)."""
    status = outcome.status
    if status in (ReadStatus.TEXT, ReadStatus.NATIVE):
        raise ValueError(f"a {status.value} outcome is presented, not noted")
    if status is ReadStatus.STILL_WORKING:
        return still_working_note(name)
    if status is ReadStatus.MODEL_UNAVAILABLE:
        return model_unavailable_note(name)
    if status is ReadStatus.PARSER_UNAVAILABLE:
        return parser_unavailable_note(name)
    if status is ReadStatus.TIMEOUT and outcome.detail == _DEADLINE:
        return deadline_note(name)
    if _is_image(outcome, declared_mime):
        if status is ReadStatus.TOO_LARGE:
            return image_too_large_note(name)
        if status is ReadStatus.UNSUPPORTED:
            return image_format_note(name)
    if status is ReadStatus.UNSUPPORTED and _is_audio(outcome, declared_mime):
        return audio_unavailable_note(name)
    if status is ReadStatus.TOO_LARGE:
        return size_alert(name, size_bytes, limit_bytes)
    return conversion_alert(name, declared_mime)


def present_text(body: str, *, store: str, ref: str, label: str, nonce: str, outcome: ReadOutcome) -> str:
    """Read text as the model gets it: the store tag's reminder, then `body` marked (§4.6) and cut at a paragraph to
    `presentation_cap(store)` (D20), then, outside the wrapper, the cut note when it was cut and D7's shown line when
    the read was page-wise. Skill and unknown stores are refused (ValueError), as `tag_for`."""
    reminder = _REMINDERS[tag_for(store)]
    cap = presentation_cap(store)
    head = body if len(body) <= cap else head_at_paragraph(body, cap)
    lines: List[str] = [reminder, wrap_untrusted(head, nonce=nonce, store=store, ref=ref, label=label)]
    if len(head) < len(body):
        lines.append(cut_note(label, len(head), len(body) - len(head)))
    shown = shown_line(label, outcome)
    if shown:
        lines.append(shown)
    return "\n".join(lines)
