from typing import Optional

from ..ports.account_repository import AccountRepository
from ..ports.alert_sink import AlertSinkPort
from ..ports.quota_service import QuotaService
from ..utils.logger import logger


class FirestoreQuotaService(QuotaService):
    """
    Firestore implementation of QuotaService.
    Writes usage directly to the account — no user→account indirection.

    The write is awaited (not detached): callers invoke record_usage at the end of
    request handling so the Firestore write completes while the request still holds
    CPU. A task detached past the request boundary is starved by Cloud Run CPU
    throttling and lost on instance recycle. Errors are swallowed and logged — billing
    is best-effort and must never break the caller's response path.

    Budget alerting is advisory only: crossing the account's daily cost limit posts to
    the ops channel and nothing else. There is no gate here — the hard monthly cap was
    dropped 2026-07-26, and a miscounted quota that silently disables the bot is a worse
    failure than an overspent day.
    """

    def __init__(
        self,
        account_repo: AccountRepository,
        alert_sink: Optional[AlertSinkPort] = None,
    ):
        self.account_repo = account_repo
        self.alert_sink = alert_sink

    async def record_usage(self, account_id: str, model: str, tokens: int, cost: float) -> None:
        """Record usage durably, awaited. Best-effort: repo errors are logged, not raised."""
        try:
            increment = await self.account_repo.increment_account_usage(account_id, tokens, cost)
            logger.debug(f"📊 Usage recorded for account {account_id}: {tokens} tokens, ${cost:.6f}")
        except Exception as e:
            logger.error(f"❌ Failed to record usage for account {account_id}: {e}")
            return

        if increment is not None and increment.crossed_daily_limit:
            await self._alert_daily_limit_crossed(account_id, increment)

    async def _alert_daily_limit_crossed(self, account_id: str, increment) -> None:
        """Announce the crossing once. Never raises — an alert must not break billing."""
        message = (
            f"💸 *Daily budget exceeded* — account `{account_id}`\n"
            f"  Spend today: ${increment.daily_cost_after:.4f} "
            f"(limit ${increment.daily_cost_limit:.2f})\n"
            f"  Execution continues — this is an alert, not a cap."
        )
        logger.warning(
            "budget_daily_limit_crossed account=%s spend=%.4f limit=%.2f",
            account_id, increment.daily_cost_after, increment.daily_cost_limit,
            extra={
                "event": "budget_daily_limit_crossed",
                "account_id": account_id,
                "daily_cost": increment.daily_cost_after,
                "daily_cost_limit": increment.daily_cost_limit,
            },
        )
        if self.alert_sink is None:
            return
        try:
            await self.alert_sink.post(message)
        except Exception as e:
            logger.error(f"❌ Failed to deliver budget alert for account {account_id}: {e}")
