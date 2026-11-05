"""
File Conversion Service
=======================

Helpers for file attachments (native-binary and audio checks, history stubs, safe temp names). The parsing itself
is not here: every attachment is read by the document service through the parser child.
"""

import tempfile

from .document_hardening import (  # noqa: F401 — re-exported: the hardening's historical home
    MAX_ZIP_MEMBERS,
    MAX_ZIP_UNCOMPRESSED_BYTES,
    ArchiveTooLargeError,
    check_zip_container,
)
from ..domain.document_limits import MAX_CONVERTED_CHARS
from ..domain.document_limits import MAX_FILE_BYTES  # noqa: F401 — re-exported (tests import it from here)
from ..domain.document_open_texts import conversion_alert as _conversion_alert  # noqa: F401 — re-exported
from ..domain.document_open_texts import cut_note
from ..domain.document_open_texts import size_alert as _size_alert  # noqa: F401 — re-exported
from .logger import logger

HISTORY_PREVIEW_CHARS = 1000         # chars kept in history stub per file

NAME_MAX_BYTES = 255                 # ext4 / overlayfs limit for ONE path component, in bytes
_MAX_KEPT_EXTENSION = 12             # ".markdown" fits; a 300-char "extension" is not one

_NATIVE_BINARY_EXACT = frozenset(["application/pdf"])

_AUDIO_MIME_TYPES = frozenset([
    "audio/mpeg",   # mp3
    "audio/mp3",
    "audio/wav",    # wav
    "audio/x-wav",  # wav as Python's mimetypes names it
    "audio/wave",
    "audio/vnd.wave",
    "audio/mp4",    # m4a (Slack voice messages)
    "audio/x-m4a",  # m4a alt
    "audio/mp4a-latm",  # m4a as Python 3.13's mimetypes names it
    "audio/ogg",    # ogg
    "audio/flac",
    "audio/x-flac",
    "audio/webm",
])
# A ref resolved by name alone can come with no usable MIME type (mimetypes knows no .m4a on 3.11).
_AUDIO_EXTENSIONS = frozenset([".mp3", ".wav", ".m4a", ".ogg", ".oga", ".opus", ".flac", ".webm"])


def is_native_binary(mime_type: str) -> bool:
    """Return True for types that adapters handle natively as binary (image/*, PDF)."""
    return mime_type.startswith("image/") or mime_type in _NATIVE_BINARY_EXACT


def is_audio(mime_type: str) -> bool:
    """Return True for audio MIME types handled via AudioTranscriptionPort."""
    return mime_type in _AUDIO_MIME_TYPES



def safe_temp_suffix(filename: str) -> str:
    """Build a `tempfile` suffix from `filename` that cannot overflow NAME_MAX.

    `tempfile` spends its own budget first (`gettempprefix()` + 8 random chars) and applies the
    suffix verbatim, so an unbounded suffix raises `OSError: [Errno 36] File name too long` —
    which is exactly how a browser-saved page (255-char name) silently lost its attachment on
    2026-07-31. The budget is counted in BYTES, not characters: one Cyrillic character costs 2
    and an emoji 4, so a character-based cap still overflows.

    The leading part of the name is kept (that is what identifies the file to a human reading
    logs) and the extension is preserved when it is plausibly one.
    """
    if not filename:
        return ""

    stem, dot, extension = filename.rpartition(".")
    if not dot or len(extension.encode("utf-8")) > _MAX_KEPT_EXTENSION:
        stem, extension = filename, ""      # no extension, or something too long to be one
    tail = f".{extension}" if extension else ""

    budget = (
        NAME_MAX_BYTES
        - len(tempfile.gettempprefix())
        - 8                                  # tempfile's random component
        - 1                                  # our "_" separator
        - len(tail.encode("utf-8"))
    )
    return "_" + _truncate_bytes(stem, budget) + tail


def _truncate_bytes(text: str, limit: int) -> str:
    """Cut `text` to at most `limit` UTF-8 bytes without splitting a character."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:max(limit, 0)].decode("utf-8", errors="ignore")


def download_alert(filename: str) -> str:
    """System note standing in for an attachment that never reached us.

    Public, unlike its `_size_alert` / `_conversion_alert` siblings (now in `domain/document_open_texts`): the download happens in the
    response channel, one layer above conversion, so ConversationHandler builds this part itself.
    Same contract though — the attachment slot carries an instruction, and the ordinary agent
    tells the user in its own voice instead of the failure being invisible (2026-07-31: an
    over-long filename dropped a file and only the model's guesswork revealed it).
    """
    return (
        f"[System: User attached '{filename}' but the file could not be retrieved from the "
        f"messaging platform, so its content is NOT available in this message. "
        f"Do not guess what it contained. Tell the user the file did not arrive and ask them "
        f"to send it again.]"
    )


def storage_unavailable_alert(filename: str) -> str:
    """System note for an attachment that arrived but cannot be stored or read: the service has no file service.

    Public like `download_alert`: ConversationHandler builds this part itself, in the attachment slot, because the
    upload path always goes through `FileConversionService.process_attachment` and never parses in the handler.
    Same contract: the ordinary agent tells the user in its own voice instead of the failure being invisible.
    """
    return (
        f"[System: User attached '{filename}' and it was received, but it cannot be stored or read "
        f"right now, so its content is NOT available in this message. "
        f"Do not guess what it contained. Tell the user the file could not be processed at the moment "
        f"and ask them to try sending it again later.]"
    )


def transcription_alert(filename: str) -> str:
    """System note for a voice message that arrived but could not be transcribed.

    Public for the same reason as `download_alert`: voice messages are transcribed in
    ConversationHandler, not in `convert_file_to_text`, because their transcript becomes
    the user's turn rather than file content.
    """
    return (
        f"[System: User sent a voice message ('{filename}') but it could not be transcribed, "
        f"so its content is NOT available. Do not guess what was said. Tell the user the "
        f"voice message could not be recognised and ask them to repeat it or write it out.]"
    )


def truncate_with_alert(text: str, filename: str) -> str:
    """Truncate text to MAX_CONVERTED_CHARS and append a system alert if needed."""
    if len(text) <= MAX_CONVERTED_CHARS:
        return text
    omitted = len(text) - MAX_CONVERTED_CHARS
    logger.info(f"[FileConversion] Truncated '{filename}' to {MAX_CONVERTED_CHARS} chars ({omitted} omitted)")
    return text[:MAX_CONVERTED_CHARS] + "\n" + cut_note(filename, MAX_CONVERTED_CHARS, omitted)


def make_history_stub(full_output: str, filename: str) -> str:
    """
    Create a compact stub for session history from convert_file_to_text() output.

    Keeps first HISTORY_PREVIEW_CHARS of content so LLM has a reminder the file
    existed and what it started with — without dragging full content through every
    subsequent turn.

    Short files (content ≤ HISTORY_PREVIEW_CHARS) are returned unchanged.
    """
    lines = full_output.split("\n", 1)
    if len(lines) < 2:
        return full_output  # malformed output, pass through
    rest = lines[1]
    closing = f"\n[/File: {filename}]"
    content = rest[: -len(closing)] if rest.endswith(closing) else rest

    if len(content) <= HISTORY_PREVIEW_CHARS:
        return full_output  # short file — no truncation needed

    preview = content[:HISTORY_PREVIEW_CHARS]
    omitted = len(content) - HISTORY_PREVIEW_CHARS
    return (
        f"[File: {filename}]\n"
        f"{preview}\n"
        f"[...{omitted} chars omitted. Re-upload for full analysis.]\n"
        f"[/File: {filename}]"
    )
