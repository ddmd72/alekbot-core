from abc import ABC, abstractmethod
from typing import Optional

class QuotaService(ABC):
    """
    Interface for usage tracking and quota management.
    Designed to be non-blocking and fail-open.
    """

    @abstractmethod
    async def record_usage(self, user_id: str, model: str, tokens: int, cost: float) -> None:
        """
        Records token usage and cost for a user.
        Should be implemented as a fire-and-forget operation to avoid blocking the user path.
        """
        pass
