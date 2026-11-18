"""The `billing_daily_summary` `/worker` task: yesterday's usage of every active account, posted as one alert."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Tuple

from ..ports.account_repository import AccountRepository
from ..ports.alert_sink import AlertSinkPort
from ..ports.user_repository import UserRepository
from ..utils.logger import logger


class BillingDailySummaryTask:
    """Aggregates the accounts with activity yesterday into one alert message; a `WorkerTask`."""

    def __init__(
        self,
        *,
        billing_webhook: AlertSinkPort,
        account_repo: AccountRepository,
        user_repo: UserRepository,
    ) -> None:
        self._billing_webhook = billing_webhook
        self._account_repo = account_repo
        self._user_repo = user_repo

    async def __call__(self, payload: dict[str, Any], headers: dict[str, Any]) -> Tuple[dict[str, Any], int]:
        """
        Send daily billing summary to a Slack channel via incoming webhook.
        Aggregates all accounts with activity today into one message.
        Called by Cloud Scheduler at 09:00 Europe/Madrid.
        """
        accounts = await self._account_repo.list_all_accounts()
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
        yesterday_str = yesterday.strftime("%Y-%m-%d")

        lines = [f"📊 *Billing Summary — {yesterday_str}*\n"]
        reported = 0

        for account in accounts:
            u = account.usage
            # Resolve "yesterday" against the calendar, not the raw snapshot: an idle
            # yesterday must report 0, never the last active day's leftover total.
            prev_tokens, prev_cost = u.usage_for_date(yesterday)
            if prev_tokens == 0 and prev_cost == 0.0:
                continue

            # The month of "yesterday", like the day: on the 1st the counters may or may not have rotated yet.
            month_tokens, month_cost = u.usage_for_month(yesterday)

            owner_id = next(
                (uid for uid, role in account.iam_policy.items() if role == "owner"),
                None,
            )
            profile = await self._user_repo.get_user(owner_id) if owner_id else None
            name = profile.display_name if profile else account.account_id[:8]

            lines.append(
                f"*{name}*\n"
                f"  Yesterday: {prev_tokens:,} tok / ${prev_cost:.4f}\n"
                f"  Month:     {month_tokens:,} tok / ${month_cost:.4f}\n"
                f"  Total:     {u.total_tokens:,} tok / ${u.total_cost:.4f}"
            )
            reported += 1

        if reported == 0:
            logger.info("[Worker] billing_daily_summary: no accounts with activity yesterday")
            return {"reported": 0}, 200

        await self._billing_webhook.post("\n\n".join(lines))
        logger.info(f"[Worker] billing_daily_summary: reported {reported} accounts")
        return {"reported": reported}, 200
