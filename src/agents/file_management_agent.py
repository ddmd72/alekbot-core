"""
File Management Agent
=====================

File storage operations via uniform intent delegation.

Zero-LLM agent: no LLM calls, direct port operations.
Evolution path: add LLM for search/list/metadata queries (Phase 2).

Intents:
  open_file          — download from GCS + convert to text
  delete_file        — remove from GCS
"""

import mimetypes
import os
import tempfile
from typing import TYPE_CHECKING

import aiofiles

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentMessage, AgentResponse, AgentConfig, AgentIntent
from ..infrastructure.agent_manifest import Intent
from ..ports.file_storage_port import FileStoragePort
from ..utils.file_conversion import is_native_binary
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..services.file_conversion_service import FileConversionService


class FileManagementAgent(BaseAgent):
    """
    File storage operations for the orchestrator.

    Zero LLM calls — delegates to FileConversionService and FileStoragePort.
    """

    def __init__(
        self,
        config: AgentConfig,
        conversion_service: "FileConversionService",
        storage: FileStoragePort,
    ) -> None:
        super().__init__(config)
        self._conversion_service = conversion_service
        self._storage = storage

    async def can_handle(self, message: AgentMessage) -> bool:
        return message.intent == AgentIntent.QUERY

    async def execute(self, message: AgentMessage) -> AgentResponse:
        intent = message.payload.get("intent")
        # context_schemas params are spread directly into payload by coordinator
        payload = message.payload
        user_id = message.context.get("user_id", "")

        if intent == Intent.OPEN_FILE:
            return await self._fetch(message, payload, user_id)

        if intent == Intent.DELETE_FILE:
            return await self._delete(message, payload, user_id)

        logger.warning(
            "FileManagementAgent: unknown intent '%s'", intent,
        )
        return AgentResponse.failure(
            task_id=message.task_id,
            agent_id=self.agent_id,
            error=(
                f"File agent does not support intent '{intent}'. "
                f"Supported intents: open_file (retrieve file text), "
                f"delete_file (remove file from storage)."
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

        mime_type, _ = mimetypes.guess_type(ref)
        mime_type = mime_type or "application/octet-stream"

        try:
            if is_native_binary(mime_type):
                return await self._fetch_binary(message, ref, user_id, mime_type)
            else:
                return await self._fetch_text(message, ref, user_id)
        except FileNotFoundError:
            logger.warning("FileManagementAgent: file not found '%s'", ref)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=(
                    f"File '{ref}' not found in storage. "
                    f"It may have been deleted or expired (files are kept for 90 days). "
                    f"Ask the user to re-upload the file."
                ),
            )
        except Exception as e:
            logger.error("FileManagementAgent: fetch failed '%s': %s", ref, e, exc_info=True)
            self._on_agent_error(e, f"fetch {ref}")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=(
                    f"Could not read file '{ref}': {type(e).__name__}. "
                    f"The file exists but could not be converted to text. "
                    f"Ask the user to re-upload or paste the content directly."
                ),
            )

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
