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
    """

    CACHEABLE_AGENTS: frozenset = frozenset({"consolidation", "smart", "quick", "websearch"})

    def resolve(
        self, agent_type: str, capabilities: ProviderCapabilities
    ) -> Optional[PromptCacheConfig]:
        if not capabilities.context_caching:
            return None

        if agent_type not in self.CACHEABLE_AGENTS:
            return None

        logger.debug(
            "💾 [PromptCacheStrategy] Caching enabled for agent_type=%s",
            agent_type,
        )
        return PromptCacheConfig(enabled=True)
