"""
Unit tests for prompt-related port contracts.

Covers:
- PromptBuilderPort (6 abstract methods: 2 async, 4 sync)
"""

import inspect
import pytest
from abc import ABC
from unittest.mock import AsyncMock, MagicMock

from src.ports.prompt_builder_port import PromptBuilderPort
from src.ports.prompt_cache_strategy_port import PromptCacheStrategyPort
from src.ports.llm_port import ProviderCapabilities, PromptCacheConfig


# =============================================================================
# PromptBuilderPort
# =============================================================================

class TestPromptBuilderPortContract:
    """Verify PromptBuilderPort port declares all required abstract methods."""

    def test_is_abstract_class(self):
        assert issubclass(PromptBuilderPort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            PromptBuilderPort()

    def test_has_preload_components(self):
        assert hasattr(PromptBuilderPort, "preload_components")
        assert getattr(PromptBuilderPort.preload_components, "__isabstractmethod__", False)

    def test_has_build_for_agent(self):
        assert hasattr(PromptBuilderPort, "build_for_agent")
        assert getattr(PromptBuilderPort.build_for_agent, "__isabstractmethod__", False)

    def test_has_merge_enriched_context_with_biographical(self):
        assert hasattr(PromptBuilderPort, "merge_enriched_context_with_biographical")
        assert getattr(
            PromptBuilderPort.merge_enriched_context_with_biographical,
            "__isabstractmethod__",
            False,
        )

    def test_has_invalidate_cache(self):
        assert hasattr(PromptBuilderPort, "invalidate_cache")
        assert getattr(PromptBuilderPort.invalidate_cache, "__isabstractmethod__", False)

    def test_has_invalidate_biographical_cache(self):
        assert hasattr(PromptBuilderPort, "invalidate_biographical_cache")
        assert getattr(PromptBuilderPort.invalidate_biographical_cache, "__isabstractmethod__", False)

    def test_has_get_cache_stats(self):
        assert hasattr(PromptBuilderPort, "get_cache_stats")
        assert getattr(PromptBuilderPort.get_cache_stats, "__isabstractmethod__", False)

    def test_all_abstract_methods_count(self):
        """Port should have exactly 6 abstract methods."""
        abstract_methods = {
            name for name, method in inspect.getmembers(PromptBuilderPort)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 6, f"Expected 6 abstract methods, got {abstract_methods}"

    def test_build_for_agent_signature(self):
        sig = inspect.signature(PromptBuilderPort.build_for_agent)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "agent_type" in params
        assert "user_id" in params
        assert sig.parameters["user_id"].default is None
        assert sig.return_annotation == str

    def test_invalidate_cache_signature(self):
        sig = inspect.signature(PromptBuilderPort.invalidate_cache)
        params = list(sig.parameters.keys())
        assert params == ["self", "component_key"]
        assert sig.parameters["component_key"].default is None

    def test_invalidate_biographical_cache_signature(self):
        sig = inspect.signature(PromptBuilderPort.invalidate_biographical_cache)
        params = list(sig.parameters.keys())
        assert params == ["self", "user_id"]


class TestPromptBuilderPortMockImplementation:
    """Verify AsyncMock(spec=PromptBuilderPort) satisfies the port contract."""

    @pytest.fixture
    def mock_builder(self):
        return AsyncMock(spec=PromptBuilderPort)

    async def test_preload_components(self, mock_builder):
        await mock_builder.preload_components()
        mock_builder.preload_components.assert_called_once()

    async def test_build_for_agent_returns_string(self, mock_builder):
        mock_builder.build_for_agent.return_value = "SYSTEM PROMPT"
        result = await mock_builder.build_for_agent(agent_type="quick", user_id="u1")
        assert isinstance(result, str)
        assert result == "SYSTEM PROMPT"

    def test_invalidate_cache(self, mock_builder):
        mock_builder.invalidate_cache(component_key=None)
        mock_builder.invalidate_cache.assert_called_once_with(component_key=None)

    def test_invalidate_biographical_cache(self, mock_builder):
        mock_builder.invalidate_biographical_cache("user1")
        mock_builder.invalidate_biographical_cache.assert_called_once_with("user1")

    def test_get_cache_stats(self, mock_builder):
        mock_builder.get_cache_stats.return_value = {"hits": 5, "misses": 2}
        result = mock_builder.get_cache_stats()
        assert isinstance(result, dict)


# =============================================================================
# PromptCacheStrategyPort
# =============================================================================

class TestPromptCacheStrategyPortContract:
    """Verify PromptCacheStrategyPort port declares all required abstract methods."""

    def test_is_abstract_class(self):
        assert issubclass(PromptCacheStrategyPort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            PromptCacheStrategyPort()

    def test_has_resolve(self):
        assert getattr(PromptCacheStrategyPort.resolve, "__isabstractmethod__", False)

    def test_resolve_is_sync(self):
        assert not inspect.iscoroutinefunction(PromptCacheStrategyPort.resolve)

    def test_all_abstract_methods_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(PromptCacheStrategyPort)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 1, f"Expected 1 abstract method, got {abstract_methods}"

    def test_resolve_signature(self):
        sig = inspect.signature(PromptCacheStrategyPort.resolve)
        params = list(sig.parameters.keys())
        assert params == ["self", "agent_type", "capabilities"]


class TestPromptCacheStrategyPortMockImplementation:
    """Verify MagicMock(spec=PromptCacheStrategyPort) satisfies the port contract."""

    @pytest.fixture
    def mock_strategy(self):
        return MagicMock(spec=PromptCacheStrategyPort)

    def test_resolve_returns_none_when_no_caching(self, mock_strategy):
        mock_strategy.resolve.return_value = None
        caps = ProviderCapabilities()
        result = mock_strategy.resolve("quick", caps)
        assert result is None

    def test_resolve_returns_cache_config_when_caching(self, mock_strategy):
        config = PromptCacheConfig(ttl=300)
        mock_strategy.resolve.return_value = config
        caps = ProviderCapabilities(context_caching=True)
        result = mock_strategy.resolve("consolidation", caps)
        assert result is config
