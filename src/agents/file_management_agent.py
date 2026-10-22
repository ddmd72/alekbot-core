"""
File Management Agent
=====================

File storage operations via uniform intent delegation.

Zero-LLM agent: no LLM calls, direct port operations.

Intents:
  open_file / delete_file         — chat files, delivered documents, skill files (GCS);
                                    open_file also opens drive: refs (lenient read)
  save_file_to_drive              — chat file / delivered document → the user's drive
  list_files_in_drive, open_file_from_drive
  move_file_in_drive, create_folder_in_drive
  update_file_in_drive (append / replace-by-file), delete_file_from_drive
  search_files_in_drive           — the drive index (USER_DRIVE_INDEXING_RFC §10)

The drive work itself runs in `DriveOperations` (src/infrastructure/drive_operations.py, DRIVE_SPECIALIST_RFC §5.5),
one instance per user shared with the drive specialist; this agent keeps its intent surface and maps each typed
outcome back to today's AgentResponse, text for text. Mutations wait `MUTATION_WAIT_S` inside the agent's own 120 s
timeout, run shielded from cancellation, and delete and replace post a receipt via notify_raw (USER_DRIVE_RFC §4.4,
§4.10, §4.13).
"""

import asyncio
from typing import TYPE_CHECKING, Any, Dict, Optional, Set

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentMessage, AgentResponse, AgentConfig, AgentIntent
from ..domain.drive_outcome import DriveOutcome
from ..domain.skill import SKILL_REF_PREFIX
from ..domain.user_drive import is_drive_ref, parse_drive_ref
from ..domain.workspace import WORKSPACE_REF_PREFIX
from ..infrastructure.agent_manifest import Intent
from ..infrastructure.drive_operations import (
    GONE, MAX_VIDEO_ATTACH_BYTES, NAME_TAKEN, NOT_CONFIGURED, NOT_CONNECTED, STILL_RUNNING, DriveOperations,
    wrong_store_text,
)
from ..ports.file_storage_port import FileStoragePort
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..ports.language_service_port import LanguageServicePort
    from ..services.localization_service import LocalizationService
    from ..services.drive_search_service import DriveSearchService
    from ..services.user_drive_service import UserDriveService
    from ..services.file_conversion_service import FileConversionService
    from ..services.user_notification_service import UserNotificationService


def _flag(value: Any) -> bool:
    """A boolean the delegate tool sent as a string (`"true"`, `"yes"`, `"1"`, any case), or as a boolean."""
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in ("true", "yes", "1")


class FileManagementAgent(BaseAgent):
    """
    File storage operations for the orchestrator.

    Zero LLM calls — delegates to DriveOperations and FileStoragePort.
    """

    MAX_VIDEO_ATTACH_BYTES = MAX_VIDEO_ATTACH_BYTES
    _NOT_CONNECTED = NOT_CONNECTED
    _GONE = GONE
    _NAME_TAKEN = NAME_TAKEN
    # How long the agent waits for a mutation — inside its own 120 s timeout, so BaseAgent's
    # timeout (a false "failed" + a circuit-breaker failure) never fires for one (§4.13).
    MUTATION_WAIT_S = 100.0
    STILL_RUNNING = STILL_RUNNING

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

    # Read-only views of the shared operations' state (tests and the late-failure contract read them).
    @property
    def _shielded(self) -> Set["asyncio.Task[Any]"]:
        return self._ops._shielded

    @property
    def _notices(self) -> Set["asyncio.Task[Any]"]:
        return self._ops._notices

    @property
    def _armed(self) -> Dict["asyncio.Task[Any]", Any]:
        return self._ops._armed

    @property
    def _targets(self) -> Dict["asyncio.Task[Any]", str]:
        return self._ops._targets

    @property
    def _localization(self) -> Optional["LocalizationService"]:
        return self._ops._localization

    @property
    def _conversion_service(self) -> "FileConversionService":
        return self._ops._conversion_service

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
        wait = self.MUTATION_WAIT_S

        if intent == Intent.OPEN_FILE:
            ref = payload.get("file_ref") or ""
            if is_drive_ref(ref):
                return self._respond(message, await ops.open(context, user_id, ref))
            return self._respond(message, await ops.fetch_stored(context, user_id, payload.get("file_ref")))

        if intent == Intent.DELETE_FILE:
            ref = payload.get("file_ref") or ""
            if is_drive_ref(ref):
                return self._fail(message, f"'{ref}' is on the user's drive; use delete_file_from_drive.")
            return await self._delete(message, payload, user_id)

        if intent == Intent.OPEN_FILE_FROM_DRIVE:
            return self._respond(message, await ops.open(context, user_id, payload.get("file_ref") or "",
                                                         original=payload.get("original") is True))
        if intent == Intent.LIST_FILES_IN_DRIVE:
            return self._respond(message, await ops.list_folder(context, user_id, payload.get("folder")))
        if intent == Intent.SEARCH_FILES_IN_DRIVE:
            if ops.configured and _flag(payload.get("drive_deleted")):
                return self._respond(message, await ops.search_deleted(context, user_id, payload.get("drive_query")))
            return self._respond(message, await ops.search(
                context, user_id, query=payload.get("drive_query"), folder=payload.get("drive_folder"),
                kind=payload.get("drive_kind"), date_from=payload.get("drive_date_from"),
                date_to=payload.get("drive_date_to")))
        if intent == Intent.SAVE_FILE_TO_DRIVE:
            return await self._save_to_drive(message, payload, user_id)
        if intent == Intent.MOVE_FILE_IN_DRIVE:
            return self._respond(message, await ops.move(context, user_id, payload.get("file_ref") or "",
                                                         folder=payload.get("folder"),
                                                         new_name=payload.get("new_name"), wait_s=wait))
        if intent == Intent.CREATE_FOLDER_IN_DRIVE:
            return self._respond(message, await ops.create_folder(context, user_id, payload.get("folder"),
                                                                  wait_s=wait))
        if intent == Intent.UPDATE_FILE_IN_DRIVE:
            return await self._update_in_drive(message, payload, user_id)
        if intent == Intent.DELETE_FILE_FROM_DRIVE:
            return self._respond(message, await ops.delete(context, user_id, payload.get("file_ref") or "",
                                                           wait_s=wait))

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

    async def _save_to_drive(self, message: AgentMessage, payload: dict, user_id: str) -> AgentResponse:
        """save_file_to_drive takes `file_ref` (a copy) XOR `text` + `name` (a new text file); the shape checks run
        where today's did, after the drive-configured check and before any provider call."""
        if not self._ops.configured:
            return self._fail(message, NOT_CONFIGURED)
        ref = payload.get("file_ref") or ""
        text = payload.get("text")
        has_text = isinstance(text, str) and text != ""
        if text == "" and not ref:
            return self._fail(message, '"text" is empty: nothing to write. Give the full content of the file.')
        if bool(ref) == has_text:
            return self._fail(message, 'Give exactly one of "file_ref" (a file to keep) or "text" + "name" '
                                       '(a new text file).')
        context = message.context
        if has_text:
            return self._respond(message, await self._ops.save_text(
                context, user_id, text, file_name=payload.get("name"), folder=payload.get("folder"),
                wait_s=self.MUTATION_WAIT_S))
        return self._respond(message, await self._ops.copy(
            context, user_id, ref, folder=payload.get("folder"), file_name=payload.get("name"),
            wait_s=self.MUTATION_WAIT_S))

    async def _update_in_drive(self, message: AgentMessage, payload: dict, user_id: str) -> AgentResponse:
        """update_file_in_drive takes `append_text` XOR `source_ref` (USER_DRIVE_RFC §4.8)."""
        if not self._ops.configured:
            return self._fail(message, NOT_CONFIGURED)
        ref = payload.get("file_ref") or ""
        if parse_drive_ref(ref) is None:
            return self._fail(message, wrong_store_text(ref))
        append_text, source_ref = payload.get("append_text"), payload.get("source_ref")
        if bool(append_text) == bool(source_ref):
            return self._fail(message, 'Give exactly one of "append_text" or "source_ref".')
        if append_text:
            return self._respond(message, await self._ops.append(message.context, user_id, ref, append_text,
                                                                 wait_s=self.MUTATION_WAIT_S))
        return self._respond(message, await self._ops.replace(message.context, user_id, ref, source_ref,
                                                              wait_s=self.MUTATION_WAIT_S))

    def _respond(self, message: AgentMessage, outcome: DriveOutcome) -> AgentResponse:
        """Today's AgentResponse for an outcome: success with the text, the touched items' `drive_context` and the
        vision file; failure with the text as the error."""
        if not outcome.success:
            return self._fail(message, outcome.text)
        self._on_agent_success(char_count=len(outcome.text), output_text=outcome.text[:200])
        entries = outcome.drive_context()
        return AgentResponse.success(
            task_id=message.task_id, agent_id=self.agent_id, result=outcome.text, confidence=1.0,
            history_context={"drive_context": entries} if entries else None,
            metadata={"file_data": outcome.file_data} if outcome.file_data else None,
        )

    def _fail(self, message: AgentMessage, error: str) -> AgentResponse:
        return AgentResponse.failure(task_id=message.task_id, agent_id=self.agent_id, error=error)

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
