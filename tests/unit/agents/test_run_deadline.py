"""CURRENT_RUN_DEADLINE (DRIVE_SPECIALIST_RFC §5.6): an orchestrator publishes its own deadline for the specialists it
runs and clears it when it runs without a timeout; a specialist leaves the value alone; the value never rides in a
message context, and a task created inside the run inherits it."""
import asyncio
import time

import pytest

from src.agents.base_agent import BaseAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from src.domain.run_deadline import CURRENT_RUN_DEADLINE


class _Agent(BaseAgent):
    seen: list

    async def can_handle(self, message):
        return True

    async def execute(self, message):
        child = asyncio.ensure_future(_read())
        self.seen.append((CURRENT_RUN_DEADLINE.get(), await child, dict(message.context)))
        return AgentResponse.success(task_id=message.task_id, agent_id=self.agent_id, result="ok")


async def _read():
    return CURRENT_RUN_DEADLINE.get()


class _Orchestrator(_Agent):
    SETS_RUN_DEADLINE = True


def _agent(cls, timeout_ms):
    agent = cls(AgentConfig(agent_id=f"{cls.__name__}_u1", agent_type="t", timeout_ms=timeout_ms))
    agent.seen = []
    return agent


def _msg(timeout_ms=None):
    return AgentMessage.create(sender="s", recipient="r", intent=AgentIntent.QUERY, payload={"query": "q"},
                               context={"user_id": "u1"}, timeout_ms=timeout_ms)


async def test_an_orchestrator_publishes_its_own_timeout_and_a_child_task_inherits_it():
    agent = _agent(_Orchestrator, 180_000)
    before = time.monotonic()
    await agent.process(_msg())
    deadline, in_child, context = agent.seen[0]
    assert before + 179 < deadline <= time.monotonic() + 180
    assert in_child == deadline
    assert "run_deadline" not in str(context) and context == {"user_id": "u1"}
    assert CURRENT_RUN_DEADLINE.get() is None   # reset after the run


async def test_the_message_timeout_wins_over_the_config():
    agent = _agent(_Orchestrator, 300_000)
    before = time.monotonic()
    await agent.process(_msg(timeout_ms=60_000))
    assert agent.seen[0][0] < before + 61


async def test_an_orchestrator_without_a_timeout_clears_the_outer_value():
    token = CURRENT_RUN_DEADLINE.set(123.0)
    try:
        agent = _agent(_Orchestrator, None)
        await agent.process(_msg())
        assert agent.seen[0][0] is None
        assert CURRENT_RUN_DEADLINE.get() == 123.0
    finally:
        CURRENT_RUN_DEADLINE.reset(token)


@pytest.mark.parametrize("timeout_ms", [None, 30_000])
async def test_a_specialist_leaves_the_callers_value(timeout_ms):
    token = CURRENT_RUN_DEADLINE.set(456.0)
    try:
        agent = _agent(_Agent, timeout_ms)
        await agent.process(_msg())
        assert agent.seen[0][0] == 456.0
    finally:
        CURRENT_RUN_DEADLINE.reset(token)


def test_smart_and_quick_publish_it():
    from src.agents.core.quick_response_agent import QuickResponseAgent
    from src.agents.core.smart_response_agent import SmartResponseAgent
    assert SmartResponseAgent.SETS_RUN_DEADLINE and QuickResponseAgent.SETS_RUN_DEADLINE
    assert not BaseAgent.SETS_RUN_DEADLINE
