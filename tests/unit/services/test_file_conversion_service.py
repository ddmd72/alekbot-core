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
    convert_file_to_text,
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


# =============================================================================
# convert_file_to_text — file size limit
# =============================================================================

@pytest.mark.asyncio
async def test_convert_rejects_oversized_file(tmp_path):
    large_file = tmp_path / "big.csv"
    large_file.write_bytes(b"x")  # Create the file

    with patch("os.path.getsize", return_value=MAX_FILE_BYTES + 1):
        result = await convert_file_to_text(str(large_file), "big.csv", "text/csv")

    assert "5 MB limit" in result
    assert "big.csv" in result
    assert result.startswith("[System:")


# =============================================================================
# convert_file_to_text — audio path
# =============================================================================

@pytest.mark.asyncio
async def test_convert_audio_with_service(tmp_path):
    audio_file = tmp_path / "voice.mp3"
    audio_file.write_bytes(b"fake audio data")

    mock_audio = AsyncMock()
    mock_audio.transcribe = AsyncMock(return_value="Hello, this is the transcription.")

    with patch("os.path.getsize", return_value=1024):
        result = await convert_file_to_text(
            str(audio_file), "voice.mp3", "audio/mpeg", audio_service=mock_audio
        )

    assert "[File: voice.mp3]" in result
    assert "Hello, this is the transcription." in result
    assert "[/File: voice.mp3]" in result
    mock_audio.transcribe.assert_awaited_once()


@pytest.mark.asyncio
async def test_convert_audio_without_service_returns_alert(tmp_path):
    audio_file = tmp_path / "voice.wav"
    audio_file.write_bytes(b"fake audio")

    with patch("os.path.getsize", return_value=1024):
        result = await convert_file_to_text(
            str(audio_file), "voice.wav", "audio/wav", audio_service=None
        )

    assert result.startswith("[System:")
    assert "audio" in result.lower()
    assert "voice.wav" in result


@pytest.mark.asyncio
async def test_convert_audio_transcription_fails_returns_alert(tmp_path):
    audio_file = tmp_path / "broken.mp3"
    audio_file.write_bytes(b"corrupt")

    mock_audio = AsyncMock()
    mock_audio.transcribe = AsyncMock(side_effect=RuntimeError("Transcription API down"))

    with patch("os.path.getsize", return_value=1024):
        result = await convert_file_to_text(
            str(audio_file), "broken.mp3", "audio/mpeg", audio_service=mock_audio
        )

    assert result.startswith("[System:")
    assert "broken.mp3" in result


# =============================================================================
# convert_file_to_text — plain text path
# =============================================================================

@pytest.mark.asyncio
async def test_convert_plain_text_file(tmp_path):
    text_file = tmp_path / "notes.txt"
    text_file.write_text("My meeting notes go here.", encoding="utf-8")

    with patch("os.path.getsize", return_value=100):
        result = await convert_file_to_text(str(text_file), "notes.txt", "text/plain")

    assert "[File: notes.txt]" in result
    assert "My meeting notes go here." in result
    assert "[/File: notes.txt]" in result


@pytest.mark.asyncio
async def test_convert_csv_file(tmp_path):
    csv_file = tmp_path / "data.csv"
    csv_file.write_text("name,age\nAlice,30\nBob,25", encoding="utf-8")

    with patch("os.path.getsize", return_value=50):
        result = await convert_file_to_text(str(csv_file), "data.csv", "text/csv")

    assert "[File: data.csv]" in result
    assert "Alice,30" in result


@pytest.mark.asyncio
async def test_convert_plain_text_read_failure_returns_alert(tmp_path):
    text_file = tmp_path / "locked.txt"
    text_file.write_text("content", encoding="utf-8")

    with patch("os.path.getsize", return_value=50):
        with patch("asyncio.to_thread", side_effect=OSError("Permission denied")):
            result = await convert_file_to_text(str(text_file), "locked.txt", "text/plain")

    assert result.startswith("[System:")
    assert "locked.txt" in result


@pytest.mark.asyncio
async def test_convert_empty_text_file_returns_alert(tmp_path):
    empty_file = tmp_path / "empty.txt"
    empty_file.write_text("   ", encoding="utf-8")

    with patch("os.path.getsize", return_value=3):
        result = await convert_file_to_text(str(empty_file), "empty.txt", "text/plain")

    assert result.startswith("[System:")
    assert "empty.txt" in result


# =============================================================================
# convert_file_to_text — markitdown path
# =============================================================================

@pytest.mark.asyncio
async def test_convert_docx_via_markitdown(tmp_path):
    docx_file = tmp_path / "report.docx"
    docx_file.write_bytes(b"fake docx bytes")

    mock_md_instance = MagicMock()
    mock_md_instance.convert.return_value.text_content = "Report content here."

    with patch("os.path.getsize", return_value=2048):
        with patch("asyncio.to_thread") as mock_thread:
            # asyncio.to_thread runs _convert() — simulate it returning the text
            mock_thread.return_value = "Report content here."
            result = await convert_file_to_text(str(docx_file), "report.docx",
                                                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    assert "[File: report.docx]" in result
    assert "Report content here." in result


@pytest.mark.asyncio
async def test_convert_markitdown_failure_returns_alert(tmp_path):
    bad_file = tmp_path / "bad.xlsx"
    bad_file.write_bytes(b"corrupt bytes")

    with patch("os.path.getsize", return_value=100):
        with patch("asyncio.to_thread", side_effect=Exception("MarkItDown parse error")):
            result = await convert_file_to_text(str(bad_file), "bad.xlsx",
                                                 "application/vnd.ms-excel")

    assert result.startswith("[System:")
    assert "bad.xlsx" in result


# =============================================================================
# convert_file_to_text — truncation
# =============================================================================

@pytest.mark.asyncio
async def test_convert_long_text_gets_truncated(tmp_path):
    long_content = "W" * (MAX_CONVERTED_CHARS + 5000)
    text_file = tmp_path / "huge.txt"
    text_file.write_text(long_content, encoding="utf-8")

    with patch("os.path.getsize", return_value=len(long_content)):
        result = await convert_file_to_text(str(text_file), "huge.txt", "text/plain")

    assert "truncated" in result.lower() or "omitted" in result.lower()
    assert "[File: huge.txt]" in result
