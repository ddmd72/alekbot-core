"""BaseAgent under a TurnClock (RFC §5.1, plan delta D2)."""
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.base_agent import BaseAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentResponse
from src.domain.exceptions import LLMServerError, TranscriptLockedError
from src.domain.retry_policy import NO_RETRY_POLICY
from src.domain.turn_clock import CURRENT_TURN_CLOCK, TurnClock
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


class _MinimalAgent(BaseAgent):
    """Concrete subclass with trivial can_handle/execute — only used to reach
    ``_effective_retry_policy``. Mirrors ``_MinimalAgent`` in
    tests/unit/agents/core/test_base_agent_fallback.py.
    """

    async def can_handle(self, message: AgentMessage) -> bool:
        return True

    async def execute(self, message: AgentMessage) -> AgentResponse:
        raise NotImplementedError("not used in these tests")


@pytest.fixture
def make_base_agent():
    def _make(agent_type: str) -> BaseAgent:
        config = AgentConfig(agent_id="a", agent_type=agent_type)
        return _MinimalAgent(config)

    return _make


def _policy_for(agent: BaseAgent, context: Dict[str, Any]):
    # The helper extracted in Step 3; the retry loop uses it.
    return agent._effective_retry_policy(context)


def test_orchestrator_of_a_clocked_turn_gets_no_whole_turn_retry(make_base_agent):
    agent = make_base_agent(agent_type="smart_response")
    token = CURRENT_TURN_CLOCK.set(TurnClock.start(orchestrator_agent_type="smart_response"))
    try:
        assert _policy_for(agent, {}) is NO_RETRY_POLICY
    finally:
        CURRENT_TURN_CLOCK.reset(token)


def test_specialist_inside_a_clocked_turn_keeps_its_retry(make_base_agent):
    agent = make_base_agent(agent_type="web_search")
    token = CURRENT_TURN_CLOCK.set(TurnClock.start(orchestrator_agent_type="smart_response"))
    try:
        assert _policy_for(agent, {}) is agent.retry_policy
    finally:
        CURRENT_TURN_CLOCK.reset(token)


def test_no_clock_keeps_todays_behaviour(make_base_agent):
    agent = make_base_agent(agent_type="smart_response")
    assert _policy_for(agent, {}) is agent.retry_policy
    assert _policy_for(agent, {"suppress_transient_retry": True}) is NO_RETRY_POLICY


# --------------------------------------------------------------------------- #
# Fix round 1 — same-provider retry (_call_llm) respects the clock: no retry
# once in the wrap-up reserve, and the retry request's timeout is clamped to
# the clock's CURRENT remaining budget rather than reusing the stale value
# the call started with.
# --------------------------------------------------------------------------- #


class _ClockMinimalAgent(BaseAgent):
    """Concrete subclass with ``llm`` injected directly — only used to reach
    ``_call_llm``'s same-provider retry loop. Mirrors ``_MinimalAgent`` in
    tests/unit/agents/core/test_base_agent_fallback.py.
    """

    def __init__(self, config: AgentConfig, llm: LLMPort):
        super().__init__(config)
        self.llm = llm

    async def can_handle(self, message: AgentMessage) -> bool:
        return True

    async def execute(self, message: AgentMessage) -> AgentResponse:
        raise NotImplementedError("not used in these tests")


def _make_clock_test_config() -> AgentConfig:
    return AgentConfig(agent_id="clock_retry_test_agent", agent_type="quick")


def _make_locked_request(timeout: Optional[int] = None) -> LLMRequest:
    """A provider-locked multi-turn transcript (tool_response part present) —
    mirrors ``_make_locked_request`` in test_base_agent_fallback.py."""
    return LLMRequest(
        model_name="gemini-flash",
        system_instruction="test",
        messages=[
            Message(role="user", parts=[MessagePart(text="hi")]),
            Message(role="user", parts=[MessagePart(
                tool_response={"name": "search_memory", "response": {"result": "x"}}
            )]),
        ],
        timeout=timeout,
    )


def _make_llm_response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=[],
        raw_content=None,
        usage_metadata=UsageMetadata(prompt_tokens=5, completion_tokens=3, total_tokens=8),
    )


def _make_clock_execution_context(primary_llm: LLMPort) -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="quick",
        provider=primary_llm,
        model_name="gemini-flash",
        tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities(),
        provider_name="gemini",
        resilience_port=InMemoryProviderResilience(),
    )


@pytest.fixture
def clock_primary_llm():
    llm = MagicMock(spec=LLMPort)
    llm.generate_content = AsyncMock(return_value=_make_llm_response("primary ok"))
    return llm


async def test_same_provider_retry_skipped_when_clock_in_reserve(clock_primary_llm):
    """Transcript-locked transient error + clock already in its wrap-up reserve
    → no same-provider retry attempt: generate_content is awaited exactly once
    (the failing initial call) and the terminal TranscriptLockedError propagates.
    """
    agent = _ClockMinimalAgent(config=_make_clock_test_config(), llm=clock_primary_llm)
    exc = LLMServerError("529 overloaded", http_status=529)
    clock_primary_llm.generate_content = AsyncMock(side_effect=exc)
    agent._set_execution_context(_make_clock_execution_context(clock_primary_llm))

    # budget_s < wrap_up_reserve_s: remaining() <= wrap_up_reserve_s immediately,
    # so in_reserve()/not can_retry() is true from the very first attempt.
    clock = TurnClock.start(budget_s=50, wrap_up_reserve_s=100)
    token = CURRENT_TURN_CLOCK.set(clock)
    try:
        with patch("src.agents.base_agent.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(TranscriptLockedError):
                await agent._call_llm(_make_locked_request())
    finally:
        CURRENT_TURN_CLOCK.reset(token)

    assert clock_primary_llm.generate_content.await_count == 1


async def test_same_provider_retry_clamps_stale_timeout_to_remaining_budget(clock_primary_llm):
    """Transcript-locked transient error, clock NOT in reserve → the retry fires,
    but its request's timeout is re-clamped to the clock's remaining budget at
    retry time instead of reusing the stale timeout the original call started
    with (a call that started with T=1380s and failed after 1000s must not
    retry with T=1380s again — it could run past the wrap-up reserve / hard stop).
    """
    agent = _ClockMinimalAgent(config=_make_clock_test_config(), llm=clock_primary_llm)
    exc = LLMServerError("529 overloaded", http_status=529)
    clock_primary_llm.generate_content = AsyncMock(
        side_effect=[exc, _make_llm_response("primary recovered")]
    )
    agent._set_execution_context(_make_clock_execution_context(clock_primary_llm))

    clock = TurnClock.start(budget_s=600, wrap_up_reserve_s=100)
    # Largest possible ceiling for the retry's clamped timeout — captured BEFORE
    # the call so elapsed test time can only make the real retry-time value
    # smaller, never larger; keeps the assertion below non-flaky.
    ceiling_before_call = clock.call_timeout()
    stale_request = _make_locked_request(timeout=1380)

    token = CURRENT_TURN_CLOCK.set(clock)
    try:
        with patch("src.agents.base_agent.asyncio.sleep", new=AsyncMock()):
            response = await agent._call_llm(stale_request)
    finally:
        CURRENT_TURN_CLOCK.reset(token)

    assert response.text == "primary recovered"
    assert clock_primary_llm.generate_content.await_count == 2
    retry_request: LLMRequest = clock_primary_llm.generate_content.call_args_list[1].kwargs["request"]
    assert retry_request.timeout <= ceiling_before_call
    assert retry_request.timeout < stale_request.timeout


# --------------------------------------------------------------------------- #
# Final review I3 — cross-provider failover respects the clock: skipped in the
# wrap-up reserve, otherwise the fallback request's timeout is clamped.
# --------------------------------------------------------------------------- #


def _make_failover_context(primary_llm: LLMPort, fallback_llm: LLMPort) -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="quick",
        provider=primary_llm,
        model_name="gemini-flash",
        tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities(),
        provider_name="gemini",
        fallback_provider=fallback_llm,
        fallback_model_name="claude-sonnet",
        fallback_provider_name="claude",
        resilience_port=InMemoryProviderResilience(),
    )


def _make_fresh_request(timeout: Optional[int] = None) -> LLMRequest:
    """Not transcript-locked: a primary failure goes to cross-provider failover."""
    return LLMRequest(
        model_name="gemini-flash",
        system_instruction="test",
        messages=[Message(role="user", parts=[MessagePart(text="hi")])],
        timeout=timeout,
    )


@pytest.fixture
def failover_llms():
    from src.domain.exceptions import LLMUnavailableError
    primary = MagicMock(spec=LLMPort)
    primary.generate_content = AsyncMock(
        side_effect=LLMUnavailableError("unavailable", http_status=503))
    fallback = MagicMock(spec=LLMPort)
    fallback.generate_content = AsyncMock(return_value=_make_llm_response("fallback ok"))
    return primary, fallback


async def test_failover_skipped_when_clock_in_reserve(failover_llms):
    from src.domain.exceptions import BothProvidersUnavailableError
    primary, fallback = failover_llms
    agent = _ClockMinimalAgent(config=_make_clock_test_config(), llm=primary)
    agent._set_execution_context(_make_failover_context(primary, fallback))
    clock = TurnClock.start(budget_s=50, wrap_up_reserve_s=100)   # in reserve at once
    token = CURRENT_TURN_CLOCK.set(clock)
    try:
        with pytest.raises(BothProvidersUnavailableError):
            await agent._call_llm(_make_fresh_request())
    finally:
        CURRENT_TURN_CLOCK.reset(token)
    fallback.generate_content.assert_not_awaited()


async def test_failover_clamps_a_stale_timeout_to_the_clock(failover_llms):
    primary, fallback = failover_llms
    agent = _ClockMinimalAgent(config=_make_clock_test_config(), llm=primary)
    agent._set_execution_context(_make_failover_context(primary, fallback))
    clock = TurnClock.start(budget_s=600, wrap_up_reserve_s=100)
    ceiling = clock.call_timeout()
    token = CURRENT_TURN_CLOCK.set(clock)
    try:
        response = await agent._call_llm(_make_fresh_request(timeout=1380))
    finally:
        CURRENT_TURN_CLOCK.reset(token)
    assert response.text == "fallback ok"
    sent: LLMRequest = fallback.generate_content.call_args.kwargs["request"]
    assert sent.timeout <= ceiling and sent.model_name == "claude-sonnet"


async def test_failover_without_a_timeout_gets_the_clock_timeout(failover_llms):
    primary, fallback = failover_llms
    agent = _ClockMinimalAgent(config=_make_clock_test_config(), llm=primary)
    agent._set_execution_context(_make_failover_context(primary, fallback))
    clock = TurnClock.start(budget_s=600, wrap_up_reserve_s=100)
    ceiling = clock.call_timeout()
    token = CURRENT_TURN_CLOCK.set(clock)
    try:
        await agent._call_llm(_make_fresh_request(timeout=None))
    finally:
        CURRENT_TURN_CLOCK.reset(token)
    sent: LLMRequest = fallback.generate_content.call_args.kwargs["request"]
    assert sent.timeout is not None and 1 <= sent.timeout <= ceiling


async def test_failover_without_a_clock_keeps_the_request_timeout(failover_llms):
    primary, fallback = failover_llms
    agent = _ClockMinimalAgent(config=_make_clock_test_config(), llm=primary)
    agent._set_execution_context(_make_failover_context(primary, fallback))
    await agent._call_llm(_make_fresh_request(timeout=1380))
    sent: LLMRequest = fallback.generate_content.call_args.kwargs["request"]
    assert sent.timeout == 1380
