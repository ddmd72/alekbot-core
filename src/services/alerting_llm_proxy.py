"""Transparent LLM proxy that pushes a Slack alert on deterministic client errors.

Agents receive this proxy (wrapped around the provider, outside CachingLLMProxy)
via AgentExecutionContext. It forwards every call to the inner provider and, when
the provider raises ``LLMClientError`` (any 4xx — provider credit/billing
exhaustion, malformed request, content-policy rejection), fires an operator alert
before re-raising the original error unchanged.

Why here and not in an agent: ``LLMClientError`` is non-retryable and non-failover
(see domain/exceptions.py), so by the time an agent turns it into an
``AgentResponse.failure()`` the signal is gone — and background tasks
(consolidation, daily email review, reminders) fail with no human watching. This
proxy is the single chokepoint every agent's LLM call passes through, so one hook
covers them all.

Throttle: identical alerts (keyed by HTTP status) are suppressed within
``throttle_seconds`` so a provider outage hitting many background tasks does not
flood Slack. In-process state — fine for the single-instance Cloud Run deployment.
"""

import asyncio
from typing import Awaitable, Callable, Dict, Optional
import time

from ..domain.exceptions import LLMClientError
from ..domain.user import PerformanceTier
from ..ports.llm_port import (
    LLMPort,
    LLMRequest,
    LLMResponse,
    MessagePart,
    ProviderCapabilities,
)
from ..utils.logger import logger

_ALERT_SEND_TIMEOUT_SECONDS = 5.0


class AlertingLLMProxy(LLMPort):
    """Transparent proxy that alerts on ``LLMClientError`` (4xx) and re-raises it."""

    def __init__(
        self,
        inner: LLMPort,
        alert_fn: Callable[[str], Awaitable[None]],
        throttle_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._inner = inner
        self._alert_fn = alert_fn
        self._throttle_seconds = throttle_seconds
        self._clock = clock
        self._last_alert: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def generate_content(self, request: LLMRequest) -> LLMResponse:
        try:
            return await self._inner.generate_content(request=request)
        except LLMClientError as e:
            await self._maybe_alert(e, request)
            raise

    async def _maybe_alert(self, error: LLMClientError, request: LLMRequest) -> None:
        """Send an alert unless an identical one fired within the throttle window.

        Any failure to deliver the alert is swallowed — alerting must never mask or
        replace the original LLMClientError that the caller is about to receive.
        """
        key = str(error.http_status)
        now = self._clock()
        async with self._lock:
            last = self._last_alert.get(key)
            if last is not None and (now - last) < self._throttle_seconds:
                return
            self._last_alert[key] = now

        model = getattr(request, "model_name", None) or "unknown"
        text = (
            f"🚨 LLM client error (HTTP {error.http_status}) — model={model}\n"
            f"{error}"
        )
        try:
            await asyncio.wait_for(self._alert_fn(text), timeout=_ALERT_SEND_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 — alerting is best-effort
            logger.error(f"[AlertingLLMProxy] alert delivery failed: {exc}")

    def supports_caching(self) -> bool:
        return self._inner.supports_caching()

    async def upload_file(self, path: str, mime_type: str) -> MessagePart:
        return await self._inner.upload_file(path, mime_type)

    def get_capabilities(self) -> ProviderCapabilities:
        return self._inner.get_capabilities()

    def get_model_for_tier(self, tier: PerformanceTier) -> str:
        return self._inner.get_model_for_tier(tier)
