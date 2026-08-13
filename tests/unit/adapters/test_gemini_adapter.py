import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
import httpx

from src.adapters.gemini_adapter import GeminiAdapter
from src.domain.exceptions import (
    LLMClientError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from src.domain.llm import FinishReason
from src.domain.user import PerformanceTier
from src.ports.llm_port import (
    PromptCacheConfig,
    LLMRequest,
    Message,
    MessagePart,
    PROMPT_CACHE_BOUNDARY,
)
from google.genai import types as gemini_types
from google.genai import errors as genai_errors


# ============================================================================
# NEW Provider Refactor Session 6: Gemini capabilities tests
# Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
# ============================================================================
def test_gemini_capabilities():
    adapter = GeminiAdapter(api_key="test-key")
    caps = adapter.get_capabilities()

    assert caps.native_tools is True
    assert caps.context_caching is False
    assert caps.vision is True
    assert caps.max_context_window == 1000000


# ============================================================================
# NEW Provider Refactor Session 6: Tier-to-model mapping tests
# Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
# ============================================================================
def test_gemini_model_for_tier():
    adapter = GeminiAdapter(api_key="test-key")

    assert adapter.get_model_for_tier(PerformanceTier.ECO) == "gemini-flash-lite-latest"
    assert adapter.get_model_for_tier(PerformanceTier.BALANCED) == "gemini-flash-latest"
    assert adapter.get_model_for_tier(PerformanceTier.PERFORMANCE) == "gemini-pro-latest"


# ============================================================================
# NEW Provider Refactor Session 6: Tier validation tests
# Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
# ============================================================================
def test_gemini_unsupported_tier_raises():
    adapter = GeminiAdapter(api_key="test-key")

    with pytest.raises(ValueError, match="Unsupported tier"):
        adapter.get_model_for_tier("invalid_tier")


# ============================================================================
# MODIFIED Provider Refactor Session 6: Unsupported feature validation tests
# Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
# ============================================================================
@pytest.mark.asyncio
async def test_gemini_prompt_caching_fails_fast():
    adapter = GeminiAdapter(api_key="test-key")
    cache_config = PromptCacheConfig(enabled=True)

    with pytest.raises(ValueError, match="does not support prompt caching"):
        await adapter.generate_content(
            request=LLMRequest(
                model_name="gemini-3-flash-preview",
                system_instruction="test",
                messages=[],
                cache_config=cache_config,
            )
        )


# ============================================================================
# Wire tests: verify what config is actually sent to the Gemini SDK
#
# Pattern: replace adapter.client with MagicMock(), then assign an async
# function to client.aio.models.generate_content to capture the config arg.
# ============================================================================

_MESSAGES = [Message(role="user", parts=[MessagePart(text="Hi")])]
_TOOLS = [
    {
        "name": "search_memory",
        "description": "Search memories",
        "parameters": {"type": "object", "properties": {}},
    }
]


def _make_gemini_response(text="OK", function_calls=None):
    """Minimal Gemini response that _parse_response can consume."""
    text_part = MagicMock()
    text_part.text = text
    text_part.function_call = None

    parts = [text_part]
    if function_calls:
        for name, args in function_calls:
            p = MagicMock()
            p.text = None
            fc = MagicMock()
            fc.name = name
            fc.args = args
            # Prevent MagicMock from auto-creating truthy thought_signature attributes.
            # _extract_thought_signature checks these via getattr and MagicMock returns
            # truthy Mock objects for any attribute, causing ToolCall pydantic validation to fail.
            fc.thought_signature = None
            fc.thoughtSignature = None
            fc.model_dump = MagicMock(return_value={})
            p.function_call = fc
            parts.append(p)

    content = MagicMock()
    content.parts = parts

    candidate = MagicMock()
    candidate.content = content
    candidate.grounding_metadata = None

    usage = MagicMock()
    usage.prompt_token_count = 10
    usage.candidates_token_count = 5
    usage.total_token_count = 15

    response = MagicMock()
    response.candidates = [candidate]
    response.usage_metadata = usage
    return response


@pytest.mark.asyncio
async def test_force_tool_use_sets_function_calling_mode_any():
    """force_tool_use=True + tools → config.tool_config.function_calling_config.mode == 'ANY'."""
    adapter = GeminiAdapter(api_key="test-key")
    captured = {}

    async def mock_generate(model=None, contents=None, config=None):
        captured["config"] = config
        return _make_gemini_response()

    adapter.client = MagicMock()
    adapter.client.aio.models.generate_content = mock_generate

    await adapter.generate_content(
        request=LLMRequest(
            model_name="gemini-flash-latest",
            messages=_MESSAGES,
            tools=_TOOLS,
            force_tool_use=True,
        )
    )

    config = captured.get("config")
    assert config is not None
    assert config.tool_config is not None, "tool_config must be set when force_tool_use=True"
    assert config.tool_config.function_calling_config.mode == "ANY", (
        f"Expected mode='ANY', got {config.tool_config.function_calling_config.mode!r}"
    )


@pytest.mark.asyncio
async def test_no_force_tool_use_omits_tool_config():
    """force_tool_use=False → config.tool_config must be None."""
    adapter = GeminiAdapter(api_key="test-key")
    captured = {}

    async def mock_generate(model=None, contents=None, config=None):
        captured["config"] = config
        return _make_gemini_response()

    adapter.client = MagicMock()
    adapter.client.aio.models.generate_content = mock_generate

    await adapter.generate_content(
        request=LLMRequest(
            model_name="gemini-flash-latest",
            messages=_MESSAGES,
            tools=_TOOLS,
            force_tool_use=False,
        )
    )

    assert captured["config"].tool_config is None


@pytest.mark.asyncio
async def test_use_grounding_injects_google_search_tool():
    """use_grounding=True → config.tools contains a Tool with google_search set."""
    adapter = GeminiAdapter(api_key="test-key")
    captured = {}

    async def mock_generate(model=None, contents=None, config=None):
        captured["config"] = config
        return _make_gemini_response()

    adapter.client = MagicMock()
    adapter.client.aio.models.generate_content = mock_generate

    await adapter.generate_content(
        request=LLMRequest(
            model_name="gemini-flash-latest",
            messages=_MESSAGES,
            use_grounding=True,
        )
    )

    tools = captured["config"].tools or []
    has_google_search = any(
        getattr(t, "google_search", None) is not None for t in tools
    )
    assert has_google_search, f"GoogleSearch tool not found in config.tools={tools}"


@pytest.mark.asyncio
async def test_thinking_low_maps_to_thinking_level_low():
    """thinking='low' → config.thinking_config.thinking_level == ThinkingLevel.LOW."""
    adapter = GeminiAdapter(api_key="test-key")
    captured = {}

    async def mock_generate(model=None, contents=None, config=None):
        captured["config"] = config
        return _make_gemini_response()

    adapter.client = MagicMock()
    adapter.client.aio.models.generate_content = mock_generate

    await adapter.generate_content(
        request=LLMRequest(
            model_name="gemini-flash-latest",
            messages=_MESSAGES,
            thinking="low",
        )
    )

    assert captured["config"].thinking_config is not None
    assert captured["config"].thinking_config.thinking_level == gemini_types.ThinkingLevel.LOW


@pytest.mark.asyncio
async def test_thinking_high_maps_to_thinking_level_high():
    """thinking='high' → config.thinking_config.thinking_level == ThinkingLevel.HIGH."""
    adapter = GeminiAdapter(api_key="test-key")
    captured = {}

    async def mock_generate(model=None, contents=None, config=None):
        captured["config"] = config
        return _make_gemini_response()

    adapter.client = MagicMock()
    adapter.client.aio.models.generate_content = mock_generate

    await adapter.generate_content(
        request=LLMRequest(
            model_name="gemini-flash-latest",
            messages=_MESSAGES,
            thinking="high",
        )
    )

    assert captured["config"].thinking_config.thinking_level == gemini_types.ThinkingLevel.HIGH


@pytest.mark.asyncio
async def test_no_thinking_omits_thinking_config():
    """thinking=None → config.thinking_config must be None."""
    adapter = GeminiAdapter(api_key="test-key")
    captured = {}

    async def mock_generate(model=None, contents=None, config=None):
        captured["config"] = config
        return _make_gemini_response()

    adapter.client = MagicMock()
    adapter.client.aio.models.generate_content = mock_generate

    await adapter.generate_content(
        request=LLMRequest(
            model_name="gemini-flash-latest",
            messages=_MESSAGES,
        )
    )

    assert captured["config"].thinking_config is None


# ============================================================================
# Thought parts must never be mistaken for the answer.
#
# Regression 2026-08-12: `include_thoughts=True` made Gemini return an extra part
# with `thought=True` whose `.text` is the reasoning. The extractor joined `.text`
# across ALL parts, so the reasoning was prepended to the answer — it shipped 4244
# bytes of "**My Approach to Crafting…**" at the top of a user-facing HTML file and
# made `json.loads` fail at char 0 for every JSON-parsing Gemini agent.
#
# These build REAL google.genai Part objects on purpose: MagicMock fabricates any
# attribute on access, so a mocked `part.thought` is truthy no matter what the code
# under test does, and the test would pass against the broken extractor.
# ============================================================================

_THOUGHT_TEXT = "**Analysis**\nOkay, here's how I'm tackling this. First I will…"
_ANSWER_TEXT = '{"valuable": true}'


def _make_response_with_thought_part(answer=_ANSWER_TEXT, thought=_THOUGHT_TEXT):
    """Gemini response shaped like a real include_thoughts=True reply.

    Thought part comes FIRST, as the API returns it — that ordering is what put the
    reasoning in front of the answer and broke parsing at char 0.
    """
    parts = []
    if thought is not None:
        parts.append(gemini_types.Part(text=thought, thought=True))
    if answer is not None:
        parts.append(gemini_types.Part(text=answer))

    candidate = MagicMock()
    candidate.content = gemini_types.Content(role="model", parts=parts)
    candidate.grounding_metadata = None
    candidate.finish_reason = gemini_types.FinishReason.STOP

    usage = MagicMock()
    usage.prompt_token_count = 10
    usage.candidates_token_count = 5
    usage.total_token_count = 15
    usage.cached_content_token_count = 0
    usage.thoughts_token_count = 99

    response = MagicMock()
    response.candidates = [candidate]
    response.usage_metadata = usage
    return response


async def _parse_via_adapter(response):
    adapter = GeminiAdapter(api_key="test-key")

    async def mock_generate(model=None, contents=None, config=None):
        return response

    adapter.client = MagicMock()
    adapter.client.aio.models.generate_content = mock_generate
    return await adapter.generate_content(
        request=LLMRequest(model_name="gemini-flash-latest", messages=_MESSAGES, thinking="medium")
    )


@pytest.mark.asyncio
async def test_thought_part_excluded_from_text():
    """text must be the answer alone — never the reasoning, never the two concatenated."""
    result = await _parse_via_adapter(_make_response_with_thought_part())

    assert result.text == _ANSWER_TEXT
    assert "Analysis" not in (result.text or "")
    assert "tackling this" not in (result.text or "")


@pytest.mark.asyncio
async def test_thought_only_response_yields_empty_text_not_reasoning():
    """No answer part → text is empty. Reasoning must not be promoted into the answer slot.

    This is the exact HTML-leak shape: the model spends its budget thinking and the
    reasoning must not become the delivered document.
    """
    result = await _parse_via_adapter(_make_response_with_thought_part(answer=None))

    assert not result.text


@pytest.mark.asyncio
async def test_answer_text_is_json_parseable_with_thought_present():
    """The classify_batch failure: json.loads on text must succeed despite a thought part."""
    result = await _parse_via_adapter(_make_response_with_thought_part())

    assert json.loads(result.text) == {"valuable": True}


@pytest.mark.asyncio
async def test_thought_text_captured_separately():
    """Reasoning is kept — it is already paid for — just not in `text`."""
    result = await _parse_via_adapter(_make_response_with_thought_part())

    assert result.thought_text == _THOUGHT_TEXT


@pytest.mark.asyncio
async def test_thought_text_is_none_when_no_thought_part():
    result = await _parse_via_adapter(_make_response_with_thought_part(thought=None))

    assert result.thought_text is None
    assert result.text == _ANSWER_TEXT


@pytest.mark.asyncio
async def test_raw_content_keeps_thought_part_for_history():
    """Thought blocks must be resent unmodified — Gemini requires it for reasoning continuity.

    So raw_content (which becomes the model turn in the next request) keeps both parts;
    only the caller-facing `text` is filtered.
    """
    result = await _parse_via_adapter(_make_response_with_thought_part())

    assert len(result.raw_content.parts) == 2
    assert any(p.thought for p in result.raw_content.parts)


@pytest.mark.asyncio
async def test_include_thoughts_enabled_so_reasoning_is_observable():
    """Thinking is billed whether or not it is returned, so ask for the summary."""
    adapter = GeminiAdapter(api_key="test-key")
    captured = {}

    async def mock_generate(model=None, contents=None, config=None):
        captured["config"] = config
        return _make_gemini_response()

    adapter.client = MagicMock()
    adapter.client.aio.models.generate_content = mock_generate

    await adapter.generate_content(
        request=LLMRequest(
            model_name="gemini-flash-latest", messages=_MESSAGES, thinking="medium",
        )
    )

    assert captured["config"].thinking_config.include_thoughts is True


@pytest.mark.asyncio
async def test_cache_boundary_stripped_from_system_instruction():
    """PROMPT_CACHE_BOUNDARY in system_instruction → stripped before being sent to Gemini."""
    adapter = GeminiAdapter(api_key="test-key")
    captured = {}

    async def mock_generate(model=None, contents=None, config=None):
        captured["config"] = config
        return _make_gemini_response()

    adapter.client = MagicMock()
    adapter.client.aio.models.generate_content = mock_generate

    system = f"static part\n\n{PROMPT_CACHE_BOUNDARY}\ndynamic part"

    await adapter.generate_content(
        request=LLMRequest(
            model_name="gemini-flash-latest",
            system_instruction=system,
            messages=_MESSAGES,
        )
    )

    sent_instruction = captured["config"].system_instruction
    assert PROMPT_CACHE_BOUNDARY not in sent_instruction, (
        f"PROMPT_CACHE_BOUNDARY must be stripped; got: {sent_instruction!r}"
    )


@pytest.mark.asyncio
async def test_dict_response_schema_uses_json_schema_path():
    """response_schema as dict → config.response_json_schema set, config.response_schema=None."""
    adapter = GeminiAdapter(api_key="test-key")
    captured = {}

    async def mock_generate(model=None, contents=None, config=None):
        captured["config"] = config
        return _make_gemini_response()

    adapter.client = MagicMock()
    adapter.client.aio.models.generate_content = mock_generate

    await adapter.generate_content(
        request=LLMRequest(
            model_name="gemini-flash-latest",
            messages=_MESSAGES,
            response_schema={"type": "object", "properties": {"answer": {"type": "string"}}},
        )
    )

    config = captured["config"]
    assert config.response_json_schema is not None, "response_json_schema must be set for dict schema"
    assert config.response_schema is None, "response_schema must be None when using json_schema path"


@pytest.mark.asyncio
async def test_tool_calls_parsed_from_response():
    """function_call part in response → LLMResponse.tool_calls populated correctly."""
    adapter = GeminiAdapter(api_key="test-key")
    response = _make_gemini_response(
        text="",
        function_calls=[("search_memory", {"query": "test"})],
    )
    # Clear text from the text part so only function_call parts remain relevant
    response.candidates[0].content.parts[0].text = None

    async def mock_generate(model=None, contents=None, config=None):
        return response

    adapter.client = MagicMock()
    adapter.client.aio.models.generate_content = mock_generate

    result = await adapter.generate_content(
        request=LLMRequest(
            model_name="gemini-flash-latest",
            messages=_MESSAGES,
            tools=_TOOLS,
        )
    )

    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.name == "search_memory"
    assert tc.args == {"query": "test"}


# ---------------------------------------------------------------------------
# GCS reference file_data — graceful handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gcs_ref_file_data_no_error():
    """file_data with 'ref' key should not raise — it's a GCS reference with no binary."""
    adapter = GeminiAdapter(api_key="test-key")
    messages = [
        Message(role="user", parts=[
            MessagePart(text='[File: "report.docx" (45KB)]'),
            MessagePart(file_data={"ref": "report.docx", "mime_type": "text/plain", "size_bytes": 45000}),
        ]),
    ]

    # Should not raise — ref-only file_data is silently handled
    result = await adapter._convert_messages(messages)

    assert len(result) == 1


# ============================================================================
# F4.5 Phase 2 — exception translation
# ============================================================================

_GEMINI_REQUEST = LLMRequest(
    model_name="gemini-flash-latest",
    messages=[Message(role="user", parts=[MessagePart(text="hi")])],
)


def _make_adapter_with_mock_call(side_effect):
    """Build a GeminiAdapter whose generate_content call raises ``side_effect``."""
    adapter = GeminiAdapter(api_key="test-key")
    adapter.client = MagicMock()
    adapter.client.aio.models.generate_content = AsyncMock(side_effect=side_effect)
    return adapter


@pytest.mark.asyncio
async def test_asyncio_timeout_translates_to_LLMTimeoutError():
    """asyncio.TimeoutError from our wait_for wrap → LLMTimeoutError."""
    adapter = _make_adapter_with_mock_call(asyncio.TimeoutError())

    request = _GEMINI_REQUEST.model_copy(update={"timeout": 10})
    with pytest.raises(LLMTimeoutError):
        await adapter.generate_content(request=request)


@pytest.mark.asyncio
async def test_sdk_timeout_translates_to_LLMTimeoutError():
    """httpx.TimeoutException (SDK transport-level timeout when request.timeout
    is None and httpx default fires) → LLMTimeoutError."""
    sdk_exc = httpx.ReadTimeout("read timeout")
    adapter = _make_adapter_with_mock_call(sdk_exc)

    with pytest.raises(LLMTimeoutError):
        await adapter.generate_content(request=_GEMINI_REQUEST)


@pytest.mark.asyncio
async def test_connection_error_translates_to_LLMNetworkError():
    """httpx.NetworkError (TCP/DNS/SSL handshake) → LLMNetworkError."""
    sdk_exc = httpx.ConnectError("DNS resolution failed")
    adapter = _make_adapter_with_mock_call(sdk_exc)

    with pytest.raises(LLMNetworkError):
        await adapter.generate_content(request=_GEMINI_REQUEST)


@pytest.mark.asyncio
async def test_5xx_non_503_translates_to_LLMServerError():
    """genai_errors.ServerError(code=500) → LLMServerError (distinct from 503)."""
    sdk_exc = genai_errors.ServerError(
        code=500, response_json={"error": {"message": "internal"}}
    )
    adapter = _make_adapter_with_mock_call(sdk_exc)

    with pytest.raises(LLMServerError) as exc_info:
        await adapter.generate_content(request=_GEMINI_REQUEST)
    assert exc_info.value.http_status == 500


@pytest.mark.asyncio
async def test_4xx_non_429_translates_to_LLMClientError():
    """genai_errors.ClientError(code=400) → LLMClientError (deterministic, not a
    failover trigger)."""
    sdk_exc = genai_errors.ClientError(
        code=400, response_json={"error": {"message": "bad request"}}
    )
    adapter = _make_adapter_with_mock_call(sdk_exc)

    with pytest.raises(LLMClientError) as exc_info:
        await adapter.generate_content(request=_GEMINI_REQUEST)
    assert exc_info.value.http_status == 400

# ============================================================================
# finish_reason translation
#
# Incident 2026-08-13: a newspaper-page request came back with
# finish_reason=RECITATION and zero parts. The adapter returned a bare
# LLMResponse(text=""), so "the provider refused" and "the model stalled" reached
# the agent as the same value. Real gemini_types.FinishReason members are used —
# the mapping reads `.name`, and a string stand-in would not prove it.
# ============================================================================

def _make_blocked_response(finish_reason):
    """A blocked Gemini reply: candidate present, content/parts absent."""
    candidate = MagicMock()
    candidate.content = None
    candidate.finish_reason = finish_reason
    candidate.finish_message = None
    candidate.safety_ratings = None
    candidate.grounding_metadata = None

    usage = MagicMock()
    usage.prompt_token_count = 10
    usage.candidates_token_count = 0
    usage.total_token_count = 10
    usage.cached_content_token_count = 0
    usage.thoughts_token_count = 2665

    response = MagicMock()
    response.candidates = [candidate]
    response.usage_metadata = usage
    return response


@pytest.mark.asyncio
async def test_recitation_block_surfaces_finish_reason():
    """The exact prod failure: RECITATION must survive translation to the domain."""
    result = await _parse_via_adapter(
        _make_blocked_response(gemini_types.FinishReason.RECITATION)
    )

    assert result.finish_reason is FinishReason.RECITATION


@pytest.mark.asyncio
async def test_recitation_block_still_yields_empty_text():
    result = await _parse_via_adapter(
        _make_blocked_response(gemini_types.FinishReason.RECITATION)
    )

    assert result.text == ""


@pytest.mark.asyncio
async def test_image_recitation_maps_to_recitation():
    result = await _parse_via_adapter(
        _make_blocked_response(gemini_types.FinishReason.IMAGE_RECITATION)
    )

    assert result.finish_reason is FinishReason.RECITATION


@pytest.mark.asyncio
async def test_prohibited_content_maps_to_safety_not_recitation():
    result = await _parse_via_adapter(
        _make_blocked_response(gemini_types.FinishReason.PROHIBITED_CONTENT)
    )

    assert result.finish_reason is FinishReason.SAFETY


@pytest.mark.asyncio
async def test_max_tokens_is_reported_distinctly():
    result = await _parse_via_adapter(
        _make_blocked_response(gemini_types.FinishReason.MAX_TOKENS)
    )

    assert result.finish_reason is FinishReason.MAX_TOKENS


@pytest.mark.asyncio
async def test_unmapped_finish_reason_becomes_other():
    result = await _parse_via_adapter(
        _make_blocked_response(gemini_types.FinishReason.LANGUAGE)
    )

    assert result.finish_reason is FinishReason.OTHER


@pytest.mark.asyncio
async def test_missing_finish_reason_stays_none():
    result = await _parse_via_adapter(_make_blocked_response(None))

    assert result.finish_reason is None


@pytest.mark.asyncio
async def test_successful_response_reports_stop():
    """The normal path must carry the reason too — not only the blocked one."""
    result = await _parse_via_adapter(_make_response_with_thought_part())

    assert result.finish_reason is FinishReason.STOP


@pytest.mark.asyncio
async def test_no_candidates_reports_no_finish_reason():
    response = MagicMock()
    response.candidates = []
    response.prompt_feedback = None
    response.finish_reason = None

    result = await _parse_via_adapter(response)

    assert result.finish_reason is None
