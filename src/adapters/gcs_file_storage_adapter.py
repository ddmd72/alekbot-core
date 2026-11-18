"""
GcsFileStorageAdapter — FileStoragePort implementation backed by Google Cloud Storage.

Stores user file attachments under {user_id}/files/ prefix in a shared GCS bucket.
Finder-style duplicate name resolution: report.docx → report (1).docx → report (2).docx. The write is conditional
(`if_generation_match=0`): when another upload takes the chosen name between the check and the write (two instances,
two channels), the 412 moves on to the next free name instead of replacing that file. Each write stamps a fresh
`upload_id`, so a 412 met by the client's own retry of a write that had landed is read back as success.

Filename sanitization: replaces GCS-prohibited characters (#?[]*\n\r\t:) with underscore.
"""
import asyncio
import os
import re
import uuid
from functools import partial
from typing import Optional

from google.api_core.exceptions import NotFound, PreconditionFailed

from ..ports.file_storage_port import FileStoragePort
from ..utils.logger import logger

_INVALID_GCS_CHARS = re.compile(r'[#?\[\]*\n\r\t:]')
_MAX_NAME_ATTEMPTS = 20  # names lost to concurrent uploads before giving up


def sanitize_filename(filename: str) -> str:
    """Normalize Unicode to NFC and replace GCS-prohibited characters (incl. ':', which collides
    with the `skill:` ref prefix) with underscore.

    NFC normalization is critical: Slack sends filenames in NFD (decomposed, e.g. "і" + combining
    diaeresis for "ї"), GCS stores as-is. Without NFC, upload and download paths diverge on
    Cyrillic characters with diacritics.
    """
    import unicodedata
    filename = unicodedata.normalize("NFC", filename)
    return _INVALID_GCS_CHARS.sub('_', filename)


class GcsFileStorageAdapter(FileStoragePort):
    """Stores user files in a GCS bucket under {user_id}/files/ prefix.

    The bucket is GCS_MEDIA_BUCKET, the same private bucket GcsMediaAdapter writes delivered content to (since
    2026-06-01); the two keep apart by key prefix only."""

    def __init__(self, bucket_name: str) -> None:
        self._bucket_name = bucket_name
        self._client: Optional[object] = None

    def _get_client(self):
        if self._client is None:
            from google.cloud import storage  # type: ignore[attr-defined]  # mypy resolves google.cloud to a stub namespace without it
            self._client = storage.Client()
        return self._client

    def _key(self, filename: str, user_id: str) -> str:
        return f"{user_id}/files/{filename}"

    async def upload(self, data: bytes, filename: str, user_id: str, content_type: str) -> str:
        sanitized = sanitize_filename(filename)
        upload_id = uuid.uuid4().hex
        loop = asyncio.get_event_loop()
        for _ in range(_MAX_NAME_ATTEMPTS):
            final_name = await self._deduplicate(sanitized, user_id)
            key = self._key(final_name, user_id)
            try:
                await loop.run_in_executor(None, partial(self._upload_sync, data, key, content_type, upload_id))
            except PreconditionFailed:
                if not await loop.run_in_executor(None, partial(self._upload_landed_sync, key, upload_id)):
                    logger.info("GcsFileStorageAdapter: '%s' was taken by a concurrent upload; next name", final_name)
                    continue
            logger.info("GcsFileStorageAdapter: uploaded '%s' for user %s", final_name, user_id)
            return final_name
        logger.error("GcsFileStorageAdapter: no free name for '%s' after %d attempts", sanitized, _MAX_NAME_ATTEMPTS)
        raise PreconditionFailed(f"no free name for {sanitized!r} after {_MAX_NAME_ATTEMPTS} attempts")

    async def download(self, filename: str, user_id: str) -> bytes:
        import unicodedata
        filename = unicodedata.normalize("NFC", filename)
        key = self._key(filename, user_id)
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, partial(self._download_sync, key))
        except NotFound as e:
            # Port contract: a missing file is FileNotFoundError, distinct from a storage outage.
            logger.warning("GcsFileStorageAdapter: '%s' not found for user %s", filename, user_id[:8])
            raise FileNotFoundError(filename) from e

    async def delete(self, filename: str, user_id: str) -> None:
        key = self._key(filename, user_id)
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, partial(self._delete_sync, key))
        except NotFound as e:
            # Port contract: a missing file is FileNotFoundError ("nothing to delete"), distinct from an outage.
            logger.warning("GcsFileStorageAdapter: '%s' not found for user %s", filename, user_id[:8])
            raise FileNotFoundError(filename) from e
        logger.info("GcsFileStorageAdapter: deleted '%s' for user %s", filename, user_id)

    async def exists(self, filename: str, user_id: str) -> bool:
        key = self._key(filename, user_id)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(self._exists_sync, key))

    async def get_url(self, filename: str, user_id: str) -> str:
        key = self._key(filename, user_id)
        return f"https://storage.googleapis.com/{self._bucket_name}/{key}"

    # -- Finder-style dedup --------------------------------------------------

    async def _deduplicate(self, filename: str, user_id: str) -> str:
        if not await self.exists(filename, user_id):
            return filename
        stem, ext = os.path.splitext(filename)
        n = 1
        while True:
            candidate = f"{stem} ({n}){ext}"
            if not await self.exists(candidate, user_id):
                return candidate
            n += 1

    # -- Sync GCS operations (run in executor) --------------------------------

    def _upload_sync(self, data: bytes, key: str, content_type: str, upload_id: str) -> None:
        client = self._get_client()
        bucket = client.bucket(self._bucket_name)
        blob = bucket.blob(key)
        blob.metadata = {"upload_id": upload_id}
        blob.upload_from_string(data, content_type=content_type, if_generation_match=0)

    def _upload_landed_sync(self, key: str, upload_id: str) -> bool:
        """After a 412: is the live object our own write (the client retried a request whose response was lost)?"""
        live = self._get_client().bucket(self._bucket_name).get_blob(key)
        return live is not None and (live.metadata or {}).get("upload_id") == upload_id

    def _download_sync(self, key: str) -> bytes:
        client = self._get_client()
        bucket = client.bucket(self._bucket_name)
        blob = bucket.blob(key)
        return blob.download_as_bytes()

    def _delete_sync(self, key: str) -> None:
        client = self._get_client()
        bucket = client.bucket(self._bucket_name)
        blob = bucket.blob(key)
        blob.delete()

    def _exists_sync(self, key: str) -> bool:
        client = self._get_client()
        bucket = client.bucket(self._bucket_name)
        blob = bucket.blob(key)
        return blob.exists()
