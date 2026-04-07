"""
Integration tests for the FORCE_TOOL_USE_SENDS_CORRECT_MODE contract.

Tests the full adapter.generate_content() path (real adapter code, mocked SDK)
and validates the output against the centralized ContractRule — the "rule repository".

Origin: ClaudeAdapter sent tool_choice="auto" instead of {"type":"any"} when
force_tool_use=True. This allowed Claude to return plain text instead of a tool
call. The LLM post-processing fallback masked the regression for an extended period.
"""
import pytest
from unittest.mock import patch

from src.adapters.claude_adapter import ClaudeAdapter
from src.adapters.gemini_adapter import GeminiAdapter
from src.adapters.grok_adapter import GrokAdapter
from src.adapters.openai_adapter import OpenAIAdapter
from src.ports.llm_port import LLMRequest, Message, MessagePart
from tests.contracts.adapter_contracts import FORCE_TOOL_USE_SENDS_CORRECT_MODE
from tests.integration.adapters.conftest import (
    ClaudeCapturingStub,
    GeminiCapturingStub,
    OpenAILikeCapturingStub,
    OpenAIResponsesCapturingStub,
)

_MESSAGES = [Message(role="user", parts=[MessagePart(text="Hi")])]
_TOOLS = [
    {
        "name": "search_memory",
        "description": "Search memories",
        "parameters": {"type": "object", "properties": {}},
    }
]


@pytest.mark.asyncio
async def test_claude_force_tool_use_contract():
    """THE ORIGINAL BUG: ClaudeAdapter must use {'type':'any'}, not 'auto'."""
    adapter = ClaudeAdapter(api_key="test-key")
    stub = ClaudeCapturingStub.with_tool_response("search_memory", {"q": "x"}).install(adapter)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-sonnet-4-6",
            system_instruction="test",
            messages=_MESSAGES,
            tools=_TOOLS,
            force_tool_use=True,
        )
    )

    FORCE_TOOL_USE_SENDS_CORRECT_MODE.validate("claude", stub.captured_kwargs)


@pytest.mark.asyncio
async def test_gemini_force_tool_use_contract():
    """GeminiAdapter must set FunctionCallingConfig.mode='ANY' when force_tool_use=True."""
    adapter = GeminiAdapter(api_key="test-key")
    stub = GeminiCapturingStub().install(adapter)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="gemini-flash-latest",
            messages=_MESSAGES,
            tools=_TOOLS,
            force_tool_use=True,
        )
    )

    FORCE_TOOL_USE_SENDS_CORRECT_MODE.validate("gemini", stub.captured_kwargs)


@pytest.mark.asyncio
async def test_grok_force_tool_use_contract():
    """GrokAdapter must use tool_choice='required' when force_tool_use=True."""
    with patch("src.adapters.grok_adapter.socket.gethostbyname", return_value="0.0.0.0"):
        adapter = GrokAdapter(api_key="test-key")
    stub = OpenAILikeCapturingStub().install(adapter)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4-1-fast-non-reasoning",
            messages=_MESSAGES,
            tools=_TOOLS,
            force_tool_use=True,
        )
    )

    FORCE_TOOL_USE_SENDS_CORRECT_MODE.validate("grok", stub.captured_kwargs)


@pytest.mark.asyncio
async def test_openai_force_tool_use_contract():
    """OpenAIAdapter must use tool_choice='required' when force_tool_use=True."""
    adapter = OpenAIAdapter(api_key="test-key")
    stub = OpenAIResponsesCapturingStub().install(adapter)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="gpt-5-mini",
            messages=_MESSAGES,
            tools=_TOOLS,
            force_tool_use=True,
        )
    )

    FORCE_TOOL_USE_SENDS_CORRECT_MODE.validate("openai", stub.captured_kwargs)
