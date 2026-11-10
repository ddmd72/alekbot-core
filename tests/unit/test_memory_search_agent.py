"""
Unit tests for MemorySearchAgent.
"""

import pytest
from unittest.mock import Mock, AsyncMock, patch
from src.agents.memory_search_agent import FactsMemoryAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent, AgentStatus
from src.domain.entities import FactEntity, FactType
from src.ports.llm_port import AgentExecutionContext, ProviderCapabilities


class TestMemorySearchAgent:
    """Test suite for MemorySearchAgent."""

    @pytest.fixture
    def mock_repo(self):
        repo = Mock()
        repo.search_facts = AsyncMock()
        return repo

    @pytest.fixture
    def mock_embedding(self):
        service = Mock()
        service.get_embedding = AsyncMock()
        return service

    @pytest.fixture
    def agent(self, mock_repo, mock_embedding):
        config = AgentConfig(
            agent_id="memory_agent",
            agent_type="memory_search"
        )
        return FactsMemoryAgent(
            config=config,
            repository=mock_repo,
            embedding_service=mock_embedding,
            account_id="user123"
        )



    @pytest.mark.asyncio
    async def test_can_handle_wrong_intent(self, agent):
        """Test capability check with wrong intent."""
        message = AgentMessage.create(
            sender="test",
            recipient="memory_agent",
            intent=AgentIntent.DELEGATE,
            payload={"query": "my car"}
        )
        assert await agent.can_handle(message) is False




    @pytest.mark.asyncio
    async def test_execute_missing_query(self, agent):
        """Test execution without query."""
        message = AgentMessage.create(
            sender="test",
            recipient="memory_agent",
            intent=AgentIntent.QUERY,
            payload={}
        )

        response = await agent.execute(message)

        assert response.status == AgentStatus.FAILED
        assert "No search keys provided" in response.error


