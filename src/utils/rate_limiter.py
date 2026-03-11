"""
Platform-agnostic rate limiter using token bucket algorithm.
"""
import asyncio
import time
from dataclasses import dataclass


@dataclass
class RateLimitConfig:
    """Rate limit configuration for a platform."""
    messages_per_second: float  # Max messages per second
    burst_size: int  # Max burst (bucket size)

    @classmethod
    def for_slack(cls) -> 'RateLimitConfig':
        """Slack: ~1 msg/sec per channel (soft limit)."""
        return cls(messages_per_second=1.0, burst_size=5)

    @classmethod
    def for_telegram(cls) -> 'RateLimitConfig':
        """Telegram: 30 msg/sec per chat (hard limit with 429 ban)."""
        return cls(messages_per_second=20.0, burst_size=30)  # Leave safety margin


class RateLimiter:
    """
    Token bucket rate limiter.

    Usage:
        limiter = RateLimiter(RateLimitConfig.for_telegram())
        for chunk in chunks:
            await limiter.acquire()  # Blocks if rate limit exceeded
            await send_message(chunk)
    """

    def __init__(self, config: RateLimitConfig):
        self.config = config
        self.tokens = float(config.burst_size)
        self.last_update = time.time()
        self.lock = asyncio.Lock()

    async def acquire(self, tokens: int = 1) -> None:
        """
        Acquire tokens (blocks if insufficient).

        Args:
            tokens: Number of tokens to acquire (default: 1 message)
        """
        async with self.lock:
            while True:
                # Refill tokens based on time elapsed
                now = time.time()
                elapsed = now - self.last_update
                self.tokens = min(
                    self.config.burst_size,
                    self.tokens + elapsed * self.config.messages_per_second
                )
                self.last_update = now

                # Check if enough tokens
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return

                # Wait for next token
                wait_time = (tokens - self.tokens) / self.config.messages_per_second
                await asyncio.sleep(wait_time)
