"""
GcsFileStorageAdapter — FileStoragePort implementation backed by Google Cloud Storage.

Stores user file attachments under {user_id}/files/ prefix in a shared GCS bucket.
Finder-style duplicate name resolution: report.docx → report (1).docx → report (2).docx.

Filename sanitization: replaces GCS-prohibited characters (#?[]*\n\r\t) with underscore.
"""
import asyncio
import os
import re
from functools import partial
from typing import Optional

from ..ports.file_storage_port import FileStoragePort
from ..utils.logger import logger

_INVALID_GCS_CHARS = re.compile(r'[#?\[\]*\n\r\t]')


def sanitize_filename(filename: str) -> str:
    """Replace GCS-prohibited characters with underscore. UTF-8 (incl. Cyrillic) is allowed."""
    return _INVALID_GCS_CHARS.sub('_', filename)


class GcsFileStorageAdapter(FileStoragePort):
    """Stores user files in a GCS bucket under {user_id}/files/ prefix."""

    def __init__(self, bucket_name: str) -> None:
        self._bucket_name = bucket_name
        self._client: Optional[object] = None

    def _get_client(self):
        if self._client is None:
            from google.cloud import storage
            self._client = storage.Client()
        return self._client

    def _key(self, filename: str, user_id: str) -> str:
        return f"{user_id}/files/{filename}"

    async def upload(self, data: bytes, filename: str, user_id: str, content_type: str) -> str:
        sanitized = sanitize_filename(filename)
        final_name = await self._deduplicate(sanitized, user_id)
        key = self._key(final_name, user_id)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, partial(self._upload_sync, data, key, content_type))
        logger.info("GcsFileStorageAdapter: uploaded '%s' for user %s", final_name, user_id)
        return final_name

    async def download(self, filename: str, user_id: str) -> bytes:
        key = self._key(filename, user_id)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(self._download_sync, key))

    async def delete(self, filename: str, user_id: str) -> None:
        key = self._key(filename, user_id)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, partial(self._delete_sync, key))
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

    def _upload_sync(self, data: bytes, key: str, content_type: str) -> None:
        client = self._get_client()
        bucket = client.bucket(self._bucket_name)
        blob = bucket.blob(key)
        blob.upload_from_string(data, content_type=content_type)

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
