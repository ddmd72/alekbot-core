"""An in-memory `EmailCardStore` with the port's semantics: documents exist only when seeded (the card path never creates
one), a write compares the stored `card_version` and `people_digest` with the expectation in one step (no await between
the read and the write), only `CARD_FIELDS` are written, and `card_vector` is set from the vector or removed."""
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from src.domain.email_card_lifecycle import CARD_FIELDS, CardExpect, CardWriteResult
from src.ports.email_card_store import EmailCardStore


class FakeEmailCardStore(EmailCardStore):
    def __init__(self) -> None:
        self.docs: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def seed(self, user_id: str, email_id: str, **v1_fields: Any) -> None:
        """A stored v1 document (what the indexer saved before the card step)."""
        self.docs[(user_id, email_id)] = dict(v1_fields)

    async def read_expect(self, user_id: str, email_ids: Sequence[str]) -> Dict[str, CardExpect]:
        found: Dict[str, CardExpect] = {}
        for email_id in email_ids:
            doc = self.docs.get((user_id, email_id))
            if doc is not None:
                found[email_id] = CardExpect(doc.get("card_version"), doc.get("people_digest"))
        return found

    async def write_card(self, user_id: str, email_id: str, fields: Mapping[str, Any],
                         vector: Optional[Sequence[float]], expect: CardExpect) -> CardWriteResult:
        outside = sorted(set(fields) - set(CARD_FIELDS))
        if outside or "card_vector" in fields:
            raise ValueError(f"not writable by the card path: {outside or ['card_vector']}")
        doc = self.docs.get((user_id, email_id))
        if doc is None:
            return CardWriteResult.MISSING
        if (doc.get("card_version"), doc.get("people_digest")) != (expect.card_version, expect.people_digest):
            return CardWriteResult.CONFLICT
        doc.update(fields)
        if vector is None:
            doc.pop("card_vector", None)
        else:
            doc["card_vector"] = list(vector)
        return CardWriteResult.WRITTEN
