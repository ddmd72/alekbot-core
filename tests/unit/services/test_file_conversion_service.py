"""
Unit tests for FileConversionService helper functions and convert_file_to_text.

Tests pure-function utilities and the async conversion pipeline without
hitting real filesystem (os.path.getsize patched) or real dependencies.
"""

import pytest
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

from src.utils.file_conversion import (
    is_native_binary,
    is_audio,
    make_history_stub,
    MAX_FILE_BYTES,
    MAX_CONVERTED_CHARS,
    HISTORY_PREVIEW_CHARS,
)


# =============================================================================
# is_native_binary
# =============================================================================

def test_is_native_binary_image():
    assert is_native_binary("image/jpeg") is True
    assert is_native_binary("image/png") is True
    assert is_native_binary("image/gif") is True
    assert is_native_binary("image/webp") is True


def test_is_native_binary_pdf():
    assert is_native_binary("application/pdf") is True


def test_is_native_binary_text_returns_false():
    assert is_native_binary("text/plain") is False
    assert is_native_binary("text/csv") is False
    assert is_native_binary("application/vnd.openxmlformats-officedocument.wordprocessingml.document") is False


def test_is_native_binary_audio_returns_false():
    assert is_native_binary("audio/mpeg") is False


# =============================================================================
# is_audio
# =============================================================================

def test_is_audio_mp3():
    assert is_audio("audio/mpeg") is True


def test_is_audio_wav():
    assert is_audio("audio/wav") is True


def test_is_audio_m4a():
    assert is_audio("audio/mp4") is True
    assert is_audio("audio/x-m4a") is True


def test_is_audio_ogg():
    assert is_audio("audio/ogg") is True


def test_is_audio_text_returns_false():
    assert is_audio("text/plain") is False
    assert is_audio("image/jpeg") is False
    assert is_audio("application/pdf") is False


# =============================================================================
# make_history_stub
# =============================================================================

def test_make_history_stub_short_content_unchanged():
    short_text = "Short content here."
    full_output = f"[File: test.txt]\n{short_text}\n[/File: test.txt]"

    result = make_history_stub(full_output, "test.txt")

    assert result == full_output


def test_make_history_stub_long_content_truncated():
    long_content = "A" * (HISTORY_PREVIEW_CHARS + 500)
    full_output = f"[File: bigfile.txt]\n{long_content}\n[/File: bigfile.txt]"

    result = make_history_stub(full_output, "bigfile.txt")

    assert "bigfile.txt" in result
    assert "omitted" in result
    assert "Re-upload for full analysis" in result
    assert len(result) < len(full_output)


def test_make_history_stub_exactly_at_limit_unchanged():
    exactly_limit = "B" * HISTORY_PREVIEW_CHARS
    full_output = f"[File: edge.txt]\n{exactly_limit}\n[/File: edge.txt]"

    result = make_history_stub(full_output, "edge.txt")

    assert result == full_output


def test_make_history_stub_malformed_passthrough():
    malformed = "No newline in this output"
    result = make_history_stub(malformed, "test.txt")
    assert result == malformed
