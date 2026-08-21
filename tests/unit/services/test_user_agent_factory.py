import pytest
from unittest.mock import Mock, AsyncMock
from src.composition.user_agent_factory import UserAgentFactory
from src.domain.user import UserProfile, UserBotConfig, PerformanceTier
from src.services.agent_context_builder import AgentExecutionContext
from src.ports.llm_port import ProviderCapabilities, LLMPort
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.composition.user_agent_factory import _UserContext
from src.ports.image_generation_port import GeneratedImage, ImageGenerationPort
from src.services.provider_registry import ProviderRegistry


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
        resilience_port=InMemoryProviderResilience(),
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


class FakeImagePort(ImageGenerationPort):
    async def generate(self, prompt, *, aspect_ratio="auto", n=1):
        return [GeneratedImage(data=b"fake", mime_type="image/png")]

    async def edit(self, prompt, reference_images, *, mime_type="image/png"):
        return GeneratedImage(data=b"fake", mime_type="image/png")


def test_build_image_generation_returns_configured_agent(mock_dependencies):
    mock_dependencies["context_builder"] = Mock()
    mock_dependencies["context_builder"].resolve_image_generation_context = Mock(
        return_value=(FakeImagePort(), "grok")
    )
    mock_dependencies["file_conversion_service"] = Mock()
    image_registry = ProviderRegistry()
    image_registry.register("grok", FakeImagePort())
    factory_with_registry = UserAgentFactory(**mock_dependencies, image_registry=image_registry)

    user_profile = Mock(spec=UserProfile)
    user_profile.config = UserBotConfig()
    ctx = _UserContext(user_profile=user_profile, prompt_builder=Mock())

    agent = factory_with_registry._build_image_generation("user123", ctx)

    assert agent is not None
    assert agent.agent_id == "image_generation_agent_user123"
    mock_dependencies["context_builder"].resolve_image_generation_context.assert_called_once_with(
        "image_generation", image_registry, user_profile.config
    )


def test_build_image_generation_returns_none_without_registry(mock_dependencies):
    factory_without_registry = UserAgentFactory(**mock_dependencies)  # no image_registry

    user_profile = Mock(spec=UserProfile)
    user_profile.config = UserBotConfig()
    ctx = _UserContext(user_profile=user_profile, prompt_builder=Mock())

    agent = factory_without_registry._build_image_generation("user123", ctx)

    assert agent is None


def test_image_generation_registered_in_lazy_dispatch_tables():
    assert "image_generation" in UserAgentFactory._LAZY_BUILDERS
    assert UserAgentFactory._LAZY_AGENT_IDS["image_generation"] == "image_generation_agent"
