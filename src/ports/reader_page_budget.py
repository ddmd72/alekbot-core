"""
ReaderPageBudget — a cap on the pages the paid document reader may read for the email index (email index v2, slice 5).

The attachment service asks before it hands a scan to the reader. One implementation online (a per-user daily counter
in Firestore) and one in the scripts (a per-run counter with a dollar gate), so the port earns its place by
substitution.
"""
from abc import ABC, abstractmethod


class ReaderPageBudget(ABC):

    @abstractmethod
    async def reserve(self, user_id: str, pages: int) -> bool:
        """True when `pages` are booked and may be read now; False when the budget refuses. Never raises: a failure of
        the budget's own store answers False (fail closed: the scan stays `scan_not_read`)."""
