"""
Grok Adapter (xAI)
==================

Adapter for xAI Grok API using OpenAI-compatible SDK.
Implements the LLMPort port.
"""

from typing import List, Any, Optional, Set
import asyncio
import json
import socket
import openai
from openai import AsyncOpenAI
from ..ports.llm_port import (
    LLMPort,
    LLMResponse,
    ToolCall,
    Message,
    MessagePart,
    UsageMetadata,
    ProviderCapabilities,
    LLMRequest
)
from ..domain.user import PerformanceTier
from ..domain.exceptions import (
    LLMClientError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from ..utils.logger import logger


class GrokAdapter(LLMPort):
    """
    Adapter for xAI Grok API.
    Uses OpenAI-compatible SDK with xAI base URL.
    """

    # ========================================================================
    # Tier-to-model mapping
    # Purpose: Decouple agent performance tier from concrete model names
    # ========================================================================
    MODEL_TIERS = {
        PerformanceTier.ECO:         "grok-4-1-fast-non-reasoning",
        PerformanceTier.BALANCED:    "grok-4-1-fast-reasoning",
        PerformanceTier.PERFORMANCE: "grok-4-1-fast-reasoning",
        PerformanceTier.ULTRA:       "grok-4-1-fast-reasoning",   # no separate ultra model yet
        PerformanceTier.TIER1:       "grok-4-1-fast-non-reasoning",
        PerformanceTier.TIER2:       "grok-4-1-fast-non-reasoning",
        PerformanceTier.TIER3:       "grok-4-1-fast-non-reasoning",
    }

    # ========================================================================
    # Provider capability declaration
    # Purpose: Feature gating and runtime validation
    # ========================================================================
    CAPABILITIES = ProviderCapabilities(
        native_tools=True,  # Grok supports function calling
        context_caching=False,  # Not supported by xAI yet
        vision=False,  # Not supported yet
        max_context_window=2000000,  # 2M tokens
        supports_system_prompt=True,
        supports_json_mode=False
    )

    def __init__(self, api_key: str):
        """
        Initialize Grok adapter with xAI API key.
        
        Args:
            api_key: xAI API key (format: xai-...)
        """
        self.api_key = api_key
        self.base_url = "https://api.x.ai/v1"  # SDK adds /v1/chat/completions automatically
        
        # ====================================================================
        # Diagnostic: DNS pre-check
        # Purpose: Identify if api.x.ai is resolvable in Cloud Run
        # ====================================================================
        try:
            ip = socket.gethostbyname("api.x.ai")
            logger.info(f"✅ [GrokAdapter] DNS OK: api.x.ai → {ip}")
        except socket.gaierror as e:
            logger.error(
                f"❌ [GrokAdapter] DNS FAILED: api.x.ai → {e}",
                extra={"dns_error": str(e), "domain": "api.x.ai"}
            )
            # Continue anyway - AsyncOpenAI will handle it
        
        # ====================================================================
        # Simplified initialization without custom httpx client
        # Using OpenAI SDK default client with increased timeout
        # ====================================================================
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=self.base_url,
            timeout=60.0,  # Simple timeout: 60 seconds total
            max_retries=2,  # OpenAI SDK default is 2
            default_headers={"User-Agent": "alek-bot/1.0"}  # Identify ourselves
        )
        
        logger.info(
            f"✅ [GrokAdapter] Initialized: base_url={self.base_url}, timeout=60s"
        )

    async def generate_content(self, request: LLMRequest) -> LLMResponse:
        """Generate content using Grok API."""
        model_name = request.model_name
        system_instruction = request.system_instruction
        messages = request.messages
        tools = request.tools
        temperature = request.temperature
        response_mime_type = request.response_mime_type
        response_schema = request.response_schema
        cache_config = request.cache_config
        force_tool_use = request.force_tool_use

        # Validate unsupported features
        if cache_config and cache_config.enabled:
            raise ValueError(
                "Grok does not support prompt caching. "
                "Use a provider with prompt_caching capability (e.g., Claude)."
            )

        # Map to json_object mode when JSON output is requested (same as OpenAI adapter).
        # response_schema itself is not forwarded — structure enforced by OUTPUT_FORMAT prompt token.
        use_json_mode = response_mime_type == "application/json" or response_schema is not None

        # Convert messages to OpenAI format
        openai_messages = self._convert_messages(messages, system_instruction)

        # Convert tools to OpenAI format
        openai_tools = self._convert_tools(tools) if tools else None

        # Inject native Grok search tools when grounding is requested.
        # Bypasses _convert_tools() — native tools are already in Grok dict format.
        if request.use_grounding:
            grok_native = [{"type": "web_search"}, {"type": "web_fetch"}]
            openai_tools = grok_native + (openai_tools or [])

        # Log request
        logger.info(
            "🔍 [GrokAdapter] Request: model=%s messages_count=%s tools=%s",
            model_name,
            len(openai_messages),
            len(openai_tools) if openai_tools else 0
        )

        # Build kwargs — only include tools/tool_choice when tools are present.
        # OpenAI API rejects tool_choice when tools is absent or empty.
        create_kwargs: dict = dict(
            model=model_name,
            messages=openai_messages,
            temperature=temperature,
        )
        if request.max_tokens:
            create_kwargs["max_tokens"] = request.max_tokens
        if use_json_mode:
            create_kwargs["response_format"] = {"type": "json_object"}
        if openai_tools:
            create_kwargs["tools"] = openai_tools
            create_kwargs["tool_choice"] = "required" if force_tool_use else "auto"

        # Make API call
        request_timeout = request.timeout
        try:
            _coro = self.client.chat.completions.create(**create_kwargs)
            completion = await (
                asyncio.wait_for(_coro, timeout=request_timeout)
                if request_timeout else _coro
            )
        except asyncio.TimeoutError as e:
            raise LLMTimeoutError(f"request timeout after {request_timeout}s") from e
        except openai.APITimeoutError as e:
            # SDK-level timeout (default httpx 60s when request.timeout is None).
            raise LLMTimeoutError(str(e)) from e
        except openai.RateLimitError as e:
            raise LLMRateLimitError(str(e), http_status=429) from e
        except openai.APIConnectionError as e:
            raise LLMNetworkError(str(e)) from e
        except openai.APIStatusError as e:
            status = getattr(e, "status_code", None)
            if status == 503:
                raise LLMUnavailableError(str(e), http_status=503) from e
            if isinstance(status, int) and 500 <= status < 600:
                raise LLMServerError(str(e), http_status=status) from e
            # 4xx (non-429) → deterministic client error. Not a failover trigger;
            # surfaces immediately + alerts.
            if isinstance(status, int) and 400 <= status < 500:
                raise LLMClientError(str(e), http_status=status) from e
            # Detailed error logging for diagnostics before re-raise
            logger.error(
                "❌ [GrokAdapter] API Error: type=%s status=%s message=%s model=%s",
                type(e).__name__, status, str(e), model_name,
                exc_info=True,
            )
            raise
        except Exception as e:
            logger.error(
                "❌ [GrokAdapter] Unexpected error: type=%s message=%s model=%s",
                type(e).__name__, str(e), model_name,
                exc_info=True,
            )
            raise

        # Parse response
        return self._parse_response(completion)

    def supports_caching(self) -> bool:
        """Grok does not support prompt caching."""
        return False

    def get_capabilities(self) -> ProviderCapabilities:
        """
        Return Grok provider capabilities.

        Grok supports:
        - Native tools (function calling)
        - Very large context window (2M tokens)

        Grok does NOT support:
        - Prompt caching
        - Vision (yet)
        """
        return self.CAPABILITIES

    def get_model_for_tier(self, tier: PerformanceTier) -> str:
        """
        Map performance tier to Grok model name.

        Raises:
            ValueError: If tier is not supported.
        """
        if tier not in self.MODEL_TIERS:
            raise ValueError(
                f"Unsupported tier '{tier}' for Grok. "
                f"Supported: {list(self.MODEL_TIERS.keys())}"
            )
        return self.MODEL_TIERS[tier]

    async def upload_file(self, path: str, mime_type: str) -> MessagePart:
        """Grok does not support file uploads yet."""
        raise NotImplementedError(
            "GrokAdapter does not support file uploads. "
            "Vision capabilities not available yet."
        )

    def _find_tool_call_id(self, messages: List[Message], tool_name: str, current_idx: int, used_ids: Set[str]) -> str:
        """
        Find the OpenAI tool_call_id for a given tool name from preceding model messages.

        Searches backward for a model message whose raw_content contains a tool_call
        with the given name and an unused ID. Handles parallel same-tool calls via used_ids.
        """
        for i in range(current_idx - 1, -1, -1):
            prev = messages[i]
            if prev.role != "model":
                continue

            # Primary: raw_content from OpenAI ChatCompletionMessage
            if prev.raw_content is not None and hasattr(prev.raw_content, "tool_calls") and prev.raw_content.tool_calls:
                for tc in prev.raw_content.tool_calls:
                    if tc.function.name == tool_name and tc.id not in used_ids:
                        return tc.id

            # Fallback: thought_signature stored in parts
            for part in prev.parts:
                if part.tool_call and part.tool_call.name == tool_name:
                    sig = part.tool_call.thought_signature
                    if sig and sig not in used_ids:
                        return sig

        logger.warning(f"[GrokAdapter] Could not find tool_call_id for '{tool_name}'")
        return f"call_{tool_name}"

    def _convert_messages(
        self,
        messages: List[Message],
        system_instruction: Optional[str] = None
    ) -> List[dict]:
        """
        Convert domain Message objects to OpenAI message format.

        Args:
            messages: Domain Message objects
            system_instruction: Optional system prompt (prepended as first message)

        Returns:
            List of OpenAI-format messages
        """
        openai_messages = []

        # Add system instruction as first message if provided
        if system_instruction:
            openai_messages.append({
                "role": "system",
                "content": system_instruction
            })

        for idx, msg in enumerate(messages):
            # ------------------------------------------------------------------
            # Model messages: prefer raw_content to preserve original tool_call IDs.
            # When raw_content is an OpenAI ChatCompletionMessage, reconstruct
            # the assistant message directly instead of going through parts
            # (parts=[] for tool-call turns — they only carry raw_content).
            # ------------------------------------------------------------------
            if msg.role == "model" and msg.raw_content is not None and hasattr(msg.raw_content, "tool_calls"):
                raw = msg.raw_content
                assistant_msg: dict = {"role": "assistant", "content": raw.content}
                if raw.tool_calls:
                    assistant_msg["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in raw.tool_calls
                    ]
                openai_messages.append(assistant_msg)
                continue

            role = "assistant" if msg.role == "model" else msg.role

            # Track used tool_call_ids within this message to handle parallel
            # same-tool calls (e.g. 2× search_memory → different IDs).
            used_tool_ids: Set[str] = set()
            content_parts = []
            tool_calls = []

            for part in msg.parts:
                if part.text:
                    content_parts.append(part.text)
                elif part.tool_call:
                    tool_calls.append({
                        "id": part.tool_call.thought_signature or f"call_{len(tool_calls)}",
                        "type": "function",
                        "function": {
                            "name": part.tool_call.name,
                            "arguments": json.dumps(part.tool_call.args),
                        },
                    })
                elif part.tool_response:
                    # Tool result — must reference the exact tool_call_id from the model message.
                    tool_name = part.tool_response.get("name", "")
                    tool_call_id = self._find_tool_call_id(messages, tool_name, idx, used_tool_ids)
                    used_tool_ids.add(tool_call_id)
                    openai_messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": str(part.tool_response.get("response", "")),
                    })
                elif part.file_data:
                    if "ref" in part.file_data:
                        logger.debug(f"[GrokAdapter] file ref '{part.file_data['ref']}' (no binary content)")
                    else:
                        logger.warning(
                            "[GrokAdapter] File attachment ignored - Grok does not support vision yet. "
                            f"file_data keys: {list(part.file_data.keys())}"
                        )

            # Build the message only if there's content or tool calls
            if content_parts:
                message: dict = {"role": role, "content": " ".join(content_parts)}
                if tool_calls:
                    message["tool_calls"] = tool_calls
                openai_messages.append(message)
            elif tool_calls:
                openai_messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": tool_calls,
                })
            # tool_response parts are already appended inline above — no outer message needed

        return openai_messages

    def _convert_tools(self, tools: List[Any]) -> List[dict]:
        """
        Convert domain tool definitions to Grok tool format.
        
        Supports two types of tools:
        1. Custom function calls (our tools like search_memory)
        2. Native Grok tools (web_search, code_execution, etc.)
        
        Args:
            tools: List of tool definitions (dict or structured)
            
        Returns:
            List of Grok-format tool definitions
        """
        grok_tools = []
        
        for tool in tools:
            if isinstance(tool, dict):
                tool_type = tool.get("type", "function")
                
                if tool_type == "function":
                    # Custom function call - convert to OpenAI schema
                    grok_tools.append({
                        "type": "function",
                        "function": {
                            "name": tool["name"],
                            "description": tool.get("description", ""),
                            "parameters": tool.get("parameters", {
                                "type": "object",
                                "properties": {}
                            })
                        }
                    })
                else:
                    # Native Grok tool (web_search, code_execution, etc.)
                    # Pass through as-is (already in Grok format)
                    grok_tools.append(tool)
        
        return grok_tools

    def _parse_response(self, completion) -> LLMResponse:
        """
        Parse OpenAI completion into domain LLMResponse.
        
        Args:
            completion: OpenAI ChatCompletion object
            
        Returns:
            Domain LLMResponse object
        """
        choice = completion.choices[0]
        message = choice.message

        # Extract text content
        text = message.content or ""

        # Extract tool calls
        tool_calls = []
        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    raw = tc.function.arguments or ""
                    logger.warning(
                        f"[GrokAdapter] Truncated tool args for {tc.function.name} "
                        f"(len={len(raw)}): {raw[:200]}..."
                    )
                    args = {
                        "_parse_error": "truncated_json",
                        "_raw_prefix": raw[:500],
                    }
                tool_calls.append(ToolCall(
                    name=tc.function.name,
                    args=args,
                    thought_signature=tc.id
                ))

        # Extract usage metadata
        usage_metadata = None
        if completion.usage:
            usage_metadata = UsageMetadata(
                prompt_tokens=completion.usage.prompt_tokens,
                completion_tokens=completion.usage.completion_tokens,
                total_tokens=completion.usage.total_tokens
            )

        logger.info(
            "🔍 [GrokAdapter] Response: text_len=%s tool_calls=%s tokens=%s",
            len(text),
            len(tool_calls),
            usage_metadata.total_tokens if usage_metadata else 0
        )

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            raw_content=message,  # Store original for potential round-trip
            usage_metadata=usage_metadata
        )
