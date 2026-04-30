import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.agents.core.smart_response_agent import SmartResponseAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent, RoutingMetadata
from src.services.prompt_builder import PromptBuilder
from src.ports.llm_port import LLMPort, LLMResponse, UsageMetadata, ProviderCapabilities
from src.ports.session_store import SessionStore
from src.services.agent_context_builder import AgentExecutionContext
from src.domain.user import PerformanceTier, UserBotConfig
from src.infrastructure.task_execution_resolver import TaskExecutionResolver


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
    llm = MagicMock(spec=LLMPort)
    llm.generate_content = AsyncMock(return_value=LLMResponse(
        text="Hello",
        tool_calls=[],
        usage_metadata=UsageMetadata(total_tokens=10, prompt_tokens=5, completion_tokens=5)
    ))
    
    session_store = MagicMock(spec=SessionStore)
    session_store.load_session = AsyncMock(return_value=None)
    
    prompt_builder = MagicMock(spec=PromptBuilder)
    prompt_builder.build_for_agent = AsyncMock(return_value="SYSTEM PROMPT STRING")

    return llm, session_store, prompt_builder

@pytest.mark.asyncio
async def test_smart_agent_stores_and_passes_user_id(mock_deps):
    llm, session_store, prompt_builder = mock_deps
    user_id = _read_env_value("DEV_USER_ID")
    
    config = AgentConfig(
        agent_id="test",
        agent_type="smart_response",
        llm_model="gemini",
        metadata={"user_id": user_id}
    )
    
    execution_context = AgentExecutionContext(
        agent_type="smart",
        provider=llm,
        model_name="gemini",
        tier=PerformanceTier.PERFORMANCE,
        capabilities=ProviderCapabilities()
    )
    agent = SmartResponseAgent(
        config=config,
        execution_context=execution_context,
        session_store=session_store,
        prompt_builder=prompt_builder,
        resolver=MagicMock(spec=TaskExecutionResolver),
        user_config=UserBotConfig()
    )
    
    assert agent.user_id == user_id
    
    # Execute to trigger prompt build
    msg = AgentMessage.create(
        sender="user",
        recipient="test",
        intent=AgentIntent.QUERY,
        payload={"text": "hi"},
        context={"user_id": user_id, "session_id": "s1"}
    )
    
    await agent.execute(msg)

    # Verify user_id was passed to PromptBuilder
    prompt_builder.build_for_agent.assert_called_once()
    kwargs = prompt_builder.build_for_agent.call_args.kwargs
    assert kwargs["user_id"] == user_id
    assert kwargs["agent_type"] == "smart"

