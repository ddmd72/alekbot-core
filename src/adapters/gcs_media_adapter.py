"""
GcsMediaAdapter — MediaStoragePort implementation backed by Google Cloud Storage.

Uploads bytes as a PRIVATE GCS object and returns the object key (not a URL).
User-facing links are minted by FileLinkService; agents re-read via fetch().

Configured via GCS_MEDIA_BUCKET env variable (passed in constructor).
"""
import asyncio
import re
from functools import partial

from ..ports.media_storage_port import MediaStoragePort
from ..utils.logger import logger


_ROBOTS_META = b'<meta name="robots" content="noindex, nofollow">'
_HEAD_OPEN = re.compile(rb"<head(?:\s[^>]*)?>", re.IGNORECASE)  # <head>, <head lang=..>; never <header>
_HTML_OPEN = re.compile(rb"<html(?:\s[^>]*)?>", re.IGNORECASE)
_DOCTYPE = re.compile(rb"\A\s*<!doctype[^>]*>", re.IGNORECASE)


def _inject_noindex(data: bytes) -> bytes:
    """Insert the robots `noindex, nofollow` meta into an HTML page, working on bytes (no decoding).

    A model writes the page, so its head can look any way: the meta must hold for every shape. It goes right after
    the first `<head ...>` tag, else after the first `<html ...>` tag, else after the doctype (a meta before the
    doctype would switch the page to quirks mode); a fragment with none of them gets it first.
    """
    for pattern in (_HEAD_OPEN, _HTML_OPEN, _DOCTYPE):
        match = pattern.search(data)
        if match:
            at = match.end()
            return data[:at] + b"\n    " + _ROBOTS_META + data[at:]
    return _ROBOTS_META + b"\n" + data


class GcsMediaAdapter(MediaStoragePort):
    """Stores content privately in a GCS bucket, addressable by object key.

    Signed URLs are V4 GET URLs: a credential that signs locally signs them; otherwise (Cloud Run metadata
    credentials) they are signed through IAM signBlob as the refreshed runtime service account, or the constructor's
    override. The bucket's lifecycle deletes `email_review/` objects after 5 days and everything else after 30, so a
    fetch of an older key finds nothing."""

    def __init__(self, bucket_name: str, service_account_email: str | None = None) -> None:
        self._bucket_name = bucket_name
        # Optional explicit signer override for IAM signBlob. When None (the normal case) the
        # signer is the runtime credential's own service account (see _signed_url_sync).
        self._sa_email = service_account_email

    async def store(self, data: bytes, key: str, content_type: str) -> str:
        """Upload bytes to a private GCS object; return the object key."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, partial(self._upload_sync, data, key, content_type)
        )

    async def fetch(self, key: str) -> bytes:
        """Read object bytes server-side via the GCS SDK (service-account auth). A missing object (the lifecycle
        deleted it, or it was never stored) is FileNotFoundError, distinct from a storage outage."""
        from google.api_core.exceptions import NotFound

        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, partial(self._fetch_sync, key))
        except NotFound as e:
            logger.warning("GcsMediaAdapter: '%s' is not stored (expired or never written)", key)
            raise FileNotFoundError(key) from e

    async def exists(self, key: str) -> bool:
        """Whether the object is stored (the SDK's `exists` reads its metadata; a not-found is False)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(self._exists_sync, key))

    async def generate_signed_url(self, key: str, ttl_seconds: int) -> str:
        """Mint a short-lived V4 signed URL (IAM signBlob on Cloud Run)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, partial(self._signed_url_sync, key, ttl_seconds)
        )

    def _upload_sync(self, data: bytes, key: str, content_type: str) -> str:
        from google.cloud import storage  # type: ignore[attr-defined]  # lazy import — optional at startup; mypy resolves google.cloud to a stub namespace without it

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
        from google.cloud import storage  # type: ignore[attr-defined]  # lazy import — optional at startup; mypy resolves google.cloud to a stub namespace without it

        client = storage.Client()
        bucket = client.bucket(self._bucket_name)
        blob = bucket.blob(key)
        return blob.download_as_bytes()

    def _exists_sync(self, key: str) -> bool:
        from google.cloud import storage  # type: ignore[attr-defined]  # lazy import — optional at startup; mypy resolves google.cloud to a stub namespace without it

        return storage.Client().bucket(self._bucket_name).blob(key).exists()

    def _signed_url_sync(self, key: str, ttl_seconds: int) -> str:
        from datetime import timedelta
        from google.cloud import storage  # type: ignore[attr-defined]  # lazy import — optional at startup; mypy resolves google.cloud to a stub namespace without it

        client = storage.Client()
        bucket = client.bucket(self._bucket_name)
        blob = bucket.blob(key)

        kwargs = {
            "version": "v4",
            "expiration": timedelta(seconds=ttl_seconds),
            "method": "GET",
        }
        # Signer = the runtime's own identity. A credential that can sign locally (service-account key file)
        # signs as today; local dev runs under a user ADC, which has no SA email and is refused below. Otherwise (Cloud Run metadata credentials) signing goes through the IAM
        # signBlob API with the refreshed credential's own SA email + access token; that SA needs
        # signBlob on itself. The constructor email stays as an explicit override.
        import google.auth
        import google.auth.exceptions
        from google.auth.credentials import Signing
        from google.auth.transport.requests import Request as AuthRequest

        try:
            credentials, _ = google.auth.default()
        except google.auth.exceptions.DefaultCredentialsError:
            # No ambient credentials: let the storage client sign (or fail) with what it holds, as before.
            credentials = None
        if credentials is not None and not isinstance(credentials, Signing):
            credentials.refresh(AuthRequest())
            signer = self._sa_email or getattr(credentials, "service_account_email", None)
            if not signer or signer == "default":
                # "default" is the placeholder a compute credential carries before its first refresh
                # resolves the real email.
                logger.error("GcsMediaAdapter: cannot sign URL for '%s': runtime service account email unknown", key)
                raise RuntimeError("Cannot sign GCS URL: the runtime service account email is unknown")
            kwargs["service_account_email"] = signer
            kwargs["access_token"] = credentials.token

        url = blob.generate_signed_url(**kwargs)
        logger.info("GcsMediaAdapter: signed URL for '%s' (ttl=%ds)", key, ttl_seconds)
        return url
