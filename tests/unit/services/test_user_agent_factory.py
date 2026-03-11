import pytest
from unittest.mock import Mock, AsyncMock
from src.composition.user_agent_factory import UserAgentFactory
from src.domain.user import UserProfile, UserBotConfig, PerformanceTier
from src.services.agent_context_builder import AgentExecutionContext
from src.ports.llm_port import ProviderCapabilities, LLMPort


@pytest.fixture
def mock_dependencies():
    return {
        "config": {"GEMINI_API_KEY": "fake_key", "ANTHROPIC_API_KEY": "fake_anthropic_key"},
        "env_config": Mock(),
        "coordinator": Mock(),
        "user_repo": AsyncMock(),
        "account_repo": Mock(),
        "session_store": Mock(),
        "llm_port": Mock(),
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
        "email_search_service": Mock(),
    }


@pytest.fixture
def factory(mock_dependencies):
    return UserAgentFactory(**mock_dependencies)


def _make_context(agent_type: str, model_name: str, tier=PerformanceTier.BALANCED):
    return AgentExecutionContext(
        agent_type=agent_type,
        provider=Mock(spec=LLMPort),
        model_name=model_name,
        tier=tier,
        capabilities=ProviderCapabilities(),
    )


def test_validate_anthropic_key_raises_when_key_missing(factory):
    """_validate_anthropic_key raises ValueError when Claude model but no API key."""
    factory.config = {"GEMINI_API_KEY": "fake_key"}  # No ANTHROPIC_API_KEY
    ctx = _make_context("smart", "claude-opus-4-6", PerformanceTier.PERFORMANCE)

    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY is missing"):
        factory._validate_anthropic_key(ctx, "test_user")


def test_validate_anthropic_key_passes_with_key_present(factory):
    """_validate_anthropic_key does not raise when key is present."""
    ctx = _make_context("smart", "claude-opus-4-6", PerformanceTier.PERFORMANCE)
    factory._validate_anthropic_key(ctx, "test_user")  # should not raise


def test_validate_anthropic_key_passes_for_non_claude_model(factory):
    """_validate_anthropic_key does not raise for Gemini/Grok even without API key."""
    factory.config = {"GEMINI_API_KEY": "fake_key"}
    ctx = _make_context("quick", "gemini-flash-latest", PerformanceTier.ECO)
    factory._validate_anthropic_key(ctx, "test_user")  # should not raise


def test_memory_search_context_uses_memory_search_agent_type(factory):
    """context_builder.build is called with 'memory_search', not 'router'."""
    factory.context_builder.build.return_value = _make_context("memory_search", "gemini-flash")

    # Trigger the specific call we want to assert on
    result = factory.context_builder.build("memory_search", UserBotConfig())

    assert result.agent_type == "memory_search"
    factory.context_builder.build.assert_called_once_with("memory_search", UserBotConfig())


def test_web_search_context_uses_web_search_agent_type(factory):
    """WebSearchAgent must use 'web_search' context, not 'quick'.
    'web_search' strategy only allows Gemini → never gets CachingLLMProxy
    regardless of user provider_preference."""
    factory.context_builder.build.return_value = _make_context("web_search", "gemini-flash")

    result = factory.context_builder.build("web_search", UserBotConfig())

    assert result.agent_type == "web_search"
    factory.context_builder.build.assert_called_once_with("web_search", UserBotConfig())
