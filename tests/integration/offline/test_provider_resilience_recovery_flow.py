"""
F4.5 Phase 2 — integration recovery flow test.

Verifies the open → cooldown → half-open → closed cycle end-to-end through
``BaseAgent._call_llm`` against a real ``InMemoryProviderResilience`` adapter
with an injectable monotonic clock.

Scope: NOT testing adapter SDK translations (covered by unit wire tests).
Scope: testing that ``BaseAgent`` consults the resilience port correctly,
records failures/successes at the right moments, and that the breaker state
machine transitions occur as expected across multiple ``_call_llm`` calls.
"""

import pytest
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.base_agent import BaseAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentResponse
from src.domain.exceptions import (
    BothProvidersUnavailableError,
    LLMRateLimitError,
    LLMUnavailableError,
)
from src.domain.user import PerformanceTier
from src.ports.llm_port import (
    AgentExecutionContext,
    LLMPort,
    LLMRequest,
    LLMResponse,
    Message,
    MessagePart,
    ProviderCapabilities,
    UsageMetadata,
)


# ----------------------------------------------------------------------------
# Test scaffolding
# ----------------------------------------------------------------------------

class _MinimalAgent(BaseAgent):
    """Concrete subclass exposing _call_llm — no real execute() needed."""

    def __init__(self, config: AgentConfig, llm: LLMPort):
        super().__init__(config)
        self.llm = llm

    async def can_handle(self, message: AgentMessage) -> bool:
        return True

    async def execute(self, message: AgentMessage) -> AgentResponse:
        raise NotImplementedError


def _make_response(text: str = "ok") -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=[],
        raw_content=None,
        usage_metadata=UsageMetadata(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


def _make_request() -> LLMRequest:
    return LLMRequest(
        model_name="primary-model",
        system_instruction="test",
        messages=[Message(role="user", parts=[MessagePart(text="hi")])],
    )


def _make_ctx(
    primary: LLMPort,
    fallback: Optional[LLMPort],
    resilience: InMemoryProviderResilience,
) -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="quick",
        provider=primary,
        model_name="primary-model",
        tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities(),
        provider_name="primary",
        fallback_provider=fallback,
        fallback_model_name="fallback-model" if fallback else None,
        fallback_provider_name="fallback" if fallback else None,
        resilience_port=resilience,
    )


class _FakeClock:
    """Injectable monotonic clock for deterministic breaker timing."""

    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ----------------------------------------------------------------------------
# Recovery flow
# ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_cooldown_halfopen_closed_cycle():
    """Full breaker lifecycle through BaseAgent._call_llm:

    1. Failures accumulate to threshold → breaker opens.
    2. Subsequent call short-circuits to fallback (no primary call attempted).
    3. After cooldown elapses, primary is retried (HALF-OPEN auto-transition).
    4. Successful primary call closes the breaker (record_success full-reset).
    5. Subsequent failures must accumulate fresh threshold to re-open.
    """
    clock = _FakeClock()
    resilience = InMemoryProviderResilience(
        failure_threshold=3,
        window_seconds=60.0,
        cooldown_seconds=30.0,
        time_source=clock,
    )

    primary = MagicMock(spec=LLMPort)
    fallback = MagicMock(spec=LLMPort)
    fallback.generate_content = AsyncMock(return_value=_make_response("from fallback"))

    agent = _MinimalAgent(
        config=AgentConfig(agent_id="t", agent_type="quick"),
        llm=primary,
    )
    agent._set_execution_context(_make_ctx(primary, fallback, resilience))

    # Phase 1: 3 failures → breaker opens
    primary.generate_content = AsyncMock(
        side_effect=LLMRateLimitError("429", http_status=429)
    )
    for _ in range(3):
        # Each call tries primary, fails, falls back to fallback (success).
        result = await agent._call_llm(_make_request())
        assert result.text == "from fallback"
    assert primary.generate_content.call_count == 3
    assert resilience.is_provider_open("primary") is True

    # Phase 2: breaker open → primary skipped → fallback dispatched without primary call
    primary.generate_content.reset_mock()
    result = await agent._call_llm(_make_request())
    assert result.text == "from fallback"
    primary.generate_content.assert_not_called()  # short-circuited

    # Phase 3: cooldown elapses → next is_provider_open transitions to False (HALF-OPEN)
    clock.advance(31.0)  # past cooldown_seconds=30
    # Make primary succeed on the half-open probe.
    primary.generate_content = AsyncMock(return_value=_make_response("primary recovered"))
    result = await agent._call_llm(_make_request())
    assert result.text == "primary recovered"
    primary.generate_content.assert_called_once()
    # record_success(primary) was called → state reset
    assert resilience.is_provider_open("primary") is False

    # Phase 4: fresh failures must hit threshold again to re-open
    primary.generate_content = AsyncMock(
        side_effect=LLMUnavailableError("503", http_status=503)
    )
    # First failure shouldn't open the breaker yet (threshold=3, window reset)
    await agent._call_llm(_make_request())
    assert resilience.is_provider_open("primary") is False
    await agent._call_llm(_make_request())
    assert resilience.is_provider_open("primary") is False
    await agent._call_llm(_make_request())
    assert resilience.is_provider_open("primary") is True


@pytest.mark.asyncio
async def test_window_eviction_prevents_premature_opening():
    """Failures spaced beyond window_seconds must NOT accumulate — window
    eviction (Phase 1 _evict logic) keeps stale failures from triggering open."""
    clock = _FakeClock()
    resilience = InMemoryProviderResilience(
        failure_threshold=3,
        window_seconds=10.0,
        cooldown_seconds=30.0,
        time_source=clock,
    )

    primary = MagicMock(spec=LLMPort)
    fallback = MagicMock(spec=LLMPort)
    fallback.generate_content = AsyncMock(return_value=_make_response("fb"))
    primary.generate_content = AsyncMock(
        side_effect=LLMRateLimitError("429", http_status=429)
    )

    agent = _MinimalAgent(
        config=AgentConfig(agent_id="t", agent_type="quick"),
        llm=primary,
    )
    agent._set_execution_context(_make_ctx(primary, fallback, resilience))

    # Two failures separated by > window_seconds → first evicted before second counts.
    await agent._call_llm(_make_request())  # failure at t=0
    clock.advance(15.0)  # past window
    await agent._call_llm(_make_request())  # failure at t=15 → window has only this one
    clock.advance(2.0)
    await agent._call_llm(_make_request())  # failure at t=17 → 2 in window

    # Only 2 failures within the rolling 10s window — breaker still closed.
    assert resilience.is_provider_open("primary") is False


@pytest.mark.asyncio
async def test_both_breakers_open_raises_terminal_error():
    """Once both primary and fallback are open, the next request raises
    BothProvidersUnavailableError immediately — no SDK calls attempted."""
    clock = _FakeClock()
    resilience = InMemoryProviderResilience(
        failure_threshold=2,
        window_seconds=60.0,
        cooldown_seconds=30.0,
        time_source=clock,
    )

    primary = MagicMock(spec=LLMPort)
    fallback = MagicMock(spec=LLMPort)
    primary.generate_content = AsyncMock(
        side_effect=LLMRateLimitError("primary 429", http_status=429)
    )
    fallback.generate_content = AsyncMock(
        side_effect=LLMUnavailableError("fallback 503", http_status=503)
    )

    agent = _MinimalAgent(
        config=AgentConfig(agent_id="t", agent_type="quick"),
        llm=primary,
    )
    agent._set_execution_context(_make_ctx(primary, fallback, resilience))

    # 2 round-trips: each tries primary → fails → tries fallback → fails
    # → BothProvidersUnavailableError. Each round records failure for both.
    for _ in range(2):
        with pytest.raises(BothProvidersUnavailableError):
            await agent._call_llm(_make_request())

    assert resilience.is_provider_open("primary") is True
    assert resilience.is_provider_open("fallback") is True

    # Reset call counts to verify next call attempts neither SDK
    primary.generate_content.reset_mock()
    fallback.generate_content.reset_mock()

    with pytest.raises(BothProvidersUnavailableError):
        await agent._call_llm(_make_request())

    primary.generate_content.assert_not_called()
    fallback.generate_content.assert_not_called()
