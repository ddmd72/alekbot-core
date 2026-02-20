"""
Unit tests for WebSearchAgent.
"""

import pytest
from unittest.mock import Mock, AsyncMock, MagicMock
from src.agents.web_search_agent import WebSearchAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent, AgentStatus
from src.ports.llm_service import AgentExecutionContext, ProviderCapabilities, LLMService, LLMResponse
from src.domain.user import PerformanceTier


class TestWebSearchAgent:
    """Test suite for WebSearchAgent."""

    @pytest.fixture
    def mock_grounding_tool(self):
        return Mock()

    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock(spec=LLMService)
        llm.generate_content = AsyncMock(return_value=LLMResponse(text=""))
        return llm

    @pytest.fixture
    def agent(self, mock_grounding_tool, mock_llm):
        config = AgentConfig(
            agent_id="web_agent",
            agent_type="web_search",
            llm_model="gemini-3-flash-preview"
        )
        ec = AgentExecutionContext(
            agent_type="websearch",
            provider=mock_llm,
            model_name="gemini-3-flash-preview",
            tier=PerformanceTier.ECO,
            capabilities=ProviderCapabilities()
        )
        return WebSearchAgent(
            config=config,
            execution_context=ec,
            grounding_tool=mock_grounding_tool
        )

    @pytest.mark.asyncio
    async def test_can_handle_web_keywords(self, agent):
        """Test capability check with keywords."""
        message = AgentMessage.create(
            sender="test",
            recipient="web_agent",
            intent=AgentIntent.QUERY,
            payload={"query": "What is the weather?"}
        )
        assert await agent.can_handle(message) is True

    @pytest.mark.asyncio
    async def test_can_handle_explicit_domain(self, agent):
        """Test capability check with explicit domain."""
        message = AgentMessage.create(
            sender="test",
            recipient="web_agent",
            intent=AgentIntent.QUERY,
            payload={"query": "something", "domain": "web"}
        )
        assert await agent.can_handle(message) is True

    @pytest.mark.asyncio
    async def test_can_handle_wrong_intent(self, agent):
        """Test capability check with wrong intent."""
        message = AgentMessage.create(
            sender="test",
            recipient="web_agent",
            intent=AgentIntent.DELEGATE,
            payload={"query": "weather"}
        )
        assert await agent.can_handle(message) is False

    @pytest.mark.asyncio
    async def test_execute_success(self, agent):
        """Test successful execution."""
        agent._llm.generate_content = AsyncMock(
            return_value=LLMResponse(text="The weather is sunny.")
        )

        message = AgentMessage.create(
            sender="test",
            recipient="web_agent",
            intent=AgentIntent.QUERY,
            payload={"query": "weather in Valencia"}
        )

        response = await agent.execute(message)

        assert response.status == AgentStatus.SUCCESS
        assert response.result == "The weather is sunny."
        assert response.confidence > 0.0
        agent._llm.generate_content.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_no_results(self, agent):
        """Test execution with no results."""
        agent._llm.generate_content = AsyncMock(
            return_value=LLMResponse(text=None)
        )

        message = AgentMessage.create(
            sender="test",
            recipient="web_agent",
            intent=AgentIntent.QUERY,
            payload={"query": "unknown thing"}
        )

        response = await agent.execute(message)

        # Should return partial status with default message
        assert response.status == "partial"
        assert "No relevant information" in response.result
        assert response.confidence == 0.0

    @pytest.mark.asyncio
    async def test_execute_gemini_error(self, agent):
        """Test handling of Gemini errors."""
        agent._llm.generate_content = AsyncMock(side_effect=Exception("API Error"))

        message = AgentMessage.create(
            sender="test",
            recipient="web_agent",
            intent=AgentIntent.QUERY,
            payload={"query": "weather"}
        )

        response = await agent.execute(message)

        assert response.status == AgentStatus.FAILED
        assert "Web search failed" in response.error

    @pytest.mark.asyncio
    async def test_execute_missing_query(self, agent):
        """Test execution without query."""
        message = AgentMessage.create(
            sender="test",
            recipient="web_agent",
            intent=AgentIntent.QUERY,
            payload={}
        )

        response = await agent.execute(message)

        assert response.status == AgentStatus.FAILED
        assert "No query provided" in response.error

