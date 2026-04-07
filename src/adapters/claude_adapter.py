import time
import anthropic
from anthropic import types, AsyncAnthropic
from typing import List, Any, Optional, Dict, Union
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
from ..utils.groovy_to_markdown_transformer import GroovyToMarkdownConverter

class ClaudeAdapter(LLMPort):
    """
    Adapter for Anthropic Claude API.
    Implements the LLMPort port with prompt caching support.
    """

    # Feature flag for Groovy -> Markdown transformation
    USE_MARKDOWN_PROMPT = False

    # ========================================================================
    # NEW Provider Refactor Session 7: Tier-to-model mapping
    # Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
    # Purpose: Decouple agent performance tier from concrete model names
    # ========================================================================
    MODEL_TIERS = {
        PerformanceTier.ECO: "claude-haiku-4-5-20251001",
        PerformanceTier.BALANCED: "claude-sonnet-4-6",
        PerformanceTier.PERFORMANCE: "claude-opus-4-6",
    }

    # Models that support adaptive thinking (Sonnet 4.6+, Opus 4.6+).
    # Haiku models do not support thinking — silently skipped when thinking_effort is set.
    _THINKING_MODELS = ("claude-sonnet", "claude-opus")

    # Models that support dynamic filtering web search (web_search_20260209 / web_fetch_20260209).
    # Haiku 4.5 only supports the legacy web_search_20250305 (no dynamic filtering, no code_execution).
    _DYNAMIC_SEARCH_MODELS = ("claude-sonnet", "claude-opus")

    # ========================================================================
    # NEW Provider Refactor Session 7: Provider capability declaration
    # Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
    # Purpose: Feature gating and runtime validation
    # ========================================================================
    CAPABILITIES = ProviderCapabilities(
        native_tools=False,
        context_caching=True,
        vision=True,
        max_context_window=200000,
        native_grounding=True,  # web_search_20250305 built-in tool
    )

    def __init__(self, api_key: str):
        # read=120s: timeout between individual SSE chunks during streaming.
        # Anthropic stalls mid-stream on long responses (e.g. DocGenerator max_tokens=64k)
        # without ever raising — get_final_message() waits indefinitely by default (600s).
        # connect/write/pool kept tight; read kept at 120s so a stalled stream fails fast.
        self.client = AsyncAnthropic(
            api_key=api_key,
            timeout=anthropic.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0),
        )
        if self.USE_MARKDOWN_PROMPT:
            try:
                self.prompt_converter = GroovyToMarkdownConverter()
            except Exception as e:
                logger.error(f"Failed to initialize GroovyToMarkdownConverter: {e}")
                self.USE_MARKDOWN_PROMPT = False

    async def generate_content(
        self, 
        request: Optional[LLMRequest] = None,
        model_name: Optional[str] = None, 
        system_instruction: Optional[str] = None, 
        messages: Optional[List[Message]] = None, 
        tools: Optional[List[Any]] = None,
        temperature: float = 0.8,
        stream_callback: Optional[Any] = None,
        response_mime_type: Optional[str] = None,
        response_schema: Optional[Any] = None,
        cache_config: Optional[PromptCacheConfig] = None,
        automatic_function_calling: Optional[AutomaticFunctionCallingConfig] = None
    ) -> LLMResponse:
        force_tool_use = False
        use_grounding = False
        max_tokens = 16_000  # default; overridden by request.max_tokens when set
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
            if request.max_tokens:
                max_tokens = request.max_tokens
            stream_callback = None

        # Transform Groovy to Markdown if enabled
        if self.USE_MARKDOWN_PROMPT and system_instruction and "class Alek" in system_instruction:
            logger.info("[ClaudeAdapter] Transforming Groovy prompt to Markdown")
            system_instruction = self.prompt_converter.convert(system_instruction)

        if not model_name or messages is None:
            raise ValueError("model_name and messages are required for Claude generate_content")

        # ====================================================================
        # MODIFIED Provider Refactor Session 22.1: Fix tool validation
        # Plan: docs/architecture/provider_refactor/SESSION_22_1_REPORT.md
        # Issue: Claude DOES support manual tool orchestration (tools in request/response)
        #        but does NOT support automatic function calling (native tools)
        # Fix: Only block automatic_function_calling, not all tool usage
        # ====================================================================
        if automatic_function_calling and automatic_function_calling.mode:
            raise ValueError(
                "Claude does not support automatic function calling (native tools). "
                "Use manual tool orchestration or switch to Gemini."
            )
        
        # Claude uses 'system' parameter for system instructions.
        # When cache is enabled and the prompt contains a CACHE_BOUNDARY marker, split into two
        # blocks: static prefix (cached) and dynamic suffix (sent fresh every request).
        # If no boundary present, cache the entire instruction (legacy / consolidation path).
        # Guard: never apply cache_control to empty text — Anthropic returns 400.
        if cache_config and cache_config.enabled and system_instruction:
            if PROMPT_CACHE_BOUNDARY in system_instruction:
                static_part, dynamic_part = system_instruction.split(PROMPT_CACHE_BOUNDARY, 1)
                static_text = static_part.strip()
                dynamic_text = dynamic_part.strip()
                system_parts = [
                    {"type": "text", "text": static_text, "cache_control": {"type": "ephemeral"}},
                ]
                if dynamic_text:
                    system_parts.append({"type": "text", "text": dynamic_text})
            else:
                system_parts = [{"type": "text", "text": system_instruction, "cache_control": {"type": "ephemeral"}}]
        else:
            # Empty or None system_instruction → no system parts.
            # Claude API rejects empty text content blocks (400 invalid_request_error).
            system_parts = [{"type": "text", "text": system_instruction}] if system_instruction else []

        # Convert messages to Anthropic format
        claude_messages = await self._convert_messages(messages)

        # Convert tools to Anthropic format
        claude_tools = self._convert_tools(tools) if tools else []
        # Inject built-in web search when requested; must prepend (not via _convert_tools —
        # built-in tools use {type, name} format, not {name, description, input_schema})
        # Dynamic filtering (20260209) requires Sonnet/Opus — Haiku falls back to legacy tools.
        # New tool versions are GA — no web-search-2025-03-05 beta header needed.
        # code_execution_20250825 is auto-injected by the API when 20260209 tools are active.
        _use_dynamic_search = use_grounding and any(m in (model_name or "") for m in self._DYNAMIC_SEARCH_MODELS)
        if use_grounding:
            if _use_dynamic_search:
                claude_tools = [
                    {"type": "web_search_20260209", "name": "web_search"},
                    {"type": "web_fetch_20260209",  "name": "web_fetch"},
                ] + claude_tools
            else:
                # Haiku fallback: legacy tools, no dynamic filtering
                claude_tools = [
                    {"type": "web_search_20250305", "name": "web_search"},
                    {"type": "web_fetch_20250910",  "name": "web_fetch"},
                ] + claude_tools

        # Claude analog of Gemini's response_schema: inject a "respond" tool so Claude is
        # forced into structured output even when real delegation tools are also present.
        # Gemini enforces schema natively; Claude enforces it via tool_use input validation.
        # The response is intercepted below: "respond" call → serialised to JSON text →
        # returned as plain LLMResponse.text. Agents see no difference.
        # force_tool_use=True (tool_choice: any) is required: with tool_choice=auto, Haiku
        # ignores the respond tool and returns plain text, bypassing schema enforcement.
        # Note: condition does NOT require claude_tools — schema enforcement applies even
        # when the agent has no delegation tools (e.g. DocPlannerAgent).
        _schema_tool_active = False
        if response_schema and isinstance(response_schema, dict):
            respond_tool_schema = {k: v for k, v in response_schema.items() if k != "nullable"}
            claude_tools = claude_tools + [{
                "name": "respond",
                "description": "You MUST call this tool to submit your final answer. Do not output plain text.",
                "input_schema": respond_tool_schema,
            }]
            _schema_tool_active = True
            force_tool_use = True  # Must call one of: delegate_to_specialist | respond

        if stream_callback:
            # Note: Prompt caching in streaming is supported but handled slightly differently in usage stats
            try:
                async with self.client.messages.stream(
                    model=model_name,
                    max_tokens=max_tokens,
                    system=system_parts,
                    messages=claude_messages,
                    tools=claude_tools if claude_tools else [],
                    temperature=temperature,
                ) as stream:
                    full_text = ""
                    async for text in stream.text_stream:
                        full_text += text
                        await stream_callback(full_text)

                    final_message = await stream.get_final_message()
                    return self._parse_response(final_message)
            except anthropic.RateLimitError as e:
                raise LLMRateLimitError(str(e), http_status=429) from e
            except anthropic.APIStatusError as e:
                if e.status_code == 503:
                    raise LLMUnavailableError(str(e), http_status=503) from e
                raise

        # Regular request
        # Claude API rejects tool_choice=null — must be omitted entirely when not forcing a tool
        thinking_effort = request.thinking if request else None
        # web-search-2025-03-05 is needed only for legacy web_search_20250305 (Haiku fallback).
        # New 20260209 tools are GA — no extra beta header required.
        beta_headers = ["prompt-caching-2024-07-31"]
        if use_grounding and not _use_dynamic_search:
            beta_headers.append("web-search-2025-03-05")

        # Adaptive thinking + effort dispatch — model-based, provider-internal.
        # Sonnet 4.6 / Opus 4.6: thinking={"type":"adaptive"} enables adaptive thinking.
        # effort → output_config={"effort": "..."} (separate from thinking, no beta header needed).
        # Haiku: no thinking support — silently skipped regardless of thinking_effort param.
        # Claude API hard requirement: temperature must be 1.0 when thinking is enabled.
        thinking_param: Optional[dict] = None
        effort = thinking_effort or None
        if effort and any(m in model_name for m in self._THINKING_MODELS):
            thinking_param = {"type": "adaptive"}
            temperature = 1.0
            logger.info(
                f"[ClaudeAdapter] Adaptive thinking enabled, effort={effort}, model={model_name}"
            )

        create_kwargs: dict = dict(
            model=model_name,
            extra_headers={"anthropic-beta": ",".join(beta_headers)},
            max_tokens=max_tokens,
            system=system_parts,
            messages=claude_messages,
            tools=claude_tools if claude_tools else [],
            temperature=temperature,
        )
        if thinking_param:
            create_kwargs["thinking"] = thinking_param
        if effort:
            create_kwargs["output_config"] = {"effort": effort}
        if force_tool_use and claude_tools:
            # thinking is incompatible with tool_choice="any" — fall back to "auto"
            create_kwargs["tool_choice"] = {"type": "auto" if thinking_param else "any"}
        # Always stream: Anthropic SDK requires streaming for requests >10 min
        # (multi-turn tool loops like consolidation regularly exceed this threshold).
        # Grounding path uses a pause_turn loop — code_execution_20250825 (auto-injected by
        # the API when web_search_20260209/web_fetch_20260209 are active) may pause mid-turn.
        # Container ID is captured from message_delta events and passed on each continuation
        # so the API can reuse the same execution sandbox (fetch optimisation).
        if _use_dynamic_search:
            llm_response = await self._grounded_stream_loop(create_kwargs)
        else:
            try:
                async with self.client.messages.stream(**create_kwargs) as stream:
                    response = await stream.get_final_message()
            except anthropic.RateLimitError as e:
                raise LLMRateLimitError(str(e), http_status=429) from e
            except anthropic.APIStatusError as e:
                if e.status_code == 503:
                    raise LLMUnavailableError(str(e), http_status=503) from e
                raise
            llm_response = self._parse_response(response)

        # Intercept "respond" tool call: extract structured args → return as JSON text.
        # Remaining real tool_calls (if any) are discarded — Claude signalled it is done.
        if _schema_tool_active and llm_response.tool_calls:
            respond_call = next((tc for tc in llm_response.tool_calls if tc.name == "respond"), None)
            if respond_call:
                import json
                return LLMResponse(
                    text=json.dumps(respond_call.args or {}, ensure_ascii=False),
                    tool_calls=[],
                    raw_content=llm_response.raw_content,
                    usage_metadata=llm_response.usage_metadata,
                    cache_metadata=llm_response.cache_metadata,
                )

        # Force respond: when response_schema is active but the model returned end_turn
        # without calling respond (happens with grounding — web_search satisfies tool_choice:any).
        # Second call with tool_choice forced to respond; pass first response as assistant turn
        # so the model sees its own search results and reformats into JSON.
        if _schema_tool_active and not _use_dynamic_search:
            has_respond = llm_response.tool_calls and any(
                tc.name == "respond" for tc in llm_response.tool_calls
            )
            if not has_respond and response is not None:
                logger.debug("[ClaudeAdapter] respond not called — forcing second turn")
                force_messages = list(claude_messages) + [
                    {"role": "assistant", "content": response.content}
                ]
                force_kwargs = {
                    "model": model_name,
                    "max_tokens": max_tokens,
                    "system": system_parts,
                    "messages": force_messages,
                    "tools": claude_tools,
                    "tool_choice": {"type": "tool", "name": "respond"},
                    "temperature": temperature,
                }
                if beta_headers:
                    force_kwargs["betas"] = beta_headers
                try:
                    async with self.client.messages.stream(**force_kwargs) as stream:
                        force_response = await stream.get_final_message()
                except anthropic.RateLimitError as e:
                    raise LLMRateLimitError(str(e), http_status=429) from e
                except anthropic.APIStatusError as e:
                    if e.status_code == 503:
                        raise LLMUnavailableError(str(e), http_status=503) from e
                    raise

                force_parsed = self._parse_response(force_response)
                # Merge usage from both calls
                total_usage = UsageMetadata(
                    prompt_tokens=(llm_response.usage_metadata.prompt_tokens or 0) + (force_parsed.usage_metadata.prompt_tokens or 0),
                    completion_tokens=(llm_response.usage_metadata.completion_tokens or 0) + (force_parsed.usage_metadata.completion_tokens or 0),
                    total_tokens=(llm_response.usage_metadata.total_tokens or 0) + (force_parsed.usage_metadata.total_tokens or 0),
                    cache_read_tokens=(llm_response.usage_metadata.cache_read_tokens or 0) + (force_parsed.usage_metadata.cache_read_tokens or 0),
                    cache_creation_tokens=(llm_response.usage_metadata.cache_creation_tokens or 0) + (force_parsed.usage_metadata.cache_creation_tokens or 0),
                )
                respond_call = next(
                    (tc for tc in force_parsed.tool_calls if tc.name == "respond"), None
                )
                if respond_call:
                    import json
                    return LLMResponse(
                        text=json.dumps(respond_call.args or {}, ensure_ascii=False),
                        tool_calls=[],
                        raw_content=force_parsed.raw_content,
                        usage_metadata=total_usage,
                        cache_metadata=llm_response.cache_metadata,
                    )
                # Fallback: respond still not called — return force_parsed as-is
                force_parsed.usage_metadata = total_usage
                return force_parsed

        return llm_response

    async def _grounded_stream_loop(self, create_kwargs: dict) -> LLMResponse:
        """
        Streaming loop for use_grounding=True calls.

        Handles pause_turn continuations required when code_execution_20250825 (auto-injected
        by the API alongside web_search_20260209/web_fetch_20260209) is still running
        server-side. Container ID is captured from message_delta stream events and forwarded
        on each continuation so the API reuses the same execution sandbox.

        Pattern mirrors ClaudeDeepResearchRunnerAgent._call_with_overload_retry /
        _research_loop, scoped to the grounding use-case.
        """
        _MAX_PAUSE_TURNS = 20

        call_kwargs = dict(create_kwargs)
        original_messages: list = call_kwargs.pop("messages")
        messages: list = list(original_messages)
        accumulated_content: list = []
        container_id: Optional[str] = None
        pause_count = 0
        total_input = 0
        total_output = 0
        total_cache_read = 0
        total_cache_creation = 0

        while True:
            kwargs = {**call_kwargs, "messages": messages}
            if container_id:
                kwargs["container"] = container_id

            try:
                new_container: Optional[str] = None
                async with self.client.messages.stream(**kwargs) as stream:
                    async for event in stream:
                        if new_container is None and getattr(event, "type", None) == "message_delta":
                            delta = getattr(event, "delta", None)
                            delta_container = getattr(delta, "container", None) if delta else None
                            if delta_container is not None:
                                cid = getattr(delta_container, "id", None)
                                if cid:
                                    new_container = cid
                    response = await stream.get_final_message()
            except anthropic.RateLimitError as e:
                raise LLMRateLimitError(str(e), http_status=429) from e
            except anthropic.APIStatusError as e:
                if e.status_code == 503:
                    raise LLMUnavailableError(str(e), http_status=503) from e
                raise

            container_id = new_container or container_id

            if hasattr(response, "usage") and response.usage:
                total_input += response.usage.input_tokens or 0
                total_output += response.usage.output_tokens or 0
                total_cache_read += getattr(response.usage, "cache_read_input_tokens", 0)
                total_cache_creation += getattr(response.usage, "cache_creation_input_tokens", 0)

            accumulated_content.extend(response.content)

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason == "pause_turn":
                pause_count += 1
                if pause_count >= _MAX_PAUSE_TURNS:
                    logger.warning(
                        "[ClaudeAdapter] grounded loop: exceeded %d pause_turns — returning partial",
                        _MAX_PAUSE_TURNS,
                    )
                    break
                messages = list(original_messages) + [
                    {"role": "assistant", "content": accumulated_content}
                ]
                logger.debug(
                    "[ClaudeAdapter] grounded loop: pause_turn #%d container=%s",
                    pause_count, container_id,
                )
                continue

            # max_tokens or unexpected — return whatever we have
            logger.warning(
                "[ClaudeAdapter] grounded loop: unexpected stop_reason=%s", response.stop_reason
            )
            break

        text = ""
        tool_calls = []
        for block in accumulated_content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text += block.text
            elif block_type == "tool_use":
                tool_calls.append(ToolCall(
                    name=block.name,
                    args=block.input,
                    thought_signature=block.id,
                ))

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            raw_content=None,
            usage_metadata=UsageMetadata(
                prompt_tokens=total_input,
                completion_tokens=total_output,
                total_tokens=total_input + total_output,
                cache_read_tokens=total_cache_read,
                cache_creation_tokens=total_cache_creation,
            ),
        )

    def supports_caching(self) -> bool:
        return True

    # ====================================================================
    # NEW Provider Refactor Session 7: Provider capabilities accessor
    # Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
    # Purpose: Expose provider feature support for routing/feature gating
    # ====================================================================
    def get_capabilities(self) -> ProviderCapabilities:
        """
        Return Claude provider capabilities.

        Claude supports:
        - Prompt caching
        - Vision (multimodal)

        Claude does NOT support:
        - Native tools
        """
        return self.CAPABILITIES

    # ====================================================================
    # NEW Provider Refactor Session 7: Tier-based model resolution
    # Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
    # Purpose: Map performance tiers to Claude models
    # ====================================================================
    def get_model_for_tier(self, tier: PerformanceTier) -> str:
        """
        Map performance tier to Claude model name.

        Raises:
            ValueError: If tier is not supported.
        """
        if tier not in self.MODEL_TIERS:
            raise ValueError(
                f"Unsupported tier '{tier}' for Claude. "
                f"Supported: {list(self.MODEL_TIERS.keys())}"
            )
        return self.MODEL_TIERS[tier]

    async def upload_file(self, path: str, mime_type: str) -> MessagePart:
        """
        Upload file for Claude by encoding to base64.
        
        Claude doesn't have a separate file upload API like Gemini.
        Instead, files are embedded directly in messages as base64-encoded data.
        
        Returns MessagePart with file_data containing base64-encoded content.
        This maintains hexagonal architecture: file_data is provider-agnostic dict.
        """
        import base64
        import asyncio
        
        # Read file and encode to base64 (async to avoid blocking)
        def read_and_encode():
            with open(path, "rb") as f:
                return base64.standard_b64encode(f.read()).decode("utf-8")
        
        base64_data = await asyncio.to_thread(read_and_encode)
        
        logger.info(f"📎 [ClaudeAdapter] Encoded file to base64: {mime_type}, {len(base64_data)} chars")
        
        return MessagePart(file_data={
            "base64": base64_data,
            "mime_type": mime_type
        })

    def _get_claude_content_type(self, mime_type: str) -> Optional[str]:
        """
        Map MIME type to Claude content type.

        Claude API supports:
        - "image"    for images (image/jpeg, image/png, image/gif, image/webp)
        - "document" ONLY for application/pdf

        Returns None for unsupported types — callers must skip those parts.
        """
        if mime_type.startswith("image/"):
            return "image"
        elif mime_type == "application/pdf":
            return "document"
        else:
            return None  # Not supported by Claude API

    def _find_tool_use_id(self, messages: List[Message], tool_name: str, current_idx: int, used_ids: set = None) -> str:
        """
        Find the tool_use ID for a given tool name from previous messages.

        Claude requires strict matching: tool_result must reference a tool_use ID
        from a previous assistant message.

        Args:
            messages: Full message history
            tool_name: Name of the tool to find
            current_idx: Current message index (search backwards from here)
            used_ids: Set of already-matched IDs to skip (for parallel same-tool calls)

        Returns:
            tool_use ID if found, otherwise "unknown"
        """
        if used_ids is None:
            used_ids = set()

        # Search backwards through messages
        for i in range(current_idx - 1, -1, -1):
            prev_msg = messages[i]

            # Only look in model/assistant messages
            if prev_msg.role != "model":
                continue

            # Check raw_content first (preserves original Claude structure)
            if prev_msg.raw_content and hasattr(prev_msg.raw_content, 'content'):
                for content_block in prev_msg.raw_content.content:
                    if hasattr(content_block, 'type') and content_block.type == "tool_use":
                        if hasattr(content_block, 'name') and content_block.name == tool_name:
                            if content_block.id not in used_ids:
                                return content_block.id

            # Fallback: check parts
            for part in prev_msg.parts:
                if part.tool_call and part.tool_call.name == tool_name:
                    if part.tool_call.thought_signature and part.tool_call.thought_signature not in used_ids:
                        return part.tool_call.thought_signature

        logger.warning(
            f"[ClaudeAdapter] Could not find tool_use ID for tool '{tool_name}'. "
            f"This may cause API errors."
        )
        return "unknown"

    async def _convert_messages(self, messages: List[Message]) -> List[Dict[str, Any]]:
        claude_messages = []
        for idx, msg in enumerate(messages):
            # ====================================================================
            # MODIFIED Provider Refactor Session 22.1: Preserve provider-specific IDs
            # Issue: Round-trip loses Claude tool_use IDs when using raw_content
            # Fix: Use raw_content directly if available (preserves original structure)
            # ====================================================================
            logger.debug(
                f"[ClaudeAdapter] Converting message {idx}: role={msg.role}, "
                f"parts_count={len(msg.parts)}, has_raw_content={msg.raw_content is not None}"
            )

            if msg.raw_content and hasattr(msg.raw_content, 'content'):
                # Use raw Claude message content directly (preserves IDs)
                role = "assistant" if msg.role == "model" else "user"
                content_list = list(msg.raw_content.content)
                logger.debug(f"[ClaudeAdapter] Using raw_content: {len(content_list)} parts")
                claude_messages.append({
                    "role": role,
                    "content": content_list
                })
                continue

            # Convert from domain objects
            content_parts = []
            used_tool_ids: set = set()  # Track matched tool_use IDs within this message
            for part_idx, p in enumerate(msg.parts):
                if p.text:
                    content_parts.append({"type": "text", "text": p.text})
                    logger.debug(f"[ClaudeAdapter]   Part {part_idx}: text ({len(p.text)} chars)")
                elif p.tool_call:
                    # Use thought_signature as ID (preserves Claude's original ID)
                    tool_id = p.tool_call.thought_signature or f"call_{int(time.time()*1000)}"
                    content_parts.append({
                        "type": "tool_use",
                        "id": tool_id,
                        "name": p.tool_call.name,
                        "input": p.tool_call.args
                    })
                    logger.debug(f"[ClaudeAdapter]   Part {part_idx}: tool_call name={p.tool_call.name} id={tool_id}")
                elif p.tool_response:
                    # Find matching tool_use ID from previous messages.
                    # Pass used_ids to avoid duplicate IDs when the same tool was called
                    # multiple times in parallel (e.g. 2x search_memory).
                    tool_name = p.tool_response.get("name", "")
                    tool_id = self._find_tool_use_id(messages, tool_name, idx, used_tool_ids)
                    used_tool_ids.add(tool_id)
                    content_parts.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": str(p.tool_response["response"])
                    })
                    logger.debug(f"[ClaudeAdapter]   Part {part_idx}: tool_response name={tool_name} id={tool_id}")
                elif p.file_data:
                    # 🆕 HEXAGONAL: Adapter handles provider-specific file preparation
                    if "base64" in p.file_data:
                        # Already base64 encoded (from history)
                        mime_type = p.file_data["mime_type"]
                        content_type = self._get_claude_content_type(mime_type)
                        if content_type is None:
                            logger.warning(f"[ClaudeAdapter]   Part {part_idx}: skipping unsupported MIME type '{mime_type}' (Claude only accepts image/* and application/pdf)")
                        else:
                            content_parts.append({
                                "type": content_type,
                                "source": {
                                    "type": "base64",
                                    "media_type": mime_type,
                                    "data": p.file_data["base64"]
                                }
                            })
                            logger.debug(f"[ClaudeAdapter]   Part {part_idx}: {content_type} from history ({mime_type}, {len(p.file_data['base64'])} chars)")
                    elif "path" in p.file_data:
                        # New file: read and encode to base64
                        try:
                            import aiofiles
                            import base64

                            async with aiofiles.open(p.file_data["path"], "rb") as f:
                                file_content = await f.read()

                            base64_data = base64.b64encode(file_content).decode("utf-8")
                            mime_type = p.file_data["mime_type"]
                            content_type = self._get_claude_content_type(mime_type)
                            if content_type is None:
                                logger.warning(f"[ClaudeAdapter]   Part {part_idx}: skipping unsupported MIME type '{mime_type}' (Claude only accepts image/* and application/pdf)")
                            else:
                                content_parts.append({
                                    "type": content_type,
                                    "source": {
                                        "type": "base64",
                                        "media_type": mime_type,
                                        "data": base64_data
                                    }
                                })
                                logger.info(f"📎 [ClaudeAdapter]   Part {part_idx}: {content_type} encoded ({mime_type}, {len(base64_data)} chars)")
                        except Exception as e:
                            logger.error(f"❌ [ClaudeAdapter]   Part {part_idx}: Failed to encode file: {e}")
                    elif "ref" in p.file_data:
                        # GCS reference — no content to display, text label already in message
                        logger.debug(f"[ClaudeAdapter]   Part {part_idx}: file ref '{p.file_data['ref']}' (no binary content)")
                    else:
                        logger.warning(f"[ClaudeAdapter]   Part {part_idx}: Unsupported file_data format: {list(p.file_data.keys())}")

            # Claude roles are 'user' and 'assistant'
            role = "assistant" if msg.role == "model" else "user"
            logger.debug(f"[ClaudeAdapter] Created message: role={role}, content_parts={len(content_parts)}")
            claude_messages.append({"role": role, "content": content_parts})
        
        return claude_messages

    def _convert_tools(self, tools: List[Any]) -> List[Dict[str, Any]]:
        claude_tools = []
        for tool in tools:
            if isinstance(tool, dict):
                claude_tools.append({
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("parameters", {"type": "object", "properties": {}})
                })
        return claude_tools

    def _parse_response(self, response: types.Message) -> LLMResponse:
        text = ""
        tool_calls = []

        for content in response.content:
            if content.type == "thinking":
                # Extended thinking block — internal reasoning trace, not part of the output.
                # Skipped intentionally; the text blocks that follow contain the actual response.
                continue
            elif content.type == "text":
                text += content.text
            elif content.type == "tool_use":
                tool_calls.append(ToolCall(
                    name=content.name,
                    args=content.input,
                    thought_signature=content.id # Use ID as signature for Claude
                ))

        # Parse usage metadata — cache tokens included directly
        usage = response.usage
        usage_metadata = UsageMetadata(
            prompt_tokens=usage.input_tokens,
            completion_tokens=usage.output_tokens,
            total_tokens=usage.input_tokens + usage.output_tokens,
            cache_read_tokens=getattr(usage, 'cache_read_input_tokens', 0),
            cache_creation_tokens=getattr(usage, 'cache_creation_input_tokens', 0),
        )

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            raw_content=response,
            usage_metadata=usage_metadata,
        )
