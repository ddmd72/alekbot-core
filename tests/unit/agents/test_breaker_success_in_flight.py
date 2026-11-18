"""P15-S7 beyond the pin: a success that was in flight still counts whenever the breaker did not trip meanwhile.

The pin (`test_breaker_stale_success_interleaving.py`) shows a success admitted before a trip leaves the breaker OPEN.
These tests hold the other two states the success can land in: failures below the threshold (the success clears them),
and a trip whose cooldown elapsed during the call (HALF-OPEN: the success closes the breaker).
"""
import asyncio
from unittest.mock import MagicMock

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.base_agent import BaseAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentResponse
from src.domain.user import PerformanceTier
from src.ports.llm_port import AgentExecutionContext, LLMPort, LLMRequest, LLMResponse, ProviderCapabilities

GATE_TIMEOUT_S = 5


class _Agent(BaseAgent):
    async def can_handle(self, message: AgentMessage) -> bool:
        return True

    async def execute(self, message: AgentMessage) -> AgentResponse:
        raise NotImplementedError


class _GatedLlm:
    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def generate_content(self, request: LLMRequest) -> LLMResponse:
        self.reached.set()
        await self.release.wait()
        return LLMResponse(text="success")


def _setup():
    now = [1000.0]
    resilience = InMemoryProviderResilience(
        failure_threshold=3, window_seconds=60.0, cooldown_seconds=30.0, time_source=lambda: now[0],
    )
    llm = _GatedLlm()
    agent = _Agent(AgentConfig(agent_id="a1", agent_type="mock"))
    agent.llm = llm
    agent._set_execution_context(AgentExecutionContext(
        agent_type="mock", provider=MagicMock(spec=LLMPort), model_name="primary-model", tier=PerformanceTier.ECO,
        capabilities=ProviderCapabilities(), provider_name="primary", resilience_port=resilience,
    ))
    return now, resilience, llm, agent


async def _in_flight(agent, llm, meanwhile) -> LLMResponse:
    call = asyncio.create_task(agent._call_llm(LLMRequest(model_name="primary-model", messages=[])))
    await asyncio.wait_for(llm.reached.wait(), GATE_TIMEOUT_S)
    meanwhile()
    llm.release.set()
    return await call


async def test_a_success_clears_failures_that_stayed_below_the_threshold():
    now, resilience, llm, agent = _setup()

    def two_failures():
        resilience.record_failure("primary")
        resilience.record_failure("primary")

    await _in_flight(agent, llm, two_failures)
    resilience.record_failure("primary")  # one more failure after the success: the window restarted from zero

    assert llm.reached.is_set()
    assert not resilience.is_provider_open("primary")


async def test_a_success_landing_after_the_cooldown_closes_the_breaker():
    now, resilience, llm, agent = _setup()

    def trip_then_cool_down():
        for _ in range(3):
            resilience.record_failure("primary")
        assert resilience.is_provider_open("primary")
        now[0] += 31.0  # past the cooldown: HALF-OPEN

    await _in_flight(agent, llm, trip_then_cool_down)
    resilience.record_failure("primary")
    resilience.record_failure("primary")  # two fresh failures stay below the threshold of a closed breaker

    assert llm.reached.is_set()
    assert not resilience.is_provider_open("primary")
