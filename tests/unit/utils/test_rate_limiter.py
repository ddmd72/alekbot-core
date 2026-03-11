"""Tests for rate limiter."""
import pytest
import time
from src.utils.rate_limiter import RateLimiter, RateLimitConfig


@pytest.mark.asyncio
async def test_rate_limiter_allows_burst():
    """Test that burst is allowed up to burst_size."""
    config = RateLimitConfig(messages_per_second=1.0, burst_size=5)
    limiter = RateLimiter(config)

    start = time.time()
    for _ in range(5):
        await limiter.acquire()
    elapsed = time.time() - start

    assert elapsed < 0.1  # Should be instant (burst)


@pytest.mark.asyncio
async def test_rate_limiter_enforces_limit():
    """Test that rate limit is enforced after burst."""
    config = RateLimitConfig(messages_per_second=10.0, burst_size=5)
    limiter = RateLimiter(config)

    start = time.time()
    for _ in range(15):  # 5 burst + 10 rate-limited
        await limiter.acquire()
    elapsed = time.time() - start

    # Should take ~1 second (10 messages after burst at 10 msg/s)
    assert 0.9 < elapsed < 1.2


@pytest.mark.asyncio
async def test_telegram_config():
    """Test Telegram-specific config."""
    config = RateLimitConfig.for_telegram()
    assert config.messages_per_second == 20.0
    assert config.burst_size == 30


@pytest.mark.asyncio
async def test_slack_config():
    """Test Slack-specific config."""
    config = RateLimitConfig.for_slack()
    assert config.messages_per_second == 1.0
    assert config.burst_size == 5
