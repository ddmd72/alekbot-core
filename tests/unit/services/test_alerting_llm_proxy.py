"""Unit tests for AlertingLLMProxy.

The proxy wraps an LLMPort, fires an operator alert on LLMClientError (4xx), and
re-raises the original error unchanged. Other errors and successes pass through.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.exceptions import LLMClientError, LLMRateLimitError
from src.ports.llm_port import LLMPort
from src.services.alerting_llm_proxy import AlertingLLMProxy


def _request(model_name: str = "claude-sonnet-4-6"):
    req = MagicMock()
    req.model_name = model_name
    return req


def _make(inner_side_effect=None, inner_return=None, clock=None):
    inner = AsyncMock(spec=LLMPort)
    if inner_side_effect is not None:
        inner.generate_content.side_effect = inner_side_effect
    else:
        inner.generate_content.return_value = inner_return
    alert_fn = AsyncMock(return_value=None)
    kwargs = {"throttle_seconds": 600.0}
    if clock is not None:
        kwargs["clock"] = clock
    proxy = AlertingLLMProxy(inner, alert_fn=alert_fn, **kwargs)
    return proxy, inner, alert_fn


class TestPassThrough:
    @pytest.mark.asyncio
    async def test_success_delegates_and_no_alert(self):
        sentinel = object()
        proxy, inner, alert_fn = _make(inner_return=sentinel)

        result = await proxy.generate_content(_request())

        assert result is sentinel
        alert_fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_client_error_not_alerted(self):
        proxy, inner, alert_fn = _make(
            inner_side_effect=LLMRateLimitError("429", http_status=429)
        )

        with pytest.raises(LLMRateLimitError):
            await proxy.generate_content(_request())
        alert_fn.assert_not_awaited()


class TestAlertOnClientError:
    @pytest.mark.asyncio
    async def test_client_error_alerts_then_reraises(self):
        proxy, inner, alert_fn = _make(
            inner_side_effect=LLMClientError("credit balance too low", http_status=400)
        )

        with pytest.raises(LLMClientError):
            await proxy.generate_content(_request("claude-sonnet-4-6"))

        alert_fn.assert_awaited_once()
        text = alert_fn.await_args[0][0]
        assert "400" in text
        assert "claude-sonnet-4-6" in text
        assert "credit balance too low" in text

    @pytest.mark.asyncio
    async def test_alert_delivery_failure_is_swallowed(self):
        proxy, inner, alert_fn = _make(
            inner_side_effect=LLMClientError("bad request", http_status=400)
        )
        alert_fn.side_effect = RuntimeError("slack down")

        # Original error must still surface; alert failure must not mask it.
        with pytest.raises(LLMClientError):
            await proxy.generate_content(_request())


class TestThrottle:
    @pytest.mark.asyncio
    async def test_same_status_throttled_within_window(self):
        now = [1000.0]
        proxy, inner, alert_fn = _make(
            inner_side_effect=LLMClientError("400", http_status=400),
            clock=lambda: now[0],
        )

        for _ in range(3):
            with pytest.raises(LLMClientError):
                await proxy.generate_content(_request())

        alert_fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_alert_again_after_window(self):
        now = [1000.0]
        proxy, inner, alert_fn = _make(
            inner_side_effect=LLMClientError("400", http_status=400),
            clock=lambda: now[0],
        )

        with pytest.raises(LLMClientError):
            await proxy.generate_content(_request())
        now[0] += 601  # past the 600s window
        with pytest.raises(LLMClientError):
            await proxy.generate_content(_request())

        assert alert_fn.await_count == 2

    @pytest.mark.asyncio
    async def test_distinct_status_not_throttled_together(self):
        now = [1000.0]
        inner = AsyncMock(spec=LLMPort)
        alert_fn = AsyncMock(return_value=None)
        proxy = AlertingLLMProxy(inner, alert_fn=alert_fn, throttle_seconds=600.0, clock=lambda: now[0])

        inner.generate_content.side_effect = LLMClientError("400", http_status=400)
        with pytest.raises(LLMClientError):
            await proxy.generate_content(_request())

        inner.generate_content.side_effect = LLMClientError("402", http_status=402)
        with pytest.raises(LLMClientError):
            await proxy.generate_content(_request())

        assert alert_fn.await_count == 2


class TestDelegation:
    def test_supports_caching_delegates(self):
        inner = AsyncMock(spec=LLMPort)
        inner.supports_caching = MagicMock(return_value=True)
        proxy = AlertingLLMProxy(inner, alert_fn=AsyncMock())
        assert proxy.supports_caching() is True

    def test_get_capabilities_delegates(self):
        inner = AsyncMock(spec=LLMPort)
        caps = object()
        inner.get_capabilities = MagicMock(return_value=caps)
        proxy = AlertingLLMProxy(inner, alert_fn=AsyncMock())
        assert proxy.get_capabilities() is caps

    @pytest.mark.asyncio
    async def test_upload_file_delegates(self):
        inner = AsyncMock(spec=LLMPort)
        part = object()
        inner.upload_file = AsyncMock(return_value=part)
        proxy = AlertingLLMProxy(inner, alert_fn=AsyncMock())
        assert await proxy.upload_file("/p", "image/png") is part
