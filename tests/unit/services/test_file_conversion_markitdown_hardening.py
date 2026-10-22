"""
markitdown runs on untrusted user files: only local document converters are registered, and a
ZIP container is checked on its central directory before any converter opens it.
"""

import zipfile

import pytest
from unittest.mock import AsyncMock, patch

from src.utils.file_conversion import (
    ArchiveTooLargeError,
    MAX_ZIP_MEMBERS,
    MAX_ZIP_UNCOMPRESSED_BYTES,
    build_markitdown,
    check_zip_container,
    convert_file_to_text,
)

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _registered_names(md) -> set:
    return {type(reg.converter).__name__ for reg in md._converters}


# =============================================================================
# build_markitdown — converter allowlist
# =============================================================================

@pytest.mark.slow
def test_build_markitdown_excludes_unsafe_converters():
    names = _registered_names(build_markitdown())
    for unsafe in ("ZipConverter", "AudioConverter", "ImageConverter", "YouTubeConverter",
                   "WikipediaConverter", "BingSerpConverter", "DocumentIntelligenceConverter"):
        assert unsafe not in names


@pytest.mark.slow
def test_build_markitdown_keeps_document_converters():
    names = _registered_names(build_markitdown())
    for kept in ("DocxConverter", "XlsxConverter", "XlsConverter", "PptxConverter", "PdfConverter",
                 "EpubConverter", "HtmlConverter", "PlainTextConverter", "CsvConverter"):
        assert kept in names


# =============================================================================
# check_zip_container — central-directory caps
# =============================================================================

def test_check_zip_container_ignores_non_zip(tmp_path):
    f = tmp_path / "notes.bin"
    f.write_bytes(b"not an archive")
    check_zip_container(str(f))  # no exception


def test_check_zip_container_accepts_small_archive(tmp_path):
    f = tmp_path / "ok.zip"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("a.txt", "hello")
    check_zip_container(str(f))


def test_check_zip_container_rejects_inflated_size(tmp_path):
    f = tmp_path / "bomb.docx"
    chunk = b"\0" * (1024 * 1024)
    with zipfile.ZipFile(f, "w", compression=zipfile.ZIP_DEFLATED) as z:
        with z.open("word/document.xml", "w", force_zip64=True) as member:
            for _ in range(MAX_ZIP_UNCOMPRESSED_BYTES // len(chunk) + 1):
                member.write(chunk)
    assert f.stat().st_size < 5 * 1024 * 1024  # passes the upload cap — that is the attack
    with pytest.raises(ArchiveTooLargeError):
        check_zip_container(str(f))


@pytest.mark.slow
def test_check_zip_container_rejects_too_many_members(tmp_path):
    f = tmp_path / "many.xlsx"
    with zipfile.ZipFile(f, "w") as z:
        for i in range(MAX_ZIP_MEMBERS + 1):
            z.writestr(f"m{i}", b"")
    with pytest.raises(ArchiveTooLargeError):
        check_zip_container(str(f))


# =============================================================================
# convert_file_to_text — end to end through the real markitdown
# =============================================================================

@pytest.mark.slow
@pytest.mark.asyncio
async def test_convert_real_docx_still_works(tmp_path):
    import docx

    path = tmp_path / "report.docx"
    document = docx.Document()
    document.add_paragraph("Penalty is 0.1% per day.")
    document.save(path)

    result = await convert_file_to_text(str(path), "report.docx", DOCX_MIME)

    assert result.startswith("[File: report.docx]")
    assert "Penalty is 0.1% per day." in result


@pytest.mark.slow
@pytest.mark.asyncio
async def test_convert_plain_zip_is_not_unpacked(tmp_path):
    path = tmp_path / "archive.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("secret.txt", "INNER CONTENT")

    result = await convert_file_to_text(str(path), "archive.zip", "application/zip")

    assert result.startswith("[System:")
    assert "INNER CONTENT" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("filename,mime_type", [
    ("speech.wav", "audio/x-wav"),           # Python's mimetypes name for .wav
    ("memo.m4a", "audio/mp4a-latm"),         # Python 3.13's name for .m4a
    ("memo.m4a", "application/octet-stream"),  # Python 3.11 has no .m4a entry
    ("take.flac", "audio/flac"),
])
async def test_audio_reaches_transcription_port_not_markitdown(tmp_path, filename, mime_type):
    path = tmp_path / filename
    path.write_bytes(b"\0" * 64)
    audio = AsyncMock()
    audio.transcribe.return_value = "hello there"

    with patch("src.utils.file_conversion.build_markitdown") as build:
        result = await convert_file_to_text(str(path), filename, mime_type, audio_service=audio)

    audio.transcribe.assert_awaited_once()
    build.assert_not_called()
    assert "hello there" in result


@pytest.mark.asyncio
async def test_convert_zip_bomb_returns_alert_without_running_converter(tmp_path):
    path = tmp_path / "bomb.docx"
    with zipfile.ZipFile(path, "w") as z:
        for i in range(MAX_ZIP_MEMBERS + 1):
            z.writestr(f"m{i}", b"")

    with patch("src.utils.file_conversion.build_markitdown") as build:
        result = await convert_file_to_text(str(path), "bomb.docx", DOCX_MIME)

    assert result.startswith("[System:")
    assert "bomb.docx" in result
    build.assert_not_called()
