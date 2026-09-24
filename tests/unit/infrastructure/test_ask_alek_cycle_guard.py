from unittest.mock import AsyncMock

import pytest

from src.agents.alek_gateway_agent import AlekGatewayAgent
from src.agents.base_agent import BaseAgent
from src.domain.agent import AgentConfig, AgentStatus
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import ALEK
from src.infrastructure.agent_registry import AgentRegistry


class _ReenteringRouter(BaseAgent):
    """A Router whose Smart tries ask_alek again with the context it was handed."""

    async def can_handle(self, message):
        return True

    async def execute(self, message):
        return await self.coordinator.handle_delegation(
            intent="ask_alek", query="again", context=message.context, calling_agent_id=self.agent_id,
        )


@pytest.mark.asyncio
async def test_re_entering_alek_through_the_gateway_is_refused():
    registry = AgentRegistry()
    registry.register(ALEK)
    alert = AsyncMock()
    coordinator = AgentCoordinator(registry=registry, alert_sink=alert)
    gateway = AlekGatewayAgent(config=AgentConfig(agent_id="alek_agent_u1", agent_type="alek"),
                               notification_service=AsyncMock())
    router = _ReenteringRouter(AgentConfig(agent_id="router_agent_u1", agent_type="router"))
    for a in (gateway, router):
        a.coordinator = coordinator
        coordinator.register_agent(a)

    response = await coordinator.handle_delegation(
        intent="ask_alek", query="what's new",
        context={"user_id": "u1", "account_id": "a1", "_call_chain": ["lelik_agent"]},
        calling_agent_id="lelik_agent_u1",
    )

    assert response.status != AgentStatus.SUCCESS
    assert "Delegation cycle refused: lelik_agent → alek_agent → alek_agent" in response.error
    alert.post.assert_awaited_once()
