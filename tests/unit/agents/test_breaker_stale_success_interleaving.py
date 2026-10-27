"""S7: a success that was in flight when the breaker tripped closes the tripped breaker.

``BaseAgent._call_llm`` checks ``is_provider_open``, awaits ``generate_content`` and then
``record_success``. A call admitted while the breaker was closed can finish after other
calls tripped it; its ``record_success`` wipes the failure window and the OPEN marker.
HALF-OPEN admitting every caller is by design and is not tested here.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.base_agent import BaseAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentResponse
from src.domain.user import PerformanceTier
from src.ports.llm_port import AgentExecutionContext, LLMRequest, LLMPort, LLMResponse, ProviderCapabilities


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
        return LLMResponse(text="late success")


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="P15-S7: a success admitted before the breaker tripped calls record_success "
    "afterwards and closes the OPEN breaker; owner decision",
)
async def test_success_admitted_before_trip_does_not_close_the_tripped_breaker():
    now = [1000.0]
    resilience = InMemoryProviderResilience(
        failure_threshold=3, window_seconds=60.0, cooldown_seconds=30.0, time_source=lambda: now[0]
    )
    llm = _GatedLlm()
    agent = _Agent(AgentConfig(agent_id="a1", agent_type="mock"))
    agent.llm = llm
    agent._set_execution_context(
        AgentExecutionContext(
            agent_type="mock",
            provider=MagicMock(spec=LLMPort),  # context shell; the call runs on agent.llm
            model_name="primary-model",
            tier=PerformanceTier.ECO,
            capabilities=ProviderCapabilities(),
            provider_name="primary",
            resilience_port=resilience,
        )
    )

    call = asyncio.create_task(agent._call_llm(LLMRequest(model_name="primary-model", messages=[])))
    await asyncio.wait_for(llm.reached.wait(), timeout=5)
    for _ in range(3):
        resilience.record_failure("primary")
    assert resilience.is_provider_open("primary")

    llm.release.set()
    response = await call
    assert llm.reached.is_set()
    assert response.text == "late success"

    assert resilience.is_provider_open("primary")
