"""Tests for CachingLLMProxy — transparent LLM wrapper for prompt caching."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.services.caching_llm_proxy import CachingLLMProxy
from src.ports.llm_service import (
    LLMService,
    LLMRequest,
    LLMResponse,
    PromptCacheConfig,
    ProviderCapabilities,
    Message,
    MessagePart,
)
from src.domain.user import PerformanceTier


@pytest.fixture
def mock_inner():
    inner = AsyncMock(spec=LLMService)
    inner.supports_caching.return_value = True
    inner.get_capabilities.return_value = ProviderCapabilities(context_caching=True)
    inner.get_model_for_tier.return_value = "claude-opus-4"
    inner.generate_content.return_value = LLMResponse(text="test response")
    return inner


@pytest.fixture
def cache_config():
    return PromptCacheConfig(enabled=True)


@pytest.fixture
def proxy(mock_inner, cache_config):
    return CachingLLMProxy(mock_inner, cache_config)


@pytest.fixture
def sample_request():
    return LLMRequest(
        model_name="claude-opus-4",
        messages=[Message(role="user", parts=[MessagePart(text="hello")])],
        system_instruction="You are helpful.",
        temperature=0.7,
    )


async def test_injects_cache_config_when_request_has_none(proxy, mock_inner, sample_request):
    """Proxy injects cache_config when request doesn't have one."""
    assert sample_request.cache_config is None

    await proxy.generate_content(request=sample_request)

    call_kwargs = mock_inner.generate_content.call_args
    forwarded_request = call_kwargs.kwargs["request"]
    assert forwarded_request.cache_config is not None
    assert forwarded_request.cache_config.enabled is True


async def test_preserves_explicit_cache_config(proxy, mock_inner):
    """Proxy does NOT override explicit cache_config set by caller."""
    explicit_config = PromptCacheConfig(enabled=False, ttl_seconds=60)
    request = LLMRequest(
        model_name="claude-opus-4",
        messages=[Message(role="user", parts=[MessagePart(text="hello")])],
        cache_config=explicit_config,
    )

    await proxy.generate_content(request=request)

    call_kwargs = mock_inner.generate_content.call_args
    forwarded_request = call_kwargs.kwargs["request"]
    assert forwarded_request.cache_config is explicit_config


async def test_does_not_mutate_original_request(proxy, mock_inner, sample_request):
    """Proxy creates a copy, never mutates the original LLMRequest."""
    await proxy.generate_content(request=sample_request)

    # Original request should still have no cache_config
    assert sample_request.cache_config is None


async def test_delegates_supports_caching(proxy, mock_inner):
    """supports_caching delegates to inner provider."""
    result = proxy.supports_caching()
    assert result is True
    mock_inner.supports_caching.assert_called_once()


async def test_delegates_get_capabilities(proxy, mock_inner):
    """get_capabilities delegates to inner provider."""
    caps = proxy.get_capabilities()
    assert caps.context_caching is True
    mock_inner.get_capabilities.assert_called_once()


async def test_delegates_get_model_for_tier(proxy, mock_inner):
    """get_model_for_tier delegates to inner provider."""
    model = proxy.get_model_for_tier(PerformanceTier.BALANCED)
    assert model == "claude-opus-4"
    mock_inner.get_model_for_tier.assert_called_once_with(PerformanceTier.BALANCED)
