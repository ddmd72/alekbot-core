"""A success after a same-provider retry follows the same stale-success rule as a first-attempt success.

A locked transcript (a tool turn) retries the same provider. If other calls trip the breaker while the retry is in
flight, the retry's success is older evidence than the failures that tripped it and must not close the breaker
(`test_breaker_stale_success_interleaving.py` pins the same rule for the first attempt).
"""
from unittest.mock import AsyncMock, MagicMock, patch

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.base_agent import BaseAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentResponse
from src.domain.exceptions import LLMServerError
from src.domain.user import PerformanceTier
from src.ports.llm_port import (
    AgentExecutionContext, LLMPort, LLMRequest, LLMResponse, Message, MessagePart, ProviderCapabilities,
)


class _Agent(BaseAgent):
    async def can_handle(self, message: AgentMessage) -> bool:
        return True

    async def execute(self, message: AgentMessage) -> AgentResponse:
        raise NotImplementedError


class _FailsThenSucceeds:
    """First call: a transient server error. Second call (the retry): `during_retry` runs, then it succeeds."""

    def __init__(self, during_retry) -> None:
        self.calls = 0
        self._during_retry = during_retry

    async def generate_content(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            raise LLMServerError("529 overloaded", http_status=529)
        self._during_retry()
        return LLMResponse(text="recovered")


def _locked_request() -> LLMRequest:
    return LLMRequest(model_name="primary-model", messages=[
        Message(role="user", parts=[MessagePart(text="hi")]),
        Message(role="user", parts=[MessagePart(tool_response={"name": "search_memory", "response": {"result": "x"}})]),
    ])


async def _retry_succeeds(during_retry) -> InMemoryProviderResilience:
    resilience = InMemoryProviderResilience(
        failure_threshold=3, window_seconds=60.0, cooldown_seconds=30.0, time_source=lambda: 1000.0,
    )
    agent = _Agent(AgentConfig(agent_id="a1", agent_type="mock"))
    agent.llm = _FailsThenSucceeds(lambda: during_retry(resilience))
    agent._set_execution_context(AgentExecutionContext(
        agent_type="mock", provider=MagicMock(spec=LLMPort), model_name="primary-model", tier=PerformanceTier.ECO,
        capabilities=ProviderCapabilities(), provider_name="primary", resilience_port=resilience,
    ))
    with patch("src.agents.base_agent.asyncio.sleep", new=AsyncMock()):
        response = await agent._call_llm(_locked_request())
    assert response.text == "recovered"
    return resilience


async def test_a_retry_success_does_not_close_a_breaker_tripped_during_the_retry():
    def trip(resilience):
        resilience.record_failure("primary")
        resilience.record_failure("primary")   # with the first attempt's failure: the third

    resilience = await _retry_succeeds(trip)

    assert resilience.is_provider_open("primary")


async def test_a_retry_success_still_clears_the_failures_when_the_breaker_is_closed():
    resilience = await _retry_succeeds(lambda _r: None)
    resilience.record_failure("primary")
    resilience.record_failure("primary")   # the first attempt's failure was cleared: two are below the threshold

    assert not resilience.is_provider_open("primary")
