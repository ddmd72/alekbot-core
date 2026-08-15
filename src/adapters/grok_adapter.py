"""
Grok Adapter (xAI)
==================

Adapter for xAI Grok API using the OpenAI-compatible SDK against xAI's
**Responses API** (`/v1/responses`). Implements the LLMPort port.

Why Responses and not Chat Completions (migrated 2026-08-14):
- xAI retired `search_parameters` on chat/completions — it now returns HTTP 410
  "Live search is deprecated. Please switch to the Agent Tools API", and a
  `{"type": "web_search"}` tool there is rejected with HTTP 422 "unknown variant".
  Server-side search exists ONLY on `/v1/responses`.
- It is the same boundary `OpenAIAdapter` already speaks, so both adapters share
  one request/response dialect instead of two.

Verified live against xAI on 2026-08-14: text, `web_search`, `x_search`, custom
function tools with `tool_choice="required"`, `instructions` as system prompt,
`text.format` JSON mode, `temperature`, and `reasoning.effort` all work here.

Timeouts (2026-08-15): the client ceiling is 300s, matching OpenAIAdapter, and an
explicit `LLMRequest.timeout` is forwarded to the SDK as well as bounding total
wall-time. The previous 60s ceiling silently capped both.
"""

from typing import List, Any, Optional, Set
import asyncio
import base64
import hashlib
import json
import openai
from openai import AsyncOpenAI
from ..domain.llm import PROMPT_CACHE_BOUNDARY, USER_TURN_SYSTEM_ANCHOR
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
    Uses the OpenAI-compatible SDK against xAI's Responses API.
    """

    # ========================================================================
    # Tier-to-model mapping
    # Purpose: Decouple agent performance tier from concrete model names
    # ========================================================================
    # The grok-4-1-fast-* IDs used until 2026-08-14 are retired: they are absent from
    # GET /v1/models yet still answer HTTP 200 — xAI silently serves grok-4.3 and says
    # so in the response body. Requesting a retired ID therefore does not fail, it
    # mis-bills (we priced $0.20/$0.50 while grok-4.3 ran at $1.25/$2.50). Keep this
    # map on IDs that GET /v1/models actually lists.
    # Note: xAI retired the sub-$1 tier — the cheapest live model is now $1.25/$2.50.
    MODEL_TIERS = {
        # Note: BOTH live models reason by default — probed 2026-08-15, a bare request
        # with no `reasoning` block still returns a `reasoning` item (grok-4.3: 83
        # reasoning tokens, grok-4.6: 66). An earlier comment here claimed grok-4.3 did
        # not; it does. Only grok-4.3 can be told to stop (`effort="none"`).
        PerformanceTier.ECO:         "grok-4.3",   # cheapest live
        PerformanceTier.BALANCED:    "grok-4.3",
        PerformanceTier.PERFORMANCE: "grok-4.6",   # flagship, reasons by default
        PerformanceTier.ULTRA:       "grok-4.6",   # no separate ultra model yet
        PerformanceTier.TIER1:       "grok-4.3",
        PerformanceTier.TIER2:       "grok-4.3",
        PerformanceTier.TIER3:       "grok-4.3",
    }

    # ========================================================================
    # Provider capability declaration
    # Purpose: Feature gating and runtime validation
    # ========================================================================
    CAPABILITIES = ProviderCapabilities(
        native_tools=True,  # Grok supports function calling
        # xAI DOES cache prompts, but automatically and with no API surface to control
        # it (usage.input_tokens_details.cached_tokens comes back non-zero on its own).
        # This flag gates PromptCacheStrategy, which exists to place explicit
        # Anthropic-style cache_control breakpoints — there is nothing here to place.
        # False means "no controllable caching", not "no caching"; the cached tokens
        # are still measured and priced in _parse_response / billing.py.
        context_caching=False,
        vision=True,  # verified: input_image accepted on grok-4.6 and grok-4.3 (min 8x8 px)
        # Per-model: grok-4.6 = 500k, grok-4.3 = 1M. Conservative value so this never
        # over-promises for the flagship. Declarative only — nothing reads it today.
        max_context_window=500000,
        supports_system_prompt=True,
        supports_json_mode=True,       # verified: text.format={"type":"json_object"}
        native_grounding=True,         # verified: {"type": "web_search"} on /v1/responses
    )

    # Name of the synthesized terminal tool. Must match the `terminal_tool` value
    # SmartResponseAgent passes to DelegationEngine — adapters cannot import agents,
    # so the string is duplicated deliberately.
    TERMINAL_TOOL_NAME = "deliver_response"

    # Accepted `reasoning.effort` values, probed live 2026-08-15 (one call per cell,
    # both models): low / medium / high are accepted everywhere. "none" is model-
    # specific — grok-4.3 accepts it (0 reasoning tokens), grok-4.6 returns
    # `400 invalid-argument: This model does not support reasoning_effort value none`.
    # Anything else is normalised to "medium", mirroring OpenAIAdapter — the value
    # originates in user config (`agent_thinking`, `complexity_settings_overrides`),
    # so an unexpected string must not become an HTTP 400 mid-conversation.
    _EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high"}
    _EFFORT_NONE_MODELS = ("grok-4.3",)

    # Reinforcement block for the `developer` channel, mirroring OpenAIAdapter. Gated on
    # the same `humor_engine` marker: it names the personality sections of the assembled
    # prompt and re-asserts them from the highest-priority role the API offers. Kept
    # verbatim rather than paraphrased — it is the text that was validated on OpenAI
    # (memory: solution_openai_personality_grounding, 2026-08-02).
    _PERSONALITY_ANCHOR = """PERSONALITY ANCHOR — High Priority

You MUST follow these sections from the system prompt above:

1. **voice** section → Tone, brevity, anti-patterns (aphoristic, paradoxical, sharp)
2. **humor_engine** section → ALWAYS_ACTIVE (Ranevskaya-filtered: paradox, self-deprecation, dark humor)
3. **identity** section → Intellectual equal and co-conspirator, not helper
4. **engagement** section → Analyze, don't echo. Challenge vague plans playfully.
5. **few_shot_examples** section → These are your EXACT voice patterns. Internalize them.
6. **standing_directives** section → Apply unconditionally every response.

If output reads as helpful/safe/generic → you have FAILED.
Personalization > Safety. Use the voice."""

    def __init__(self, api_key: str):
        """
        Initialize Grok adapter with xAI API key.

        Args:
            api_key: xAI API key (format: xai-...)
        """
        # .strip() is load-bearing, not cosmetic. Secret Manager stores the value
        # verbatim, and a trailing newline in the secret makes httpx reject the
        # Authorization header (LocalProtocolError) — which the OpenAI SDK surfaces
        # as APIConnectionError and this adapter then maps to LLMNetworkError. The
        # result reads as "xAI blocks Google Cloud" while the request never leaves
        # the container. That misdiagnosis cost this integration months; see
        # decisions/grok_revival_2026_08.md.
        self.api_key = api_key.strip()
        self.base_url = "https://api.x.ai/v1"

        self.client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            # 300s, matching OpenAIAdapter. It was 60s until 2026-08-15, which was
            # harmless only while the adapter was dead: with live grok-4.6 at
            # reasoning effort medium, a Smart delegation turn measured 42s / 25s /
            # 49s at 20-73k tokens and crossed 60s at ~100k. Each miss cost 3x60s of
            # SDK retries and killed the daily briefing three runs in a row.
            timeout=300.0,
            max_retries=2,  # OpenAI SDK default is 2
        )

        logger.info(
            f"✅ [GrokAdapter] Initialized: base_url={self.base_url}, timeout=300s"
        )

    async def generate_content(self, request: LLMRequest) -> LLMResponse:
        """Generate content using the xAI Responses API."""
        model_name = request.model_name
        system_instruction = request.system_instruction
        messages = request.messages
        tools = request.tools
        temperature = request.temperature
        response_mime_type = request.response_mime_type
        response_schema = request.response_schema
        cache_config = request.cache_config
        force_tool_use = request.force_tool_use
        use_grounding = request.use_grounding
        thinking = request.thinking

        # `cache_config` asks for EXPLICIT Anthropic-style cache breakpoints, which xAI
        # exposes no control for (CAPABILITIES.context_caching=False). It used to raise
        # ValueError here — a hard failure of the whole agent execution over a hint that
        # this adapter can simply ignore, and self-contradictory besides, since the same
        # method sends `prompt_cache_key` twenty lines below. xAI caches automatically;
        # an enabled cache_config is already satisfied, just not the way it asked.
        if cache_config and cache_config.enabled:
            logger.debug(
                "[GrokAdapter] cache_config ignored — xAI caches automatically and "
                "exposes no breakpoint control (model=%s)", request.model_name,
            )

        # Strip the PROMPT_CACHE_BOUNDARY marker — it is an Anthropic-only cut point and
        # would otherwise reach the model as a literal "<!-- CACHE_BOUNDARY -->" string.
        # Capture the static prefix first: it is the stable cacheable head, used to derive
        # a `prompt_cache_key`. Falls back to the full instruction when unmarked.
        cache_key_seed = system_instruction
        if system_instruction and PROMPT_CACHE_BOUNDARY in system_instruction:
            cache_key_seed = system_instruction.split(PROMPT_CACHE_BOUNDARY, 1)[0]
            system_instruction = system_instruction.replace(PROMPT_CACHE_BOUNDARY, "\n")

        # Lift USER_TURN_SYSTEM_ANCHOR out of the last user turn into a `developer`
        # item, mirroring OpenAIAdapter. Measured on xAI 2026-08-15 (6 runs per cell,
        # both orderings, conflicting output-language instructions): the precedence is
        # developer > instructions > user — the same as OpenAI. The anchor otherwise
        # sits in the WEAKEST channel available.
        messages_for_conversion, extracted_anchor = self._extract_turn_anchor(messages)

        # Convert domain messages to Responses API input items
        input_items = self._convert_input(messages_for_conversion)

        # Combine every high-priority override into ONE developer item, mirroring
        # OpenAIAdapter: the turn anchor plus the personality reinforcement block when
        # the assembled prompt carries personality sections.
        developer_parts: List[str] = []
        if extracted_anchor:
            developer_parts.append(extracted_anchor)
        if system_instruction and "humor_engine" in system_instruction:
            developer_parts.append(self._PERSONALITY_ANCHOR)
        if developer_parts:
            input_items.insert(
                0, {"role": "developer", "content": "\n\n".join(developer_parts)}
            )

        # Convert tools to Responses API format (internally-tagged, no nested wrapper)
        api_tools = self._convert_tools(tools) if tools else []

        # Inject xAI's server-side search when grounding is requested. This is the
        # Agent Tools API surface — the only place web search still exists.
        if use_grounding:
            api_tools = [{"type": "web_search"}] + api_tools

        # Build text format for JSON mode, mirroring OpenAIAdapter:
        #   grounding      → no format (search and structured output conflict).
        #   schema + tools → terminal tool instead of a text format (see below).
        #   dict schema    → json_schema (strict=False): the schema IS sent, so the model
        #                    populates every described field. strict=False because our
        #                    variant schemas can't satisfy strict=True (which demands
        #                    additionalProperties:false + every field required).
        #   mime_type only → json_object (valid JSON, no schema).
        # Verified against xAI 2026-08-14: text.format json_schema is accepted and honoured.
        text_format = None
        synthesized_terminal = False
        if use_grounding:
            pass
        elif isinstance(response_schema, dict) and api_tools:
            # Constrained JSON output and function calling COMPETE on Grok. Measured
            # 2026-08-15 on the real orchestrator prompt: 15/15 turns delegated with no
            # text format, 9/15 with one (json_object degrades identically, so this is
            # not specific to json_schema). The model fills the answer field with a
            # promise — "I'll compute the route and reschedule" — and calls nothing.
            #
            # So when structure AND tools are both required, the answer moves off the
            # text channel and onto a tool: the schema becomes the parameters of a
            # synthesized `deliver_response`. DelegationEngine already terminates on it
            # (Smart passes terminal_tool="deliver_response") and reads its args, so
            # nothing outside this adapter changes.
            api_tools = api_tools + [{
                "type": "function",
                "name": self.TERMINAL_TOOL_NAME,
                "description": (
                    "Deliver the final answer to the user. Call this when no further "
                    "specialist work is needed. This is the ONLY way to reply — plain "
                    "text is not delivered to the user."
                ),
                "parameters": self._to_json_schema(response_schema),
            }]
            synthesized_terminal = True
        elif isinstance(response_schema, dict):
            text_format = {"format": {
                "type": "json_schema",
                "name": "response",
                "schema": self._to_json_schema(response_schema),
                "strict": False,
            }}
        elif response_mime_type == "application/json":
            # No schema to build a terminal tool from. Structure comes from the agent's
            # OUTPUT_FORMAT token; the same tool-suppression applies but cannot be
            # mitigated here.
            text_format = {"format": {"type": "json_object"}}

        logger.info(
            "🔍 [GrokAdapter] Request: model=%s input_items=%s tools=%s json_mode=%s "
            "grounding=%s thinking=%s",
            model_name,
            len(input_items),
            len(api_tools),
            text_format is not None,
            use_grounding,
            thinking or "none",
        )

        # Build kwargs — only include tools/tool_choice when tools are present.
        # The API rejects tool_choice when tools is absent or empty.
        create_kwargs: dict = dict(
            model=model_name,
            input=input_items,
            temperature=temperature,
            store=True,  # keep responses in the xAI dashboard for debugging
        )
        if system_instruction:
            create_kwargs["instructions"] = system_instruction
        cache_key = self._prompt_cache_key(cache_key_seed)
        if cache_key:
            create_kwargs["prompt_cache_key"] = cache_key
        if request.max_tokens:
            create_kwargs["max_output_tokens"] = request.max_tokens
        if text_format:
            create_kwargs["text"] = text_format
        if api_tools:
            create_kwargs["tools"] = api_tools
            # With the answer living on a tool, a bare text turn delivers nothing — so a
            # tool call is mandatory. The model still chooses WHICH: delegate or deliver.
            create_kwargs["tool_choice"] = (
                "required" if (force_tool_use or synthesized_terminal) else "auto"
            )
        # Reasoning effort. Unlike OpenAIAdapter there is no grounding branch here:
        # OpenAI must force effort="low" under grounding because gpt-5.4 defaults to
        # "none", which disables agentic search. Both xAI models reason by default, and
        # grounding without any `reasoning` block was measured 2026-08-15 to run real
        # agentic search (grok-4.6: 1 web_search_call, grok-4.3: 2) — so there is
        # nothing to force on.
        if thinking:
            effort = self._EFFORT_MAP.get(thinking)
            if effort is None:
                if thinking == "none" and model_name.startswith(self._EFFORT_NONE_MODELS):
                    effort = "none"
                else:
                    effort = "medium"
            create_kwargs["reasoning"] = {"effort": effort}

        # Make API call
        request_timeout = request.timeout
        # Honor an explicit caller timeout at the SDK level via the per-request
        # `timeout` kwarg — otherwise the client's 300s ceiling fires first and clamps
        # the effective timeout. The outer asyncio.wait_for bounds the TOTAL wall-time
        # (including any SDK retries) to the same value, so a retry cannot push the call
        # past the Cloud Task deadline. Mirrors OpenAIAdapter; its absence here meant
        # LLMRequest.timeout was silently capped by the client ceiling.
        if request_timeout:
            create_kwargs["timeout"] = float(request_timeout)
        try:
            _coro = self.client.responses.create(**create_kwargs)
            response = await (
                asyncio.wait_for(_coro, timeout=request_timeout)
                if request_timeout else _coro
            )
        except asyncio.TimeoutError as e:
            raise LLMTimeoutError(f"request timeout after {request_timeout}s") from e
        except openai.APITimeoutError as e:
            # SDK-level timeout (client ceiling 300s when request.timeout is None).
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
        return self._parse_response(response)

    def supports_caching(self) -> bool:
        """Grok exposes no controllable prompt caching (see CAPABILITIES)."""
        return False

    def get_capabilities(self) -> ProviderCapabilities:
        """Return Grok provider capabilities."""
        return self.CAPABILITIES

    def get_model_for_tier(self, tier: PerformanceTier) -> str:
        """
        Map performance tier to concrete Grok model name.

        Args:
            tier: Performance tier

        Returns:
            Model name string

        Raises:
            ValueError: If tier is not supported
        """
        model = self.MODEL_TIERS.get(tier)
        if not model:
            raise ValueError(f"Unsupported tier for Grok: {tier}")
        return model

    async def upload_file(self, file_path: str, mime_type: str) -> Any:
        """
        Grok does not support file uploads yet.

        Raises:
            NotImplementedError: Always
        """
        raise NotImplementedError("Grok does not support file uploads")

    # ========================================================================
    # Internal helpers
    # ========================================================================
    #
    # _prompt_cache_key and _to_json_schema are deliberate duplicates of the
    # equivalents in OpenAIAdapter. REQ-ARCH-23 forbids adapter→adapter imports,
    # and neither is domain logic worth promoting to domain/ for two call sites.

    @staticmethod
    def _prompt_cache_key(seed: Optional[str]) -> Optional[str]:
        """Derive a stable `prompt_cache_key` from the static prompt prefix, so requests
        sharing the same cacheable head route to the same cached copy. None → no key."""
        if not seed:
            return None
        return "alek-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:48]

    @staticmethod
    def _to_json_schema(schema: dict) -> dict:
        """Normalize a response schema for `text.format.json_schema`.

        Lowercases `type` values: some callers (RouterAgent) declare Gemini-native
        uppercase types ("OBJECT"/"STRING"), which the OpenAI-compatible schema
        validator rejects. Everything else passes through unchanged under strict=False.
        """
        def norm(node):
            if isinstance(node, dict):
                return {
                    k: (v.lower() if k == "type" and isinstance(v, str) else norm(v))
                    for k, v in node.items()
                }
            if isinstance(node, list):
                return [norm(item) for item in node]
            return node

        return norm(schema)

    @staticmethod
    def _extract_turn_anchor(
        messages: List[Message],
    ) -> tuple[List[Message], Optional[str]]:
        """Pull USER_TURN_SYSTEM_ANCHOR out of the last user message.

        Returns (messages_without_anchor, anchor_text). The caller re-injects the
        anchor as a `developer` item, which outranks both `instructions` and `user`
        on xAI. Returns the input untouched when no anchor is present.

        Extraction only. The caller combines this with `_PERSONALITY_ANCHOR` into a
        single `developer` item — see `generate_content`.
        """
        if not messages or messages[-1].role != "user":
            return messages, None

        last = messages[-1]
        for i, part in enumerate(last.parts):
            if not part.text or USER_TURN_SYSTEM_ANCHOR not in part.text:
                continue
            cleaned = part.text.replace(USER_TURN_SYSTEM_ANCHOR + "\n\n", "")
            new_part = MessagePart(
                text=cleaned,
                full_text=part.full_text,
                file_data=part.file_data,
                tool_call=part.tool_call,
                tool_response=part.tool_response,
                consolidation_text=part.consolidation_text,
            )
            new_last = Message(
                role="user",
                parts=last.parts[:i] + [new_part] + last.parts[i + 1:],
                raw_content=last.raw_content,
            )
            return messages[:-1] + [new_last], USER_TURN_SYSTEM_ANCHOR

        return messages, None

    def _find_call_id(
        self,
        messages: List[Message],
        tool_name: str,
        before_idx: int,
        used_ids: Set[str],
    ) -> str:
        """
        Find the call_id for a function result by matching the preceding function_call.

        Walks backwards from the message holding the result. `used_ids` keeps parallel
        calls to the same tool from all binding to the first id.
        """
        for i in range(before_idx, -1, -1):
            msg = messages[i]
            if msg.role != "model":
                continue

            raw = msg.raw_content
            # Responses API format: list of output items
            if isinstance(raw, list):
                for item in raw:
                    if getattr(item, "type", None) != "function_call":
                        continue
                    if getattr(item, "name", None) != tool_name:
                        continue
                    cid = getattr(item, "call_id", None)
                    if cid and cid not in used_ids:
                        return cid

            # Domain parts fallback
            for part in msg.parts:
                if part.tool_call and part.tool_call.name == tool_name:
                    cid = part.tool_call.thought_signature
                    if cid and cid not in used_ids:
                        return cid

        # Last resort: deterministic synthetic id. The API only requires that a
        # function_call_output's call_id matches some function_call in the same input.
        fallback = f"call_{tool_name}"
        logger.warning(
            "[GrokAdapter] call_id not found for tool '%s' — using %s",
            tool_name, fallback,
        )
        return fallback

    def _convert_input(self, messages: List[Message]) -> List[dict]:
        """
        Convert domain Message objects to Responses API input format.

        Responses API input is a list of items:
        - User messages:    {"role": "user", "content": "..."}
        - Model messages:   {"role": "assistant", "content": "..."}
        - Function calls:   {"type": "function_call", "call_id": ..., "name": ..., "arguments": ...}
        - Function results: {"type": "function_call_output", "call_id": ..., "output": ...}

        System instruction is NOT included — it goes into the separate `instructions`
        parameter.
        """
        items: List[dict] = []

        for idx, msg in enumerate(messages):
            role = "assistant" if msg.role == "model" else msg.role

            # Model messages with raw_content: replay output items verbatim so the
            # reasoning/function_call sequence stays intact across turns.
            if msg.role == "model" and msg.raw_content is not None:
                raw = msg.raw_content
                if isinstance(raw, list):
                    items.extend(raw)
                    continue
                # Pre-migration Chat Completions shape: ChatCompletionMessage object.
                # History written before 2026-08-14 still carries it.
                if getattr(raw, "tool_calls", None):
                    if getattr(raw, "content", None):
                        items.append({"role": "assistant", "content": raw.content})
                    for tc in raw.tool_calls:
                        items.append({
                            "type": "function_call",
                            "call_id": tc.id,
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        })
                    continue
                if getattr(raw, "content", None):
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
                        "call_id": (
                            part.tool_call.thought_signature
                            or f"call_{part.tool_call.name}"
                        ),
                        "name": part.tool_call.name,
                        "arguments": json.dumps(part.tool_call.args),
                    })
                elif part.tool_response:
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
                    if "ref" in part.file_data:
                        # GCS reference — carries no binary payload to send.
                        logger.debug(
                            "[GrokAdapter] file ref '%s' (no binary content)",
                            part.file_data["ref"],
                        )
                    elif mime.startswith("image/"):
                        b64 = part.file_data.get("base64")
                        if not b64 and "path" in part.file_data:
                            try:
                                with open(part.file_data["path"], "rb") as fh:
                                    b64 = base64.b64encode(fh.read()).decode("utf-8")
                            except Exception as e:
                                logger.error("[GrokAdapter] Failed to encode image: %s", e)
                        if b64:
                            content_parts.append({
                                "type": "input_image",
                                "image_url": f"data:{mime};base64,{b64}",
                            })
                    else:
                        # No Files API here: unlike OpenAI there is no `input_file` path,
                        # so non-image binaries cannot be forwarded. Upstream
                        # FileConversionService normally turns these into text first.
                        logger.warning(
                            "[GrokAdapter] Non-image file ignored (mime=%s) — "
                            "Grok accepts images only",
                            mime,
                        )

            if content_parts:
                if len(content_parts) == 1 and content_parts[0].get("type") == "input_text":
                    items.append({"role": role, "content": content_parts[0]["text"]})
                else:
                    items.append({"role": role, "content": content_parts})
            if function_calls:
                items.extend(function_calls)

        return items

    def _convert_tools(self, tools: List[Any]) -> List[dict]:
        """
        Convert domain tool definitions to Responses API format.

        Two shapes are supported:
        1. Custom function calls (our tools like search_memory) — converted to the
           internally-tagged format {"type": "function", "name": ..., "parameters": ...}
           (note: NOT the nested {"function": {...}} wrapper Chat Completions used).
        2. Native xAI tools (web_search, x_search, code_execution) — passed through.
        """
        api_tools = []

        for tool in tools:
            if isinstance(tool, dict):
                tool_type = tool.get("type", "function")

                if tool_type == "function":
                    api_tools.append({
                        "type": "function",
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters", {
                            "type": "object",
                            "properties": {}
                        })
                    })
                else:
                    # Native xAI tool — already in the right shape
                    api_tools.append(tool)

        return api_tools

    def _parse_response(self, response) -> LLMResponse:
        """
        Parse a Responses API response into domain LLMResponse.

        Structure:
        - response.output: list of items (reasoning, message, function_call,
          web_search_call, custom_tool_call)
        - response.output_text: concatenated assistant text
        - response.usage: token usage, including the automatic cache hit
        """
        text = response.output_text or ""
        if not isinstance(text, str):
            text = ""

        output_items = response.output or []

        # Reasoning trace. grok-4.6 reasons by default and returns it as dedicated
        # `reasoning` items, so it never pollutes `text` the way Gemini's thought parts
        # once did — but it is billed as output either way, so capture rather than drop
        # (mirrors GeminiAdapter.thought_text → BigQuery prompt_content).
        thought_chunks: List[str] = []
        tool_calls: List[ToolCall] = []
        server_side_calls = 0

        for item in output_items:
            item_type = getattr(item, "type", None)

            if item_type == "reasoning":
                for block in getattr(item, "summary", None) or []:
                    block_text = getattr(block, "text", None)
                    if isinstance(block_text, str) and block_text:
                        thought_chunks.append(block_text)

            elif item_type == "function_call":
                raw_args = getattr(item, "arguments", None) or ""
                try:
                    args = json.loads(raw_args) if raw_args else {}
                except json.JSONDecodeError:
                    logger.warning(
                        "[GrokAdapter] Truncated tool args for %s (len=%s): %s...",
                        getattr(item, "name", "?"), len(raw_args), raw_args[:200],
                    )
                    args = {
                        "_parse_error": "truncated_json",
                        "_raw_prefix": raw_args[:500],
                    }
                tool_calls.append(ToolCall(
                    name=getattr(item, "name", ""),
                    args=args,
                    thought_signature=getattr(item, "call_id", None),
                ))

            elif item_type in ("web_search_call", "custom_tool_call"):
                # Server-side tools xAI ran on our behalf — billed as input tokens,
                # never surfaced to the agent as a ToolCall. Log what it actually did,
                # mirroring OpenAIAdapter: without this a grounded turn is a black box
                # and there is no way to tell "searched and found nothing" from
                # "never searched".
                server_side_calls += 1
                action = getattr(item, "action", None)
                if action is not None:
                    action_type = getattr(action, "type", "") or ""
                    queries = getattr(action, "queries", None)
                    if queries:
                        for q in queries:
                            logger.info("🔍 [GrokAdapter] web_search [%s]: %s", action_type, q)
                    elif action_type == "open_page":
                        logger.info(
                            "🔍 [GrokAdapter] web_search [open_page]: %s",
                            getattr(action, "url", "") or "",
                        )

        thought_text = "".join(thought_chunks) or None

        # Append url_citation annotations as a sources block, mirroring OpenAIAdapter,
        # so downstream agents get the URLs in a predictable place. xAI also inlines
        # its own markdown citations in the text; this block is the machine-readable one.
        annotations: List[str] = []
        for item in output_items:
            if getattr(item, "type", None) != "message":
                continue
            for block in getattr(item, "content", None) or []:
                for ann in getattr(block, "annotations", None) or []:
                    if getattr(ann, "type", None) != "url_citation":
                        continue
                    url = getattr(ann, "url", "") or ""
                    title = getattr(ann, "title", "") or ""
                    if url:
                        annotations.append(f"- [{title}]({url})" if title else f"- {url}")
        if annotations:
            seen = set()
            unique = [a for a in annotations if not (a in seen or seen.add(a))]
            text += "\n\n*Sources:*\n" + "\n".join(unique)

        # Extract usage metadata
        usage_metadata = None
        if response.usage:
            usage = response.usage
            # xAI caches prompts automatically and reports the hit here. Repo-wide
            # convention is that UsageMetadata.prompt_tokens holds UNCACHED input only
            # (OpenAI/Gemini subtract it the same way), with the cached leg priced
            # separately by its own multiplier — so subtract instead of double-counting.
            details = getattr(usage, "input_tokens_details", None)
            cached_tokens = getattr(details, "cached_tokens", 0) or 0
            # The block is optional in the OpenAI-compatible schema, so treat anything
            # non-numeric as "no cache hit" rather than propagating it into the ledger.
            if not isinstance(cached_tokens, int):
                cached_tokens = 0
            input_tokens = getattr(usage, "input_tokens", 0) or 0
            output_tokens = getattr(usage, "output_tokens", 0) or 0
            if not isinstance(input_tokens, int):
                input_tokens = 0
            if not isinstance(output_tokens, int):
                output_tokens = 0
            usage_metadata = UsageMetadata(
                prompt_tokens=max(input_tokens - cached_tokens, 0),
                completion_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                cache_read_tokens=cached_tokens,
            )

        if not text and not tool_calls:
            # A reply that is neither text nor a tool call is a dead turn — surfaced
            # here so it is visible in Cloud Logging, mirroring OpenAIAdapter.
            logger.warning(
                "⚠️ [GrokAdapter] Empty response: model=%s output_items=%s",
                getattr(response, "model", "?"),
                len(output_items),
            )

        logger.info(
            "🔍 [GrokAdapter] Response: text_len=%s thought_len=%s tool_calls=%s "
            "server_tools=%s tokens=%s cached=%s",
            len(text),
            len(thought_text or ""),
            len(tool_calls),
            server_side_calls,
            usage_metadata.total_tokens if usage_metadata else 0,
            usage_metadata.cache_read_tokens if usage_metadata else 0,
        )

        return LLMResponse(
            text=text,
            thought_text=thought_text,
            tool_calls=tool_calls,
            # Output items are replayed verbatim on the next turn, so the model's
            # reasoning/function_call sequence must survive the round trip.
            raw_content=list(output_items),
            usage_metadata=usage_metadata
        )
