"""
Unit tests for ProviderResiliencePort ABC contract.
"""
import pytest

from src.ports.provider_resilience_port import ProviderResiliencePort


class CompleteResilience(ProviderResiliencePort):
    def record_failure(self, provider_name: str) -> None:
        pass

    def record_success(self, provider_name: str) -> None:
        pass

    def is_provider_open(self, provider_name: str) -> bool:
        return False


class MissingRecordFailure(ProviderResiliencePort):
    def record_success(self, provider_name: str) -> None:
        pass

    def is_provider_open(self, provider_name: str) -> bool:
        return False


class MissingRecordSuccess(ProviderResiliencePort):
    def record_failure(self, provider_name: str) -> None:
        pass

    def is_provider_open(self, provider_name: str) -> bool:
        return False


class MissingIsProviderOpen(ProviderResiliencePort):
    def record_failure(self, provider_name: str) -> None:
        pass

    def record_success(self, provider_name: str) -> None:
        pass


class TestPortAbstractness:
    def test_abc_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            ProviderResiliencePort()  # type: ignore[abstract]

    @pytest.mark.parametrize(
        "incomplete",
        [MissingRecordFailure, MissingRecordSuccess, MissingIsProviderOpen],
    )
    def test_incomplete_subclass_cannot_instantiate(self, incomplete):
        with pytest.raises(TypeError):
            incomplete()  # type: ignore[abstract]


class TestCompleteSubclass:
    def test_can_instantiate(self):
        impl = CompleteResilience()
        assert isinstance(impl, ProviderResiliencePort)

    def test_methods_callable(self):
        impl = CompleteResilience()
        impl.record_failure("any")
        impl.record_success("any")
        assert impl.is_provider_open("any") is False


class TestContractSurface:
    @pytest.mark.parametrize(
        "method_name",
        ["record_failure", "record_success", "is_provider_open"],
    )
    def test_method_is_abstract(self, method_name):
        method = getattr(ProviderResiliencePort, method_name)
        assert getattr(method, "__isabstractmethod__", False), (
            f"{method_name} must remain abstract on the port contract"
        )

    def test_no_should_failover_on_port(self):
        # Failover-trigger policy lives in the domain (FAILOVER_TRIGGER_TYPES),
        # not on the port. Guard against accidental re-introduction.
        assert not hasattr(ProviderResiliencePort, "should_failover")
