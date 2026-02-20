import pytest
from unittest.mock import Mock, AsyncMock
from src.services.user_agent_factory import UserAgentFactory
from src.domain.user import UserProfile, UserBotConfig, PerformanceTier, LLMProvider
from src.services.agent_context_builder import AgentExecutionContext
from src.ports.llm_service import ProviderCapabilities, LLMService

@pytest.fixture
def mock_dependencies():
    return {
        "config": {"GEMINI_API_KEY": "fake_key", "ANTHROPIC_API_KEY": "fake_anthropic_key"},
        "env_config": Mock(),
        "coordinator": Mock(),
        "user_repo": AsyncMock(),
        "account_repo": Mock(),
        "session_store": Mock(),
        "llm_service": Mock(),
        "claude_service": Mock(),
        "grok_service": None,
        "embedding_service": Mock(),
        "repository": AsyncMock(),
        "config_service": Mock(),
        "biographical_context_service": Mock(),
        "registry": Mock(),
        "context_builder": Mock(),
        "component_service": Mock(),
        "assembly_service": None,
        "fact_write_service": Mock(),
        "fact_management_adapter_factory": Mock(return_value=Mock()),
    }

@pytest.fixture
def factory(mock_dependencies):
    return UserAgentFactory(**mock_dependencies)

@pytest.mark.asyncio
async def test_resolve_smart_llm_uses_context_builder(factory):
    # Setup
    user_profile = Mock(spec=UserProfile)
    user_profile.user_id = "test_user"
    user_profile.config = UserBotConfig()

    mock_provider = Mock(spec=LLMService)
    mock_context = AgentExecutionContext(
        agent_type="smart",
        provider=mock_provider,
        model_name="test-smart-model",
        tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities()
    )
    factory.context_builder.build.return_value = mock_context

    # Execute
    provider, model = factory._resolve_smart_llm(user_profile)

    # Verify
    factory.context_builder.build.assert_called_once_with("smart", user_profile.config)
    assert provider == mock_provider
    assert model == "test-smart-model"

@pytest.mark.asyncio
async def test_resolve_light_llm_uses_context_builder(factory):
    # Setup
    user_profile = Mock(spec=UserProfile)
    user_profile.user_id = "test_user"
    user_profile.config = UserBotConfig()

    mock_provider = Mock(spec=LLMService)
    mock_context = AgentExecutionContext(
        agent_type="quick",
        provider=mock_provider,
        model_name="test-light-model",
        tier=PerformanceTier.ECO,
        capabilities=ProviderCapabilities()
    )
    factory.context_builder.build.return_value = mock_context

    # Execute
    provider, model = factory._resolve_light_llm(user_profile)

    # Verify
    factory.context_builder.build.assert_called_once_with("quick", user_profile.config)
    assert provider == mock_provider
    assert model == "test-light-model"

@pytest.mark.asyncio
async def test_resolve_smart_llm_validates_anthropic_key(factory):
    # Setup
    user_profile = Mock(spec=UserProfile)
    user_profile.user_id = "test_user"
    user_profile.config = UserBotConfig()

    mock_provider = Mock(spec=LLMService)
    mock_context = AgentExecutionContext(
        agent_type="smart",
        provider=mock_provider,
        model_name="claude-opus-4-6",  # Starts with "claude" → triggers the check
        tier=PerformanceTier.PERFORMANCE,
        capabilities=ProviderCapabilities()
    )
    factory.context_builder.build.return_value = mock_context

    # Remove API key from config
    factory.config = {"GEMINI_API_KEY": "fake_key"}  # No ANTHROPIC_API_KEY

    # Execute & Verify
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY is missing"):
        factory._resolve_smart_llm(user_profile)
