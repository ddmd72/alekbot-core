"""
FileConversionService — centralized file upload and conversion.

Orchestrates:
  1. Upload to GCS via FileStoragePort (with Finder-style dedup)
  2. Reference-only MessagePart creation (no file content in history)
  3. On-demand content resolution (download + convert) for agents

All handlers (ConversationHandler, WorkerHandler, etc.) call this service
instead of using file_conversion utilities directly.

A `drive:<tag>:<item_id>` ref is resolved to its connection's handle through the injected
DriveConnectionService before any drive call (MULTI_DRIVE_RFC §9); a ref that resolves to no live
connection raises DriveRefRefused with the code-built line (D13).

Access policy (MULTI_DRIVE_RFC §11.3, slice 2b): a drive ref is model-supplied, so its item is read through ONE helper,
`_drive_item`, that runs the READ check of the injected `access` (UserDriveService) after the metadata read and before
any download or any text that prints the item's path. `access` is required: a service without it would skip the check.
"""
import mimetypes
import os
import secrets
import tempfile
import uuid
from typing import TYPE_CHECKING, NamedTuple, Optional, Tuple

import aiofiles

from ..domain import drive_ref
from ..domain.drive_access_op import AccessOp
from ..domain.drive_handle import DriveHandle
from ..domain.file_ref import FileRefKind, classify_ref
from ..domain.document_kind import mime_decides_kind, mime_kind
from ..domain.document_open import OpenedFile, Prefetched
from ..domain.document_open_texts import open_failed_note, parser_unavailable_note
from ..domain.document_presentation import note_for, present_text
from ..domain.document_read import CALLER_OPEN, CALLER_PREFETCH, ReadOutcome, ReadRequest, ReadStatus
from ..domain.drive_ref_refusal import DriveRefRefusal, stale_ref_line
from ..domain.exceptions import WorkspaceRefused
from ..domain.llm import MessagePart
from ..domain.native_format import NativeFormat
from ..domain.skill import DELIVERED_REF_PREFIXES, SKILL_REF_PREFIX
from ..domain.user_drive import (
    DriveFileTooLarge,
    DriveFileTooLargeError,
    DriveItem,
    DrivePathError,
    DriveRefRefused,
    format_size,
)
from ..domain.document_limits import MAX_DRIVE_DOWNLOAD_BYTES, MAX_FILE_BYTES, MAX_TEXT_EXPORT_BYTES
from ..domain.workspace import (
    OPEN_FILE_MAX_TEXT_BYTES,
    WORKSPACE_REF_PREFIX,
    parse_workspace_path,
    workspace_kind,
)
from ..ports.file_storage_port import FileStoragePort
from ..utils.file_conversion import is_native_binary
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..ports.document_read_port import DocumentReadPort
    from ..ports.media_storage_port import MediaStoragePort
    from ..ports.user_drive_port import UserDrivePort
    from ..ports.workspace_store_port import WorkspaceStorePort
    from .drive_connection_service import DriveConnectionService
    from .skill_file_resolver import SkillFileResolver
    from .user_drive_service import UserDriveService


_KIND_SUFFIX = {"docx": ".docx", "xlsx": ".xlsx", "pptx": ".pptx", "pdf": ".pdf", "markdown": ".md",
                "text": ".txt", "csv": ".csv"}


def _too_large_line(path: str, size_bytes: int, limit_bytes: int = MAX_FILE_BYTES) -> str:
    """The size line, naming the limit that applied (the export cap for a text file, MAX_FILE_BYTES otherwise)."""
    return (f"[System: '{path}' is {format_size(size_bytes)}; files above "
            f"{format_size(limit_bytes)} cannot be read as text yet. Long audio and video "
            f"processing is not available yet.]")


class _Opening(NamedTuple):
    """An open's answer with what its presenter needs: the name shown and the bare text (no `[File:]` wrapper)."""
    opened: OpenedFile
    display: str
    text: str = ""


def _staged_name(path: str, name: str, item_mime: str) -> str:
    """The name a drive file is read under: its path, with the suffix the mime decides (a native Google Sheet has none)."""
    ext = os.path.splitext(name)[1]
    if item_mime and (not ext or mime_decides_kind(name, item_mime)):
        ext = _KIND_SUFFIX.get(mime_kind(item_mime) or "") or ext
    base, current = os.path.splitext(path)
    return path if current == ext else (base if current else path) + ext


def _drive_cap(mime: str, consumer: Tuple[NativeFormat, ...]) -> int:
    """The byte cap of a drive file that is not text: MAX_FILE_BYTES, or the consumer's declared `max_bytes` for the
    item's mime when that is larger (a native-eligible PDF up to 20 MiB must still download)."""
    bare = mime.split(";")[0].strip().lower()
    return max([MAX_FILE_BYTES] + [f.max_bytes for f in consumer if f.mime == bare])


def _native_file(outcome: ReadOutcome, content: bytes, declared_mime: str, name: str) -> OpenedFile:
    """Write the bytes of a NATIVE outcome outside any staging directory: the adapter reads the file later in the turn
    (the leak is today's, EXTERNAL_FILES_RFC §10)."""
    native_mime = outcome.native_mime or declared_mime
    suffix = os.path.splitext(name)[1] or mimetypes.guess_extension(native_mime) or ".bin"
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(content)
    return OpenedFile(outcome, native_path=path, native_mime=native_mime)


# Statuses that are the system's, not the file's: they log at ERROR, where the alerting looks (severity >= ERROR).
_SYSTEM_STATUSES = frozenset({ReadStatus.MEMORY, ReadStatus.TIMEOUT, ReadStatus.ERROR, ReadStatus.PARSER_UNAVAILABLE,
                              ReadStatus.MODEL_UNAVAILABLE, ReadStatus.STILL_WORKING})


def _log_unread(outcome: ReadOutcome, store: str, ref: str) -> None:
    """One structured line for a read that gave no text: ERROR when the system failed, WARNING when the file is at fault."""
    line = "[FileOpen] %s store=%s ref=%s kind=%s detail=%s"
    level = logger.error if outcome.status in _SYSTEM_STATUSES else logger.warning
    level(line, outcome.status.value, store, ref, outcome.kind, outcome.detail)


def _is_delivered_key(ref: str) -> bool:
    """True if ref is a system-delivered document key (vs a user-upload filename)."""
    return ref.startswith(DELIVERED_REF_PREFIXES)


def _is_workspace_ref(ref: str) -> bool:
    """`workspace:<path>` — Smart's own working files (AGENT_WORKSPACE_RFC §12)."""
    return ref.startswith(WORKSPACE_REF_PREFIX)


def _is_drive_ref(ref: str) -> bool:
    """True if ref addresses the user's drive (USER_DRIVE_RFC §4.3), tagged or not (MULTI_DRIVE_RFC §9).

    `drive_ref.is_drive_ref` searches anywhere in the string (label punctuation tolerance), so a
    skill ref, a workspace ref or a delivered-document key whose *filename* contains `drive:` must keep its
    own store: those shapes are decided by prefix first.
    """
    if ref.startswith(SKILL_REF_PREFIX) or _is_workspace_ref(ref) or _is_delivered_key(ref):
        return False
    return drive_ref.is_drive_ref(ref)


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
        media_storage: Optional["MediaStoragePort"] = None,
        skill_files: Optional["SkillFileResolver"] = None,
        drive: Optional["UserDrivePort"] = None,
        workspace: Optional["WorkspaceStorePort"] = None,
        connections: Optional["DriveConnectionService"] = None,
        *,
        access: "UserDriveService",
    ) -> None:
        self._storage = storage
        # MediaStoragePort serves system-delivered documents (docs/, email_review/,
        # deep_research/) so agents can re-read them via open_file without an external
        # URL fetch. None → delivered-document re-read is unavailable.
        self._media_storage = media_storage
        # Resolves `skill:<name>/<path>` refs (RFC §15.4). None → skill files unavailable.
        self._skill_files = skill_files
        # drive:<tag>:<id> refs — the user's drives (USER_DRIVE_RFC §4.6): the router over every drive kind and the
        # connections a ref's tag resolves to (MULTI_DRIVE_RFC §6, §9). Both or neither: without them drive refs are
        # unavailable.
        self._drive = drive
        self._connections = connections
        # workspace:<path> refs (AGENT_WORKSPACE_RFC §12) — the port, not WorkspaceService, so there is no
        # service-to-service import and no cycle (REQ-ARCH-22). None → workspace refs are unavailable.
        self._workspace = workspace
        # The access policy of the user's drives (MULTI_DRIVE_RFC §11.3): required, so an unwired service cannot skip it.
        self._access = access
        # The document service `open` and `prefetch` read through (EXTERNAL_FILES_RFC §4.4); attached by the graph
        # right after it is built (`attach_documents`), None until then.
        self._documents: Optional["DocumentReadPort"] = None

    def attach_documents(self, documents: "DocumentReadPort") -> None:
        """Attach the document service (`composition/document_service_setup.py`). Built after this service, because
        it needs the graph's quota service and context builder."""
        self._documents = documents

    async def open(self, ref: str, user_id: str, account_id: str, *, consumer: Tuple[NativeFormat, ...] = (),
                   deadline_at: float, caller: str = CALLER_OPEN) -> OpenedFile:
        """Open a ref for a model (EXTERNAL_FILES_RFC §4.2): classify and resolve it in its store (the ownership
        check on delivered keys), check its size on metadata before download, stage its bytes for this call only,
        read them through the document service with the consumer's native declaration and the one absolute
        `deadline_at` (epoch seconds), and answer an `OpenedFile`: today's `[File: <name>]` body (raw, unmarked,
        uncut; the store's presenter marks and cuts it), a `[System: …]` note, or a native file for the consumer to
        attach. `consumer` empty means text only. `caller` is `CALLER_OPEN`, `CALLER_PREFETCH` or
        `CALLER_CONNECTOR` (the drive connector: the paid reader never runs, `document_read.reader_skipped`).
        Store-specific branches (folders, recordings, the drive's §29 decision, video) stay with the callers (`DriveOperations`).
        Store errors (FileNotFoundError, PermissionError, DriveRefRefused, DriveItemNotFoundError, WorkspaceRefused)
        propagate; the callers map them. Every failed read is logged at WARNING (the file's) or ERROR (the system's)."""
        return (await self._open(ref, user_id, account_id, consumer, deadline_at, caller)).opened

    async def _open(self, ref: str, user_id: str, account_id: str, consumer: Tuple[NativeFormat, ...],
                    deadline_at: float, caller: str) -> "_Opening":
        kind = classify_ref(ref)
        if kind is FileRefKind.SKILL:
            text = await self._read_skill(ref, user_id)
            return _Opening(OpenedFile(None, body=f"[File: {ref}]\n{text}\n[/File: {ref}]"), ref, text)
        if kind is FileRefKind.EMAIL:
            raise FileNotFoundError(f"{ref!r}: email attachments open through the email tool")

        display, declared_mime, name = ref, "", ref
        if kind is FileRefKind.DRIVE:
            handle, item = await self._drive_item(ref, user_id)
            if item.is_folder:
                return _Opening(OpenedFile(None, note=(
                    f"[System: '{item.path or '/'}' is a folder, not a file. "
                    f"List the folder to see what is inside.]")), item.path)
            display = item.path
            declared_mime = item.mime_type or (mimetypes.guess_type(item.name)[0] or "application/octet-stream")
            name = _staged_name(item.path, item.name, item.mime_type)
            if declared_mime.startswith("text/"):
                # a text file's size is not the text it holds once inline images are dropped (G1b): stream it up to
                # the export cap and let the reader apply the 5 MB cap to what is left
                with tempfile.TemporaryDirectory() as staging:
                    staged = os.path.join(staging, "staged" + (os.path.splitext(name)[1] or ".bin"))
                    try:
                        await self._require_drive().download_to_file(handle, item.item_id, staged,
                                                                     MAX_TEXT_EXPORT_BYTES)
                    except DriveFileTooLarge:
                        logger.warning("[FileOpen] too_large store=drive ref=%s limit=%d", ref, MAX_TEXT_EXPORT_BYTES)
                        return _Opening(OpenedFile(None, note=_too_large_line(
                            item.path, max(item.size_bytes, MAX_TEXT_EXPORT_BYTES), MAX_TEXT_EXPORT_BYTES)), display)
                    async with aiofiles.open(staged, "rb") as f:
                        content = await f.read()
            else:
                cap = _drive_cap(declared_mime, consumer)
                if item.size_bytes > cap:
                    logger.warning("[FileOpen] too_large store=drive ref=%s size=%d limit=%d", ref, item.size_bytes, cap)
                    return _Opening(OpenedFile(None, note=_too_large_line(item.path, item.size_bytes, cap)), display)
                content = await self._require_drive().download(handle, item.item_id)
        elif kind is FileRefKind.WORKSPACE:
            path, content = await self._read_workspace(ref, user_id)
            declared_mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
            name = path
            if workspace_kind(path) == "text":
                if len(content) > OPEN_FILE_MAX_TEXT_BYTES:
                    logger.warning("[FileOpen] too_large store=workspace ref=%s size=%d limit=%d",
                                   ref, len(content), OPEN_FILE_MAX_TEXT_BYTES)
                    return _Opening(OpenedFile(None, note=(
                        f"[System: '{ref}' is {format_size(len(content))}; open_file reads a workspace text file only "
                        f"up to {format_size(OPEN_FILE_MAX_TEXT_BYTES)}. Read it in parts with the workspace tool: "
                        f"command view, path {path}, offset and limit (lines).]")), display)
                try:
                    # The skill: rule — text kinds are decoded, never mime-guessed (§12).
                    text = content.decode("utf-8")
                    return _Opening(OpenedFile(None, body=f"[File: {ref}]\n{text}\n[/File: {ref}]"), display, text)
                except UnicodeDecodeError:
                    logger.info("FileConversionService: %s is not UTF-8; reading it as a file", ref)
        else:
            content = await self._download_by_ref(ref, user_id)
            declared_mime = mimetypes.guess_type(ref)[0] or "application/octet-stream"

        if self._documents is None:
            logger.error("[FileOpen] parser_unavailable store=%s ref=%s: no document service attached", kind.value, ref)
            return _Opening(OpenedFile(None, note=parser_unavailable_note(display)), display)
        request = ReadRequest(
            content=content, name=name, declared_mime=declared_mime, store=kind.value, caller=caller,
            native_formats=consumer, user_id=user_id, account_id=account_id, deadline_at=deadline_at,
            request_id=uuid.uuid4().hex)
        try:
            outcome = await self._documents.read(request)
        except Exception:
            logger.error("[FileOpen] read raised store=%s ref=%s", kind.value, ref, exc_info=True)
            raise
        if outcome.status is ReadStatus.NATIVE:
            return _Opening(_native_file(outcome, content, declared_mime, name), display)
        if outcome.status is ReadStatus.TEXT:
            body = f"[File: {display}]\n{outcome.text}\n[/File: {display}]"
            return _Opening(OpenedFile(outcome, body=body), display, outcome.text)
        _log_unread(outcome, kind.value, ref)
        return _Opening(OpenedFile(outcome, note=note_for(outcome, display, declared_mime, size_bytes=len(content))),
                        display)

    async def prefetch(self, ref: str, user_id: str, account_id: str, *, deadline_at: float) -> Prefetched:
        """A specialist's prefetch of a ref (EXTERNAL_FILES_RFC §4.2 P18): `open` as text only, under the worker's
        `deadline_at`, answering the text as `content` (marked, cut at D20's 60 000), or a `note` the specialist
        reports when the file could not be read (a recording, a folder or a video ref gets a note too). Never
        transcribes (P21) and never raises: a store error becomes a one-line note, logged at WARNING. A skill ref's
        body is the system's own text and comes back unmarked."""
        try:
            opening = await self._open(ref, user_id, account_id, (), deadline_at, CALLER_PREFETCH)
        except Exception as e:
            logger.warning("[FileOpen] prefetch could not open ref=%s: %s: %s", ref, type(e).__name__, e)
            return Prefetched(note=open_failed_note(ref))
        opened = opening.opened
        if opened.note:
            return Prefetched(note=opened.note)
        kind = classify_ref(ref)
        if kind is FileRefKind.SKILL:
            return Prefetched(content=opened.body)
        # The presenter marks the bare text, not today's `[File:]` body.
        return Prefetched(content=present_text(opening.text, store=kind.value, ref=ref, label=opening.display,
                                               nonce=secrets.token_hex(8),
                                               outcome=opened.outcome or ReadOutcome(status=ReadStatus.TEXT)))

    async def _read_skill(self, ref: str, user_id: str) -> str:
        if self._skill_files is None:
            raise FileNotFoundError("Skill files are unavailable")
        return await self._skill_files.read(user_id, ref)

    async def get_drive_item(self, ref: str, user_id: str) -> DriveItem:
        """Metadata of a `drive:` ref. FileNotFoundError when it is not one or no drive is wired; DriveRefRefused
        (with its line) when it resolves to no live connection of the user."""
        return (await self._drive_item(ref, user_id))[1]

    async def _drive_item(self, ref: str, user_id: str) -> Tuple[DriveHandle, DriveItem]:
        """The handle and metadata of a drive ref's item, once the access policy allows READ of it (checked right after
        the metadata read, before any download or any text that prints the path)."""
        handle, item_id = await self._drive_target(ref, user_id)
        item = await self._require_drive().get_item(handle, item_id)
        await self._access.check_access(handle, AccessOp.READ, item=item)
        return handle, item

    async def _drive_target(self, ref: str, user_id: str) -> Tuple[DriveHandle, str]:
        """The handle of the connection a `drive:<tag>:<item_id>` ref names, and the item id (MULTI_DRIVE_RFC §9)."""
        if self._drive is None or self._connections is None or not drive_ref.is_drive_ref(ref):
            raise FileNotFoundError(f"{ref!r} is not available on the user's drive")
        found = await self._connections.resolve_ref(user_id, ref)
        if found is None:
            raise DriveRefRefused(stale_ref_line(ref))
        if isinstance(found, DriveRefRefusal):
            raise DriveRefRefused(found.line)
        return found.handle, found.item_id

    def _require_drive(self) -> "UserDrivePort":
        if self._drive is None:
            raise FileNotFoundError("The user's drive is unavailable")
        return self._drive

    async def _resolve_drive_bytes(self, ref: str, user_id: str) -> bytes:
        handle, item = await self._drive_item(ref, user_id)
        if item.is_folder:
            raise DrivePathError(f"'{item.path or '/'}' is a folder, not a file")
        if item.size_bytes > MAX_DRIVE_DOWNLOAD_BYTES:
            raise DriveFileTooLargeError(item, MAX_DRIVE_DOWNLOAD_BYTES)
        return await self._require_drive().download(handle, item.item_id)

    async def _read_workspace(self, ref: str, user_id: str) -> Tuple[str, bytes]:
        """The live bytes of a `workspace:` ref, for the requesting user. Any folder: draft_skill's from_file reads
        scratch/; open_file's files/-only rule lives in FileManagementAgent (§12)."""
        if self._workspace is None:
            raise FileNotFoundError("The workspace is unavailable")
        try:
            path = parse_workspace_path(ref)
        except WorkspaceRefused as e:
            raise FileNotFoundError(str(e)) from e
        blob = await self._workspace.read(user_id, path)
        return path, blob.data

    async def _download_by_ref(self, ref: str, user_id: str) -> bytes:
        """Fetch bytes for a ref, dispatching by ref shape (no LLM decision).

        - `workspace:<path>` ref → WorkspaceStorePort.read for the requesting user (AGENT_WORKSPACE_RFC §12).
        - `skill:<name>/<path>` ref → SkillFileResolver, encoded as UTF-8 (RFC §15.4).
        - Delivered-document key ("{prefix}/{user_id}/...") → MediaStoragePort.fetch,
          gated by an ownership check on the user_id path segment.
        - Bare filename → user upload via FileStoragePort.download.
        """
        if _is_workspace_ref(ref):
            return (await self._read_workspace(ref, user_id))[1]
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

    async def resolve_bytes(self, ref: str, user_id: str) -> bytes:
        """Download raw bytes. For specialists that need the original.

        Dispatches by ref shape: delivered-document key → MediaStoragePort (with
        ownership check); bare filename → user-upload FileStoragePort.
        """
        if _is_drive_ref(ref):
            return await self._resolve_drive_bytes(ref, user_id)
        return await self._download_by_ref(ref, user_id)
