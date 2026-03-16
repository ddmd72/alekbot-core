"""
GcsMediaAdapter — MediaStoragePort implementation backed by Google Cloud Storage.

Uploads bytes as a public GCS object and returns the public URL.
Only suitable for non-PII public content (HTML widgets, map images).

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
    """Stores content in a GCS bucket and returns a public URL."""

    def __init__(self, bucket_name: str) -> None:
        self._bucket_name = bucket_name

    async def store(self, data: bytes, key: str, content_type: str) -> str:
        """Upload bytes to GCS (public), return public URL."""
        loop = asyncio.get_event_loop()
        url = await loop.run_in_executor(None, partial(self._upload_sync, data, key, content_type))
        return url

    def _upload_sync(self, data: bytes, key: str, content_type: str) -> str:
        from google.cloud import storage  # lazy import — optional at startup

        if content_type.startswith("text/html"):
            data = _inject_noindex(data)

        client = storage.Client()
        bucket = client.bucket(self._bucket_name)
        blob = bucket.blob(key)
        blob.upload_from_string(data, content_type=content_type)
        url = blob.public_url
        logger.info("GcsMediaAdapter: uploaded '%s' → %s", key, url)
        return url
