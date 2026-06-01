"""
Wire tests for GcsMediaAdapter.

Mocking strategy: patch google.cloud.storage.Client at the SDK boundary.
Locks in the private-storage contract:
  - store() uploads WITHOUT any public ACL and returns the object KEY (not a URL)
  - HTML gets the noindex meta injected
  - fetch() reads bytes server-side via the SDK
"""
import pytest
from unittest.mock import MagicMock, patch

from src.adapters.gcs_media_adapter import GcsMediaAdapter, _inject_noindex


@pytest.fixture
def adapter():
    return GcsMediaAdapter(bucket_name="test-bucket")


def _patch_client():
    """Patch storage.Client; return (patch_ctx, blob_mock)."""
    blob = MagicMock()
    bucket = MagicMock()
    bucket.blob.return_value = blob
    client = MagicMock()
    client.bucket.return_value = bucket
    ctx = patch("google.cloud.storage.Client", return_value=client)
    return ctx, blob


class TestStorePrivate:

    @pytest.mark.asyncio
    async def test_store_returns_object_key_not_url(self, adapter):
        ctx, blob = _patch_client()
        with ctx:
            result = await adapter.store(b"data", "docs/uuid-report.pdf", "application/pdf")
        # Contract: returns the KEY, never a public URL.
        assert result == "docs/uuid-report.pdf"
        assert "storage.googleapis.com" not in result
        assert not result.startswith("http")

    @pytest.mark.asyncio
    async def test_store_does_not_make_public(self, adapter):
        ctx, blob = _patch_client()
        with ctx:
            await adapter.store(b"data", "docs/x.pdf", "application/pdf")
        # The bucket is private — adapter must never grant public access.
        blob.make_public.assert_not_called()
        # upload_from_string must carry no public predefined ACL kwarg.
        _, kwargs = blob.upload_from_string.call_args
        assert "predefined_acl" not in kwargs

    @pytest.mark.asyncio
    async def test_store_uploads_bytes_with_content_type(self, adapter):
        ctx, blob = _patch_client()
        with ctx:
            await adapter.store(b"%PDF", "docs/x.pdf", "application/pdf")
        args, kwargs = blob.upload_from_string.call_args
        assert args[0] == b"%PDF"
        assert kwargs.get("content_type") == "application/pdf"

    @pytest.mark.asyncio
    async def test_html_gets_noindex_injected(self, adapter):
        ctx, blob = _patch_client()
        with ctx:
            await adapter.store(
                b"<html><head></head><body>x</body></html>",
                "html/x.html",
                "text/html; charset=utf-8",
            )
        uploaded = blob.upload_from_string.call_args[0][0]
        assert b"noindex" in uploaded


class TestFetch:

    @pytest.mark.asyncio
    async def test_fetch_returns_bytes_via_sdk(self, adapter):
        ctx, blob = _patch_client()
        blob.download_as_bytes.return_value = b"file-content"
        with ctx:
            result = await adapter.fetch("docs/uuid-report.pdf")
        assert result == b"file-content"
        blob.download_as_bytes.assert_called_once()


class TestSignedUrl:

    @pytest.mark.asyncio
    async def test_generate_signed_url_v4_get(self, adapter):
        ctx, blob = _patch_client()
        blob.generate_signed_url.return_value = "https://signed.example/key?sig=x"
        with ctx:
            url = await adapter.generate_signed_url("docs/x.pdf", 300)
        assert url == "https://signed.example/key?sig=x"
        kwargs = blob.generate_signed_url.call_args.kwargs
        assert kwargs["version"] == "v4"
        assert kwargs["method"] == "GET"
        # ttl carried as a timedelta of the requested seconds
        assert kwargs["expiration"].total_seconds() == 300

    @pytest.mark.asyncio
    async def test_signing_uses_iam_when_sa_email_set(self):
        adapter = GcsMediaAdapter(bucket_name="b", service_account_email="sa@proj.iam")
        ctx, blob = _patch_client()
        blob.generate_signed_url.return_value = "https://signed"
        with ctx, patch("google.auth.default", return_value=(MagicMock(token="tok"), "proj")):
            await adapter.generate_signed_url("docs/x.pdf", 300)
        kwargs = blob.generate_signed_url.call_args.kwargs
        # IAM signBlob path: SA email + access token passed for keyless signing.
        assert kwargs["service_account_email"] == "sa@proj.iam"
        assert kwargs["access_token"] == "tok"

    @pytest.mark.asyncio
    async def test_signing_without_sa_email_omits_iam_kwargs(self, adapter):
        ctx, blob = _patch_client()
        blob.generate_signed_url.return_value = "https://signed"
        with ctx:
            await adapter.generate_signed_url("docs/x.pdf", 300)
        kwargs = blob.generate_signed_url.call_args.kwargs
        assert "service_account_email" not in kwargs
        assert "access_token" not in kwargs


class TestNoindexHelper:

    def test_injects_after_head(self):
        out = _inject_noindex(b"<html><head></head></html>")
        assert b"noindex" in out
        assert out.index(b"<head>") < out.index(b"noindex")

    def test_no_head_returns_unchanged(self):
        src = b"<html><body>no head</body></html>"
        assert _inject_noindex(src) == src
