"""
DeepResearchAgent
=================

Thin specialist that kicks off a background research job and returns an ACK
immediately — the actual 5–60 minute research runs in the background.

Delivery-agnostic: the agent calls port.create_interaction() and returns ACK.
The adapter owns delivery:
  GeminiDeepResearchAdapter → enqueues Cloud Task (polling loop)
  OpenAIDeepResearchAdapter → creates response with webhook_url (push delivery)

Orchestration responsibilities:
  1. Receive {query, language} from Smart Agent after preparation protocol.
  2. Assemble system prompt via PromptBuilder (if configured).
  3. Append language instruction to query.
  4. Call DeepResearchPort.create_interaction() — adapter handles delivery setup.
  5. Return ACK to Smart Agent.

Result delivery: adapter-owned (polling or webhook). WorkerHandler / webhook handler
generates HTML report, uploads to GCS, delivers URL via UserNotificationService.
"""

from typing import Optional

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentMessage, AgentResponse, AgentConfig, AgentIntent
from ..domain.user import PerformanceTier
from ..ports.deep_research_port import DeepResearchPort
from ..ports.prompt_builder_port import PromptBuilderPort
from ..infrastructure.agent_config import DEEP_RESEARCH
from ..utils.logger import logger


class DeepResearchAgent(BaseAgent):
    """
    Submits a background research job via DeepResearchPort and returns ACK.

    Execution mode: SYNC — returns after kicking off the async operation (~seconds).
    Does NOT use LLMPort / AgentExecutionContext: the Deep Research API is outside
    the standard LLM provider interface and is accessed directly via DeepResearchPort.
    """

    TIMEOUT_MS  = DEEP_RESEARCH.timeout_ms
    MAX_RETRIES = DEEP_RESEARCH.max_retries

    def __init__(
        self,
        config: AgentConfig,
        job_port: DeepResearchPort,
        tier: PerformanceTier = PerformanceTier.BALANCED,
        prompt_builder: Optional[PromptBuilderPort] = None,
        user_id: Optional[str] = None,
    ) -> None:
        """
        Args:
            config:         Standard agent config.
            job_port:       DeepResearchPort implementation for the selected provider.
                            The adapter owns delivery — no task_queue or provider_name needed here.
            tier:           Performance tier resolved from user config.
                            Passed to the adapter; adapter maps it internally to a provider model.
            prompt_builder: Optional PromptBuilder for assembling a system prompt.
                            Adapters that do not use a system prompt will ignore it.
            user_id:        User identifier for PromptBuilder profile resolution.
        """
        super().__init__(config)
        self._job_port = job_port
        self._tier = tier
        self._prompt_builder = prompt_builder
        self._user_id = user_id

    async def can_handle(self, message: AgentMessage) -> bool:
        return (
            message.intent == AgentIntent.QUERY
            and bool(message.payload.get("query"))
        )

    async def execute(self, message: AgentMessage) -> AgentResponse:
        query      = message.payload.get("query", "")
        brief      = message.payload.get("brief", query)  # short summary for metadata; falls back to query
        language   = message.payload.get("language", "English")
        user_id    = message.context.get("user_id", "")
        account_id = message.context.get("account_id", "")

        self._on_agent_start(query)

        system_prompt = ""
        if self._prompt_builder:
            try:
                system_prompt = await self._prompt_builder.build_for_agent(
                    "deep_research", self._user_id
                )
            except Exception as e:
                logger.warning("[DeepResearchAgent] PromptBuilder failed, proceeding without system prompt: %s", e)

        full_query = (
            f"{query}"
        )
        logger.info(
            "[DeepResearch] Submitting job (%d chars): %s...",
            len(full_query), full_query[:80],
        )

        try:
            job_id = await self._job_port.create_interaction(
                query=full_query,
                user_id=user_id,
                account_id=account_id,
                original_query=brief[:512],
                tier=self._tier,
                system_prompt=system_prompt or None,
                session_id=message.context.get("session_id"),
            )
        except Exception as exc:
            self._on_agent_error(exc, "create_interaction")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Failed to start deep research: {exc}",
            )

        self._on_delegation("deep_research_api", job_id)
        self._on_agent_success(char_count=len(query), token_count=0)

        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result={"status": "started", "interaction_id": job_id},
        )
