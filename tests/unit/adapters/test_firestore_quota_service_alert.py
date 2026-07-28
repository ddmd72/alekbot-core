"""Daily budget alert in FirestoreQuotaService.

Advisory only: crossing the account's daily cost limit posts to the ops channel and
execution continues. Added 2026-07-28 — the account carried an unenforced quota
(check_quota had no callers) while a runaway reminder fan-out went unnoticed for a day.
"""

from unittest.mock import AsyncMock

import pytest

from src.adapters.firestore_quota_service import FirestoreQuotaService
from src.domain.billing import UsageIncrement
from src.ports.alert_sink import AlertSinkPort


def _increment(before: float, after: float, limit: float = 5.0) -> UsageIncrement:
    return UsageIncrement(
        daily_cost_before=before, daily_cost_after=after, daily_cost_limit=limit
    )


def _service(increment: UsageIncrement, sink=None):
    repo = AsyncMock()
    repo.increment_account_usage.return_value = increment
    return FirestoreQuotaService(repo, alert_sink=sink), repo


class TestDailyBudgetAlert:

    async def test_posts_alert_when_limit_crossed(self):
        sink = AsyncMock(spec=AlertSinkPort)
        svc, _ = _service(_increment(4.80, 5.20), sink)

        await svc.record_usage("acct-1", "gpt-5.6-sol", tokens=1000, cost=0.40)

        sink.post.assert_awaited_once()
        text = sink.post.await_args.args[0]
        assert "acct-1" in text
        assert "5.20" in text
        assert "5.00" in text

    async def test_no_alert_below_limit(self):
        sink = AsyncMock(spec=AlertSinkPort)
        svc, _ = _service(_increment(1.00, 1.40), sink)

        await svc.record_usage("acct-1", "gpt-5.6-luna", tokens=1000, cost=0.40)

        sink.post.assert_not_awaited()

    async def test_no_repeat_alert_once_already_over(self):
        """The crossing already happened earlier today — stay quiet."""
        sink = AsyncMock(spec=AlertSinkPort)
        svc, _ = _service(_increment(9.00, 9.40), sink)

        await svc.record_usage("acct-1", "gpt-5.6-sol", tokens=1000, cost=0.40)

        sink.post.assert_not_awaited()

    async def test_usage_still_recorded_when_alert_fires(self):
        """The alert is a side effect — it must not displace the billing write."""
        sink = AsyncMock(spec=AlertSinkPort)
        svc, repo = _service(_increment(4.80, 5.20), sink)

        await svc.record_usage("acct-1", "gpt-5.6-sol", tokens=1000, cost=0.40)

        repo.increment_account_usage.assert_awaited_once_with("acct-1", 1000, 0.40)

    async def test_alert_failure_is_swallowed(self):
        """A dead webhook must not raise into the agent's response path."""
        sink = AsyncMock(spec=AlertSinkPort)
        sink.post.side_effect = RuntimeError("slack down")
        svc, _ = _service(_increment(4.80, 5.20), sink)

        await svc.record_usage("acct-1", "gpt-5.6-sol", tokens=1000, cost=0.40)

        sink.post.assert_awaited_once()

    async def test_no_sink_configured_is_a_noop(self):
        """Alerting is optional — BILLING_SLACK_WEBHOOK_URL may be unset."""
        svc, repo = _service(_increment(4.80, 5.20), sink=None)

        await svc.record_usage("acct-1", "gpt-5.6-sol", tokens=1000, cost=0.40)

        repo.increment_account_usage.assert_awaited_once()

    async def test_no_alert_when_the_write_failed(self):
        """A failed increment means we know nothing about today's spend."""
        sink = AsyncMock(spec=AlertSinkPort)
        repo = AsyncMock()
        repo.increment_account_usage.side_effect = RuntimeError("firestore down")
        svc = FirestoreQuotaService(repo, alert_sink=sink)

        await svc.record_usage("acct-1", "gpt-5.6-sol", tokens=1000, cost=0.40)

        sink.post.assert_not_awaited()

    async def test_alert_logs_structured_event(self, caplog):
        svc, _ = _service(_increment(4.80, 5.20), AsyncMock(spec=AlertSinkPort))

        with caplog.at_level("WARNING"):
            await svc.record_usage("acct-1", "gpt-5.6-sol", tokens=1000, cost=0.40)

        assert "budget_daily_limit_crossed" in caplog.text


class TestAlertSinkPortContract:

    def test_post_is_abstract(self):
        assert getattr(AlertSinkPort.post, "__isabstractmethod__", False)

    def test_cannot_instantiate_port(self):
        with pytest.raises(TypeError):
            AlertSinkPort()

    def test_slack_webhook_adapter_implements_the_port(self):
        from src.adapters.slack.webhook_adapter import SlackWebhookAdapter

        assert issubclass(SlackWebhookAdapter, AlertSinkPort)
        assert isinstance(SlackWebhookAdapter("https://example.invalid"), AlertSinkPort)
