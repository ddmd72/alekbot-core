import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock
import anthropic

from src.adapters.claude_adapter import (
    ClaudeAdapter, _strip_nullable, _nullable_to_union, _make_strict,
)
from src.domain.user import PerformanceTier
from src.domain.exceptions import (
    LLMClientError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
    LLMUnavailableError,
)
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
    assert caps.vision is True
    assert caps.max_context_window == 1000000


def test_claude_model_for_tier():
    adapter = ClaudeAdapter(api_key="test-key")

    assert adapter.get_model_for_tier(PerformanceTier.ECO) == "claude-haiku-4-5-20251001"
    assert adapter.get_model_for_tier(PerformanceTier.BALANCED) == "claude-haiku-4-5-20251001"
    assert adapter.get_model_for_tier(PerformanceTier.PERFORMANCE) == "claude-sonnet-5"
    assert adapter.get_model_for_tier(PerformanceTier.ULTRA) == "claude-opus-4-8"


def test_claude_performance_tier_env_override(monkeypatch):
    """CLAUDE_PERFORMANCE_MODEL env / constructor arg rolls PERFORMANCE back to Sonnet 4.6."""
    # Default (no override): Sonnet 5.
    monkeypatch.delenv("CLAUDE_PERFORMANCE_MODEL", raising=False)
    assert ClaudeAdapter(api_key="k").get_model_for_tier(PerformanceTier.PERFORMANCE) == "claude-sonnet-5"
    # Constructor arg wins.
    arg_adapter = ClaudeAdapter(api_key="k", performance_model="claude-sonnet-4-6")
    assert arg_adapter.get_model_for_tier(PerformanceTier.PERFORMANCE) == "claude-sonnet-4-6"
    # Env var override (rollback kill-switch); other tiers unaffected.
    monkeypatch.setenv("CLAUDE_PERFORMANCE_MODEL", "claude-sonnet-4-6")
    env_adapter = ClaudeAdapter(api_key="k")
    assert env_adapter.get_model_for_tier(PerformanceTier.PERFORMANCE) == "claude-sonnet-4-6"
    assert env_adapter.get_model_for_tier(PerformanceTier.ULTRA) == "claude-opus-4-8"


def test_claude_unsupported_tier_raises():
    adapter = ClaudeAdapter(api_key="test-key")

    with pytest.raises(ValueError, match="Unsupported tier"):
        adapter.get_model_for_tier("invalid_tier")


@pytest.mark.asyncio
async def test_claude_native_tools_fail_fast():
    adapter = ClaudeAdapter(api_key="test-key")

    with pytest.raises(ValueError, match="does not support automatic function calling"):
        await adapter.generate_content(
            request=LLMRequest(
                model_name="claude-sonnet-4-5",
                system_instruction="test",
                messages=[],
                automatic_function_calling=AutomaticFunctionCallingConfig(enabled=True, mode="AUTO"),
            )
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
# Wire tests: response_schema -> respond-tool substitution (NOT output_config.format)
# ============================================================================
# Structured output on Claude is NATIVE via output_config.format (2026-07-02) — mirrors
# Gemini's response_json_schema. The synthesized `respond` tool was removed: on Sonnet 5 it
# leaked Claude's internal <parameter> tool-call serialization into the output. The schema is
# shaped for the grammar compiler (_nullable_to_union → _make_strict) before forwarding.
# See docs/04_solution_strategy/decisions/claude_schema_respond_tool.md.

@pytest.mark.asyncio
async def test_response_schema_sets_output_config_format():
    """When response_schema is present, the adapter forwards it via output_config.format
    (native structured output) and injects NO `respond` tool. The model's JSON text is
    returned as-is."""
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    cm = _make_claude_cm(_make_sdk_response('{"answer": "42"}'))

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return cm

    adapter.client.messages.stream = capturing_stream

    result = await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-sonnet-4-6",
            system_instruction="test",
            messages=_MESSAGES,
            tools=_TOOLS,
            response_schema={"type": "object", "properties": {"answer": {"type": "string"}}},
        )
    )

    # output_config.format carries the schema (json_schema), shaped for the grammar compiler
    fmt = (captured.get("output_config") or {}).get("format")
    assert fmt == {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "additionalProperties": False,
        },
    }
    # NO respond tool injected
    assert all(t.get("name") != "respond" for t in captured.get("tools", []))
    # model's JSON text returned unchanged
    assert result.text == '{"answer": "42"}'
    assert result.tool_calls == []


@pytest.mark.asyncio
async def test_output_config_format_schema_is_strict_shaped():
    """The schema forwarded via output_config.format is grammar-compiler-legal: every object
    gets additionalProperties:false, and optional properties stay optional (no `required`
    injection — variant keys like rich_content.data must remain optional)."""
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    cm = _make_claude_cm(_make_sdk_response("{}"))

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
            response_schema={
                "type": "object",
                "properties": {
                    "nested": {"type": "object", "properties": {}},
                    "array": {"type": "array", "items": {"type": "object", "properties": {}}}
                }
            },
        )
    )

    schema = captured["output_config"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["nested"]["additionalProperties"] is False
    assert schema["properties"]["array"]["items"]["additionalProperties"] is False
    # arrays don't get additionalProperties; no required injected
    assert "additionalProperties" not in schema["properties"]["array"]
    assert "required" not in schema


@pytest.mark.asyncio
async def test_output_config_format_schema_converts_nullable_to_union():
    """'nullable: True' is translated to a JSON-Schema ["<type>", "null"] union at every level
    before it reaches output_config.format (Anthropic's grammar has no 'nullable' keyword)."""
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    cm = _make_claude_cm(_make_sdk_response("{}"))

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
            response_schema={
                "type": "object",
                "properties": {
                    "rich_content": {
                        "type": "object",
                        "nullable": True,
                        "properties": {"type": {"type": "string"}},
                    },
                },
            },
        )
    )

    schema = captured["output_config"]["format"]["schema"]
    assert "nullable" not in schema["properties"]["rich_content"]
    assert schema["properties"]["rich_content"]["type"] == ["object", "null"]


@pytest.mark.asyncio
async def test_response_schema_with_thinking_keeps_effort_and_format():
    """With both thinking and response_schema active, output_config carries BOTH effort and
    format, and thinking is set."""
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    cm = _make_claude_cm(_make_sdk_response("{}"))

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
            thinking="medium",
            response_schema={"type": "object", "properties": {}},
        )
    )

    output_config = captured.get("output_config", {})
    assert output_config.get("effort") == "medium"
    assert output_config.get("format", {}).get("type") == "json_schema"
    assert captured.get("thinking") == {"type": "adaptive"}
    assert all(t.get("name") != "respond" for t in captured.get("tools", []))


@pytest.mark.asyncio
async def test_no_response_schema_no_output_config_format():
    """When response_schema is absent, output_config must not contain 'format'."""
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

    output_config = captured.get("output_config", {})
    assert "format" not in output_config, (
        f"output_config.format must be absent when response_schema is None; output_config={output_config}"
    )


@pytest.mark.asyncio
async def test_response_schema_without_delegation_tools():
    """output_config.format is set even when no delegation tools are passed; no respond tool."""
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    cm = _make_claude_cm(_make_sdk_response('{"result": "ok"}'))

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return cm

    adapter.client.messages.stream = capturing_stream

    result = await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-sonnet-4-6",
            system_instruction="test",
            messages=_MESSAGES,
            response_schema={"type": "object", "properties": {"result": {"type": "string"}}},
        )
    )

    assert captured["output_config"]["format"]["type"] == "json_schema"
    assert all(t.get("name") != "respond" for t in captured.get("tools", []))
    assert result.text == '{"result": "ok"}'


@pytest.mark.asyncio
async def test_tool_calls_preserved_under_response_schema():
    """When response_schema is active and model returns real delegation tool_calls,
    they are returned unchanged — no interception, no second API call."""
    adapter = ClaudeAdapter(api_key="test-key")
    call_count = 0
    cm = _make_claude_cm(_make_sdk_tool_response("search_memory", {"query": "test"}, "call_abc"))

    def counting_stream(**kw):
        nonlocal call_count
        call_count += 1
        return cm

    adapter.client.messages.stream = counting_stream

    result = await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-sonnet-4-6",
            system_instruction="test",
            messages=_MESSAGES,
            tools=_TOOLS,
            response_schema={"type": "object", "properties": {"answer": {"type": "string"}}},
        )
    )

    assert call_count == 1, (
        f"Must be exactly one stream call (no force-respond path); got {call_count}"
    )
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "search_memory"


# ============================================================================
# Wire tests: respond-tool safety net (plain-text bypass under tool_choice=auto)
# ============================================================================

def test_strip_nullable_recursive():
    """_strip_nullable removes 'nullable' at every level and touches nothing else."""
    schema = {
        "type": "object",
        "nullable": True,
        "properties": {
            "rich_content": {"type": "object", "nullable": True, "properties": {"x": {"type": "string"}}},
            "items": {"type": "array", "items": {"type": "object", "nullable": True, "properties": {}}},
        },
    }
    out = _strip_nullable(schema)
    assert "nullable" not in out
    assert "nullable" not in out["properties"]["rich_content"]
    assert "nullable" not in out["properties"]["items"]["items"]
    # nothing else injected (no additionalProperties / required)
    assert "additionalProperties" not in out
    assert "additionalProperties" not in out["properties"]["rich_content"]
    assert out["properties"]["rich_content"]["properties"] == {"x": {"type": "string"}}


def test_nullable_to_union_converts_to_type_union():
    """_nullable_to_union translates 'nullable: True' into a JSON-Schema nullable
    'type' union instead of dropping it, at every nesting level, injecting nothing else."""
    schema = {
        "type": "object",
        "required": ["rich_content"],
        "properties": {
            "rich_content": {
                "type": "object",
                "nullable": True,
                "properties": {"x": {"type": "string"}},
            },
            "items": {
                "type": "array",
                "items": {"type": "object", "nullable": True, "properties": {}},
            },
        },
    }
    out = _nullable_to_union(schema)

    # 'nullable' marker is gone everywhere ...
    assert "nullable" not in out["properties"]["rich_content"]
    assert "nullable" not in out["properties"]["items"]["items"]
    # ... translated into a ["<type>", "null"] union (order preserved, "null" appended)
    assert out["properties"]["rich_content"]["type"] == ["object", "null"]
    assert out["properties"]["items"]["items"]["type"] == ["object", "null"]
    # required is preserved unchanged — the field stays required, its value may be null
    assert out["required"] == ["rich_content"]
    # sibling structure and non-nullable nodes are untouched
    assert out["properties"]["rich_content"]["properties"] == {"x": {"type": "string"}}
    assert out["properties"]["items"]["type"] == "array"
    assert "additionalProperties" not in out


def test_nullable_to_union_edge_cases():
    """No concrete 'type' → marker dropped; an existing 'type' list gains 'null' once."""
    # nullable without a type: nothing to unionize, marker simply dropped
    assert _nullable_to_union({"nullable": True, "description": "x"}) == {"description": "x"}
    # type already a union: 'null' appended once, no duplication
    assert _nullable_to_union({"type": ["string", "null"], "nullable": True}) == {
        "type": ["string", "null"]
    }
    assert _nullable_to_union({"type": ["string"], "nullable": True}) == {
        "type": ["string", "null"]
    }


def test_make_strict_shapes_schema_for_strict_tool_use():
    """_make_strict adds additionalProperties:false to every object, drops unsupported
    keywords (maxLength), preserves required + optional properties + type unions."""
    schema = {
        "type": "object",
        "required": ["full_response", "rich_content"],
        "properties": {
            "full_response": {"type": "string"},
            "response_summary": {"type": "string", "maxLength": 300},  # optional + unsupported kw
            "rich_content": {
                "type": ["object", "null"],
                "properties": {
                    "data": {
                        "type": "object",
                        "properties": {"html": {"type": "string"}},  # variant key, optional
                    },
                },
            },
            "link_list": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["url"],
                    "properties": {"url": {"type": "string"}},
                },
            },
        },
    }
    out = _make_strict(schema)

    # additionalProperties:false on every object node
    assert out["additionalProperties"] is False
    assert out["properties"]["rich_content"]["additionalProperties"] is False
    assert out["properties"]["rich_content"]["properties"]["data"]["additionalProperties"] is False
    assert out["properties"]["link_list"]["items"]["additionalProperties"] is False
    # arrays don't get additionalProperties
    assert "additionalProperties" not in out["properties"]["link_list"]
    # unsupported keyword dropped
    assert "maxLength" not in out["properties"]["response_summary"]
    # required preserved; optional properties NOT forced into required
    assert out["required"] == ["full_response", "rich_content"]
    assert "response_summary" not in out["required"]
    # type union preserved
    assert out["properties"]["rich_content"]["type"] == ["object", "null"]


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
async def test_api_status_400_raises_llm_client_error():
    """anthropic.APIStatusError(status_code=400) → LLMClientError(http_status=400).

    400 covers provider credit/billing exhaustion ("credit balance too low") and
    malformed requests. Deterministic → not a failover trigger; surfaced as a typed
    client error so AlertingLLMProxy can push an operator alert."""
    adapter = ClaudeAdapter(api_key="test-key")
    sdk_exc = anthropic.APIStatusError(
        message="Bad request",
        response=MagicMock(status_code=400),
        body={"error": {"type": "invalid_request_error"}},
    )
    adapter.client.messages.stream = lambda **kw: _make_failing_cm(sdk_exc)

    with pytest.raises(LLMClientError) as exc_info:
        await adapter.generate_content(
            request=LLMRequest(
                model_name="claude-sonnet-4-6",
                system_instruction="test",
                messages=_MESSAGES,
            )
        )

    assert exc_info.value.http_status == 400


@pytest.mark.asyncio
async def test_grammar_compilation_timeout_400_raises_llm_server_error():
    """400 "Grammar compilation timed out" → LLMServerError, NOT LLMClientError.

    A grammar-compilation timeout is a 400 by HTTP status but a transient
    server-side fault: Anthropic's constrained-decoding compiler timed out
    building the response_schema, not a malformed request. Classifying it as
    LLMServerError makes it a FAILOVER_TRIGGER_TYPE so Smart is served by another
    provider instead of failing terminally. Regression guard for 2026-06-23,
    where this 400 reached the user as a terminal failure (lost reminder)."""
    adapter = ClaudeAdapter(api_key="test-key")
    sdk_exc = anthropic.APIStatusError(
        message=(
            "Error code: 400 - {'type': 'error', 'error': {'type': "
            "'invalid_request_error', 'message': 'Grammar compilation timed out.'}}"
        ),
        response=MagicMock(status_code=400),
        body={"error": {"type": "invalid_request_error",
                        "message": "Grammar compilation timed out."}},
    )
    adapter.client.messages.stream = lambda **kw: _make_failing_cm(sdk_exc)

    with pytest.raises(LLMServerError) as exc_info:
        await adapter.generate_content(
            request=LLMRequest(
                model_name="claude-sonnet-4-6",
                system_instruction="test",
                messages=_MESSAGES,
            )
        )

    assert exc_info.value.http_status == 400


# ---------------------------------------------------------------------------
# F4.5 Phase 2 — new exception translations
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_asyncio_timeout_translates_to_LLMTimeoutError():
    """asyncio.TimeoutError from our wait_for wrap → LLMTimeoutError. This is
    the wall-clock budget path: caller passed request.timeout, the inner
    stream took too long."""
    adapter = ClaudeAdapter(api_key="test-key")
    adapter.client.messages.stream = lambda **kw: _make_failing_cm(asyncio.TimeoutError())

    with pytest.raises(LLMTimeoutError):
        await adapter.generate_content(
            request=LLMRequest(
                model_name="claude-sonnet-4-6",
                system_instruction="test",
                messages=_MESSAGES,
                timeout=10,
            )
        )


@pytest.mark.asyncio
async def test_sdk_timeout_translates_to_LLMTimeoutError():
    """anthropic.APITimeoutError (SDK-level read timeout, default 120s when
    request.timeout is None) → LLMTimeoutError. Without this branch, default-
    timeout requests would silently bypass the circuit breaker."""
    adapter = ClaudeAdapter(api_key="test-key")
    sdk_exc = anthropic.APITimeoutError(request=MagicMock())
    adapter.client.messages.stream = lambda **kw: _make_failing_cm(sdk_exc)

    with pytest.raises(LLMTimeoutError):
        await adapter.generate_content(
            request=LLMRequest(
                model_name="claude-sonnet-4-6",
                system_instruction="test",
                messages=_MESSAGES,
            )
        )


@pytest.mark.asyncio
async def test_connection_error_translates_to_LLMNetworkError():
    """anthropic.APIConnectionError → LLMNetworkError (TCP/DNS-level failure
    before any HTTP response)."""
    adapter = ClaudeAdapter(api_key="test-key")
    sdk_exc = anthropic.APIConnectionError(request=MagicMock())
    adapter.client.messages.stream = lambda **kw: _make_failing_cm(sdk_exc)

    with pytest.raises(LLMNetworkError):
        await adapter.generate_content(
            request=LLMRequest(
                model_name="claude-sonnet-4-6",
                system_instruction="test",
                messages=_MESSAGES,
            )
        )


@pytest.mark.asyncio
async def test_5xx_non_503_translates_to_LLMServerError():
    """anthropic.APIStatusError(status_code=500) → LLMServerError (500/502/504
    are distinct from 503 maintenance — counted separately by the breaker)."""
    adapter = ClaudeAdapter(api_key="test-key")
    sdk_exc = anthropic.APIStatusError(
        message="Internal server error",
        response=MagicMock(status_code=500),
        body={"error": {"type": "server_error"}},
    )
    adapter.client.messages.stream = lambda **kw: _make_failing_cm(sdk_exc)

    with pytest.raises(LLMServerError) as exc_info:
        await adapter.generate_content(
            request=LLMRequest(
                model_name="claude-sonnet-4-6",
                system_instruction="test",
                messages=_MESSAGES,
            )
        )
    assert exc_info.value.http_status == 500


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


# ---------------------------------------------------------------------------
# Parallel tool turn + file output — the image must nest INTO its tool_result,
# never interleave between two tool_result blocks (Anthropic orphans the later
# tool_use → HTTP 400 "tool_use ids ... without tool_result blocks immediately
# after"). Regression: Smart fan-out (open_file + search_web) on Claude, 2026-07-10.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_parallel_tool_result_file_nests_into_tool_result_not_interleaved():
    """A parallel tool turn where one tool_result carries a binary file (open_file →
    image) must keep the tool_result blocks contiguous and nest the image inside its
    owning tool_result.content — not emit it as a sibling block between the two."""
    from src.domain.llm import ToolCall

    adapter = ClaudeAdapter(api_key="test-key")
    messages = [
        Message(role="user", parts=[MessagePart(text="analyse this")]),
        Message(role="model", parts=[
            MessagePart(tool_call=ToolCall(
                name="delegate_to_specialist", args={"intent": "open_file"},
                thought_signature="toolu_A")),
            MessagePart(tool_call=ToolCall(
                name="delegate_to_specialist", args={"intent": "search_web"},
                thought_signature="toolu_B")),
        ]),
        Message(role="user", parts=[
            MessagePart(tool_response={
                "name": "delegate_to_specialist", "tool_use_id": "toolu_A",
                "response": {"result": "opened IMG_7136.jpg"}}),
            MessagePart(file_data={"base64": "QUJDRA==", "mime_type": "image/jpeg"}),
            MessagePart(tool_response={
                "name": "delegate_to_specialist", "tool_use_id": "toolu_B",
                "response": {"result": "web result"}}),
        ]),
    ]

    result = await adapter._convert_messages(messages)

    tool_turn = result[-1]
    assert tool_turn["role"] == "user"
    content = tool_turn["content"]

    # No sibling image between/after the tool_results — both blocks are tool_result.
    assert [b["type"] for b in content] == ["tool_result", "tool_result"], (
        f"expected two contiguous tool_result blocks, got {[b['type'] for b in content]}"
    )

    # The image nests inside open_file's tool_result (toolu_A), as a list of blocks.
    first, second = content[0], content[1]
    assert first["tool_use_id"] == "toolu_A"
    assert isinstance(first["content"], list)
    assert "image" in [b["type"] for b in first["content"]], (
        f"image must nest into open_file's tool_result, got {first['content']}"
    )
    # The second tool_result is intact and image-free.
    assert second["tool_use_id"] == "toolu_B"
    assert second["content"] == "{'result': 'web result'}"


# ---------------------------------------------------------------------------
# cache_last_message — multi-turn loop caching breakpoint on the last block
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_last_message_off_by_default():
    """Without cache_last_message, no message blocks get cache_control."""
    adapter = ClaudeAdapter(api_key="test-key")
    messages = [
        Message(role="user", parts=[MessagePart(text="initial query")]),
        Message(role="model", parts=[MessagePart(text="here is some context")]),
        Message(role="user", parts=[MessagePart(text="follow-up")]),
    ]

    result = await adapter._convert_messages(messages)

    for msg in result:
        for block in msg["content"]:
            assert "cache_control" not in block, (
                f"unexpected cache_control on {msg['role']} block: {block}"
            )


@pytest.mark.asyncio
async def test_cache_last_message_marks_last_two_user_messages():
    """With cache_last_message=True and ≥2 user messages, BOTH the last
    user message AND the previous user message get cache_control on their
    last content block (sliding-window pattern)."""
    adapter = ClaudeAdapter(api_key="test-key")
    messages = [
        Message(role="user", parts=[MessagePart(text="initial query")]),
        Message(role="model", parts=[MessagePart(text="response with context")]),
        Message(role="user", parts=[MessagePart(text="follow-up question")]),
    ]

    result = await adapter._convert_messages(messages, cache_last_message=True)

    # Assistant message: no cache_control
    assistant_msg = result[1]
    assert assistant_msg["role"] == "assistant"
    for block in assistant_msg["content"]:
        assert "cache_control" not in block

    # First user message: BP_prev → cache_control on last block
    first_user = result[0]
    assert first_user["role"] == "user"
    assert first_user["content"][-1].get("cache_control") == {"type": "ephemeral"}

    # Second user message: BP_new → cache_control on last block
    last_user = result[-1]
    assert last_user["role"] == "user"
    assert last_user["content"][-1].get("cache_control") == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_cache_last_message_single_user_marks_only_one():
    """With only one user message in the history, only that message gets
    cache_control. No 'previous' user message exists to mark."""
    adapter = ClaudeAdapter(api_key="test-key")
    messages = [
        Message(role="user", parts=[MessagePart(text="single turn query")]),
    ]

    result = await adapter._convert_messages(messages, cache_last_message=True)

    assert len(result) == 1
    assert result[0]["role"] == "user"
    assert result[0]["content"][-1].get("cache_control") == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_cache_last_message_three_user_messages_marks_only_two_latest():
    """With 3 user messages, only the LAST two get cache_control. The first
    user message remains unmarked — its cache write (from earlier turns)
    is reachable through Anthropic's automatic backward lookback."""
    adapter = ClaudeAdapter(api_key="test-key")
    messages = [
        Message(role="user", parts=[MessagePart(text="user(1)")]),
        Message(role="model", parts=[MessagePart(text="model(1)")]),
        Message(role="user", parts=[MessagePart(text="user(2)")]),
        Message(role="model", parts=[MessagePart(text="model(2)")]),
        Message(role="user", parts=[MessagePart(text="user(3)")]),
    ]

    result = await adapter._convert_messages(messages, cache_last_message=True)

    user_msgs = [m for m in result if m["role"] == "user"]
    assert len(user_msgs) == 3

    # First user: NO cache_control (lookback finds it)
    assert "cache_control" not in user_msgs[0]["content"][-1]
    # Second user: BP_prev
    assert user_msgs[1]["content"][-1].get("cache_control") == {"type": "ephemeral"}
    # Third user: BP_new
    assert user_msgs[2]["content"][-1].get("cache_control") == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_cache_last_message_marks_last_block_when_message_has_multiple_parts():
    """When the last message has multiple content blocks (e.g. several
    text parts), only the FINAL one gets cache_control."""
    adapter = ClaudeAdapter(api_key="test-key")
    messages = [
        Message(role="user", parts=[MessagePart(text="query")]),
        Message(role="model", parts=[MessagePart(text="response")]),
        Message(
            role="user",
            parts=[
                MessagePart(text="first part"),
                MessagePart(text="second part"),
                MessagePart(text="third (final) part"),
            ],
        ),
    ]

    result = await adapter._convert_messages(messages, cache_last_message=True)

    last_content = result[-1]["content"]
    assert len(last_content) == 3

    # First two: no cache_control
    assert "cache_control" not in last_content[0]
    assert "cache_control" not in last_content[1]

    # Last one: cache_control
    assert last_content[-1].get("cache_control") == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_cache_last_message_empty_messages_does_not_raise():
    """Edge case: empty messages list with cache_last_message=True is a no-op."""
    adapter = ClaudeAdapter(api_key="test-key")
    result = await adapter._convert_messages([], cache_last_message=True)
    assert result == []


# ============================================================================
# tool_use_id resolution — explicit id (carried by build_tool_turn) vs the
# legacy backward name search. Regression coverage for the 2026-06-29 Claude
# 400 "unexpected tool_use_id in tool_result blocks" incident.
# ============================================================================

from src.ports.llm_port import ToolCall  # noqa: E402


class _Block:
    """SDK-like content block (mirrors anthropic types as accessed by the adapter)."""
    def __init__(self, type, id=None, name=None, input=None):
        self.type = type
        self.id = id
        self.name = name
        self.input = input or {}


class _RawContent:
    """SDK-like Message object: only `.content` is read by the adapter."""
    def __init__(self, blocks):
        self.content = blocks


def _tool_result_blocks(converted_msg):
    return [b for b in converted_msg["content"] if b.get("type") == "tool_result"]


@pytest.mark.asyncio
async def test_tool_result_uses_explicit_tool_use_id():
    """A tool_response carrying an explicit tool_use_id is serialized to that exact
    id — independent of the tool name. The assistant turn here is raw_content with a
    tool_use named 'delegate_to_specialist'; the tool_response name diverges
    ('search_web', as a fan-out result might), which would defeat the legacy
    name-based backward search. The explicit id keeps the pairing correct."""
    adapter = ClaudeAdapter(api_key="test-key")
    messages = [
        Message(
            role="model",
            parts=[MessagePart(tool_call=ToolCall(
                name="delegate_to_specialist", args={"intent": "search_web"},
                thought_signature="toolu_RIGHT"))],
            raw_content=_RawContent([
                _Block("tool_use", id="toolu_RIGHT", name="delegate_to_specialist",
                       input={"intent": "search_web"}),
            ]),
        ),
        Message(role="user", parts=[MessagePart(
            tool_response={"name": "search_web", "tool_use_id": "toolu_RIGHT",
                           "response": {"result": "..."}})]),
    ]

    result = await adapter._convert_messages(messages)

    tr = _tool_result_blocks(result[1])
    assert len(tr) == 1
    assert tr[0]["tool_use_id"] == "toolu_RIGHT"


@pytest.mark.asyncio
async def test_explicit_id_takes_precedence_over_name_search():
    """When both an explicit id and a name-search candidate exist and DIFFER, the
    explicit id wins. Two same-named tool turns: the legacy search would resolve
    the latest result to the most-recent tool_use; the explicit id pins it to the
    correct (older) one."""
    adapter = ClaudeAdapter(api_key="test-key")
    messages = [
        Message(role="model", parts=[MessagePart(tool_call=ToolCall(
            name="X", args={}, thought_signature="toolu_OLD"))]),
        Message(role="user", parts=[MessagePart(
            tool_response={"name": "X", "tool_use_id": "toolu_OLD",
                           "response": {"result": "old"}})]),
        Message(role="model", parts=[MessagePart(tool_call=ToolCall(
            name="X", args={}, thought_signature="toolu_NEW"))]),
        # This result explicitly belongs to the OLDER tool_use; name search would
        # have grabbed toolu_NEW (most recent same-name).
        Message(role="user", parts=[MessagePart(
            tool_response={"name": "X", "tool_use_id": "toolu_OLD",
                           "response": {"result": "late"}})]),
    ]

    result = await adapter._convert_messages(messages)

    assert _tool_result_blocks(result[3])[0]["tool_use_id"] == "toolu_OLD"


@pytest.mark.asyncio
async def test_legacy_tool_response_without_id_falls_back_to_name_search():
    """Backward compat: a tool_response with no explicit id (history built before
    this change) still resolves via the legacy backward name search."""
    adapter = ClaudeAdapter(api_key="test-key")
    messages = [
        Message(role="model", parts=[MessagePart(tool_call=ToolCall(
            name="search_memory", args={}, thought_signature="toolu_LEGACY"))]),
        Message(role="user", parts=[MessagePart(
            tool_response={"name": "search_memory", "response": {"result": "r"}})]),
    ]

    result = await adapter._convert_messages(messages)

    assert _tool_result_blocks(result[1])[0]["tool_use_id"] == "toolu_LEGACY"


@pytest.mark.asyncio
async def test_diagnostic_logs_on_orphaned_tool_use_id(monkeypatch):
    """The diagnostic pass logs a detailed ERROR when a tool_result id has no
    matching tool_use in the immediately-preceding message (the exact condition
    Anthropic rejects with a 400), so a recurrence is debuggable from one log line."""
    import src.adapters.claude_adapter as mod
    errors = []
    fake_logger = MagicMock()
    fake_logger.error = lambda *a, **k: errors.append(a)
    monkeypatch.setattr(mod, "logger", fake_logger)

    adapter = ClaudeAdapter(api_key="test-key")
    messages = [
        Message(role="model", parts=[MessagePart(tool_call=ToolCall(
            name="X", args={}, thought_signature="toolu_A"))]),
        Message(role="user", parts=[MessagePart(
            tool_response={"name": "X", "tool_use_id": "toolu_ORPHAN",
                           "response": {"result": "r"}})]),
    ]

    await adapter._convert_messages(messages)

    assert errors, "expected an ERROR log for the orphaned tool_use_id"
    logged = " ".join(str(x) for a in errors for x in a)
    assert "toolu_ORPHAN" in logged
    assert "mismatch" in logged.lower()


@pytest.mark.asyncio
async def test_no_diagnostic_when_pairing_is_valid(monkeypatch):
    """No ERROR is logged when every tool_result id maps to a tool_use in the
    previous message."""
    import src.adapters.claude_adapter as mod
    errors = []
    fake_logger = MagicMock()
    fake_logger.error = lambda *a, **k: errors.append(a)
    monkeypatch.setattr(mod, "logger", fake_logger)

    adapter = ClaudeAdapter(api_key="test-key")
    messages = [
        Message(role="model", parts=[MessagePart(tool_call=ToolCall(
            name="X", args={}, thought_signature="toolu_A"))]),
        Message(role="user", parts=[MessagePart(
            tool_response={"name": "X", "tool_use_id": "toolu_A",
                           "response": {"result": "r"}})]),
    ]

    await adapter._convert_messages(messages)

    assert not errors


# ============================================================================
# Error mapping: overloaded_error (HTTP 529) classification
#
# overloaded_error can arrive mid-stream as an SSE error event after the HTTP
# connection already returned 200, so the SDK's APIStatusError carries no
# status_code. The adapter must still classify it as LLMServerError(529) so it
# lands in FAILOVER_TRIGGER_TYPES and provider failover engages — instead of
# bubbling raw and degrading Smart → Quick. See claude_adapter.generate_content.
# ============================================================================


class _FakeAPIStatusError(anthropic.APIStatusError):
    """anthropic.APIStatusError with controllable status_code/message.

    Bypasses the SDK's httpx-bound __init__; the adapter only reads
    ``.status_code`` and ``str(e)`` when classifying the error.
    """

    def __init__(self, message, status_code=None):
        Exception.__init__(self, message)
        self.status_code = status_code


def _make_raising_cm(exc):
    """Stream context manager whose get_final_message raises `exc` (mid-stream)."""
    stream = AsyncMock()
    stream.get_final_message = AsyncMock(side_effect=exc)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=stream)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


@pytest.mark.asyncio
async def test_overloaded_error_midstream_no_status_maps_to_server_error():
    """overloaded_error with status_code=None (mid-stream) → LLMServerError(529)."""
    adapter = ClaudeAdapter(api_key="test-key")
    exc = _FakeAPIStatusError(
        "{'type': 'error', 'error': {'type': 'overloaded_error', "
        "'message': 'Overloaded'}}",
        status_code=None,
    )
    adapter.client.messages.stream = lambda **kwargs: _make_raising_cm(exc)

    with pytest.raises(LLMServerError) as exc_info:
        await adapter.generate_content(
            request=LLMRequest(model_name="claude-sonnet-4-6", messages=_MESSAGES)
        )
    assert exc_info.value.http_status == 529


@pytest.mark.asyncio
async def test_overloaded_error_with_529_status_maps_to_server_error():
    """overloaded_error carrying status_code=529 → LLMServerError(529)."""
    adapter = ClaudeAdapter(api_key="test-key")
    exc = _FakeAPIStatusError("Overloaded", status_code=529)
    adapter.client.messages.stream = lambda **kwargs: _make_raising_cm(exc)

    with pytest.raises(LLMServerError) as exc_info:
        await adapter.generate_content(
            request=LLMRequest(model_name="claude-sonnet-4-6", messages=_MESSAGES)
        )
    assert exc_info.value.http_status == 529


@pytest.mark.asyncio
async def test_grounded_loop_overloaded_error_maps_to_server_error():
    """Grounding path classifies mid-stream overloaded_error as LLMServerError(529)."""
    adapter = ClaudeAdapter(api_key="test-key")
    exc = _FakeAPIStatusError(
        "{'error': {'type': 'overloaded_error'}}", status_code=None
    )
    adapter.client.messages.stream = lambda **kwargs: _make_raising_cm(exc)

    with pytest.raises(LLMServerError) as exc_info:
        await adapter.generate_content(
            request=LLMRequest(
                model_name="claude-sonnet-4-6",
                messages=_MESSAGES,
                use_grounding=True,
            )
        )
    assert exc_info.value.http_status == 529


@pytest.mark.asyncio
async def test_grounded_loop_plain_5xx_maps_to_server_error():
    """Grounding path: a plain 5xx (no overloaded marker) → LLMServerError(status)."""
    adapter = ClaudeAdapter(api_key="test-key")
    exc = _FakeAPIStatusError("Internal Server Error", status_code=500)
    adapter.client.messages.stream = lambda **kwargs: _make_raising_cm(exc)

    with pytest.raises(LLMServerError) as exc_info:
        await adapter.generate_content(
            request=LLMRequest(
                model_name="claude-sonnet-4-6",
                messages=_MESSAGES,
                use_grounding=True,
            )
        )
    assert exc_info.value.http_status == 500


# ============================================================================
# Sonnet 5: sampling gate + same-provider model fallback
# ============================================================================


def _capturing_stream(captured, cm):
    def _stream(**kwargs):
        captured.update(kwargs)
        return cm
    return _stream


@pytest.mark.asyncio
async def test_temperature_omitted_for_sonnet_5():
    """Sonnet 5 rejects a non-default temperature (400) → adapter must not send it."""
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    adapter.client.messages.stream = _capturing_stream(captured, _make_claude_cm(_make_sdk_response()))
    await adapter.generate_content(
        request=LLMRequest(model_name="claude-sonnet-5", messages=_MESSAGES, temperature=0.7)
    )
    assert "temperature" not in captured, (
        f"Sonnet 5 must not receive a temperature param, got {captured.get('temperature')!r}"
    )


@pytest.mark.asyncio
async def test_temperature_present_for_sonnet_4_6():
    """Sonnet 4.6 still accepts temperature — the gate must not strip it."""
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    adapter.client.messages.stream = _capturing_stream(captured, _make_claude_cm(_make_sdk_response()))
    await adapter.generate_content(
        request=LLMRequest(model_name="claude-sonnet-4-6", messages=_MESSAGES, temperature=0.7)
    )
    assert captured.get("temperature") == 0.7


@pytest.mark.asyncio
async def test_temperature_omitted_for_opus_4_8():
    """Opus 4.7/4.8 are also sampling-restricted — temperature must be omitted (latent-bug fix)."""
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    adapter.client.messages.stream = _capturing_stream(captured, _make_claude_cm(_make_sdk_response()))
    await adapter.generate_content(
        request=LLMRequest(model_name="claude-opus-4-8", messages=_MESSAGES, temperature=0.5)
    )
    assert "temperature" not in captured


@pytest.mark.asyncio
async def test_sonnet_5_falls_back_to_sonnet_4_6_on_transient():
    """A transient 529 on Sonnet 5 retries once on Sonnet 4.6 (same provider)."""
    adapter = ClaudeAdapter(api_key="test-key")
    models_tried = []
    exc = _FakeAPIStatusError(
        "{'type':'error','error':{'type':'overloaded_error','message':'Overloaded'}}",
        status_code=None,
    )

    def stream(**kwargs):
        models_tried.append(kwargs.get("model"))
        if kwargs.get("model") == "claude-sonnet-5":
            return _make_raising_cm(exc)
        return _make_claude_cm(_make_sdk_response("recovered"))

    adapter.client.messages.stream = stream
    result = await adapter.generate_content(
        request=LLMRequest(model_name="claude-sonnet-5", messages=_MESSAGES)
    )
    assert models_tried == ["claude-sonnet-5", "claude-sonnet-4-6"]
    assert result.text == "recovered"


@pytest.mark.asyncio
async def test_sonnet_5_no_fallback_on_4xx():
    """A 400 (real incompatibility) must NOT fall back — surfaces as LLMClientError."""
    adapter = ClaudeAdapter(api_key="test-key")
    models_tried = []
    exc = _FakeAPIStatusError("bad request", status_code=400)

    def stream(**kwargs):
        models_tried.append(kwargs.get("model"))
        return _make_raising_cm(exc)

    adapter.client.messages.stream = stream
    with pytest.raises(LLMClientError):
        await adapter.generate_content(
            request=LLMRequest(model_name="claude-sonnet-5", messages=_MESSAGES)
        )
    assert models_tried == ["claude-sonnet-5"], "Must not retry on a 4xx"


@pytest.mark.asyncio
async def test_sonnet_5_disables_thinking_when_no_effort():
    """Sonnet 5 thinks by default when thinking is omitted → adapter sends {'type':'disabled'}."""
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    adapter.client.messages.stream = _capturing_stream(captured, _make_claude_cm(_make_sdk_response()))
    await adapter.generate_content(
        request=LLMRequest(model_name="claude-sonnet-5", messages=_MESSAGES)  # no thinking effort
    )
    assert captured.get("thinking") == {"type": "disabled"}


@pytest.mark.asyncio
async def test_sonnet_4_6_no_thinking_key_when_no_effort():
    """Sonnet 4.6 stays off when thinking omitted — no 'thinking' key sent (unchanged)."""
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}
    adapter.client.messages.stream = _capturing_stream(captured, _make_claude_cm(_make_sdk_response()))
    await adapter.generate_content(
        request=LLMRequest(model_name="claude-sonnet-4-6", messages=_MESSAGES)
    )
    assert "thinking" not in captured
