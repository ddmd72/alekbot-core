"""
FactProfilerRunner — the fact profiler as a FactProfilerPort (docs/10_rfcs/FACT_PROFILE_RFC.md §4.1).

Public interface:
    profiler_model(raw) -> str      the pinned model: the stripped `FACT_PROFILER_MODEL` value, else the default
    FactProfilerRunner(context_builder, prompt_builder, model, quota_service=None, prompt_content_store=None)
        async profile(account_id, text, metadata) -> FactProfilerReply   never raises

The agent is built once, lazily, with a default user configuration (the profiler is the system's, not a user's:
no tier, provider or agent override of any user applies) and the pinned model. The email classifier pattern:
services/ cannot import agents/, so composition builds the agent. The knob is read where the container is built,
never in `agents/` (REQ-ARCH-17) and never in `load_settings()` (an optional knob).
"""
import asyncio
from typing import Any, Mapping, Optional

from ..agents.fact_profiler_agent import AGENT_TYPE, FactProfilerAgent
from ..domain.agent import AgentConfig
from ..domain.fact_profiler_reply import FactProfilerReply
from ..domain.user import UserBotConfig
from ..infrastructure.agent_config import FACT_PROFILER
from ..ports.fact_profiler_port import FactProfilerPort
from ..ports.prompt_builder_port import PromptBuilderPort
from ..ports.prompt_content_store import PromptContentStore
from ..ports.quota_service import QuotaService
from ..utils.logger import logger


def profiler_model(raw: Optional[str]) -> str:
    """The model the profiler runs on: `raw` (the `FACT_PROFILER_MODEL` value) stripped, else the default."""
    value = (raw or "").strip()
    return value or FACT_PROFILER.default_model


class FactProfilerRunner(FactProfilerPort):

    def __init__(
        self,
        context_builder: Any,
        prompt_builder: PromptBuilderPort,
        model: str,
        quota_service: Optional[QuotaService] = None,
        prompt_content_store: Optional[PromptContentStore] = None,
    ) -> None:
        self._context_builder = context_builder
        self._prompt_builder = prompt_builder
        self._model = model
        self._quota_service = quota_service
        self._prompt_content_store = prompt_content_store
        self._agent: Optional[FactProfilerAgent] = None
        self._lock = asyncio.Lock()

    async def profile(self, account_id: str, text: str, metadata: Optional[Mapping[str, Any]]) -> FactProfilerReply:
        agent = await self._get_agent()
        if agent is None:
            return FactProfilerReply.unavailable(self._model)
        return await agent.profile(account_id, text, metadata)

    async def _get_agent(self) -> Optional[FactProfilerAgent]:
        async with self._lock:
            if self._agent is None:
                try:
                    self._agent = self._build()
                except Exception as exc:
                    logger.error(f"[FactProfile] cannot build the profiler: {type(exc).__name__}: {exc}")
                    return None
            return self._agent

    def _build(self) -> FactProfilerAgent:
        context = self._context_builder.build(AGENT_TYPE, UserBotConfig())
        context = context.model_copy(update={"model_name": self._model})
        agent = FactProfilerAgent(
            config=AgentConfig(agent_id=AGENT_TYPE, agent_type=AGENT_TYPE),
            execution_context=context,
            prompt_builder=self._prompt_builder,
        )
        # Mirror UserAgentFactory's post-construction wiring: without it _flush_billing and the content capture in
        # _call_llm are silent no-ops.
        agent._quota_service = self._quota_service
        agent._prompt_content_store = self._prompt_content_store
        return agent
