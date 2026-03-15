"""
File Conversion Service
=======================

Converts non-binary file attachments (CSV, TXT, MD, DOCX, XLSX, MP3, WAV, …)
to plain text using markitdown before they reach LLM adapters.

This keeps conversion logic LLM-agnostic: text output works for Claude, Gemini,
and Grok without any adapter changes. Images and PDFs bypass this service entirely
and are handled natively by each adapter.
"""

import asyncio
import os
from typing import TYPE_CHECKING, Optional

from .logger import logger

if TYPE_CHECKING:
    from ..ports.audio_transcription_port import AudioTranscriptionPort

MAX_FILE_BYTES = 5 * 1024 * 1024    # 5 MB — context window would explode beyond this
MAX_CONVERTED_CHARS = 30_000         # ~7K tokens — hard truncation to protect context
HISTORY_PREVIEW_CHARS = 1000         # chars kept in history stub per file

_NATIVE_BINARY_EXACT = frozenset(["application/pdf"])

_AUDIO_MIME_TYPES = frozenset([
    "audio/mpeg",   # mp3
    "audio/wav",    # wav
    "audio/mp4",    # m4a (Slack voice messages)
    "audio/x-m4a",  # m4a alt
    "audio/ogg",    # ogg
])


def is_native_binary(mime_type: str) -> bool:
    """Return True for types that adapters handle natively as binary (image/*, PDF)."""
    return mime_type.startswith("image/") or mime_type in _NATIVE_BINARY_EXACT


def is_audio(mime_type: str) -> bool:
    """Return True for audio MIME types handled via AudioTranscriptionPort."""
    return mime_type in _AUDIO_MIME_TYPES



def _size_alert(filename: str, size_bytes: int) -> str:
    mb = size_bytes / (1024 * 1024)
    return (
        f"[System: User attempted to attach '{filename}' ({mb:.1f} MB). "
        f"File exceeds the 5 MB limit and was not included in this message. "
        f"Ask the user to send a smaller file or paste the content directly.]"
    )


def _conversion_alert(filename: str, mime_type: str = "") -> str:
    mime_suffix = f" ({mime_type})" if mime_type else ""
    return (
        f"[System: User attempted to attach '{filename}'{mime_suffix}. "
        f"The file could not be read or is not a supported text format. "
        f"Supported formats: images, PDF, plain text, CSV, DOCX, XLSX, MP3, WAV. "
        f"Ask the user to convert the file or paste the content directly.]"
    )


def truncate_with_alert(text: str, filename: str) -> str:
    """Truncate text to MAX_CONVERTED_CHARS and append a system alert if needed."""
    if len(text) <= MAX_CONVERTED_CHARS:
        return text
    omitted = len(text) - MAX_CONVERTED_CHARS
    logger.info(f"[FileConversion] Truncated '{filename}' to {MAX_CONVERTED_CHARS} chars ({omitted} omitted)")
    return (
        text[:MAX_CONVERTED_CHARS]
        + f"\n[System: File '{filename}' was truncated — only the first {MAX_CONVERTED_CHARS:,} characters "
        f"were included ({omitted:,} characters omitted). "
        f"Inform the user that the file was too large for full analysis and suggest "
        f"splitting it or pasting only the relevant section.]"
    )


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


def _is_plain_text(mime_type: str) -> bool:
    """MIME types readable as UTF-8 without any conversion library."""
    return mime_type.startswith("text/")



async def convert_file_to_text(
    local_path: str,
    filename: str,
    mime_type: str,
    audio_service: Optional["AudioTranscriptionPort"] = None,
) -> str:
    """
    Convert a file to a text string.

    - audio/*: AudioTranscriptionPort (injected) — mp3, wav, m4a, ogg
    - text/* (txt, csv, md, …): read directly as UTF-8 — no library needed
    - everything else: markitdown (lazy import) — covers docx, xlsx, etc.

    Returns extracted text wrapped in [File: filename]...[/File: filename],
    or a [System: ...] alert on failure.
    """
    size = os.path.getsize(local_path)
    if size > MAX_FILE_BYTES:
        logger.warning(
            f"[FileConversion] Rejecting oversized file: {filename} ({size / 1024 / 1024:.1f} MB)"
        )
        return _size_alert(filename, size)

    # Audio path — via injected AudioTranscriptionPort
    if is_audio(mime_type):
        if audio_service is None:
            logger.warning(f"[FileConversion] No audio_service for '{filename}' ({mime_type})")
            return (
                f"[System: User attached an audio file '{filename}'. "
                f"Audio transcription is not available. "
                f"Ask the user to provide a text transcript or describe what they want to discuss about the audio.]"
            )
        try:
            text = await audio_service.transcribe(local_path, mime_type)
            logger.info(f"[FileConversion] Transcribed audio '{filename}': {len(text)} chars")
        except Exception as e:
            logger.warning(f"[FileConversion] Audio transcription failed for '{filename}': {type(e).__name__}: {e}")
            return _conversion_alert(filename, mime_type)
    elif _is_plain_text(mime_type):
        # Fast path: no dependencies, just read bytes as UTF-8
        try:
            def _read() -> str:
                with open(local_path, "r", encoding="utf-8", errors="replace") as f:
                    return f.read()
            text = await asyncio.to_thread(_read)
            logger.info(f"[FileConversion] Read plain text '{filename}': {len(text)} chars")
        except Exception as e:
            logger.warning(f"[FileConversion] Failed to read '{filename}': {e}")
            return _conversion_alert(filename, mime_type)
    else:
        # markitdown path: docx, xlsx, and anything else markitdown supports
        def _convert() -> str:
            from markitdown import MarkItDown  # lazy import — optional dependency
            md = MarkItDown()
            result = md.convert(local_path)
            return result.text_content or ""

        try:
            text = await asyncio.to_thread(_convert)
        except Exception as e:
            logger.warning(f"[FileConversion] Failed to convert '{filename}': {type(e).__name__}: {e}")
            return _conversion_alert(filename, mime_type)

    if not text.strip():
        logger.warning(f"[FileConversion] Empty output for '{filename}'")
        return _conversion_alert(filename, mime_type)

    return f"[File: {filename}]\n{text}\n[/File: {filename}]"
