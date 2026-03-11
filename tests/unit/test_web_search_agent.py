"""
Unit tests for WebSearchAgent.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from src.agents.web_search_agent import WebSearchAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent, AgentStatus
from src.ports.llm_port import AgentExecutionContext, ProviderCapabilities, LLMPort, LLMResponse, LLMRequest
from src.domain.user import PerformanceTier


class TestWebSearchAgent:
    """Test suite for WebSearchAgent."""

    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock(spec=LLMPort)
        llm.generate_content = AsyncMock(return_value=LLMResponse(text=""))
        return llm

    @pytest.fixture
    def agent(self, mock_llm):
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
        )

    # ------------------------------------------------------------------ #
    # can_handle
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_can_handle_query(self, agent):
        message = AgentMessage.create(
            sender="test", recipient="web_agent",
            intent=AgentIntent.QUERY,
            payload={"query": "What is the weather?"}
        )
        assert await agent.can_handle(message) is True

    @pytest.mark.asyncio
    async def test_can_handle_url(self, agent):
        message = AgentMessage.create(
            sender="test", recipient="web_agent",
            intent=AgentIntent.QUERY,
            payload={"url": "https://example.com"}
        )
        assert await agent.can_handle(message) is True

    @pytest.mark.asyncio
    async def test_can_handle_wrong_intent(self, agent):
        message = AgentMessage.create(
            sender="test", recipient="web_agent",
            intent=AgentIntent.DELEGATE,
            payload={"query": "weather"}
        )
        assert await agent.can_handle(message) is False

    @pytest.mark.asyncio
    async def test_can_handle_empty_payload(self, agent):
        message = AgentMessage.create(
            sender="test", recipient="web_agent",
            intent=AgentIntent.QUERY,
            payload={}
        )
        assert await agent.can_handle(message) is False

    # ------------------------------------------------------------------ #
    # search_web path
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_execute_search_web_success(self, agent):
        agent._llm.generate_content = AsyncMock(
            return_value=LLMResponse(text="The weather is sunny.")
        )
        message = AgentMessage.create(
            sender="test", recipient="web_agent",
            intent=AgentIntent.QUERY,
            payload={"query": "weather in Valencia"}
        )

        response = await agent.execute(message)

        assert response.status == AgentStatus.SUCCESS
        assert response.result == "The weather is sunny."
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
    async def test_execute_search_web_no_results(self, agent):
        agent._llm.generate_content = AsyncMock(
            return_value=LLMResponse(text=None)
        )
        message = AgentMessage.create(
            sender="test", recipient="web_agent",
            intent=AgentIntent.QUERY,
            payload={"query": "unknown thing"}
        )

        response = await agent.execute(message)

        assert response.status == "partial"
        assert "No relevant information" in response.result
        assert response.confidence == 0.0

    @pytest.mark.asyncio
    async def test_execute_search_web_llm_error(self, agent):
        agent._llm.generate_content = AsyncMock(side_effect=Exception("API Error"))
        message = AgentMessage.create(
            sender="test", recipient="web_agent",
            intent=AgentIntent.QUERY,
            payload={"query": "weather"}
        )

        response = await agent.execute(message)

        assert response.status == AgentStatus.FAILED
        assert "Web search failed" in response.error

    # ------------------------------------------------------------------ #
    # fetch_url path
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_execute_fetch_url_success(self, agent):
        agent._llm.generate_content = AsyncMock(
            return_value=LLMResponse(text="Page content here.")
        )
        message = AgentMessage.create(
            sender="test", recipient="web_agent",
            intent=AgentIntent.QUERY,
            payload={"url": "https://example.com/article"}
        )

        response = await agent.execute(message)

        assert response.status == AgentStatus.SUCCESS
        assert response.result == "Page content here."
        agent._llm.generate_content.assert_called_once()
        sent_request: LLMRequest = agent._llm.generate_content.call_args.kwargs["request"]
        assert sent_request.use_grounding is True
        assert "https://example.com/article" in sent_request.messages[0].parts[0].text

    @pytest.mark.asyncio
    async def test_execute_fetch_url_error(self, agent):
        agent._llm.generate_content = AsyncMock(side_effect=Exception("Fetch failed"))
        message = AgentMessage.create(
            sender="test", recipient="web_agent",
            intent=AgentIntent.QUERY,
            payload={"url": "https://example.com"}
        )

        response = await agent.execute(message)

        assert response.status == AgentStatus.FAILED
        assert "URL fetch failed" in response.error

    # ------------------------------------------------------------------ #
    # edge cases
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_execute_missing_query_and_url(self, agent):
        message = AgentMessage.create(
            sender="test", recipient="web_agent",
            intent=AgentIntent.QUERY,
            payload={}
        )

        response = await agent.execute(message)

        assert response.status == AgentStatus.FAILED
        assert "No query or url" in response.error

    @pytest.mark.asyncio
    async def test_url_takes_priority_over_query(self, agent):
        """When both url and query present, url path is taken."""
        agent._llm.generate_content = AsyncMock(
            return_value=LLMResponse(text="Fetched content.")
        )
        message = AgentMessage.create(
            sender="test", recipient="web_agent",
            intent=AgentIntent.QUERY,
            payload={"url": "https://example.com", "query": "ignored"}
        )

        response = await agent.execute(message)

        assert response.status == AgentStatus.SUCCESS
        sent_request: LLMRequest = agent._llm.generate_content.call_args.kwargs["request"]
        assert "https://example.com" in sent_request.messages[0].parts[0].text
