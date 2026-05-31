"""Unit tests for the shared retry_async executor."""

from unittest.mock import AsyncMock, patch

import pytest

from src.domain.retry_policy import RetryPolicy
from src.utils.retry import retry_async


class _Boom(Exception):
    pass


class _Other(Exception):
    pass


_POLICY = RetryPolicy(transient_max_attempts=3, transient_backoff_base_seconds=2.0, transient_jitter_seconds=0.0)


@pytest.mark.asyncio
async def test_returns_immediately_on_success():
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        return "ok"

    result = await retry_async(fn, policy=_POLICY, retryable=(_Boom,))

    assert result == "ok"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_retries_retryable_then_succeeds():
    seq = [_Boom(), _Boom(), "ok"]

    async def fn():
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    with patch("src.utils.retry.asyncio.sleep", new=AsyncMock()) as sleep:
        result = await retry_async(fn, policy=_POLICY, retryable=(_Boom,))

    assert result == "ok"
    assert sleep.await_count == 2  # two retries before the third (successful) attempt


@pytest.mark.asyncio
async def test_exhausts_then_raises_last_error():
    async def fn():
        raise _Boom("still failing")

    with patch("src.utils.retry.asyncio.sleep", new=AsyncMock()) as sleep:
        with pytest.raises(_Boom):
            await retry_async(fn, policy=_POLICY, retryable=(_Boom,))

    # 4 total attempts (1 + 3 retries) → 3 sleeps between them.
    assert sleep.await_count == 3


@pytest.mark.asyncio
async def test_non_retryable_propagates_immediately():
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        raise _Other("deterministic")

    with patch("src.utils.retry.asyncio.sleep", new=AsyncMock()) as sleep:
        with pytest.raises(_Other):
            await retry_async(fn, policy=_POLICY, retryable=(_Boom,))

    assert calls["n"] == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_retry_policy_makes_single_attempt():
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        raise _Boom()

    policy = RetryPolicy(transient_max_attempts=0)
    with patch("src.utils.retry.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(_Boom):
            await retry_async(fn, policy=policy, retryable=(_Boom,))

    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_exponential_backoff_schedule():
    async def fn():
        raise _Boom()

    sleeps = []
    async def fake_sleep(s):
        sleeps.append(s)

    with patch("src.utils.retry.asyncio.sleep", new=fake_sleep):
        with pytest.raises(_Boom):
            await retry_async(fn, policy=_POLICY, retryable=(_Boom,))

    # base=2.0, jitter=0 → 2, 4, 8
    assert sleeps == [2.0, 4.0, 8.0]


@pytest.mark.asyncio
async def test_on_retry_callback_invoked_per_retry():
    async def fn():
        raise _Boom()

    seen = []
    with patch("src.utils.retry.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(_Boom):
            await retry_async(
                fn,
                policy=_POLICY,
                retryable=(_Boom,),
                on_retry=lambda e, attempt, backoff: seen.append((attempt, backoff)),
            )

    assert [a for a, _ in seen] == [1, 2, 3]
