"""
File Management Agent
=====================

Chat files, delivered documents, skill files and workspace copies, via uniform intent delegation.

Zero-LLM agent: no LLM calls, direct port operations.

Intents:
  open_file / delete_file         — chat files, delivered documents, skill files (GCS), workspace copies;
                                    open_file also opens a drive: ref (lenient read) through the user's
                                    DriveOperations
The user's drive itself is the drive specialist's (`work_with_drive_files`, DRIVE_SPECIALIST_RFC): its operations live
in DriveOperations, one instance per user, shared with this agent. File text and search results reach the model only
inside the escaped, nonce-tagged wrapper (USER_DRIVE_INDEXING_RFC §10.4, D2).
"""

import asyncio
import mimetypes
import os
import secrets
import tempfile
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

import aiofiles

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentMessage, AgentResponse, AgentConfig, AgentIntent
from ..domain.drive_search import (
    REFERENCE_REMINDER, REFERENCE_REMINDER_ATTACHED, wrap_untrusted,
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
    DriveMirrorWriteRefused,
    DriveRootProtectedError,
    drive_context_entry,
    drive_label,
    format_size,
    is_drive_ref,
    parse_drive_ref,
)
from ..infrastructure.agent_manifest import Intent
from ..ports.file_storage_port import FileStoragePort
from ..utils.file_conversion import is_native_binary
from ..utils.logger import exc_brief, logger

if TYPE_CHECKING:
    from ..ports.language_service_port import LanguageServicePort
    from ..services.localization_service import LocalizationService
    from ..services.drive_search_service import DriveSearchService
    from ..services.user_drive_service import UserDriveService
    from ..services.file_conversion_service import FileConversionService
    from ..services.user_notification_service import UserNotificationService
    from ..infrastructure.drive_operations import DriveOperations


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
        drive_operations: Optional["DriveOperations"] = None,   # STUB (part A): the user's shared instance (§5.5)
    ) -> None:
        super().__init__(config)
        # STUB (part A): FileManagement runs its drive intents through DriveOperations; built here when not given,
        # so a test that constructs the agent directly keeps working.
        from ..infrastructure.drive_operations import DriveOperations
        self.drive_operations = drive_operations or DriveOperations(
            conversion_service=conversion_service, drive_service=drive_service, drive_search=drive_search,
            notification=notification, localization=localization, language_service=language_service)
        self._drive = drive_service
        self._drive_search = drive_search
        self._localization = localization
        self._language = language_service
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
                return await self._open_from_drive(message, payload, user_id)
            return await self._fetch(message, payload, user_id)

        if intent == Intent.DELETE_FILE:
            ref = payload.get("file_ref") or ""
            if is_drive_ref(ref):
                return self._fail(message, f"'{ref}' is on the user's drive; delete it through work_with_drive_files.")
            return await self._delete(message, payload, user_id)

        logger.warning(
            "FileManagementAgent: unknown intent '%s'", intent,
        )
        return AgentResponse.failure(
            task_id=message.task_id,
            agent_id=self.agent_id,
            error=(
                f"File agent does not support intent '{intent}'. "
                f"Supported intents: open_file, delete_file."
            ),
        )

    async def _open_from_drive(self, message: AgentMessage, payload: dict, user_id: str) -> AgentResponse:
        """open_file on a `drive:` ref: the lenient drive read (DRIVE_SPECIALIST_RFC §4.4). STUB (part A): part A
        runs it through the user's DriveOperations.open and maps the outcome back to today's AgentResponse."""
        return await self._drive_call(message, self._open_drive, payload, user_id)

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

    async def _drive_call(
        self, message: AgentMessage,
        handler: Callable[[AgentMessage, dict, str], Awaitable[AgentResponse]],
        payload: dict, user_id: str,
    ) -> AgentResponse:
        if self._drive is None:
            return self._fail(message, "The drive is not configured.")
        self._on_agent_start(f"{payload.get('intent')}: {payload.get('file_ref') or payload.get('folder') or ''}")
        try:
            return await handler(message, payload, user_id)
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
        except DriveMirrorWriteRefused:
            return self._fail(message, await self._mirror_refused_line(user_id))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("FileManagementAgent: drive op failed: %s", e, exc_info=True)
            self._on_agent_error(e, str(payload.get("intent")))
            return self._fail(message, f"Drive operation failed: {type(e).__name__}.")

    async def _mirror_refused_line(self, user_id: str) -> str:
        """The Step 1 guard's one line (§27.9.8), in the user's UI language (English when it cannot be resolved)."""
        lang = LanguageCode.EN
        if self._language is not None:
            try:
                lang = await self._language.resolve_ui_language(user_id)
            except Exception as exc:
                logger.warning("FileManagementAgent: UI language lookup failed for %s: %s", user_id[:8],
                               exc_brief(exc))
        if self._localization is None:
            # Mirrors src/locales/en.py DRIVE_MIRROR_WRITE_REFUSED verbatim (locales load only via the adapter).
            return ("This folder is managed by the bot and its files are overwritten; "
                    "to keep your own version, copy the file to another folder.")
        return self._localization.get_ui_string(lang, UIMessage.DRIVE_MIRROR_WRITE_REFUSED)


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

    async def _archived_transcript_of(self, user_id: str, ref: str, item: Any) -> Optional[str]:
        """A ref whose item is a file of the bot's `_Transcripts` folder opens from the pinned store entry, capped; with
        none, the downloaded file, capped the same way (§27.9.11). None for any other file, or when the lookup failed
        (the plain open then runs)."""
        if self._drive_search is None:
            return None
        item_id = parse_drive_ref(ref)
        if item_id is None:
            return None

        async def downloaded() -> str:
            return await self._conversion_service.resolve_content(ref, user_id)

        try:
            text = await self._drive_search.archived_text(user_id, item_id, label=drive_label(item), path=item.path,
                                                          file_text=downloaded)
        except DriveItemNotFoundError:
            raise
        except Exception as exc:
            logger.warning("FileManagementAgent: archived transcript lookup failed: %s", exc_brief(exc))
            return None
        return text or None

    async def _open_drive_item(self, message: AgentMessage, ref: str, user_id: str,
                               payload: Optional[dict] = None) -> AgentResponse:
        item = await self._conversion_service.get_drive_item(ref, user_id)
        if item.is_folder:
            return self._fail(message, f"'{item.path or '/'}' is a folder; list it instead.")
        archived = await self._archived_transcript_of(user_id, ref, item)
        if archived is not None:
            return self._ok(message, archived, [item])
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
