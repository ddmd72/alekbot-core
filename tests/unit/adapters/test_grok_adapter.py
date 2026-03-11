"""
Unit tests for GrokAdapter.

Pattern: mock adapter.client at the SDK boundary (AsyncOpenAI), capture
what kwargs are sent to client.chat.completions.create(), assert on them.
This tests the translation layer (LLMRequest → SDK call), not business logic.
"""
import json
import pytest
from unittest.mock import MagicMock, patch

from src.adapters.grok_adapter import GrokAdapter
from src.domain.user import PerformanceTier
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


def _make_completion(text="OK"):
    """Minimal OpenAI ChatCompletion-like mock."""
    message = MagicMock()
    message.content = text
    message.tool_calls = None

    choice = MagicMock()
    choice.message = message

    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    usage.total_tokens = 15

    completion = MagicMock()
    completion.choices = [choice]
    completion.usage = usage
    return completion


def _make_completion_with_tool(name, args, tc_id="call_1"):
    """Completion with one tool_call."""
    tc = MagicMock()
    tc.id = tc_id
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = json.dumps(args)

    message = MagicMock()
    message.content = None
    message.tool_calls = [tc]

    choice = MagicMock()
    choice.message = message

    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    usage.total_tokens = 15

    completion = MagicMock()
    completion.choices = [choice]
    completion.usage = usage
    return completion


@pytest.fixture(autouse=True)
def suppress_dns(monkeypatch):
    """GrokAdapter.__init__ does a DNS preflight check; suppress it in unit tests."""
    monkeypatch.setattr(
        "src.adapters.grok_adapter.socket.gethostbyname",
        lambda host: "0.0.0.0",
    )


# ============================================================================
# Capabilities and tier mapping
# ============================================================================

def test_grok_capabilities():
    adapter = GrokAdapter(api_key="test-key")
    caps = adapter.get_capabilities()

    assert caps.native_tools is True
    assert caps.context_caching is False
    assert caps.vision is False
    assert caps.max_context_window == 2_000_000


def test_grok_model_for_tier():
    adapter = GrokAdapter(api_key="test-key")

    assert adapter.get_model_for_tier(PerformanceTier.ECO) == "grok-4-1-fast-non-reasoning"
    assert adapter.get_model_for_tier(PerformanceTier.BALANCED) == "grok-4-1-fast-reasoning"
    assert adapter.get_model_for_tier(PerformanceTier.PERFORMANCE) == "grok-4-1-fast-reasoning"


def test_grok_unsupported_tier_raises():
    adapter = GrokAdapter(api_key="test-key")

    with pytest.raises(ValueError, match="Unsupported tier"):
        adapter.get_model_for_tier("invalid_tier")


@pytest.mark.asyncio
async def test_grok_prompt_caching_raises():
    adapter = GrokAdapter(api_key="test-key")

    with pytest.raises(ValueError, match="does not support prompt caching"):
        await adapter.generate_content(
            request=LLMRequest(
                model_name="grok-4-1-fast-non-reasoning",
                messages=MESSAGES,
                cache_config=PromptCacheConfig(enabled=True),
            )
        )


# ============================================================================
# Wire tests: verify SDK call kwargs
# ============================================================================

@pytest.mark.asyncio
async def test_force_tool_use_sends_tool_choice_required():
    """force_tool_use=True + tools → tool_choice='required' in SDK call."""
    adapter = GrokAdapter(api_key="test-key")
    captured = {}

    async def mock_create(**kwargs):
        captured.update(kwargs)
        return _make_completion()

    adapter.client = MagicMock()
    adapter.client.chat.completions.create = mock_create

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4-1-fast-non-reasoning",
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

    async def mock_create(**kwargs):
        captured.update(kwargs)
        return _make_completion()

    adapter.client = MagicMock()
    adapter.client.chat.completions.create = mock_create

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4-1-fast-non-reasoning",
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

    async def mock_create(**kwargs):
        captured.update(kwargs)
        return _make_completion()

    adapter.client = MagicMock()
    adapter.client.chat.completions.create = mock_create

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4-1-fast-non-reasoning",
            messages=MESSAGES,
            force_tool_use=True,  # even with force=True, no tools → no tool_choice
        )
    )

    assert "tool_choice" not in captured, (
        f"tool_choice must be absent when tools is empty; keys={list(captured.keys())}"
    )


@pytest.mark.asyncio
async def test_use_grounding_injects_web_search_and_web_fetch():
    """use_grounding=True → web_search and web_fetch prepended to tools."""
    adapter = GrokAdapter(api_key="test-key")
    captured = {}

    async def mock_create(**kwargs):
        captured.update(kwargs)
        return _make_completion()

    adapter.client = MagicMock()
    adapter.client.chat.completions.create = mock_create

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4-1-fast-non-reasoning",
            messages=MESSAGES,
            use_grounding=True,
        )
    )

    tools = captured.get("tools") or []
    tool_types = [t.get("type") for t in tools if isinstance(t, dict)]
    assert "web_search" in tool_types, f"web_search missing; tools={tools}"
    assert "web_fetch" in tool_types, f"web_fetch missing; tools={tools}"
    # Both native tools must be prepended (first two)
    assert tool_types[0] == "web_search"
    assert tool_types[1] == "web_fetch"


@pytest.mark.asyncio
async def test_json_mode_via_response_schema():
    """response_schema → response_format={'type':'json_object'}."""
    adapter = GrokAdapter(api_key="test-key")
    captured = {}

    async def mock_create(**kwargs):
        captured.update(kwargs)
        return _make_completion(text='{"answer": "42"}')

    adapter.client = MagicMock()
    adapter.client.chat.completions.create = mock_create

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4-1-fast-non-reasoning",
            messages=MESSAGES,
            response_schema={"type": "object"},
        )
    )

    assert captured.get("response_format") == {"type": "json_object"}


@pytest.mark.asyncio
async def test_json_mode_via_mime_type():
    """response_mime_type='application/json' → response_format={'type':'json_object'}."""
    adapter = GrokAdapter(api_key="test-key")
    captured = {}

    async def mock_create(**kwargs):
        captured.update(kwargs)
        return _make_completion(text='{"ok": true}')

    adapter.client = MagicMock()
    adapter.client.chat.completions.create = mock_create

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4-1-fast-non-reasoning",
            messages=MESSAGES,
            response_mime_type="application/json",
        )
    )

    assert captured.get("response_format") == {"type": "json_object"}


@pytest.mark.asyncio
async def test_no_json_mode_by_default():
    """No schema and no mime type → response_format absent from kwargs."""
    adapter = GrokAdapter(api_key="test-key")
    captured = {}

    async def mock_create(**kwargs):
        captured.update(kwargs)
        return _make_completion()

    adapter.client = MagicMock()
    adapter.client.chat.completions.create = mock_create

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4-1-fast-non-reasoning",
            messages=MESSAGES,
        )
    )

    assert "response_format" not in captured


@pytest.mark.asyncio
async def test_tool_calls_parsed_from_response():
    """Tool call in completion → LLMResponse.tool_calls populated correctly."""
    adapter = GrokAdapter(api_key="test-key")

    async def mock_create(**kwargs):
        return _make_completion_with_tool("search_memory", {"query": "test"}, "call_abc")

    adapter.client = MagicMock()
    adapter.client.chat.completions.create = mock_create

    response = await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4-1-fast-non-reasoning",
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
async def test_system_instruction_prepended_as_system_message():
    """system_instruction → first message is role='system'."""
    adapter = GrokAdapter(api_key="test-key")
    captured = {}

    async def mock_create(**kwargs):
        captured.update(kwargs)
        return _make_completion()

    adapter.client = MagicMock()
    adapter.client.chat.completions.create = mock_create

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4-1-fast-non-reasoning",
            system_instruction="You are a helpful assistant.",
            messages=MESSAGES,
        )
    )

    msgs = captured.get("messages", [])
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "You are a helpful assistant."


@pytest.mark.asyncio
async def test_upload_file_raises_not_implemented():
    """Grok does not support vision — upload_file must raise NotImplementedError."""
    adapter = GrokAdapter(api_key="test-key")

    with pytest.raises(NotImplementedError):
        await adapter.upload_file("/some/path.jpg", "image/jpeg")
