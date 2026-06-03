from abc import ABC, abstractmethod

class QuotaService(ABC):
    """
    Interface for usage tracking and quota management.
    Designed to be non-blocking and fail-open.
    """

    @abstractmethod
    async def record_usage(self, account_id: str, model: str, tokens: int, cost: float) -> None:
        """
        Records token usage and cost for an account.
        Should be implemented as a fire-and-forget operation to avoid blocking the user path.
        """
        pass
