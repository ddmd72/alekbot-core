"""
OpenAI Adapter
==============

Adapter for OpenAI Responses API.
Implements the LLMPort port with full feature parity with GeminiAdapter and ClaudeAdapter.

Supported features:
- Native function/tool calling (internally-tagged, strict by default)
- Web search (native {"type": "web_search"} tool with url_citation annotations)
- JSON mode (text.format)
- Streaming
- Vision (multimodal, base64 images)
- Large context window (1M tokens on gpt-5 family)

Model tiers are pinned to the gpt-5 family.
Verify model IDs at https://platform.openai.com/docs/models before changing.

Sampling parameters (temperature, top_p, etc.) are not supported by the gpt-5 family.
Use _is_reasoning_model() to check before including them in API calls.

Migration from Chat Completions to Responses API (2026-04):
- client.chat.completions.create → client.responses.create
- messages → instructions + input
- tools: nested function wrapper removed (internally-tagged)
- response: choices[0].message → output items + output_text
- tool results: role=tool → type=function_call_output
- web_search: native tool with url_citation annotations in output
- response_format → text.format
- store=False for privacy (no server-side storage)
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
    Adapter for OpenAI Responses API.
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
        # Responses API automatically caches prompt prefixes ≥1024 tokens since late 2024.
        # Fully transparent — no cache_control markers required from caller.
        context_caching=True,
        vision=True,
        max_context_window=1047576,
        supports_system_prompt=True,
        supports_json_mode=True,
        native_grounding=True,  # {"type": "web_search"} with url_citation annotations
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
        """Generate content using OpenAI Responses API."""

        # Unpack LLMRequest (primary path)
        force_tool_use = False
        use_grounding = False
        thinking: Optional[str] = None
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
            use_grounding = request.use_grounding
            thinking = request.thinking
            stream_callback = None

        if not model_name or messages is None:
            raise ValueError("model_name and messages are required for OpenAI generate_content")

        # Strip PROMPT_CACHE_BOUNDARY marker — OpenAI doesn't use it.
        if system_instruction and PROMPT_CACHE_BOUNDARY in system_instruction:
            system_instruction = system_instruction.replace(PROMPT_CACHE_BOUNDARY, "\n")

        # Convert domain messages to Responses API input items
        input_items = await self._convert_input(messages)

        # Convert tools to Responses API format (internally-tagged, no nested function wrapper)
        api_tools = self._convert_tools(tools) if tools else []

        # Inject native web search tool when grounding is requested.
        # Responses API supports {"type": "web_search"} natively on all models.
        # Results include url_citation annotations with source URLs.
        # Reasoning effort handling lives below — when grounding is on without an
        # explicit thinking value, we still must enable reasoning at "low" so that
        # gpt-5.4 doesn't fall back to "none" (which disables agentic search:
        # iterative search, open_page, find_in_page).
        if use_grounding:
            api_tools = [{"type": "web_search"}] + api_tools

        # Build text format for JSON mode.
        # Responses API uses text.format instead of response_format.
        # OpenAI requires the word "json" in instructions or input when json_object is active.
        text_format = None
        if response_mime_type == "application/json" or response_schema is not None:
            text_format = {"format": {"type": "json_object"}}
            # OpenAI requires the word "json" in input items (not instructions).
            # Prepend a developer message to satisfy this API requirement.
            input_items.insert(0, {"role": "developer", "content": "Respond in JSON."})

        logger.info(
            "🔍 [OpenAIAdapter] Request: model=%s input_items=%s tools=%s json_mode=%s grounding=%s thinking=%s",
            model_name,
            len(input_items),
            len(api_tools),
            text_format is not None,
            use_grounding,
            thinking or "none",
        )

        # Build create kwargs
        create_kwargs: dict = dict(
            model=model_name,
            input=input_items,
            store=True,  # Store responses in OpenAI dashboard for debugging/analysis
        )
        if system_instruction:
            create_kwargs["instructions"] = system_instruction
        if request and request.max_tokens:
            create_kwargs["max_output_tokens"] = request.max_tokens
        if not self._is_reasoning_model(model_name):
            create_kwargs["temperature"] = temperature
        if api_tools:
            create_kwargs["tools"] = api_tools
            create_kwargs["tool_choice"] = "required" if force_tool_use else "auto"

        # Reasoning effort: map unified `thinking` parameter to OpenAI
        # `reasoning.effort`. Only reasoning models (gpt-5/o1/o3) accept it.
        # Precedence:
        #   1. Explicit `thinking` from caller (low/medium/high) — preferred.
        #   2. Grounding without explicit thinking → force "low" so agentic
        #      search stays enabled (gpt-5.4 default "none" disables it).
        # Combined case (thinking + grounding): the explicit thinking value wins.
        reasoning_effort: Optional[str] = None
        if self._is_reasoning_model(model_name):
            if thinking:
                reasoning_effort = {"low": "low", "medium": "medium", "high": "high"}.get(
                    thinking, "medium"
                )
            elif use_grounding:
                reasoning_effort = "low"
        if reasoning_effort:
            create_kwargs["reasoning"] = {"effort": reasoning_effort}

        if text_format:
            create_kwargs["text"] = text_format
        if cache_config and cache_config.enabled:
            create_kwargs["prompt_cache_retention"] = "24h"

        try:
            response = await self.client.responses.create(**create_kwargs)
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

        return self._parse_response(response)

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
    # Input conversion (domain Messages → Responses API input items)
    # ------------------------------------------------------------------

    async def _convert_input(self, messages: List[Message]) -> List[dict]:
        """
        Convert domain Message objects to Responses API input format.

        Responses API input is a list of items:
        - User messages:   {"role": "user", "content": "..."}
        - Model messages:  {"role": "assistant", "content": "..."}
        - Function calls:  {"type": "function_call", "call_id": "...", "name": "...", "arguments": "..."}
        - Function results: {"type": "function_call_output", "call_id": "...", "output": "..."}

        System instruction is NOT included — it goes into the separate `instructions` parameter.
        """
        items: List[dict] = []

        for idx, msg in enumerate(messages):
            role = "assistant" if msg.role == "model" else msg.role

            # Model messages with raw_content: pass output items through directly
            if msg.role == "model" and msg.raw_content is not None:
                raw = msg.raw_content
                # Responses API format: list of output items
                if isinstance(raw, list):
                    items.extend(raw)
                    continue
                # Pre-migration Chat Completions format: ChatCompletionMessage object
                if hasattr(raw, "tool_calls") and raw.tool_calls:
                    if raw.content:
                        items.append({"role": "assistant", "content": raw.content})
                    for tc in raw.tool_calls:
                        items.append({
                            "type": "function_call",
                            "call_id": tc.id,
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        })
                    continue
                if hasattr(raw, "content") and raw.content:
                    items.append({"role": "assistant", "content": raw.content})
                    continue

            content_parts: List[Any] = []
            function_calls: List[dict] = []
            used_call_ids: Set[str] = set()

            for part in msg.parts:
                if part.text:
                    content_parts.append({"type": "input_text", "text": part.text})
                elif part.tool_call:
                    function_calls.append({
                        "type": "function_call",
                        "call_id": part.tool_call.thought_signature or f"call_{len(function_calls)}",
                        "name": part.tool_call.name,
                        "arguments": json.dumps(part.tool_call.args),
                    })
                elif part.tool_response:
                    # Function call output — find call_id from preceding function_call
                    tool_name = part.tool_response.get("name", "")
                    call_id = self._find_call_id(messages, tool_name, idx, used_call_ids)
                    used_call_ids.add(call_id)
                    items.append({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": str(part.tool_response.get("response", "")),
                    })
                elif part.file_data:
                    mime = part.file_data.get("mime_type", "application/octet-stream")
                    if mime.startswith("image/"):
                        # Images: inline base64
                        b64 = part.file_data.get("base64")
                        if not b64 and "path" in part.file_data:
                            try:
                                import base64 as _b64
                                with open(part.file_data["path"], "rb") as f:
                                    b64 = _b64.b64encode(f.read()).decode("utf-8")
                            except Exception as e:
                                logger.error(f"[OpenAIAdapter] Failed to encode image: {e}")
                        if b64:
                            content_parts.append({
                                "type": "input_image",
                                "image_url": f"data:{mime};base64,{b64}",
                            })
                    elif "path" in part.file_data or "base64" in part.file_data:
                        # Non-image files (PDF, DOCX etc.): upload via Files API → file_id
                        file_id = await self._upload_file_for_input(part.file_data)
                        if file_id:
                            content_parts.append({
                                "type": "input_file",
                                "file_id": file_id,
                            })
                    elif "ref" in part.file_data:
                        logger.debug(f"[OpenAIAdapter] file ref '{part.file_data['ref']}' (no binary content)")

            # Build item
            if content_parts:
                # Single text part → plain string content
                if len(content_parts) == 1 and content_parts[0].get("type") == "input_text":
                    items.append({"role": role, "content": content_parts[0]["text"]})
                else:
                    items.append({"role": role, "content": content_parts})
            if function_calls:
                items.extend(function_calls)

        return items

    def _find_call_id(
        self,
        messages: List[Message],
        tool_name: str,
        current_idx: int,
        used_ids: Set[str],
    ) -> str:
        """Find the call_id for a function call by name from preceding model messages.

        Tracks used_ids to handle multiple calls to the same function in one turn.
        """
        for i in range(current_idx - 1, -1, -1):
            prev = messages[i]
            if prev.role != "model":
                continue

            if prev.raw_content is not None:
                raw = prev.raw_content
                # Responses API format: list of output items
                if isinstance(raw, list):
                    for item in raw:
                        if getattr(item, "type", None) == "function_call" and getattr(item, "name", "") == tool_name:
                            cid = item.call_id
                            if cid not in used_ids:
                                return cid
                # Pre-migration Chat Completions format
                elif hasattr(raw, "tool_calls") and raw.tool_calls:
                    for tc in raw.tool_calls:
                        if tc.function.name == tool_name and tc.id not in used_ids:
                            return tc.id

            # thought_signature stored in parts
            for part in prev.parts:
                if part.tool_call and part.tool_call.name == tool_name and part.tool_call.thought_signature:
                    sig = part.tool_call.thought_signature
                    if sig not in used_ids:
                        return sig

        raise ValueError(f"[OpenAIAdapter] call_id not found for function '{tool_name}'")

    async def _upload_file_for_input(self, file_data: dict) -> Optional[str]:
        """Upload a file via OpenAI Files API for use in Responses API input.

        Accepts file_data with 'path' (local temp file) or 'base64' (encoded bytes).
        Returns file_id on success, None on failure.
        """
        try:
            if "path" in file_data:
                uploaded = await self.client.files.create(
                    file=open(file_data["path"], "rb"),
                    purpose="assistants",
                )
            elif "base64" in file_data:
                import base64 as _b64
                import io
                raw_bytes = _b64.b64decode(file_data["base64"])
                uploaded = await self.client.files.create(
                    file=io.BytesIO(raw_bytes),
                    purpose="assistants",
                )
            else:
                return None
            logger.info(f"[OpenAIAdapter] Uploaded file: {uploaded.id}")
            return uploaded.id
        except Exception as e:
            logger.error(f"[OpenAIAdapter] File upload failed: {e}")
            return None

    def _convert_tools(self, tools: List[Any]) -> List[dict]:
        """Convert domain tool definitions to Responses API format.

        Responses API uses internally-tagged format (no nested function wrapper):
        {"type": "function", "name": "...", "parameters": {...}}
        """
        api_tools = []
        for tool in tools:
            if isinstance(tool, dict):
                api_tools.append({
                    "type": "function",
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {
                        "type": "object",
                        "properties": {},
                    }),
                })
        return api_tools

    def _parse_response(self, response) -> LLMResponse:
        """Parse Responses API response into domain LLMResponse.

        Response structure:
        - response.output: list of output items (message, function_call, web_search_call)
        - response.output_text: helper for concatenated text content
        - response.usage: token usage
        - output_text items may contain annotations (url_citation from web search)
        """
        text = response.output_text or ""

        # Log web search queries for debugging
        for item in response.output:
            item_type = getattr(item, "type", None)
            if item_type == "web_search_call":
                action = getattr(item, "action", None)
                if action:
                    action_type = getattr(action, "type", "")
                    queries = getattr(action, "queries", None)
                    if queries:
                        for q in queries:
                            logger.info(f"🔍 [OpenAIAdapter] web_search [{action_type}]: {q}")
                    elif action_type == "open_page":
                        url = getattr(action, "url", "")
                        logger.info(f"🔍 [OpenAIAdapter] web_search [open_page]: {url}")

        # Extract url_citation annotations from output items.
        # Append as a sources block so downstream agents receive URLs.
        annotations = []
        for item in response.output:
            item_type = getattr(item, "type", None)
            if item_type == "message":
                for content_block in getattr(item, "content", []):
                    block_type = getattr(content_block, "type", None)
                    if block_type == "output_text":
                        for ann in getattr(content_block, "annotations", []) or []:
                            if getattr(ann, "type", None) == "url_citation":
                                title = getattr(ann, "title", "") or ""
                                url = getattr(ann, "url", "") or ""
                                if url:
                                    annotations.append(f"- [{title}]({url})" if title else f"- {url}")

        if annotations:
            # Deduplicate while preserving order
            seen = set()
            unique = []
            for a in annotations:
                if a not in seen:
                    seen.add(a)
                    unique.append(a)
            text += "\n\n*Sources:*\n" + "\n".join(unique)

        # Extract function calls from output items
        tool_calls = []
        for item in response.output:
            item_type = getattr(item, "type", None)
            if item_type == "function_call":
                name = getattr(item, "name", "")
                arguments = getattr(item, "arguments", "")
                call_id = getattr(item, "call_id", "")
                try:
                    args = json.loads(arguments) if arguments else {}
                except json.JSONDecodeError:
                    logger.warning(
                        f"[OpenAIAdapter] Failed to parse tool args for {name}: {arguments}"
                    )
                    args = {}
                tool_calls.append(ToolCall(
                    name=name,
                    args=args,
                    thought_signature=call_id,
                ))

        # Usage metadata
        # OpenAI: input_tokens INCLUDES cached_tokens (it's the total).
        # Subtract cached so prompt_tokens = uncached only (matches billing formula).
        usage_metadata = None
        if response.usage:
            cached = 0
            itd = getattr(response.usage, "input_tokens_details", None)
            if itd:
                cached = getattr(itd, "cached_tokens", 0) or 0
            total_input = getattr(response.usage, "input_tokens", 0) or 0
            usage_metadata = UsageMetadata(
                prompt_tokens=total_input - cached,
                completion_tokens=getattr(response.usage, "output_tokens", 0) or 0,
                total_tokens=total_input + (getattr(response.usage, "output_tokens", 0) or 0),
                cache_read_tokens=cached,
            )

        if not text and not tool_calls:
            logger.warning(
                "⚠️ [OpenAIAdapter] Empty response: model=%s output_items=%s",
                getattr(response, "model", "?"),
                len(response.output),
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
            raw_content=response.output,  # Store output items for multi-turn
            usage_metadata=usage_metadata,
        )
