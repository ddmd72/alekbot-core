"""
Unit tests for ConsolidationAgent.
"""

import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent, AgentStatus
from src.domain.entities import FactEntity, FactType
from src.ports.llm_service import AgentExecutionContext, ProviderCapabilities, LLMService
from src.domain.user import PerformanceTier
from src.domain.request_context import RequestContext


class TestConsolidationAgent:
    """Test suite for ConsolidationAgent."""

    @pytest.fixture
    def mock_llm(self):
        service = MagicMock(spec=LLMService)
        service.generate_content = AsyncMock()
        return service

    @pytest.fixture
    def mock_repo(self):
        repo = Mock()
        repo.get_observations = AsyncMock()
        repo.get_active_facts = AsyncMock(return_value=[])
        repo.add_fact = AsyncMock()
        repo.add_fact_if_unique = AsyncMock(return_value=(True, "new_id"))
        repo.archive_observations = AsyncMock()
        repo.refresh_biographical_context_cache = AsyncMock()
        # Returns List[Dict] — empty list is fine for tests
        repo.get_biographical_context_cached = AsyncMock(return_value=[])
        return repo

    @pytest.fixture
    def mock_embedding(self):
        service = Mock()
        service.get_embedding = AsyncMock()
        return service

    @pytest.fixture
    def mock_fact_write_service(self):
        svc = AsyncMock()
        # add_facts_batch returns (saved_count, skipped_count)
        svc.add_facts_batch = AsyncMock(return_value=(2, 0))
        return svc

    @pytest.fixture
    def mock_prompt_builder(self):
        builder = MagicMock()
        builder.build_for_agent = AsyncMock(return_value="CONSOLIDATION SYSTEM PROMPT")
        builder.invalidate_biographical_cache = MagicMock()
        return builder

    @pytest.fixture
    def agent(self, mock_llm, mock_repo, mock_embedding, mock_fact_write_service, mock_prompt_builder):
        config = AgentConfig(
            agent_id="consolidation_agent",
            agent_type="consolidation",
            llm_model="gemini-3-pro-preview"
        )
        ec = AgentExecutionContext(
            agent_type="consolidation",
            provider=mock_llm,
            model_name="gemini-3-pro-preview",
            tier=PerformanceTier.PERFORMANCE,
            capabilities=ProviderCapabilities()
        )
        return ConsolidationAgent(
            config=config,
            execution_context=ec,
            repository=mock_repo,
            embedding_service=mock_embedding,
            fact_write_service=mock_fact_write_service,
            prompt_builder=mock_prompt_builder
        )

    @pytest.mark.asyncio
    async def test_can_handle_consolidate_task(self, agent):
        """Test capability check with correct task."""
        message = AgentMessage.create(
            sender="test",
            recipient="consolidation_agent",
            intent=AgentIntent.DELEGATE,
            payload={"task": "consolidate"}
        )
        assert await agent.can_handle(message) is True

    @pytest.mark.asyncio
    async def test_can_handle_synthesize_task(self, agent):
        """Test capability check with synthesize task."""
        message = AgentMessage.create(
            sender="test",
            recipient="consolidation_agent",
            intent=AgentIntent.DELEGATE,
            payload={"task": "synthesize"}
        )
        assert await agent.can_handle(message) is True

    @pytest.mark.asyncio
    async def test_can_handle_wrong_intent(self, agent):
        """Test capability check with wrong intent."""
        message = AgentMessage.create(
            sender="test",
            recipient="consolidation_agent",
            intent=AgentIntent.QUERY,
            payload={"task": "consolidate"}
        )
        assert await agent.can_handle(message) is False

    @pytest.mark.asyncio
    async def test_execute_no_data(self, agent, mock_repo):
        """Test execution with no data in payload — returns success with 0 facts."""
        message = AgentMessage.create(
            sender="test",
            recipient="consolidation_agent",
            intent=AgentIntent.DELEGATE,
            payload={"task": "consolidate"},
            context={"user_id": "user123"}
        )

        async with RequestContext(user_id="user123", account_id="account-123"):
            response = await agent.execute(message)

        assert response.status == AgentStatus.SUCCESS
        assert response.result["new_facts"] == 0

    @pytest.mark.asyncio
    async def test_execute_success(self, agent, mock_llm, mock_repo, mock_embedding):
        """Test successful consolidation with session messages (v2 legacy path)."""
        # Mock data
        messages = [
            {"role": "user", "text": "I have a car"},
            {"role": "model", "text": "Got it."}
        ]
        mock_repo.get_active_facts.return_value = []
        mock_embedding.get_embedding.return_value = [0.1, 0.2, 0.3]

        # Mock LLM response with valid JSON
        mock_response = Mock()
        mock_response.text = """
        ```json
        {
            "new_facts": [
                {"id": "fact1", "content": "User owns a car", "tags": ["possessions"], "type": "STATE"}
            ],
            "new_anchors": [
                {"id": "anchor1", "content": "Value: Honesty", "tags": ["values"], "type": "PRINCIPLE"}
            ]
        }
        ```
        """
        mock_llm.generate_content.return_value = mock_response

        message = AgentMessage.create(
            sender="test",
            recipient="consolidation_agent",
            intent=AgentIntent.DELEGATE,
            payload={
                "task": "consolidate",
                "messages": messages
            },
            context={"user_id": "user123"}
        )

        async with RequestContext(user_id="user123", account_id="account-123"):
            response = await agent.execute(message)

        assert response.status == AgentStatus.SUCCESS
        assert response.result["new_facts"] == 1
        assert response.result["new_anchors"] == 1

        # v2 path uses FactWriteService.add_facts_batch, not repo.add_fact_if_unique directly
        agent._fact_write_service.add_facts_batch.assert_called_once()

    @pytest.mark.asyncio
    async def test_sanitize_duplicate_ids(self, agent):
        """Test ID sanitization."""
        items = [
            {"id": "duplicate", "content": "1"},
            {"id": "duplicate", "content": "2"},
            {"id": "unique", "content": "3"}
        ]

        sanitized = agent._sanitize_ids(items)

        assert len(sanitized) == 3
        ids = [item["id"] for item in sanitized]
        assert len(set(ids)) == 3  # All unique
        assert "duplicate" in ids
        assert "duplicate_a" in ids
        assert "unique" in ids

    @pytest.mark.asyncio
    async def test_execute_missing_context(self, agent):
        """Test execution without user_id fails before RequestContext check."""
        message = AgentMessage.create(
            sender="test",
            recipient="consolidation_agent",
            intent=AgentIntent.DELEGATE,
            payload={"task": "consolidate"},
            context={}  # Missing user_id
        )

        response = await agent.execute(message)

        assert response.status == AgentStatus.FAILED
        assert "Missing user_id" in response.error

    @pytest.mark.asyncio
    async def test_parse_consolidation_results_invalid_json(self, agent, mock_llm, mock_repo):
        """Test handling of invalid JSON from LLM — returns failure."""
        mock_response = Mock()
        mock_response.text = "Not JSON"
        mock_llm.generate_content.return_value = mock_response

        message = AgentMessage.create(
            sender="test",
            recipient="consolidation_agent",
            intent=AgentIntent.DELEGATE,
            payload={
                "task": "consolidate",
                "messages": [{"role": "user", "text": "hello"}]
            },
            context={"user_id": "user123"}
        )

        async with RequestContext(user_id="user123", account_id="account-123"):
            response = await agent.execute(message)

        assert response.status == AgentStatus.FAILED
        # _parse_consolidation_results returns {} on invalid JSON,
        # which is falsy → agent returns "Failed to parse consolidation results"
        assert "Failed to parse" in response.error
