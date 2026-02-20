import pytest

from src.services.provider_registry import ProviderRegistry
from src.ports.llm_service import LLMService


class FakeProvider(LLMService):
    async def generate_content(self, *args, **kwargs):
        raise NotImplementedError

    def supports_caching(self) -> bool:
        return False

    async def upload_file(self, path: str, mime_type: str):
        raise NotImplementedError

    def get_capabilities(self):
        raise NotImplementedError

    def get_model_for_tier(self, tier):
        raise NotImplementedError


def test_register_and_get_provider():
    registry = ProviderRegistry()
    provider = FakeProvider()

    registry.register("gemini", provider)

    assert registry.get("gemini") is provider


def test_get_unknown_provider_raises():
    registry = ProviderRegistry()

    with pytest.raises(ValueError, match="Provider 'missing' not registered"):
        registry.get("missing")


def test_list_available_providers():
    registry = ProviderRegistry()

    registry.register("gemini", FakeProvider())
    registry.register("claude", FakeProvider())

    assert sorted(registry.list_available()) == ["claude", "gemini"]


def test_register_overwrites_existing_provider():
    registry = ProviderRegistry()
    first = FakeProvider()
    second = FakeProvider()

    registry.register("gemini", first)
    registry.register("gemini", second)

    assert registry.get("gemini") is second