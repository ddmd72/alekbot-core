import asyncio
import json
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import openai

from src.adapters.openai_adapter import OpenAIAdapter
from src.domain.exceptions import (
    LLMClientError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from src.domain.user import PerformanceTier
from src.ports.llm_port import (
    LLMRequest,
    Message,
    MessagePart,
    ToolCall,
    PromptCacheConfig,
    PROMPT_CACHE_BOUNDARY,
)


# ============================================================================
# Helpers — mock Responses API response object
# ============================================================================

def _make_response(text="", output=None, usage=None, function_calls=None):
    """Build a mock Responses API response object."""
    resp = MagicMock()

    if output is None:
        output = []
        if text:
            msg_item = MagicMock()
            msg_item.type = "message"
            text_block = MagicMock()
            text_block.type = "output_text"
            text_block.text = text
            text_block.annotations = []
            msg_item.content = [text_block]
            output.append(msg_item)
        if function_calls:
            for fc in function_calls:
                fc_item = MagicMock()
                fc_item.type = "function_call"
                fc_item.name = fc["name"]
                fc_item.arguments = fc["arguments"]
                fc_item.call_id = fc["call_id"]
                output.append(fc_item)

    resp.output = output
    resp.output_text = text
    resp.usage = usage
    resp.model = "gpt-5-mini"
    return resp


# ============================================================================
# Capabilities and tier mapping
# ============================================================================

def test_openai_capabilities():
    adapter = OpenAIAdapter(api_key="test-key")
    caps = adapter.get_capabilities()

    assert caps.native_tools is True
    assert caps.context_caching is True
    assert caps.vision is True
    assert caps.supports_json_mode is True
    assert caps.supports_system_prompt is True
    assert caps.max_context_window == 1047576
    assert caps.native_grounding is True


def test_openai_model_for_tier():
    adapter = OpenAIAdapter(api_key="test-key")

    assert adapter.get_model_for_tier(PerformanceTier.ECO) == "gpt-5.4-nano"
    assert adapter.get_model_for_tier(PerformanceTier.BALANCED) == "gpt-5.4-mini"
    assert adapter.get_model_for_tier(PerformanceTier.PERFORMANCE) == "gpt-5.4"


def test_openai_model_for_tier_ultra():
    """ULTRA tier maps to gpt-5.5-pro (upgraded from gpt-5.4-pro on 2026-05-30).

    Same price, newer model. See decisions/openai_ultra_tier_to_gpt_5_5_pro.md.
    """
    adapter = OpenAIAdapter(api_key="test-key")
    assert adapter.get_model_for_tier(PerformanceTier.ULTRA) == "gpt-5.5-pro"


def test_openai_unsupported_tier_raises():
    adapter = OpenAIAdapter(api_key="test-key")

    with pytest.raises(ValueError, match="Unsupported tier"):
        adapter.get_model_for_tier("invalid_tier")


def test_openai_supports_caching_is_false():
    adapter = OpenAIAdapter(api_key="test-key")
    assert adapter.supports_caching() is False


# ============================================================================
# _is_reasoning_model — sampling parameter gate
# ============================================================================

def test_is_reasoning_model_gpt5():
    adapter = OpenAIAdapter(api_key="test-key")
    assert adapter._is_reasoning_model("gpt-5") is True


def test_is_reasoning_model_gpt5_mini():
    adapter = OpenAIAdapter(api_key="test-key")
    assert adapter._is_reasoning_model("gpt-5-mini") is True


def test_is_reasoning_model_gpt5_nano():
    adapter = OpenAIAdapter(api_key="test-key")
    assert adapter._is_reasoning_model("gpt-5-nano") is True


def test_is_reasoning_model_o1():
    adapter = OpenAIAdapter(api_key="test-key")
    assert adapter._is_reasoning_model("o1") is True


def test_is_reasoning_model_o3():
    adapter = OpenAIAdapter(api_key="test-key")
    assert adapter._is_reasoning_model("o3-mini") is True


def test_is_reasoning_model_gpt4_is_false():
    adapter = OpenAIAdapter(api_key="test-key")
    assert adapter._is_reasoning_model("gpt-4o") is False


def test_is_reasoning_model_gpt4_turbo_is_false():
    adapter = OpenAIAdapter(api_key="test-key")
    assert adapter._is_reasoning_model("gpt-4-turbo") is False


# ============================================================================
# _convert_tools — Responses API internally-tagged format
# ============================================================================

def test_convert_tools_basic():
    adapter = OpenAIAdapter(api_key="test-key")
    tools = [
        {
            "name": "search_memory",
            "description": "Search personal knowledge base",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }
    ]

    result = adapter._convert_tools(tools)

    assert len(result) == 1
    assert result[0]["type"] == "function"
    assert result[0]["name"] == "search_memory"
    assert result[0]["description"] == "Search personal knowledge base"
    assert result[0]["parameters"]["properties"]["query"]["type"] == "string"


def test_convert_tools_empty():
    adapter = OpenAIAdapter(api_key="test-key")
    assert adapter._convert_tools([]) == []


def test_convert_tools_no_description():
    adapter = OpenAIAdapter(api_key="test-key")
    tools = [{"name": "foo"}]

    result = adapter._convert_tools(tools)

    assert result[0]["name"] == "foo"
    assert result[0]["description"] == ""


# ============================================================================
# _convert_input — Responses API input items
# ============================================================================

@pytest.mark.asyncio
async def test_convert_input_user_text():
    adapter = OpenAIAdapter(api_key="test-key")
    messages = [Message(role="user", parts=[MessagePart(text="What is 2+2?")])]

    result = await adapter._convert_input(messages)

    assert result[0]["role"] == "user"
    assert result[0]["content"] == "What is 2+2?"


@pytest.mark.asyncio
async def test_convert_input_model_text():
    adapter = OpenAIAdapter(api_key="test-key")
    messages = [Message(role="model", parts=[MessagePart(text="The answer is 4.")])]

    result = await adapter._convert_input(messages)

    assert result[0]["role"] == "assistant"
    assert result[0]["content"] == "The answer is 4."


@pytest.mark.asyncio
async def test_convert_input_model_with_responses_raw_content():
    """Responses API output items are passed through directly."""
    adapter = OpenAIAdapter(api_key="test-key")

    output_items = [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hello"}]},
        {"type": "function_call", "call_id": "call_123", "name": "search", "arguments": "{}"},
    ]

    messages = [Message(role="model", parts=[], raw_content=output_items)]
    result = await adapter._convert_input(messages)

    assert len(result) == 2
    assert result[0]["type"] == "message"
    assert result[1]["type"] == "function_call"


@pytest.mark.asyncio
async def test_convert_input_vision_base64():
    """Base64 images are wrapped in input_image format."""
    adapter = OpenAIAdapter(api_key="test-key")
    messages = [Message(
        role="user",
        parts=[MessagePart(file_data={"base64": "abc123", "mime_type": "image/jpeg"})]
    )]

    result = await adapter._convert_input(messages)

    assert result[0]["content"][0]["type"] == "input_image"
    assert "data:image/jpeg;base64,abc123" in result[0]["content"][0]["image_url"]


@pytest.mark.asyncio
async def test_convert_input_pdf_upload():
    """PDF files are uploaded via Files API and referenced by file_id."""
    adapter = OpenAIAdapter(api_key="test-key")

    mock_file = MagicMock()
    mock_file.id = "file-test123"
    adapter.client.files.create = AsyncMock(return_value=mock_file)

    messages = [Message(
        role="user",
        parts=[MessagePart(file_data={"path": "/tmp/test.pdf", "mime_type": "application/pdf"})]
    )]

    # Mock open to avoid actual file read
    import builtins
    from unittest.mock import mock_open
    with patch.object(builtins, "open", mock_open(read_data=b"fake pdf")):
        result = await adapter._convert_input(messages)

    assert len(result) == 1
    assert result[0]["content"][0]["type"] == "input_file"
    assert result[0]["content"][0]["file_id"] == "file-test123"


@pytest.mark.asyncio
async def test_convert_input_gcs_ref_no_error():
    """file_data with 'ref' key should not raise — it's a GCS reference with no binary."""
    adapter = OpenAIAdapter(api_key="test-key")
    messages = [
        Message(role="user", parts=[
            MessagePart(text='[File: "report.docx" (45KB)]'),
            MessagePart(file_data={"ref": "report.docx", "mime_type": "text/plain", "size_bytes": 45000}),
        ]),
    ]

    result = await adapter._convert_input(messages)

    assert len(result) == 1
    assert result[0]["role"] == "user"


# ============================================================================
# generate_content — Responses API call path (mocked)
# ============================================================================

@pytest.mark.asyncio
async def test_cache_boundary_stripped_from_system_instruction():
    """PROMPT_CACHE_BOUNDARY must not reach the OpenAI API."""
    adapter = OpenAIAdapter(api_key="test-key")

    captured_kwargs = {}

    async def mock_create(**kwargs):
        captured_kwargs.update(kwargs)
        return _make_response(text="OK")

    adapter.client.responses.create = mock_create

    static = "You are a helpful assistant."
    dynamic = "Current date: 2026-03-04"
    system = f"{static}\n\n{PROMPT_CACHE_BOUNDARY}\n{dynamic}"

    await adapter.generate_content(
        request=LLMRequest(
            model_name="gpt-5-mini",
            system_instruction=system,
            messages=[Message(role="user", parts=[MessagePart(text="Hi")])],
        )
    )

    sent_instructions = captured_kwargs.get("instructions", "")
    assert PROMPT_CACHE_BOUNDARY not in sent_instructions
    assert static in sent_instructions
    assert dynamic in sent_instructions


@pytest.mark.asyncio
async def test_generate_content_excludes_temperature_for_gpt5():
    """Temperature must not be sent for gpt-5 family."""
    adapter = OpenAIAdapter(api_key="test-key")

    captured_kwargs = {}

    async def mock_create(**kwargs):
        captured_kwargs.update(kwargs)
        return _make_response(text="Hello")

    adapter.client.responses.create = mock_create

    await adapter.generate_content(
        request=LLMRequest(
            model_name="gpt-5-mini",
            system_instruction="You are helpful.",
            messages=[Message(role="user", parts=[MessagePart(text="Hi")])],
            temperature=0.8,
        )
    )

    assert "temperature" not in captured_kwargs


@pytest.mark.asyncio
async def test_generate_content_includes_temperature_for_gpt4():
    """Temperature must be included for non-gpt-5 models."""
    adapter = OpenAIAdapter(api_key="test-key")

    captured_kwargs = {}

    async def mock_create(**kwargs):
        captured_kwargs.update(kwargs)
        return _make_response(text="Hello")

    adapter.client.responses.create = mock_create

    await adapter.generate_content(
        request=LLMRequest(
            model_name="gpt-4o",
            system_instruction="You are helpful.",
            messages=[Message(role="user", parts=[MessagePart(text="Hi")])],
            temperature=0.8,
        )
    )

    assert "temperature" in captured_kwargs
    assert captured_kwargs["temperature"] == 0.8


@pytest.mark.asyncio
async def test_generate_content_json_mode():
    """response_mime_type=application/json activates text.format=json_object."""
    adapter = OpenAIAdapter(api_key="test-key")

    captured_kwargs = {}

    async def mock_create(**kwargs):
        captured_kwargs.update(kwargs)
        return _make_response(text='{"answer": 42}')

    adapter.client.responses.create = mock_create

    await adapter.generate_content(
        request=LLMRequest(
            model_name="gpt-5-mini",
            system_instruction="Return JSON.",
            messages=[Message(role="user", parts=[MessagePart(text="Hi")])],
            response_mime_type="application/json",
        )
    )

    assert captured_kwargs.get("text") == {"format": {"type": "json_object"}}


@pytest.mark.asyncio
async def test_generate_content_returns_tool_calls():
    """Function calls in API response are mapped to domain ToolCall objects."""
    adapter = OpenAIAdapter(api_key="test-key")

    async def mock_create(**kwargs):
        return _make_response(function_calls=[{
            "name": "search_memory",
            "arguments": '{"query": "vacation plans"}',
            "call_id": "call_xyz",
        }])

    adapter.client.responses.create = mock_create

    response = await adapter.generate_content(
        request=LLMRequest(
            model_name="gpt-5-mini",
            system_instruction="Use tools.",
            messages=[Message(role="user", parts=[MessagePart(text="Find my vacation plans")])],
        )
    )

    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "search_memory"
    assert response.tool_calls[0].args == {"query": "vacation plans"}
    assert response.tool_calls[0].thought_signature == "call_xyz"


@pytest.mark.asyncio
async def test_generate_content_uses_max_output_tokens():
    """max_output_tokens must be used in API call."""
    adapter = OpenAIAdapter(api_key="test-key")

    captured_kwargs = {}

    async def mock_create(**kwargs):
        captured_kwargs.update(kwargs)
        return _make_response(text="OK")

    adapter.client.responses.create = mock_create

    await adapter.generate_content(
        request=LLMRequest(
            model_name="gpt-5-mini",
            system_instruction="You are helpful.",
            messages=[Message(role="user", parts=[MessagePart(text="Hi")])],
            max_tokens=1000,
        ),
    )

    assert "max_output_tokens" in captured_kwargs
    assert captured_kwargs["max_output_tokens"] == 1000


@pytest.mark.asyncio
async def test_generate_content_store_true():
    """store=True must always be set (dashboard visibility)."""
    adapter = OpenAIAdapter(api_key="test-key")

    captured_kwargs = {}

    async def mock_create(**kwargs):
        captured_kwargs.update(kwargs)
        return _make_response(text="OK")

    adapter.client.responses.create = mock_create

    await adapter.generate_content(
        request=LLMRequest(
            model_name="gpt-5-mini",
            messages=[Message(role="user", parts=[MessagePart(text="Hi")])],
        )
    )

    assert captured_kwargs.get("store") is True


# ============================================================================
# use_grounding — native web search tool injection
# ============================================================================

@pytest.mark.asyncio
async def test_use_grounding_injects_web_search_tool():
    """use_grounding=True must prepend {"type": "web_search"} to tools."""
    adapter = OpenAIAdapter(api_key="test-key")

    captured_kwargs = {}

    async def mock_create(**kwargs):
        captured_kwargs.update(kwargs)
        return _make_response(text="Search result")

    adapter.client.responses.create = mock_create

    request = LLMRequest(
        model_name="gpt-5-mini",
        messages=[Message(role="user", parts=[MessagePart(text="Latest news?")])],
        use_grounding=True,
    )
    await adapter.generate_content(request=request)

    tools_sent = captured_kwargs.get("tools", [])
    assert any(t.get("type") == "web_search" for t in tools_sent), (
        f"Expected web_search tool in tools, got: {tools_sent}"
    )


@pytest.mark.asyncio
async def test_use_grounding_false_does_not_inject_web_search():
    """use_grounding=False (default) must NOT inject web_search tool."""
    adapter = OpenAIAdapter(api_key="test-key")

    captured_kwargs = {}

    async def mock_create(**kwargs):
        captured_kwargs.update(kwargs)
        return _make_response(text="Answer")

    adapter.client.responses.create = mock_create

    await adapter.generate_content(
        request=LLMRequest(
            model_name="gpt-5-mini",
            messages=[Message(role="user", parts=[MessagePart(text="Hello")])],
        )
    )

    tools_sent = captured_kwargs.get("tools", [])
    assert not any(
        isinstance(t, dict) and t.get("type") == "web_search" for t in tools_sent
    )


# ============================================================================
# _parse_response — annotations extraction
# ============================================================================

def test_parse_response_extracts_url_citations():
    """url_citation annotations from web search are appended as Sources block."""
    adapter = OpenAIAdapter(api_key="test-key")

    ann1 = MagicMock()
    ann1.type = "url_citation"
    ann1.title = "Example Page"
    ann1.url = "https://example.com"

    ann2 = MagicMock()
    ann2.type = "url_citation"
    ann2.title = "Another Page"
    ann2.url = "https://another.com"

    text_block = MagicMock()
    text_block.type = "output_text"
    text_block.text = "Some findings"
    text_block.annotations = [ann1, ann2]

    msg_item = MagicMock()
    msg_item.type = "message"
    msg_item.content = [text_block]

    resp = MagicMock()
    resp.output = [msg_item]
    resp.output_text = "Some findings"
    resp.usage = None
    resp.model = "gpt-5-mini"

    result = adapter._parse_response(resp)

    assert "[Example Page](https://example.com)" in result.text
    assert "[Another Page](https://another.com)" in result.text
    assert "*Sources:*" in result.text


def test_parse_response_deduplicates_citations():
    """Duplicate url_citation annotations are deduplicated."""
    adapter = OpenAIAdapter(api_key="test-key")

    ann1 = MagicMock()
    ann1.type = "url_citation"
    ann1.title = "Same Page"
    ann1.url = "https://same.com"

    ann2 = MagicMock()
    ann2.type = "url_citation"
    ann2.title = "Same Page"
    ann2.url = "https://same.com"

    text_block = MagicMock()
    text_block.type = "output_text"
    text_block.text = "Findings"
    text_block.annotations = [ann1, ann2]

    msg_item = MagicMock()
    msg_item.type = "message"
    msg_item.content = [text_block]

    resp = MagicMock()
    resp.output = [msg_item]
    resp.output_text = "Findings"
    resp.usage = None
    resp.model = "gpt-5-mini"

    result = adapter._parse_response(resp)

    assert result.text.count("https://same.com") == 1


# ============================================================================
# F4.5 Phase 2 — exception translation
# ============================================================================

_OPENAI_REQUEST = LLMRequest(
    model_name="gpt-5-mini",
    system_instruction="test",
    messages=[Message(role="user", parts=[MessagePart(text="hi")])],
)


@pytest.mark.asyncio
async def test_asyncio_timeout_translates_to_LLMTimeoutError():
    """asyncio.TimeoutError from our wait_for wrap → LLMTimeoutError."""
    adapter = OpenAIAdapter(api_key="test-key")
    adapter.client.responses.create = AsyncMock(side_effect=asyncio.TimeoutError())

    request = _OPENAI_REQUEST.model_copy(update={"timeout": 10})
    with pytest.raises(LLMTimeoutError):
        await adapter.generate_content(request=request)


@pytest.mark.asyncio
async def test_sdk_timeout_translates_to_LLMTimeoutError():
    """openai.APITimeoutError (SDK-level, default httpx 300s when
    request.timeout is None) → LLMTimeoutError. Without this branch,
    default-timeout requests would silently bypass the circuit breaker."""
    adapter = OpenAIAdapter(api_key="test-key")
    sdk_exc = openai.APITimeoutError(request=MagicMock())
    adapter.client.responses.create = AsyncMock(side_effect=sdk_exc)

    with pytest.raises(LLMTimeoutError):
        await adapter.generate_content(request=_OPENAI_REQUEST)


@pytest.mark.asyncio
async def test_connection_error_translates_to_LLMNetworkError():
    """openai.APIConnectionError → LLMNetworkError."""
    adapter = OpenAIAdapter(api_key="test-key")
    sdk_exc = openai.APIConnectionError(request=MagicMock())
    adapter.client.responses.create = AsyncMock(side_effect=sdk_exc)

    with pytest.raises(LLMNetworkError):
        await adapter.generate_content(request=_OPENAI_REQUEST)


@pytest.mark.asyncio
async def test_5xx_non_503_translates_to_LLMServerError():
    """openai.APIStatusError(status_code=502) → LLMServerError."""
    adapter = OpenAIAdapter(api_key="test-key")
    sdk_exc = openai.APIStatusError(
        message="Bad gateway",
        response=MagicMock(status_code=502, request=MagicMock()),
        body={"error": {"type": "server_error"}},
    )
    adapter.client.responses.create = AsyncMock(side_effect=sdk_exc)

    with pytest.raises(LLMServerError) as exc_info:
        await adapter.generate_content(request=_OPENAI_REQUEST)
    assert exc_info.value.http_status == 502


@pytest.mark.asyncio
async def test_4xx_non_429_translates_to_LLMClientError():
    """openai.APIStatusError(status_code=400) → LLMClientError (deterministic,
    not a failover trigger; e.g. insufficient_quota / bad request)."""
    adapter = OpenAIAdapter(api_key="test-key")
    sdk_exc = openai.APIStatusError(
        message="Bad request",
        response=MagicMock(status_code=400, request=MagicMock()),
        body={"error": {"type": "invalid_request_error"}},
    )
    adapter.client.responses.create = AsyncMock(side_effect=sdk_exc)

    with pytest.raises(LLMClientError) as exc_info:
        await adapter.generate_content(request=_OPENAI_REQUEST)
    assert exc_info.value.http_status == 400
