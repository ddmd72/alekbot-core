"""Tests for PromptCacheStrategy — business rules for prompt caching resolution."""

import pytest

from src.services.prompt_cache_strategy import PromptCacheStrategy
from src.ports.llm_port import ProviderCapabilities, PromptCacheConfig


@pytest.fixture
def strategy():
    return PromptCacheStrategy()


@pytest.fixture
def caching_capabilities():
    return ProviderCapabilities(context_caching=True)


@pytest.fixture
def non_caching_capabilities():
    return ProviderCapabilities(context_caching=False)


def test_consolidation_with_caching_provider(strategy, caching_capabilities):
    """Consolidation agent + caching provider → cache enabled."""
    result = strategy.resolve("consolidation", caching_capabilities)
    assert result is not None
    assert isinstance(result, PromptCacheConfig)
    assert result.enabled is True


def test_smart_with_caching_provider(strategy, caching_capabilities):
    """Smart agent + caching provider → cache enabled."""
    result = strategy.resolve("smart", caching_capabilities)
    assert result is not None
    assert result.enabled is True


def test_quick_with_caching_provider(strategy, caching_capabilities):
    """Quick agent + caching provider → cache enabled."""
    result = strategy.resolve("quick", caching_capabilities)
    assert result is not None
    assert result.enabled is True


def test_router_returns_none(strategy, caching_capabilities):
    """Router agent → no caching (short prompts, single-shot)."""
    result = strategy.resolve("router", caching_capabilities)
    assert result is None


def test_web_search_returns_none(strategy, caching_capabilities):
    """WebSearch agent → no caching (no system prompt)."""
    result = strategy.resolve("web_search", caching_capabilities)
    assert result is None


def test_non_caching_provider_always_returns_none(strategy, non_caching_capabilities):
    """Non-caching provider (Gemini/Grok) → None for all agent types."""
    for agent_type in ["consolidation", "smart", "quick", "router", "web_search"]:
        result = strategy.resolve(agent_type, non_caching_capabilities)
        assert result is None, f"Expected None for {agent_type} with non-caching provider"


def test_unknown_agent_type_returns_none(strategy, caching_capabilities):
    """Unknown agent type → None (safe default)."""
    result = strategy.resolve("future_agent", caching_capabilities)
    assert result is None
