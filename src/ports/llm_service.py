from abc import ABC, abstractmethod
from typing import List, Any, Optional, Dict, Union
from pydantic import BaseModel, Field, ConfigDict
from ..domain.user import PerformanceTier

# Conversation types — canonical definitions live in domain.
# Reexported here for backward compatibility with existing imports.
from ..domain.llm import ToolCall, MessagePart, Message

class UsageMetadata(BaseModel):
    """Token usage metadata from LLM providers."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class AutomaticFunctionCallingConfig(BaseModel):
    """Configuration for native automatic function calling."""
    enabled: bool = False
    mode: str = "AUTO"  # AUTO, NONE, ANY


class PromptCacheConfig(BaseModel):
    """Provider-agnostic cache configuration for prompt caching."""
    enabled: bool = False
    ttl_seconds: Optional[int] = None
    cache_scope: str = "user"
    cache_key: Optional[str] = None


class CacheMetadata(BaseModel):
    """Provider-returned cache metadata."""
    provider: str
    cache_id: Optional[str] = None
    cache_hit: bool = False
    tokens_saved: int = 0
    created_at: float
    expires_at: Optional[float] = None


# ============================================================================
# NEW Provider Refactor Session 4: Provider capabilities model
# Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
# Purpose: Describe provider feature support and limits
# ============================================================================
class ProviderCapabilities(BaseModel):
    """Capabilities supported by an LLM provider."""
    native_tools: bool = False
    streaming: bool = True
    context_caching: bool = False
    vision: bool = False
    max_context_window: int = 32000
    supports_system_prompt: bool = True
    supports_json_mode: bool = False


# ============================================================================
# NEW Provider Refactor Session 4: Unified LLM request model
# Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
# Purpose: Standardize request parameters across providers
# ============================================================================
class LLMRequest(BaseModel):
    """Unified request model for LLM calls."""
    model_name: str
    messages: List[Message]
    system_instruction: Optional[str] = None
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    tools: Optional[List[Any]] = None
    stream: bool = False
    response_mime_type: Optional[str] = None
    response_schema: Optional[Any] = None
    cache_config: Optional[PromptCacheConfig] = None
    automatic_function_calling: Optional[AutomaticFunctionCallingConfig] = None
    force_tool_use: bool = False


class LLMResponse(BaseModel):
    text: Optional[str] = None
    tool_calls: List[ToolCall] = []
    raw_content: Any = None # Provider-specific content object if needed for history
    usage_metadata: Optional[UsageMetadata] = None
    cache_metadata: Optional[CacheMetadata] = None

class LLMService(ABC):
    """
    Abstract Port for LLM services.
    Follows Hexagonal Architecture principles to decouple the orchestrator from specific LLM providers.
    """

    @abstractmethod
    async def generate_content(
        self, 
        model_name: str, 
        system_instruction: str, 
        messages: List[Message], 
        tools: Optional[List[Any]] = None,
        temperature: float = 0.7,
        stream_callback: Optional[Any] = None,
        response_mime_type: Optional[str] = None,
        response_schema: Optional[Any] = None,
        cache_config: Optional[PromptCacheConfig] = None,
        automatic_function_calling: Optional[AutomaticFunctionCallingConfig] = None
    ) -> LLMResponse:
        """Generates content using the specified model and parameters."""
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
    """Context required for an agent to execute an LLM request."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    agent_type: str
    provider: LLMService
    model_name: str
    tier: PerformanceTier
    capabilities: ProviderCapabilities
