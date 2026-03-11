"""
Circuit breaker pattern for API resilience.
"""
import asyncio
import time
from enum import Enum
from typing import Callable, Any
from dataclasses import dataclass
from .logger import logger


class CircuitState(Enum):
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failures detected, reject requests
    HALF_OPEN = "half_open"  # Testing if service recovered


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5  # Open after N failures
    recovery_timeout: float = 60.0  # Try recovery after N seconds
    success_threshold: int = 2  # Close after N successes in half-open


class CircuitBreakerError(Exception):
    """Raised when circuit breaker is open."""
    pass


class CircuitBreaker:
    """
    Circuit breaker for API calls.

    Usage:
        breaker = CircuitBreaker(config)

        try:
            result = await breaker.call(risky_api_call, arg1, arg2)
        except CircuitBreakerError:
            # Circuit open - use fallback
            pass
    """

    def __init__(self, config: CircuitBreakerConfig):
        self.config = config
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = None
        self.lock = asyncio.Lock()

    async def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute function with circuit breaker protection.

        Raises:
            CircuitBreakerError: If circuit is open
        """
        async with self.lock:
            # Check if should try recovery
            if self.state == CircuitState.OPEN:
                if self.last_failure_time and \
                   time.time() - self.last_failure_time >= self.config.recovery_timeout:
                    self.state = CircuitState.HALF_OPEN
                    self.success_count = 0
                    logger.info(f"🔧 Circuit breaker entering HALF_OPEN state")
                else:
                    raise CircuitBreakerError("Circuit breaker is OPEN")

        try:
            result = await func(*args, **kwargs)
            await self._on_success()
            return result

        except Exception as e:
            await self._on_failure()
            raise

    async def _on_success(self):
        """Handle successful call."""
        async with self.lock:
            if self.state == CircuitState.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= self.config.success_threshold:
                    self.state = CircuitState.CLOSED
                    self.failure_count = 0
                    logger.info(f"✅ Circuit breaker CLOSED (service recovered)")
            elif self.state == CircuitState.CLOSED:
                self.failure_count = 0  # Reset on success

    async def _on_failure(self):
        """Handle failed call."""
        async with self.lock:
            self.failure_count += 1
            self.last_failure_time = time.time()

            if self.state == CircuitState.HALF_OPEN:
                # Failure in half-open → back to open
                self.state = CircuitState.OPEN
                logger.warning(f"🔴 Circuit breaker OPEN (half-open test failed)")

            elif self.state == CircuitState.CLOSED:
                if self.failure_count >= self.config.failure_threshold:
                    self.state = CircuitState.OPEN
                    logger.error(
                        f"🔴 Circuit breaker OPEN "
                        f"({self.failure_count} failures, threshold: {self.config.failure_threshold})"
                    )
