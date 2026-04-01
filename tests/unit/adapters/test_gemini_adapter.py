import pytest
from unittest.mock import MagicMock

from src.adapters.gemini_adapter import GeminiAdapter
from src.domain.user import PerformanceTier
from src.ports.llm_port import (
    PromptCacheConfig,
    LLMRequest,
    Message,
    MessagePart,
    PROMPT_CACHE_BOUNDARY,
)
from google.genai import types as gemini_types


# ============================================================================
# NEW Provider Refactor Session 6: Gemini capabilities tests
# Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
# ============================================================================
def test_gemini_capabilities():
    adapter = GeminiAdapter(api_key="test-key")
    caps = adapter.get_capabilities()

    assert caps.native_tools is True
    assert caps.context_caching is False
    assert caps.streaming is True
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
            model_name="gemini-3-flash-preview",
            system_instruction="test",
            messages=[],
            cache_config=cache_config
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