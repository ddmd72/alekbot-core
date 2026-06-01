"""
Unit tests for DocumentDeliveryService.

Covers:
- store() uploads privately (MediaStoragePort.store returns a KEY) then builds a
  capability link via FileLinkService
- key follows {prefix}/{uuid}-{filename}; prefix from storage_class
- UUID in key guarantees uniqueness across calls
- email_review storage_class → email_review/ prefix
- different content types (PDF, HTML, DOCX)
"""
import re
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.ports.media_storage_port import MediaStoragePort
from src.services.document_delivery_service import DocumentDeliveryService


_FAKE_LINK = "https://dev.alekbot.app/f/tok123"
_FAKE_CONTENT = b"%PDF-1.4 fake-content"
_UUID4_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_USER = "user-1"


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_storage():
    s = AsyncMock(spec=MediaStoragePort)
    # store() now returns the object key (not a URL); the link is built separately.
    s.store.return_value = "docs/uuid-q1_report.pdf"
    return s


@pytest.fixture
def mock_link_service():
    ls = MagicMock()
    ls.build_link.return_value = _FAKE_LINK
    return ls


@pytest.fixture
def service(mock_storage, mock_link_service):
    return DocumentDeliveryService(storage=mock_storage, link_service=mock_link_service)


# ============================================================================
# store() — delegation + link building
# ============================================================================

class TestStore:

    async def test_returns_capability_link(self, service):
        link = await service.store(_FAKE_CONTENT, "q1_report.pdf", "application/pdf", user_id=_USER)
        assert link == _FAKE_LINK

    async def test_calls_storage_store_once(self, service, mock_storage):
        await service.store(_FAKE_CONTENT, "q1_report.pdf", "application/pdf", user_id=_USER)
        mock_storage.store.assert_called_once()

    async def test_builds_link_with_key_and_user(self, service, mock_storage, mock_link_service):
        await service.store(_FAKE_CONTENT, "q1_report.pdf", "application/pdf", user_id=_USER)
        key = mock_storage.store.call_args.kwargs.get("key")
        mock_link_service.build_link.assert_called_once_with(key=key, user_id=_USER)

    async def test_passes_content_bytes_to_storage(self, service, mock_storage):
        content = b"custom-pdf-bytes"
        await service.store(content, "doc.pdf", "application/pdf", user_id=_USER)
        assert mock_storage.store.call_args.kwargs.get("data") == content

    async def test_passes_content_type_to_storage(self, service, mock_storage):
        await service.store(_FAKE_CONTENT, "report.pdf", "application/pdf", user_id=_USER)
        assert mock_storage.store.call_args.kwargs.get("content_type") == "application/pdf"

    async def test_content_type_html_passed_correctly(self, service, mock_storage):
        await service.store(b"<html></html>", "report.html", "text/html; charset=utf-8", user_id=_USER)
        assert mock_storage.store.call_args.kwargs.get("content_type") == "text/html; charset=utf-8"


# ============================================================================
# key format + storage_class → prefix
# ============================================================================

class TestKeyFormat:

    def _key(self, mock_storage) -> str:
        return mock_storage.store.call_args.kwargs.get("key", "")

    async def test_default_key_starts_with_docs_prefix(self, service, mock_storage):
        await service.store(_FAKE_CONTENT, "q1_report.pdf", "application/pdf", user_id=_USER)
        assert self._key(mock_storage).startswith("docs/")

    async def test_key_ends_with_filename(self, service, mock_storage):
        await service.store(_FAKE_CONTENT, "q1_report.pdf", "application/pdf", user_id=_USER)
        assert self._key(mock_storage).endswith("q1_report.pdf")

    async def test_key_contains_uuid4(self, service, mock_storage):
        await service.store(_FAKE_CONTENT, "report.pdf", "application/pdf", user_id=_USER)
        part = self._key(mock_storage).removeprefix("docs/").removesuffix("-report.pdf")
        assert _UUID4_PATTERN.match(part), f"Expected UUID4 in key, got: {part!r}"

    async def test_email_review_class_uses_email_review_prefix(self, service, mock_storage):
        await service.store(
            b"<html></html>", "review.html", "text/html; charset=utf-8",
            user_id=_USER, storage_class="email_review",
        )
        assert self._key(mock_storage).startswith("email_review/")

    async def test_unknown_class_falls_back_to_docs(self, service, mock_storage):
        await service.store(
            _FAKE_CONTENT, "x.pdf", "application/pdf", user_id=_USER, storage_class="bogus",
        )
        assert self._key(mock_storage).startswith("docs/")


# ============================================================================
# uniqueness
# ============================================================================

class TestUniqueness:

    async def test_two_calls_produce_different_keys(self, service, mock_storage):
        await service.store(_FAKE_CONTENT, "report.pdf", "application/pdf", user_id=_USER)
        await service.store(_FAKE_CONTENT, "report.pdf", "application/pdf", user_id=_USER)
        calls = mock_storage.store.call_args_list
        assert calls[0].kwargs.get("key") != calls[1].kwargs.get("key")
