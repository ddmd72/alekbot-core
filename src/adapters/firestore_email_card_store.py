"""Firestore storage of the v2 email card: extra fields on the mail's v1 document (email index v2 §4, §6.3).

The collection and the document id (`{user_id}_{email_id}`) are the v1 repository's. A card write is one transaction
over the one document: read the stored `card_version` and `people_digest`, compare them with what the writer read
before deciding, update only the card fields. The document is never created here and no v1 field is named, so a v1
save, search or consolidation mark is unaffected.
"""
from typing import Any, Dict, Mapping, Optional, Sequence

from google.cloud import firestore
from google.cloud.firestore_v1.vector import Vector

from ..config.environment import EnvironmentConfig
from ..domain.email_card_lifecycle import CARD_FIELDS, CardExpect, CardWriteResult
from ..ports.email_card_store import EmailCardStore
from ..utils.logger import logger

_CHUNK = 500


class FirestoreEmailCardStore(EmailCardStore):

    def __init__(self, db_client, env_config: EnvironmentConfig) -> None:
        self._db = db_client
        self._col = db_client.collection(env_config.domain_email_facts_collection)

    async def read_expect(self, user_id: str, email_ids: Sequence[str]) -> Dict[str, CardExpect]:
        prefix = f"{user_id}_"
        found: Dict[str, CardExpect] = {}
        ids = list(email_ids)
        for i in range(0, len(ids), _CHUNK):
            refs = [self._col.document(f"{prefix}{e}") for e in ids[i:i + _CHUNK]]
            async for snap in self._db.get_all(refs, field_paths=["card_version", "people_digest"]):
                if snap.exists:
                    data = snap.to_dict() or {}
                    found[snap.id[len(prefix):]] = CardExpect(data.get("card_version"), data.get("people_digest"))
        return found

    async def write_card(self, user_id: str, email_id: str, fields: Mapping[str, Any],
                         vector: Optional[Sequence[float]], expect: CardExpect) -> CardWriteResult:
        outside = sorted(set(fields) - set(CARD_FIELDS))
        if outside or "card_vector" in fields:
            raise ValueError(f"not writable by the card path: {outside or ['card_vector']}")
        data: Dict[str, Any] = dict(fields)
        data["card_vector"] = Vector(list(vector)) if vector is not None else firestore.DELETE_FIELD
        ref = self._col.document(f"{user_id}_{email_id}")

        @firestore.async_transactional
        async def _write(transaction) -> CardWriteResult:
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                return CardWriteResult.MISSING
            stored = snap.to_dict() or {}
            if (stored.get("card_version"), stored.get("people_digest")) != (expect.card_version,
                                                                              expect.people_digest):
                return CardWriteResult.CONFLICT
            transaction.update(ref, data)
            return CardWriteResult.WRITTEN

        result = await _write(self._db.transaction())
        logger.debug(f"[EmailCardStore] user={user_id[:8]} email={email_id} {result.value}")
        return result
