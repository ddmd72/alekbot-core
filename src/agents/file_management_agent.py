"""
File Management Agent
=====================

File storage operations via uniform intent delegation.

Zero-LLM agent: no LLM calls, direct port operations.
Evolution path: add LLM for search/list/metadata queries (Phase 2).

Intents:
  open_file / delete_file         — chat files, delivered documents, skill files (GCS);
                                    open_file also opens drive: refs (lenient read)
  save_file_to_drive              — chat file / delivered document → the user's drive
  list_files_in_drive, open_file_from_drive
  move_file_in_drive, create_folder_in_drive
  update_file_in_drive (append / replace-by-file), delete_file_from_drive
  search_files_in_drive           — the drive index (USER_DRIVE_INDEXING_RFC §10)
list_files_in_drive takes a folder path or a `drive:` folder ref. When list or open meets "not found" for an item the
index knows the bot cannot open (its sharing settings keep it out), the answer is the index's sentence, not "deleted or
moved out" (USER_DRIVE_INDEXING_RFC §26.2, Δ55).
Mutating intents refuse a ref from the other store and run shielded from cancellation;
delete and replace post a receipt via notify_raw (USER_DRIVE_RFC §4.4, §4.10, §4.13).
File text and search results reach the model only inside the escaped, nonce-tagged wrapper
(USER_DRIVE_INDEXING_RFC §10.4, D2).
"""

import asyncio
import logging
import mimetypes
import os
import secrets
import tempfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional, Set

import aiofiles

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentMessage, AgentResponse, AgentConfig, AgentIntent, AgentStatus
from ..domain.drive_search import (
    REFERENCE_REMINDER, REFERENCE_REMINDER_ATTACHED, render_results, request_from_fields, require_search_terms,
    wrap_untrusted,
)
from ..domain.drive_index_policy import TRANSCRIBED_KINDS, kind_of
from ..domain.exceptions import WorkspaceRefused
from ..domain.language import LanguageCode
from ..domain.skill import SKILL_REF_PREFIX
from ..domain.ui_messages import UIMessage
from ..domain.workspace import (
    COPY_FOLDER,
    WORKSPACE_REF_PREFIX,
    folder_of,
    parse_workspace_path,
    workspace_kind,
)
from ..domain.user_drive import (
    MAX_DRIVE_VISION_IMAGE_BYTES,
    MAX_DRIVE_VISION_PDF_BYTES,
    VISION_IMAGE_MIME_TYPES,
    DriveFileTooLargeError,
    DriveItemNotFoundError,
    DriveNameConflictError,
    DriveNotConnectedError,
    DrivePathError,
    DriveRootProtectedError,
    drive_context_entry,
    drive_filename,
    drive_label,
    format_size,
    is_drive_ref,
    prepare_drive_text_file,
    parse_drive_ref,
)
from ..infrastructure.agent_manifest import Intent
from ..ports.file_storage_port import FileStoragePort
from ..utils.file_conversion import is_native_binary
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..ports.language_service_port import LanguageServicePort
    from ..services.localization_service import LocalizationService
    from ..services.drive_search_service import DriveSearchService
    from ..services.user_drive_service import UserDriveService
    from ..services.file_conversion_service import FileConversionService
    from ..services.user_notification_service import UserNotificationService


@dataclass(frozen=True)
class _LateNotice:
    """What a late-failure notice needs, armed when "still running" was answered (§4.13)."""
    message: AgentMessage
    payload: dict
    user_id: str
    ui: UIMessage


class FileManagementAgent(BaseAgent):
    """
    File storage operations for the orchestrator.

    Zero LLM calls — delegates to FileConversionService and FileStoragePort.
    """

    # Safety margin under Telegram's 50MB bot-API document cap (Slack's own
    # limit is far higher). video_generation/ files are duration-capped
    # (RFC VIDEO_GENERATION_RFC.md §3.11 decision #10, 10s hard cap) so a real
    # file almost never approaches this — it exists only as a fallback so an
    # anomalous file degrades to a message instead of a failed upload.
    MAX_VIDEO_ATTACH_BYTES = 45 * 1024 * 1024

    _NOT_CONNECTED = ("The user's drive is not connected or its access expired. "
                      "Ask them to connect it in the Cabinet.")
    _GONE = "That item is no longer on the drive (deleted or moved out). List again."
    _NAME_TAKEN = "A file with that name already exists there."
    # How long the agent waits for a mutation — inside its own 120 s timeout, so BaseAgent's
    # timeout (a false "failed" + a circuit-breaker failure) never fires for one (§4.13).
    MUTATION_WAIT_S = 100.0
    STILL_RUNNING = ("The drive operation is still running and may complete. Check with a listing "
                     "(list_files_in_drive) before retrying; a notice follows for deletes and replaces.")

    def __init__(
        self,
        config: AgentConfig,
        conversion_service: "FileConversionService",
        storage: FileStoragePort,
        notification: Optional["UserNotificationService"] = None,
        drive_service: Optional["UserDriveService"] = None,
        localization: Optional["LocalizationService"] = None,
        language_service: Optional["LanguageServicePort"] = None,
        drive_search: Optional["DriveSearchService"] = None,
    ) -> None:
        super().__init__(config)
        self._drive = drive_service
        self._drive_search = drive_search
        self._localization = localization
        self._language = language_service
        # Shielded mutations still running after their caller stopped waiting (§4.13). Kept so the
        # tasks are not garbage-collected mid-flight; only touched on the event loop.
        self._shielded: Set["asyncio.Task[Any]"] = set()
        self._notices: Set["asyncio.Task[Any]"] = set()  # late-failure notices in flight
        # Per shielded task (§4.13): the notice to post if it raises after "still running" was
        # answered, and the human path of the item it touches, recorded before the provider call.
        self._armed: Dict["asyncio.Task[Any]", "_LateNotice"] = {}
        self._targets: Dict["asyncio.Task[Any]", str] = {}
        # One user's drive mutations run one at a time (§4.13): a mid-flight retry waits for the
        # first, then the duplicate check sees it. dict.setdefault is atomic on the event loop.
        self._mutation_locks: Dict[str, asyncio.Lock] = {}
        self._conversion_service = conversion_service
        self._storage = storage
        self._notification = notification

    async def can_handle(self, message: AgentMessage) -> bool:
        return message.intent == AgentIntent.QUERY

    async def execute(self, message: AgentMessage) -> AgentResponse:
        intent = message.payload.get("intent")
        # context_schemas params are spread directly into payload by coordinator
        payload = message.payload
        user_id = message.context.get("user_id", "")

        if intent == Intent.OPEN_FILE:
            if is_drive_ref(payload.get("file_ref") or ""):
                return await self._drive_call(message, self._open_drive, payload, user_id)
            return await self._fetch(message, payload, user_id)

        if intent == Intent.DELETE_FILE:
            ref = payload.get("file_ref") or ""
            if is_drive_ref(ref):
                return self._fail(message, f"'{ref}' is on the user's drive; use delete_file_from_drive.")
            return await self._delete(message, payload, user_id)

        reads = {
            Intent.OPEN_FILE_FROM_DRIVE: self._open_drive,
            Intent.LIST_FILES_IN_DRIVE: self._list_drive,
            Intent.SEARCH_FILES_IN_DRIVE: self._search_drive,
        }
        writes = {
            Intent.SAVE_FILE_TO_DRIVE: self._save_to_drive,
            Intent.MOVE_FILE_IN_DRIVE: self._move_in_drive,
            Intent.CREATE_FOLDER_IN_DRIVE: self._create_folder_in_drive,
            Intent.UPDATE_FILE_IN_DRIVE: self._update_in_drive,
            Intent.DELETE_FILE_FROM_DRIVE: self._delete_from_drive,
        }
        if intent in reads:
            return await self._drive_call(message, reads[intent], payload, user_id)
        if intent in writes:
            return await self._drive_call(message, writes[intent], payload, user_id, shielded=True)

        logger.warning(
            "FileManagementAgent: unknown intent '%s'", intent,
        )
        return AgentResponse.failure(
            task_id=message.task_id,
            agent_id=self.agent_id,
            error=(
                f"File agent does not support intent '{intent}'. "
                f"Supported intents: open_file, delete_file, save_file_to_drive, list_files_in_drive, "
                f"open_file_from_drive, move_file_in_drive, create_folder_in_drive, "
                f"update_file_in_drive, delete_file_from_drive, search_files_in_drive."
            ),
        )

    async def _fetch(
        self, message: AgentMessage, payload: dict, user_id: str,
    ) -> AgentResponse:
        ref = payload.get("file_ref")
        if not ref:
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=(
                    "file_ref is required for open_file. "
                    "Look for [File: name (size)] in the conversation and pass "
                    'the filename as context={"file_ref": "<filename>"}.'
                ),
            )

        self._on_agent_start(f"open_file: {ref}")

        try:
            if ref.startswith(WORKSPACE_REF_PREFIX):
                return await self._fetch_workspace(message, ref, user_id)
            # skill: refs are always text and never go through mime guessing (RFC §15.4) —
            # the ref's "path" segment has its own extension, but the ref itself isn't a
            # bare filename mimetypes can classify.
            if ref.startswith(SKILL_REF_PREFIX):
                return await self._fetch_text(message, ref, user_id)

            mime_type, _ = mimetypes.guess_type(ref)
            mime_type = mime_type or "application/octet-stream"

            if is_native_binary(mime_type):
                return await self._fetch_binary(message, ref, user_id, mime_type)
            elif mime_type.startswith("video/"):
                return await self._fetch_video(message, ref, user_id)
            else:
                return await self._fetch_text(message, ref, user_id)
        except FileNotFoundError as e:
            logger.warning("FileManagementAgent: file not found '%s'", ref)
            if ref.startswith(SKILL_REF_PREFIX):
                # Skill-specific message already names the skill/path and the fix
                # (draft_skill / $skill save) — "re-upload" doesn't apply to a skill file.
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
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=error,
            )
        except Exception as e:
            logger.error("FileManagementAgent: fetch failed '%s': %s", ref, e, exc_info=True)
            self._on_agent_error(e, f"fetch {ref}")
            if ref.startswith(WORKSPACE_REF_PREFIX):
                return self._fail(
                    message, f"The workspace could not be read right now ({type(e).__name__}); try again later.",
                )
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=(
                    f"Could not read file '{ref}': {type(e).__name__}. "
                    f"The file exists but could not be converted to text. "
                    f"Ask the user to re-upload or paste the content directly."
                ),
            )

    async def _fetch_workspace(self, message: AgentMessage, ref: str, user_id: str) -> AgentResponse:
        """`workspace:` refs open only under files/ (AGENT_WORKSPACE_RFC §12): a note in another folder is read with
        the workspace tool, which shows it with its view header (writer, generation). One reader per kind of file."""
        try:
            path = parse_workspace_path(ref)
        except WorkspaceRefused as e:
            return self._fail(message, str(e))
        if folder_of(path) != COPY_FOLDER:
            return self._fail(
                message,
                f"'{ref}' is a workspace note; view it with the workspace tool (command view, path {path}).",
            )
        if workspace_kind(path) == "text":
            return await self._fetch_text(message, ref, user_id)
        mime_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if is_native_binary(mime_type):
            return await self._fetch_binary(message, ref, user_id, mime_type)
        if mime_type.startswith("video/"):
            return await self._fetch_video(message, ref, user_id)
        return await self._fetch_text(message, ref, user_id)

    async def _fetch_text(
        self, message: AgentMessage, ref: str, user_id: str,
    ) -> AgentResponse:
        """Fetch and convert to text (docx, txt, csv, etc.)."""
        content = await self._conversion_service.resolve_content(ref, user_id)
        self._on_agent_success(char_count=len(content), output_text=content[:200])
        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=content,
            confidence=1.0,
        )

    async def _fetch_binary(
        self, message: AgentMessage, ref: str, user_id: str, mime_type: str,
    ) -> AgentResponse:
        """Fetch native binary (image/PDF) and return as file_data for LLM vision.

        Routed through the conversion service so delivered-document keys (docs/…,
        deep_research/…) resolve via MediaStoragePort with the ownership check,
        not only bare-filename user uploads.
        """
        data = await self._conversion_service.resolve_bytes(ref, user_id)

        suffix = os.path.splitext(ref)[1] or ".bin"
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        os.close(tmp_fd)
        async with aiofiles.open(tmp_path, "wb") as f:
            await f.write(data)

        file_data = {"path": tmp_path, "mime_type": mime_type}

        self._on_agent_success(
            char_count=len(data),
            output_text=f"Binary file {ref} ({mime_type}, {len(data)} bytes)",
        )
        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=f"File '{ref}' ({mime_type}) is attached. You can see and analyse it directly.",
            confidence=1.0,
            metadata={"file_data": file_data},
        )

    async def _fetch_video(
        self, message: AgentMessage, ref: str, user_id: str,
    ) -> AgentResponse:
        """Fetch a delivered video and resend it as a native file attachment.

        Video can't be converted to text (markitdown doesn't parse mp4) and isn't
        native-binary LLM input like image/PDF — the only useful thing open_file
        can do with one is hand it back to the user as an actual forwardable file,
        not a link. Delivery is a side effect (via UserNotificationService), not
        content returned to the LLM.
        """
        data = await self._conversion_service.resolve_bytes(ref, user_id)

        if len(data) > self.MAX_VIDEO_ATTACH_BYTES:
            return AgentResponse.success(
                task_id=message.task_id,
                agent_id=self.agent_id,
                result=(
                    f"Video '{ref}' is too large ({len(data) / 1_048_576:.1f}MB) to "
                    f"resend as a file attachment. The original share link still works."
                ),
                confidence=1.0,
            )

        if not self._notification:
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="File delivery is not configured — cannot resend the video as a file.",
            )

        account_id = message.context.get("account_id") or ""
        await self._notification.notify_file_bytes(
            user_id=user_id,
            account_id=account_id,
            file_bytes=data,
            filename=os.path.basename(ref),
            title="Video",
            channel_id_override=message.context.get("origin_channel_id"),
            platform_override=message.context.get("origin_platform"),
        )

        self._on_agent_success(
            char_count=len(data),
            output_text=f"Video {ref} resent as file ({len(data)} bytes)",
        )
        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=f"Video '{ref}' has been sent to the user as a file attachment.",
            confidence=1.0,
        )

    async def _delete(
        self, message: AgentMessage, payload: dict, user_id: str,
    ) -> AgentResponse:
        ref = payload.get("file_ref")
        if not ref:
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=(
                    "file_ref is required for delete_file. "
                    'Pass the filename as context={"file_ref": "<filename>"}.'
                ),
            )

        self._on_agent_start(f"delete_file: {ref}")

        if ref.startswith(WORKSPACE_REF_PREFIX):
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=(f"'{ref}' is in the workspace; delete it with the workspace tool's delete (history keeps "
                       "the version)."),
            )

        if ref.startswith(SKILL_REF_PREFIX):
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=(
                    f"'{ref}' belongs to a skill. Skill files change only through a new skill "
                    f"version (draft_skill) or are removed with the owner's `$skill delete <name>`."
                ),
            )

        try:
            await self._storage.delete(ref, user_id)
        except FileNotFoundError:
            logger.warning("FileManagementAgent: file not found for deletion '%s'", ref)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"File '{ref}' not found in storage — nothing to delete.",
            )
        except Exception as e:
            logger.error("FileManagementAgent: delete failed '%s': %s", ref, e, exc_info=True)
            self._on_agent_error(e, f"delete {ref}")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Could not delete file '{ref}': {type(e).__name__}.",
            )

        result = f"File '{ref}' deleted."
        self._on_agent_success(char_count=len(result), output_text=result)
        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=result,
            confidence=1.0,
        )

    # ------------------------------------------------------------------
    # User drive (USER_DRIVE_RFC §4.4-§4.13)
    # ------------------------------------------------------------------

    def _svc(self) -> "UserDriveService":
        if self._drive is None:
            raise DrivePathError("The drive is not configured.")
        return self._drive

    def _fail(self, message: AgentMessage, error: str) -> AgentResponse:
        return AgentResponse.failure(task_id=message.task_id, agent_id=self.agent_id, error=error)

    def _ok(self, message: AgentMessage, result: str, touched: list, **extra: Any) -> AgentResponse:
        self._on_agent_success(char_count=len(result), output_text=result[:200])
        return AgentResponse.success(
            task_id=message.task_id, agent_id=self.agent_id, result=result, confidence=1.0,
            history_context={"drive_context": [drive_context_entry(i) for i in touched]} if touched else None,
            **extra,
        )

    def _wrong_store(self, message: AgentMessage, ref: str) -> AgentResponse:
        if not ref:
            return self._fail(message, "file_ref is required: the drive:<id> ref from a [Drive: ...] label.")
        return self._fail(message, f"'{ref}' is a chat file, not on the user's drive. "
                                   f"To put it there use save_file_to_drive.")

    async def _drive_call(
        self, message: AgentMessage,
        handler: Callable[[AgentMessage, dict, str], Awaitable[AgentResponse]],
        payload: dict, user_id: str, *, shielded: bool = False,
    ) -> AgentResponse:
        if self._drive is None:
            return self._fail(message, "The drive is not configured.")
        self._on_agent_start(f"{payload.get('intent')}: {payload.get('file_ref') or payload.get('folder') or ''}")
        try:
            if not shielded:
                return await handler(message, payload, user_id)
            lock = self._mutation_locks.setdefault(user_id, asyncio.Lock())

            async def serialized() -> AgentResponse:
                async with lock:
                    return await handler(message, payload, user_id)

            # The mutation and its receipt finish whatever happens to this call (§4.13).
            task = asyncio.ensure_future(serialized())
            self._shielded.add(task)
            task.add_done_callback(self._on_mutation_done)
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=self.MUTATION_WAIT_S)
            except asyncio.TimeoutError:
                if task.done():
                    # The mutation ended as the wait ran out — usually with its own TimeoutError
                    # (aiohttp's socket timeouts subclass it): report the task's own outcome, so a
                    # finished mutation answers normally and its exception takes the mapping below.
                    return task.result()
                logger.warning("FileManagementAgent: %s still running after %.0fs", payload.get("intent"),
                               self.MUTATION_WAIT_S)
                self._arm_late_failure_notice(task, message, payload, user_id)
                return self._ok(message, self.STILL_RUNNING, [])
        except DriveNotConnectedError:
            return self._fail(message, self._NOT_CONNECTED)
        except DriveItemNotFoundError:
            # an item the index knows the bot cannot open gets the index's sentence (§26.2, Δ55): a move, a delete or
            # an update of such a file as well as an open
            item_id = parse_drive_ref(payload.get("file_ref") or "")
            note = await self._access_note(user_id, item_id=item_id) if item_id is not None else None
            return self._fail(message, note or self._GONE)
        except FileNotFoundError:
            # The conversion service has no drive wired.
            return self._fail(message, "The drive is not configured.")
        except DriveNameConflictError:
            return self._fail(message, self._NAME_TAKEN)
        except DriveFileTooLargeError as e:
            return self._fail(message, f"'{e.item.path}' is {format_size(e.item.size_bytes)}; files above "
                                       f"{format_size(e.limit_bytes)} cannot be used yet.")
        except (DriveRootProtectedError, DrivePathError) as e:
            return self._fail(message, str(e))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("FileManagementAgent: drive op failed: %s", e, exc_info=True)
            self._on_agent_error(e, str(payload.get("intent")))
            return self._fail(message, f"Drive operation failed: {type(e).__name__}.")

    _LATE_FAILURE_UI = {
        Intent.DELETE_FILE_FROM_DRIVE: UIMessage.DRIVE_DELETE_FAILED,
        Intent.UPDATE_FILE_IN_DRIVE: UIMessage.DRIVE_REPLACE_FAILED,
    }

    def _arm_late_failure_notice(self, task: "asyncio.Task[Any]", message: AgentMessage,
                                 payload: dict, user_id: str) -> None:
        """The model was told "still running" for a delete/replace: if it then raises or reports failure,
        tell the user (§4.13).

        Called with no await between the wait timing out and here, so the task cannot finish
        unseen in between: `_on_mutation_done` (its done-callback) reads `_armed` after this."""
        intent = payload.get("intent")
        ui = self._LATE_FAILURE_UI.get(intent)
        if ui is None or (intent == Intent.UPDATE_FILE_IN_DRIVE and not payload.get("source_ref")):
            return  # other intents promise no notice; an append is not a replace
        self._armed[task] = _LateNotice(message, payload, user_id, ui)

    def _note_target(self, path: str) -> None:
        """Record the path a running delete/replace touches, before its provider call, so a late-failure
        notice names the file even when the provider is down. Runs inside the shielded task."""
        task = asyncio.current_task()
        if task is not None:
            self._targets[task] = path

    async def _late_failure_notice(self, notice: "_LateNotice", path: Optional[str]) -> None:
        payload, user_id = notice.payload, notice.user_id
        ref = payload.get("file_ref") or ""
        if path is None:
            # A delete records no path (the service resolves the item itself); look it up now,
            # falling back to the ref the model used.
            path = ref
            try:
                item_id = parse_drive_ref(ref)
                if item_id is not None:
                    path = (await self._svc().get_item(user_id, item_id)).path
            except Exception:
                logger.warning("FileManagementAgent: path lookup for the late-failure notice failed", exc_info=True)
        await self._receipt(notice.message, user_id, notice.ui, path=path)

    def _on_mutation_done(self, task: "asyncio.Task[Any]") -> None:
        """Log how a shielded mutation ended — nobody may be awaiting it any more (§4.13) — and post
        the late-failure notice when one was armed and the task raised. Exactly one notice per task."""
        self._shielded.discard(task)
        armed = self._armed.pop(task, None)
        path = self._targets.pop(task, None)
        if task.cancelled():
            logger.warning("FileManagementAgent: drive mutation task was cancelled")
            return
        exc = task.exception()
        if exc is None:
            result = task.result()
            failed = isinstance(result, AgentResponse) and result.status != AgentStatus.SUCCESS
            logger.log(logging.WARNING if failed else logging.INFO,
                       "FileManagementAgent: drive mutation %s", "reported failure" if failed else "completed")
        else:
            failed = True
            logger.warning("FileManagementAgent: drive mutation ended with %s: %s", type(exc).__name__, exc)
        if failed and armed is not None:
            notice = asyncio.ensure_future(self._late_failure_notice(armed, path))
            self._notices.add(notice)
            notice.add_done_callback(self._notices.discard)

    async def _source_bytes(self, message: AgentMessage, ref: str, user_id: str) -> Any:
        """Bytes of a source file, or an AgentResponse failure when a chat file is missing.

        A missing chat file must not read as "the drive is not configured" (that mapping is for
        the conversion service having no drive), so it is answered here."""
        try:
            return await self._conversion_service.resolve_bytes(ref, user_id)
        except DriveItemNotFoundError:
            raise
        except FileNotFoundError:
            if is_drive_ref(ref):
                raise
            return self._fail(message, f"File '{ref}' not found in storage. It may have been deleted or "
                                       f"expired. Ask the user to re-upload the file.")

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
            logger.warning("FileManagementAgent: access note lookup failed", exc_info=True)
            return None

    async def _open_drive(self, message: AgentMessage, payload: dict, user_id: str) -> AgentResponse:
        ref = payload.get("file_ref") or ""
        if not is_drive_ref(ref):
            return await self._fetch(message, payload, user_id)  # lenient read
        try:
            return await self._open_drive_item(message, ref, user_id, payload)
        except DriveItemNotFoundError:
            item_id = parse_drive_ref(ref)
            note = await self._access_note(user_id, item_id=item_id) if item_id is not None else None
            if note is None:
                raise
            return self._fail(message, note)

    async def _transcript_of(self, user_id: str, item: Any) -> Optional[str]:
        """The wrapped transcript of a recording, or None (none stored, or the lookup failed: the file branch stays)."""
        try:
            text = await self._drive_search.transcript_text(user_id, item.item_id, path=item.path,
                                                          label=drive_label(item))
        except Exception:
            logger.warning("FileManagementAgent: transcript lookup failed", exc_info=True)
            return None
        return text or None

    async def _open_drive_item(self, message: AgentMessage, ref: str, user_id: str,
                               payload: Optional[dict] = None) -> AgentResponse:
        item = await self._conversion_service.get_drive_item(ref, user_id)
        if item.is_folder:
            return self._fail(message, f"'{item.path or '/'}' is a folder; use list_files_in_drive.")
        mime = item.mime_type or (mimetypes.guess_type(item.name)[0] or "application/octet-stream")
        # Vision caps and unsupported formats are decided on metadata, before any download (§4.6).
        if mime.startswith("image/") and mime not in VISION_IMAGE_MIME_TYPES:
            return self._ok(message, f"'{item.path}' is in a format that cannot be viewed yet ({mime}).", [item])
        vision_cap = (MAX_DRIVE_VISION_IMAGE_BYTES if mime.startswith("image/")
                      else MAX_DRIVE_VISION_PDF_BYTES if mime == "application/pdf" else None)
        # A recording is decided the way the indexer decides it: by the kind classifier (extension first), not the
        # provider's mime, so a transcribed .opus/.ogg/.mkv with a generic mime still opens as its transcript.
        recording = kind_of(item.name, item.mime_type or "", False) in TRANSCRIBED_KINDS
        if recording and (payload or {}).get("original") is True:
            # The recording itself comes as a drive link, never as bytes into chat (§27.11, Δ121).
            link = item.web_url or "(no link available)"
            body = wrap_untrusted(f"{drive_label(item)}\nLink to the original recording: {link}",
                                  nonce=secrets.token_hex(8), ref=item.ref, path=item.path)
            return self._ok(message, f"{REFERENCE_REMINDER}\n{body}", [item])
        if recording and self._drive_search is not None:
            transcript = await self._transcript_of(user_id, item)
            if transcript is not None:
                return self._ok(message, transcript, [item])
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
                return self._ok(message, f"{REFERENCE_REMINDER_ATTACHED}\n{drive_label(item)} is "
                                         f"attached. You can see and analyse it directly.",
                                [item], metadata={"file_data": {"path": tmp_path, "mime_type": mime}})
            if mime.startswith("video/"):
                data = await self._conversion_service.resolve_bytes(ref, user_id)
                if not self._notification:
                    return self._fail(message, "File delivery is not configured — cannot send the video.")
                await self._notification.notify_file_bytes(
                    user_id=user_id, account_id=message.context.get("account_id") or "",
                    file_bytes=data, filename=item.name, title="Video",
                    channel_id_override=message.context.get("origin_channel_id"),
                    platform_override=message.context.get("origin_platform"),
                )
                return self._ok(message, f"{REFERENCE_REMINDER_ATTACHED}\n{drive_label(item)} has "
                                         f"been sent to the user as a file.", [item])
        except DriveFileTooLargeError as e:
            return self._ok(message, f"'{item.path}' is {format_size(item.size_bytes)}; files above "
                                     f"{format_size(e.limit_bytes)} cannot be opened yet.", [item])
        content = await self._conversion_service.resolve_content(ref, user_id)
        # File text is untrusted: it reaches the model only inside the wrapper (RFC §10.4, RFC Δ11).
        wrapped = wrap_untrusted(f"{drive_label(item)}\n{content}", nonce=secrets.token_hex(8),
                                 ref=item.ref, path=item.path)
        return self._ok(message, f"{REFERENCE_REMINDER}\n{wrapped}", [item])

    async def _search_drive(self, message: AgentMessage, payload: dict, user_id: str) -> AgentResponse:
        """search_files_in_drive (USER_DRIVE_INDEXING_RFC §10). Returned refs go into drive_context, so
        "open the second one" works on the next turn (§10.4 History)."""
        if self._drive_search is None:
            return self._fail(message, "Drive search is not available on this deployment.")
        try:
            request = request_from_fields(payload.get("drive_query"), payload.get("drive_folder"),
                                          payload.get("drive_kind"), payload.get("drive_date_from"),
                                          payload.get("drive_date_to"))
            require_search_terms(request)
        except ValueError as e:
            return self._fail(message, str(e))
        result = await self._drive_search.search(user_id, request)
        return self._ok(message, render_results(result), [card.drive_item() for card in result.cards])

    async def _save_to_drive(self, message: AgentMessage, payload: dict, user_id: str) -> AgentResponse:
        ref = payload.get("file_ref") or ""
        text = payload.get("text")
        has_text = isinstance(text, str) and text != ""
        if text == "" and not ref:
            return self._fail(message, '"text" is empty: nothing to write. Give the full content of the file.')
        if bool(ref) == has_text:
            return self._fail(message, 'Give exactly one of "file_ref" (a file to keep) or "text" + "name" '
                                       '(a new text file).')
        if has_text:  # §4.7.1: validated before any provider call
            filename, data, mime = prepare_drive_text_file(payload.get("name"), text)
            return await self._store(message, user_id, data, filename, mime, payload.get("folder"))
        # Refuse before naming: drive_filename() on a drive ref would produce "drive_<id>" (§4.4).
        if is_drive_ref(ref):
            return self._fail(message, f"'{ref}' is already on the drive; use move_file_in_drive.")
        data = await self._source_bytes(message, ref, user_id)
        if isinstance(data, AgentResponse):
            return data
        filename = drive_filename(payload.get("name"), ref)
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return await self._store(message, user_id, data, filename, mime, payload.get("folder"))

    async def _store(self, message: AgentMessage, user_id: str, data: bytes, filename: str, mime: str,
                     folder: Optional[str]) -> AgentResponse:
        out = await self._svc().save(user_id, data, filename, mime, folder or None)
        created = f" Created folders: {', '.join(out.created)}." if out.created else ""
        if out.already_existed:
            head = f"Already on the drive: {drive_label(out.item)}."
        elif out.renamed:
            head = f'Saved as "{out.item.name}" (the name "{filename}" was taken): {drive_label(out.item)}.'
        else:
            head = f"Saved {drive_label(out.item)}."
        return self._ok(message, head + created, [out.item])

    async def _list_drive(self, message: AgentMessage, payload: dict, user_id: str) -> AgentResponse:
        folder = payload.get("folder") or None
        folder_id = parse_drive_ref(folder) if folder else None
        try:
            if folder_id is not None:   # a `drive:` folder ref, as search returns them (§26.2, Δ55)
                out = await self._svc().list_folder_by_id(user_id, folder_id)
            else:
                out = await self._svc().list_folder(user_id, folder)
        except DriveItemNotFoundError:
            note = await self._access_note(user_id, item_id=folder_id) if folder_id is not None else None
            if note is None:
                raise
            return self._fail(message, note)
        except DrivePathError:
            note = await self._access_note(user_id, path=folder) if folder and folder_id is None else None
            if note is None:
                raise
            return self._fail(message, note)
        lines = [drive_label(out.folder)] + [f"  {drive_label(i)}" for i in out.items]
        if not out.items:
            lines.append("  (empty)")
        if out.truncated:
            lines.append("  … more items not shown; list a subfolder.")
        return self._ok(message, "\n".join(lines), [out.folder, *out.items])

    async def _move_in_drive(self, message: AgentMessage, payload: dict, user_id: str) -> AgentResponse:
        item_id = parse_drive_ref(payload.get("file_ref") or "")
        if item_id is None:
            return self._wrong_store(message, payload.get("file_ref") or "")
        out = await self._svc().move(user_id, item_id, payload.get("folder") or None, payload.get("new_name") or None)
        created = f" Created folders: {', '.join(out.created)}." if out.created else ""
        return self._ok(message, f"Moved: {out.before.path} → {drive_label(out.after)}.{created}", [out.after])

    async def _create_folder_in_drive(self, message: AgentMessage, payload: dict, user_id: str) -> AgentResponse:
        out = await self._svc().ensure_folder(user_id, payload.get("folder") or "")
        created = f" Created: {', '.join(out.created)}." if out.created else " It already existed."
        return self._ok(message, f"{drive_label(out.folder)}{created}", [out.folder])

    async def _update_in_drive(self, message: AgentMessage, payload: dict, user_id: str) -> AgentResponse:
        item_id = parse_drive_ref(payload.get("file_ref") or "")
        if item_id is None:
            return self._wrong_store(message, payload.get("file_ref") or "")
        append_text, source_ref = payload.get("append_text"), payload.get("source_ref")
        if bool(append_text) == bool(source_ref):
            return self._fail(message, 'Give exactly one of "append_text" or "source_ref".')
        if append_text:
            out = await self._svc().append_text(user_id, item_id, append_text)
            return self._ok(message, f"Appended to {drive_label(out.item)}.", [out.item])
        data = await self._source_bytes(message, source_ref, user_id)
        if isinstance(data, AgentResponse):
            return data
        target = await self._svc().get_item(user_id, item_id)
        self._note_target(target.path)
        mime = target.mime_type or (mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        out = await self._svc().replace_with(user_id, item_id, data, mime)
        await self._receipt(message, user_id, UIMessage.DRIVE_REPLACED, path=out.item.path,
                            before=format_size(out.before_size), after=format_size(out.item.size_bytes))
        return self._ok(message, f"Replaced {drive_label(out.item)}.", [out.item])

    async def _delete_from_drive(self, message: AgentMessage, payload: dict, user_id: str) -> AgentResponse:
        item_id = parse_drive_ref(payload.get("file_ref") or "")
        if item_id is None:
            return self._wrong_store(message, payload.get("file_ref") or "")
        out = await self._svc().delete(user_id, item_id)
        if out.item.is_folder:
            count = f"{out.file_count}+" if out.count_capped else str(out.file_count)
            await self._receipt(message, user_id, UIMessage.DRIVE_DELETED_FOLDER, path=out.item.path, count=count)
            return self._ok(message, f"Deleted folder {out.item.path} ({count} files).", [])
        await self._receipt(message, user_id, UIMessage.DRIVE_DELETED_FILE, path=out.item.path)
        return self._ok(message, f"Deleted {out.item.path}.", [])

    async def _receipt(self, message: AgentMessage, user_id: str, ui: UIMessage, **fmt: str) -> None:
        """Deterministic chat receipt of a destructive action (§4.10). Best effort, logged on failure."""
        if not (self._notification and self._localization):
            logger.warning("FileManagementAgent: drive receipt not wired (%s)", ui.value)
            return
        try:
            lang = await self._language.resolve_ui_language(user_id) if self._language else LanguageCode.EN
        except Exception:
            logger.warning("FileManagementAgent: UI language lookup failed for %s", user_id[:8], exc_info=True)
            lang = LanguageCode.EN
        try:
            text = self._localization.get_ui_string(lang, ui).format(**fmt)
            await self._notification.notify_raw(
                user_id, message.context.get("account_id") or "", text,
                channel_id_override=message.context.get("origin_channel_id"),
                platform_override=message.context.get("origin_platform"),
            )
        except Exception:
            logger.warning("FileManagementAgent: drive receipt failed (%s)", ui.value, exc_info=True)
