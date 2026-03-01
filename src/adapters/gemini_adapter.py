import asyncio
from typing import List, Any, Optional, Dict
from google import genai
from google.genai import types
from ..ports.llm_service import (
    LLMService,
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
from ..utils.logger import logger

class GeminiAdapter(LLMService):
    """
    Adapter for Google Gemini API.
    Implements the LLMService port.
    """

    # ========================================================================
    # NEW Provider Refactor Session 6: Tier-to-model mapping
    # Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
    # Purpose: Decouple agent performance tier from concrete model names
    # ========================================================================
    MODEL_TIERS = {
        PerformanceTier.ECO: "gemini-flash-lite-latest",
        PerformanceTier.BALANCED: "gemini-flash-latest",
        PerformanceTier.PERFORMANCE: "gemini-pro-latest"
    }

    # ========================================================================
    # NEW Provider Refactor Session 6: Provider capability declaration
    # Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
    # Purpose: Feature gating and runtime validation
    # ========================================================================
    CAPABILITIES = ProviderCapabilities(
        native_tools=True,
        context_caching=False,
        vision=True,
        max_context_window=1000000,
        native_grounding=True,
        supports_reasoning=True,
    )

    def __init__(self, api_key: str):
        self.client = genai.Client(api_key=api_key)

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
        automatic_function_calling: Optional[AutomaticFunctionCallingConfig] = None
    ) -> LLMResponse:
        force_tool_use: bool = False
        max_tokens: Optional[int] = None
        disable_safety: bool = False
        use_grounding: bool = False
        enable_reasoning: bool = False
        request_timeout: Optional[int] = None
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
            max_tokens = request.max_tokens
            disable_safety = request.disable_safety
            use_grounding = request.use_grounding
            enable_reasoning = request.enable_reasoning
            request_timeout = request.timeout
            stream_callback = None

        # Gemini does not support prompt caching — strip the boundary marker transparently.
        # The marker is an HTML comment injected by PromptAssemblyService for Claude caching;
        # leaving it in a Groovy-DSL system instruction breaks constrained JSON generation.
        if system_instruction and PROMPT_CACHE_BOUNDARY in system_instruction:
            system_instruction = system_instruction.replace(PROMPT_CACHE_BOUNDARY, "").strip()

        if not model_name or messages is None:
            raise ValueError("model_name and messages are required for Gemini generate_content")
        # ====================================================================
        # MODIFIED Provider Refactor Session 6: Fail-fast unsupported feature validation
        # Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
        # Purpose: Clear error messages for unsupported capabilities
        # ====================================================================
        if cache_config and cache_config.enabled:
            raise ValueError(
                "Gemini does not support prompt caching. "
                "Use a provider with prompt_caching capability (e.g., Claude)."
            )
        # Convert agnostic messages to Gemini-specific contents
        contents = await self._convert_messages(messages)

        if tools:
            tools = self._convert_tools_to_sdk_format(tools)
        
        # Determine automatic function calling mode
        afc_enabled = False
        if automatic_function_calling and automatic_function_calling.enabled:
            afc_enabled = True

        # Native Google Search grounding — injected as a special tool when requested.
        # Gemini handles it transparently: model decides when to search, results are
        # embedded in the response text. Cannot be combined with force_tool_use.
        if use_grounding:
            google_search = types.Tool(google_search=types.GoogleSearch())
            tools = [google_search] + (tools or [])

        tool_config = None
        if force_tool_use and tools:
            tool_config = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="ANY")
            )

        safety_threshold = "BLOCK_NONE" if disable_safety else "BLOCK_ONLY_HIGH"
        safety_settings = [
            types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold=safety_threshold),
            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold=safety_threshold),
            types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold=safety_threshold),
            types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold=safety_threshold),
            types.SafetySetting(category="HARM_CATEGORY_CIVIC_INTEGRITY", threshold=safety_threshold),
        ]

        # SDK 1.64+: response_json_schema (standard JSON Schema, lowercase types) is preferred
        # over response_schema (Gemini proprietary uppercase types) which silently returns empty
        # responses in newer SDK versions when passed as a plain dict.
        # Route dict schemas → response_json_schema; typed/class schemas → response_schema.
        use_json_schema = response_schema is not None and isinstance(response_schema, dict)
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=temperature,
            max_output_tokens=max_tokens,
            tools=tools,
            tool_config=tool_config,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=not afc_enabled),
            response_mime_type=response_mime_type,
            response_json_schema=self._to_json_schema(response_schema) if use_json_schema else None,
            response_schema=None if use_json_schema else response_schema,
            safety_settings=safety_settings,
            thinking_config=(
                types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW)
                if enable_reasoning and model_name == "gemini-flash-latest"
                else types.ThinkingConfig(thinking_budget=-1)
                if enable_reasoning
                else None
            ),
        )

        if stream_callback:
            logger.info(
                "🔍 [GeminiAdapter] Request: model=%s contents_count=%s contents_summary=%s",
                model_name,
                len(contents),
                [
                    {
                        "role": getattr(content, "role", None),
                        "parts": len(getattr(content, "parts", []) or [])
                    }
                    for content in contents
                ]
            )
            full_text = ""
            _stream_coro = self.client.aio.models.generate_content_stream(
                model=model_name, contents=contents, config=config
            )
            stream = await (asyncio.wait_for(_stream_coro, timeout=request_timeout) if request_timeout else _stream_coro)
            async for chunk in stream:
                chunk_text = self._extract_text(chunk)
                full_text += chunk_text
                await stream_callback(full_text)
            return LLMResponse(text=full_text)

        logger.info(
            "🔍 [GeminiAdapter] Request: model=%s contents_count=%s contents_summary=%s",
            model_name,
            len(contents),
            [
                {
                    "role": getattr(content, "role", None),
                    "parts": len(getattr(content, "parts", []) or [])
                }
                for content in contents
            ]
        )
        _gen_coro = self.client.aio.models.generate_content(
            model=model_name, contents=contents, config=config
        )
        response = await (asyncio.wait_for(_gen_coro, timeout=request_timeout) if request_timeout else _gen_coro)
        candidate_count = len(getattr(response, "candidates", []) or [])
        raw_parts_count = None
        if response.candidates:
            content = response.candidates[0].content
            if content and content.parts is not None:
                raw_parts_count = len(content.parts)
        logger.info(
            "🔍 [GeminiAdapter] Raw response: candidates=%s raw_parts=%s",
            candidate_count,
            raw_parts_count
        )
        return self._parse_response(response)

    def supports_caching(self) -> bool:
        return False

    # ====================================================================
    # NEW Provider Refactor Session 6: Provider capabilities accessor
    # Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
    # Purpose: Expose provider feature support for routing/feature gating
    # ====================================================================
    def get_capabilities(self) -> ProviderCapabilities:
        """
        Return Gemini provider capabilities.

        Gemini supports:
        - Native tools (function calling)
        - Vision (multimodal)

        Gemini does NOT support:
        - Prompt caching
        """
        return self.CAPABILITIES

    # ====================================================================
    # NEW Provider Refactor Session 6: Tier-based model resolution
    # Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
    # Purpose: Map performance tiers to Gemini models
    # ====================================================================
    def get_model_for_tier(self, tier: PerformanceTier) -> str:
        """
        Map performance tier to Gemini model name.

        Raises:
            ValueError: If tier is not supported.
        """
        if tier not in self.MODEL_TIERS:
            raise ValueError(
                f"Unsupported tier '{tier}' for Gemini. "
                f"Supported: {list(self.MODEL_TIERS.keys())}"
            )
        return self.MODEL_TIERS[tier]

    async def upload_file(self, path: str, mime_type: str) -> MessagePart:
        import asyncio
        uploaded_file = await asyncio.to_thread(
            self.client.files.upload, 
            file=path, 
            config={'mime_type': mime_type}
        )
        return MessagePart(file_data={"uri": uploaded_file.uri, "mime_type": uploaded_file.mime_type})

    async def _convert_messages(self, messages: List[Message]) -> List[types.Content]:
        gemini_contents = []
        for msg in messages:
            parts = []
            if msg.raw_content:
                gemini_contents.append(msg.raw_content)
                continue

            for p in msg.parts:
                if p.text:
                    parts.append(types.Part(text=p.text))
                elif p.tool_call:
                    function_call = types.FunctionCall(
                        name=p.tool_call.name,
                        args=p.tool_call.args
                    )
                    signature = p.tool_call.thought_signature
                    if signature:
                        function_call.thought_signature = signature
                        setattr(function_call, "thoughtSignature", signature)
                    parts.append(types.Part(function_call=function_call))
                elif p.tool_response:
                    parts.append(types.Part(function_response=types.FunctionResponse(
                        name=p.tool_response["name"],
                        response=p.tool_response["response"]
                    )))
                elif p.file_data:
                    # 🆕 HEXAGONAL: Adapter handles provider-specific file preparation
                    if "uri" in p.file_data:
                        # Already uploaded (from history)
                        parts.append(types.Part(file_data=types.FileData(
                            file_uri=p.file_data["uri"],
                            mime_type=p.file_data["mime_type"]
                        )))
                    elif "path" in p.file_data:
                        # New file: upload via Gemini API
                        import asyncio
                        uploaded_file = await asyncio.to_thread(
                            self.client.files.upload,
                            file=p.file_data["path"],
                            config={'mime_type': p.file_data["mime_type"]}
                        )
                        parts.append(types.Part(file_data=types.FileData(
                            file_uri=uploaded_file.uri,
                            mime_type=uploaded_file.mime_type
                        )))
                        logger.info(f"📎 [GeminiAdapter] Uploaded file: {uploaded_file.uri}")
                    else:
                        logger.warning(f"⚠️ [GeminiAdapter] Unsupported file_data format: {list(p.file_data.keys())}")
            gemini_contents.append(types.Content(role=msg.role, parts=parts))
        return gemini_contents

    def _convert_tools_to_sdk_format(self, tools: List[Any]) -> List[types.Tool]:
        if not tools:
            return []

        tool_declarations = []
        for tool in tools:
            if isinstance(tool, types.Tool):
                tool_declarations.append(tool)
                continue

            tool_declarations.append(
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=tool["name"],
                            description=tool.get("description", ""),
                            parameters=types.Schema(**tool.get("parameters", {}))
                        )
                    ]
                )
            )

        return tool_declarations

    def _to_json_schema(self, schema: Any) -> Any:
        """Recursively lowercase type names for standard JSON Schema (response_json_schema)."""
        if isinstance(schema, dict):
            return {
                k: (v.lower() if k == "type" and isinstance(v, str) else self._to_json_schema(v))
                for k, v in schema.items()
            }
        if isinstance(schema, list):
            return [self._to_json_schema(item) for item in schema]
        return schema

    def _extract_text(self, response) -> str:
        try:
            if not response.candidates: return ""
            candidate = response.candidates[0]
            if not candidate.content or not candidate.content.parts: return ""
            return "".join([p.text for p in candidate.content.parts if p.text])
        except: return ""

    def _extract_thought_signature(self, function_call) -> Optional[str]:
        if function_call is None:
            return None

        for attr in ("thought_signature", "thoughtSignature"):
            value = getattr(function_call, attr, None)
            if value:
                return value

        if hasattr(function_call, "model_dump"):
            data = function_call.model_dump()
        elif hasattr(function_call, "to_dict"):
            data = function_call.to_dict()
        elif isinstance(function_call, dict):
            data = function_call
        else:
            data = getattr(function_call, "__dict__", None)

        if isinstance(data, dict):
            return data.get("thought_signature") or data.get("thoughtSignature")

        return None

    def _parse_response(self, response) -> LLMResponse:
        if not response.candidates:
            prompt_feedback = getattr(response, "prompt_feedback", None)
            finish_reason = getattr(response, "finish_reason", None)
            logger.error(
                "❌ [GeminiAdapter] Empty candidates! prompt_feedback=%s finish_reason=%s",
                prompt_feedback,
                finish_reason
            )
            return LLMResponse(text="")

        candidate = response.candidates[0]
        if not candidate.content or not candidate.content.parts:
            finish_reason = getattr(candidate, "finish_reason", None)
            logger.warning(
                "⚠️ [GeminiAdapter] Empty content/parts (finish_reason=%s)", finish_reason
            )
            return LLMResponse(text="")
        text = "".join([p.text for p in candidate.content.parts if p.text])
        logger.info(
            "🔍 [GeminiAdapter] Parsed response: text_len=%s parts=%s",
            len(text),
            len(candidate.content.parts) if candidate.content and candidate.content.parts else 0
        )

        tool_calls = []
        for part in candidate.content.parts:
            if part.function_call:
                tool_calls.append(ToolCall(
                    name=part.function_call.name,
                    args=part.function_call.args or {},
                    thought_signature=self._extract_thought_signature(part.function_call)
                ))

        usage_metadata = None
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            usage_metadata = UsageMetadata(
                prompt_tokens=response.usage_metadata.prompt_token_count or 0,
                completion_tokens=response.usage_metadata.candidates_token_count or 0,
                total_tokens=response.usage_metadata.total_token_count or 0
            )

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            raw_content=candidate.content,
            usage_metadata=usage_metadata
        )
