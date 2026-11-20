"""ProviderResiliencePort: a complete implementation can be driven through the port's methods."""

from src.ports.provider_resilience_port import ProviderResiliencePort


class CompleteResilience(ProviderResiliencePort):
    def record_failure(self, provider_name: str) -> None:
        pass

    def record_success(self, provider_name: str) -> None:
        pass

    def is_provider_open(self, provider_name: str) -> bool:
        return False


class TestCompleteSubclass:
    def test_methods_callable(self):
        impl = CompleteResilience()
        impl.record_failure("any")
        impl.record_success("any")
        assert impl.is_provider_open("any") is False
