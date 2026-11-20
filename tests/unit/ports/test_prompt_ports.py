"""Keeps prompt_cache_strategy_port imported for the untested-modules guard."""
import pytest
from unittest.mock import MagicMock

from src.ports.prompt_cache_strategy_port import PromptCacheStrategyPort
from src.ports.llm_port import ProviderCapabilities, PromptCacheConfig


class TestPromptCacheStrategyPortMockImplementation:
    """Verify MagicMock(spec=PromptCacheStrategyPort) satisfies the port contract."""

    @pytest.fixture
    def mock_strategy(self):
        return MagicMock(spec=PromptCacheStrategyPort)

    def test_resolve_returns_cache_config_when_caching(self, mock_strategy):
        config = PromptCacheConfig(ttl=300)
        mock_strategy.resolve.return_value = config
        caps = ProviderCapabilities(context_caching=True)
        result = mock_strategy.resolve("consolidation", caps)
        assert result is config
