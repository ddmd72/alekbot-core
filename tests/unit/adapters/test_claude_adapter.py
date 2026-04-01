import json
import pytest
from unittest.mock import MagicMock, AsyncMock
import anthropic

from src.adapters.claude_adapter import ClaudeAdapter
from src.domain.user import PerformanceTier
from src.domain.exceptions import LLMRateLimitError, LLMUnavailableError
from src.ports.llm_port import (
    PromptCacheConfig, AutomaticFunctionCallingConfig, PROMPT_CACHE_BOUNDARY,
    LLMRequest, Message, MessagePart,
)


# ============================================================================
# Capabilities, tier mapping, validation
# ============================================================================

def test_claude_capabilities():
    adapter = ClaudeAdapter(api_key="test-key")
    caps = adapter.get_capabilities()

    assert caps.native_tools is False
    assert caps.context_caching is True
    assert caps.streaming is True
    assert caps.vision is True
    assert caps.max_context_window == 200000


def test_claude_model_for_tier():
    adapter = ClaudeAdapter(api_key="test-key")

    assert adapter.get_model_for_tier(PerformanceTier.ECO) == "claude-haiku-4-5-20251001"
    assert adapter.get_model_for_tier(PerformanceTier.BALANCED) == "claude-sonnet-4-6"
    assert adapter.get_model_for_tier(PerformanceTier.PERFORMANCE) == "claude-sonnet-4-6"


def test_claude_unsupported_tier_raises():
    adapter = ClaudeAdapter(api_key="test-key")

    with pytest.raises(ValueError, match="Unsupported tier"):
        adapter.get_model_for_tier("invalid_tier")


@pytest.mark.asyncio
async def test_claude_native_tools_fail_fast():
    adapter = ClaudeAdapter(api_key="test-key")

    with pytest.raises(ValueError, match="does not support automatic function calling"):
        await adapter.generate_content(
            model_name="claude-sonnet-4-5",
            system_instruction="test",
            messages=[],
            automatic_function_calling=AutomaticFunctionCallingConfig(enabled=True, mode="AUTO")
        )


# ============================================================================
# Wire tests: verify what kwargs are actually sent to the Anthropic SDK
#
# Pattern: mock adapter.client.messages.stream with a capturing callable,
# call generate_content() end-to-end, inspect captured kwargs.
# These tests catch regressions where translation logic is changed but the
# port contract tests would not detect it.
# ============================================================================

_MESSAGES = [Message(role="user", parts=[MessagePart(text="Hi")])]
_TOOLS = [
    {
        "name": "search_memory",
        "description": "Search memories",
        "parameters": {"type": "object", "properties": {}},
    }
]


def _make_sdk_response(text="OK"):
    """Minimal Anthropic Message-like object that _parse_response can consume."""
    block = MagicMock()
    block.type = "text"
    block.text = text

    usage = MagicMock()
    usage.input_tokens = 10
    usage.output_tokens = 5
    usage.cache_creation_input_tokens = 0
    usage.cache_read_input_tokens = 0

    response = MagicMock()
    response.content = [block]
    response.usage = usage
    return response


def _make_sdk_tool_response(name, args, tc_id="call_1"):
    """Anthropic Message-like with a tool_use block."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.input = args
    block.id = tc_id

    usage = MagicMock()
    usage.input_tokens = 10
    usage.output_tokens = 5
    usage.cache_creation_input_tokens = 0
    usage.cache_read_input_tokens = 0

    response = MagicMock()
    response.content = [block]
    response.usage = usage
    return response


def _make_claude_cm(sdk_response):
    """Async context manager satisfying `async with client.messages.stream(...) as s:`."""
    stream = AsyncMock()
    stream.get_final_message = AsyncMock(return_value=sdk_response)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=stream)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


@pytest.mark.asyncio
async def test_force_tool_use_sends_tool_choice_any():
    """force_tool_use=True + tools (no thinking) → tool_choice={'type':'any'} in SDK call."""
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    cm = _make_claude_cm(_make_sdk_tool_response("search_memory", {"q": "x"}))

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return cm

    adapter.client.messages.stream = capturing_stream

    await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-sonnet-4-6",
            system_instruction="test",
            messages=_MESSAGES,
            tools=_TOOLS,
            force_tool_use=True,
        )
    )

    assert captured.get("tool_choice") == {"type": "any"}, (
        f"Expected tool_choice={{'type':'any'}}, got {captured.get('tool_choice')!r}"
    )


@pytest.mark.asyncio
async def test_force_tool_use_with_thinking_sends_tool_choice_auto():
    """force_tool_use=True + tools + thinking → tool_choice={'type':'auto'}.

    Claude API rejects tool_choice='any' when thinking is enabled.
    """
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    cm = _make_claude_cm(_make_sdk_tool_response("search_memory", {"q": "x"}))

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return cm

    adapter.client.messages.stream = capturing_stream

    await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-sonnet-4-6",
            system_instruction="test",
            messages=_MESSAGES,
            tools=_TOOLS,
            force_tool_use=True,
            thinking="medium",
        )
    )

    assert captured.get("tool_choice") == {"type": "auto"}, (
        f"Expected tool_choice={{'type':'auto'}}, got {captured.get('tool_choice')!r}"
    )


@pytest.mark.asyncio
async def test_no_force_tool_use_omits_tool_choice():
    """force_tool_use=False → tool_choice must be absent from SDK call.

    Claude API returns 400 if tool_choice is null (not just missing).
    """
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    cm = _make_claude_cm(_make_sdk_response())

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return cm

    adapter.client.messages.stream = capturing_stream

    await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-sonnet-4-6",
            system_instruction="test",
            messages=_MESSAGES,
            tools=_TOOLS,
            force_tool_use=False,
        )
    )

    assert "tool_choice" not in captured, (
        f"tool_choice must be absent when force_tool_use=False; keys={list(captured.keys())}"
    )


@pytest.mark.asyncio
async def test_force_tool_use_without_tools_omits_tool_choice():
    """force_tool_use=True but no tools → tool_choice absent (guard: if force_tool_use and claude_tools)."""
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    cm = _make_claude_cm(_make_sdk_response())

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return cm

    adapter.client.messages.stream = capturing_stream

    await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-sonnet-4-6",
            system_instruction="test",
            messages=_MESSAGES,
            force_tool_use=True,  # no tools → guard fires
        )
    )

    assert "tool_choice" not in captured


@pytest.mark.asyncio
async def test_use_grounding_injects_web_search_and_web_fetch():
    """Sonnet + use_grounding=True → dynamic filtering tools (20260209) prepended to tools."""
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    cm = _make_claude_cm(_make_sdk_response())

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return cm

    adapter.client.messages.stream = capturing_stream

    await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-sonnet-4-6",
            system_instruction="test",
            messages=_MESSAGES,
            use_grounding=True,
        )
    )

    tools = captured.get("tools", [])
    types_in_tools = [t.get("type") for t in tools if isinstance(t, dict)]
    assert "web_search_20260209" in types_in_tools, f"web_search tool missing; tools={tools}"
    assert "web_fetch_20260209" in types_in_tools, f"web_fetch tool missing; tools={tools}"
    assert tools[0]["type"] == "web_search_20260209"  # must be prepended, not appended
    assert tools[1]["type"] == "web_fetch_20260209"


@pytest.mark.asyncio
async def test_use_grounding_adds_web_search_beta_header():
    """use_grounding=True → prompt-caching header present; web-search-2025-03-05 not needed
    (new 20260209 tools are GA and require no extra beta header)."""
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    cm = _make_claude_cm(_make_sdk_response())

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return cm

    adapter.client.messages.stream = capturing_stream

    await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-sonnet-4-6",
            system_instruction="test",
            messages=_MESSAGES,
            use_grounding=True,
        )
    )

    beta_header = captured.get("extra_headers", {}).get("anthropic-beta", "")
    assert "prompt-caching-2024-07-31" in beta_header, (
        f"prompt-caching header missing; anthropic-beta={beta_header!r}"
    )
    assert "web-search-2025-03-05" not in beta_header, (
        f"obsolete web-search header should not be sent; anthropic-beta={beta_header!r}"
    )


@pytest.mark.asyncio
async def test_haiku_use_grounding_injects_legacy_tools():
    """Haiku + use_grounding=True → legacy tools (20250305/20250910), NOT dynamic filtering tools."""
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    cm = _make_claude_cm(_make_sdk_response())

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return cm

    adapter.client.messages.stream = capturing_stream

    await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-haiku-4-5-20251001",
            system_instruction="test",
            messages=_MESSAGES,
            use_grounding=True,
        )
    )

    tools = captured.get("tools", [])
    types_in_tools = [t.get("type") for t in tools if isinstance(t, dict)]
    assert "web_search_20250305" in types_in_tools, f"legacy web_search missing; tools={tools}"
    assert "web_fetch_20250910" in types_in_tools, f"legacy web_fetch missing; tools={tools}"
    assert "web_search_20260209" not in types_in_tools, "dynamic filtering tool must NOT be used for Haiku"
    assert "web_fetch_20260209" not in types_in_tools, "dynamic filtering tool must NOT be used for Haiku"


@pytest.mark.asyncio
async def test_haiku_use_grounding_adds_legacy_beta_header():
    """Haiku + use_grounding=True → web-search-2025-03-05 header required for legacy tools."""
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    cm = _make_claude_cm(_make_sdk_response())

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return cm

    adapter.client.messages.stream = capturing_stream

    await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-haiku-4-5-20251001",
            system_instruction="test",
            messages=_MESSAGES,
            use_grounding=True,
        )
    )

    beta_header = captured.get("extra_headers", {}).get("anthropic-beta", "")
    assert "web-search-2025-03-05" in beta_header, (
        f"legacy beta header missing for Haiku; anthropic-beta={beta_header!r}"
    )


@pytest.mark.asyncio
async def test_grounded_loop_handles_pause_turn_continuation():
    """Sonnet + use_grounding: pause_turn on first call → loop sends accumulated content back,
    end_turn on second call → final text returned."""
    adapter = ClaudeAdapter(api_key="test-key")

    call_kwargs_log: list = []

    def _make_response(stop_reason: str, text: str = ""):
        block = MagicMock()
        block.type = "text"
        block.text = text
        usage = MagicMock()
        usage.input_tokens = 5
        usage.output_tokens = 3
        usage.cache_creation_input_tokens = 0
        usage.cache_read_input_tokens = 0
        resp = MagicMock()
        resp.content = [block]
        resp.usage = usage
        resp.stop_reason = stop_reason
        return resp

    responses = [
        _make_response("pause_turn", ""),  # pause_turn has no text (code_execution running)
        _make_response("end_turn", "final answer"),
    ]
    call_index = 0

    def capturing_stream(**kwargs):
        nonlocal call_index
        call_kwargs_log.append(dict(kwargs))
        resp = responses[call_index]
        call_index += 1
        stream = AsyncMock()
        stream.__aiter__ = MagicMock(return_value=stream)
        stream.__anext__ = AsyncMock(side_effect=StopAsyncIteration)  # no delta events
        stream.get_final_message = AsyncMock(return_value=resp)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=stream)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm

    adapter.client.messages.stream = capturing_stream

    result = await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-sonnet-4-6",
            system_instruction="test",
            messages=_MESSAGES,
            use_grounding=True,
        )
    )

    assert call_index == 2, f"Expected 2 stream calls (pause_turn + end_turn), got {call_index}"
    # Second call must include accumulated assistant content
    second_messages = call_kwargs_log[1].get("messages", [])
    assert len(second_messages) == 2, "Second call must have original user msg + assistant continuation"
    assert second_messages[1]["role"] == "assistant"
    assert result.text == "final answer"


@pytest.mark.asyncio
async def test_thinking_enabled_sends_adaptive_param_for_sonnet():
    """thinking set + Sonnet model → thinking={'type':'adaptive'} and temperature=1.0."""
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    cm = _make_claude_cm(_make_sdk_response())

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return cm

    adapter.client.messages.stream = capturing_stream

    await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-sonnet-4-6",
            system_instruction="test",
            messages=_MESSAGES,
            thinking="medium",
        )
    )

    assert captured.get("thinking") == {"type": "adaptive"}, (
        f"Expected thinking={{'type':'adaptive'}}, got {captured.get('thinking')!r}"
    )
    assert captured.get("temperature") == 1.0, (
        f"Expected temperature=1.0 when thinking enabled, got {captured.get('temperature')!r}"
    )


@pytest.mark.asyncio
async def test_thinking_skipped_for_haiku():
    """thinking set + Haiku model → 'thinking' key must be absent (Haiku does not support it)."""
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    cm = _make_claude_cm(_make_sdk_response())

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return cm

    adapter.client.messages.stream = capturing_stream

    await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-haiku-4-5-20251001",
            system_instruction="test",
            messages=_MESSAGES,
            thinking="high",
        )
    )

    assert "thinking" not in captured, (
        f"'thinking' must be absent for Haiku; keys={list(captured.keys())}"
    )


@pytest.mark.asyncio
async def test_tool_calls_parsed_from_response():
    """tool_use content block → LLMResponse.tool_calls populated correctly."""
    adapter = ClaudeAdapter(api_key="test-key")
    cm = _make_claude_cm(
        _make_sdk_tool_response("search_memory", {"query": "test"}, "call_abc")
    )

    adapter.client.messages.stream = lambda **kw: cm

    response = await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-sonnet-4-6",
            system_instruction="test",
            messages=_MESSAGES,
            tools=_TOOLS,
        )
    )

    assert len(response.tool_calls) == 1
    tc = response.tool_calls[0]
    assert tc.name == "search_memory"
    assert tc.args == {"query": "test"}
    assert tc.thought_signature == "call_abc"


@pytest.mark.asyncio
async def test_cache_boundary_splits_system_into_two_blocks_wire():
    """Cache boundary in system instruction → system kwarg has 2 blocks, first cached."""
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    cm = _make_claude_cm(_make_sdk_response())

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return cm

    adapter.client.messages.stream = capturing_stream

    static = "static part"
    dynamic = "dynamic context"
    system = f"{static}\n\n{PROMPT_CACHE_BOUNDARY}\n{dynamic}"

    await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-sonnet-4-6",
            system_instruction=system,
            messages=_MESSAGES,
            cache_config=PromptCacheConfig(enabled=True),
        )
    )

    system_blocks = captured.get("system", [])
    assert len(system_blocks) == 2
    assert system_blocks[0].get("cache_control") == {"type": "ephemeral"}
    assert "cache_control" not in system_blocks[1]
    assert system_blocks[0]["text"] == static
    assert system_blocks[1]["text"] == dynamic


# ============================================================================
# Wire tests: system instruction + cache_control edge cases
# ============================================================================

@pytest.mark.asyncio
async def test_no_system_instruction_sends_empty_system_list():
    """system_instruction=None → empty system list sent to SDK.

    Claude API returns 400 on empty text content blocks, so the adapter must
    skip the system kwarg entirely (empty list) when instruction is absent.
    """
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    cm = _make_claude_cm(_make_sdk_response())

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return cm

    adapter.client.messages.stream = capturing_stream

    await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-sonnet-4-6",
            system_instruction=None,
            messages=_MESSAGES,
            cache_config=PromptCacheConfig(enabled=True),
        )
    )

    assert captured.get("system") == [], (
        f"Expected empty system list for None instruction; got {captured.get('system')!r}"
    )


@pytest.mark.asyncio
async def test_cache_no_boundary_caches_whole_system():
    """cache enabled + no boundary marker → single system block with cache_control."""
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    cm = _make_claude_cm(_make_sdk_response())

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return cm

    adapter.client.messages.stream = capturing_stream

    await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-sonnet-4-6",
            system_instruction="full static consolidation prompt",
            messages=_MESSAGES,
            cache_config=PromptCacheConfig(enabled=True),
        )
    )

    system = captured.get("system", [])
    assert len(system) == 1
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert system[0]["text"] == "full static consolidation prompt"


@pytest.mark.asyncio
async def test_no_cache_config_single_block_no_cache_control():
    """No cache_config → single system block without cache_control."""
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    cm = _make_claude_cm(_make_sdk_response())

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return cm

    adapter.client.messages.stream = capturing_stream

    await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-sonnet-4-6",
            system_instruction="some system instruction",
            messages=_MESSAGES,
        )
    )

    system = captured.get("system", [])
    assert len(system) == 1
    assert "cache_control" not in system[0]
    assert system[0]["text"] == "some system instruction"


# ============================================================================
# Wire tests: respond tool injection and interception
#
# Claude enforces response_schema via a synthetic "respond" tool injected when
# both real tools and a response_schema are present. The adapter intercepts the
# "respond" tool call and serialises its args as JSON text, so agents see a
# plain LLMResponse.text — identical to Gemini's native schema enforcement.
# ============================================================================

@pytest.mark.asyncio
async def test_respond_tool_injected_when_schema_and_tools_present():
    """respond tool is appended to tools list when response_schema + real tools present."""
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    cm = _make_claude_cm(_make_sdk_tool_response("search_memory", {"q": "x"}))

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return cm

    adapter.client.messages.stream = capturing_stream

    await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-sonnet-4-6",
            system_instruction="test",
            messages=_MESSAGES,
            tools=_TOOLS,
            response_schema={"type": "object", "properties": {"answer": {"type": "string"}}},
        )
    )

    tools = captured.get("tools", [])
    assert len(tools) == 2
    assert tools[-1]["name"] == "respond"
    assert tools[-1]["input_schema"] == {"type": "object", "properties": {"answer": {"type": "string"}}}


@pytest.mark.asyncio
async def test_respond_tool_injected_without_real_tools():
    """respond tool IS injected when response_schema is set even without delegation tools."""
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    cm = _make_claude_cm(_make_sdk_tool_response("respond", {"answer": "42"}))

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return cm

    adapter.client.messages.stream = capturing_stream

    await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-sonnet-4-6",
            system_instruction="test",
            messages=_MESSAGES,
            response_schema={"type": "object", "properties": {"answer": {"type": "string"}}},
        )
    )

    tools = captured.get("tools", [])
    assert len(tools) == 1
    assert tools[0]["name"] == "respond"


@pytest.mark.asyncio
async def test_respond_tool_not_injected_without_schema():
    """respond tool is NOT injected when response_schema is None."""
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    cm = _make_claude_cm(_make_sdk_response())

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return cm

    adapter.client.messages.stream = capturing_stream

    await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-sonnet-4-6",
            system_instruction="test",
            messages=_MESSAGES,
            tools=_TOOLS,
        )
    )

    tools = captured.get("tools", [])
    assert not any(t.get("name") == "respond" for t in tools)


@pytest.mark.asyncio
async def test_respond_tool_schema_strips_nullable_key():
    """'nullable' key is stripped from respond tool's input_schema (Anthropic rejects it)."""
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    cm = _make_claude_cm(_make_sdk_tool_response("search_memory", {"q": "x"}))

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return cm

    adapter.client.messages.stream = capturing_stream

    await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-sonnet-4-6",
            system_instruction="test",
            messages=_MESSAGES,
            tools=_TOOLS,
            response_schema={"type": "object", "properties": {}, "nullable": True},
        )
    )

    tools = captured.get("tools", [])
    respond = next((t for t in tools if t.get("name") == "respond"), None)
    assert respond is not None
    assert "nullable" not in respond["input_schema"]


@pytest.mark.asyncio
async def test_respond_call_intercepted_returns_json_text():
    """When LLM calls 'respond' tool, args are serialised to JSON text and tool_calls is empty."""
    adapter = ClaudeAdapter(api_key="test-key")
    cm = _make_claude_cm(
        _make_sdk_tool_response("respond", {"full_response": "hello", "response_summary": "hi"})
    )
    adapter.client.messages.stream = lambda **kw: cm

    result = await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-sonnet-4-6",
            system_instruction="test",
            messages=_MESSAGES,
            tools=_TOOLS,
            response_schema={"type": "object", "properties": {"full_response": {"type": "string"}}},
        )
    )

    assert result.tool_calls == []
    parsed = json.loads(result.text)
    assert parsed["full_response"] == "hello"
    assert parsed["response_summary"] == "hi"


@pytest.mark.asyncio
async def test_respond_call_not_present_returns_original_tool_calls():
    """When LLM calls a real tool (not respond), tool_calls are preserved unchanged."""
    adapter = ClaudeAdapter(api_key="test-key")
    cm = _make_claude_cm(_make_sdk_tool_response("search_memory", {"query": "test"}))
    adapter.client.messages.stream = lambda **kw: cm

    result = await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-sonnet-4-6",
            system_instruction="test",
            messages=_MESSAGES,
            tools=_TOOLS,
            response_schema={"type": "object", "properties": {"answer": {"type": "string"}}},
        )
    )

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "search_memory"


# ============================================================================
# Error wrapping: SDK exceptions → domain exceptions
# ============================================================================

def _make_failing_cm(exc: Exception):
    """Async context manager whose stream.get_final_message raises exc."""
    stream = AsyncMock()
    stream.get_final_message = AsyncMock(side_effect=exc)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=stream)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


@pytest.mark.asyncio
async def test_rate_limit_error_raises_llm_rate_limit_error():
    """anthropic.RateLimitError → LLMRateLimitError with http_status=429."""
    adapter = ClaudeAdapter(api_key="test-key")
    sdk_exc = anthropic.RateLimitError(
        message="Rate limit exceeded",
        response=MagicMock(status_code=429),
        body={"error": {"type": "rate_limit_error"}},
    )
    adapter.client.messages.stream = lambda **kw: _make_failing_cm(sdk_exc)

    with pytest.raises(LLMRateLimitError) as exc_info:
        await adapter.generate_content(
            request=LLMRequest(
                model_name="claude-sonnet-4-6",
                system_instruction="test",
                messages=_MESSAGES,
            )
        )

    assert exc_info.value.http_status == 429


@pytest.mark.asyncio
async def test_api_status_503_raises_llm_unavailable_error():
    """anthropic.APIStatusError(status_code=503) → LLMUnavailableError with http_status=503."""
    adapter = ClaudeAdapter(api_key="test-key")
    sdk_exc = anthropic.APIStatusError(
        message="Service unavailable",
        response=MagicMock(status_code=503),
        body={"error": {"type": "overloaded_error"}},
    )
    adapter.client.messages.stream = lambda **kw: _make_failing_cm(sdk_exc)

    with pytest.raises(LLMUnavailableError) as exc_info:
        await adapter.generate_content(
            request=LLMRequest(
                model_name="claude-sonnet-4-6",
                system_instruction="test",
                messages=_MESSAGES,
            )
        )

    assert exc_info.value.http_status == 503


@pytest.mark.asyncio
async def test_api_status_400_re_raises_as_is():
    """anthropic.APIStatusError(status_code=400) is not wrapped — re-raised unchanged."""
    adapter = ClaudeAdapter(api_key="test-key")
    sdk_exc = anthropic.APIStatusError(
        message="Bad request",
        response=MagicMock(status_code=400),
        body={"error": {"type": "invalid_request_error"}},
    )
    adapter.client.messages.stream = lambda **kw: _make_failing_cm(sdk_exc)

    with pytest.raises(anthropic.APIStatusError):
        await adapter.generate_content(
            request=LLMRequest(
                model_name="claude-sonnet-4-6",
                system_instruction="test",
                messages=_MESSAGES,
            )
        )


# ---------------------------------------------------------------------------
# GCS reference file_data — graceful handling (no binary, no error)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gcs_ref_file_data_no_error():
    """file_data with 'ref' key should not raise — it's a GCS reference with no binary."""
    adapter = ClaudeAdapter(api_key="test-key")
    messages = [
        Message(role="user", parts=[
            MessagePart(text='[File: "report.docx" (45KB)]'),
            MessagePart(file_data={"ref": "report.docx", "mime_type": "text/plain", "size_bytes": 45000}),
        ]),
    ]

    result = await adapter._convert_messages(messages)

    # Should have one user message; ref part is silently skipped (only text part emits content)
    assert len(result) == 1
    assert result[0]["role"] == "user"
