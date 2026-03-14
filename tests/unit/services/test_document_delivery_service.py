"""
Unit tests for DocumentDeliveryService.

Covers:
- store() delegates to MediaStoragePort with correct key format and content_type
- GCS key follows docs/{uuid}-{filename} pattern
- UUID in key guarantees uniqueness across calls
- Returns URL from storage
- Works for different content types (PDF, HTML, DOCX)
"""
import re
import pytest
from unittest.mock import AsyncMock

from src.ports.media_storage_port import MediaStoragePort
from src.services.document_delivery_service import DocumentDeliveryService


_FAKE_URL = "https://storage.googleapis.com/alek-media-dev/docs/uuid-q1_report.pdf"
_FAKE_CONTENT = b"%PDF-1.4 fake-content"
_UUID4_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_storage():
    s = AsyncMock(spec=MediaStoragePort)
    s.store.return_value = _FAKE_URL
    return s


@pytest.fixture
def service(mock_storage):
    return DocumentDeliveryService(storage=mock_storage)


# ============================================================================
# store() — delegation to MediaStoragePort
# ============================================================================

class TestStore:

    async def test_returns_url_from_storage(self, service):
        url = await service.store(_FAKE_CONTENT, "q1_report.pdf", "application/pdf")
        assert url == _FAKE_URL

    async def test_calls_storage_store_once(self, service, mock_storage):
        await service.store(_FAKE_CONTENT, "q1_report.pdf", "application/pdf")
        mock_storage.store.assert_called_once()

    async def test_passes_content_bytes_to_storage(self, service, mock_storage):
        content = b"custom-pdf-bytes"
        await service.store(content, "doc.pdf", "application/pdf")
        call_kwargs = mock_storage.store.call_args.kwargs
        assert call_kwargs.get("data") == content

    async def test_passes_content_type_to_storage(self, service, mock_storage):
        await service.store(_FAKE_CONTENT, "report.pdf", "application/pdf")
        call_kwargs = mock_storage.store.call_args.kwargs
        assert call_kwargs.get("content_type") == "application/pdf"

    async def test_content_type_html_passed_correctly(self, service, mock_storage):
        await service.store(b"<html></html>", "report.html", "text/html; charset=utf-8")
        call_kwargs = mock_storage.store.call_args.kwargs
        assert call_kwargs.get("content_type") == "text/html; charset=utf-8"


# ============================================================================
# store() — GCS key format
# ============================================================================

class TestKeyFormat:

    def _get_key(self, mock_storage) -> str:
        return mock_storage.store.call_args.kwargs.get("key", "")

    async def test_key_starts_with_docs_prefix(self, service, mock_storage):
        await service.store(_FAKE_CONTENT, "q1_report.pdf", "application/pdf")
        key = self._get_key(mock_storage)
        assert key.startswith("docs/")

    async def test_key_ends_with_filename(self, service, mock_storage):
        await service.store(_FAKE_CONTENT, "q1_report.pdf", "application/pdf")
        key = self._get_key(mock_storage)
        assert key.endswith("q1_report.pdf")

    async def test_key_contains_uuid4(self, service, mock_storage):
        await service.store(_FAKE_CONTENT, "report.pdf", "application/pdf")
        key = self._get_key(mock_storage)
        # key format: docs/{uuid}-{filename}
        part = key.removeprefix("docs/").removesuffix("-report.pdf")
        assert _UUID4_PATTERN.match(part), f"Expected UUID4 in key, got: {part!r}"

    async def test_key_format_docs_slash_uuid_dash_filename(self, service, mock_storage):
        await service.store(_FAKE_CONTENT, "my_file.pdf", "application/pdf")
        key = self._get_key(mock_storage)
        # Must match: docs/<uuid4>-my_file.pdf
        assert re.match(
            r"^docs/[0-9a-f-]{36}-my_file\.pdf$",
            key,
        ), f"Key format unexpected: {key!r}"

    async def test_html_filename_in_key(self, service, mock_storage):
        await service.store(b"<html></html>", "report.html", "text/html; charset=utf-8")
        key = self._get_key(mock_storage)
        assert key.endswith("report.html")


# ============================================================================
# store() — uniqueness guarantee
# ============================================================================

class TestUniqueness:

    async def test_two_calls_produce_different_keys(self, service, mock_storage):
        mock_storage.store.return_value = _FAKE_URL
        await service.store(_FAKE_CONTENT, "report.pdf", "application/pdf")
        await service.store(_FAKE_CONTENT, "report.pdf", "application/pdf")

        calls = mock_storage.store.call_args_list
        key1 = calls[0].kwargs.get("key", "")
        key2 = calls[1].kwargs.get("key", "")
        assert key1 != key2

    async def test_same_filename_different_keys(self, service, mock_storage):
        mock_storage.store.return_value = _FAKE_URL
        await service.store(b"bytes1", "same_name.pdf", "application/pdf")
        await service.store(b"bytes2", "same_name.pdf", "application/pdf")

        calls = mock_storage.store.call_args_list
        key1 = calls[0].kwargs.get("key", "")
        key2 = calls[1].kwargs.get("key", "")
        assert key1 != key2
