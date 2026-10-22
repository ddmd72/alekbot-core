"""Today's drive operations, moved out of FileManagementAgent (DRIVE_SPECIALIST_RFC §5.5).

One instance per user (the factory memoises it), shared by FileManagementAgent and the drive specialist, so both hold
the same per-user mutation lock, shielded tasks and late-failure notices (USER_DRIVE_RFC §4.13). Every public method
runs one operation and returns a `DriveOutcome` (src/domain/drive_outcome.py): today's text byte for byte, whether
today's code answered it as a success, its outcome class, the items touched, the vision file and the read facts.

Public interface (one call = one operation; `context` is the caller's message context; mutations take `wait_s`):

    configured -> bool                                   a drive service is wired
    async search(context, user_id, *, query, folder, kind, date_from, date_to) -> DriveOutcome
    async search_deleted(context, user_id, query) -> DriveOutcome
    async list_folder(context, user_id, folder) -> DriveOutcome
    async open(context, user_id, ref, *, original=False) -> DriveOutcome      drive ref, or a stored file (lenient)
    async fetch_stored(context, user_id, ref) -> DriveOutcome                 chat file, delivered key, skill:, workspace:
    async copy(context, user_id, source_ref, *, folder, file_name, wait_s) -> DriveOutcome
    async save_text(context, user_id, text, *, file_name, folder, wait_s) -> DriveOutcome
    async create_folder(context, user_id, folder, *, wait_s) -> DriveOutcome
    async move(context, user_id, ref, *, folder, new_name, wait_s) -> DriveOutcome
    async append(context, user_id, ref, text, *, wait_s) -> DriveOutcome
    async replace(context, user_id, ref, source_ref, *, wait_s) -> DriveOutcome
    async delete(context, user_id, ref, *, wait_s) -> DriveOutcome
    async preflight(user_id) -> DrivePreflight           the specialist's state block (§5.3)

The `_Transcripts` guard, root protection, never-overwrite, append-only and the move rules stay in UserDriveService.
This module imports no UserDrivePort and no adapter; services are constructor-injected (REQ-ARCH-14: type-only
imports). Delete and replace post receipts via notify_raw (§4.10); a delete or replace that fails after "still running"
was answered posts a late-failure notice, keyed on `DriveOp` (§4.13).
"""

import asyncio
import logging
import mimetypes
import os
import secrets
import tempfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Mapping, Optional, Sequence, Set

import aiofiles

from ..domain.drive_index_policy import TRANSCRIBED_KINDS, kind_of
from ..domain.drive_op import DriveOp
from ..domain.drive_outcome import DriveOutcome
from ..domain.drive_outcome_class import OutcomeClass
from ..domain.drive_read_facts import ReadFacts
from ..domain.drive_search import (
    REFERENCE_REMINDER, REFERENCE_REMINDER_ATTACHED, STATUS_AMBIGUOUS_FOLDER, STATUS_DISABLED, STATUS_DISCONNECTED,
    STATUS_FOLDER_NOT_FOUND, DriveSearchResult, render_results, request_from_fields, require_search_terms,
    wrap_untrusted,
)
from ..domain.drive_capture import Capture
from ..domain.drive_line import DriveLine
from ..domain.drive_preflight import DrivePreflight
from ..domain.drive_search_line import SearchLine
from ..domain.drive_tier_result import CoverageFacts
from ..domain.exceptions import WorkspaceRefused
from ..domain.language import LanguageCode
from ..domain.skill import SKILL_REF_PREFIX
from ..domain.ui_messages import UIMessage
from ..domain.user_drive import (
    MAX_DRIVE_VISION_IMAGE_BYTES,
    MAX_DRIVE_VISION_PDF_BYTES,
    VISION_IMAGE_MIME_TYPES,
    DriveFileTooLargeError,
    DriveItem,
    DriveItemNotFoundError,
    DriveMirrorWriteRefused,
    DriveNameConflictError,
    DriveNotConnectedError,
    DrivePathError,
    DriveRootProtectedError,
    drive_filename,
    drive_label,
    format_size,
    is_drive_ref,
    parse_drive_ref,
    prepare_drive_text_file,
)
from ..domain.workspace import COPY_FOLDER, WORKSPACE_REF_PREFIX, folder_of, parse_workspace_path, workspace_kind
from ..utils.file_conversion import is_native_binary
from ..utils.logger import exc_brief, logger

if TYPE_CHECKING:
    from ..ports.language_service_port import LanguageServicePort
    from ..services.drive_search_service import DriveSearchService
    from ..services.file_conversion_service import FileConversionService
    from ..services.localization_service import LocalizationService
    from ..services.user_drive_service import UserDriveService
    from ..services.user_notification_service import UserNotificationService


NOT_CONNECTED = ("The user's drive is not connected or its access expired. "
                 "Ask them to connect it in the Cabinet.")
GONE = "That item is no longer on the drive (deleted or moved out). List again."
NAME_TAKEN = "A file with that name already exists there."
NOT_CONFIGURED = "The drive is not configured."
STILL_RUNNING = ("The drive operation is still running and may complete. Check with a listing "
                 "(list_files_in_drive) before retrying; a notice follows for deletes and replaces.")
# Safety margin under Telegram's 50MB bot-API document cap (Slack's own limit is far higher). video_generation/ files
# are duration-capped (VIDEO_GENERATION_RFC.md §3.11 decision #10, 10s hard cap), so a real file almost never
# approaches this; it only makes an anomalous file degrade to a message instead of a failed upload.
MAX_VIDEO_ATTACH_BYTES = 45 * 1024 * 1024

_LATE_FAILURE_UI = {DriveOp.DELETE: UIMessage.DRIVE_DELETE_FAILED, DriveOp.REPLACE: UIMessage.DRIVE_REPLACE_FAILED}
_SCAN_CAPTURES = frozenset({Capture.SCAN.value, Capture.DOCUMENT_PHOTO.value})


def wrong_store_text(ref: str) -> str:
    """A mutation given a ref that is not on the drive (or none)."""
    if not ref:
        return "file_ref is required: the drive:<id> ref from a [Drive: ...] label."
    return f"'{ref}' is a chat file, not on the user's drive. To put it there use save_file_to_drive."


@dataclass(frozen=True)
class _LateNotice:
    """What a late-failure notice needs, armed when "still running" was answered (§4.13)."""
    context: Mapping[str, Any]
    ref: str
    user_id: str
    ui: UIMessage


class _Refusal(Exception):
    """A handler's own refusal, raised inside the shielded task and answered by the caller as today's failure."""

    def __init__(self, text: str, cls: OutcomeClass) -> None:
        super().__init__(text)
        self.text = text
        self.cls = cls


_Handler = Callable[[], Awaitable[DriveOutcome]]


class DriveOperations:
    """The drive operations of one user (§5.5). Only touched on the event loop."""

    def __init__(
        self,
        conversion_service: "FileConversionService",
        notification: Optional["UserNotificationService"] = None,
        drive_service: Optional["UserDriveService"] = None,
        localization: Optional["LocalizationService"] = None,
        language_service: Optional["LanguageServicePort"] = None,
        drive_search: Optional["DriveSearchService"] = None,
    ) -> None:
        self._drive = drive_service
        self._drive_search = drive_search
        self._localization = localization
        self._language = language_service
        self._conversion_service = conversion_service
        self._notification = notification
        # Shielded mutations still running after their caller stopped waiting (§4.13), kept so the tasks are not
        # garbage-collected mid-flight.
        self._shielded: Set["asyncio.Task[Any]"] = set()
        self._notices: Set["asyncio.Task[Any]"] = set()  # late-failure notices in flight
        # Per shielded task: the notice to post if it raises after "still running" was answered, and the human path
        # of the item it touches, recorded before the provider call.
        self._armed: Dict["asyncio.Task[Any]", _LateNotice] = {}
        self._targets: Dict["asyncio.Task[Any]", str] = {}
        # One user's drive mutations run one at a time (§4.13): a mid-flight retry waits for the first, then the
        # duplicate check sees it. dict.setdefault is atomic on the event loop.
        self._mutation_locks: Dict[str, asyncio.Lock] = {}

    @property
    def configured(self) -> bool:
        return self._drive is not None

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def search(self, context: Mapping[str, Any], user_id: str, *, query: Any = None, folder: Any = None,
                     kind: Any = None, date_from: Any = None, date_to: Any = None) -> DriveOutcome:
        """search_files_in_drive (USER_DRIVE_INDEXING_RFC §10). Returned refs go into drive_context, so "open the
        second one" works on the next turn (§10.4 History)."""
        async def run() -> DriveOutcome:
            if self._drive_search is None:
                return _fail(DriveOp.SEARCH, "Drive search is not available on this deployment.", OutcomeClass.FAILED)
            try:
                request = request_from_fields(query, folder, kind, date_from, date_to)
                require_search_terms(request)
            except ValueError as e:
                return _fail(DriveOp.SEARCH, str(e), OutcomeClass.REQUEST_ERROR)
            result = await self._drive_search.search(user_id, request)
            return DriveOutcome(op=DriveOp.SEARCH, cls=_search_class(result), text=render_results(result),
                                success=True, items=tuple(card.drive_item() for card in result.cards),
                                facts=_search_facts(result))
        return await self._call(DriveOp.SEARCH, context, user_id, run, label="")

    async def search_deleted(self, context: Mapping[str, Any], user_id: str, query: Any = None) -> DriveOutcome:
        """The deleted-files search (§27.9.11): transcripts of deleted recordings (and, with the owner-notes source,
        notes on deleted files). An empty query lists the newest. It runs with drive search off: it reads the
        ledger, not the index."""
        async def run() -> DriveOutcome:
            if self._drive_search is None:
                return _fail(DriveOp.SEARCH, "Drive search is not available on this deployment.", OutcomeClass.FAILED)
            text, items = await self._drive_search.search_deleted(user_id, str(query or ""))
            return DriveOutcome(op=DriveOp.SEARCH, cls=OutcomeClass.OK if items else OutcomeClass.NOT_FOUND,
                                text=f"{REFERENCE_REMINDER}\n{text}", success=True, items=tuple(items))
        return await self._call(DriveOp.SEARCH, context, user_id, run, label="")

    async def list_folder(self, context: Mapping[str, Any], user_id: str, folder: Any = None) -> DriveOutcome:
        """A folder path or a `drive:` folder ref, as search returns them (§26.2, Δ55)."""
        async def run() -> DriveOutcome:
            wanted = folder or None
            folder_id = parse_drive_ref(wanted) if wanted else None
            try:
                if folder_id is not None:
                    out = await self._svc().list_folder_by_id(user_id, folder_id)
                else:
                    out = await self._svc().list_folder(user_id, wanted)
            except DriveItemNotFoundError:
                note = await self._access_note(user_id, item_id=folder_id) if folder_id is not None else None
                if note is None:
                    raise
                return _fail(DriveOp.LIST, note, OutcomeClass.REFUSED, inaccessible=True)
            except DrivePathError:
                note = await self._access_note(user_id, path=wanted) if wanted and folder_id is None else None
                if note is None:
                    raise
                return _fail(DriveOp.LIST, note, OutcomeClass.REFUSED, inaccessible=True)
            lines = [drive_label(out.folder)] + [f"  {drive_label(i)}" for i in out.items]
            if not out.items:
                lines.append("  (empty)")
            if out.truncated:
                lines.append("  … more items not shown; list a subfolder.")
            return _ok(DriveOp.LIST, "\n".join(lines), [out.folder, *out.items])
        return await self._call(DriveOp.LIST, context, user_id, run, label=str(folder or ""))

    async def open(self, context: Mapping[str, Any], user_id: str, ref: str, *, original: bool = False) -> DriveOutcome:
        """A `drive:` ref opens from the drive; any other ref is opened from storage (lenient, as today)."""
        async def run() -> DriveOutcome:
            if not is_drive_ref(ref or ""):
                return await self.fetch_stored(context, user_id, ref)
            try:
                return await self._open_drive_item(context, ref, user_id, original=original)
            except DriveItemNotFoundError:
                item_id = parse_drive_ref(ref)
                note = await self._access_note(user_id, item_id=item_id) if item_id is not None else None
                if note is None:
                    raise
                return _fail(DriveOp.OPEN, note, OutcomeClass.REFUSED, inaccessible=True)
        return await self._call(DriveOp.OPEN, context, user_id, run, label=ref or "", ref=ref or "")

    # ------------------------------------------------------------------
    # Mutations (shielded, one at a time per user)
    # ------------------------------------------------------------------

    async def copy(self, context: Mapping[str, Any], user_id: str, source_ref: str, *, folder: Any = None,
                   file_name: Any = None, wait_s: float) -> DriveOutcome:
        """A chat file, delivered document, `workspace:` or `skill:` file → the drive (§4.4)."""
        async def run() -> DriveOutcome:
            ref = source_ref or ""
            # Refuse before naming: drive_filename() on a drive ref would produce "drive_<id>" (§4.4).
            if is_drive_ref(ref):
                return _fail(DriveOp.COPY, f"'{ref}' is already on the drive; use move_file_in_drive.",
                             OutcomeClass.REQUEST_ERROR)
            data = await self._source_bytes(ref, user_id, DriveOp.COPY)
            filename = drive_filename(file_name, ref)
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            return await self._store(DriveOp.COPY, user_id, data, filename, mime, folder)
        return await self._call(DriveOp.COPY, context, user_id, run, label=source_ref or "", ref=source_ref or "",
                                wait_s=wait_s)

    async def save_text(self, context: Mapping[str, Any], user_id: str, text: str, *, file_name: Any = None,
                        folder: Any = None, wait_s: float) -> DriveOutcome:
        """Text the bot composed → a new text file (§4.7.1): validated before any provider call."""
        async def run() -> DriveOutcome:
            if text == "":
                return _fail(DriveOp.SAVE_TEXT, '"text" is empty: nothing to write. Give the full content of the file.',
                             OutcomeClass.REQUEST_ERROR)
            try:
                filename, data, mime = prepare_drive_text_file(file_name, text)
            except DrivePathError as e:   # the name or the text: a request-shape error, before any provider call
                return _fail(DriveOp.SAVE_TEXT, str(e), OutcomeClass.REQUEST_ERROR)
            return await self._store(DriveOp.SAVE_TEXT, user_id, data, filename, mime, folder)
        return await self._call(DriveOp.SAVE_TEXT, context, user_id, run, label=str(folder or ""), wait_s=wait_s)

    async def create_folder(self, context: Mapping[str, Any], user_id: str, folder: Any, *,
                            wait_s: float) -> DriveOutcome:
        async def run() -> DriveOutcome:
            out = await self._svc().ensure_folder(user_id, folder or "")
            created = f" Created: {', '.join(out.created)}." if out.created else " It already existed."
            return _ok(DriveOp.CREATE_FOLDER, f"{drive_label(out.folder)}{created}", [out.folder])
        return await self._call(DriveOp.CREATE_FOLDER, context, user_id, run, label=str(folder or ""), wait_s=wait_s)

    async def move(self, context: Mapping[str, Any], user_id: str, ref: str, *, folder: Any = None,
                   new_name: Any = None, wait_s: float) -> DriveOutcome:
        """Move and/or rename in one provider call (one `svc.move`)."""
        async def run() -> DriveOutcome:
            item_id = parse_drive_ref(ref or "")
            if item_id is None:
                return _fail(DriveOp.MOVE, wrong_store_text(ref or ""), OutcomeClass.REQUEST_ERROR)
            out = await self._svc().move(user_id, item_id, folder or None, new_name or None)
            created = f" Created folders: {', '.join(out.created)}." if out.created else ""
            return _ok(DriveOp.MOVE, f"Moved: {out.before.path} → {drive_label(out.after)}.{created}", [out.after])
        return await self._call(DriveOp.MOVE, context, user_id, run, label=ref or "", ref=ref or "", wait_s=wait_s)

    async def append(self, context: Mapping[str, Any], user_id: str, ref: str, text: str, *,
                     wait_s: float) -> DriveOutcome:
        async def run() -> DriveOutcome:
            item_id = parse_drive_ref(ref or "")
            if item_id is None:
                return _fail(DriveOp.APPEND, wrong_store_text(ref or ""), OutcomeClass.REQUEST_ERROR)
            if not text:
                return _fail(DriveOp.APPEND, 'Give exactly one of "append_text" or "source_ref".',
                             OutcomeClass.REQUEST_ERROR)
            out = await self._svc().append_text(user_id, item_id, text)
            return _ok(DriveOp.APPEND, f"Appended to {drive_label(out.item)}.", [out.item])
        return await self._call(DriveOp.APPEND, context, user_id, run, label=ref or "", ref=ref or "", wait_s=wait_s)

    async def replace(self, context: Mapping[str, Any], user_id: str, ref: str, source_ref: str, *,
                      wait_s: float) -> DriveOutcome:
        async def run() -> DriveOutcome:
            item_id = parse_drive_ref(ref or "")
            if item_id is None:
                return _fail(DriveOp.REPLACE, wrong_store_text(ref or ""), OutcomeClass.REQUEST_ERROR)
            if not source_ref:
                return _fail(DriveOp.REPLACE, 'Give exactly one of "append_text" or "source_ref".',
                             OutcomeClass.REQUEST_ERROR)
            data = await self._source_bytes(source_ref, user_id, DriveOp.REPLACE)
            target = await self._svc().get_item(user_id, item_id)
            self._note_target(target.path)
            mime = target.mime_type or (mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            out = await self._svc().replace_with(user_id, item_id, data, mime)
            await self._receipt(context, user_id, UIMessage.DRIVE_REPLACED, path=out.item.path,
                                before=format_size(out.before_size), after=format_size(out.item.size_bytes))
            return _ok(DriveOp.REPLACE, f"Replaced {drive_label(out.item)}.", [out.item])
        return await self._call(DriveOp.REPLACE, context, user_id, run, label=ref or "", ref=ref or "", wait_s=wait_s)

    async def delete(self, context: Mapping[str, Any], user_id: str, ref: str, *, wait_s: float) -> DriveOutcome:
        async def run() -> DriveOutcome:
            item_id = parse_drive_ref(ref or "")
            if item_id is None:
                return _fail(DriveOp.DELETE, wrong_store_text(ref or ""), OutcomeClass.REQUEST_ERROR)
            out = await self._svc().delete(user_id, item_id)
            if out.item.is_folder:
                count = f"{out.file_count}+" if out.count_capped else str(out.file_count)
                await self._receipt(context, user_id, UIMessage.DRIVE_DELETED_FOLDER, path=out.item.path, count=count)
                return _ok(DriveOp.DELETE, f"Deleted folder {out.item.path} ({count} files).", [])
            await self._receipt(context, user_id, UIMessage.DRIVE_DELETED_FILE, path=out.item.path)
            return _ok(DriveOp.DELETE, f"Deleted {out.item.path}.", [])
        return await self._call(DriveOp.DELETE, context, user_id, run, label=ref or "", ref=ref or "", wait_s=wait_s)

    # ------------------------------------------------------------------
    # The specialist's preflight (§5.3)
    # ------------------------------------------------------------------

    async def preflight(self, user_id: str) -> DrivePreflight:
        """The state block's `drive` and `search` lines, read in parallel. A failed read is logged and leaves its line
        at the optimistic value: the operations then carry today's text for the real state."""
        if self._drive is None:
            return DrivePreflight(drive=DriveLine.NOT_CONFIGURED, search=SearchLine.OFF)

        drive_service = self._drive

        async def drive_line() -> DriveLine:
            try:
                connection = await drive_service.connection(user_id)
            except Exception as exc:
                logger.warning("[DriveOperations] connection read failed for %s: %s", user_id[:8], exc_brief(exc))
                return DriveLine.CONNECTED
            return DriveLine.of(connection)

        async def search_line() -> SearchLine:
            if self._drive_search is None:
                return SearchLine.OFF
            try:
                return await self._drive_search.search_state(user_id)
            except Exception as exc:
                logger.warning("[DriveOperations] search state read failed for %s: %s", user_id[:8], exc_brief(exc))
                return SearchLine.ON

        drive, search = await asyncio.gather(drive_line(), search_line())
        return DrivePreflight(drive=drive, search=search)

    # ------------------------------------------------------------------
    # Stored files: chat uploads, delivered documents, skill: and workspace: refs (GCS)
    # ------------------------------------------------------------------

    async def fetch_stored(self, context: Mapping[str, Any], user_id: str, ref: Any) -> DriveOutcome:
        if not ref:
            return _fail(DriveOp.OPEN, (
                "file_ref is required for open_file. "
                "Look for [File: name (size)] in the conversation and pass "
                'the filename as context={"file_ref": "<filename>"}.'
            ), OutcomeClass.REQUEST_ERROR)
        logger.info("[DriveOperations] open_file: %s", ref)
        try:
            if ref.startswith(WORKSPACE_REF_PREFIX):
                return await self._fetch_workspace(context, ref, user_id)
            # skill: refs are always text and never go through mime guessing (RFC §15.4): the ref's "path" segment
            # has its own extension, but the ref itself isn't a bare filename mimetypes can classify.
            if ref.startswith(SKILL_REF_PREFIX):
                return await self._fetch_text(ref, user_id)
            mime_type = mimetypes.guess_type(ref)[0] or "application/octet-stream"
            if is_native_binary(mime_type):
                return await self._fetch_binary(ref, user_id, mime_type)
            if mime_type.startswith("video/"):
                return await self._fetch_video(context, ref, user_id)
            return await self._fetch_text(ref, user_id)
        except FileNotFoundError as e:
            logger.warning("DriveOperations: file not found '%s'", ref)
            if ref.startswith(SKILL_REF_PREFIX):
                # The skill-specific message already names the skill/path and the fix (draft_skill / $skill save).
                error = str(e)
            elif ref.startswith(WORKSPACE_REF_PREFIX):
                error = (f"'{ref}' is not in your workspace — view the folder with the workspace tool to check "
                         "the path.")
            else:
                error = (
                    f"File '{ref}' not found in storage. "
                    f"It may have been deleted or expired (files are kept for 90 days). "
                    f"Ask the user to re-upload the file."
                )
            return _fail(DriveOp.OPEN, error, OutcomeClass.NOT_FOUND)
        except Exception as e:
            logger.error("DriveOperations: fetch failed '%s': %s", ref, e, exc_info=True)
            if ref.startswith(WORKSPACE_REF_PREFIX):
                return _fail(DriveOp.OPEN, f"The workspace could not be read right now ({type(e).__name__}); try "
                                           "again later.", OutcomeClass.FAILED)
            return _fail(DriveOp.OPEN, (
                f"Could not read file '{ref}': {type(e).__name__}. "
                f"The file exists but could not be converted to text. "
                f"Ask the user to re-upload or paste the content directly."
            ), OutcomeClass.FAILED)

    async def _fetch_workspace(self, context: Mapping[str, Any], ref: str, user_id: str) -> DriveOutcome:
        """`workspace:` refs open only under files/ (AGENT_WORKSPACE_RFC §12): a note in another folder is read with
        the workspace tool, which shows it with its view header (writer, generation). One reader per kind of file."""
        try:
            path = parse_workspace_path(ref)
        except WorkspaceRefused as e:
            return _fail(DriveOp.OPEN, str(e), OutcomeClass.REFUSED)
        if folder_of(path) != COPY_FOLDER:
            return _fail(DriveOp.OPEN,
                         f"'{ref}' is a workspace note; view it with the workspace tool (command view, path {path}).",
                         OutcomeClass.REFUSED)
        if workspace_kind(path) == "text":
            return await self._fetch_text(ref, user_id)
        mime_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if is_native_binary(mime_type):
            return await self._fetch_binary(ref, user_id, mime_type)
        if mime_type.startswith("video/"):
            return await self._fetch_video(context, ref, user_id)
        return await self._fetch_text(ref, user_id)

    async def _fetch_text(self, ref: str, user_id: str) -> DriveOutcome:
        """Fetch and convert to text (docx, txt, csv, etc.)."""
        content = await self._conversion_service.resolve_content(ref, user_id)
        return DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.OK, text=content, success=True)

    async def _fetch_binary(self, ref: str, user_id: str, mime_type: str) -> DriveOutcome:
        """Native binary (image/PDF) as file_data for LLM vision, through the conversion service so delivered-document
        keys (docs/…, deep_research/…) resolve via MediaStoragePort with the ownership check."""
        data = await self._conversion_service.resolve_bytes(ref, user_id)
        suffix = os.path.splitext(ref)[1] or ".bin"
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        os.close(tmp_fd)
        async with aiofiles.open(tmp_path, "wb") as f:
            await f.write(data)
        return DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.OK, success=True,
                            text=f"File '{ref}' ({mime_type}) is attached. You can see and analyse it directly.",
                            file_data={"path": tmp_path, "mime_type": mime_type})

    async def _fetch_video(self, context: Mapping[str, Any], ref: str, user_id: str) -> DriveOutcome:
        """A delivered video is resent as a native file attachment: it cannot be converted to text and is not native
        LLM input, so the only useful open is handing it back as a forwardable file. Delivery is a side effect."""
        data = await self._conversion_service.resolve_bytes(ref, user_id)
        if len(data) > MAX_VIDEO_ATTACH_BYTES:
            return DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.OK, success=True, text=(
                f"Video '{ref}' is too large ({len(data) / 1_048_576:.1f}MB) to "
                f"resend as a file attachment. The original share link still works."))
        if not self._notification:
            return _fail(DriveOp.OPEN, "File delivery is not configured — cannot resend the video as a file.",
                         OutcomeClass.FAILED)
        await self._notification.notify_file_bytes(
            user_id=user_id, account_id=context.get("account_id") or "", file_bytes=data,
            filename=os.path.basename(ref), title="Video",
            channel_id_override=context.get("origin_channel_id"), platform_override=context.get("origin_platform"),
        )
        return DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.OK, success=True,
                            text=f"Video '{ref}' has been sent to the user as a file attachment.")

    # ------------------------------------------------------------------
    # The call wrapper: error mapping, lock, shield, still-running (§4.13)
    # ------------------------------------------------------------------

    def _svc(self) -> "UserDriveService":
        if self._drive is None:
            raise DrivePathError(NOT_CONFIGURED)
        return self._drive

    async def _call(self, op: DriveOp, context: Mapping[str, Any], user_id: str, handler: _Handler, *,
                    label: str, ref: str = "", wait_s: Optional[float] = None) -> DriveOutcome:
        """One operation: `wait_s` set = a mutation, run shielded under the user's lock and waited for that long.
        `label` is today's log text (the file ref or the folder); `ref` the file ref an access note and a late notice
        are looked up by."""
        if self._drive is None:
            return _fail(op, NOT_CONFIGURED, OutcomeClass.FAILED)
        logger.info("[DriveOperations] %s: %s", op.value, label)
        try:
            if wait_s is None:
                return await handler()
            lock = self._mutation_locks.setdefault(user_id, asyncio.Lock())

            async def serialized() -> DriveOutcome:
                async with lock:
                    return await handler()

            # The mutation and its receipt finish whatever happens to this call (§4.13).
            task = asyncio.ensure_future(serialized())
            self._shielded.add(task)
            task.add_done_callback(self._on_mutation_done)
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=wait_s)
            except asyncio.TimeoutError:
                if task.done():
                    # The mutation ended as the wait ran out — usually with its own TimeoutError (aiohttp's socket
                    # timeouts subclass it): report the task's own outcome, so a finished mutation answers normally
                    # and its exception takes the mapping below.
                    return task.result()
                logger.warning("DriveOperations: %s still running after %.0fs", op.value, wait_s)
                self._arm_late_failure_notice(task, op, context, ref, user_id)
                return DriveOutcome(op=op, cls=OutcomeClass.STILL_RUNNING, text=STILL_RUNNING, success=True)
        except _Refusal as r:
            return _fail(op, r.text, r.cls)
        except DriveNotConnectedError:
            return _fail(op, NOT_CONNECTED, OutcomeClass.NOT_CONNECTED)
        except DriveItemNotFoundError:
            # An item the index knows the bot cannot open gets the index's sentence (§26.2, Δ55): a move, a delete or
            # an update of such a file as well as an open.
            item_id = parse_drive_ref(ref)
            note = await self._access_note(user_id, item_id=item_id) if item_id is not None else None
            if note is not None:
                return _fail(op, note, OutcomeClass.REFUSED, inaccessible=True)
            return _fail(op, GONE, OutcomeClass.NOT_FOUND)
        except FileNotFoundError:
            # The conversion service has no drive wired.
            return _fail(op, NOT_CONFIGURED, OutcomeClass.FAILED)
        except DriveNameConflictError:
            return _fail(op, NAME_TAKEN, OutcomeClass.REFUSED)
        except DriveFileTooLargeError as e:
            return _fail(op, f"'{e.item.path}' is {format_size(e.item.size_bytes)}; files above "
                             f"{format_size(e.limit_bytes)} cannot be used yet.", OutcomeClass.REFUSED)
        except (DriveRootProtectedError, DrivePathError) as e:
            return _fail(op, str(e), _path_error_class(str(e)))
        except DriveMirrorWriteRefused:
            return _fail(op, await self._mirror_refused_line(user_id), OutcomeClass.REFUSED)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("DriveOperations: drive op failed: %s", e, exc_info=True)
            return _fail(op, f"Drive operation failed: {type(e).__name__}.", OutcomeClass.FAILED)

    async def _mirror_refused_line(self, user_id: str) -> str:
        """The Step 1 guard's one line (§27.9.8), in the user's UI language (English when it cannot be resolved)."""
        lang = LanguageCode.EN
        if self._language is not None:
            try:
                lang = await self._language.resolve_ui_language(user_id)
            except Exception as exc:
                logger.warning("DriveOperations: UI language lookup failed for %s: %s", user_id[:8], exc_brief(exc))
        if self._localization is None:
            # Mirrors src/locales/en.py DRIVE_MIRROR_WRITE_REFUSED verbatim (locales load only via the adapter).
            return ("This folder is managed by the bot and its files are overwritten; "
                    "to keep your own version, copy the file to another folder.")
        return self._localization.get_ui_string(lang, UIMessage.DRIVE_MIRROR_WRITE_REFUSED)

    def _arm_late_failure_notice(self, task: "asyncio.Task[Any]", op: DriveOp, context: Mapping[str, Any], ref: str,
                                 user_id: str) -> None:
        """The caller was told "still running" for a delete/replace: if it then raises or reports failure, tell the
        user (§4.13). Called with no await between the wait timing out and here, so the task cannot finish unseen in
        between: `_on_mutation_done` (its done-callback) reads `_armed` after this."""
        ui = _LATE_FAILURE_UI.get(op)
        if ui is None:
            return  # other operations promise no notice
        self._armed[task] = _LateNotice(context, ref, user_id, ui)

    def _note_target(self, path: str) -> None:
        """Record the path a running delete/replace touches, before its provider call, so a late-failure notice names
        the file even when the provider is down. Runs inside the shielded task."""
        task = asyncio.current_task()
        if task is not None:
            self._targets[task] = path

    async def _late_failure_notice(self, notice: _LateNotice, path: Optional[str]) -> None:
        user_id, ref = notice.user_id, notice.ref
        if path is None:
            # A delete records no path (the service resolves the item itself); look it up now, falling back to the
            # ref the caller used.
            path = ref
            try:
                item_id = parse_drive_ref(ref)
                if item_id is not None:
                    path = (await self._svc().get_item(user_id, item_id)).path
            except Exception:
                logger.warning("DriveOperations: path lookup for the late-failure notice failed", exc_info=True)
        await self._receipt(notice.context, user_id, notice.ui, path=path)

    def _on_mutation_done(self, task: "asyncio.Task[Any]") -> None:
        """Log how a shielded mutation ended — nobody may be awaiting it any more (§4.13) — and post the late-failure
        notice when one was armed and the task raised or reported failure. Exactly one notice per task."""
        self._shielded.discard(task)
        armed = self._armed.pop(task, None)
        path = self._targets.pop(task, None)
        if task.cancelled():
            logger.warning("DriveOperations: drive mutation task was cancelled")
            return
        exc = task.exception()
        if exc is None:
            result = task.result()
            failed = isinstance(result, DriveOutcome) and not result.success
            logger.log(logging.WARNING if failed else logging.INFO,
                       "DriveOperations: drive mutation %s", "reported failure" if failed else "completed")
        else:
            failed = True
            logger.warning("DriveOperations: drive mutation ended with %s: %s", type(exc).__name__, exc)
        if failed and armed is not None:
            notice = asyncio.ensure_future(self._late_failure_notice(armed, path))
            self._notices.add(notice)
            notice.add_done_callback(self._notices.discard)

    async def _source_bytes(self, ref: str, user_id: str, op: DriveOp) -> bytes:
        """Bytes of a source file. A missing chat file must not read as "the drive is not configured" (that mapping
        is for the conversion service having no drive), so it is refused here."""
        try:
            data: bytes = await self._conversion_service.resolve_bytes(ref, user_id)
            return data
        except DriveItemNotFoundError:
            raise
        except FileNotFoundError:
            if is_drive_ref(ref):
                raise
            raise _Refusal(f"File '{ref}' not found in storage. It may have been deleted or "
                           f"expired. Ask the user to re-upload the file.", OutcomeClass.NOT_FOUND)

    async def _access_note(self, user_id: str, *, item_id: Optional[str] = None,
                           path: Optional[str] = None) -> Optional[str]:
        """The index's sentence for an item the bot cannot open (§26.2, Δ55), or None: no search service, an item the
        index does not know or can read, or a lookup that failed (the caller keeps its own answer)."""
        if self._drive_search is None:
            return None
        try:
            if item_id is not None:
                return await self._drive_search.access_note(user_id, item_id=item_id)
            return await self._drive_search.access_note(user_id, path=path)
        except Exception:
            logger.warning("DriveOperations: access note lookup failed", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Open a drive file
    # ------------------------------------------------------------------

    async def _transcript_of(self, user_id: str, item: DriveItem) -> Optional[str]:
        """The wrapped transcript of a recording, or None (none stored, or the lookup failed: the file branch stays)."""
        if self._drive_search is None:
            return None
        try:
            text = await self._drive_search.transcript_text(user_id, item.item_id, path=item.path,
                                                          label=drive_label(item))
        except Exception:
            logger.warning("DriveOperations: transcript lookup failed", exc_info=True)
            return None
        return text or None

    async def _archived_transcript_of(self, user_id: str, ref: str, item: DriveItem) -> Optional[str]:
        """A ref whose item is a file of the bot's `_Transcripts` folder opens from the pinned store entry, capped; with
        none, the downloaded file, capped the same way (§27.9.11). None for any other file, or when the lookup failed
        (the plain open then runs)."""
        if self._drive_search is None:
            return None
        item_id = parse_drive_ref(ref)
        if item_id is None:
            return None

        async def downloaded() -> str:
            text: str = await self._conversion_service.resolve_content(ref, user_id)
            return text

        try:
            text = await self._drive_search.archived_text(user_id, item_id, label=drive_label(item), path=item.path,
                                                          file_text=downloaded)
        except DriveItemNotFoundError:
            raise
        except Exception as exc:
            logger.warning("DriveOperations: archived transcript lookup failed: %s", exc_brief(exc))
            return None
        return text or None

    async def _open_drive_item(self, context: Mapping[str, Any], ref: str, user_id: str, *,
                               original: bool) -> DriveOutcome:
        item = await self._conversion_service.get_drive_item(ref, user_id)
        if item.is_folder:
            return _fail(DriveOp.OPEN, f"'{item.path or '/'}' is a folder; use list_files_in_drive.",
                         OutcomeClass.REQUEST_ERROR)
        archived = await self._archived_transcript_of(user_id, ref, item)
        if archived is not None:
            return _ok(DriveOp.OPEN, archived, [item])
        mime = item.mime_type or (mimetypes.guess_type(item.name)[0] or "application/octet-stream")
        # Vision caps and unsupported formats are decided on metadata, before any download (§4.6).
        if mime.startswith("image/") and mime not in VISION_IMAGE_MIME_TYPES:
            return _ok(DriveOp.OPEN, f"'{item.path}' is in a format that cannot be viewed yet ({mime}).", [item])
        vision_cap = (MAX_DRIVE_VISION_IMAGE_BYTES if mime.startswith("image/")
                      else MAX_DRIVE_VISION_PDF_BYTES if mime == "application/pdf" else None)
        # A recording is decided the way the indexer decides it: by the kind classifier (extension first), not the
        # provider's mime, so a transcribed .opus/.ogg/.mkv with a generic mime still opens as its transcript.
        recording = kind_of(item.name, item.mime_type or "", False) in TRANSCRIBED_KINDS
        if recording and original:
            # The recording itself comes as a drive link, never as bytes into chat (§27.11, Δ121).
            link = item.web_url or "(no link available)"
            body = wrap_untrusted(f"{drive_label(item)}\nLink to the original recording: {link}",
                                  nonce=secrets.token_hex(8), ref=item.ref, path=item.path)
            return _ok(DriveOp.OPEN, f"{REFERENCE_REMINDER}\n{body}", [item])
        if recording and self._drive_search is not None:
            transcript = await self._transcript_of(user_id, item)
            if transcript is not None:
                return _ok(DriveOp.OPEN, transcript, [item])
        try:
            if vision_cap is not None:
                if item.size_bytes > vision_cap:
                    raise DriveFileTooLargeError(item, vision_cap)
                data = await self._conversion_service.resolve_bytes(ref, user_id)
                tmp_fd, tmp_path = tempfile.mkstemp(suffix=os.path.splitext(item.name)[1] or ".bin")
                os.close(tmp_fd)
                async with aiofiles.open(tmp_path, "wb") as f:
                    await f.write(data)
                # The model reads an attached PDF or image natively: the reminder travels with it (D2).
                return _ok(DriveOp.OPEN, f"{REFERENCE_REMINDER_ATTACHED}\n{drive_label(item)} is "
                                         f"attached. You can see and analyse it directly.",
                           [item], file_data={"path": tmp_path, "mime_type": mime})
            if mime.startswith("video/"):
                data = await self._conversion_service.resolve_bytes(ref, user_id)
                if not self._notification:
                    return _fail(DriveOp.OPEN, "File delivery is not configured — cannot send the video.",
                                 OutcomeClass.FAILED)
                await self._notification.notify_file_bytes(
                    user_id=user_id, account_id=context.get("account_id") or "",
                    file_bytes=data, filename=item.name, title="Video",
                    channel_id_override=context.get("origin_channel_id"),
                    platform_override=context.get("origin_platform"),
                )
                return _ok(DriveOp.OPEN, f"{REFERENCE_REMINDER_ATTACHED}\n{drive_label(item)} has "
                                         f"been sent to the user as a file.", [item])
        except DriveFileTooLargeError as e:
            return _ok(DriveOp.OPEN, f"'{item.path}' is {format_size(item.size_bytes)}; files above "
                                     f"{format_size(e.limit_bytes)} cannot be opened yet.", [item])
        content = await self._conversion_service.resolve_content(ref, user_id)
        # File text is untrusted: it reaches the model only inside the wrapper (RFC §10.4, RFC Δ11).
        wrapped = wrap_untrusted(f"{drive_label(item)}\n{content}", nonce=secrets.token_hex(8),
                                 ref=item.ref, path=item.path)
        return _ok(DriveOp.OPEN, f"{REFERENCE_REMINDER}\n{wrapped}", [item])

    # ------------------------------------------------------------------
    # Writes and receipts
    # ------------------------------------------------------------------

    async def _store(self, op: DriveOp, user_id: str, data: bytes, filename: str, mime: str,
                     folder: Any) -> DriveOutcome:
        out = await self._svc().save(user_id, data, filename, mime, folder or None)
        created = f" Created folders: {', '.join(out.created)}." if out.created else ""
        if out.already_existed:
            head = f"Already on the drive: {drive_label(out.item)}."
        elif out.renamed:
            head = f'Saved as "{out.item.name}" (the name "{filename}" was taken): {drive_label(out.item)}.'
        else:
            head = f"Saved {drive_label(out.item)}."
        return _ok(op, head + created, [out.item])

    async def _receipt(self, context: Mapping[str, Any], user_id: str, ui: UIMessage, **fmt: str) -> None:
        """Deterministic chat receipt of a destructive action (§4.10). Best effort, logged on failure. The path is the
        user's or the model's text, so the receipt is sent literally."""
        if not (self._notification and self._localization):
            logger.warning("DriveOperations: drive receipt not wired (%s)", ui.value)
            return
        try:
            lang = await self._language.resolve_ui_language(user_id) if self._language else LanguageCode.EN
        except Exception:
            logger.warning("DriveOperations: UI language lookup failed for %s", user_id[:8], exc_info=True)
            lang = LanguageCode.EN
        try:
            text = self._localization.get_ui_string(lang, ui).format(**fmt)
            await self._notification.notify_raw(
                user_id, context.get("account_id") or "", text,
                channel_id_override=context.get("origin_channel_id"),
                platform_override=context.get("origin_platform"),
                literal=True,
            )
        except Exception:
            logger.warning("DriveOperations: drive receipt failed (%s)", ui.value, exc_info=True)


# ----------------------------------------------------------------------
# Outcome helpers
# ----------------------------------------------------------------------

def _ok(op: DriveOp, text: str, items: Sequence[DriveItem], *,
        file_data: Optional[Dict[str, Any]] = None) -> DriveOutcome:
    return DriveOutcome(op=op, cls=OutcomeClass.OK, text=text, success=True, items=tuple(items), file_data=file_data)


def _fail(op: DriveOp, text: str, cls: OutcomeClass, *, inaccessible: bool = False) -> DriveOutcome:
    return DriveOutcome(op=op, cls=cls, text=text, success=False, facts=ReadFacts(inaccessible=inaccessible))


def _path_error_class(text: str) -> OutcomeClass:
    """A folder the path names is not there (`No folder …`) is not_found; every other path refusal is refused."""
    return OutcomeClass.NOT_FOUND if text.startswith("No folder") else OutcomeClass.REFUSED


def _search_class(result: DriveSearchResult) -> OutcomeClass:
    """§7.2, from the typed result: the gate's states, then cards or none."""
    if result.awaiting_rebuild or (result.status == STATUS_DISCONNECTED and result.other_drive):
        return OutcomeClass.REBUILDING
    if result.status == STATUS_DISABLED:
        return OutcomeClass.INDEXING_OFF
    if result.status == STATUS_DISCONNECTED:
        return OutcomeClass.NOT_CONNECTED
    if result.status == STATUS_FOLDER_NOT_FOUND:
        return OutcomeClass.NOT_FOUND
    if result.status == STATUS_AMBIGUOUS_FOLDER:
        return OutcomeClass.OK   # candidate folders are shown: content the reader acts on
    return OutcomeClass.OK if result.cards else OutcomeClass.NOT_FOUND


def _search_facts(result: DriveSearchResult) -> ReadFacts:
    cards = result.cards
    coverage = result.tiered.coverage if result.tiered is not None else None
    incomplete = coverage is not None and coverage != CoverageFacts(building=coverage.building,
                                                                    inaccessible=coverage.inaccessible)
    return ReadFacts(
        cards=len(cards),
        passage=any(c.passage for c in cards),
        accuracy=any(c.card_accuracy is not None for c in cards),
        scan=any(c.capture in _SCAN_CAPTURES for c in cards),
        inaccessible=any(c.inaccessible for c in cards),
        image=any(c.kind == "image" for c in cards),
        coverage_incomplete=incomplete or bool(result.envelope.unread or result.envelope.not_read_yet
                                               or result.envelope.reading or result.envelope.refreshing),
        building=result.envelope.building or result.awaiting_rebuild,
    )
