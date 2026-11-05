"""
File Management Agent
=====================

File storage operations via uniform intent delegation.

Zero-LLM agent: no LLM calls, direct port operations.

Intents:
  open_file / delete_file         — chat files, delivered documents, skill files (GCS);
                                    open_file also opens drive: refs (lenient read)

The user's drive is the drive specialist's (`work_with_drive_files`, DRIVE_SPECIALIST_RFC §4.1): its operations run
in `DriveOperations` (src/infrastructure/drive_operations.py, §5.5), one instance per user shared with the specialist.
This agent keeps the lenient `open_file` on a `drive:` ref through that instance and maps the typed outcome back to
today's AgentResponse, text for text; `delete_file` refuses a `drive:` ref (§4.4).
"""

import asyncio
import dataclasses
import time
from typing import TYPE_CHECKING, Any, Dict, Optional, Set

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentMessage, AgentResponse, AgentConfig, AgentIntent
from ..domain import drive_answer_texts as drive_texts
from ..domain.drive_history import DRIVE_CONTEXT_KEY, history_entries
from ..domain.drive_open_texts import ROUTE_ORCHESTRATOR
from ..domain.drive_outcome import DriveOutcome
from ..domain.skill import SKILL_REF_PREFIX
from ..domain.drive_ref import is_drive_ref
from ..domain.workspace import WORKSPACE_REF_PREFIX
from ..infrastructure.agent_manifest import Intent
from ..infrastructure.drive_operations import DriveOperations
from ..domain.document_limits import MAX_VIDEO_ATTACH_BYTES
from ..domain.document_kind import SNIFF_HEAD_BYTES, kind_of, sniff_kind
from ..domain.document_open import ACTION_SEND
from ..domain.document_read import INTERACTIVE_OPEN_S, OPEN_MARGIN_S
from ..domain.email_open_texts import active_kind_refusal_note
from ..domain.email_ref import escape_email_name
from ..domain.exceptions import FileNotResolved
from ..domain.file_ref import FileRefKind, classify_ref
from ..domain.user_drive import DriveAccessRefused, DriveRefRefused, drive_filename
from ..ports.file_storage_port import FileStoragePort
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..ports.language_service_port import LanguageServicePort
    from ..services.localization_service import LocalizationService
    from ..services.drive_search_service import DriveSearchService
    from ..services.user_drive_service import UserDriveService
    from ..services.file_conversion_service import FileConversionService
    from ..services.user_notification_service import UserNotificationService


# What a sent file's upload title names first (the store it came from).
_STORE_TITLES = {
    FileRefKind.EMAIL: "Email attachment", FileRefKind.DRIVE: "Drive file", FileRefKind.WORKSPACE: "Workspace file",
    FileRefKind.SKILL: "Skill file", FileRefKind.DELIVERED: "Delivered document", FileRefKind.CHAT: "Chat file",
}


class FileManagementAgent(BaseAgent):
    """
    File storage operations for the orchestrator.

    Zero LLM calls — delegates to DriveOperations and FileStoragePort.
    """

    MAX_VIDEO_ATTACH_BYTES = MAX_VIDEO_ATTACH_BYTES

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
        drive_operations: Optional[DriveOperations] = None,
    ) -> None:
        """`drive_operations` is the user's shared instance (the factory memoises one per user, so this agent and the
        drive specialist hold one mutation lock); without it the agent builds its own from the same services."""
        super().__init__(config)
        self._ops = drive_operations or DriveOperations(
            conversion_service=conversion_service, notification=notification, drive_service=drive_service,
            localization=localization, language_service=language_service, drive_search=drive_search,
        )
        self._storage = storage
        self._conversion = conversion_service
        self._notification = notification

    # Read-only views of the shared operations' state, still read by the shared-instance contract test.
    @property
    def _shielded(self) -> Set["asyncio.Task[Any]"]:
        return self._ops._shielded

    @property
    def _armed(self) -> Dict["asyncio.Task[Any]", Any]:
        return self._ops._armed

    @property
    def drive_operations(self) -> DriveOperations:
        return self._ops

    async def can_handle(self, message: AgentMessage) -> bool:
        return message.intent == AgentIntent.QUERY

    async def execute(self, message: AgentMessage) -> AgentResponse:
        intent = message.payload.get("intent")
        # context_schemas params are spread directly into payload by coordinator
        payload = message.payload
        context = message.context
        user_id = context.get("user_id", "")
        ops = self._ops

        if intent == Intent.OPEN_FILE:
            ref = payload.get("file_ref") or ""
            # The open keeps this agent's timeout minus the margin for presentation (EXTERNAL_FILES_RFC §4.4); the
            # open is not shielded (USER_DRIVE_RFC §4.13).
            deadline_at = time.time() + self._open_budget_s()
            if is_drive_ref(ref):
                return self._respond(message, await ops.open(context, user_id, ref, deadline_at=deadline_at))
            return self._respond(message, await ops.fetch_stored(context, user_id, payload.get("file_ref"),
                                                                 deadline_at=deadline_at))

        if intent == Intent.DELETE_FILE:
            ref = payload.get("file_ref") or ""
            if is_drive_ref(ref):
                return self._fail(message, f"'{ref}' is on the user's drive; delete it through work_with_drive_files.")
            return await self._delete(message, payload, user_id)

        if intent == Intent.SEND_FILE_TO_USER:
            return await self._send(message, payload, context, user_id)

        logger.warning(
            "FileManagementAgent: unknown intent '%s'", intent,
        )
        return AgentResponse.failure(
            task_id=message.task_id,
            agent_id=self.agent_id,
            error=(
                f"File agent does not support intent '{intent}'. "
                f"Supported intents: open_file, delete_file, send_file_to_user."
            ),
        )

    def _open_budget_s(self) -> float:
        """Seconds an open may take: the agent timeout minus the presentation margin (no timeout configured: the
        interactive default)."""
        timeout_ms = self.config.timeout_ms
        return INTERACTIVE_OPEN_S if not timeout_ms else timeout_ms / 1000 - OPEN_MARGIN_S

    def _respond(self, message: AgentMessage, outcome: DriveOutcome) -> AgentResponse:
        """Today's AgentResponse for an outcome: success with the text, the touched items' `drive_context` and the
        vision file; failure with the text as the error."""
        if outcome.facts.too_long:
            # A refused open is an answer, not a failure: a failure gets the engine's "correct your input and try
            # again" suffix and invites the same open again (RFC §29.6 L13).
            self._on_agent_success(char_count=len(outcome.text), output_text=outcome.text[:200])
            entries = history_entries(drive_texts.KIND_OPEN, outcome)
            return AgentResponse.success(
                task_id=message.task_id, agent_id=self.agent_id, result=outcome.text + "\n" + ROUTE_ORCHESTRATOR,
                confidence=1.0, history_context={DRIVE_CONTEXT_KEY: entries} if entries else None)
        if not outcome.success:
            return self._fail(message, outcome.text)
        self._on_agent_success(char_count=len(outcome.text), output_text=outcome.text[:200])
        # Only open_file answers through here (a drive ref's delete is refused above), so the entries name an open.
        entries = history_entries(drive_texts.KIND_OPEN, outcome)
        return AgentResponse.success(
            task_id=message.task_id, agent_id=self.agent_id, result=outcome.text, confidence=1.0,
            history_context={DRIVE_CONTEXT_KEY: entries} if entries else None,
            metadata={"file_data": outcome.file_data} if outcome.file_data else None,
        )

    def _fail(self, message: AgentMessage, error: str) -> AgentResponse:
        return AgentResponse.failure(task_id=message.task_id, agent_id=self.agent_id, error=error)

    def _answer(self, message: AgentMessage, text: str) -> AgentResponse:
        """A successful answer: what the model reads next. A refusal travels this way, never as a failure (a failure
        is wrapped as "correct your input" and the model repeats the call)."""
        self._on_agent_success(char_count=len(text), output_text=text[:200])
        return AgentResponse.success(task_id=message.task_id, agent_id=self.agent_id, result=text, confidence=1.0)

    async def _send(self, message: AgentMessage, payload: dict, context: dict, user_id: str) -> AgentResponse:
        """`send_file_to_user` (EXTERNAL_FILES_RFC §4.2, D8, D13): any ref's bytes through the core's resolver, an
        email attachment through D13's active-kind check, then one upload to the origin channel. A refusal is an
        answer; only an upload that did not happen is a failure."""
        ref = payload.get("file_ref") or ""
        if not ref:
            return self._fail(message, 'file_ref is required for send_file_to_user. Pass context={"file_ref": "<ref>"}.')
        self._on_agent_start(f"send_file_to_user: {ref}")
        deadline_at = time.time() + self._open_budget_s()
        try:
            resolved = await self._conversion.resolve_file(ref, user_id, action=ACTION_SEND)
        except (FileNotResolved, DriveRefRefused, DriveAccessRefused) as e:
            return self._answer(message, e.line)
        except (FileNotFoundError, PermissionError) as e:
            logger.warning("FileManagementAgent: send_file_to_user could not read '%s': %s", ref, type(e).__name__)
            return self._fail(message, f"File '{ref}' was not found (or is not the user's); nothing was sent.")
        name = drive_filename(payload.get("file_name"), resolved.name)
        is_email = classify_ref(ref) is FileRefKind.EMAIL
        if is_email:
            candidates = [resolved]
            if name != resolved.name:
                candidates.append(dataclasses.replace(resolved, name=name))
            for candidate in candidates:
                active = await self._conversion.active_kind_of(candidate, deadline_at=deadline_at)
                if active is not None:
                    logger.info("FileManagementAgent: send_file_to_user refused an email attachment (kind=%s)", active)
                    return self._answer(message, active_kind_refusal_note(active))
        kind = sniff_kind(resolved.data[:SNIFF_HEAD_BYTES], kind_of(resolved.name, resolved.mime, False))
        store = _STORE_TITLES.get(classify_ref(ref), "File")
        if self._notification is None:
            return self._fail(message, "File delivery is not configured; the file was not sent.")
        sent = await self._notification.notify_file_bytes(
            user_id=user_id, account_id=context.get("account_id") or "", file_bytes=resolved.data, filename=name,
            title=f"{store} ({kind})", channel_id_override=context.get("origin_channel_id"),
            platform_override=context.get("origin_platform"),
        )
        if not sent:
            return self._fail(message, f"'{name}' could not be delivered to the user's chat; nothing was sent.")
        shown = escape_email_name(name) if is_email else name
        return self._answer(message, f"Sent '{shown}' ({kind}) to the user's chat as a file.")

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
