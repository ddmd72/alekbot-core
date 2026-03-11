from typing import Dict, Generic, List, TypeVar

T = TypeVar("T")


class ProviderRegistry(Generic[T]):
    """
    Generic registry for named service providers.

    Used for two provider families:
      ProviderRegistry[LLMPort]   — LLM providers (gemini, claude, grok, openai)
      ProviderRegistry[DeepResearchPort] — async job providers (gemini, openai deep research)

    Pattern: register(name, impl) at bootstrap → get(name) at runtime.
    """

    def __init__(self) -> None:
        self._providers: Dict[str, T] = {}

    def register(self, name: str, provider: T) -> None:
        """Register a provider instance under the given name."""
        self._providers[name] = provider

    def get(self, name: str) -> T:
        """Return provider by name. Raises ValueError if not registered."""
        if name not in self._providers:
            raise ValueError(
                f"Provider '{name}' not registered. Available: {list(self._providers.keys())}"
            )
        return self._providers[name]

    def list_available(self) -> List[str]:
        """List all registered provider names."""
        return list(self._providers.keys())
