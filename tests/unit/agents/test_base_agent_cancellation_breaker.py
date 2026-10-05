"""Cancellation is not an agent failure (2026-10-05).

A user cancelling a long turn (LONG_RUNNING_TURNS_RFC §5.4) cancels the orchestrator's task.
Counting that as a circuit-breaker failure meant a few cancels in a row could open the breaker
and disable Smart and the Router for everyone's next requests.
"""
import asyncio

import pytest

from src.agents.base_agent import BaseAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse


class _Agent(BaseAgent):
    def __init__(self, config):
        super().__init__(config)
        self.error = None

    async def can_handle(self, message: AgentMessage) -> bool:
        return True

    async def execute(self, message: AgentMessage) -> AgentResponse:
        if self.error:
            raise self.error
        return AgentResponse.success(task_id=message.task_id, agent_id=self.agent_id, result="ok")


def _config():
    return AgentConfig(agent_id="a", agent_type="mock", timeout_ms=1000,
                       circuit_breaker_threshold=2, circuit_breaker_recovery_ms=60_000)


def _message():
    return AgentMessage.create(sender="t", recipient="a", intent=AgentIntent.QUERY, payload={})


async def test_cancellation_still_propagates_but_records_no_failure():
    agent = _Agent(_config())
    agent.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await agent.process(_message())
    assert agent.agent_id not in agent.circuit_breaker._failures


async def test_repeated_cancellations_never_open_the_breaker():
    agent = _Agent(_config())
    agent.error = asyncio.CancelledError()
    for _ in range(5):
        with pytest.raises(asyncio.CancelledError):
            await agent.process(_message())
    assert not agent.circuit_breaker.is_open(agent.agent_id, 2, 60_000)
    agent.error = None
    assert (await agent.process(_message())).result == "ok"


async def test_real_failures_still_count():
    agent = _Agent(_config())
    agent.error = RuntimeError("boom")
    await agent.process(_message())
    await agent.process(_message())
    assert agent.circuit_breaker.is_open(agent.agent_id, 2, 60_000)
