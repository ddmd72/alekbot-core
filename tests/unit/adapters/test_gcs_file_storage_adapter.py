"""
Unit tests for GcsFileStorageAdapter.

Mocking strategy: patch google.cloud.storage.Client at SDK boundary.
All async methods delegate to sync GCS calls via run_in_executor.
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from src.adapters.gcs_file_storage_adapter import (
    GcsFileStorageAdapter,
    sanitize_filename,
)


# ---------------------------------------------------------------------------
# sanitize_filename (module-level pure function)
# ---------------------------------------------------------------------------

class TestSanitizeFilename:

    def test_replaces_hash(self):
        assert sanitize_filename("file#1.txt") == "file_1.txt"

    def test_replaces_question_mark(self):
        assert sanitize_filename("file?.txt") == "file_.txt"

    def test_replaces_brackets(self):
        assert sanitize_filename("file[1].txt") == "file_1_.txt"

    def test_replaces_asterisk(self):
        assert sanitize_filename("file*.txt") == "file_.txt"

    def test_replaces_newline(self):
        assert sanitize_filename("file\nname.txt") == "file_name.txt"

    def test_replaces_tab(self):
        assert sanitize_filename("file\tname.txt") == "file_name.txt"

    def test_replaces_carriage_return(self):
        assert sanitize_filename("file\rname.txt") == "file_name.txt"

    def test_preserves_utf8_cyrillic(self):
        assert sanitize_filename("звіт.docx") == "звіт.docx"

    def test_preserves_clean_name(self):
        assert sanitize_filename("report-2026.docx") == "report-2026.docx"

    def test_multiple_prohibited_chars(self):
        assert sanitize_filename("a#b?c[d]*e.txt") == "a_b_c_d__e.txt"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_storage_client():
    """Patch google.cloud.storage.Client at the SDK boundary."""
    with patch("src.adapters.gcs_file_storage_adapter.GcsFileStorageAdapter._get_client") as mock_get:
        client = MagicMock()
        mock_get.return_value = client
        yield client


@pytest.fixture
def adapter():
    return GcsFileStorageAdapter(bucket_name="test-bucket")


# ---------------------------------------------------------------------------
# _key()
# ---------------------------------------------------------------------------

class TestKey:

    def test_assembles_key(self, adapter):
        assert adapter._key("report.docx", "user1") == "user1/files/report.docx"

    def test_key_with_dedup_name(self, adapter):
        assert adapter._key("report (1).docx", "user1") == "user1/files/report (1).docx"


# ---------------------------------------------------------------------------
# upload()
# ---------------------------------------------------------------------------

class TestUpload:

    async def test_upload_returns_filename(self, adapter, mock_storage_client):
        blob = MagicMock()
        blob.exists.return_value = False
        mock_storage_client.bucket.return_value.blob.return_value = blob

        result = await adapter.upload(b"data", "report.docx", "user1", "application/pdf")

        assert result == "report.docx"
        blob.upload_from_string.assert_called_once_with(b"data", content_type="application/pdf")

    async def test_upload_sanitizes_filename(self, adapter, mock_storage_client):
        blob = MagicMock()
        blob.exists.return_value = False
        mock_storage_client.bucket.return_value.blob.return_value = blob

        result = await adapter.upload(b"data", "file#1.txt", "user1", "text/plain")

        assert result == "file_1.txt"

    async def test_upload_dedup_appends_counter(self, adapter, mock_storage_client):
        """When filename exists, should return name (1) variant."""
        blob = MagicMock()
        # First call (report.docx) → exists, second call (report (1).docx) → not exists
        blob.exists.side_effect = [True, False]
        mock_storage_client.bucket.return_value.blob.return_value = blob

        result = await adapter.upload(b"data", "report.docx", "user1", "text/plain")

        assert result == "report (1).docx"

    async def test_upload_dedup_increments(self, adapter, mock_storage_client):
        """When (1) also exists, should try (2)."""
        blob = MagicMock()
        blob.exists.side_effect = [True, True, False]
        mock_storage_client.bucket.return_value.blob.return_value = blob

        result = await adapter.upload(b"data", "report.docx", "user1", "text/plain")

        assert result == "report (2).docx"


# ---------------------------------------------------------------------------
# download()
# ---------------------------------------------------------------------------

class TestDownload:

    async def test_download_returns_bytes(self, adapter, mock_storage_client):
        blob = MagicMock()
        blob.download_as_bytes.return_value = b"file-content"
        mock_storage_client.bucket.return_value.blob.return_value = blob

        result = await adapter.download("report.docx", "user1")

        assert result == b"file-content"
        mock_storage_client.bucket.assert_called_with("test-bucket")
        mock_storage_client.bucket.return_value.blob.assert_called_with("user1/files/report.docx")

    async def test_download_missing_blob_raises_file_not_found(self, adapter, mock_storage_client):
        from google.api_core.exceptions import NotFound
        blob = MagicMock()
        blob.download_as_bytes.side_effect = NotFound("No such object: test-bucket/user1/files/gone.csv")
        mock_storage_client.bucket.return_value.blob.return_value = blob

        with pytest.raises(FileNotFoundError) as e:
            await adapter.download("gone.csv", "user1")

        assert str(e.value) == "gone.csv"

    async def test_download_other_gcs_error_is_not_file_not_found(self, adapter, mock_storage_client):
        from google.api_core.exceptions import ServiceUnavailable
        blob = MagicMock()
        blob.download_as_bytes.side_effect = ServiceUnavailable("outage")
        mock_storage_client.bucket.return_value.blob.return_value = blob

        with pytest.raises(ServiceUnavailable):
            await adapter.download("report.docx", "user1")


# ---------------------------------------------------------------------------
# delete()
# ---------------------------------------------------------------------------

class TestDelete:

    async def test_delete_calls_blob_delete(self, adapter, mock_storage_client):
        blob = MagicMock()
        mock_storage_client.bucket.return_value.blob.return_value = blob

        await adapter.delete("report.docx", "user1")

        blob.delete.assert_called_once()


# ---------------------------------------------------------------------------
# exists()
# ---------------------------------------------------------------------------

class TestExists:

    async def test_exists_returns_true(self, adapter, mock_storage_client):
        blob = MagicMock()
        blob.exists.return_value = True
        mock_storage_client.bucket.return_value.blob.return_value = blob

        assert await adapter.exists("report.docx", "user1") is True

    async def test_exists_returns_false(self, adapter, mock_storage_client):
        blob = MagicMock()
        blob.exists.return_value = False
        mock_storage_client.bucket.return_value.blob.return_value = blob

        assert await adapter.exists("report.docx", "user1") is False


# ---------------------------------------------------------------------------
# get_url()
# ---------------------------------------------------------------------------

class TestGetUrl:

    async def test_get_url_assembles_public_url(self, adapter, mock_storage_client):
        url = await adapter.get_url("report.docx", "user1")

        assert url == "https://storage.googleapis.com/test-bucket/user1/files/report.docx"
