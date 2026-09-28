"""The ask_alek path outlasts the relay's 300 s wait (voice UAT round 1, Task 2 fix 2): the
message the gateway routes carries an explicit 600 s, which wins over Router's and Smart's own
config (BaseAgent._execute_with_timeout)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.alek_gateway_agent import ASK_ALEK_TIMEOUT_MS, AlekGatewayAgent
from src.domain.agent import AgentIntent, AgentMessage, AgentResponse


def test_ask_alek_timeout_is_600_s():
    assert ASK_ALEK_TIMEOUT_MS == 600_000


@pytest.mark.asyncio
async def test_routed_message_carries_the_explicit_timeout():
    agent = AlekGatewayAgent(config=MagicMock(agent_id="alek_agent_u1", timeout_ms=None),
                             notification_service=AsyncMock())
    coordinator = MagicMock()
    coordinator.route_message = AsyncMock(
        return_value=AgentResponse.success(task_id="t", agent_id="smart", result="plain answer"))
    agent.coordinator = coordinator
    message = AgentMessage.create(sender="lelik", recipient="alek_agent_u1", intent=AgentIntent.QUERY,
                                  payload={"query": "q"}, context={"user_id": "u1", "account_id": "a1"})
    await agent.execute(message)
    routed = coordinator.route_message.await_args.args[0]
    assert routed.recipient == "router_agent_u1"
    assert routed.timeout_ms == 600_000
