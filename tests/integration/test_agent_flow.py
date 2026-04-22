"""
Integration tests for agent-based conversation flow.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.infrastructure.agent_coordinator import AgentCoordinator
from src.domain.agent import AgentMessage, AgentIntent, AgentStatus
from src.agents.core.router_agent import create_router_agent
from src.agents.core.quick_response_agent import QuickResponseAgent
from src.agents.core.smart_response_agent import SmartResponseAgent
from src.domain.messaging import SmartResponse


@pytest.mark.asyncio
async def test_simple_query_routes_to_smart_agent():
    coordinator = AgentCoordinator()

    smart_agent = MagicMock(spec=SmartResponseAgent)
    smart_agent.agent_id = "smart_response_agent_u1"
    smart_agent.agent_type = "smart_response"
    smart_agent.config = MagicMock(capabilities=["complex_reasoning"])
    smart_agent.process = AsyncMock(return_value=MagicMock(
        status=AgentStatus.SUCCESS,
        result=SmartResponse(text="Привіт з Smart!"),
        confidence=1.0,
        metadata={}
    ))
    coordinator.register_agent(smart_agent)

    router = create_router_agent(
        coordinator=coordinator,
        quick_agent_id="quick_response_agent_u1",
        smart_agent_id="smart_response_agent_u1",
        user_id="u1"
    )
    coordinator.register_agent(router)

    message = AgentMessage.create(
        sender="conversation_handler",
        recipient="router_agent_u1",
        intent=AgentIntent.QUERY,
        payload={"text": "Привіт"},
        context={"session_id": "s1", "user_id": "u1"}
    )

    response = await coordinator.route_message(message)

    assert response.status == AgentStatus.SUCCESS
    assert response.result.text == "Привіт з Smart!"


@pytest.mark.asyncio
async def test_complex_query_routes_to_smart_agent():
    from unittest.mock import patch

    coordinator = AgentCoordinator()

    smart_agent = MagicMock(spec=SmartResponseAgent)
    smart_agent.agent_id = "smart_response_agent_u1"
    smart_agent.agent_type = "smart_response"
    smart_agent.config = MagicMock(capabilities=["complex_reasoning"])
    smart_agent.process = AsyncMock(return_value=MagicMock(
        status=AgentStatus.SUCCESS,
        result=SmartResponse(text="Complex answer"),
        confidence=1.0,
        metadata={}
    ))
    coordinator.register_agent(smart_agent)

    router = create_router_agent(
        coordinator=coordinator,
        quick_agent_id="quick_response_agent_u1",
        smart_agent_id=smart_agent.agent_id,
        user_id="u1"
    )
    coordinator.register_agent(router)

    # Rule-based triage caps complexity at 6 (= threshold) without LLM.
    # Patch classification to return complexity=8 to exercise Smart routing.
    complex_classification = {
        "metadata": {
            "task_complexity": "deep_reasoning",
            "needs_tools": [],
            "user_tone": "friendly",
            "reasoning": "test_override",
        },
        "semantic_lens": [],
        "search_intent": "none",
    }

    message = AgentMessage.create(
        sender="conversation_handler",
        recipient="router_agent_u1",
        intent=AgentIntent.QUERY,
        payload={"text": "Яка погода в Києві?"},
        context={"session_id": "s1", "user_id": "u1"}
    )

    with patch.object(router, "_classify_request_with_fallback", AsyncMock(return_value=complex_classification)):
        response = await coordinator.route_message(message)

    assert response.status == AgentStatus.SUCCESS
    assert isinstance(response.result, SmartResponse)
    assert response.result.text == "Complex answer"


@pytest.mark.asyncio
async def test_router_agent_returns_error_when_target_missing():
    coordinator = AgentCoordinator()
    router = create_router_agent(
        coordinator=coordinator,
        quick_agent_id="quick_response_agent_u1",
        smart_agent_id="smart_response_agent_u1",
        user_id="u1"
    )
    coordinator.register_agent(router)

    message = AgentMessage.create(
        sender="conversation_handler",
        recipient="router_agent_u1",
        intent=AgentIntent.QUERY,
        payload={"text": "Hello"},
        context={"session_id": "s1", "user_id": "u1"}
    )

    response = await coordinator.route_message(message)

    assert response.status == AgentStatus.FAILED
    assert "Unknown recipient" in response.error
