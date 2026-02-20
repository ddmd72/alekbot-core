import pytest
from unittest.mock import AsyncMock, MagicMock

from src.agents.core.quick_response_agent import create_quick_response_agent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent
from src.domain.user import PerformanceTier
from src.ports.llm_service import LLMRequest, LLMResponse, ProviderCapabilities, LLMService
from src.services.agent_context_builder import AgentExecutionContext


@pytest.mark.asyncio
async def test_quick_agent_builds_llm_request():
    llm = MagicMock(spec=LLMService)
    llm.generate_content = AsyncMock(return_value=LLMResponse(text="Hello"))

    session_store = MagicMock()
    session_store.load_session = AsyncMock(return_value=None)

    prompt_builder = MagicMock()
    prompt_builder.build_for_agent = AsyncMock(return_value="SYSTEM PROMPT STRING")

    execution_context = AgentExecutionContext(
        agent_type="quick",
        provider=llm,
        model_name="gemini",
        tier=PerformanceTier.ECO,
        capabilities=ProviderCapabilities()
    )

    agent = create_quick_response_agent(
        execution_context=execution_context,
        session_store=session_store,
        prompt_builder=prompt_builder,
        coordinator=None,
        user_id="user-1"
    )

    msg = AgentMessage.create(
        sender="user",
        recipient=agent.agent_id,
        intent=AgentIntent.QUERY,
        payload={"text": "Hi"},
        context={"user_id": "user-1", "session_id": "s1", "routing": {"user_tone": "friendly"}}
    )

    await agent.execute(msg)

    llm.generate_content.assert_called_once()
    kwargs = llm.generate_content.call_args.kwargs
    assert "request" in kwargs
    request = kwargs["request"]
    assert isinstance(request, LLMRequest)
    assert request.model_name == "gemini"