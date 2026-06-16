from ..ports.quota_service import QuotaService
from ..ports.account_repository import AccountRepository
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
    """

    def __init__(self, account_repo: AccountRepository):
        self.account_repo = account_repo

    async def record_usage(self, account_id: str, model: str, tokens: int, cost: float) -> None:
        """Record usage durably, awaited. Best-effort: repo errors are logged, not raised."""
        try:
            await self.account_repo.increment_account_usage(account_id, tokens, cost)
            logger.debug(f"📊 Usage recorded for account {account_id}: {tokens} tokens, ${cost:.6f}")
        except Exception as e:
            logger.error(f"❌ Failed to record usage for account {account_id}: {e}")
