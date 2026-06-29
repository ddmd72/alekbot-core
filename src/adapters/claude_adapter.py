import asyncio
import time
import anthropic
from anthropic import types, AsyncAnthropic
from typing import List, Any, Optional, Dict
from ..ports.llm_port import (
    LLMPort,
    LLMResponse,
    ToolCall,
    Message,
    MessagePart,
    UsageMetadata,
    ProviderCapabilities,
    LLMRequest,
    PROMPT_CACHE_BOUNDARY,
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


def _make_schema_strict(schema: dict) -> dict:
    """
    Recursively strips Anthropic-unsupported keys and injects required constraints.

    - Removes "nullable" at every nesting level (Claude GA rejects it with 400).
      Semantic side-effect: Gemini with nullable=True allows the model to return null for
      that field (e.g. "rich_content": null); Claude without nullable must return an object
      (e.g. "rich_content": {}). Both are handled correctly by the agent parsers.
    - Injects "additionalProperties": False on object types (required by GA strict mode).
    """
    if not isinstance(schema, dict):
        return schema

    strict_schema = {k: v for k, v in schema.items() if k != "nullable"}
    if strict_schema.get("type") == "object" and "additionalProperties" not in strict_schema:
        strict_schema["additionalProperties"] = False
        
    if "properties" in strict_schema:
        strict_schema["properties"] = {
            k: _make_schema_strict(v) for k, v in strict_schema["properties"].items()
        }
        
    if "items" in strict_schema:
        if isinstance(strict_schema["items"], dict):
            strict_schema["items"] = _make_schema_strict(strict_schema["items"])
        elif isinstance(strict_schema["items"], list):
            strict_schema["items"] = [_make_schema_strict(i) for i in strict_schema["items"]]
            
    for key in ["anyOf", "allOf", "oneOf"]:
        if key in strict_schema:
            strict_schema[key] = [_make_schema_strict(i) for i in strict_schema[key]]
            
    return strict_schema

class ClaudeAdapter(LLMPort):
    """
    Adapter for Anthropic Claude API.
    Implements the LLMPort port with prompt caching support.
    """

    # ========================================================================
    # NEW Provider Refactor Session 7: Tier-to-model mapping
    # Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
    # Purpose: Decouple agent performance tier from concrete model names
    # ========================================================================
    MODEL_TIERS = {
        PerformanceTier.ECO:         "claude-haiku-4-5-20251001",
        PerformanceTier.BALANCED:    "claude-haiku-4-5-20251001",
        PerformanceTier.PERFORMANCE: "claude-sonnet-4-6",
        PerformanceTier.ULTRA:       "claude-opus-4-8",
        PerformanceTier.TIER1:       "claude-haiku-4-5-20251001",
        PerformanceTier.TIER2:       "claude-haiku-4-5-20251001",
        PerformanceTier.TIER3:       "claude-haiku-4-5-20251001",
    }

    # Models that support adaptive thinking (Sonnet 4.6+, Opus 4.6+).
    # Haiku models do not support thinking — silently skipped when thinking_effort is set.
    # Upgraded 2026-05-30: ULTRA → opus-4-8 (same price as 4-7, better benchmarks).
    # See decisions/claude_ultra_tier_to_opus_4_8_plus_dr_gate_unification.md.
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
        max_context_window=1000000,  # model-dependent: sonnet-4-6 1M (verified via API + empirical 224K req, no beta header); haiku-4-5/opus 200K. Declarative only — not consumed for truncation.
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

    async def generate_content(self, request: LLMRequest) -> LLMResponse:
        model_name = request.model_name
        system_instruction = request.system_instruction
        messages = request.messages
        tools = request.tools
        temperature = request.temperature
        response_schema = request.response_schema
        cache_config = request.cache_config
        automatic_function_calling = request.automatic_function_calling
        force_tool_use = request.force_tool_use
        use_grounding = request.use_grounding
        max_tokens = request.max_tokens if request.max_tokens else 16_000

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
        cache_last_message = bool(
            cache_config and cache_config.enabled and cache_config.cache_last_message
        )
        claude_messages = await self._convert_messages(
            messages, cache_last_message=cache_last_message
        )

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

        # Regular request
        # Claude API rejects tool_choice=null — must be omitted entirely when not forcing a tool
        thinking_effort = request.thinking
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
        # Only attach output_config.effort when the model supports it.
        # Per Anthropic capabilities (verified 2026-04-25 against models.retrieve):
        #   Sonnet 4.6 / Opus 4.7 → effort.supported = True
        #   Haiku 4.5            → effort.supported = False  → API returns 400
        # We gate by the same _THINKING_MODELS substring used for adaptive thinking,
        # since the two go together: only Sonnet/Opus accept both. Haiku silently
        # drops effort here instead of crashing the request.
        if effort and any(m in model_name for m in self._THINKING_MODELS):
            create_kwargs["output_config"] = {"effort": effort}
            
        if response_schema and isinstance(response_schema, dict):
            schema = _make_schema_strict(response_schema)
            output_config = create_kwargs.get("output_config", {})
            output_config["format"] = {"type": "json_schema", "schema": schema}
            create_kwargs["output_config"] = output_config
            
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
            request_timeout = request.timeout

            async def _do_stream() -> Any:
                async with self.client.messages.stream(**create_kwargs) as stream:
                    return await stream.get_final_message()

            try:
                if request_timeout:
                    response = await asyncio.wait_for(_do_stream(), timeout=request_timeout)
                else:
                    response = await _do_stream()
            except asyncio.TimeoutError as e:
                # Wall-clock budget from request.timeout exhausted.
                raise LLMTimeoutError(f"request timeout after {request_timeout}s") from e
            except anthropic.APITimeoutError as e:
                # SDK-level timeout (default httpx read=120s when request.timeout is None).
                raise LLMTimeoutError(str(e)) from e
            except anthropic.RateLimitError as e:
                raise LLMRateLimitError(str(e), http_status=429) from e
            except anthropic.APIConnectionError as e:
                raise LLMNetworkError(str(e)) from e
            except anthropic.APIStatusError as e:
                status = getattr(e, "status_code", None)
                # overloaded_error (HTTP 529) frequently arrives mid-stream as an SSE
                # error event — the connection already returned 200, so the SDK builds
                # an APIStatusError with status_code unset. Detect it by type (mirrors
                # ClaudeDeepResearchRunnerAgent._call_with_overload_retry) so it still
                # classifies as a transient server error and triggers provider failover
                # via FAILOVER_TRIGGER_TYPES, instead of bubbling raw and degrading
                # Smart → Quick.
                if status == 529 or "overloaded_error" in str(e):
                    raise LLMServerError(str(e), http_status=529) from e
                if status == 503:
                    raise LLMUnavailableError(str(e), http_status=503) from e
                if isinstance(status, int) and 500 <= status < 600:
                    raise LLMServerError(str(e), http_status=status) from e
                # Grammar compilation timeout: a 400 by HTTP status, but a
                # transient SERVER-side fault — Anthropic's constrained-decoding
                # grammar compiler timed out building the response_schema, not a
                # malformed request. Classify as a failover trigger (like the
                # overloaded_error case above) so Smart is served by another
                # provider instead of failing terminally. A chronic recurrence
                # accumulates provider failures and trips the breaker, which
                # alerts — so we recover transients silently and escalate only
                # the persistent case, rather than masking it.
                if status == 400 and "Grammar compilation timed out" in str(e):
                    raise LLMServerError(str(e), http_status=status) from e
                # 4xx (non-429, e.g. 400 credit-balance / bad request) → deterministic
                # client error. Not a failover trigger; surfaces immediately + alerts.
                if isinstance(status, int) and 400 <= status < 500:
                    raise LLMClientError(str(e), http_status=status) from e
                raise
            llm_response = self._parse_response(response)

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
                status = getattr(e, "status_code", None)
                # Same mid-stream overloaded_error / 5xx classification as the
                # non-grounded path above — keep the two branches symmetric so a
                # transient server error in the grounding loop also fails over.
                if status == 529 or "overloaded_error" in str(e):
                    raise LLMServerError(str(e), http_status=529) from e
                if status == 503:
                    raise LLMUnavailableError(str(e), http_status=503) from e
                if isinstance(status, int) and 500 <= status < 600:
                    raise LLMServerError(str(e), http_status=status) from e
                if isinstance(status, int) and 400 <= status < 500:
                    raise LLMClientError(str(e), http_status=status) from e
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

        # TEMP tool-id tracing (2026-06-29): grounded path nulls raw_content — confirm the
        # tool_use ids survive the pause_turn accumulation here.
        if tool_calls:
            _blocks = [(getattr(b, "name", None), getattr(b, "id", None))
                       for b in accumulated_content if getattr(b, "type", None) == "tool_use"]
            logger.info("🔎 [_grounded_stream_loop] tool_use blocks (name,id)=%s", _blocks)

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

        # TEMP tracing (2026-06-29): dump what the backward search actually saw in every
        # prior model message, so we know WHY no id matched (name mismatch? all sigs None?).
        _seen = []
        for i in range(current_idx - 1, -1, -1):
            pm = messages[i]
            if pm.role != "model":
                continue
            if pm.raw_content and hasattr(pm.raw_content, "content"):
                _seen.append((i, "raw", [(getattr(b, "name", None), getattr(b, "id", None))
                              for b in pm.raw_content.content
                              if getattr(b, "type", None) == "tool_use"]))
            else:
                _seen.append((i, "parts", [(p.tool_call.name, p.tool_call.thought_signature)
                              for p in pm.parts if p.tool_call]))
        logger.warning(
            "🔎 [ClaudeAdapter] Could not find tool_use ID for tool '%s' (used_ids=%s) → 'unknown'. "
            "Prior model messages' tool_use (idx,source,[(name,id)])=%s",
            tool_name, sorted(i for i in used_ids if i and i != "unknown"), _seen,
        )
        return "unknown"

    async def _convert_messages(
        self,
        messages: List[Message],
        cache_last_message: bool = False,
    ) -> List[Dict[str, Any]]:
        claude_messages = []
        for idx, msg in enumerate(messages):
            # TEMP tool-id tracing (2026-06-29): for any message carrying tool parts, dump
            # role / raw_content presence / each tool_call's thought_signature / each
            # tool_response's carried tool_use_id. This shows the EXACT message where the id
            # is missing in the history that the engine assembled.
            _tc_sigs = [p.tool_call.thought_signature for p in msg.parts if p.tool_call]
            _tr_ids = [(p.tool_response.get("name"), p.tool_response.get("tool_use_id"))
                       for p in msg.parts if p.tool_response]
            if _tc_sigs or _tr_ids:
                logger.info(
                    "🔎 [_convert_messages] msg %d role=%s raw_content=%s tool_call_sigs=%s tool_response=%s",
                    idx, msg.role, msg.raw_content is not None, _tc_sigs, _tr_ids,
                )
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
                    if not p.tool_call.thought_signature:
                        # TEMP tracing (2026-06-29): a tool_call reached serialization with NO id —
                        # this is the orphan trigger. The matching tool_result will resolve to a
                        # DIFFERENT value, so log loudly where it happened.
                        logger.warning(
                            "🔎 [_convert_messages] msg %d tool_call '%s' has NO thought_signature "
                            "→ minted synthetic id=%s (orphan risk)",
                            idx, p.tool_call.name, tool_id,
                        )
                    content_parts.append({
                        "type": "tool_use",
                        "id": tool_id,
                        "name": p.tool_call.name,
                        "input": p.tool_call.args
                    })
                    logger.debug(f"[ClaudeAdapter]   Part {part_idx}: tool_call name={p.tool_call.name} id={tool_id}")
                elif p.tool_response:
                    # Prefer the tool_use id carried explicitly on the tool_response
                    # (set by build_tool_turn from the originating ToolCall). This is
                    # the exact id the provider assigned to the matching tool_use block,
                    # so it is order-independent and immune to same-name collisions across
                    # turns / parallel fan-out. Fall back to the legacy backward name
                    # search only for history that predates the explicit id (e.g. tool
                    # turns built before this change, or providers without a call id).
                    tool_name = p.tool_response.get("name", "")
                    tool_id = p.tool_response.get("tool_use_id")
                    if not tool_id:
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

        # Multi-turn loop caching: rolling sliding window of cache_control
        # breakpoints on user messages.
        #
        # We place TWO breakpoints (BP_new + BP_prev) within the messages
        # array on every multi-turn request:
        #   BP_new  → last content block of the LAST user message
        #             (write a fresh cache entry at the new frontier)
        #   BP_prev → last content block of the previous user message
        #             (re-affirms the prior turn's cache write — keeps the
        #             lookback chain unbroken when a single turn produces
        #             >20 content blocks of tool_use/tool_result, which can
        #             happen on parallel batch tool calls)
        #
        # Why both markers (not just BP_new): Anthropic's lookback window is
        # 20 blocks. A single consolidation turn with 12+ parallel tool calls
        # easily produces 24+ blocks (tool_use × N + tool_result × N), pushing
        # the previous user message past the lookback horizon. With BP_prev
        # explicitly set on the same position the prior turn wrote, the new
        # request anchors directly at the prior cache write — distance 0 in
        # the lookback window, guaranteed HIT.
        #
        # Why on USER messages (not assistant): tool_result blocks live in
        # user messages, and the heaviest content (long search outputs) is
        # there. Caching at user-message boundaries gives the largest reusable
        # prefix per write.
        #
        # Total breakpoints: 1 on system static + up to 2 on messages = max 3.
        # Anthropic limit is 4 — leaves a slot for future use (e.g. dynamic
        # system block, tools-list cache).
        if cache_last_message and claude_messages:
            user_indices = [
                i for i, m in enumerate(claude_messages)
                if m.get("role") == "user"
            ]

            def _mark_last_block(msg_idx: int, label: str) -> None:
                content = claude_messages[msg_idx].get("content")
                if not isinstance(content, list) or not content:
                    return
                last_block = content[-1]
                if not isinstance(last_block, dict):
                    return
                last_block["cache_control"] = {"type": "ephemeral"}
                logger.debug(
                    "💾 [ClaudeAdapter] cache_control on %s user message "
                    "(idx=%s, block_type=%s)",
                    label, msg_idx, last_block.get("type"),
                )

            if user_indices:
                # BP_new on the last user message
                _mark_last_block(user_indices[-1], "last")
                # BP_prev on the previous user message (sliding window)
                if len(user_indices) >= 2:
                    _mark_last_block(user_indices[-2], "prev")

        self._diagnose_tool_pairing(claude_messages)
        return claude_messages

    @staticmethod
    def _block_field(block: Any, field: str) -> Any:
        """Read a field from a content block that may be a dict (reconstructed)
        or an SDK object (passed through verbatim from raw_content)."""
        if isinstance(block, dict):
            return block.get(field)
        return getattr(block, field, None)

    def _diagnose_tool_pairing(self, claude_messages: List[Dict[str, Any]]) -> None:
        """Assert Anthropic's tool-pairing invariant and log loudly on violation.

        Anthropic rejects a request (HTTP 400 invalid_request_error) when a
        tool_result block's tool_use_id does not correspond to a tool_use block
        in the IMMEDIATELY preceding message. That 400 carries only an id and an
        index — useless for diagnosis after the fact. This pass detects the same
        condition before the call and logs the offending id alongside the ids
        that WERE available, so a recurrence (this fired once in 30 days under
        heavy parallel fan-out) is debuggable from one log line instead of a bare
        provider error. Non-fatal by design: it adds visibility, not a new failure
        mode — the request is still sent and the provider remains the source of truth.
        """
        for idx, msg in enumerate(claude_messages):
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            result_ids = [
                self._block_field(b, "tool_use_id")
                for b in content
                if self._block_field(b, "type") == "tool_result"
            ]
            if not result_ids:
                continue
            prev_content = claude_messages[idx - 1].get("content") if idx > 0 else None
            available_ids = set()
            if isinstance(prev_content, list):
                available_ids = {
                    self._block_field(b, "id")
                    for b in prev_content
                    if self._block_field(b, "type") == "tool_use"
                }
            orphans = [rid for rid in result_ids if rid not in available_ids]
            if orphans:
                logger.error(
                    "❌ [ClaudeAdapter] tool_result/tool_use mismatch at message %s: "
                    "orphaned tool_use_id(s)=%s not in previous message's tool_use ids=%s "
                    "(this WILL be rejected by Anthropic as invalid_request_error). "
                    "result_ids=%s",
                    idx, orphans, sorted(i for i in available_ids if i), result_ids,
                )

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

        # TEMP tool-id tracing (2026-06-29): dump the raw SDK tool_use block ids so we
        # can see whether the id is present AT PARSE TIME (the SDK boundary) or lost later.
        if tool_calls:
            _blocks = [(getattr(b, "name", None), getattr(b, "id", None))
                       for b in response.content if getattr(b, "type", None) == "tool_use"]
            logger.info("🔎 [_parse_response] SDK tool_use blocks (name,id)=%s  stop_reason=%s",
                        _blocks, getattr(response, "stop_reason", None))

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
