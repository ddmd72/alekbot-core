"""
Unit tests for MemorySearchAgent.
"""

import pytest
from unittest.mock import Mock, AsyncMock
from src.agents.memory_search_agent import MemorySearchAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent, AgentStatus
from src.domain.entities import FactEntity, FactType


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
        return MemorySearchAgent(
            config=config,
            repository=mock_repo,
            embedding_service=mock_embedding,
            account_id="user123"
        )

    @pytest.mark.asyncio
    async def test_can_handle_memory_keywords(self, agent):
        """Test capability check with keywords."""
        message = AgentMessage.create(
            sender="test",
            recipient="memory_agent",
            intent=AgentIntent.QUERY,
            payload={"query": "What is my car?"}
        )
        assert await agent.can_handle(message) is True

    @pytest.mark.asyncio
    async def test_can_handle_explicit_domain(self, agent):
        """Test capability check with explicit domain."""
        message = AgentMessage.create(
            sender="test",
            recipient="memory_agent",
            intent=AgentIntent.QUERY,
            payload={"query": "something", "domain": "memory"}
        )
        assert await agent.can_handle(message) is True

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
    async def test_execute_success(self, agent, mock_repo, mock_embedding):
        """Test successful execution."""
        # Setup mocks
        mock_embedding.get_embedding.return_value = [0.1, 0.2, 0.3]
        
        fact = FactEntity(
            account_id="account-1",
            created_by_user_id="user123",
            lineage_id="lineage-1",
            text="User owns a Honda Civic",
            tags=["possessions"],
            type=FactType.EVENT
        )
        mock_repo.search_facts.return_value = [fact]

        message = AgentMessage.create(
            sender="test",
            recipient="memory_agent",
            intent=AgentIntent.QUERY,
            payload={"query": "my car"}
        )

        response = await agent.execute(message)

        assert response.status == AgentStatus.SUCCESS
        assert len(response.result) == 1
        assert response.result[0] == "User owns a Honda Civic"
        assert response.confidence > 0.0
        
        # Verify calls
        mock_embedding.get_embedding.assert_called_once()
        mock_repo.search_facts.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_no_results(self, agent, mock_repo, mock_embedding):
        """Test execution with no results."""
        mock_embedding.get_embedding.return_value = [0.1, 0.2, 0.3]
        mock_repo.search_facts.return_value = []

        message = AgentMessage.create(
            sender="test",
            recipient="memory_agent",
            intent=AgentIntent.QUERY,
            payload={"query": "unknown thing"}
        )

        response = await agent.execute(message)

        assert response.status == AgentStatus.SUCCESS
        assert len(response.result) == 0
        assert response.confidence == 0.0

    @pytest.mark.asyncio
    async def test_execute_repository_error(self, agent, mock_repo, mock_embedding):
        """Test handling of repository errors."""
        mock_embedding.get_embedding.return_value = [0.1, 0.2, 0.3]
        mock_repo.search_facts.side_effect = Exception("DB Error")

        message = AgentMessage.create(
            sender="test",
            recipient="memory_agent",
            intent=AgentIntent.QUERY,
            payload={"query": "my car"}
        )

        response = await agent.execute(message)

        assert response.status == AgentStatus.FAILED
        assert "Memory search failed" in response.error

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
