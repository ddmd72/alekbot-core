import time
from anthropic import types, AsyncAnthropic
from typing import List, Any, Optional, Dict, Union
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
    CacheMetadata,
    LLMRequest
)
from ..domain.user import PerformanceTier
from ..utils.logger import logger
from .groovy_to_markdown_transformer import GroovyToMarkdownConverter

class ClaudeAdapter(LLMService):
    """
    Adapter for Anthropic Claude API.
    Implements the LLMService port with prompt caching support.
    """

    # Feature flag for Groovy -> Markdown transformation
    USE_MARKDOWN_PROMPT = False

    # ========================================================================
    # NEW Provider Refactor Session 7: Tier-to-model mapping
    # Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
    # Purpose: Decouple agent performance tier from concrete model names
    # ========================================================================
    MODEL_TIERS = {
        PerformanceTier.ECO: "claude-haiku-4-5",
        PerformanceTier.BALANCED: "claude-sonnet-4-5",
        PerformanceTier.PERFORMANCE: "claude-sonnet-4-5"
    }

    # ========================================================================
    # NEW Provider Refactor Session 7: Provider capability declaration
    # Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
    # Purpose: Feature gating and runtime validation
    # ========================================================================
    CAPABILITIES = ProviderCapabilities(
        native_tools=False,
        context_caching=True,
        vision=True,
        max_context_window=200000
    )

    def __init__(self, api_key: str):
        self.client = AsyncAnthropic(api_key=api_key)
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
        
        # Claude uses 'system' parameter for system instructions
        system_parts = [{"type": "text", "text": system_instruction}]
        
        # Apply caching to system instruction if enabled
        if cache_config and cache_config.enabled:
            system_parts[0]["cache_control"] = {"type": "ephemeral"}

        # Convert messages to Anthropic format
        claude_messages = await self._convert_messages(messages)

        # Convert tools to Anthropic format
        claude_tools = self._convert_tools(tools) if tools else None

        if stream_callback:
            # Note: Prompt caching in streaming is supported but handled slightly differently in usage stats
            async with self.client.messages.stream(
                model=model_name,
                max_tokens=4096,
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

        # Regular request
        # Claude API rejects tool_choice=null — must be omitted entirely when not forcing a tool
        create_kwargs: dict = dict(
            model=model_name,
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
            max_tokens=4096,
            system=system_parts,
            messages=claude_messages,
            tools=claude_tools if claude_tools else [],
            temperature=temperature,
        )
        if force_tool_use and claude_tools:
            create_kwargs["tool_choice"] = {"type": "any"}
        response = await self.client.messages.create(**create_kwargs)

        return self._parse_response(response)

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
            if content.type == "text":
                text += content.text
            elif content.type == "tool_use":
                tool_calls.append(ToolCall(
                    name=content.name,
                    args=content.input,
                    thought_signature=content.id # Use ID as signature for Claude
                ))

        # Parse usage and cache metadata
        usage = response.usage
        usage_metadata = UsageMetadata(
            prompt_tokens=usage.input_tokens,
            completion_tokens=usage.output_tokens,
            total_tokens=usage.input_tokens + usage.output_tokens
        )

        cache_metadata = None
        if hasattr(usage, 'cache_creation_input_tokens') or hasattr(usage, 'cache_read_input_tokens'):
            cache_metadata = CacheMetadata(
                provider="anthropic",
                cache_hit=getattr(usage, 'cache_read_input_tokens', 0) > 0,
                tokens_saved=getattr(usage, 'cache_read_input_tokens', 0),
                created_at=time.time()
            )

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            raw_content=response,
            usage_metadata=usage_metadata,
            cache_metadata=cache_metadata
        )
