"""
Core LLM domain types — conversation models, provider capabilities, request/response shapes.

These are fundamental domain types for the conversational system — not
implementation details of any specific LLM provider.

ToolCall, MessagePart, Message: moved from ports/llm_port.py earlier.
UsageMetadata, PromptCacheConfig, CacheMetadata, ProviderCapabilities,
AutomaticFunctionCallingConfig, LLMRequest, LLMResponse: moved 2026-03-08 (TD-V2).
"""

import time
from typing import List, Any, Optional, Dict
from pydantic import BaseModel, Field


# Prompt cache boundary marker — used by prompt assembly and LLM adapters.
PROMPT_CACHE_BOUNDARY = "<!-- CACHE_BOUNDARY -->"


class ToolCall(BaseModel):
    name: str
    args: Dict[str, Any]
    thought_signature: Optional[str] = None  # Claude/OpenAI/Grok: tool call ID. Gemini: unused (raw_content carries history).


class MessagePart(BaseModel):
    text: Optional[str] = None
    full_text: Optional[str] = None  # Full response (when text=summary). Used for tiered history loading.
    consolidation_text: Optional[str] = None  # Visible only to consolidation serializer. Never exposed to agents or LLM adapters.
    tool_call: Optional[ToolCall] = None
    tool_response: Optional[Dict[str, Any]] = None  # {name: str, response: Any}
    file_data: Optional[Dict[str, Any]] = None  # {uri: str, mime_type: str}


class Message(BaseModel):
    role: str  # "user", "model", "system"
    parts: List[MessagePart]
    raw_content: Any = None
    created_at: float = Field(default_factory=time.time)


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
    ttl_seconds: Optional[int] = None   # Reserved: provider-managed TTL (Claude ephemeral = 5 min)
    cache_scope: str = "user"           # Reserved: future per-scope invalidation
    cache_key: Optional[str] = None     # Reserved: future manual cache key control


class CacheMetadata(BaseModel):
    """Provider-returned cache metadata."""
    provider: str
    cache_id: Optional[str] = None
    cache_hit: bool = False
    tokens_saved: int = 0
    created_at: float
    expires_at: Optional[float] = None


class ProviderCapabilities(BaseModel):
    """Capabilities supported by an LLM provider."""
    native_tools: bool = False
    streaming: bool = True
    context_caching: bool = False
    vision: bool = False
    max_context_window: int = 32000
    supports_system_prompt: bool = True
    supports_json_mode: bool = False
    native_grounding: bool = False  # Google Search grounding injected at API level (Gemini only)
    supports_reasoning: bool = False  # Extended reasoning / thinking mode


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
    disable_safety: bool = False
    use_grounding: bool = False       # Request native search grounding; adapter decides how to implement
    use_code_execution: bool = False  # Request sandboxed Python code execution; Gemini only, others ignore
    thinking: Optional[str] = None    # Thinking effort: "low" | "medium" | "high". None = disabled.
    timeout: Optional[int] = None     # Request timeout in seconds; None = no limit


class LLMResponse(BaseModel):
    text: Optional[str] = None
    tool_calls: List[ToolCall] = []
    raw_content: Any = None  # Provider-specific content object if needed for history
    usage_metadata: Optional[UsageMetadata] = None
    cache_metadata: Optional[CacheMetadata] = None
    grounding_metadata: Optional[Any] = None  # Gemini grounding metadata (Maps widget token, search sources)
