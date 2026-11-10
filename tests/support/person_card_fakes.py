"""An in-memory people-card store (`PersonCardRepository`) for the fact search tests: holds a fixed set of cards per
account and counts the reads."""
from typing import Dict, List, Sequence

from src.domain.person_cards import PersonCard
from src.ports.person_card_repository import PersonCardRepository


class HeldCards(PersonCardRepository):
    def __init__(self, *cards: PersonCard, account_id: str = "acc-1") -> None:
        self.held: Dict[str, List[PersonCard]] = {account_id: list(cards)}
        self.reads: List[str] = []

    async def list_cards(self, account_id: str) -> List[PersonCard]:
        self.reads.append(account_id)
        return list(self.held.get(account_id, []))

    async def replace_cards(self, account_id: str, cards: Sequence[PersonCard]) -> None:
        self.held[account_id] = list(cards)
