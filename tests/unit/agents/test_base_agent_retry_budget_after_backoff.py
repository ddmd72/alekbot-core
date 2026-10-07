"""Same-provider retry vs the turn clock: the budget can run out DURING the backoff sleep.

Prod log audit C-08 (2026-10-06): `can_retry()` passed, the backoff slept, and the clamp then
found the budget inside the wrap-up reserve — `call_timeout()` floors at 1 s, so a call that
could not succeed was made, and the turn died with a misleading "request timeout after 1s".
"""
import time
from unittest.mock import AsyncMock, MagicMock, patch

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.base_agent import BaseAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentResponse
from src.domain.exceptions import LLMServerError, TranscriptLockedError
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

import pytest


class _Agent(BaseAgent):
    def __init__(self, config: AgentConfig, llm: LLMPort):
        super().__init__(config)
        self.llm = llm

    async def can_handle(self, message: AgentMessage) -> bool:
        return True

    async def execute(self, message: AgentMessage) -> AgentResponse:
        raise NotImplementedError


def _locked_request() -> LLMRequest:
    return LLMRequest(
        model_name="gemini-flash",
        system_instruction="test",
        messages=[
            Message(role="user", parts=[MessagePart(text="hi")]),
            Message(role="user", parts=[MessagePart(
                tool_response={"name": "search_memory", "response": {"result": "x"}}
            )]),
        ],
    )


def _ok() -> LLMResponse:
    return LLMResponse(
        text="ok", tool_calls=[], raw_content=None,
        usage_metadata=UsageMetadata(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


def _agent(llm) -> _Agent:
    agent = _Agent(AgentConfig(agent_id="a", agent_type="quick"), llm)
    agent._set_execution_context(AgentExecutionContext(
        agent_type="quick", provider=llm, model_name="gemini-flash",
        tier=PerformanceTier.BALANCED, capabilities=ProviderCapabilities(),
        provider_name="gemini", resilience_port=InMemoryProviderResilience(),
    ))
    return agent


async def test_retry_not_made_when_the_backoff_sleep_ate_the_budget():
    llm = MagicMock(spec=LLMPort)
    llm.generate_content = AsyncMock(side_effect=LLMServerError("529", http_status=529))
    agent = _agent(llm)
    clock = TurnClock.start(budget_s=600, wrap_up_reserve_s=100)

    async def sleep_into_the_reserve(_seconds):
        clock.deadline = time.monotonic() + 50  # 50 s left < 100 s reserve

    token = CURRENT_TURN_CLOCK.set(clock)
    try:
        with patch("src.agents.base_agent.asyncio.sleep", new=sleep_into_the_reserve):
            with pytest.raises(TranscriptLockedError):
                await agent._call_llm(_locked_request())
    finally:
        CURRENT_TURN_CLOCK.reset(token)

    assert llm.generate_content.await_count == 1  # only the original call; no doomed 1 s retry


async def test_retry_still_made_when_budget_survives_the_backoff():
    llm = MagicMock(spec=LLMPort)
    llm.generate_content = AsyncMock(side_effect=[LLMServerError("529", http_status=529), _ok()])
    agent = _agent(llm)
    clock = TurnClock.start(budget_s=600, wrap_up_reserve_s=100)

    token = CURRENT_TURN_CLOCK.set(clock)
    try:
        with patch("src.agents.base_agent.asyncio.sleep", new=AsyncMock()):
            response = await agent._call_llm(_locked_request())
    finally:
        CURRENT_TURN_CLOCK.reset(token)

    assert response.text == "ok"
    assert llm.generate_content.await_count == 2
