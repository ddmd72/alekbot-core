"""
Unit tests for InMemoryProviderResilience.
"""
import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience


class FakeClock:
    """Controllable monotonic clock for deterministic tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def resilience(clock):
    """Threshold 3 within 60s window; 30s cooldown."""
    return InMemoryProviderResilience(
        failure_threshold=3,
        window_seconds=60.0,
        cooldown_seconds=30.0,
        time_source=clock,
    )


# --------------------------------------------------------------------------- #
# Constructor                                                                 #
# --------------------------------------------------------------------------- #


class TestConstructorValidation:
    def test_default_constructor_works(self):
        impl = InMemoryProviderResilience()
        assert impl.is_provider_open("anything") is False

    @pytest.mark.parametrize("bad", [0, -1, -100])
    def test_rejects_non_positive_threshold(self, bad):
        with pytest.raises(ValueError):
            InMemoryProviderResilience(failure_threshold=bad)

    @pytest.mark.parametrize("bad", [0, -0.1, -60])
    def test_rejects_non_positive_window(self, bad):
        with pytest.raises(ValueError):
            InMemoryProviderResilience(window_seconds=bad)

    @pytest.mark.parametrize("bad", [0, -1, -30])
    def test_rejects_non_positive_cooldown(self, bad):
        with pytest.raises(ValueError):
            InMemoryProviderResilience(cooldown_seconds=bad)


# --------------------------------------------------------------------------- #
# State machine                                                               #
# --------------------------------------------------------------------------- #


class TestStateMachine:
    def test_closed_initially(self, resilience):
        assert resilience.is_provider_open("claude") is False

    def test_below_threshold_stays_closed(self, resilience):
        resilience.record_failure("claude")
        resilience.record_failure("claude")
        assert resilience.is_provider_open("claude") is False

    def test_at_threshold_opens_eagerly(self, resilience):
        # Eager opening: the threshold-th record_failure trips the
        # breaker without needing a separate query.
        for _ in range(3):
            resilience.record_failure("claude")
        assert resilience.is_provider_open("claude") is True

    def test_remains_open_during_cooldown(self, resilience, clock):
        for _ in range(3):
            resilience.record_failure("claude")
        clock.advance(29.99)
        assert resilience.is_provider_open("claude") is True

    def test_transitions_to_half_open_after_cooldown(self, resilience, clock):
        for _ in range(3):
            resilience.record_failure("claude")
        clock.advance(30.0)
        # HALF-OPEN reads as CLOSED externally — caller is allowed to probe.
        assert resilience.is_provider_open("claude") is False

    def test_half_open_clears_failure_window(self, resilience, clock):
        # After cooldown elapses, the still-OPEN failures must not
        # immediately re-open the breaker on the very next failure.
        for _ in range(3):
            resilience.record_failure("claude")
        clock.advance(30.0)
        # Probe transitions to HALF-OPEN (clears state).
        assert resilience.is_provider_open("claude") is False
        # One fresh failure: would re-open if old state survived.
        resilience.record_failure("claude")
        assert resilience.is_provider_open("claude") is False

    def test_record_success_clears_open(self, resilience):
        for _ in range(3):
            resilience.record_failure("claude")
        assert resilience.is_provider_open("claude") is True
        resilience.record_success("claude")
        assert resilience.is_provider_open("claude") is False

    def test_record_success_clears_failure_window(self, resilience):
        # 2 failures (below threshold). Success clears the counter so
        # the next 2 alone do not trip.
        resilience.record_failure("claude")
        resilience.record_failure("claude")
        resilience.record_success("claude")
        resilience.record_failure("claude")
        resilience.record_failure("claude")
        assert resilience.is_provider_open("claude") is False


# --------------------------------------------------------------------------- #
# Rolling-window eviction                                                     #
# --------------------------------------------------------------------------- #


class TestRollingWindow:
    def test_failures_within_window_count_together(self, resilience, clock):
        resilience.record_failure("claude")
        clock.advance(30.0)
        resilience.record_failure("claude")
        resilience.record_failure("claude")
        # All three within 60s window → opens.
        assert resilience.is_provider_open("claude") is True

    def test_old_failures_evicted_so_fresh_failure_alone_does_not_open(
        self, resilience, clock
    ):
        resilience.record_failure("openai")
        resilience.record_failure("openai")
        clock.advance(61.0)  # past the 60s rolling window
        resilience.record_failure("openai")
        # Only one in window → CLOSED.
        assert resilience.is_provider_open("openai") is False


# --------------------------------------------------------------------------- #
# Multi-provider isolation                                                    #
# --------------------------------------------------------------------------- #


class TestMultiProviderIsolation:
    def test_one_provider_open_does_not_affect_another(self, resilience):
        for _ in range(3):
            resilience.record_failure("claude")
        assert resilience.is_provider_open("claude") is True
        assert resilience.is_provider_open("openai") is False
        assert resilience.is_provider_open("gemini") is False

    def test_record_success_isolated_per_provider(self, resilience):
        for _ in range(3):
            resilience.record_failure("claude")
        for _ in range(3):
            resilience.record_failure("openai")
        resilience.record_success("claude")
        assert resilience.is_provider_open("claude") is False
        assert resilience.is_provider_open("openai") is True

    def test_unknown_provider_starts_closed(self, resilience):
        assert resilience.is_provider_open("nonexistent") is False


# --------------------------------------------------------------------------- #
# on_open escalation hook                                                      #
# --------------------------------------------------------------------------- #


class TestOnOpenCallback:
    def _make(self, clock, calls):
        return InMemoryProviderResilience(
            failure_threshold=3,
            window_seconds=60.0,
            cooldown_seconds=30.0,
            time_source=clock,
            on_open=calls.append,
        )

    def test_fires_once_on_transition_with_provider_name(self, clock):
        calls = []
        breaker = self._make(clock, calls)
        for _ in range(3):
            breaker.record_failure("claude")
        assert calls == ["claude"]

    def test_does_not_fire_below_threshold(self, clock):
        calls = []
        breaker = self._make(clock, calls)
        breaker.record_failure("claude")
        breaker.record_failure("claude")
        assert calls == []

    def test_does_not_refire_while_open(self, clock):
        calls = []
        breaker = self._make(clock, calls)
        for _ in range(5):  # 2 extra failures past the threshold
            breaker.record_failure("claude")
        assert calls == ["claude"]

    def test_refires_after_reopen_following_half_open(self, clock):
        calls = []
        breaker = self._make(clock, calls)
        for _ in range(3):
            breaker.record_failure("claude")
        clock.advance(30.0)
        breaker.is_provider_open("claude")  # HALF-OPEN clears state
        for _ in range(3):
            breaker.record_failure("claude")  # re-trip
        assert calls == ["claude", "claude"]

    def test_fires_per_provider(self, clock):
        calls = []
        breaker = self._make(clock, calls)
        for _ in range(3):
            breaker.record_failure("claude")
        for _ in range(3):
            breaker.record_failure("openai")
        assert calls == ["claude", "openai"]

    def test_default_on_open_is_noop(self, clock):
        breaker = InMemoryProviderResilience(failure_threshold=3, time_source=clock)
        for _ in range(3):
            breaker.record_failure("claude")  # must not raise
        assert breaker.is_provider_open("claude") is True
