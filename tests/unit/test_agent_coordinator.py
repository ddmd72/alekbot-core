"""
Unit tests for AgentCoordinator.
"""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.agents.base_agent import BaseAgent
from src.domain.agent import AgentMessage, AgentResponse, AgentConfig, AgentIntent, AgentStatus


class MockAgent(BaseAgent):
    """Mock agent for testing coordinator."""
    
    def __init__(self, agent_id, capabilities=None):
        config = AgentConfig(
            agent_id=agent_id,
            agent_type="mock",
            capabilities=capabilities or []
        )
        super().__init__(config)
        self.process_calls = 0
        self.can_handle_result = True

    async def can_handle(self, message: AgentMessage) -> bool:
        return self.can_handle_result

    async def execute(self, message: AgentMessage) -> AgentResponse:
        self.process_calls += 1
        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=f"processed by {self.agent_id}"
        )


class TestAgentCoordinator:
    """Test suite for AgentCoordinator."""

    @pytest.fixture
    def coordinator(self):
        return AgentCoordinator()

    @pytest.fixture
    def agent1(self):
        return MockAgent("agent1", capabilities=["search"])

    @pytest.fixture
    def agent2(self):
        return MockAgent("agent2", capabilities=["compute"])

    def test_register_agent(self, coordinator, agent1):
        """Test registering an agent."""
        coordinator.register_agent(agent1)
        assert coordinator.get_agent("agent1") == agent1
        assert "agent1" in coordinator.list_agents()

    def test_register_duplicate_agent(self, coordinator, agent1):
        """Test registering duplicate agent raises error."""
        coordinator.register_agent(agent1)
        with pytest.raises(ValueError):
            coordinator.register_agent(agent1)

    def test_unregister_agent(self, coordinator, agent1):
        """Test unregistering an agent."""
        coordinator.register_agent(agent1)
        assert coordinator.unregister_agent("agent1")
        assert coordinator.get_agent("agent1") is None

    def test_get_agents_by_capability(self, coordinator, agent1, agent2):
        """Test finding agents by capability."""
        coordinator.register_agent(agent1)
        coordinator.register_agent(agent2)
        
        search_agents = coordinator.get_agents_by_capability("search")
        assert len(search_agents) == 1
        assert search_agents[0] == agent1
        
        compute_agents = coordinator.get_agents_by_capability("compute")
        assert len(compute_agents) == 1
        assert compute_agents[0] == agent2

    @pytest.mark.asyncio
    async def test_explicit_routing(self, coordinator, agent1):
        """Test routing to specific agent."""
        coordinator.register_agent(agent1)
        
        message = AgentMessage.create(
            sender="test",
            recipient="agent1",
            intent=AgentIntent.QUERY,
            payload={}
        )
        
        response = await coordinator.route_message(message)
        
        assert response.status == AgentStatus.SUCCESS
        assert response.agent_id == "agent1"
        assert agent1.process_calls == 1

    @pytest.mark.asyncio
    async def test_broadcast_routing(self, coordinator, agent1, agent2):
        """Test broadcast routing to capable agent."""
        coordinator.register_agent(agent1)
        coordinator.register_agent(agent2)
        
        # Only agent1 can handle this
        agent1.can_handle_result = True
        agent2.can_handle_result = False
        
        message = AgentMessage.create(
            sender="test",
            recipient="broadcast",
            intent=AgentIntent.QUERY,
            payload={}
        )
        
        response = await coordinator.route_message(message)
        
        assert response.status == AgentStatus.SUCCESS
        assert response.agent_id == "agent1"
        assert agent1.process_calls == 1
        assert agent2.process_calls == 0

    @pytest.mark.asyncio
    async def test_no_route_found(self, coordinator):
        """Test when no agent found."""
        message = AgentMessage.create(
            sender="test",
            recipient="unknown",
            intent=AgentIntent.QUERY,
            payload={}
        )
        
        response = await coordinator.route_message(message)
        
        assert response.status == AgentStatus.FAILED
        assert "Unknown recipient" in response.error

    @pytest.mark.asyncio
    async def test_broadcast_no_capable_agent(self, coordinator, agent1):
        """Test broadcast when no agent can handle."""
        coordinator.register_agent(agent1)
        agent1.can_handle_result = False
        
        message = AgentMessage.create(
            sender="test",
            recipient="broadcast",
            intent=AgentIntent.QUERY,
            payload={}
        )
        
        response = await coordinator.route_message(message)
        
        assert response.status == AgentStatus.FAILED
        assert "No agent can handle" in response.error

    @pytest.mark.asyncio
    async def test_parallel_execution(self, coordinator, agent1, agent2):
        """Test parallel execution of multiple messages."""
        coordinator.register_agent(agent1)
        coordinator.register_agent(agent2)
        
        msg1 = AgentMessage.create(
            sender="test",
            recipient="agent1",
            intent=AgentIntent.QUERY,
            payload={}
        )
        
        msg2 = AgentMessage.create(
            sender="test",
            recipient="agent2",
            intent=AgentIntent.QUERY,
            payload={}
        )
        
        responses = await coordinator.parallel_execute([msg1, msg2])
        
        assert len(responses) == 2
        assert responses[0].agent_id == "agent1"
        assert responses[1].agent_id == "agent2"
        assert agent1.process_calls == 1
        assert agent2.process_calls == 1
