"""
Unit tests for MemorySearchAgent.
"""

import pytest
from src.agents.memory_search_agent import FactsMemoryAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent
from src.ports.fact_search_port import FactSearchPort


class _UntouchedSearch(FactSearchPort):
    async def search(self, account_id, query, *, exclude=frozenset()):
        raise AssertionError("no search expected")

    async def search_phrases(self, account_id, queries):
        raise AssertionError("no search expected")


class _UntouchedSlots:
    async def query_for_request(self, request, *, path, account_id, user_id):
        raise AssertionError("no slots call expected")


class TestMemorySearchAgent:
    """Test suite for MemorySearchAgent."""

    @pytest.fixture
    def agent(self):
        config = AgentConfig(
            agent_id="memory_agent",
            agent_type="memory_search"
        )
        return FactsMemoryAgent(
            config=config,
            fact_search=_UntouchedSearch(),
            slots=_UntouchedSlots(),
            account_id="user123",
            user_id="usr-1",
            user_location="Springfield, Portugal",
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
