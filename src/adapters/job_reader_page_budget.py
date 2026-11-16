"""In-memory page budget of the email scripts' paid scan reader (email index v2, slice 5).

A script run books pages against a fixed page cap and, optionally, a dollar `gate` (`gate(pages)` answers whether the
run's money cap still allows the booking). One lock serialises bookings, so concurrent mails cannot overbook. It counts
`used` (pages booked), `asked` (bookings asked) and `asked_pages` (pages asked, booked or not): a dry run reads these
to project the cost of a real run.
"""
import asyncio
from typing import Awaitable, Callable, Optional

from ..ports.reader_page_budget import ReaderPageBudget


class JobReaderPageBudget(ReaderPageBudget):

    def __init__(self, max_pages: int, gate: Optional[Callable[[int], Awaitable[bool]]] = None) -> None:
        self._max = max_pages
        self._gate = gate
        self._lock = asyncio.Lock()
        self.used = 0
        self.asked = 0
        self.asked_pages = 0

    async def reserve(self, user_id: str, pages: int) -> bool:
        async with self._lock:
            self.asked += 1
            self.asked_pages += pages
            if pages <= 0 or self.used + pages > self._max:
                return False
            if self._gate is not None and not await self._gate(pages):
                return False
            self.used += pages
            return True
