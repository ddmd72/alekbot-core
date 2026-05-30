"""
Unit tests for FileConversionService (GCS upload + resolve pipeline).

Separate from test_file_conversion_service.py which covers file_conversion utilities.
Mocking: FileStoragePort, aiofiles, convert_file_to_text.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.domain.llm import MessagePart
from src.ports.file_storage_port import FileStoragePort
from src.services.file_conversion_service import FileConversionService, _format_size


# ---------------------------------------------------------------------------
# _format_size (module-level pure function)
# ---------------------------------------------------------------------------

class TestFormatSize:

    def test_megabytes(self):
        assert _format_size(1_234_567) == "1.2MB"

    def test_exact_megabyte(self):
        assert _format_size(1_048_576) == "1.0MB"

    def test_kilobytes(self):
        assert _format_size(45_000) == "44KB"

    def test_bytes_small(self):
        assert _format_size(500) == "500B"

    def test_zero(self):
        assert _format_size(0) == "0B"

    def test_one_kb(self):
        assert _format_size(1024) == "1KB"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_storage():
    return AsyncMock(spec=FileStoragePort)


@pytest.fixture
def service(mock_storage):
    return FileConversionService(storage=mock_storage)


# ---------------------------------------------------------------------------
# process_attachment — text file
# ---------------------------------------------------------------------------

class TestProcessAttachment:

    async def test_uploads_and_returns_reference_part(self, service, mock_storage):
        mock_storage.upload = AsyncMock(return_value="report.docx")

        with patch("src.services.file_conversion_service.aiofiles") as mock_aiofiles:
            mock_aio_ctx = AsyncMock()
            mock_aio_ctx.read = AsyncMock(return_value=b"file data bytes here")
            mock_aiofiles.open.return_value.__aenter__ = AsyncMock(return_value=mock_aio_ctx)
            mock_aiofiles.open.return_value.__aexit__ = AsyncMock()

            result = await service.process_attachment(
                local_path="/tmp/report.docx",
                filename="report.docx",
                mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                user_id="user1",
            )

        assert isinstance(result, MessagePart)
        assert '[File: "report.docx"' in result.text
        assert result.file_data["ref"] == "report.docx"
        assert result.file_data["mime_type"].startswith("application/")
        assert result.file_data["size_bytes"] == 20
        # Text files should NOT have path in file_data
        assert "path" not in result.file_data

    async def test_label_includes_size(self, service, mock_storage):
        mock_storage.upload = AsyncMock(return_value="big.csv")

        with patch("src.services.file_conversion_service.aiofiles") as mock_aiofiles:
            mock_aio_ctx = AsyncMock()
            mock_aio_ctx.read = AsyncMock(return_value=b"x" * 45_000)
            mock_aiofiles.open.return_value.__aenter__ = AsyncMock(return_value=mock_aio_ctx)
            mock_aiofiles.open.return_value.__aexit__ = AsyncMock()

            result = await service.process_attachment(
                "/tmp/big.csv", "big.csv", "text/csv", "user1"
            )

        assert "44KB" in result.text

    async def test_calls_storage_upload(self, service, mock_storage):
        mock_storage.upload = AsyncMock(return_value="file.txt")

        with patch("src.services.file_conversion_service.aiofiles") as mock_aiofiles:
            mock_aio_ctx = AsyncMock()
            mock_aio_ctx.read = AsyncMock(return_value=b"content")
            mock_aiofiles.open.return_value.__aenter__ = AsyncMock(return_value=mock_aio_ctx)
            mock_aiofiles.open.return_value.__aexit__ = AsyncMock()

            await service.process_attachment(
                "/tmp/file.txt", "file.txt", "text/plain", "user1"
            )

        mock_storage.upload.assert_called_once_with(
            b"content", "file.txt", "user1", "text/plain"
        )


# ---------------------------------------------------------------------------
# process_attachment — native binary (image)
# ---------------------------------------------------------------------------

class TestProcessAttachmentBinary:

    async def test_binary_includes_path(self, service, mock_storage):
        mock_storage.upload = AsyncMock(return_value="photo.png")

        with patch("src.services.file_conversion_service.aiofiles") as mock_aiofiles:
            mock_aio_ctx = AsyncMock()
            mock_aio_ctx.read = AsyncMock(return_value=b"\x89PNG" + b"\x00" * 100)
            mock_aiofiles.open.return_value.__aenter__ = AsyncMock(return_value=mock_aio_ctx)
            mock_aiofiles.open.return_value.__aexit__ = AsyncMock()

            result = await service.process_attachment(
                "/tmp/photo.png", "photo.png", "image/png", "user1"
            )

        assert result.file_data["ref"] == "photo.png"
        assert result.file_data["path"] == "/tmp/photo.png"
        assert result.file_data["mime_type"] == "image/png"

    async def test_pdf_includes_path(self, service, mock_storage):
        mock_storage.upload = AsyncMock(return_value="doc.pdf")

        with patch("src.services.file_conversion_service.aiofiles") as mock_aiofiles:
            mock_aio_ctx = AsyncMock()
            mock_aio_ctx.read = AsyncMock(return_value=b"%PDF-1.4")
            mock_aiofiles.open.return_value.__aenter__ = AsyncMock(return_value=mock_aio_ctx)
            mock_aiofiles.open.return_value.__aexit__ = AsyncMock()

            result = await service.process_attachment(
                "/tmp/doc.pdf", "doc.pdf", "application/pdf", "user1"
            )

        assert result.file_data["path"] == "/tmp/doc.pdf"


# ---------------------------------------------------------------------------
# process_attachment — dedup
# ---------------------------------------------------------------------------

class TestProcessAttachmentDedup:

    async def test_dedup_preserves_original_name(self, service, mock_storage):
        mock_storage.upload = AsyncMock(return_value="report (1).docx")

        with patch("src.services.file_conversion_service.aiofiles") as mock_aiofiles:
            mock_aio_ctx = AsyncMock()
            mock_aio_ctx.read = AsyncMock(return_value=b"data")
            mock_aiofiles.open.return_value.__aenter__ = AsyncMock(return_value=mock_aio_ctx)
            mock_aiofiles.open.return_value.__aexit__ = AsyncMock()

            result = await service.process_attachment(
                "/tmp/report.docx", "report.docx", "text/plain", "user1"
            )

        assert result.file_data["ref"] == "report (1).docx"
        assert result.file_data["original_name"] == "report.docx"

    async def test_label_uses_deduped_ref_not_original_name(self, service, mock_storage):
        """Label must carry the unique GCS ref, not the (possibly colliding) original
        filename — the orchestrator addresses files by the label name when it calls
        open_file / forwards to a specialist. If the label showed the original name
        (e.g. Slack's generic 'image.png'), the exact-key download lands on a stale
        object that squats the un-suffixed slot. See file_conversion_service.py:71.
        """
        mock_storage.upload = AsyncMock(return_value="image (4).png")

        with patch("src.services.file_conversion_service.aiofiles") as mock_aiofiles:
            mock_aio_ctx = AsyncMock()
            mock_aio_ctx.read = AsyncMock(return_value=b"\x89PNG" + b"\x00" * 100)
            mock_aiofiles.open.return_value.__aenter__ = AsyncMock(return_value=mock_aio_ctx)
            mock_aiofiles.open.return_value.__aexit__ = AsyncMock()

            result = await service.process_attachment(
                "/tmp/image.png", "image.png", "image/png", "user1"
            )

        assert '[File: "image (4).png"' in result.text
        assert "image.png\"" not in result.text  # original name must not be the label name

    async def test_no_original_name_when_not_deduped(self, service, mock_storage):
        mock_storage.upload = AsyncMock(return_value="report.docx")

        with patch("src.services.file_conversion_service.aiofiles") as mock_aiofiles:
            mock_aio_ctx = AsyncMock()
            mock_aio_ctx.read = AsyncMock(return_value=b"data")
            mock_aiofiles.open.return_value.__aenter__ = AsyncMock(return_value=mock_aio_ctx)
            mock_aiofiles.open.return_value.__aexit__ = AsyncMock()

            result = await service.process_attachment(
                "/tmp/report.docx", "report.docx", "text/plain", "user1"
            )

        assert "original_name" not in result.file_data


# ---------------------------------------------------------------------------
# resolve_content
# ---------------------------------------------------------------------------

class TestResolveContent:

    async def test_downloads_converts_and_returns_text(self, service, mock_storage):
        mock_storage.download = AsyncMock(return_value=b"raw file bytes")

        with patch("src.services.file_conversion_service.aiofiles") as mock_aiofiles, \
             patch("src.services.file_conversion_service.convert_file_to_text") as mock_convert, \
             patch("src.services.file_conversion_service.tempfile") as mock_tempfile, \
             patch("src.services.file_conversion_service.os") as mock_os:
            mock_tempfile.mkstemp.return_value = (5, "/tmp/tmpXXX.docx")
            mock_aio_ctx = AsyncMock()
            mock_aiofiles.open.return_value.__aenter__ = AsyncMock(return_value=mock_aio_ctx)
            mock_aiofiles.open.return_value.__aexit__ = AsyncMock()
            mock_convert.return_value = "Converted text content"

            result = await service.resolve_content("report.docx", "user1")

        assert result == "Converted text content"
        mock_storage.download.assert_called_once_with("report.docx", "user1")
        mock_convert.assert_called_once()
        mock_os.remove.assert_called_once_with("/tmp/tmpXXX.docx")

    async def test_temp_file_cleaned_on_error(self, service, mock_storage):
        mock_storage.download = AsyncMock(return_value=b"data")

        with patch("src.services.file_conversion_service.aiofiles") as mock_aiofiles, \
             patch("src.services.file_conversion_service.convert_file_to_text") as mock_convert, \
             patch("src.services.file_conversion_service.tempfile") as mock_tempfile, \
             patch("src.services.file_conversion_service.os") as mock_os:
            mock_tempfile.mkstemp.return_value = (5, "/tmp/tmpXXX.txt")
            mock_aio_ctx = AsyncMock()
            mock_aiofiles.open.return_value.__aenter__ = AsyncMock(return_value=mock_aio_ctx)
            mock_aiofiles.open.return_value.__aexit__ = AsyncMock()
            mock_convert.side_effect = RuntimeError("conversion error")

            with pytest.raises(RuntimeError, match="conversion error"):
                await service.resolve_content("broken.txt", "user1")

        mock_os.remove.assert_called_once_with("/tmp/tmpXXX.txt")


# ---------------------------------------------------------------------------
# resolve_bytes
# ---------------------------------------------------------------------------

class TestResolveBytes:

    async def test_returns_raw_bytes(self, service, mock_storage):
        mock_storage.download = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n")

        result = await service.resolve_bytes("photo.png", "user1")

        assert result == b"\x89PNG\r\n\x1a\n"
        mock_storage.download.assert_called_once_with("photo.png", "user1")
