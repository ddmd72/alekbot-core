"""The drive specialist caches its prefix and its rolling transcript (cross-cut review X10)."""
from src.ports.llm_port import ProviderCapabilities
from src.services.prompt_cache_strategy import PromptCacheStrategy


def test_drive_is_cacheable_and_multi_turn():
    config = PromptCacheStrategy().resolve("drive", ProviderCapabilities(context_caching=True))
    assert config is not None and config.enabled is True and config.cache_last_message is True


def test_drive_is_not_cached_on_a_provider_without_caching():
    assert PromptCacheStrategy().resolve("drive", ProviderCapabilities(context_caching=False)) is None
