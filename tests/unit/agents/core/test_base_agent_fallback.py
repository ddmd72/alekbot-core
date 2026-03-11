"""
Unit tests for BaseAgent._call_llm fallback provider logic.

Covers:
- LLMRateLimitError → transparent retry with fallback_provider
- LLMUnavailableError → transparent retry with fallback_provider
- No fallback configured → original exception propagates
- Fallback uses fallback_model_name in request
- Structured log event="llm_fallback" is emitted
- Non-transient exception (RuntimeError) → not caught, propagates as-is
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass, field
from typing import Optional, List

from src.agents.base_agent import BaseAgent
from src.domain.agent import AgentMessage, AgentResponse, AgentConfig
from src.domain.exceptions import LLMRateLimitError, LLMUnavailableError
from src.ports.llm_port import (
    LLMPort, LLMRequest, LLMResponse, ProviderCapabilities, Message, MessagePart,
    UsageMetadata,
)
from src.domain.user import PerformanceTier
from src.services.agent_context_builder import AgentExecutionContext


# ============================================================================
# Minimal concrete agent for testing (only _call_llm matters)
# ============================================================================

class _MinimalAgent(BaseAgent):
    """Concrete subclass with trivial can_handle/execute — only used to reach _call_llm."""

    def __init__(self, config: AgentConfig, llm: LLMPort):
        super().__init__(config)
        self.llm = llm  # BaseAgent._call_llm looks for self.llm first

    async def can_handle(self, message: AgentMessage) -> bool:
        return True

    async def execute(self, message: AgentMessage) -> AgentResponse:
        raise NotImplementedError("not used in these tests")


# ============================================================================
# Helpers
# ============================================================================

def _make_config() -> AgentConfig:
    return AgentConfig(
        agent_id="test_agent",
        agent_type="quick",
    )


def _make_request() -> LLMRequest:
    return LLMRequest(
        model_name="gemini-flash",
        system_instruction="test",
        messages=[Message(role="user", parts=[MessagePart(text="hi")])],
    )


def _make_llm_response(text: str = "ok") -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=[],
        raw_content=None,
        usage_metadata=UsageMetadata(prompt_tokens=5, completion_tokens=3, total_tokens=8),
    )


def _make_execution_context(
    primary_llm: LLMPort,
    fallback_llm: Optional[LLMPort] = None,
    fallback_model: str = "claude-sonnet-4-6",
) -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="quick",
        provider=primary_llm,
        model_name="gemini-flash",
        tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities(),
        provider_name="gemini",
        fallback_provider=fallback_llm,
        fallback_model_name=fallback_model if fallback_llm else None,
        fallback_provider_name="claude" if fallback_llm else None,
    )


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def primary_llm():
    llm = MagicMock(spec=LLMPort)
    llm.generate_content = AsyncMock(return_value=_make_llm_response("primary ok"))
    return llm


@pytest.fixture
def fallback_llm():
    llm = MagicMock(spec=LLMPort)
    llm.generate_content = AsyncMock(return_value=_make_llm_response("fallback ok"))
    return llm


@pytest.fixture
def agent(primary_llm) -> _MinimalAgent:
    return _MinimalAgent(config=_make_config(), llm=primary_llm)


# ============================================================================
# Tests
# ============================================================================

@pytest.mark.asyncio
async def test_rate_limit_triggers_fallback(agent, primary_llm, fallback_llm):
    """LLMRateLimitError from primary → fallback_provider called transparently."""
    primary_llm.generate_content = AsyncMock(
        side_effect=LLMRateLimitError("rate limit", http_status=429)
    )
    ctx = _make_execution_context(primary_llm, fallback_llm, fallback_model="claude-sonnet-4-6")
    agent._set_execution_context(ctx)

    response = await agent._call_llm(_make_request())

    assert response.text == "fallback ok"
    fallback_llm.generate_content.assert_called_once()
    sent_request: LLMRequest = fallback_llm.generate_content.call_args.kwargs["request"]
    assert isinstance(sent_request, LLMRequest)
    assert sent_request.messages[0].parts[0].text == "hi"  # original content preserved


@pytest.mark.asyncio
async def test_unavailable_triggers_fallback(agent, primary_llm, fallback_llm):
    """LLMUnavailableError from primary → fallback_provider called transparently."""
    primary_llm.generate_content = AsyncMock(
        side_effect=LLMUnavailableError("unavailable", http_status=503)
    )
    ctx = _make_execution_context(primary_llm, fallback_llm)
    agent._set_execution_context(ctx)

    response = await agent._call_llm(_make_request())

    assert response.text == "fallback ok"
    fallback_llm.generate_content.assert_called_once()
    sent_request: LLMRequest = fallback_llm.generate_content.call_args.kwargs["request"]
    assert isinstance(sent_request, LLMRequest)
    assert sent_request.messages[0].parts[0].text == "hi"  # original content preserved


@pytest.mark.asyncio
async def test_fallback_uses_fallback_model_name(agent, primary_llm, fallback_llm):
    """Fallback request must use fallback_model_name, not primary model_name."""
    primary_llm.generate_content = AsyncMock(
        side_effect=LLMRateLimitError("rate limit", http_status=429)
    )
    ctx = _make_execution_context(primary_llm, fallback_llm, fallback_model="claude-haiku-4-5-20251001")
    agent._set_execution_context(ctx)

    await agent._call_llm(_make_request())

    call_kwargs = fallback_llm.generate_content.call_args
    sent_request: LLMRequest = call_kwargs.kwargs.get("request") or call_kwargs.args[0]
    assert sent_request.model_name == "claude-haiku-4-5-20251001"


@pytest.mark.asyncio
async def test_no_fallback_configured_re_raises(agent, primary_llm):
    """When no fallback_provider set, original exception propagates."""
    exc = LLMRateLimitError("rate limit", http_status=429)
    primary_llm.generate_content = AsyncMock(side_effect=exc)
    ctx = _make_execution_context(primary_llm, fallback_llm=None)
    agent._set_execution_context(ctx)

    with pytest.raises(LLMRateLimitError):
        await agent._call_llm(_make_request())


@pytest.mark.asyncio
async def test_no_context_set_re_raises(agent, primary_llm):
    """When _set_execution_context was never called, original exception propagates."""
    exc = LLMRateLimitError("rate limit", http_status=429)
    primary_llm.generate_content = AsyncMock(side_effect=exc)
    # _agent_execution_context stays None (no _set_execution_context call)

    with pytest.raises(LLMRateLimitError):
        await agent._call_llm(_make_request())


@pytest.mark.asyncio
async def test_non_transient_exception_not_caught(agent, primary_llm, fallback_llm):
    """RuntimeError is not a transient LLM error — must not be swallowed by fallback logic."""
    primary_llm.generate_content = AsyncMock(side_effect=RuntimeError("unexpected"))
    ctx = _make_execution_context(primary_llm, fallback_llm)
    agent._set_execution_context(ctx)

    with pytest.raises(RuntimeError, match="unexpected"):
        await agent._call_llm(_make_request())

    fallback_llm.generate_content.assert_not_called()


@pytest.mark.asyncio
async def test_fallback_emits_structured_log(agent, primary_llm, fallback_llm):
    """On fallback, logger.warning is called with event='llm_fallback' in extra."""
    primary_llm.generate_content = AsyncMock(
        side_effect=LLMRateLimitError("rate limit", http_status=429)
    )
    ctx = _make_execution_context(primary_llm, fallback_llm)
    agent._set_execution_context(ctx)

    with patch("src.agents.base_agent.logger") as mock_logger:
        await agent._call_llm(_make_request())

    mock_logger.warning.assert_called_once()
    call_args = mock_logger.warning.call_args
    extra = call_args.kwargs.get("extra", {})
    assert extra.get("event") == "llm_fallback"
    assert extra.get("primary_provider") == "gemini"
    assert extra.get("fallback_provider") == "claude"
    assert extra.get("error_type") == "rate_limit"
    assert extra.get("http_status") == 429


@pytest.mark.asyncio
async def test_fallback_log_error_type_unavailable(agent, primary_llm, fallback_llm):
    """LLMUnavailableError → error_type='unavailable' in structured log."""
    primary_llm.generate_content = AsyncMock(
        side_effect=LLMUnavailableError("overloaded", http_status=503)
    )
    ctx = _make_execution_context(primary_llm, fallback_llm)
    agent._set_execution_context(ctx)

    with patch("src.agents.base_agent.logger") as mock_logger:
        await agent._call_llm(_make_request())

    extra = mock_logger.warning.call_args.kwargs.get("extra", {})
    assert extra.get("error_type") == "unavailable"
    assert extra.get("http_status") == 503
