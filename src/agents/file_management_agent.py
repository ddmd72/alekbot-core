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
from typing import TYPE_CHECKING, Any, Dict, Optional, Set

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentMessage, AgentResponse, AgentConfig, AgentIntent
from ..domain import drive_answer_texts as drive_texts
from ..domain.drive_history import DRIVE_CONTEXT_KEY, history_entries
from ..domain.drive_outcome import DriveOutcome
from ..domain.skill import SKILL_REF_PREFIX
from ..domain.user_drive import is_drive_ref
from ..domain.workspace import WORKSPACE_REF_PREFIX
from ..infrastructure.agent_manifest import Intent
from ..infrastructure.drive_operations import MAX_VIDEO_ATTACH_BYTES, DriveOperations
from ..ports.file_storage_port import FileStoragePort
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..ports.language_service_port import LanguageServicePort
    from ..services.localization_service import LocalizationService
    from ..services.drive_search_service import DriveSearchService
    from ..services.user_drive_service import UserDriveService
    from ..services.file_conversion_service import FileConversionService
    from ..services.user_notification_service import UserNotificationService


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
            if is_drive_ref(ref):
                return self._respond(message, await ops.open(context, user_id, ref))
            return self._respond(message, await ops.fetch_stored(context, user_id, payload.get("file_ref")))

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

    def _respond(self, message: AgentMessage, outcome: DriveOutcome) -> AgentResponse:
        """Today's AgentResponse for an outcome: success with the text, the touched items' `drive_context` and the
        vision file; failure with the text as the error."""
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
