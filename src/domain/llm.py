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
from pydantic import BaseModel, ConfigDict, Field


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
    prompt_tokens: int = 0          # non-cached input tokens
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0      # tokens read from cache (multiplier per provider in billing.py)
    cache_creation_tokens: int = 0  # tokens written to cache (Claude only: 1.25× input price)


class AutomaticFunctionCallingConfig(BaseModel):
    """Configuration for native automatic function calling."""
    enabled: bool = False
    mode: str = "AUTO"  # AUTO, NONE, ANY


class PromptCacheConfig(BaseModel):
    """Provider-agnostic cache configuration for prompt caching."""
    enabled: bool = False
    # Multi-turn loop caching: when True, the adapter places an additional
    # cache_control breakpoint on the last content block of the messages
    # array. On the next turn the prefix up to (and including) that block is
    # served from cache via Anthropic's automatic backward lookback. Only
    # set this for agents whose loop is guaranteed multi-turn within the
    # 5-minute ephemeral TTL — single-turn calls would pay the +25% cache
    # write surcharge with no read to amortize it.
    cache_last_message: bool = False
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
    """Unified request model for LLM calls.

    extra="forbid": unknown kwargs raise ValidationError at construction time.
    Without this guard, Pydantic silently dropped extra fields — a long-form
    AI-pair-programming hazard. A 2026-03-16 commit renamed `max_tokens` to
    `max_output_tokens` in DocGenerator; the rename was accepted silently and
    DocGenerator ran with provider defaults (4-8x smaller token budget) for
    ~46 days before inspection caught it. See R14.3 in
    docs/reviews/ARCHITECTURE_INSPECTION_FOLLOWUP.md.
    """
    model_config = ConfigDict(extra="forbid")

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
    # Request timeout in seconds. Upper bound — adapters wrap their SDK call in
    # asyncio.wait_for(timeout=request.timeout) when set. Effective timeout is
    # min(request.timeout, sdk_client_timeout) — the SDK client also enforces
    # a default ceiling (Claude 120s read, OpenAI 300s, Grok 60s, Gemini per-request).
    # Both sources translate to LLMTimeoutError on expiry.
    timeout: Optional[int] = None


class LLMResponse(BaseModel):
    text: Optional[str] = None
    tool_calls: List[ToolCall] = []
    raw_content: Any = None  # Provider-specific content object if needed for history
    usage_metadata: Optional[UsageMetadata] = None
    cache_metadata: Optional[CacheMetadata] = None
    grounding_metadata: Optional[Any] = None  # Gemini grounding metadata (Maps widget token, search sources)


def build_tool_turn(response: "LLMResponse", tool_results: list) -> List[Message]:
    """Build message history entries from an LLM response with tool calls + their results.

    Standard formatting for multi-turn tool calling. Handles adapter-specific
    serialization (call_id, raw_content) so individual agents don't need to.

    Args:
        response: LLMResponse with tool_calls and raw_content.
        tool_results: List of tuples, one per tool call in execution order.
            Each tuple: (ToolCall, result_string) or (ToolCall, result_string, file_data_dict).

    Returns:
        List of Message objects to append to the conversation history.
    """
    messages: List[Message] = [
        Message(
            role="model",
            parts=[MessagePart(tool_call=tc) for tc in response.tool_calls],
            raw_content=response.raw_content,
        ),
    ]
    tool_parts: List[MessagePart] = []
    for entry in tool_results:
        tc, result_str = entry[0], entry[1]
        file_data = entry[2] if len(entry) > 2 else None
        tool_parts.append(MessagePart(
            tool_response={"name": tc.name, "response": {"result": result_str}},
        ))
        if file_data:
            tool_parts.append(MessagePart(file_data=file_data))
    messages.append(Message(role="user", parts=tool_parts))
    return messages
