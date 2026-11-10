from abc import ABC, abstractmethod
from typing import Optional

from ..domain.confirmed_people import ConfirmedPeople


class ConfirmedPeopleRepository(ABC):
    """The owner-confirmed people list (docs/10_rfcs/FACT_PROFILE_RFC.md §4.5). Written only from the owner's
    confirmation; never by the profiler, the consolidator or the review pass.

    Tenancy: the document id is the account id; one account never reads or replaces another's list. Backend errors
    propagate.
    """

    @abstractmethod
    async def get(self, account_id: str) -> Optional[ConfirmedPeople]:
        """The account's list; None when it has none. A stored document that does not parse raises ValueError
        (never None: an unreadable list is not an empty one)."""

    @abstractmethod
    async def replace(self, account_id: str, people: ConfirmedPeople, source: str) -> None:
        """Make `people` the account's whole list (one set of the one document) with `source` and the time of the
        write. `source` is "owner_script" or "review_answer"; anything else raises ValueError before any I/O."""
