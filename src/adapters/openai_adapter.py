"""
OpenAI Adapter
==============

Adapter for OpenAI Chat Completions API.
Implements the LLMPort port with full feature parity with GeminiAdapter and ClaudeAdapter.

Supported features:
- Native function/tool calling
- JSON mode (response_format=json_object)
- Streaming
- Vision (multimodal, base64 images)
- Large context window (1M tokens on gpt-5 family)

Model tiers are pinned to the gpt-5 family.
Verify model IDs at https://platform.openai.com/docs/models before changing.

Sampling parameters (temperature, top_p, etc.) are not supported by the gpt-5 family.
Use _is_reasoning_model() to check before including them in API calls.
"""

import json
import openai
from typing import List, Any, Optional, Set
from openai import AsyncOpenAI
from ..ports.llm_port import (
    LLMPort,
    LLMResponse,
    ToolCall,
    Message,
    MessagePart,
    UsageMetadata,
    PromptCacheConfig,
    AutomaticFunctionCallingConfig,
    ProviderCapabilities,
    LLMRequest,
    PROMPT_CACHE_BOUNDARY,
)
from ..domain.user import PerformanceTier
from ..domain.exceptions import LLMRateLimitError, LLMUnavailableError
from ..utils.logger import logger


class OpenAIAdapter(LLMPort):
    """
    Adapter for OpenAI Chat Completions API.
    Uses official openai SDK. No custom base_url (api.openai.com).
    """

    # ========================================================================
    # Tier-to-model mapping
    # ECO: gpt-5.4-nano (cheapest, fastest)
    # BALANCED: gpt-5.4-mini (mid-tier quality)
    # PERFORMANCE: gpt-5.4 (flagship)
    # Verify model IDs at https://platform.openai.com/docs/models
    # ========================================================================
    MODEL_TIERS = {
        PerformanceTier.ECO: "gpt-5.4-nano",
        PerformanceTier.BALANCED: "gpt-5.4-mini",
        PerformanceTier.PERFORMANCE: "gpt-5.4",
    }

    # Models that do not support sampling parameters (temperature, top_p, etc.):
    # - gpt-5 family: confirmed via 400 error on temperature!=default (empirically verified)
    # - o1, o3 families: OpenAI reasoning models, documented restriction
    # Detected by model name prefix.
    _REASONING_PREFIXES = ("gpt-5", "o1", "o3")

    # ========================================================================
    # Provider capability declaration
    # ========================================================================
    CAPABILITIES = ProviderCapabilities(
        native_tools=True,
        context_caching=False,
        vision=True,
        max_context_window=1047576,
        supports_system_prompt=True,
        supports_json_mode=True,
    )

    def __init__(self, api_key: str) -> None:
        self.client = AsyncOpenAI(
            api_key=api_key,
            timeout=300.0,
            max_retries=2,
        )
        logger.info("✅ [OpenAIAdapter] Initialized: base_url=api.openai.com, timeout=300s")

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
        """Generate content using OpenAI Chat Completions API."""

        # Unpack LLMRequest (primary path)
        force_tool_use = False
        if request:
            model_name = request.model_name
            system_instruction = request.system_instruction
            messages = request.messages
            tools = request.tools
            temperature = request.temperature
            response_mime_type = request.response_mime_type
            response_schema = request.response_schema
            cache_config = request.cache_config
            automatic_function_calling = request.automatic_function_calling
            force_tool_use = request.force_tool_use
            stream_callback = None

        if not model_name or messages is None:
            raise ValueError("model_name and messages are required for OpenAI generate_content")

        # OpenAI does not support prompt caching (as of implementation date)
        if cache_config and cache_config.enabled:
            logger.warning("[OpenAIAdapter] Prompt caching not supported. Ignoring cache_config.")

        # Strip PROMPT_CACHE_BOUNDARY marker — OpenAI doesn't use it and the comment
        # would appear literally in the system prompt. Replace with a newline.
        if system_instruction and PROMPT_CACHE_BOUNDARY in system_instruction:
            system_instruction = system_instruction.replace(PROMPT_CACHE_BOUNDARY, "\n")

        # Convert messages to OpenAI format
        openai_messages = self._convert_messages(messages, system_instruction)

        # Convert tools to OpenAI format
        openai_tools = self._convert_tools(tools) if tools else None

        # Inject native OpenAI web search tool when grounding is requested.
        # Bypasses _convert_tools() — native tool is already in the correct dict format.
        if request and request.use_grounding:
            openai_native = [{"type": "web_search"}]
            openai_tools = openai_native + (openai_tools or [])

        # Build response_format for JSON mode.
        # Triggers: response_mime_type="application/json" OR response_schema provided.
        # response_schema itself is not forwarded — OpenAI json_object mode enforces valid JSON;
        # the schema structure is enforced by the OUTPUT_FORMAT prompt token.
        response_format = None
        if response_mime_type == "application/json" or response_schema is not None:
            response_format = {"type": "json_object"}

        logger.info(
            "🔍 [OpenAIAdapter] Request: model=%s messages=%s tools=%s json_mode=%s",
            model_name,
            len(openai_messages),
            len(openai_tools) if openai_tools else 0,
            response_format is not None,
        )

        # Streaming path
        if stream_callback:
            return await self._generate_streaming(
                model_name=model_name,
                openai_messages=openai_messages,
                openai_tools=openai_tools,
                temperature=temperature,
                response_format=response_format,
                force_tool_use=force_tool_use,
                stream_callback=stream_callback,
            )

        # Build kwargs — tool_choice only when tools present (API rejects otherwise)
        # Note: gpt-5 family requires max_completion_tokens (not max_tokens) and
        # does not support sampling params (temperature, top_p, etc.).
        create_kwargs: dict = dict(
            model=model_name,
            messages=openai_messages,
        )
        if request and request.max_tokens:
            create_kwargs["max_completion_tokens"] = request.max_tokens
        if not self._is_reasoning_model(model_name):
            create_kwargs["temperature"] = temperature
        if openai_tools:
            create_kwargs["tools"] = openai_tools
            create_kwargs["tool_choice"] = "required" if force_tool_use else "auto"
        if response_format:
            create_kwargs["response_format"] = response_format

        try:
            completion = await self.client.chat.completions.create(**create_kwargs)
        except openai.RateLimitError as e:
            raise LLMRateLimitError(str(e), http_status=429) from e
        except openai.APIStatusError as e:
            if e.status_code == 503:
                raise LLMUnavailableError(str(e), http_status=503) from e
            logger.error(
                "❌ [OpenAIAdapter] API error: model=%s status=%s error=%s",
                model_name, e.status_code, str(e),
                exc_info=True,
            )
            raise
        except Exception as e:
            logger.error(
                "❌ [OpenAIAdapter] Unexpected error: model=%s error=%s",
                model_name, str(e),
                exc_info=True,
            )
            raise

        return self._parse_response(completion)

    async def _generate_streaming(
        self,
        model_name: str,
        openai_messages: List[dict],
        openai_tools: Optional[List[dict]],
        temperature: float,
        response_format: Optional[dict],
        force_tool_use: bool,
        stream_callback: Any,
    ) -> LLMResponse:
        """
        Handle streaming response via OpenAI beta context manager.

        Uses client.chat.completions.stream() — wrapper over create(stream=True).
        Provides text_stream for incremental callbacks + get_final_completion() for
        tool calls and usage metadata (no double API call).
        """
        create_kwargs: dict = dict(
            model=model_name,
            messages=openai_messages,
        )
        if not self._is_reasoning_model(model_name):
            create_kwargs["temperature"] = temperature
        if openai_tools:
            create_kwargs["tools"] = openai_tools
            create_kwargs["tool_choice"] = "required" if force_tool_use else "auto"
        if response_format:
            create_kwargs["response_format"] = response_format

        full_text = ""
        async with self.client.chat.completions.stream(**create_kwargs) as stream:
            async for event in stream:
                if event.type == "content.delta":
                    full_text += event.delta
                    await stream_callback(full_text)

            final = await stream.get_final_completion()

        return self._parse_response(final)

    def _is_reasoning_model(self, model_name: str) -> bool:
        """Return True for models that do not support sampling params (temperature etc.)."""
        return any(model_name.startswith(p) for p in self._REASONING_PREFIXES)

    def supports_caching(self) -> bool:
        return False

    def get_capabilities(self) -> ProviderCapabilities:
        return self.CAPABILITIES

    def get_model_for_tier(self, tier: PerformanceTier) -> str:
        if tier not in self.MODEL_TIERS:
            raise ValueError(
                f"Unsupported tier '{tier}' for OpenAI. "
                f"Supported: {list(self.MODEL_TIERS.keys())}"
            )
        return self.MODEL_TIERS[tier]

    async def upload_file(self, path: str, mime_type: str) -> MessagePart:
        """
        Upload file for OpenAI by encoding to base64.
        OpenAI vision accepts base64-encoded images in message content.
        """
        import base64
        import asyncio

        def read_and_encode():
            with open(path, "rb") as f:
                return base64.standard_b64encode(f.read()).decode("utf-8")

        base64_data = await asyncio.to_thread(read_and_encode)
        logger.info(f"📎 [OpenAIAdapter] Encoded file to base64: {mime_type}, {len(base64_data)} chars")

        return MessagePart(file_data={
            "base64": base64_data,
            "mime_type": mime_type
        })

    # ------------------------------------------------------------------
    # Message conversion
    # ------------------------------------------------------------------

    def _find_tool_call_id(
        self,
        messages: List[Message],
        tool_name: str,
        current_idx: int,
        used_ids: Set[str],
    ) -> str:
        """Find the OpenAI tool_call_id for a given tool from preceding model messages."""
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

        logger.warning(f"[OpenAIAdapter] Could not find tool_call_id for '{tool_name}'")
        return f"call_{tool_name}"

    def _convert_messages(
        self,
        messages: List[Message],
        system_instruction: Optional[str] = None,
    ) -> List[dict]:
        """
        Convert domain Message objects to OpenAI message format.

        System instruction is prepended as a system message.
        Model messages with raw_content (OpenAI ChatCompletionMessage) are reconstructed
        directly to preserve tool_call IDs.
        """
        openai_messages = []

        if system_instruction:
            openai_messages.append({
                "role": "system",
                "content": system_instruction,
            })

        for idx, msg in enumerate(messages):
            # Model messages: preserve raw_content if available (keeps tool_call IDs intact)
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

            used_tool_ids: Set[str] = set()
            content_parts: List[Any] = []
            tool_calls = []

            for part in msg.parts:
                if part.text:
                    content_parts.append({"type": "text", "text": part.text})
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
                    tool_name = part.tool_response.get("name", "")
                    tool_call_id = self._find_tool_call_id(messages, tool_name, idx, used_tool_ids)
                    used_tool_ids.add(tool_call_id)
                    openai_messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": str(part.tool_response.get("response", "")),
                    })
                elif part.file_data:
                    # Vision: inline base64 image
                    if "base64" in part.file_data:
                        mime = part.file_data["mime_type"]
                        if mime.startswith("image/"):
                            content_parts.append({
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime};base64,{part.file_data['base64']}",
                                    "detail": "auto",
                                },
                            })
                        else:
                            logger.warning(
                                f"[OpenAIAdapter] Skipping unsupported MIME type '{mime}' "
                                f"(OpenAI vision accepts image/* only)"
                            )
                    elif "path" in part.file_data:
                        # Legacy path — new code should call upload_file() before building messages.
                        # _convert_messages is sync, so we use plain synchronous I/O here.
                        try:
                            import base64
                            with open(part.file_data["path"], "rb") as f:
                                b64 = base64.b64encode(f.read()).decode("utf-8")
                            mime = part.file_data["mime_type"]
                            if mime.startswith("image/"):
                                content_parts.append({
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:{mime};base64,{b64}",
                                        "detail": "auto",
                                    },
                                })
                        except Exception as e:
                            logger.error(f"[OpenAIAdapter] Failed to encode file from path: {e}")
                    elif "ref" in part.file_data:
                        logger.debug(f"[OpenAIAdapter] file ref '{part.file_data['ref']}' (no binary content)")
                    else:
                        logger.warning(f"[OpenAIAdapter] Unsupported file_data format: {list(part.file_data.keys())}")

            # Build message
            if content_parts:
                # Flatten to plain string if only text parts (no vision)
                if all(p.get("type") == "text" for p in content_parts):
                    content = " ".join(p["text"] for p in content_parts)
                else:
                    content = content_parts  # List form for multimodal

                msg_dict: dict = {"role": role, "content": content}
                if tool_calls:
                    msg_dict["tool_calls"] = tool_calls
                openai_messages.append(msg_dict)
            elif tool_calls:
                openai_messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": tool_calls,
                })
            # tool_response parts already appended inline above

        return openai_messages

    def _convert_tools(self, tools: List[Any]) -> List[dict]:
        """Convert domain tool definitions to OpenAI function calling format."""
        openai_tools = []
        for tool in tools:
            if isinstance(tool, dict):
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters", {
                            "type": "object",
                            "properties": {},
                        }),
                    },
                })
        return openai_tools

    def _parse_response(self, completion) -> LLMResponse:
        """Parse OpenAI ChatCompletion into domain LLMResponse."""
        choice = completion.choices[0]
        message = choice.message

        text = message.content or ""

        if not text and choice.finish_reason != "tool_calls":
            logger.warning(
                "⚠️ [OpenAIAdapter] Empty content: model=%s finish_reason=%s refusal=%r",
                completion.model,
                choice.finish_reason,
                getattr(message, "refusal", None),
            )

        tool_calls = []
        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    logger.warning(
                        f"[OpenAIAdapter] Failed to parse tool args for {tc.function.name}: "
                        f"{tc.function.arguments}"
                    )
                    args = {}
                tool_calls.append(ToolCall(
                    name=tc.function.name,
                    args=args,
                    thought_signature=tc.id,
                ))

        usage_metadata = None
        if completion.usage:
            cached = 0
            ptd = getattr(completion.usage, "prompt_tokens_details", None)
            if ptd:
                cached = getattr(ptd, "cached_tokens", 0) or 0
            usage_metadata = UsageMetadata(
                prompt_tokens=completion.usage.prompt_tokens,
                completion_tokens=completion.usage.completion_tokens,
                total_tokens=completion.usage.total_tokens,
                cache_read_tokens=cached,
            )

        logger.info(
            "🔍 [OpenAIAdapter] Response: text_len=%s tool_calls=%s tokens=%s",
            len(text),
            len(tool_calls),
            usage_metadata.total_tokens if usage_metadata else 0,
        )

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            raw_content=message,
            usage_metadata=usage_metadata,
        )
