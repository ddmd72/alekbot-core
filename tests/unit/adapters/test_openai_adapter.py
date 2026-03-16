import json
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from src.adapters.openai_adapter import OpenAIAdapter
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
# Capabilities and tier mapping
# ============================================================================

def test_openai_capabilities():
    adapter = OpenAIAdapter(api_key="test-key")
    caps = adapter.get_capabilities()

    assert caps.native_tools is True
    assert caps.context_caching is False
    assert caps.vision is True
    assert caps.supports_json_mode is True
    assert caps.supports_system_prompt is True
    assert caps.max_context_window == 1047576


def test_openai_model_for_tier():
    adapter = OpenAIAdapter(api_key="test-key")

    assert adapter.get_model_for_tier(PerformanceTier.ECO) == "gpt-5-nano"
    assert adapter.get_model_for_tier(PerformanceTier.BALANCED) == "gpt-5-mini"
    assert adapter.get_model_for_tier(PerformanceTier.PERFORMANCE) == "gpt-5.2"


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
# _convert_tools — tool schema conversion
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
    assert result[0]["function"]["name"] == "search_memory"
    assert result[0]["function"]["description"] == "Search personal knowledge base"
    assert result[0]["function"]["parameters"]["properties"]["query"]["type"] == "string"


def test_convert_tools_empty():
    adapter = OpenAIAdapter(api_key="test-key")
    assert adapter._convert_tools([]) == []


def test_convert_tools_no_description():
    adapter = OpenAIAdapter(api_key="test-key")
    tools = [{"name": "foo"}]

    result = adapter._convert_tools(tools)

    assert result[0]["function"]["description"] == ""


# ============================================================================
# _convert_messages — message format conversion
# ============================================================================

def test_convert_messages_system_instruction():
    adapter = OpenAIAdapter(api_key="test-key")
    messages = [Message(role="user", parts=[MessagePart(text="Hello")])]

    result = adapter._convert_messages(messages, system_instruction="You are helpful.")

    assert result[0]["role"] == "system"
    assert result[0]["content"] == "You are helpful."
    assert result[1]["role"] == "user"


def test_convert_messages_no_system_instruction():
    adapter = OpenAIAdapter(api_key="test-key")
    messages = [Message(role="user", parts=[MessagePart(text="Hello")])]

    result = adapter._convert_messages(messages)

    assert result[0]["role"] == "user"
    assert len(result) == 1


def test_convert_messages_user_text():
    adapter = OpenAIAdapter(api_key="test-key")
    messages = [Message(role="user", parts=[MessagePart(text="What is 2+2?")])]

    result = adapter._convert_messages(messages)

    assert result[0]["role"] == "user"
    assert result[0]["content"] == "What is 2+2?"


def test_convert_messages_model_text():
    adapter = OpenAIAdapter(api_key="test-key")
    messages = [Message(role="model", parts=[MessagePart(text="The answer is 4.")])]

    result = adapter._convert_messages(messages)

    assert result[0]["role"] == "assistant"
    assert result[0]["content"] == "The answer is 4."


def test_convert_messages_model_with_raw_content():
    """raw_content (OpenAI ChatCompletionMessage) is reconstructed directly to preserve tool_call IDs."""
    adapter = OpenAIAdapter(api_key="test-key")

    raw = MagicMock()
    raw.content = "Let me search."
    raw.tool_calls = []

    messages = [Message(role="model", parts=[], raw_content=raw)]
    result = adapter._convert_messages(messages)

    assert result[0]["role"] == "assistant"
    assert result[0]["content"] == "Let me search."


def test_convert_messages_model_with_raw_content_and_tool_calls():
    """tool_call IDs from raw_content are preserved exactly."""
    adapter = OpenAIAdapter(api_key="test-key")

    tc = MagicMock()
    tc.id = "call_abc123"
    tc.function.name = "search_memory"
    tc.function.arguments = '{"query": "birthday"}'

    raw = MagicMock()
    raw.content = None
    raw.tool_calls = [tc]

    messages = [Message(role="model", parts=[], raw_content=raw)]
    result = adapter._convert_messages(messages)

    assert result[0]["tool_calls"][0]["id"] == "call_abc123"
    assert result[0]["tool_calls"][0]["function"]["name"] == "search_memory"


def test_convert_messages_vision_base64():
    """Base64 images are wrapped in image_url format."""
    adapter = OpenAIAdapter(api_key="test-key")
    messages = [Message(
        role="user",
        parts=[MessagePart(file_data={"base64": "abc123", "mime_type": "image/jpeg"})]
    )]

    result = adapter._convert_messages(messages)

    assert result[0]["content"][0]["type"] == "image_url"
    assert "data:image/jpeg;base64,abc123" in result[0]["content"][0]["image_url"]["url"]


def test_convert_messages_vision_non_image_skipped():
    """Non-image MIME types are skipped (OpenAI vision accepts image/* only)."""
    adapter = OpenAIAdapter(api_key="test-key")
    messages = [Message(
        role="user",
        parts=[MessagePart(file_data={"base64": "abc123", "mime_type": "application/pdf"})]
    )]

    result = adapter._convert_messages(messages)

    # No content parts should have been added — message is empty or dropped
    assert len(result) == 0 or result[0].get("content") is None


# ============================================================================
# _convert_messages — PROMPT_CACHE_BOUNDARY stripped
# ============================================================================

@pytest.mark.asyncio
async def test_cache_boundary_stripped_from_system_instruction():
    """PROMPT_CACHE_BOUNDARY must not reach the OpenAI API (stripped before the SDK call)."""
    adapter = OpenAIAdapter(api_key="test-key")

    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock()]
    mock_completion.choices[0].message.content = "OK"
    mock_completion.choices[0].message.tool_calls = None
    mock_completion.usage = None

    captured_kwargs = {}

    async def mock_create(**kwargs):
        captured_kwargs.update(kwargs)
        return mock_completion

    adapter.client.chat.completions.create = mock_create

    static = "You are a helpful assistant."
    dynamic = "Current date: 2026-03-04"
    system = f"{static}\n\n{PROMPT_CACHE_BOUNDARY}\n{dynamic}"

    await adapter.generate_content(
        model_name="gpt-5-mini",
        system_instruction=system,
        messages=[Message(role="user", parts=[MessagePart(text="Hi")])],
    )

    sent_system = captured_kwargs.get("messages", [{}])[0].get("content", "")
    assert PROMPT_CACHE_BOUNDARY not in sent_system, (
        f"PROMPT_CACHE_BOUNDARY must be stripped before sending to OpenAI; got: {sent_system!r}"
    )
    assert static in sent_system
    assert dynamic in sent_system


# ============================================================================
# generate_content — API call path (mocked)
# ============================================================================

@pytest.mark.asyncio
async def test_generate_content_excludes_temperature_for_gpt5():
    """Temperature must not be sent for gpt-5 family (400 error from API)."""
    adapter = OpenAIAdapter(api_key="test-key")

    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock()]
    mock_completion.choices[0].message.content = "Hello"
    mock_completion.choices[0].message.tool_calls = None
    mock_completion.usage = None

    captured_kwargs = {}

    async def mock_create(**kwargs):
        captured_kwargs.update(kwargs)
        return mock_completion

    adapter.client.chat.completions.create = mock_create

    await adapter.generate_content(
        model_name="gpt-5-mini",
        system_instruction="You are helpful.",
        messages=[Message(role="user", parts=[MessagePart(text="Hi")])],
        temperature=0.8,
    )

    assert "temperature" not in captured_kwargs


@pytest.mark.asyncio
async def test_generate_content_includes_temperature_for_gpt4():
    """Temperature must be included for non-gpt-5 models."""
    adapter = OpenAIAdapter(api_key="test-key")

    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock()]
    mock_completion.choices[0].message.content = "Hello"
    mock_completion.choices[0].message.tool_calls = None
    mock_completion.usage = None

    captured_kwargs = {}

    async def mock_create(**kwargs):
        captured_kwargs.update(kwargs)
        return mock_completion

    adapter.client.chat.completions.create = mock_create

    await adapter.generate_content(
        model_name="gpt-4o",
        system_instruction="You are helpful.",
        messages=[Message(role="user", parts=[MessagePart(text="Hi")])],
        temperature=0.8,
    )

    assert "temperature" in captured_kwargs
    assert captured_kwargs["temperature"] == 0.8


@pytest.mark.asyncio
async def test_generate_content_json_mode():
    """response_mime_type=application/json activates response_format=json_object."""
    adapter = OpenAIAdapter(api_key="test-key")

    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock()]
    mock_completion.choices[0].message.content = '{"answer": 42}'
    mock_completion.choices[0].message.tool_calls = None
    mock_completion.usage = None

    captured_kwargs = {}

    async def mock_create(**kwargs):
        captured_kwargs.update(kwargs)
        return mock_completion

    adapter.client.chat.completions.create = mock_create

    await adapter.generate_content(
        model_name="gpt-5-mini",
        system_instruction="Return JSON.",
        messages=[Message(role="user", parts=[MessagePart(text="Hi")])],
        response_mime_type="application/json",
    )

    assert captured_kwargs.get("response_format") == {"type": "json_object"}


@pytest.mark.asyncio
async def test_generate_content_returns_tool_calls():
    """Tool calls in API response are mapped to domain ToolCall objects."""
    adapter = OpenAIAdapter(api_key="test-key")

    tc_mock = MagicMock()
    tc_mock.id = "call_xyz"
    tc_mock.function.name = "search_memory"
    tc_mock.function.arguments = '{"query": "vacation plans"}'

    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock()]
    mock_completion.choices[0].message.content = ""
    mock_completion.choices[0].message.tool_calls = [tc_mock]
    mock_completion.usage = None

    async def mock_create(**kwargs):
        return mock_completion

    adapter.client.chat.completions.create = mock_create

    response = await adapter.generate_content(
        model_name="gpt-5-mini",
        system_instruction="Use tools.",
        messages=[Message(role="user", parts=[MessagePart(text="Find my vacation plans")])],
    )

    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "search_memory"
    assert response.tool_calls[0].args == {"query": "vacation plans"}
    assert response.tool_calls[0].thought_signature == "call_xyz"


@pytest.mark.asyncio
async def test_generate_content_uses_max_completion_tokens():
    """max_completion_tokens (not max_tokens) must be used in API call."""
    adapter = OpenAIAdapter(api_key="test-key")

    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock()]
    mock_completion.choices[0].message.content = "OK"
    mock_completion.choices[0].message.tool_calls = None
    mock_completion.usage = None

    captured_kwargs = {}

    async def mock_create(**kwargs):
        captured_kwargs.update(kwargs)
        return mock_completion

    adapter.client.chat.completions.create = mock_create

    await adapter.generate_content(
        request=LLMRequest(
            model_name="gpt-5-mini",
            system_instruction="You are helpful.",
            messages=[Message(role="user", parts=[MessagePart(text="Hi")])],
            max_tokens=1000,
        ),
    )

    assert "max_completion_tokens" in captured_kwargs
    assert captured_kwargs["max_completion_tokens"] == 1000
    assert "max_tokens" not in captured_kwargs


# ============================================================================
# use_grounding — native web search tool injection
# ============================================================================

@pytest.mark.asyncio
async def test_use_grounding_injects_web_search_tool():
    """use_grounding=True must prepend {"type": "web_search"} to tools."""
    adapter = OpenAIAdapter(api_key="test-key")

    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock()]
    mock_completion.choices[0].message.content = "Search result"
    mock_completion.choices[0].message.tool_calls = None
    mock_completion.usage = None

    captured_kwargs = {}

    async def mock_create(**kwargs):
        captured_kwargs.update(kwargs)
        return mock_completion

    adapter.client.chat.completions.create = mock_create

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

    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock()]
    mock_completion.choices[0].message.content = "Answer"
    mock_completion.choices[0].message.tool_calls = None
    mock_completion.usage = None

    captured_kwargs = {}

    async def mock_create(**kwargs):
        captured_kwargs.update(kwargs)
        return mock_completion

    adapter.client.chat.completions.create = mock_create

    await adapter.generate_content(
        model_name="gpt-5-mini",
        messages=[Message(role="user", parts=[MessagePart(text="Hello")])],
    )

    tools_sent = captured_kwargs.get("tools")
    assert tools_sent is None or not any(
        isinstance(t, dict) and t.get("type") == "web_search" for t in (tools_sent or [])
    )
