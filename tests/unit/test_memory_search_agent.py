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
        assert "User owns a Honda Civic" in response.result
        assert response.confidence > 0.0
        
        # Verify calls
        mock_embedding.get_embedding.assert_called_once()
        assert mock_embedding.get_embedding.call_args[0][0] == "my car"
        mock_repo.search_facts.assert_called_once()
        search_call = mock_repo.search_facts.call_args
        assert search_call[1].get("limit") is not None  # limit kwarg is always passed

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


class TestMemorySearchAgentLLM:
    """Tests for the LLM-based key formulation path."""

    @pytest.fixture
    def mock_llm(self):
        llm = Mock()
        llm.generate_content = AsyncMock()
        llm.get_capabilities = Mock(return_value=ProviderCapabilities())
        return llm

    @pytest.fixture
    def mock_execution_context(self, mock_llm):
        ctx = Mock(spec=AgentExecutionContext)
        ctx.provider = mock_llm
        ctx.model_name = "gemini-flash"
        ctx.capabilities = ProviderCapabilities()
        return ctx

    @pytest.fixture
    def mock_prompt_builder(self):
        builder = Mock()
        builder.build_for_agent = AsyncMock(return_value="You extract search keys.")
        return builder

    @pytest.fixture
    def mock_search_enrichment(self):
        enrichment = Mock()
        enrichment.enrich_context = AsyncMock()
        return enrichment

    @pytest.fixture
    def agent_with_llm(self, mock_execution_context, mock_prompt_builder, mock_search_enrichment):
        from unittest.mock import Mock, AsyncMock
        mock_repo = Mock()
        mock_repo.search_facts = AsyncMock(return_value=[])
        mock_embedding = Mock()
        mock_embedding.get_embedding = AsyncMock(return_value=[0.1])

        config = AgentConfig(agent_id="memory_agent_llm", agent_type="memory_search")
        return FactsMemoryAgent(
            config=config,
            repository=mock_repo,
            embedding_service=mock_embedding,
            account_id="account-123",
            search_enrichment=mock_search_enrichment,
            execution_context=mock_execution_context,
            prompt_builder=mock_prompt_builder,
            user_id="user-123",
        )

    @pytest.mark.asyncio
    async def test_formulate_keys_valid_json(self, agent_with_llm, mock_llm):
        """LLM returns valid JSON — keys extracted correctly."""
        mock_llm.generate_content.return_value = Mock(
            text='{"keywords":["car","vehicle"],"primary_query":"user car brand","alternative_query":"vehicle model ownership","domains":["possession"]}',
            tool_calls=[],
            usage_metadata=None,
        )

        keys = await agent_with_llm._formulate_search_keys("Какая марка моей машины?")

        assert keys["keywords"] == ["car", "vehicle"]
        assert keys["primary_query"] == "user car brand"
        assert keys["alternative_query"] == "vehicle model ownership"
        assert keys["domains"] == ["possession"]

    @pytest.mark.asyncio
    async def test_formulate_keys_malformed_response_falls_back(self, agent_with_llm, mock_llm):
        """Structured output is enforced by the adapter; if malformed JSON still arrives, fallback to empty dict."""
        mock_llm.generate_content.return_value = Mock(
            text='```json\n{"keywords":["home"],"primary_query":"user home address","alternative_query":"residence location"}\n```',
            tool_calls=[],
            usage_metadata=None,
        )

        keys = await agent_with_llm._formulate_search_keys("Где я живу?")

        assert keys == {}

    @pytest.mark.asyncio
    async def test_formulate_keys_llm_failure_returns_empty(self, agent_with_llm, mock_llm):
        """LLM call raises exception — returns empty dict without crashing."""
        mock_llm.generate_content.side_effect = Exception("LLM timeout")

        keys = await agent_with_llm._formulate_search_keys("What is my job?")

        assert keys == {}

    @pytest.mark.asyncio
    async def test_formulate_keys_invalid_json_returns_empty(self, agent_with_llm, mock_llm):
        """LLM returns non-JSON text — returns empty dict without crashing."""
        mock_llm.generate_content.return_value = Mock(
            text="Sorry, I cannot help with that.",
            tool_calls=[],
            usage_metadata=None,
        )

        keys = await agent_with_llm._formulate_search_keys("What is my job?")

        assert keys == {}

    @pytest.mark.asyncio
    async def test_execute_llm_path_calls_enrichment(self, agent_with_llm, mock_llm, mock_search_enrichment):
        """execute() uses LLM to formulate keys then calls SearchEnrichmentService."""
        mock_llm.generate_content.return_value = Mock(
            text='{"keywords":["car"],"primary_query":"user car","alternative_query":"vehicle ownership"}',
            tool_calls=[],
            usage_metadata=None,
        )

        from unittest.mock import Mock as M
        enriched = M()
        enriched.facts = []
        enriched.dedup_count = 0
        enriched.total_sources = 0
        mock_search_enrichment.enrich_context.return_value = enriched

        message = AgentMessage.create(
            sender="smart",
            recipient="memory_agent_llm",
            intent=AgentIntent.QUERY,
            payload={"query": "Какая марка моей машины?"},
        )

        response = await agent_with_llm.execute(message)

        assert response.status == AgentStatus.SUCCESS
        mock_llm.generate_content.assert_called_once()
        mock_search_enrichment.enrich_context.assert_called_once()
        call_kwargs = mock_search_enrichment.enrich_context.call_args.kwargs
        assert call_kwargs["keywords"] == ["car"]
        assert call_kwargs["search_phrase_1"] == "user car"

    @pytest.mark.asyncio
    async def test_execute_llm_failure_falls_back_to_legacy(self, agent_with_llm, mock_llm):
        """If LLM fails during key formulation, falls back to raw query legacy search."""
        mock_llm.generate_content.side_effect = Exception("LLM down")

        message = AgentMessage.create(
            sender="smart",
            recipient="memory_agent_llm",
            intent=AgentIntent.QUERY,
            payload={"query": "my car"},
        )

        response = await agent_with_llm.execute(message)

        # Should not crash — falls back to legacy single-vector search
        assert response.status == AgentStatus.SUCCESS
