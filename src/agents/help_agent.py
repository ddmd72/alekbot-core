"""
Help Agent
==========

Returns a static description of system capabilities.
No LLM calls — pure static lookup.

Triggered by the get_help intent. The orchestrator (Quick/Smart) re-presents
the result in the user's language via LANG_MIRROR/LANG_FIXED.

To update the capabilities list, edit src/utils/capabilities.py.
"""

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentMessage, AgentResponse, AgentConfig, AgentIntent
from ..utils.capabilities import CAPABILITIES_TEXT


class HelpAgent(BaseAgent):
    """
    Thin agent that returns a static description of system capabilities.

    Zero LLM calls — zero tokens, minimal latency.
    Language handling delegated to the orchestrator.
    """

    def __init__(self, config: AgentConfig) -> None:
        super().__init__(config)

    async def can_handle(self, message: AgentMessage) -> bool:
        return message.intent == AgentIntent.QUERY

    async def execute(self, message: AgentMessage) -> AgentResponse:
        self._on_agent_start("get_help")
        self._on_agent_success(char_count=len(CAPABILITIES_TEXT), output_text=CAPABILITIES_TEXT)
        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=CAPABILITIES_TEXT,
            confidence=1.0,
        )
