"""EmailMemoryBatches — the stored mail Stage 3 of consolidation reads, with each mail's card fields (email index v2
§6.7, slice 9)."""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Sequence

from ..domain.email_memory_mail import MemoryMail


class EmailMemoryBatches(ABC):
    """Tenancy: `user_id` scopes every read. Dates come back as the v1 repository returns them (naive, UTC)."""

    @abstractmethod
    async def memory_batch(self, user_id: str, limit: int, settled: Sequence[str],
                           stale_before: datetime) -> List[MemoryMail]:
        """Unconsolidated mail, oldest `indexed_at` first, each once, at most `limit`: every mail whose `card_state` is
        in `settled`, plus every one indexed before `stale_before` (whatever its card). A consolidated mail never
        returns: a later card write does not clear the mark. Backend errors propagate."""
