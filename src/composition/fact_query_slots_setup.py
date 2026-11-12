"""
Memory-search slots agent wiring (fact profile RFC §5.1).

Public interface:
    search_slots_model(raw) -> str          the pinned model: the stripped `FACT_QUERY_SLOTS_MODEL` value, else the default
    build_search_slots_agent(...)           the FactQuerySlotsAgent the memory-search path runs

The memory-search slots call has its own model and thinking effort, apart from the router's slots call (which keeps the
router's context and no thinking). The context is built for the Claude-only `fact_query_slots` strategy entry, then its
model is replaced by the pinned one, as the fact profiler does. The knob is optional: read where the factory builds the
agent, never in `agents/` and never in `load_settings()`.
"""
from typing import Any, Optional

from ..agents.fact_query_slots_agent import FactQuerySlotsAgent
from ..domain.agent import AgentConfig
from ..domain.user import UserBotConfig
from ..infrastructure.agent_config import FACT_QUERY_SLOTS
from ..ports.prompt_builder_port import PromptBuilderPort

AGENT_TYPE = "fact_query_slots"


def search_slots_model(raw: Optional[str]) -> str:
    """The model the memory-search slots call runs on: `raw` (the `FACT_QUERY_SLOTS_MODEL` value) stripped, else the default."""
    value = (raw or "").strip()
    return value or FACT_QUERY_SLOTS.search_model


def build_search_slots_agent(
    *, context_builder: Any, user_config: UserBotConfig, prompt_builder: PromptBuilderPort, user_id: str, model: str,
) -> FactQuerySlotsAgent:
    context = context_builder.build(AGENT_TYPE, user_config).model_copy(update={"model_name": model})
    return FactQuerySlotsAgent(
        AgentConfig(agent_id=f"fact_query_slots_{user_id}", agent_type=AGENT_TYPE),
        execution_context=context,
        prompt_builder=prompt_builder,
        user_timezone=user_config.timezone,
        thinking=FACT_QUERY_SLOTS.search_thinking,
    )
