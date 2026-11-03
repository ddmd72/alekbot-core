"""Mutation P3: a failing alert sink is logged as an error and never raised."""
import logging
from types import SimpleNamespace

from src.adapters.firestore_quota_service import FirestoreQuotaService
from src.ports.alert_sink import AlertSinkPort


class _FailingSink(AlertSinkPort):
    async def post(self, text: str) -> None:
        raise RuntimeError("webhook unavailable")


async def test_failed_alert_delivery_is_logged_as_error(caplog):
    quota = FirestoreQuotaService(account_repo=None, alert_sink=_FailingSink())
    increment = SimpleNamespace(daily_cost_after=5.0, daily_cost_limit=1.0)
    with caplog.at_level(logging.ERROR):
        await quota._alert_daily_limit_crossed("acc-1", increment)
    assert "Failed to deliver budget alert for account acc-1" in caplog.text
