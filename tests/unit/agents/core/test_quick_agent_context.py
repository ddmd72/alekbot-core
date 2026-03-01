import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.agents.core.quick_response_agent import QuickResponseAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent, AgentStatus, AgentResponse
from src.services.prompt_builder import PromptBuilder
from src.ports.llm_service import LLMService, LLMResponse, UsageMetadata, ToolCall, ProviderCapabilities
from src.ports.session_store import SessionStore
from src.services.agent_context_builder import AgentExecutionContext
from src.domain.user import PerformanceTier


def _read_env_value(key: str) -> str:
    repo_root = Path(__file__).resolve().parents[5]
    env_path = repo_root / ".env"
    if not env_path.exists():
        return "test-user"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"{key} not found in .env")

@pytest.fixture
def mock_deps():
    llm = MagicMock(spec=LLMService)
    llm.generate_content = AsyncMock(return_value=LLMResponse(
        text="Hi",
        tool_calls=[],
        usage_metadata=UsageMetadata(total_tokens=5, prompt_tokens=2, completion_tokens=3)
    ))
    
    session_store = MagicMock(spec=SessionStore)
    session_store.load_session = AsyncMock(return_value=None)
    
    prompt_builder = MagicMock(spec=PromptBuilder)
    prompt_builder.build_for_agent = AsyncMock(return_value="SYSTEM PROMPT STRING")

    return llm, session_store, prompt_builder


@pytest.mark.asyncio
async def test_quick_agent_single_turn_tool_flow(mock_deps):
    llm, session_store, prompt_builder = mock_deps
    user_id = _read_env_value("DEV_USER_ID")

    config = AgentConfig(
        agent_id="test",
        agent_type="quick_response",
        llm_model="gemini",
        metadata={"user_id": user_id}
    )

    execution_context = AgentExecutionContext(
        agent_type="quick",
        provider=llm,
        model_name="gemini",
        tier=PerformanceTier.ECO,
        capabilities=ProviderCapabilities()
    )
    coordinator = MagicMock()
    coordinator.route_message = AsyncMock()
    coordinator.get_available_intents = MagicMock(return_value=[
        {"name": "search_memory", "description": "Search biographical facts"},
        {"name": "search_web_light", "description": "Lightweight web search"},
    ])
    coordinator.handle_delegation = AsyncMock(
        return_value=AgentResponse.success(
            task_id="t1",
            agent_id="memory_search_agent",
            result="glove size: M",
            confidence=1.0,
        )
    )
    agent = QuickResponseAgent(
        config=config,
        execution_context=execution_context,
        session_store=session_store,
        prompt_builder=prompt_builder,
        coordinator=coordinator
    )

    # Quick uses delegate_to_specialist with intent/query args
    tool_call = ToolCall(
        name="delegate_to_specialist",
        args={"intent": "search_memory", "query": "glove size"}
    )

    llm.generate_content = AsyncMock(side_effect=[
        LLMResponse(
            text=None,
            tool_calls=[tool_call],
            usage_metadata=UsageMetadata(total_tokens=5, prompt_tokens=2, completion_tokens=3)
        ),
        LLMResponse(
            text="Glove size is M",
            tool_calls=[],
            usage_metadata=UsageMetadata(total_tokens=7, prompt_tokens=3, completion_tokens=4)
        )
    ])

    msg = AgentMessage.create(
        sender="user",
        recipient="test",
        intent=AgentIntent.QUERY,
        payload={"text": "What is my glove size?"},
        context={"user_id": user_id, "session_id": "s1", "classification": {"is_simple": True}}
    )

    response = await agent.execute(msg)

    assert response.status == AgentStatus.SUCCESS
    assert response.result.text == "Glove size is M"  # delegation loop returns final LLM text
    assert llm.generate_content.call_count == 2  # turn 1 (tool call) + turn 2 (final answer)
    coordinator.handle_delegation.assert_called_once_with(
        intent="search_memory",
        query="glove size",
        context={"user_id": user_id, "account_id": None, "session_id": "s1", "memory_context": [], "params": {}},
        calling_agent_id="test",
    )

@pytest.mark.asyncio
async def test_quick_agent_stores_and_passes_user_id(mock_deps):
    llm, session_store, prompt_builder = mock_deps
    user_id = _read_env_value("DEV_USER_ID")
    
    config = AgentConfig(
        agent_id="test",
        agent_type="quick_response",
        llm_model="gemini",
        metadata={"user_id": user_id}
    )
    
    execution_context = AgentExecutionContext(
        agent_type="quick",
        provider=llm,
        model_name="gemini",
        tier=PerformanceTier.ECO,
        capabilities=ProviderCapabilities()
    )
    agent = QuickResponseAgent(
        config=config,
        execution_context=execution_context,
        session_store=session_store,
        prompt_builder=prompt_builder
    )
    
    assert agent.user_id == user_id
    
    # Execute
    msg = AgentMessage.create(
        sender="user",
        recipient="test",
        intent=AgentIntent.QUERY,
        payload={"text": "hi"},
        context={"user_id": user_id, "session_id": "s1", "classification": {"is_simple": True}}
    )
    
    await agent.execute(msg)

    # Verify user_id was passed to PromptBuilder
    prompt_builder.build_for_agent.assert_called_once()
    kwargs = prompt_builder.build_for_agent.call_args.kwargs
    assert kwargs["user_id"] == user_id
    assert kwargs["agent_type"] == "quick"

