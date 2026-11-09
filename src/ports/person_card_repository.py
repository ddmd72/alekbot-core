from abc import ABC, abstractmethod
from typing import List, Sequence

from ..domain.person_cards import PersonCard


class PersonCardRepository(ABC):
    """People cards, the retrieval index entries of docs/10_rfcs/FACT_PROFILE_RFC.md §7. A card is never a fact, never in
    a prompt, never read by the consolidator.

    Tenancy: the account is part of every card's key; one account never reads or replaces another's cards. Backend
    errors propagate.
    """

    @abstractmethod
    async def list_cards(self, account_id: str) -> List[PersonCard]:
        """Return every card of account `account_id`.

        Order: none (the caller sorts). A document that does not parse is skipped and logged.
        Tenancy: only that account's documents (the stored `account_id`).
        """

    @abstractmethod
    async def replace_cards(self, account_id: str, cards: Sequence[PersonCard]) -> None:
        """Make `cards` the account's whole set: write each (overwriting the card of the same key) and delete every other
        card of the account.

        Atomicity: none across the whole set; writes and deletes go in batches of at most 500 operations, so an error
            can leave a mix of the old and the new set until the next rebuild, which converges.
        Tenancy: touches only documents whose stored `account_id` is `account_id`.
        """
