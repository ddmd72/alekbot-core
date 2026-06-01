"""
Integration test: private file storage end-to-end seam.

Mock boundary: only the GCS SDK (google.cloud.storage.Client). Every alekbot
object in the chain is REAL — GcsMediaAdapter, DocumentDeliveryService,
FileLinkService, FileAccessTokenService, FileConversionService. This is the test
that mock-at-port unit tests cannot give: it exercises the CONTRACT between
store() and its consumers, so a change like "store() returns key not URL" or a
prefix/ownership regression fails here.

Covered round-trips:
  1. store → DeliveredDocument(link, key); link is /f/<token>, key is private.
  2. token in the link verifies back to the same key + user + gating.
  3. agent re-fetch: FileConversionService.resolve_bytes(key) reads it back.
  4. ownership: another user's id is rejected.
  5. email_review storage_class → gated token + email_review/ prefix.
"""
import pytest
from unittest.mock import MagicMock, patch

from src.adapters.gcs_media_adapter import GcsMediaAdapter
from src.services.file_access_token_service import FileAccessTokenService
from src.services.file_link_service import FileLinkService
from src.services.document_delivery_service import DocumentDeliveryService, DeliveredDocument
from src.services.file_conversion_service import FileConversionService

_SECRET = "x" * 40
_BASE = "https://dev.alekbot.app"
_USER = "user-abc"


class _FakeBucket:
    """In-memory stand-in for a GCS bucket: blob bytes keyed by object name."""

    def __init__(self, store: dict):
        self._store = store

    def blob(self, key):
        store = self._store
        blob = MagicMock()

        def _upload(data, content_type=None):
            store[key] = data
        blob.upload_from_string.side_effect = _upload

        def _download():
            if key not in store:
                raise FileNotFoundError(key)
            return store[key]
        blob.download_as_bytes.side_effect = _download

        blob.generate_signed_url.return_value = f"https://storage.googleapis.com/bucket/{key}?sig=x"
        return blob


@pytest.fixture
def gcs_objects():
    return {}


@pytest.fixture
def media(gcs_objects):
    adapter = GcsMediaAdapter(bucket_name="test-bucket")
    fake_client = MagicMock()
    fake_client.bucket.return_value = _FakeBucket(gcs_objects)
    # Patch the SDK boundary only.
    patcher = patch("google.cloud.storage.Client", return_value=fake_client)
    patcher.start()
    yield adapter
    patcher.stop()


@pytest.fixture
def tokens():
    return FileAccessTokenService(secret_key=_SECRET)


@pytest.fixture
def links(tokens):
    return FileLinkService(token_service=tokens, base_url=_BASE)


@pytest.fixture
def doc_delivery(media, links):
    return DocumentDeliveryService(storage=media, link_service=links)


@pytest.fixture
def conversion(media):
    # FileStoragePort unused in these paths (delivered keys go to media); a bare
    # MagicMock is fine — we only exercise the media branch.
    return FileConversionService(storage=MagicMock(), media_storage=media)


class TestPrivateStorageRoundTrip:

    @pytest.mark.asyncio
    async def test_store_returns_link_and_private_key(self, doc_delivery):
        result = await doc_delivery.store(
            b"%PDF-1.4 body", "report.pdf", "application/pdf", user_id=_USER,
        )
        assert isinstance(result, DeliveredDocument)
        # Link is the capability route, NOT a public storage URL.
        assert result.link.startswith(f"{_BASE}/f/")
        assert "storage.googleapis.com" not in result.link
        # Key is private + carries user_id for ownership checks.
        assert result.key.startswith(f"docs/{_USER}/")
        assert result.key.endswith("report.pdf")

    @pytest.mark.asyncio
    async def test_link_token_verifies_to_key(self, doc_delivery, tokens):
        result = await doc_delivery.store(
            b"data", "report.pdf", "application/pdf", user_id=_USER,
        )
        token = result.link.rsplit("/f/", 1)[1]
        decoded = tokens.verify(token)
        assert decoded.key == result.key
        assert decoded.user_id == _USER
        assert decoded.gated is False

    @pytest.mark.asyncio
    async def test_agent_can_refetch_stored_bytes(self, doc_delivery, conversion):
        body = b"%PDF-1.4 the report body"
        result = await doc_delivery.store(
            body, "report.pdf", "application/pdf", user_id=_USER,
        )
        # Agent re-reads server-side by the key from history.
        fetched = await conversion.resolve_bytes(result.key, _USER)
        assert fetched == body

    @pytest.mark.asyncio
    async def test_refetch_by_other_user_rejected(self, doc_delivery, conversion):
        result = await doc_delivery.store(
            b"secret", "report.pdf", "application/pdf", user_id=_USER,
        )
        with pytest.raises(PermissionError):
            await conversion.resolve_bytes(result.key, "intruder")

    @pytest.mark.asyncio
    async def test_email_review_is_gated_and_prefixed(self, doc_delivery, tokens):
        result = await doc_delivery.store(
            b"<html>review</html>", "review.html", "text/html; charset=utf-8",
            user_id=_USER, storage_class="email_review",
        )
        assert result.key.startswith(f"email_review/{_USER}/")
        token = result.link.rsplit("/f/", 1)[1]
        assert tokens.verify(token).gated is True

    @pytest.mark.asyncio
    async def test_stored_bytes_actually_land_in_bucket(self, doc_delivery, gcs_objects):
        await doc_delivery.store(b"payload", "x.pdf", "application/pdf", user_id=_USER)
        # The real adapter wrote through to the (fake) GCS bucket under the key.
        assert any(k.startswith(f"docs/{_USER}/") for k in gcs_objects)
        assert b"payload" in gcs_objects.values()
