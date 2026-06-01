"""
GcsMediaAdapter — MediaStoragePort implementation backed by Google Cloud Storage.

Uploads bytes as a PRIVATE GCS object and returns the object key (not a URL).
User-facing links are minted by FileLinkService; agents re-read via fetch().

Configured via GCS_MEDIA_BUCKET env variable (passed in constructor).
"""
import asyncio
from functools import partial

from ..ports.media_storage_port import MediaStoragePort
from ..utils.logger import logger


def _inject_noindex(data: bytes) -> bytes:
    """Inject <meta name="robots" content="noindex, nofollow"> after <head> tag."""
    html = data.decode("utf-8")
    lower = html.lower()
    head_idx = lower.find("<head>")
    if head_idx != -1:
        insert_at = head_idx + len("<head>")
        html = html[:insert_at] + '\n    <meta name="robots" content="noindex, nofollow">' + html[insert_at:]
        return html.encode("utf-8")
    return data


class GcsMediaAdapter(MediaStoragePort):
    """Stores content privately in a GCS bucket, addressable by object key."""

    def __init__(self, bucket_name: str, service_account_email: str | None = None) -> None:
        self._bucket_name = bucket_name
        # Used to sign V4 URLs via IAM signBlob on Cloud Run (no local key file).
        # When None (e.g. local dev with a key file), signing falls back to the
        # ambient credentials' own signing capability.
        self._sa_email = service_account_email

    async def store(self, data: bytes, key: str, content_type: str) -> str:
        """Upload bytes to a private GCS object; return the object key."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, partial(self._upload_sync, data, key, content_type)
        )

    async def fetch(self, key: str) -> bytes:
        """Read object bytes server-side via the GCS SDK (service-account auth)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(self._fetch_sync, key))

    async def generate_signed_url(self, key: str, ttl_seconds: int) -> str:
        """Mint a short-lived V4 signed URL (IAM signBlob on Cloud Run)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, partial(self._signed_url_sync, key, ttl_seconds)
        )

    def _upload_sync(self, data: bytes, key: str, content_type: str) -> str:
        from google.cloud import storage  # lazy import — optional at startup

        if content_type.startswith("text/html"):
            data = _inject_noindex(data)

        client = storage.Client()
        bucket = client.bucket(self._bucket_name)
        blob = bucket.blob(key)
        # No make_public / predefined ACL — the bucket is private; access is
        # granted only through FileLinkService-minted signed URLs.
        blob.upload_from_string(data, content_type=content_type)
        logger.info("GcsMediaAdapter: uploaded '%s' (private)", key)
        return key

    def _fetch_sync(self, key: str) -> bytes:
        from google.cloud import storage  # lazy import — optional at startup

        client = storage.Client()
        bucket = client.bucket(self._bucket_name)
        blob = bucket.blob(key)
        return blob.download_as_bytes()

    def _signed_url_sync(self, key: str, ttl_seconds: int) -> str:
        from datetime import timedelta
        from google.cloud import storage  # lazy import — optional at startup

        client = storage.Client()
        bucket = client.bucket(self._bucket_name)
        blob = bucket.blob(key)

        kwargs = {
            "version": "v4",
            "expiration": timedelta(seconds=ttl_seconds),
            "method": "GET",
        }
        # On Cloud Run there is no private key file, so blob signing must go
        # through the IAM signBlob API: provide the SA email + an access token.
        # roles/iam.serviceAccountTokenCreator on the runtime SA is required.
        if self._sa_email:
            import google.auth
            from google.auth.transport.requests import Request as AuthRequest

            credentials, _ = google.auth.default()
            credentials.refresh(AuthRequest())
            kwargs["service_account_email"] = self._sa_email
            kwargs["access_token"] = credentials.token

        url = blob.generate_signed_url(**kwargs)
        logger.info("GcsMediaAdapter: signed URL for '%s' (ttl=%ds)", key, ttl_seconds)
        return url
