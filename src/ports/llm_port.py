from abc import ABC, abstractmethod
from typing import Any, Optional, TYPE_CHECKING
from pydantic import BaseModel, ConfigDict
from ..domain.user import PerformanceTier

if TYPE_CHECKING:
    # Forward-only reference: AgentExecutionContext.resilience_port is typed as
    # ProviderResiliencePort but the import lives behind TYPE_CHECKING to keep
    # ports/ free of cross-port imports (REQ-ARCH-06). At runtime the field is
    # validated as ``Any`` and isinstance-checked by callers.
    from .provider_resilience_port import ProviderResiliencePort

# Domain types — canonical definitions in domain/llm.py.
# Re-exported here for backward compatibility with existing imports.
from ..domain.llm import (
    ToolCall,
    MessagePart,
    Message,
    PROMPT_CACHE_BOUNDARY,
    UsageMetadata,
    AutomaticFunctionCallingConfig,
    PromptCacheConfig,
    CacheMetadata,
    ProviderCapabilities,
    LLMRequest,
    LLMResponse,
)

__all__ = [
    "LLMPort",
    "AgentExecutionContext",
    # Re-exports from domain/llm.py
    "ToolCall",
    "MessagePart",
    "Message",
    "PROMPT_CACHE_BOUNDARY",
    "UsageMetadata",
    "AutomaticFunctionCallingConfig",
    "PromptCacheConfig",
    "CacheMetadata",
    "ProviderCapabilities",
    "LLMRequest",
    "LLMResponse",
]


class LLMPort(ABC):
    """
    Abstract Port for LLM providers.
    Follows Hexagonal Architecture principles to decouple the orchestrator from specific LLM providers.
    """

    @abstractmethod
    async def generate_content(self, request: LLMRequest) -> LLMResponse:
        """Generate content from a fully-constructed ``LLMRequest``."""
        pass

    @abstractmethod
    def supports_caching(self) -> bool:
        """Returns True if this provider supports prompt caching."""
        pass

    @abstractmethod
    async def upload_file(self, path: str, mime_type: str) -> MessagePart:
        """Uploads a file and returns a MessagePart with file data."""
        pass

    @abstractmethod
    def get_capabilities(self) -> ProviderCapabilities:
        """
        Return provider capability flags for feature gating.

        Implementations should describe support for native tools, caching,
        vision, and max context size.
        """
        pass

    @abstractmethod
    def get_model_for_tier(self, tier: PerformanceTier) -> str:
        """
        Map a performance tier to a provider-specific model name.

        Implementations should raise ValueError for unsupported tiers.
        """
        pass


class AgentExecutionContext(BaseModel):
    """Context required for an agent to execute an LLM request.

    Kept in ports/ (not domain/) because it references LLMPort (an ABC from this module).
    Moving to domain/ would create a domain→ports dependency.
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    agent_type: str
    provider: LLMPort
    model_name: str
    tier: PerformanceTier
    capabilities: ProviderCapabilities

    # Provider name string for structured logging (e.g. "gemini", "claude").
    provider_name: str = ""

    # Optional fallback used by BaseAgent._call_llm on FAILOVER_TRIGGER_TYPES.
    # Populated by AgentContextBuilder from AgentProviderStrategy.STRATEGIES["fallback"].
    fallback_provider: Optional[LLMPort] = None
    fallback_model_name: Optional[str] = None
    fallback_provider_name: Optional[str] = None

    # Provider-level circuit breaker. Required — no silent no-op fallback.
    # Missing wiring is a composition bug, not runtime state. AgentContextBuilder
    # MUST inject the per-process singleton from ServiceContainer. Test fixtures
    # construct AgentExecutionContext with an explicit InMemoryProviderResilience()
    # instance (the canonical test double — pure in-memory, no I/O).
    # Typed as Any at runtime to avoid a cross-port import (REQ-ARCH-06); the
    # static annotation lives behind TYPE_CHECKING above. Callers obtain
    # static type guarantees from the TYPE_CHECKING annotation; runtime users
    # treat it as a duck-typed ProviderResiliencePort instance.
    resilience_port: Any

    def __eq__(self, other: object) -> bool:
        # ``resilience_port`` is process-local infrastructure (a singleton),
        # not part of context identity. Two contexts that differ only by
        # which resilience-port instance they hold are semantically equal —
        # callers comparing contexts (e.g. ExecutionOverride equality) care
        # about routing identity (agent_type / provider / model / tier /
        # fallback_*), not which breaker bookkeeping object is plugged in.
        if not isinstance(other, AgentExecutionContext):
            return NotImplemented
        return self.model_dump(exclude={"resilience_port"}) == other.model_dump(
            exclude={"resilience_port"}
        )

    def __hash__(self) -> int:
        # BaseModel disables __hash__ when __eq__ is overridden; restore it
        # using the same identity fields used by __eq__. Stable across
        # instances with different resilience_port singletons.
        return hash(
            (
                self.agent_type,
                self.model_name,
                self.tier,
                self.provider_name,
                self.fallback_model_name,
                self.fallback_provider_name,
            )
        )
