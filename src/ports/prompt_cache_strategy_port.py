"""Port for resolving prompt cache configuration based on agent identity.

Implements the principle: agents declare WHAT they are,
the strategy decides HOW (and whether) to cache.

See: docs/10_rfcs/HEXAGONAL_PROMPT_CACHING_RFC.md
"""

from abc import ABC, abstractmethod
from typing import Optional

from src.domain.llm import ProviderCapabilities, PromptCacheConfig


class PromptCacheStrategyPort(ABC):
    """Abstract port for prompt caching strategy resolution.

    Given an agent_type and provider capabilities, resolves whether
    prompt caching should be applied and with what configuration.
    """

    @abstractmethod
    def resolve(
        self, agent_type: str, capabilities: ProviderCapabilities
    ) -> Optional[PromptCacheConfig]:
        """Resolve cache configuration for a given agent type and provider.

        Args:
            agent_type: Agent identity string (e.g., "consolidation", "smart").
            capabilities: Provider feature flags (context_caching, etc.).

        Returns:
            PromptCacheConfig if caching should be applied, None otherwise.
        """
        pass
