"""
FirestorePersonCardRepository — people cards (docs/10_rfcs/FACT_PROFILE_RFC.md §7).

Collection: EnvironmentConfig.person_cards_collection   doc id = {account_id}:{card_key}
  doc: account_id, card_key, display, spellings, name_keys, relation, fact_count, built_at
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from google.cloud.firestore import FieldFilter

from ..config.environment import EnvironmentConfig
from ..domain.person_cards import PersonCard
from ..ports.person_card_repository import PersonCardRepository
from ..utils.logger import logger

BATCH_SIZE = 500


class FirestorePersonCardRepository(PersonCardRepository):
    """Firestore mechanics behind the port's contract.

    One equality query on `account_id` serves the listing and the replacement, so no composite index is needed. The
    client is injected, so the `database=` choice stays with the caller.
    """

    def __init__(self, db_client: Any, env_config: EnvironmentConfig) -> None:
        self._db = db_client
        self._cards = db_client.collection(env_config.person_cards_collection)

    @staticmethod
    def doc_id(account_id: str, card_key: str) -> str:
        return f"{account_id}:{card_key}"

    async def list_cards(self, account_id: str) -> List[PersonCard]:
        cards: List[PersonCard] = []
        async for snap in self._account_query(account_id).stream():
            card = self._parse(snap.id, snap.to_dict() or {})
            if card is not None:
                cards.append(card)
        return cards

    async def replace_cards(self, account_id: str, cards: Sequence[PersonCard]) -> None:
        built_at = datetime.now(timezone.utc)
        keep = {self.doc_id(account_id, card.card_key) for card in cards}
        operations: List[Any] = []
        for card in cards:
            ref = self._cards.document(self.doc_id(account_id, card.card_key))
            operations.append(("set", ref, self._document(account_id, card, built_at)))
        async for snap in self._account_query(account_id).stream():
            if snap.id not in keep:
                operations.append(("delete", snap.reference, None))
        for start in range(0, len(operations), BATCH_SIZE):
            batch = self._db.batch()
            for kind, ref, data in operations[start:start + BATCH_SIZE]:
                if kind == "set":
                    batch.set(ref, data)
                else:
                    batch.delete(ref)
            await batch.commit()

    def _account_query(self, account_id: str) -> Any:
        return self._cards.where(filter=FieldFilter("account_id", "==", account_id))

    @staticmethod
    def _document(account_id: str, card: PersonCard, built_at: datetime) -> Dict[str, Any]:
        return {
            "account_id": account_id, "card_key": card.card_key, "display": card.display,
            "spellings": list(card.spellings), "name_keys": list(card.name_keys), "relation": card.relation,
            "fact_count": card.fact_count, "built_at": built_at,
        }

    @staticmethod
    def _parse(doc_id: str, data: Dict[str, Any]) -> Optional[PersonCard]:
        try:
            relation = data.get("relation")
            return PersonCard(
                card_key=str(data["card_key"]), display=str(data["display"]),
                spellings=tuple(str(x) for x in data["spellings"]), name_keys=tuple(str(x) for x in data["name_keys"]),
                relation=str(relation) if relation else None, fact_count=int(data["fact_count"]))
        except (KeyError, TypeError, ValueError):
            logger.warning(f"[PersonCards] card {doc_id[:8]} does not parse; skipped")
            return None
