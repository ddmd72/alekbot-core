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
from ..domain.skill import DELIVERED_REF_PREFIXES, SKILL_REF_PREFIX
from ..domain.user_drive import (
    MAX_DRIVE_DOWNLOAD_BYTES,
    DriveFileTooLargeError,
    DriveItem,
    DrivePathError,
    format_size,
    parse_drive_ref,
)
from ..ports.file_storage_port import FileStoragePort
from ..utils.file_conversion import (
    MAX_FILE_BYTES,
    convert_file_to_text,
    is_native_binary,
)
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..ports.audio_transcription_port import AudioTranscriptionPort
    from ..ports.media_storage_port import MediaStoragePort
    from ..ports.user_drive_port import UserDrivePort
    from .skill_file_resolver import SkillFileResolver


def _is_delivered_key(ref: str) -> bool:
    """True if ref is a system-delivered document key (vs a user-upload filename)."""
    return ref.startswith(DELIVERED_REF_PREFIXES)


def _is_drive_ref(ref: str) -> bool:
    """True if ref addresses the user's drive (USER_DRIVE_RFC §4.3).

    `parse_drive_ref` searches anywhere in the string (label punctuation tolerance), so a
    skill ref or a delivered-document key whose *filename* contains `drive:` must keep its
    own store: those shapes are decided by prefix first.
    """
    if ref.startswith(SKILL_REF_PREFIX) or _is_delivered_key(ref):
        return False
    return parse_drive_ref(ref) is not None


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
        media_storage: Optional["MediaStoragePort"] = None,
        skill_files: Optional["SkillFileResolver"] = None,
        drive: Optional["UserDrivePort"] = None,
    ) -> None:
        self._storage = storage
        self._audio_service = audio_service
        # MediaStoragePort serves system-delivered documents (docs/, email_review/,
        # deep_research/) so agents can re-read them via open_file without an external
        # URL fetch. None → delivered-document re-read is unavailable.
        self._media_storage = media_storage
        # Resolves `skill:<name>/<path>` refs (RFC §15.4). None → skill files unavailable.
        self._skill_files = skill_files
        # drive:<id> refs — the user's long-term file area (USER_DRIVE_RFC §4.6).
        self._drive = drive

    async def _read_skill(self, ref: str, user_id: str) -> str:
        if self._skill_files is None:
            raise FileNotFoundError("Skill files are unavailable")
        return await self._skill_files.read(user_id, ref)

    async def get_drive_item(self, ref: str, user_id: str) -> DriveItem:
        """Metadata of a `drive:` ref. FileNotFoundError when it is not one or no drive is wired."""
        item_id = parse_drive_ref(ref)
        if item_id is None or self._drive is None:
            raise FileNotFoundError(f"{ref!r} is not available on the user's drive")
        return await self._drive.get_item(user_id, item_id)

    def _require_drive(self) -> "UserDrivePort":
        if self._drive is None:
            raise FileNotFoundError("The user's drive is unavailable")
        return self._drive

    async def _resolve_drive_content(self, ref: str, user_id: str) -> str:
        item = await self.get_drive_item(ref, user_id)
        if item.is_folder:
            return (f"[System: '{item.path or '/'}' is a folder, not a file. "
                    f"Use list_files_in_drive to see what is inside.]")
        if item.size_bytes > MAX_FILE_BYTES:
            return (f"[System: '{item.path}' is {format_size(item.size_bytes)}; files above "
                    f"{format_size(MAX_FILE_BYTES)} cannot be read as text yet. Long audio and video "
                    f"processing is not available yet.]")
        data = await self._require_drive().download(user_id, item.item_id)
        mime_type = item.mime_type or (mimetypes.guess_type(item.name)[0] or "application/octet-stream")
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=os.path.splitext(item.name)[1] or ".bin")
        try:
            os.close(tmp_fd)
            async with aiofiles.open(tmp_path, "wb") as f:
                await f.write(data)
            return await convert_file_to_text(tmp_path, item.path, mime_type, audio_service=self._audio_service)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                logger.debug("Failed to remove temp file %s", tmp_path)

    async def _resolve_drive_bytes(self, ref: str, user_id: str) -> bytes:
        item = await self.get_drive_item(ref, user_id)
        if item.is_folder:
            raise DrivePathError(f"'{item.path or '/'}' is a folder, not a file")
        if item.size_bytes > MAX_DRIVE_DOWNLOAD_BYTES:
            raise DriveFileTooLargeError(item, MAX_DRIVE_DOWNLOAD_BYTES)
        return await self._require_drive().download(user_id, item.item_id)

    async def _download_by_ref(self, ref: str, user_id: str) -> bytes:
        """Fetch bytes for a ref, dispatching by ref shape (no LLM decision).

        - `skill:<name>/<path>` ref → SkillFileResolver, encoded as UTF-8 (RFC §15.4).
        - Delivered-document key ("{prefix}/{user_id}/...") → MediaStoragePort.fetch,
          gated by an ownership check on the user_id path segment.
        - Bare filename → user upload via FileStoragePort.download.
        """
        if ref.startswith(SKILL_REF_PREFIX):
            return (await self._read_skill(ref, user_id)).encode("utf-8")
        if _is_delivered_key(ref):
            if not self._media_storage:
                raise FileNotFoundError(ref)
            # Ownership: the second path segment must be the requesting user.
            parts = ref.split("/")
            if len(parts) < 3 or parts[1] != user_id:
                logger.warning(
                    "FileConversionService: ownership check failed for ref=%r user=%s",
                    ref, user_id[:8] if user_id else "?",
                )
                raise PermissionError(f"ref {ref!r} does not belong to the requesting user")
            return await self._media_storage.fetch(ref)
        return await self._storage.download(ref, user_id)

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
        if ref.startswith(SKILL_REF_PREFIX):
            text = await self._read_skill(ref, user_id)
            return f"[File: {ref}]\n{text}\n[/File: {ref}]"
        if _is_drive_ref(ref):
            return await self._resolve_drive_content(ref, user_id)

        mime_type, _ = mimetypes.guess_type(ref)
        mime_type = mime_type or "application/octet-stream"

        data = await self._download_by_ref(ref, user_id)

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
        """Download raw bytes. For specialists that need the original.

        Dispatches by ref shape: delivered-document key → MediaStoragePort (with
        ownership check); bare filename → user-upload FileStoragePort.
        """
        if _is_drive_ref(ref):
            return await self._resolve_drive_bytes(ref, user_id)
        return await self._download_by_ref(ref, user_id)
