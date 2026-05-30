"""
FileConversionService — centralized file upload and conversion.

Orchestrates:
  1. Upload to GCS via FileStoragePort (with Finder-style dedup)
  2. Reference-only MessagePart creation (no file content in history)
  3. On-demand content resolution (download + convert) for agents

All handlers (ConversationHandler, WorkerHandler, etc.) call this service
instead of using file_conversion utilities directly.
"""
import mimetypes
import os
import tempfile
from typing import TYPE_CHECKING, Optional

import aiofiles

from ..domain.llm import MessagePart
from ..ports.file_storage_port import FileStoragePort
from ..utils.file_conversion import (
    convert_file_to_text,
    is_native_binary,
)
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..ports.audio_transcription_port import AudioTranscriptionPort


def _format_size(size_bytes: int) -> str:
    """Human-readable file size: 1.2MB, 340KB, 512B."""
    if size_bytes >= 1_048_576:
        return f"{size_bytes / 1_048_576:.1f}MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.0f}KB"
    return f"{size_bytes}B"


class FileConversionService:
    """Centralized file upload to GCS + on-demand content resolution."""

    def __init__(
        self,
        storage: FileStoragePort,
        audio_service: Optional["AudioTranscriptionPort"] = None,
    ) -> None:
        self._storage = storage
        self._audio_service = audio_service

    async def process_attachment(
        self,
        local_path: str,
        filename: str,
        mime_type: str,
        user_id: str,
    ) -> MessagePart:
        """
        Upload file to GCS and return a reference-only MessagePart.

        For native binary (image/*, PDF): keeps local path for current turn's LLM call.
        For all types: uploads to GCS and returns reference metadata in file_data.
        No file content is stored in the MessagePart.
        """
        async with aiofiles.open(local_path, "rb") as f:
            data = await f.read()

        ref = await self._storage.upload(data, filename, user_id, mime_type)
        size_bytes = len(data)

        # Label carries the unique GCS ref (NOT the original filename): the orchestrator
        # addresses files by this name when it calls open_file / forwards to a specialist.
        # Slack names every pasted image 'image.png' → dedup gives a unique ref; showing
        # the original name would make the exact-key download land on a stale object that
        # squats the un-suffixed slot.
        label = f'[File: "{ref}" ({_format_size(size_bytes)})]'

        file_data = {
            "ref": ref,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
        }

        if filename != ref:
            file_data["original_name"] = filename

        if is_native_binary(mime_type):
            file_data["path"] = local_path

        logger.info(
            "FileConversionService: processed '%s' → ref='%s' (%s)",
            filename, ref, _format_size(size_bytes),
        )
        return MessagePart(text=label, file_data=file_data)

    async def resolve_content(
        self,
        ref: str,
        user_id: str,
    ) -> str:
        """
        Download from GCS and convert to text.

        Mime type is inferred from the filename extension — callers never pass it.
        Uses existing conversion logic: audio → transcribe, text/* → read UTF-8,
        everything else → markitdown.
        Native binary images cannot be converted — returns a descriptive message.
        PDFs are handled by markitdown (text extraction).

        Returns text wrapped in [File: ref]...[/File: ref] markers,
        or a [System: ...] alert on failure.
        """
        mime_type, _ = mimetypes.guess_type(ref)
        mime_type = mime_type or "application/octet-stream"

        data = await self._storage.download(ref, user_id)

        suffix = os.path.splitext(ref)[1] or ".bin"
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        try:
            os.close(tmp_fd)
            async with aiofiles.open(tmp_path, "wb") as f:
                await f.write(data)

            text = await convert_file_to_text(
                tmp_path, ref, mime_type,
                audio_service=self._audio_service,
            )
            return text
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                logger.debug("Failed to remove temp file %s", tmp_path)

    async def resolve_bytes(self, ref: str, user_id: str) -> bytes:
        """Download raw bytes from GCS. For specialists that need the original."""
        return await self._storage.download(ref, user_id)
