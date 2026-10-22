"""STUB (part A) — the drive specialist (DRIVE_SPECIALIST_RFC §5). Part A owns the real class; part B wires only the
constructor below and reads only `AgentResponse.metadata["drive_status"]`, `history_context["drive_context"]`
and `metadata["file_data"]`."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from ..domain.retry_policy import NO_RETRY_POLICY
from .base_agent import BaseAgent

if TYPE_CHECKING:
    from ..infrastructure.drive_operations import DriveOperations
    from ..ports.llm_port import AgentExecutionContext
    from ..services.drive_search_service import DriveSearchService
    from ..services.prompt_builder import UserPromptBuilder


class DriveAgent(BaseAgent):
    """STUB (part A)."""

    RETRY_POLICY = NO_RETRY_POLICY

    def __init__(self, config: AgentConfig, *, execution_context: "AgentExecutionContext",
                 prompt_builder: "UserPromptBuilder", operations: "DriveOperations",
                 drive_search: Optional["DriveSearchService"], user_timezone: str, indexing_enabled: bool,
                 thinking_effort: Optional[str]) -> None:
        super().__init__(config)
        self._set_execution_context(execution_context)
        self._user_timezone = user_timezone
        self.prompt_builder = prompt_builder
        self.operations = operations
        self.drive_search = drive_search
        self.user_timezone = user_timezone
        self.indexing_enabled = indexing_enabled
        self.thinking_effort = thinking_effort

    async def can_handle(self, message: AgentMessage) -> bool:
        return message.intent == AgentIntent.QUERY

    async def execute(self, message: AgentMessage) -> AgentResponse:
        return AgentResponse.success(task_id=message.task_id, agent_id=self.agent_id,
                                     result="Drive result: ok\nWhat to do: Answer from the results below.",
                                     metadata={"drive_status": "ok"})
