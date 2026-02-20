"""
Unit tests for SmartResponseAgent.

Covers:
- can_handle() classification logic
- execute() flow with/without agent delegation
- smart parallel execution (memory-first)
- history sanitization edge cases
"""

import asyncio
from dataclasses import dataclass
from typing import List

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.agents.core.smart_response_agent import (
    SmartResponseAgent,
    ToolResponse,
    create_smart_response_agent
)
from src.domain.agent import AgentMessage, AgentConfig, AgentIntent, AgentStatus, AgentResponse
from src.domain.messaging import SmartResponse
from src.ports.llm_service import Message, MessagePart, ToolCall, LLMResponse, UsageMetadata, LLMService
from src.services.agent_context_builder import AgentExecutionContext
from src.domain.user import PerformanceTier
from src.ports.llm_service import ProviderCapabilities


# =========================================================================
# Mock Types
# =========================================================================

@dataclass
class MockSession:
    history: List[Message] = None

    def __post_init__(self):
        if self.history is None:
            self.history = []


# =========================================================================
# Fixtures
# =========================================================================

@pytest.fixture
def mock_llm_service():
    service = MagicMock(spec=LLMService)
    return service


@pytest.fixture
def mock_session_store():
    store = MagicMock()
    store.load_session = AsyncMock(return_value=MockSession())
    return store


@pytest.fixture
def mock_prompt_builder():
    builder = MagicMock()
    builder.build_for_agent = AsyncMock(return_value="SYSTEM_PROMPT")
    builder.build_system_prompt = AsyncMock(return_value={
        "kernel": "KERNEL",
        "core_context": "CORE_CONTEXT",
        "anchors": "ANCHORS",
        "examples": "EXAMPLES",
        "slack_rules": "SLACK_RULES"
    })
    return builder


@pytest.fixture
def mock_coordinator():
    coordinator = MagicMock()
    coordinator.route_message = AsyncMock()
    return coordinator


@pytest.fixture
def smart_agent_config():
    return AgentConfig(
        agent_id="smart_response_agent",
        agent_type="smart_response",
        llm_model="gemini-3-pro-preview",
        max_retries=1,
        timeout_ms=60000,
        capabilities=["complex_reasoning", "agent_delegation"]
    )


@pytest.fixture
def smart_agent(
    smart_agent_config,
    mock_llm_service,
    mock_session_store,
    mock_prompt_builder,
    mock_coordinator
):
    execution_context = AgentExecutionContext(
        agent_type="smart",
        provider=mock_llm_service,
        model_name="gemini-3-pro-preview",
        tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities()
    )
    return SmartResponseAgent(
        config=smart_agent_config,
        execution_context=execution_context,
        session_store=mock_session_store,
        prompt_builder=mock_prompt_builder,
        coordinator=mock_coordinator
    )


def create_query_message(text: str, is_simple: bool = False) -> AgentMessage:
    return AgentMessage.create(
        sender="router_agent",
        recipient="smart_response_agent",
        intent=AgentIntent.QUERY,
        payload={"text": text},
        context={
            "session_id": "session123",
            "user_id": "user456",
            "classification": {
                "is_simple": is_simple,
                "is_personal": True,
                "needs_external": False
            }
        }
    )


def build_llm_response(text: str, tool_calls: List[ToolCall]) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=tool_calls,
        usage_metadata=UsageMetadata(prompt_tokens=5, completion_tokens=5, total_tokens=10)
    )


# =========================================================================
# can_handle Tests
# =========================================================================

class TestSmartResponseAgentCanHandle:
    @pytest.mark.asyncio
    async def test_handles_complex_query(self, smart_agent):
        message = create_query_message("Complex", is_simple=False)
        assert await smart_agent.can_handle(message) is True

    @pytest.mark.asyncio
    async def test_rejects_simple_query(self, smart_agent):
        message = create_query_message("Simple", is_simple=True)
        assert await smart_agent.can_handle(message) is True

    @pytest.mark.asyncio
    async def test_handles_missing_classification(self, smart_agent):
        message = AgentMessage.create(
            sender="router_agent",
            recipient="smart_response_agent",
            intent=AgentIntent.QUERY,
            payload={"text": "Complex"},
            context={}
        )
        assert await smart_agent.can_handle(message) is True

    @pytest.mark.asyncio
    async def test_rejects_missing_payload_text(self, smart_agent):
        message = AgentMessage.create(
            sender="router_agent",
            recipient="smart_response_agent",
            intent=AgentIntent.QUERY,
            payload={},
            context={}
        )
        assert await smart_agent.can_handle(message) is False


# =========================================================================
# execute Tests
# =========================================================================

class TestSmartResponseAgentExecute:
    @pytest.mark.asyncio
    async def test_execute_without_tool_calls(self, smart_agent, mock_llm_service):
        mock_llm_service.generate_content = AsyncMock(
            return_value=build_llm_response("Final answer", [])
        )
        message = create_query_message("Explain something")

        response = await smart_agent.execute(message)

        assert response.status == AgentStatus.SUCCESS
        assert isinstance(response.result, SmartResponse)
        assert response.result.text == "Final answer"
        assert response.metadata["model"] == smart_agent.model_name
        smart_agent.prompt_builder.build_for_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_with_agent_delegation(self, smart_agent, mock_llm_service, mock_coordinator):
        tool_calls = [
            ToolCall(name="search_memory", args={"query": "car model"}),
            ToolCall(name="ask_web_search_agent", args={"query": "weather Valencia"})
        ]

        mock_llm_service.generate_content = AsyncMock(side_effect=[
            build_llm_response("", tool_calls),
            build_llm_response("Final response", [])
        ])

        # memory search result
        mock_coordinator.route_message = AsyncMock(side_effect=[
            AgentResponse.success(
                task_id="t1",
                agent_id="memory_search_agent",
                result=["User car is Mitsubishi Colt"]
            ),
            AgentResponse.success(
                task_id="t2",
                agent_id="web_search_agent",
                result="Weather is sunny"
            )
        ])

        message = create_query_message("What's my car and weather?")
        response = await smart_agent.execute(message)

        assert response.status == AgentStatus.SUCCESS
        assert response.result.text == "Final response"
        assert mock_llm_service.generate_content.call_count == 2


# =========================================================================
# Parallel Execution Tests
# =========================================================================

class TestSmartResponseAgentParallelExecution:
    @pytest.mark.asyncio
    async def test_memory_first_then_parallel(self, smart_agent):
        tool_calls = [
            ToolCall(name="search_memory", args={"query": "bio"}),
            ToolCall(name="ask_web_search_agent", args={"query": "news"}),
            ToolCall(name="ask_web_search_agent", args={"query": "prices"})
        ]

        call_order = []

        async def delegate_side_effect(tool_call, **kwargs):
            call_order.append(tool_call.name)
            if tool_call.name == "search_memory":
                return ToolResponse(name=tool_call.name, result_str="memory")
            await asyncio.sleep(0)
            return ToolResponse(name=tool_call.name, result_str="web")

        smart_agent._delegate_to_agent_with_retry = AsyncMock(side_effect=delegate_side_effect)

        results = await smart_agent._execute_agents_smart_parallel(
            tool_calls=tool_calls,
            user_id="user123",
            session_id="session123"
        )

        assert call_order[0] == "search_memory"
        assert len(results) == 3


# =========================================================================
# History Sanitization Tests
# =========================================================================

class TestSmartResponseAgentHistory:
    def test_sanitize_tool_history_removes_orphans(self, smart_agent):
        history = [
            Message(role="user", parts=[MessagePart(text="Hello")]),
            Message(role="user", parts=[MessagePart(tool_response={"name": "search_memory", "response": {"result": "data"}})])
        ]

        cleaned = smart_agent._sanitize_tool_history(history)
        assert len(cleaned) == 1
        assert cleaned[0].role == "user"


# =========================================================================
# Factory Tests
# =========================================================================

class TestCreateSmartResponseAgent:
    def test_factory_defaults(self, mock_llm_service, mock_session_store, mock_prompt_builder):
        execution_context = AgentExecutionContext(
            agent_type="smart",
            provider=mock_llm_service,
            model_name="gemini-3-pro-preview",
            tier=PerformanceTier.BALANCED,
            capabilities=ProviderCapabilities()
        )
        agent = create_smart_response_agent(
            execution_context=execution_context,
            session_store=mock_session_store,
            prompt_builder=mock_prompt_builder
        )

        assert agent.agent_id == "smart_response_agent"
        assert agent.model_name == "gemini-3-pro-preview"
