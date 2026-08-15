"""
Unit tests for GrokAdapter.

Pattern: mock adapter.client at the SDK boundary (AsyncOpenAI), capture
what kwargs are sent to client.responses.create(), assert on them.
This tests the translation layer (LLMRequest → SDK call), not business logic.

Boundary moved from chat.completions.create to responses.create on 2026-08-14:
xAI serves server-side search only on /v1/responses (chat/completions returns
HTTP 410 for search_parameters and 422 for a web_search tool).
"""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
import openai

from src.adapters.grok_adapter import GrokAdapter
from src.domain.exceptions import (
    LLMClientError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from src.domain.user import PerformanceTier
from src.domain.llm import PROMPT_CACHE_BOUNDARY, USER_TURN_SYSTEM_ANCHOR
from src.ports.llm_port import (
    LLMRequest,
    Message,
    MessagePart,
    PromptCacheConfig,
)


# ============================================================================
# Test fixtures and response helpers
# ============================================================================

MESSAGES = [Message(role="user", parts=[MessagePart(text="Hi")])]
TOOLS = [
    {
        "name": "search_memory",
        "description": "Search memory",
        "parameters": {"type": "object", "properties": {}},
    }
]


def _usage(input_tokens=10, output_tokens=5, cached_tokens=0):
    """Responses API usage block."""
    details = MagicMock()
    details.cached_tokens = cached_tokens

    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    usage.total_tokens = input_tokens + output_tokens
    usage.input_tokens_details = details
    return usage


def _message_item(text="OK"):
    """A Responses API `message` output item."""
    block = MagicMock()
    block.type = "output_text"
    block.text = text

    item = MagicMock()
    item.type = "message"
    item.content = [block]
    return item


def _reasoning_item(text="thinking hard"):
    """A Responses API `reasoning` output item (xAI returns summary[].text)."""
    summary_block = MagicMock()
    summary_block.type = "summary_text"
    summary_block.text = text

    item = MagicMock()
    item.type = "reasoning"
    item.summary = [summary_block]
    return item


def _function_call_item(name, args, call_id="call_1"):
    """A Responses API `function_call` output item."""
    item = MagicMock()
    item.type = "function_call"
    item.name = name
    item.arguments = json.dumps(args)
    item.call_id = call_id
    return item


def _make_response(text="OK", output=None, usage=None):
    """Minimal OpenAI Responses-API-like mock."""
    response = MagicMock()
    response.output_text = text
    response.output = output if output is not None else [_message_item(text)]
    response.usage = usage if usage is not None else _usage()
    return response


def _make_response_with_tool(name, args, call_id="call_1"):
    """Response carrying one function_call."""
    return _make_response(
        text="",
        output=[_function_call_item(name, args, call_id)],
    )


def _install(adapter, response=None, captured=None):
    """Mock the SDK boundary; optionally capture the kwargs sent to it."""
    async def mock_create(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        return response if response is not None else _make_response()

    adapter.client = MagicMock()
    adapter.client.responses.create = mock_create
    return adapter


# ============================================================================
# Capabilities and tier mapping
# ============================================================================

def test_grok_capabilities():
    adapter = GrokAdapter(api_key="test-key")
    caps = adapter.get_capabilities()

    assert caps.native_tools is True
    # False means "no controllable caching" — xAI caches automatically with no API
    # surface for it, so PromptCacheStrategy has no breakpoint to place.
    assert caps.context_caching is False
    # Verified live: input_image is accepted on grok-4.6 and grok-4.3 (min 8x8 px).
    assert caps.vision is True
    assert caps.supports_json_mode is True
    assert caps.native_grounding is True
    # grok-4.6 = 500k, grok-4.3 = 1M; the conservative value must not over-promise.
    assert caps.max_context_window == 500_000


def test_grok_model_for_tier():
    adapter = GrokAdapter(api_key="test-key")

    # Must be IDs that GET /v1/models actually lists. Retired IDs do not fail loudly —
    # xAI serves grok-4.3 for them and we mis-bill.
    assert adapter.get_model_for_tier(PerformanceTier.ECO) == "grok-4.3"
    assert adapter.get_model_for_tier(PerformanceTier.BALANCED) == "grok-4.3"
    assert adapter.get_model_for_tier(PerformanceTier.PERFORMANCE) == "grok-4.6"


def test_grok_unsupported_tier_raises():
    adapter = GrokAdapter(api_key="test-key")

    with pytest.raises(ValueError, match="Unsupported tier"):
        adapter.get_model_for_tier("invalid_tier")


def test_api_key_whitespace_is_stripped():
    """A trailing newline in the key makes httpx reject the auth header, which
    surfaces as a bogus 'connection error'. The adapter must never pass it on."""
    adapter = GrokAdapter(api_key="xai-secret\n")

    assert adapter.api_key == "xai-secret"


@pytest.mark.asyncio
async def test_grok_prompt_caching_is_ignored_not_fatal():
    """An enabled cache_config must NOT abort the request.

    It used to raise ValueError, killing the whole agent execution over a hint the
    adapter can simply drop: cache_config asks for explicit Anthropic-style
    breakpoints, xAI exposes no control for them, and xAI caches automatically
    regardless. The request goes through; no cache_control reaches the SDK.
    """
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    response = await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4.6",
            messages=MESSAGES,
            cache_config=PromptCacheConfig(enabled=True),
        )
    )

    assert response is not None
    assert captured["model"] == "grok-4.6"
    assert "cache_control" not in captured
    assert "prompt_cache_retention" not in captured


# ============================================================================
# Wire tests: verify SDK call kwargs
# ============================================================================

@pytest.mark.asyncio
async def test_force_tool_use_sends_tool_choice_required():
    """force_tool_use=True + tools → tool_choice='required' in SDK call."""
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4.6",
            messages=MESSAGES,
            tools=TOOLS,
            force_tool_use=True,
        )
    )

    assert captured.get("tool_choice") == "required", (
        f"Expected tool_choice='required', got {captured.get('tool_choice')!r}"
    )


@pytest.mark.asyncio
async def test_no_force_tool_use_sends_tool_choice_auto():
    """force_tool_use=False + tools → tool_choice='auto'."""
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4.6",
            messages=MESSAGES,
            tools=TOOLS,
            force_tool_use=False,
        )
    )

    assert captured.get("tool_choice") == "auto"


@pytest.mark.asyncio
async def test_no_tools_omits_tool_choice():
    """No tools → tool_choice must be absent (Grok rejects it with empty tools)."""
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4.6",
            messages=MESSAGES,
            force_tool_use=True,  # even with force=True, no tools → no tool_choice
        )
    )

    assert "tool_choice" not in captured, (
        f"tool_choice must be absent when tools is empty; keys={list(captured.keys())}"
    )


@pytest.mark.asyncio
async def test_use_grounding_injects_web_search():
    """use_grounding=True → xAI's server-side web_search prepended to tools.

    Only web_search: the old injection also sent {"type": "web_fetch"}, which
    /v1/responses rejects (HTTP 422, "unknown variant").
    """
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4.6",
            messages=MESSAGES,
            use_grounding=True,
        )
    )

    tools = captured.get("tools") or []
    tool_types = [t.get("type") for t in tools if isinstance(t, dict)]
    assert "web_search" in tool_types, f"web_search missing; tools={tools}"
    assert tool_types[0] == "web_search", "native search must be prepended"


@pytest.mark.asyncio
async def test_grounding_keeps_custom_tools_after_web_search():
    """Grounding must not displace the agent's own tools."""
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4.6",
            messages=MESSAGES,
            tools=TOOLS,
            use_grounding=True,
        )
    )

    tools = captured.get("tools") or []
    assert [t.get("type") for t in tools] == ["web_search", "function"]
    assert tools[1]["name"] == "search_memory"


@pytest.mark.asyncio
async def test_json_mode_via_response_schema():
    """A dict response_schema is forwarded natively as json_schema (strict=False).

    json_object would send no schema at all, leaving the model to guess structure
    from the prompt — that is why the schema must reach the API.
    """
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, response=_make_response(text='{"answer": "42"}'), captured=captured)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4.6",
            messages=MESSAGES,
            response_schema={"type": "OBJECT", "properties": {"a": {"type": "STRING"}}},
        )
    )

    fmt = captured.get("text", {}).get("format", {})
    assert fmt.get("type") == "json_schema"
    assert fmt.get("strict") is False
    # Gemini-native uppercase types must be folded, or the validator rejects them.
    assert fmt["schema"] == {"type": "object", "properties": {"a": {"type": "string"}}}


@pytest.mark.asyncio
async def test_json_mode_via_mime_type():
    """response_mime_type='application/json' → text={'format': {'type':'json_object'}}."""
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, response=_make_response(text='{"ok": true}'), captured=captured)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4.6",
            messages=MESSAGES,
            response_mime_type="application/json",
        )
    )

    assert captured.get("text") == {"format": {"type": "json_object"}}


@pytest.mark.asyncio
async def test_no_json_mode_by_default():
    """No schema and no mime type → text format absent from kwargs."""
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4.6",
            messages=MESSAGES,
        )
    )

    assert "text" not in captured


@pytest.mark.asyncio
async def test_schema_plus_tools_synthesizes_terminal_tool():
    """Structured output and function calling compete on Grok: with a text format the
    model fills the answer field with a promise and calls nothing (measured 9/15 vs
    15/15). So the answer moves onto a synthesized deliver_response tool instead."""
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4.6",
            messages=MESSAGES,
            tools=TOOLS,
            response_schema={"type": "object", "properties": {"full_response": {"type": "string"}}},
        )
    )

    names = [t.get("name") for t in captured["tools"]]
    assert names == ["search_memory", "deliver_response"]
    # The answer channel is now a tool, so a bare text turn would deliver nothing.
    assert captured["tool_choice"] == "required"
    assert "text" not in captured, "no text format when the terminal tool carries structure"

    terminal = captured["tools"][-1]
    assert terminal["parameters"] == {
        "type": "object", "properties": {"full_response": {"type": "string"}}
    }


@pytest.mark.asyncio
async def test_schema_without_tools_keeps_text_format():
    """No tools → nothing to compete with, so the native json_schema path stays."""
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4.6",
            messages=MESSAGES,
            response_schema={"type": "object"},
        )
    )

    assert captured["text"]["format"]["type"] == "json_schema"
    assert "tools" not in captured


@pytest.mark.asyncio
async def test_grounding_does_not_synthesize_terminal_tool():
    """Grounding already drops the format; do not stack a forced tool call on top."""
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4.6",
            messages=MESSAGES,
            tools=TOOLS,
            response_schema={"type": "object"},
            use_grounding=True,
        )
    )

    names = [t.get("type") if "name" not in t else t["name"] for t in captured["tools"]]
    assert "deliver_response" not in names
    assert captured["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_grounding_suppresses_json_mode():
    """Search and JSON mode conflict — grounding wins, format is dropped."""
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4.6",
            messages=MESSAGES,
            response_mime_type="application/json",
            use_grounding=True,
        )
    )

    assert "text" not in captured


@pytest.mark.asyncio
async def test_thinking_maps_to_reasoning_effort():
    """thinking='low' → reasoning={'effort': 'low'}."""
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4.6",
            messages=MESSAGES,
            thinking="low",
        )
    )

    assert captured.get("reasoning") == {"effort": "low"}


@pytest.mark.asyncio
async def test_no_thinking_omits_reasoning():
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(model_name="grok-4.6", messages=MESSAGES)
    )

    assert "reasoning" not in captured


@pytest.mark.asyncio
async def test_tool_calls_parsed_from_response():
    """function_call output item → LLMResponse.tool_calls populated correctly."""
    adapter = GrokAdapter(api_key="test-key")
    _install(
        adapter,
        response=_make_response_with_tool("search_memory", {"query": "test"}, "call_abc"),
    )

    response = await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4.6",
            messages=MESSAGES,
            tools=TOOLS,
        )
    )

    assert len(response.tool_calls) == 1
    tc = response.tool_calls[0]
    assert tc.name == "search_memory"
    assert tc.args == {"query": "test"}
    assert tc.thought_signature == "call_abc"


@pytest.mark.asyncio
async def test_system_instruction_sent_as_instructions():
    """system_instruction → the separate `instructions` parameter.

    Responses API has no system message in the input list; the Chat Completions
    era prepended {"role": "system"} instead.
    """
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4.6",
            system_instruction="You are a helpful assistant.",
            messages=MESSAGES,
        )
    )

    assert captured.get("instructions") == "You are a helpful assistant."
    roles = [i.get("role") for i in captured.get("input", []) if isinstance(i, dict)]
    assert "system" not in roles


@pytest.mark.asyncio
async def test_turn_anchor_lifted_into_developer_item():
    """Role precedence on xAI is developer > instructions > user (measured 2026-08-15,
    both orderings). Left in the user turn the anchor sits in the weakest channel."""
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4.6",
            messages=[Message(role="user", parts=[
                MessagePart(text=f"{USER_TURN_SYSTEM_ANCHOR}\n\nWhat is the sea like?"),
            ])],
        )
    )

    items = captured["input"]
    assert items[0]["role"] == "developer"
    assert items[0]["content"] == USER_TURN_SYSTEM_ANCHOR
    # ...and it must be gone from the user turn, not duplicated into both channels.
    user_items = [i for i in items if i.get("role") == "user"]
    assert USER_TURN_SYSTEM_ANCHOR not in str(user_items)
    assert "What is the sea like?" in str(user_items)


@pytest.mark.asyncio
async def test_no_developer_item_without_anchor():
    """Plain turns must not grow an empty developer channel."""
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(model_name="grok-4.6", messages=MESSAGES)
    )

    assert [i.get("role") for i in captured["input"]] == ["user"]


def test_anchor_extraction_only_touches_the_last_user_turn():
    """An anchor left in history from an earlier turn stays where it is."""
    adapter = GrokAdapter(api_key="test-key")
    messages = [
        Message(role="user", parts=[MessagePart(text=f"{USER_TURN_SYSTEM_ANCHOR}\n\nold")]),
        Message(role="model", parts=[MessagePart(text="reply")]),
        Message(role="user", parts=[MessagePart(text=f"{USER_TURN_SYSTEM_ANCHOR}\n\nnew")]),
    ]

    cleaned, anchor = adapter._extract_turn_anchor(messages)

    assert anchor == USER_TURN_SYSTEM_ANCHOR
    assert cleaned[-1].parts[0].text == "new"
    assert USER_TURN_SYSTEM_ANCHOR in cleaned[0].parts[0].text


@pytest.mark.asyncio
async def test_reasoning_summary_captured_as_thought_text():
    """Reasoning is billed as output, so it is captured — but never as answer text."""
    adapter = GrokAdapter(api_key="test-key")
    _install(
        adapter,
        response=_make_response(
            text="ok",
            output=[_reasoning_item("weighing options"), _message_item("ok")],
        ),
    )

    response = await adapter.generate_content(
        request=LLMRequest(model_name="grok-4.6", messages=MESSAGES)
    )

    assert response.thought_text == "weighing options"
    assert response.text == "ok"


@pytest.mark.asyncio
async def test_cached_tokens_are_subtracted_from_prompt_tokens():
    """prompt_tokens holds UNCACHED input only; the cached leg is priced separately."""
    adapter = GrokAdapter(api_key="test-key")
    _install(
        adapter,
        response=_make_response(usage=_usage(input_tokens=100, output_tokens=20, cached_tokens=80)),
    )

    response = await adapter.generate_content(
        request=LLMRequest(model_name="grok-4.6", messages=MESSAGES)
    )

    usage = response.usage_metadata
    assert usage.prompt_tokens == 20
    assert usage.cache_read_tokens == 80
    assert usage.completion_tokens == 20


@pytest.mark.asyncio
async def test_cache_boundary_marker_is_stripped_from_instructions():
    """PROMPT_CACHE_BOUNDARY is an Anthropic-only cut point. Every other adapter strips
    it; leaving it in sends a literal '<!-- CACHE_BOUNDARY -->' to the model."""
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4.6",
            system_instruction=f"static head{PROMPT_CACHE_BOUNDARY}dynamic tail",
            messages=MESSAGES,
        )
    )

    assert PROMPT_CACHE_BOUNDARY not in captured["instructions"]
    assert "static head" in captured["instructions"]
    assert "dynamic tail" in captured["instructions"]


@pytest.mark.asyncio
async def test_prompt_cache_key_derives_from_static_prefix_only():
    """Two requests sharing a static head must share a cache key even when the
    dynamic tail differs — that is the whole point of routing to a warm copy."""
    adapter = GrokAdapter(api_key="test-key")
    keys = []
    for tail in ("tail one", "tail two"):
        captured = {}
        _install(adapter, captured=captured)
        await adapter.generate_content(
            request=LLMRequest(
                model_name="grok-4.6",
                system_instruction=f"same head{PROMPT_CACHE_BOUNDARY}{tail}",
                messages=MESSAGES,
            )
        )
        keys.append(captured.get("prompt_cache_key"))

    assert keys[0] and keys[0].startswith("alek-")
    assert keys[0] == keys[1]


@pytest.mark.asyncio
async def test_image_file_data_sent_as_input_image():
    """Grok accepts images; a base64 image part must reach the API as input_image."""
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4.6",
            messages=[Message(role="user", parts=[
                MessagePart(text="what is this?"),
                MessagePart(file_data={"mime_type": "image/png", "base64": "QUJD"}),
            ])],
        )
    )

    content = captured["input"][0]["content"]
    kinds = [c.get("type") for c in content]
    assert "input_image" in kinds
    img = next(c for c in content if c["type"] == "input_image")
    assert img["image_url"] == "data:image/png;base64,QUJD"


@pytest.mark.asyncio
async def test_url_citations_appended_as_sources_block():
    """Grounded answers must expose their URLs in a predictable machine-readable place."""
    adapter = GrokAdapter(api_key="test-key")

    ann = MagicMock()
    ann.type = "url_citation"
    ann.url = "https://x.ai/"
    ann.title = "xAI"
    block = MagicMock()
    block.type = "output_text"
    block.text = "Grok 4.6 is newest."
    block.annotations = [ann, ann]  # duplicate must be collapsed
    item = MagicMock()
    item.type = "message"
    item.content = [block]

    _install(adapter, response=_make_response(text="Grok 4.6 is newest.", output=[item]))

    response = await adapter.generate_content(
        request=LLMRequest(model_name="grok-4.6", messages=MESSAGES, use_grounding=True)
    )

    assert "*Sources:*" in response.text
    assert response.text.count("https://x.ai/") == 1


@pytest.mark.asyncio
async def test_store_enabled_for_dashboard_debugging():
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(model_name="grok-4.6", messages=MESSAGES)
    )

    assert captured["store"] is True


@pytest.mark.asyncio
async def test_truncated_tool_args_do_not_raise():
    """A tool call cut off mid-JSON must surface as a flagged payload, not an exception:
    the turn still carries usable context and the engine can report the failure."""
    adapter = GrokAdapter(api_key="test-key")
    broken = _function_call_item("search_memory", {}, "call_1")
    broken.arguments = '{"query": "unterminat'
    _install(adapter, response=_make_response(text="", output=[broken]))

    response = await adapter.generate_content(
        request=LLMRequest(model_name="grok-4.6", messages=MESSAGES, tools=TOOLS)
    )

    assert response.tool_calls[0].args["_parse_error"] == "truncated_json"
    assert response.tool_calls[0].args["_raw_prefix"] == '{"query": "unterminat'


@pytest.mark.asyncio
async def test_server_side_tool_calls_are_not_surfaced_as_tool_calls():
    """web_search runs inside xAI and is billed as input tokens. It must never reach
    the agent as a ToolCall — the engine would try to dispatch it to a specialist."""
    adapter = GrokAdapter(api_key="test-key")
    search = MagicMock()
    search.type = "web_search_call"
    _install(adapter, response=_make_response(text="answer", output=[search, _message_item("answer")]))

    response = await adapter.generate_content(
        request=LLMRequest(model_name="grok-4.6", messages=MESSAGES, use_grounding=True)
    )

    assert response.tool_calls == []
    assert response.text == "answer"


@pytest.mark.asyncio
async def test_upload_file_raises_not_implemented():
    """Grok does not support vision — upload_file must raise NotImplementedError."""
    adapter = GrokAdapter(api_key="test-key")

    with pytest.raises(NotImplementedError):
        await adapter.upload_file("/some/path.jpg", "image/jpeg")


# ---------------------------------------------------------------------------
# GCS reference file_data — graceful handling
# ---------------------------------------------------------------------------

def test_gcs_ref_file_data_no_error():
    """file_data with 'ref' key should not raise — it's a GCS reference with no binary."""
    adapter = GrokAdapter(api_key="test-key")
    messages = [
        Message(role="user", parts=[
            MessagePart(text='[File: "report.docx" (45KB)]'),
            MessagePart(file_data={"ref": "report.docx", "mime_type": "text/plain", "size_bytes": 45000}),
        ]),
    ]

    # Should not raise — ref-only file_data is silently handled
    result = adapter._convert_input(messages)

    assert len(result) == 1
    assert result[0]["role"] == "user"


def test_tool_result_round_trip_reuses_call_id():
    """A function result must carry the call_id of its function_call, or xAI 400s."""
    adapter = GrokAdapter(api_key="test-key")
    call_item = _function_call_item("search_memory", {"query": "x"}, "call_xyz")
    messages = [
        Message(role="user", parts=[MessagePart(text="find x")]),
        Message(role="model", parts=[], raw_content=[call_item]),
        Message(role="user", parts=[
            MessagePart(tool_response={"name": "search_memory", "response": "found"}),
        ]),
    ]

    items = adapter._convert_input(messages)

    outputs = [i for i in items if isinstance(i, dict) and i.get("type") == "function_call_output"]
    assert len(outputs) == 1
    assert outputs[0]["call_id"] == "call_xyz"
    assert outputs[0]["output"] == "found"


# ============================================================================
# F4.5 Phase 2 — exception translation
# ============================================================================

_GROK_REQUEST = LLMRequest(
    model_name="grok-4.6",
    system_instruction="test",
    messages=MESSAGES,
)


@pytest.mark.asyncio
async def test_asyncio_timeout_translates_to_LLMTimeoutError():
    """asyncio.TimeoutError from our wait_for wrap → LLMTimeoutError."""
    adapter = GrokAdapter(api_key="test-key")
    adapter.client.responses.create = AsyncMock(side_effect=asyncio.TimeoutError())

    request = _GROK_REQUEST.model_copy(update={"timeout": 10})
    with pytest.raises(LLMTimeoutError):
        await adapter.generate_content(request=request)


@pytest.mark.asyncio
async def test_sdk_timeout_translates_to_LLMTimeoutError():
    """openai.APITimeoutError (SDK-level, default httpx 60s when
    request.timeout is None) → LLMTimeoutError."""
    adapter = GrokAdapter(api_key="test-key")
    sdk_exc = openai.APITimeoutError(request=MagicMock())
    adapter.client.responses.create = AsyncMock(side_effect=sdk_exc)

    with pytest.raises(LLMTimeoutError):
        await adapter.generate_content(request=_GROK_REQUEST)


@pytest.mark.asyncio
async def test_connection_error_translates_to_LLMNetworkError():
    """openai.APIConnectionError → LLMNetworkError."""
    adapter = GrokAdapter(api_key="test-key")
    sdk_exc = openai.APIConnectionError(request=MagicMock())
    adapter.client.responses.create = AsyncMock(side_effect=sdk_exc)

    with pytest.raises(LLMNetworkError):
        await adapter.generate_content(request=_GROK_REQUEST)


@pytest.mark.asyncio
async def test_rate_limit_translates_to_LLMRateLimitError():
    """openai.RateLimitError → LLMRateLimitError(429)."""
    adapter = GrokAdapter(api_key="test-key")
    sdk_exc = openai.RateLimitError(
        message="slow down",
        response=MagicMock(status_code=429, request=MagicMock()),
        body={"error": {"type": "rate_limit_error"}},
    )
    adapter.client.responses.create = AsyncMock(side_effect=sdk_exc)

    with pytest.raises(LLMRateLimitError) as exc_info:
        await adapter.generate_content(request=_GROK_REQUEST)
    assert exc_info.value.http_status == 429


@pytest.mark.asyncio
async def test_503_translates_to_LLMUnavailableError():
    """openai.APIStatusError(status_code=503) → LLMUnavailableError."""
    adapter = GrokAdapter(api_key="test-key")
    sdk_exc = openai.APIStatusError(
        message="Service unavailable",
        response=MagicMock(status_code=503, request=MagicMock()),
        body={"error": {"type": "server_error"}},
    )
    adapter.client.responses.create = AsyncMock(side_effect=sdk_exc)

    with pytest.raises(LLMUnavailableError) as exc_info:
        await adapter.generate_content(request=_GROK_REQUEST)
    assert exc_info.value.http_status == 503


@pytest.mark.asyncio
async def test_5xx_non_503_translates_to_LLMServerError():
    """openai.APIStatusError(status_code=504) → LLMServerError."""
    adapter = GrokAdapter(api_key="test-key")
    sdk_exc = openai.APIStatusError(
        message="Gateway timeout",
        response=MagicMock(status_code=504, request=MagicMock()),
        body={"error": {"type": "server_error"}},
    )
    adapter.client.responses.create = AsyncMock(side_effect=sdk_exc)

    with pytest.raises(LLMServerError) as exc_info:
        await adapter.generate_content(request=_GROK_REQUEST)
    assert exc_info.value.http_status == 504


@pytest.mark.asyncio
async def test_4xx_non_429_translates_to_LLMClientError():
    """openai.APIStatusError(status_code=400) → LLMClientError (deterministic,
    not a failover trigger)."""
    adapter = GrokAdapter(api_key="test-key")
    sdk_exc = openai.APIStatusError(
        message="Bad request",
        response=MagicMock(status_code=400, request=MagicMock()),
        body={"error": {"type": "invalid_request_error"}},
    )
    adapter.client.responses.create = AsyncMock(side_effect=sdk_exc)

    with pytest.raises(LLMClientError) as exc_info:
        await adapter.generate_content(request=_GROK_REQUEST)
    assert exc_info.value.http_status == 400


# ============================================================================
# Timeouts — the 2026-08-15 incident
#
# grok-4.6 at reasoning effort medium runs 25-52s on a Smart delegation turn and
# crosses 60s at ~100k tokens of context. The client ceiling was 60s with
# max_retries=2, so every slow turn burned 3x60s and the daily briefing died at
# turn 5 three runs in a row.
# ============================================================================

def test_client_timeout_matches_openai_adapter():
    """300s ceiling, not the 60s that truncated live grok-4.6 turns."""
    adapter = GrokAdapter(api_key="test-key")

    assert adapter.client.timeout == 300.0


@pytest.mark.asyncio
async def test_request_timeout_is_forwarded_to_the_sdk():
    """An explicit LLMRequest.timeout must reach responses.create().

    Without the kwarg the client ceiling fires first and clamps the caller's
    intent — the defect OpenAIAdapter documents and Grok was missing entirely.
    """
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(model_name="grok-4.6", messages=MESSAGES, timeout=540),
    )

    assert captured["timeout"] == 540.0


@pytest.mark.asyncio
async def test_no_request_timeout_sends_no_timeout_kwarg():
    """Absent LLMRequest.timeout → the client ceiling governs; nothing is sent."""
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(model_name="grok-4.6", messages=MESSAGES),
    )

    assert "timeout" not in captured


# ============================================================================
# reasoning.effort — values probed live against xAI on 2026-08-15
# ============================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("effort", ["low", "medium", "high"])
async def test_standard_efforts_pass_through(effort):
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(model_name="grok-4.6", messages=MESSAGES, thinking=effort),
    )

    assert captured["reasoning"] == {"effort": effort}


@pytest.mark.asyncio
async def test_effort_none_allowed_on_grok_43():
    """grok-4.3 accepts effort='none' (probed: 0 reasoning tokens)."""
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(model_name="grok-4.3", messages=MESSAGES, thinking="none"),
    )

    assert captured["reasoning"] == {"effort": "none"}


@pytest.mark.asyncio
async def test_effort_none_clamped_to_medium_on_grok_46():
    """grok-4.6 returns 400 'does not support reasoning_effort value none'.

    A user-settable value (agent_thinking / complexity_settings_overrides) must
    never become an HTTP 400 mid-conversation, so it is clamped, not forwarded.
    """
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(model_name="grok-4.6", messages=MESSAGES, thinking="none"),
    )

    assert captured["reasoning"] == {"effort": "medium"}


@pytest.mark.asyncio
async def test_unknown_effort_normalised_to_medium():
    """Mirrors OpenAIAdapter's .get(thinking, 'medium') fallback."""
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(model_name="grok-4.6", messages=MESSAGES, thinking="turbo"),
    )

    assert captured["reasoning"] == {"effort": "medium"}


@pytest.mark.asyncio
async def test_grounding_does_not_force_an_effort():
    """Unlike OpenAI, xAI needs no forced effort under grounding.

    gpt-5.4 defaults to effort='none', which disables agentic search, so
    OpenAIAdapter forces 'low'. Both xAI models reason by default and were
    measured running real agentic search with no reasoning block at all.
    """
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4.6", messages=MESSAGES, use_grounding=True,
        ),
    )

    assert "reasoning" not in captured
    assert {"type": "web_search"} in captured["tools"]


# ============================================================================
# PERSONALITY ANCHOR — ported from OpenAIAdapter
# ============================================================================

@pytest.mark.asyncio
async def test_personality_anchor_injected_when_prompt_has_humor_engine():
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4.6",
            messages=MESSAGES,
            system_instruction="voice {}\nhumor_engine { ALWAYS_ACTIVE }",
        ),
    )

    developer = [i for i in captured["input"] if i.get("role") == "developer"]
    assert len(developer) == 1
    assert "PERSONALITY ANCHOR" in developer[0]["content"]


@pytest.mark.asyncio
async def test_no_personality_anchor_without_humor_engine():
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4.6",
            messages=MESSAGES,
            system_instruction="plain system prompt",
        ),
    )

    assert not [i for i in captured["input"] if i.get("role") == "developer"]


@pytest.mark.asyncio
async def test_turn_anchor_and_personality_share_one_developer_item():
    """Both high-priority overrides are combined, mirroring OpenAIAdapter."""
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, captured=captured)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4.6",
            messages=[Message(
                role="user",
                parts=[MessagePart(text=USER_TURN_SYSTEM_ANCHOR + "\n\nhello")],
            )],
            # Two sections minimum: the persona anchor is skipped below that, so a
            # specialist prompt with one stray block is never told to apply a persona.
            system_instruction="identity {\n a\n}\nhumor_engine {\n b\n}",
        ),
    )

    developer = [i for i in captured["input"] if i.get("role") == "developer"]
    assert len(developer) == 1
    assert USER_TURN_SYSTEM_ANCHOR in developer[0]["content"]
    assert "PERSONALITY ANCHOR" in developer[0]["content"]


# ============================================================================
# Anchor extraction across a delegation loop
#
# BaseAgent prepends USER_TURN_SYSTEM_ANCHOR to the latest user message when an
# execution starts. From delegation turn 2 the last message is a tool-result turn:
# role "user", parts carrying tool_response and no text. Inspecting only
# messages[-1] found nothing on every turn after the first, so the anchor stayed
# buried mid-history in the weakest channel the API offers while the developer item
# carried only the persona block. Confirmed in production 2026-08-15: turn 1 had it
# at 90% of the prompt, turns 2-3 at 30% and 24%.
# ============================================================================

def _tool_result_turn(call_id="call-1"):
    return Message(role="user", parts=[MessagePart(
        tool_response={"name": "search_web", "content": "...", "tool_use_id": call_id},
    )])


def _anchored_user_turn(text="what is the weather"):
    return Message(role="user", parts=[MessagePart(
        text=f"{USER_TURN_SYSTEM_ANCHOR}\n\n{text}",
    )])


class TestAnchorExtractionAcrossTurns:
    @staticmethod
    async def _developer_items(messages, system_instruction=None):
        adapter = GrokAdapter(api_key="test-key")
        captured = {}
        _install(adapter, captured=captured)
        await adapter.generate_content(request=LLMRequest(
            model_name="grok-4.6", messages=messages,
            system_instruction=system_instruction,
        ))
        return captured, [i for i in captured["input"] if i.get("role") == "developer"]

    async def test_lifted_when_the_anchor_is_the_last_message(self):
        _, dev = await self._developer_items([_anchored_user_turn()])

        assert len(dev) == 1
        assert USER_TURN_SYSTEM_ANCHOR in dev[0]["content"]

    async def test_lifted_when_tool_results_follow_it(self):
        """The regression: turn 2+ of every delegation loop."""
        messages = [
            _anchored_user_turn(),
            Message(role="model", parts=[MessagePart(text="calling a tool")]),
            _tool_result_turn(),
        ]

        captured, dev = await self._developer_items(messages)

        assert len(dev) == 1, "anchor was not lifted out of mid-history"
        assert USER_TURN_SYSTEM_ANCHOR in dev[0]["content"]

    async def test_anchor_is_removed_from_the_message_it_came_from(self):
        messages = [
            _anchored_user_turn("what is the weather"),
            Message(role="model", parts=[MessagePart(text="x")]),
            _tool_result_turn(),
        ]

        captured, _ = await self._developer_items(messages)

        non_developer = [i for i in captured["input"] if i.get("role") != "developer"]
        blob = json.dumps(non_developer, ensure_ascii=False)
        assert "System anchors." not in blob, "anchor duplicated: lifted AND left behind"
        assert "what is the weather" in blob, "the user's own text was dropped"

    async def test_no_developer_item_without_an_anchor(self):
        _, dev = await self._developer_items([
            Message(role="user", parts=[MessagePart(text="plain message")]),
        ])

        assert dev == []

    async def test_only_the_most_recent_anchor_is_lifted(self):
        """Two anchored turns in history — take the latest, leave the older one be."""
        messages = [
            _anchored_user_turn("older question"),
            Message(role="model", parts=[MessagePart(text="x")]),
            _anchored_user_turn("newer question"),
        ]

        captured, dev = await self._developer_items(messages)

        assert len(dev) == 1
        blob = json.dumps(
            [i for i in captured["input"] if i.get("role") != "developer"],
            ensure_ascii=False,
        )
        assert blob.count("System anchors.") == 1
        assert "newer question" in blob
