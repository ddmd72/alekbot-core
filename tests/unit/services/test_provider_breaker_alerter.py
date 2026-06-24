"""Unit tests for ProviderBreakerAlerter.

Covers the sync→async bridge: on_open() is a sync breaker hook that must schedule
the async Slack post without awaiting, throttle repeated opens, and never raise
(it runs inside the LLM failover handler).
"""
import asyncio

import pytest

from src.services.provider_breaker_alerter import ProviderBreakerAlerter


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_delivers_alert_with_provider_name():
    delivered = []
    ready = asyncio.Event()

    async def alert_fn(text: str) -> None:
        delivered.append(text)
        ready.set()

    alerter = ProviderBreakerAlerter(alert_fn=alert_fn)
    alerter.on_open("claude")

    await asyncio.wait_for(ready.wait(), timeout=1.0)
    assert len(delivered) == 1
    assert "claude" in delivered[0]


@pytest.mark.asyncio
async def test_throttles_within_window_then_fires_after():
    clock = _Clock(1000.0)
    delivered = []
    ready = asyncio.Event()

    async def alert_fn(text: str) -> None:
        delivered.append(text)
        ready.set()

    alerter = ProviderBreakerAlerter(alert_fn=alert_fn, throttle_seconds=600.0, clock=clock)

    alerter.on_open("claude")
    await asyncio.wait_for(ready.wait(), timeout=1.0)
    assert len(delivered) == 1

    # Re-open within the throttle window → suppressed (no schedule).
    ready.clear()
    alerter.on_open("claude")
    await asyncio.sleep(0.02)
    assert len(delivered) == 1

    # Window elapsed → fires again.
    clock.now += 600.0
    alerter.on_open("claude")
    await asyncio.wait_for(ready.wait(), timeout=1.0)
    assert len(delivered) == 2


@pytest.mark.asyncio
async def test_throttle_is_per_provider():
    clock = _Clock(1000.0)
    delivered = []

    async def alert_fn(text: str) -> None:
        delivered.append(text)

    alerter = ProviderBreakerAlerter(alert_fn=alert_fn, throttle_seconds=600.0, clock=clock)
    alerter.on_open("claude")
    alerter.on_open("openai")
    await asyncio.sleep(0.02)
    assert len(delivered) == 2


@pytest.mark.asyncio
async def test_alert_fn_error_is_swallowed():
    done = asyncio.Event()

    async def alert_fn(text: str) -> None:
        done.set()
        raise RuntimeError("slack down")

    alerter = ProviderBreakerAlerter(alert_fn=alert_fn)
    alerter.on_open("claude")  # must not raise

    await asyncio.wait_for(done.wait(), timeout=1.0)
    await asyncio.sleep(0)  # let _deliver swallow the error


def test_on_open_without_running_loop_does_not_raise():
    """Sync context, no event loop — on_open must not raise (nothing to schedule)."""
    async def alert_fn(text: str) -> None:  # never awaited here
        pass

    alerter = ProviderBreakerAlerter(alert_fn=alert_fn)
    alerter.on_open("claude")  # no running loop → no schedule, no exception
