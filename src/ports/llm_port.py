from abc import ABC, abstractmethod
from typing import List, Any, Optional
from pydantic import BaseModel, ConfigDict
from ..domain.user import PerformanceTier

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
    async def generate_content(
        self,
        request: Optional[LLMRequest] = None,
        model_name: Optional[str] = None,
        system_instruction: Optional[str] = None,
        messages: Optional[List[Message]] = None,
        tools: Optional[List[Any]] = None,
        temperature: float = 0.7,
        stream_callback: Optional[Any] = None,
        response_mime_type: Optional[str] = None,
        response_schema: Optional[Any] = None,
        cache_config: Optional[PromptCacheConfig] = None,
        automatic_function_calling: Optional[AutomaticFunctionCallingConfig] = None,
    ) -> LLMResponse:
        """Generates content using the specified model and parameters.

        Primary path: pass a fully-constructed ``LLMRequest`` via ``request``.
        Legacy path: pass individual keyword arguments (backward-compat shim).
        Implementations must handle both paths.
        """
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
        streaming, vision, and max context size.
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

    # Optional fallback used by BaseAgent._call_llm on LLMRateLimitError / LLMUnavailableError.
    # Populated by AgentContextBuilder from AgentProviderStrategy.STRATEGIES["fallback"].
    fallback_provider: Optional[LLMPort] = None
    fallback_model_name: Optional[str] = None
    fallback_provider_name: Optional[str] = None
