"""ProviderBreakerAlerter — escalates a tripped provider circuit breaker to Slack.

Bridges the breaker's sync ``on_open`` hook to the async alert sink. The breaker
is a pure sync state machine called from the hot-path failure handler, so this
must return immediately: it schedules the async post on the running loop
(fire-and-forget) rather than awaiting it.

Why this exists: provider failover keeps the user served on transient faults, but
a failed-over request is otherwise silent — a provider that is *chronically* down
would be masked forever with no human signal. The breaker tripping is exactly the
"transient became chronic" discriminator; this turns that transition into an
operator alert. The per-occurrence path (deterministic 4xx) is already covered by
``AlertingLLMProxy``; this covers the failover bucket.

Throttle: repeated opens for the same provider (a sustained outage re-opens after
each HALF-OPEN cooldown) are suppressed within ``throttle_seconds`` so Slack is
not flooded. In-process state — fine for the single-instance Cloud Run deployment.
"""

import asyncio
import time
from typing import Awaitable, Callable, Dict

from ..utils.logger import logger

_ALERT_SEND_TIMEOUT_SECONDS = 5.0


class ProviderBreakerAlerter:
    """Throttled Slack alert on each provider circuit-breaker CLOSED→OPEN trip."""

    def __init__(
        self,
        alert_fn: Callable[[str], Awaitable[None]],
        throttle_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._alert_fn = alert_fn
        self._throttle_seconds = throttle_seconds
        self._clock = clock
        self._last_alert: Dict[str, float] = {}

    def on_open(self, provider_name: str) -> None:
        """Sync breaker hook: throttle, then schedule the async alert.

        Must never raise — it runs inside ``record_failure`` in the LLM failover
        handler, where an exception would mask the original provider failure.
        """
        try:
            now = self._clock()
            last = self._last_alert.get(provider_name)
            if last is not None and (now - last) < self._throttle_seconds:
                return
            self._last_alert[provider_name] = now

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No event loop (e.g. a sync test or non-async caller). Nothing
                # to schedule onto; the transition is already logged below.
                loop = None

            logger.warning(
                f"provider_breaker_open provider={provider_name} — failover engaged; "
                f"chronic failures, check logs (event=llm_fallback) for the cause"
            )
            if loop is not None:
                loop.create_task(self._deliver(provider_name))
        except Exception as exc:  # noqa: BLE001 — escalation must never break failover
            logger.error(f"[ProviderBreakerAlerter] on_open failed: {exc}")

    async def _deliver(self, provider_name: str) -> None:
        text = (
            f"🚨 Provider circuit breaker OPEN — provider={provider_name}\n"
            f"Repeated LLM failures tripped the breaker; requests are failing over "
            f"to the fallback provider. This is chronic, not a blip — investigate.\n"
            f"Cause: filter Cloud Run logs by event=llm_fallback / event=llm_both_open "
            f"for the underlying error_type + http_status."
        )
        try:
            await asyncio.wait_for(self._alert_fn(text), timeout=_ALERT_SEND_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 — alerting is best-effort
            logger.error(f"[ProviderBreakerAlerter] alert delivery failed: {exc}")
