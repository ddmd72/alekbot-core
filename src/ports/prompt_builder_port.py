"""
PromptBuilderPort — abstract interface for prompt assembly.

Justification for port promotion:
- v1 (PromptBuilder) and v3 (PromptAssemblyService) already co-exist.
- Agents must not depend on a concrete service class.
- Future implementations: stripped-down prompt builder, test double.
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from ..domain.agent import RoutingMetadata
from ..domain.llm import ProviderCapabilities


class PromptBuilderPort(ABC):
    """Abstract interface for prompt assembly services."""

    @abstractmethod
    async def preload_components(self) -> None:
        """Preload all system prompt components (warm-up cache)."""

    @abstractmethod
    async def build_for_agent(
        self,
        agent_type: str,
        user_id: Optional[str] = None,
        account_id: Optional[str] = None,
        routing_metadata: Optional[RoutingMetadata] = None,
        capabilities: Optional[ProviderCapabilities] = None,
        biographical_facts: Optional[List[Dict]] = None,
        conversation_history: Optional[List[dict]] = None,
        include_biographical: bool = True,
        kb_preamble: bool = False,
        agent_notes: Optional[List[dict]] = None,
    ) -> str:
        """
        Build complete system prompt string for the given agent type.

        Returns:
            Fully formatted system prompt string.
        """

    @abstractmethod
    def merge_enriched_context_with_biographical(
        self,
        enriched_context: Optional[Dict],
        cached_biographical: Optional[List[Dict]] = None,
    ) -> List[Dict]:
        """Merge router enriched facts with cached biographical facts."""

    @abstractmethod
    def invalidate_cache(self, component_key: Optional[str] = None) -> None:
        """Invalidate one or all cache entries."""

    @abstractmethod
    def invalidate_biographical_cache(self, user_id: str) -> None:
        """Invalidate biographical cache for a specific user."""

    @abstractmethod
    def get_cache_stats(self) -> Dict:
        """Return cache statistics for monitoring."""
