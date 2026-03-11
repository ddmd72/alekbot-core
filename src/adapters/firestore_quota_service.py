import asyncio
from ..ports.quota_service import QuotaService
from ..ports.user_repository import UserRepository
from ..utils.logger import logger

class FirestoreQuotaService(QuotaService):
    """
    Firestore implementation of QuotaService.
    Delegates actual storage updates to UserRepository (which cascades to AccountRepository).
    Implements fire-and-forget pattern via asyncio.create_task.
    """

    def __init__(self, user_repo: UserRepository):
        self.user_repo = user_repo

    async def record_usage(self, user_id: str, model: str, tokens: int, cost: float) -> None:
        """
        Records usage asynchronously without blocking the caller.
        """
        try:
            # Schedule the update as a background task
            asyncio.create_task(self._record_usage_impl(user_id, tokens, cost))
        except Exception as e:
            # Fallback logging if task creation fails (unlikely)
            logger.error(f"Failed to schedule usage recording for user {user_id}: {e}")

    async def _record_usage_impl(self, user_id: str, tokens: int, cost: float) -> None:
        try:
            await self.user_repo.increment_usage(user_id, tokens, cost)
            logger.debug(f"📊 Usage recorded for {user_id}: {tokens} tokens, ${cost:.6f}")
        except Exception as e:
            logger.error(f"❌ Failed to record usage for user {user_id}: {e}")
