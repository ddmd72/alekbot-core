"""Tests for circuit breaker."""
import pytest
import asyncio
from src.utils.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerError,
    CircuitState
)


async def failing_call():
    raise Exception("API error")


async def success_call():
    return "ok"


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_failures():
    """Test circuit opens after threshold failures."""
    config = CircuitBreakerConfig(failure_threshold=3, recovery_timeout=1.0)
    breaker = CircuitBreaker(config)

    # First 3 failures should pass through
    for _ in range(3):
        with pytest.raises(Exception, match="API error"):
            await breaker.call(failing_call)

    assert breaker.state == CircuitState.OPEN

    # 4th call should be rejected without trying
    with pytest.raises(CircuitBreakerError):
        await breaker.call(failing_call)


@pytest.mark.asyncio
async def test_circuit_breaker_recovers():
    """Test circuit recovers after timeout."""
    config = CircuitBreakerConfig(
        failure_threshold=2,
        recovery_timeout=0.5,
        success_threshold=2
    )
    breaker = CircuitBreaker(config)

    # Trigger open
    for _ in range(2):
        with pytest.raises(Exception):
            await breaker.call(failing_call)

    assert breaker.state == CircuitState.OPEN

    # Wait for recovery timeout
    await asyncio.sleep(0.6)

    # Next call should enter half-open
    await breaker.call(success_call)
    assert breaker.state == CircuitState.HALF_OPEN

    # Another success should close
    await breaker.call(success_call)
    assert breaker.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_circuit_breaker_half_open_failure():
    """Test that failure in half-open returns to open."""
    config = CircuitBreakerConfig(
        failure_threshold=2,
        recovery_timeout=0.3,
        success_threshold=2
    )
    breaker = CircuitBreaker(config)

    # Trigger open
    for _ in range(2):
        with pytest.raises(Exception):
            await breaker.call(failing_call)

    assert breaker.state == CircuitState.OPEN

    # Wait for recovery
    await asyncio.sleep(0.4)

    # Try call (enters half-open, then fails)
    with pytest.raises(Exception):
        await breaker.call(failing_call)

    # Should be back to OPEN
    assert breaker.state == CircuitState.OPEN
