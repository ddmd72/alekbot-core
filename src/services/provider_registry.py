from typing import Dict, List

from ..ports.llm_service import LLMService


# ============================================================================
# NEW Provider Refactor Session 8: Provider registry service
# Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
# Purpose: Centralize provider lookup for application orchestration
# ============================================================================
class ProviderRegistry:
    """Simple provider registry (Service Locator pattern)."""

    def __init__(self) -> None:
        self._providers: Dict[str, LLMService] = {}

    def register(self, name: str, provider: LLMService) -> None:
        """Register a provider instance."""
        self._providers[name] = provider

    def get(self, name: str) -> LLMService:
        """Get provider by name. Raises ValueError if not found."""
        if name not in self._providers:
            raise ValueError(
                f"Provider '{name}' not registered. Available: {list(self._providers.keys())}"
            )
        return self._providers[name]

    def list_available(self) -> List[str]:
        """List all registered provider names."""
        return list(self._providers.keys())