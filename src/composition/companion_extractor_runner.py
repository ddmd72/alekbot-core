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
from typing import Any, Dict, List

from ..agents.tutor_extractor_agent import TutorExtractorAgent
from ..domain.agent import AgentIntent, AgentConfig, AgentMessage, AgentStatus
from ..domain.companion_extraction import EXTRACTION_TASK
from ..infrastructure.agent_config import TUTOR_EXTRACTOR
from ..ports.companion_extractor_port import CompanionExtractorPort

_EXTRACTORS = {
    "tutor": ("tutor_extractor", TutorExtractorAgent),
}


class CompanionExtractorRunner(CompanionExtractorPort):

    def __init__(self, context_builder: Any, user_repo: Any, prompt_builder: Any) -> None:
        self._context_builder = context_builder
        self._user_repo = user_repo
        self._prompt_builder = prompt_builder

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
