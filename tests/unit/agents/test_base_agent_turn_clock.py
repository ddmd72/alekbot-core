"""BaseAgent under a TurnClock (RFC §5.1, plan delta D2)."""
from typing import Any, Dict

import pytest

from src.agents.base_agent import BaseAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentResponse
from src.domain.retry_policy import NO_RETRY_POLICY
from src.domain.turn_clock import CURRENT_TURN_CLOCK, TurnClock


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
