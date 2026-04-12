"""Default prompt cache strategy implementation.

Resolves prompt cache configuration based on agent type and provider capabilities.
Agents are completely unaware of this logic — they only declare their identity.

See: docs/10_rfcs/HEXAGONAL_PROMPT_CACHING_RFC.md
"""

from typing import Optional

from ..ports.prompt_cache_strategy_port import PromptCacheStrategyPort
from ..ports.llm_port import ProviderCapabilities, PromptCacheConfig
from ..utils.logger import logger


class PromptCacheStrategy(PromptCacheStrategyPort):
    """Default prompt cache strategy.

    Business rules:
    - Consolidation, Smart, Quick, WebSearch agents benefit from caching
      (static/semi-static system prompts, multi-turn reuse).
    - Router does not benefit
      (short prompt, single-shot).
    - Provider must support context_caching
      (Claude yes, Gemini/Grok no).

    Multi-turn loop caching (cache_last_message):
    - Consolidation: guaranteed 2-6 turn loop within 60-90s, well under
      ephemeral TTL — second breakpoint amortizes on every subsequent turn.
    - Smart: empirically always ≥2 turns because the agent is forced to
      call the deliver_response terminal tool, and any non-trivial query
      first delegates to a specialist (search_memory etc.). Cache write
      reliably read at least once.
    - Quick: most calls are single-turn (no terminal_tool, plain text
      response when no delegation needed). Cache write would not be
      amortized → pay the +25% surcharge for nothing. Skipped.
    - WebSearch: single LLM call with native grounding, no follow-up.
    """

    CACHEABLE_AGENTS: frozenset = frozenset({"consolidation", "smart", "quick", "websearch"})
    MULTI_TURN_AGENTS: frozenset = frozenset({"consolidation", "smart"})

    def resolve(
        self, agent_type: str, capabilities: ProviderCapabilities
    ) -> Optional[PromptCacheConfig]:
        if not capabilities.context_caching:
            return None

        if agent_type not in self.CACHEABLE_AGENTS:
            return None

        cache_last = agent_type in self.MULTI_TURN_AGENTS

        logger.debug(
            "💾 [PromptCacheStrategy] Caching enabled for agent_type=%s "
            "(cache_last_message=%s)",
            agent_type, cache_last,
        )
        return PromptCacheConfig(enabled=True, cache_last_message=cache_last)
