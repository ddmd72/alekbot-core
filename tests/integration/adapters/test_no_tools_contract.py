"""
Integration tests for the FORCE_TOOL_USE_WITHOUT_TOOLS_OMITS_TOOL_CHOICE contract.

When force_tool_use=True but no tools are provided, the adapter must not include
tool_choice in the SDK call. Provider APIs return a 400 error when tool_choice is
set but the tools list is absent or empty.
"""
import pytest
from unittest.mock import patch

from src.adapters.claude_adapter import ClaudeAdapter
from src.adapters.grok_adapter import GrokAdapter
from src.adapters.openai_adapter import OpenAIAdapter
from src.ports.llm_port import LLMRequest, Message, MessagePart
from tests.contracts.adapter_contracts import FORCE_TOOL_USE_WITHOUT_TOOLS_OMITS_TOOL_CHOICE
from tests.integration.adapters.conftest import (
    ClaudeCapturingStub,
    OpenAILikeCapturingStub,
    OpenAIResponsesCapturingStub,
)

_MESSAGES = [Message(role="user", parts=[MessagePart(text="Hi")])]


@pytest.mark.asyncio
async def test_claude_no_tools_contract():
    """Claude: force_tool_use=True but no tools → tool_choice absent from SDK call."""
    adapter = ClaudeAdapter(api_key="test-key")
    stub = ClaudeCapturingStub().install(adapter)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-sonnet-4-6",
            system_instruction="test",
            messages=_MESSAGES,
            force_tool_use=True,
        )
    )

    FORCE_TOOL_USE_WITHOUT_TOOLS_OMITS_TOOL_CHOICE.validate("claude", stub.captured_kwargs)


@pytest.mark.asyncio
async def test_grok_no_tools_contract():
    """Grok: force_tool_use=True but no tools → tool_choice absent from SDK call."""
    with patch("src.adapters.grok_adapter.socket.gethostbyname", return_value="0.0.0.0"):
        adapter = GrokAdapter(api_key="test-key")
    stub = OpenAILikeCapturingStub().install(adapter)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4-1-fast-non-reasoning",
            messages=_MESSAGES,
            force_tool_use=True,
        )
    )

    FORCE_TOOL_USE_WITHOUT_TOOLS_OMITS_TOOL_CHOICE.validate("grok", stub.captured_kwargs)


@pytest.mark.asyncio
async def test_openai_no_tools_contract():
    """OpenAI: force_tool_use=True but no tools → tool_choice absent from SDK call."""
    adapter = OpenAIAdapter(api_key="test-key")
    stub = OpenAIResponsesCapturingStub().install(adapter)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="gpt-5-mini",
            messages=_MESSAGES,
            force_tool_use=True,
        )
    )

    FORCE_TOOL_USE_WITHOUT_TOOLS_OMITS_TOOL_CHOICE.validate("openai", stub.captured_kwargs)
