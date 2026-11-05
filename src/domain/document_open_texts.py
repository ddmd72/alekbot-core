"""
The notes an opened file can come back as (docs/10_rfcs/EXTERNAL_FILES_RFC.md §4.4 "Presentation", §4.3 "Images the
consumer cannot take natively", D7). Pure.

The first four are today's converter notes, moved here unchanged from `src/utils/file_conversion.py` (which imports
them), so a chat file that fails reads as today whichever engine read it. The rest are the document service's new
statuses and D7's line on what a page-wise read shows.

Public interface:
    size_alert(filename, size_bytes, limit_bytes=MAX_FILE_BYTES)   the file is above a byte cap
    conversion_alert(filename, mime_type="")                       the file could not be read as text
    audio_unavailable_note(filename)                               an audio file on a path that does not transcribe
    cut_note(filename, kept, omitted)                              the text was cut to `kept` characters
    still_working_note(name), deadline_note(name)                  not read in time (the same words, D7)
    model_unavailable_note(name), parser_unavailable_note(name)    an outage of the reader or the parser
    image_too_large_note(name), image_format_note(name)            an image the consumer cannot take
    open_failed_note(ref)                                          an unexpected error while opening a file
    storage_read_note(ref)                                         the store could not be read right now
    folder_note(path)                                              a folder ref given where a file is needed
    action_too_large_note(name, size_bytes, limit_bytes, action)   a file above the action cap (save, send; U4b)
    shown_line(name, outcome) -> str                               D7: what a page-wise read shows; "" for nothing
"""
from __future__ import annotations

from typing import List, Sequence

from .document_limits import MAX_FILE_BYTES
from .document_read import ReadOutcome


def size_alert(filename: str, size_bytes: int, limit_bytes: int = MAX_FILE_BYTES) -> str:
    mb = size_bytes / (1024 * 1024)
    return (
        f"[System: User attempted to attach '{filename}' ({mb:.1f} MB). "
        f"File exceeds the {limit_bytes // (1024 * 1024)} MB limit and was not included in this message. "
        f"Ask the user to send a smaller file or paste the content directly.]"
    )


def conversion_alert(filename: str, mime_type: str = "") -> str:
    mime_suffix = f" ({mime_type})" if mime_type else ""
    return (
        f"[System: User attempted to attach '{filename}'{mime_suffix}. "
        f"The file could not be read or is not a supported text format. "
        f"Supported formats: images, PDF, plain text, CSV, DOCX, XLSX, MP3, WAV. "
        f"Ask the user to convert the file or paste the content directly.]"
    )


def audio_unavailable_note(filename: str) -> str:
    return (
        f"[System: User attached an audio file '{filename}'. "
        f"Audio transcription is not available. "
        f"Ask the user to provide a text transcript or describe what they want to discuss about the audio.]"
    )


def cut_note(filename: str, kept: int, omitted: int) -> str:
    """The note after a cut text: only the first `kept` characters were included, `omitted` were not."""
    return (
        f"[System: File '{filename}' was truncated — only the first {kept:,} characters "
        f"were included ({omitted:,} characters omitted). "
        f"Inform the user that the file was too large for full analysis and suggest "
        f"splitting it or pasting only the relevant section.]"
    )


def _not_in_time(name: str) -> str:
    return (f"[System: The file '{name}' could not be read in time; try again. "
            f"Do not guess what it contains.]")


def still_working_note(name: str) -> str:
    """`STILL_WORKING`: no async path (D7), so it reads as a timeout."""
    return _not_in_time(name)


def deadline_note(name: str) -> str:
    """A `TIMEOUT` at the open's deadline."""
    return _not_in_time(name)


def model_unavailable_note(name: str) -> str:
    return (f"[System: The document reader is temporarily unavailable, so '{name}' could not be read; "
            f"try again later. Do not guess what it contains.]")


def parser_unavailable_note(name: str) -> str:
    return (f"[System: The file converter is temporarily unavailable, so '{name}' could not be read; "
            f"ask the user to send it again later. Do not guess what it contains.]")


def image_too_large_note(name: str) -> str:
    return (f"[System: The image '{name}' is too large to view. "
            f"Ask the user to send a smaller image or describe what it shows.]")


def image_format_note(name: str) -> str:
    return (f"[System: The image '{name}' is in a format that cannot be read. "
            f"Ask the user to send it as JPEG or PNG, or describe what it shows.]")


def open_failed_note(ref: str) -> str:
    """An unexpected error while opening a ref: neither the file's fault nor a known outage."""
    return f"[System: The file '{ref}' could not be opened. Do not guess what it contains.]"


def storage_read_note(ref: str) -> str:
    """The file store failed while a stored file was downloaded: the file may be fine, the store is not."""
    return (f"[System: The file '{ref}' could not be read from storage right now; try again later. "
            f"Do not guess what it contains.]")


def folder_note(path: str) -> str:
    """A folder ref given where a file is needed (save, send): the hint to list it."""
    return f"[System: '{path or '/'}' is a folder, not a file. List the folder to see what is inside.]"


_ACTION_WORDS = {"save": "saved to the drive", "send": "sent to the chat"}


def action_too_large_note(name: str, size_bytes: int, limit_bytes: int, action: str) -> str:
    """A file above `document_limits.action_cap()` for a save or a send (EXTERNAL_FILES §4.5 "Bytes for an action").
    `name` is printed as given: the caller escapes a sender's name first."""
    mb = 1024 * 1024
    return (f"[System: '{name}' is {size_bytes / mb:.1f} MB; a file above {limit_bytes // mb} MB cannot be "
            f"{_ACTION_WORDS.get(action, 'used here')}.]")


def _pages(pages: Sequence[int]) -> str:
    ordered = sorted(set(pages))
    if len(ordered) == 1:
        return f"page {ordered[0]}"
    if ordered[-1] - ordered[0] == len(ordered) - 1:
        return f"pages {ordered[0]}–{ordered[-1]}"
    return "pages " + ", ".join(str(page) for page in ordered)


def shown_line(name: str, outcome: ReadOutcome) -> str:
    """D7: what a page-wise read shows, without false precision (the reader's text has no page markers): "pages 1–N
    of M read in full", "text cut within pages a–b", "page k skipped" for each skipped page, and a may-be-incomplete
    remark when set. "" when the outcome reports none of these (a read that was not page-wise)."""
    parts: List[str] = []
    if outcome.pages_read > 0:
        total = f" of {outcome.pages_total}" if outcome.pages_total else ""
        first = "page 1" if outcome.pages_read == 1 else f"pages 1–{outcome.pages_read}"
        parts.append(f"{first}{total} read in full")
    if outcome.cut_within_pages:
        parts.append(f"text cut within {_pages(outcome.cut_within_pages)}")
    parts.extend(f"page {page} skipped" for page in sorted(set(outcome.skipped_pages)))
    if outcome.may_be_incomplete:
        parts.append("the text may be incomplete")
    if not parts:
        return ""
    return f"[System: '{name}': {'; '.join(parts)}.]"
