"""
Unit tests for WebSearchLightAgent.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from src.agents.web_search_light_agent import WebSearchLightAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent, AgentStatus
from src.ports.llm_port import AgentExecutionContext, ProviderCapabilities, LLMPort, LLMResponse, LLMRequest
from src.domain.user import PerformanceTier


class TestWebSearchLightAgent:
    """Test suite for WebSearchLightAgent."""

    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock(spec=LLMPort)
        llm.generate_content = AsyncMock(return_value=LLMResponse(text=""))
        return llm

    @pytest.fixture
    def agent(self, mock_llm):
        config = AgentConfig(
            agent_id="web_light_agent",
            agent_type="web_search_light",
            llm_model="gemini-3-flash-lite-preview"
        )
        ec = AgentExecutionContext(
            agent_type="websearch_light",
            provider=mock_llm,
            model_name="gemini-3-flash-lite-preview",
            tier=PerformanceTier.ECO,
            capabilities=ProviderCapabilities()
        )
        return WebSearchLightAgent(
            config=config,
            execution_context=ec,
        )

    @pytest.mark.asyncio
    async def test_can_handle_valid_query(self, agent):
        message = AgentMessage.create(
            sender="test",
            recipient="web_light_agent",
            intent=AgentIntent.QUERY,
            payload={"query": "What is the weather?"}
        )
        assert await agent.can_handle(message) is True

    @pytest.mark.asyncio
    async def test_can_handle_wrong_intent(self, agent):
        message = AgentMessage.create(
            sender="test",
            recipient="web_light_agent",
            intent=AgentIntent.DELEGATE,
            payload={"query": "weather"}
        )
        assert await agent.can_handle(message) is False

    @pytest.mark.asyncio
    async def test_can_handle_empty_query(self, agent):
        message = AgentMessage.create(
            sender="test",
            recipient="web_light_agent",
            intent=AgentIntent.QUERY,
            payload={"query": ""}
        )
        assert await agent.can_handle(message) is False

    @pytest.mark.asyncio
    async def test_execute_success(self, agent):
        agent._llm.generate_content = AsyncMock(
            return_value=LLMResponse(text="It is sunny in Valencia.")
        )
        message = AgentMessage.create(
            sender="test",
            recipient="web_light_agent",
            intent=AgentIntent.QUERY,
            payload={"query": "weather in Valencia"}
        )

        response = await agent.execute(message)

        assert response.status == AgentStatus.SUCCESS
        assert response.result == "It is sunny in Valencia."
        assert response.confidence > 0.0
        agent._llm.generate_content.assert_called_once()
        # Verify use_grounding=True; system_instruction = cognitive process + datetime; user message = raw query
        sent_request: LLMRequest = agent._llm.generate_content.call_args.kwargs["request"]
        assert sent_request.use_grounding is True
        assert sent_request.tools is None
        assert sent_request.system_instruction is not None
        assert "current_date_time" in sent_request.system_instruction
        assert sent_request.messages[0].parts[0].text == "weather in Valencia"

    @pytest.mark.asyncio
    async def test_execute_no_results_returns_fallback(self, agent):
        """Light agent returns SUCCESS with fallback text (no 'partial' status)."""
        agent._llm.generate_content = AsyncMock(
            return_value=LLMResponse(text=None)
        )
        message = AgentMessage.create(
            sender="test",
            recipient="web_light_agent",
            intent=AgentIntent.QUERY,
            payload={"query": "unknown thing"}
        )

        response = await agent.execute(message)

        assert response.status == AgentStatus.SUCCESS
        assert "No relevant information found" in response.result

    @pytest.mark.asyncio
    async def test_execute_llm_error(self, agent):
        agent._llm.generate_content = AsyncMock(side_effect=Exception("API Error"))
        message = AgentMessage.create(
            sender="test",
            recipient="web_light_agent",
            intent=AgentIntent.QUERY,
            payload={"query": "weather"}
        )

        response = await agent.execute(message)

        assert response.status == AgentStatus.FAILED
        assert "Web search failed" in response.error

    @pytest.mark.asyncio
    async def test_execute_missing_query(self, agent):
        message = AgentMessage.create(
            sender="test",
            recipient="web_light_agent",
            intent=AgentIntent.QUERY,
            payload={}
        )

        response = await agent.execute(message)

        assert response.status == AgentStatus.FAILED
        assert "No query provided" in response.error
