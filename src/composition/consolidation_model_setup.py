"""
Consolidation agent model pin (owner ruling 2026-11-20, decisions/sonnet55_adaptive_thinking_stall.md).

Public interface:
    consolidation_context(context_builder, config) -> AgentExecutionContext   the context ConsolidationAgent runs with

The agent's tier stays PERFORMANCE, but on Claude that tier resolves to the model the adapter (or the global
`CLAUDE_PERFORMANCE_MODEL` env var, which a deploy resets) names. Consolidation must not move with it, so on Claude
the context's model is replaced by `ConsolidationAgentConfig.claude_model`. Another provider keeps its own tier model
(a Claude id sent there would be refused), and a user's `model_overrides["consolidation"]` still wins.
"""
from typing import Any

from ..domain.user import UserBotConfig
from ..infrastructure.agent_config import CONSOLIDATION
from ..ports.llm_port import AgentExecutionContext

AGENT_TYPE = "consolidation"
PINNED_PROVIDER = "claude"


def consolidation_context(context_builder: Any, config: UserBotConfig) -> AgentExecutionContext:
    context = context_builder.build(AGENT_TYPE, config)
    if context.provider_name != PINNED_PROVIDER or config.get_model_override(AGENT_TYPE):
        return context
    return context.model_copy(update={"model_name": CONSOLIDATION.claude_model})
