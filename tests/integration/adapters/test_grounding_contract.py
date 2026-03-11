"""
Integration tests for the GROUNDING_INJECTS_SEARCH_TOOL contract.

Verifies that each adapter correctly injects the provider's native search
tool when use_grounding=True. The search tool must be prepended so it takes
priority over domain function tools.
"""
import pytest
from unittest.mock import patch

from src.adapters.claude_adapter import ClaudeAdapter
from src.adapters.gemini_adapter import GeminiAdapter
from src.adapters.grok_adapter import GrokAdapter
from src.adapters.openai_adapter import OpenAIAdapter
from src.ports.llm_port import LLMRequest, Message, MessagePart
from tests.contracts.adapter_contracts import GROUNDING_INJECTS_SEARCH_TOOL
from tests.integration.adapters.conftest import (
    ClaudeCapturingStub,
    GeminiCapturingStub,
    OpenAILikeCapturingStub,
)

_MESSAGES = [Message(role="user", parts=[MessagePart(text="Hi")])]


@pytest.mark.asyncio
async def test_claude_grounding_contract():
    """Claude: use_grounding=True → web_search_20250305 in tools list."""
    adapter = ClaudeAdapter(api_key="test-key")
    stub = ClaudeCapturingStub().install(adapter)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="claude-sonnet-4-6",
            system_instruction="test",
            messages=_MESSAGES,
            use_grounding=True,
        )
    )

    GROUNDING_INJECTS_SEARCH_TOOL.validate("claude", stub.captured_kwargs)


@pytest.mark.asyncio
async def test_gemini_grounding_contract():
    """Gemini: use_grounding=True → GoogleSearch in config.tools."""
    adapter = GeminiAdapter(api_key="test-key")
    stub = GeminiCapturingStub().install(adapter)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="gemini-flash-latest",
            messages=_MESSAGES,
            use_grounding=True,
        )
    )

    GROUNDING_INJECTS_SEARCH_TOOL.validate("gemini", stub.captured_kwargs)


@pytest.mark.asyncio
async def test_grok_grounding_contract():
    """Grok: use_grounding=True → web_search in tools list."""
    with patch("src.adapters.grok_adapter.socket.gethostbyname", return_value="0.0.0.0"):
        adapter = GrokAdapter(api_key="test-key")
    stub = OpenAILikeCapturingStub().install(adapter)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="grok-4-1-fast-non-reasoning",
            messages=_MESSAGES,
            use_grounding=True,
        )
    )

    GROUNDING_INJECTS_SEARCH_TOOL.validate("grok", stub.captured_kwargs)


@pytest.mark.asyncio
async def test_openai_grounding_contract():
    """OpenAI: use_grounding=True → web_search in tools list."""
    adapter = OpenAIAdapter(api_key="test-key")
    stub = OpenAILikeCapturingStub().install(adapter)

    await adapter.generate_content(
        request=LLMRequest(
            model_name="gpt-5-mini",
            messages=_MESSAGES,
            use_grounding=True,
        )
    )

    GROUNDING_INJECTS_SEARCH_TOOL.validate("openai", stub.captured_kwargs)
