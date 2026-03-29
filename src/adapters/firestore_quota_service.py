import asyncio
from ..ports.quota_service import QuotaService
from ..ports.account_repository import AccountRepository
from ..utils.logger import logger


class FirestoreQuotaService(QuotaService):
    """
    Firestore implementation of QuotaService.
    Writes usage directly to the account — no user→account indirection.
    Implements fire-and-forget pattern via asyncio.create_task.
    """

    def __init__(self, account_repo: AccountRepository):
        self.account_repo = account_repo

    async def record_usage(self, account_id: str, model: str, tokens: int, cost: float) -> None:
        """
        Records usage asynchronously without blocking the caller.
        """
        try:
            asyncio.create_task(self._record_usage_impl(account_id, tokens, cost))
        except Exception as e:
            logger.error(f"Failed to schedule usage recording for account {account_id}: {e}")

    async def _record_usage_impl(self, account_id: str, tokens: int, cost: float) -> None:
        try:
            await self.account_repo.increment_account_usage(account_id, tokens, cost)
            logger.debug(f"📊 Usage recorded for account {account_id}: {tokens} tokens, ${cost:.6f}")
        except Exception as e:
            logger.error(f"❌ Failed to record usage for account {account_id}: {e}")
