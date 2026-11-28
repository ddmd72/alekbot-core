"""
Unit tests for ConsolidationAgent.
"""

import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent, AgentStatus
from src.domain.entities import FactEntity, FactType
from src.domain.fact_write_context import FactWriteContext
from src.ports.llm_port import AgentExecutionContext, ProviderCapabilities, LLMPort, LLMResponse
from src.ports.fact_management_port import FactManagementPort
from src.domain.llm import ToolCall
from src.domain.user import PerformanceTier
from src.domain.request_context import RequestContext
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from tests.support.typed_writes import NoTypedWrites, typed_port_for


class TestConsolidationAgent:
    """Test suite for ConsolidationAgent."""

    @pytest.fixture
    def mock_llm(self):
        service = MagicMock(spec=LLMPort)
        service.generate_content = AsyncMock()
        return service

    @pytest.fixture
    def mock_repo(self):
        repo = Mock()
        repo.get_observations = AsyncMock()
        repo.get_active_facts = AsyncMock(return_value=[])
        repo.add_fact = AsyncMock()
        repo.archive_observations = AsyncMock()
        # Returns List[Dict] — empty list is fine for tests
        repo.get_biographical_context_cached = AsyncMock(return_value=[])
        return repo

    @pytest.fixture
    def mock_embedding(self):
        service = Mock()
        service.get_embedding = AsyncMock()
        return service

    @pytest.fixture
    def mock_prompt_builder(self):
        builder = MagicMock()
        builder.build_for_agent = AsyncMock(return_value="CONSOLIDATION SYSTEM PROMPT")
        builder.invalidate_biographical_cache = MagicMock()
        return builder

    @pytest.fixture
    def agent(self, mock_llm, mock_repo, mock_embedding, mock_prompt_builder):
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
            capabilities=ProviderCapabilities(),
            resilience_port=InMemoryProviderResilience(),
        )
        return ConsolidationAgent(
            config=config,
            execution_context=ec,
            repository=mock_repo,
            embedding_service=mock_embedding,
            prompt_builder=mock_prompt_builder,
            fact_updates=NoTypedWrites(),  # reviewer edit (S4 T6b): the typed port is required
            bio_cache=AsyncMock(),
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


class TestConsolidationAgentV3:
    """Tests for ConsolidationAgent v3 multi-turn tool-use path."""

    @pytest.fixture
    def mock_llm(self):
        service = MagicMock(spec=LLMPort)
        service.generate_content = AsyncMock()
        return service

    @pytest.fixture
    def mock_repo(self):
        repo = Mock()
        repo.get_observations = AsyncMock(return_value=[])
        repo.get_active_facts = AsyncMock(return_value=[])
        repo.get_active_facts_ordered = AsyncMock(return_value=[])  # Stage 2b directive review → empty rulebook
        repo.archive_observations = AsyncMock()
        repo.get_biographical_context_cached = AsyncMock(return_value=[])
        return repo

    @pytest.fixture
    def mock_embedding(self):
        service = Mock()
        service.get_embedding = AsyncMock()
        return service

    @pytest.fixture
    def mock_prompt_builder(self):
        builder = MagicMock()
        builder.build_for_agent = AsyncMock(return_value="CONSOLIDATION V3 PROMPT")
        builder.invalidate_biographical_cache = MagicMock()
        return builder

    @pytest.fixture
    def mock_fact_management(self):
        return AsyncMock(spec=FactManagementPort)

    @pytest.fixture
    def agent_v3(self, mock_llm, mock_repo, mock_embedding,
                 mock_prompt_builder, mock_fact_management):
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
            capabilities=ProviderCapabilities(),
            resilience_port=InMemoryProviderResilience(),
        )
        return ConsolidationAgent(
            config=config,
            execution_context=ec,
            repository=mock_repo,
            embedding_service=mock_embedding,
            fact_management_port=mock_fact_management,
            fact_updates=typed_port_for(mock_fact_management),  # reviewer edit (S4 T6b): the typed port is required
            prompt_builder=mock_prompt_builder,
            bio_cache=AsyncMock(),
        )

    @pytest.mark.asyncio
    async def test_v3_create_fact_tool_called(self, agent_v3, mock_llm, mock_fact_management):
        """v3: LLM calls create_fact tool → FactManagementPort.create_fact is invoked."""
        mock_fact_management.create_fact.return_value = {
            "fact_id": "new-fact-123", "status": "created", "message": "ok"
        }

        # Turn 1: LLM returns a create_fact tool call
        turn1 = LLMResponse(
            tool_calls=[ToolCall(
                name="create_fact",
                args={
                    "content": "User owns a cat",
                    "fact_attributes": {
                        "domain": "personal",
                        "temporal_class": "stable",
                        "context_priority": "medium",
                        "tags": ["pets"],
                        "type": "state",
                    }
                }
            )]
        )
        # Turn 2: LLM returns final report (no tool calls)
        turn2 = LLMResponse(
            text='{"operations": [{"action": "CREATE", "fact_id": "new-fact-123", "reason": "new fact"}]}'
        )
        mock_llm.generate_content.side_effect = [turn1, turn2]

        message = AgentMessage.create(
            sender="test",
            recipient="consolidation_agent",
            intent=AgentIntent.DELEGATE,
            payload={"task": "consolidate", "messages": [{"role": "user", "text": "I have a cat"}]},
            context={"user_id": "user123"}
        )

        async with RequestContext(user_id="user123", account_id="account-123"):
            response = await agent_v3.execute(message)

        assert response.status == AgentStatus.SUCCESS
        # Reviewer edit (facts S3, T4a; plan §3.2): every write carries its pass's context. A Stage 1 run whose message has no
        # owner zone and whose batch messages carry no time writes as stage1 in UTC with no span; the model's dates count.
        mock_fact_management.create_fact.assert_called_once_with(
            content="User owns a cat",
            metadata={
                "domain": "personal",
                "temporal_class": "stable",
                "context_priority": "medium",
                "tags": ["pets"],
                "type": "state",
                "account_id": "account-123",
                "user_id": "user123",
            },
            context=FactWriteContext(actor="stage1", timezone="UTC", span=None),
        )

    @pytest.mark.asyncio
    async def test_v3_update_fact_tool_called(self, agent_v3, mock_llm, mock_fact_management):
        """v3: LLM calls update_fact tool → FactManagementPort.update_fact is invoked."""
        mock_fact_management.update_fact.return_value = {
            "fact_id": "existing-456", "status": "updated", "message": "ok"
        }

        turn1 = LLMResponse(
            tool_calls=[ToolCall(
                name="update_fact",
                args={"fact_id": "existing-456", "updates": {"content": "User owns two cats"}}
            )]
        )
        turn2 = LLMResponse(
            text='{"operations": [{"action": "UPDATE", "fact_id": "existing-456", "reason": "correction"}]}'
        )
        mock_llm.generate_content.side_effect = [turn1, turn2]

        message = AgentMessage.create(
            sender="test",
            recipient="consolidation_agent",
            intent=AgentIntent.DELEGATE,
            payload={"task": "consolidate", "messages": [{"role": "user", "text": "I have two cats now"}]},
            context={"user_id": "user123"}
        )

        async with RequestContext(user_id="user123", account_id="account-123"):
            response = await agent_v3.execute(message)

        assert response.status == AgentStatus.SUCCESS
        # Reviewer edit (facts S3, T4a; plan §3.2): every write carries its pass's context. A Stage 1 run whose message has no
        # owner zone and whose batch messages carry no time writes as stage1 in UTC with no span; the model's dates count.
        mock_fact_management.update_fact.assert_called_once_with(
            fact_id="existing-456",
            updates={"content": "User owns two cats"},
            account_id="account-123",
            context=FactWriteContext(actor="stage1", timezone="UTC", span=None),
        )

    @pytest.mark.asyncio
    async def test_v3_search_fact_tool_called(self, agent_v3, mock_llm, mock_fact_management):
        """v3: LLM calls search_existing_facts → FactManagementPort.search_existing_facts invoked."""
        mock_fact_management.search_existing_facts.return_value = [
            {"fact_id": "f1", "content": "User owns a cat", "similarity": 0.9}
        ]

        turn1 = LLMResponse(
            tool_calls=[ToolCall(
                name="search_existing_facts",
                args={"keywords": ["pets", "cat"], "primary_query": "does user have pets", "limit": 10}
            )]
        )
        turn2 = LLMResponse(
            text='{"operations": []}'
        )
        mock_llm.generate_content.side_effect = [turn1, turn2]

        message = AgentMessage.create(
            sender="test",
            recipient="consolidation_agent",
            intent=AgentIntent.DELEGATE,
            payload={"task": "consolidate", "messages": [{"role": "user", "text": "I still have my cat"}]},
            context={"user_id": "user123"}
        )

        async with RequestContext(user_id="user123", account_id="account-123"):
            response = await agent_v3.execute(message)

        assert response.status == AgentStatus.SUCCESS
        # Reviewer edit (facts S3, T4a; plan §3.2): a search carries the owner's zone; with none in the message it is UTC.
        mock_fact_management.search_existing_facts.assert_called_once_with(
            keywords=["pets", "cat"],
            primary_query="does user have pets",
            alternative_query="",
            limit=10,
            timezone="UTC",
        )

