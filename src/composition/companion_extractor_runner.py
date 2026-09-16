"""
CompanionExtractorRunner — composition-layer implementation of
CompanionExtractorPort. Constructs the per-companion-type extractor agent
fresh per call and runs it directly (agent.process(), no coordinator
involved — see Task 3's docstring for why construction is on-demand, not
eager/registered). composition/ is the one layer allowed to cross the
service/agent boundary (root CLAUDE.md Layer Semantics).

One companion type today ("tutor"). _EXTRACTORS maps companion_type ->
(agent_type string, agent class); extend both together when a second
companion type ships its own extractor (RFC §6).
"""
from typing import Any, Dict, List, Optional

from ..agents.tutor_extractor_agent import TutorExtractorAgent
from ..domain.agent import AgentIntent, AgentConfig, AgentMessage, AgentStatus
from ..domain.companion_extraction import EXTRACTION_TASK
from ..infrastructure.agent_config import TUTOR_EXTRACTOR
from ..ports.companion_extractor_port import CompanionExtractorPort
from ..ports.prompt_builder_port import PromptBuilderPort

_EXTRACTORS = {
    "tutor": ("tutor_extractor", TutorExtractorAgent),
}


class CompanionExtractorRunner(CompanionExtractorPort):

    def __init__(
        self,
        context_builder: Any,
        user_repo: Any,
        prompt_builder: PromptBuilderPort,
        quota_service: Optional[Any] = None,
        prompt_content_store: Optional[Any] = None,
    ) -> None:
        self._context_builder = context_builder
        self._user_repo = user_repo
        self._prompt_builder = prompt_builder
        # Mirror the two post-construction attributes UserAgentFactory sets on every
        # agent it builds (see user_agent_factory.py::_create_and_cache_agents). This
        # runner bypasses the factory by design (constructs the agent fresh per batch,
        # not per-user-cached) so nothing else performs that wiring — without it,
        # BaseAgent._flush_billing()/_call_llm() silently no-op (both default to None),
        # making companion extraction's LLM usage invisible to billing + BigQuery.
        self._quota_service = quota_service
        self._prompt_content_store = prompt_content_store

    async def extract(
        self,
        companion_type: str,
        account_id: str,
        created_by_user_id: str,
        messages: List[dict],
    ) -> Dict[str, Any]:
        entry = _EXTRACTORS.get(companion_type)
        if entry is None:
            raise ValueError(f"No extractor registered for companion_type={companion_type!r}")
        agent_type, agent_cls = entry

        user_profile = await self._user_repo.get_user(created_by_user_id)
        if user_profile is None:
            raise ValueError(f"created_by_user_id={created_by_user_id!r} not found")

        execution_context = self._context_builder.build(agent_type, user_profile.config)
        agent = agent_cls(
            config=AgentConfig(
                agent_id=f"{agent_type}_{created_by_user_id}",
                agent_type=agent_type,
                timeout_ms=TUTOR_EXTRACTOR.timeout_ms,
                capabilities=[agent_type],
            ),
            execution_context=execution_context,
            prompt_builder=self._prompt_builder,
        )
        # See __init__ comment — mirror UserAgentFactory's post-construction wiring
        # (BaseAgent defaults both to None) so this agent's LLM usage is billed and
        # captured to BigQuery like every other agent's.
        agent._quota_service = self._quota_service
        agent._prompt_content_store = self._prompt_content_store

        message = AgentMessage.create(
            sender="companion_extractor_runner",
            recipient=agent.agent_id,
            intent=AgentIntent.DELEGATE,
            payload={"task": EXTRACTION_TASK, "messages": messages},
            context={"account_id": account_id},
        )
        response = await agent.process(message)
        if response.status != AgentStatus.SUCCESS:
            raise RuntimeError(f"Extraction failed: {response.error}")
        return response.result or {}
