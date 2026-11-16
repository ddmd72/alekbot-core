"""
FirestoreEmailCardIndex — EmailCardIndex on the live facts collection (email index v2 §4, §5; search slice 8). The
second implementation of the port (the first is in memory).

The cards are extra fields on the mail's v1 document (`firestore_email_card_store`). Every read filters on `user_id`
and `card_state == "carded"` and projects the card fields only (`READ_FIELDS`): the v1 vectors and text are never read.
Each document is read back by `card_from_document`; an unreadable one is skipped (WARNING, the mail id only) and a
document of another user is skipped (ERROR), as the drive does. The composite and vector indexes are declared in
`config/firestore.indexes.json`; the vector one is created by `scripts/infrastructure/create_email_card_vector_index.sh`
(`make deploy-indexes` skips vector indexes).

One DEBUG line per read, with counts and milliseconds, never keys or content.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from google.cloud.firestore_v1.base_query import FieldFilter
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector

from ..config.environment import EnvironmentConfig
from ..domain.drive_index_params import CONCEPT_KEYS_MAX
from ..domain.email_card import CardState, EmailCard
from ..domain.email_card_document import card_from_document
from ..ports.email_card_index import KEY_FIELDS, EmailCardIndex
from ..utils.logger import logger

READ_FIELDS = ("user_id", "card_state", "card_version", "about_keys", "name_keys", "people_keys", "month_keys",
               "template_key", "card")
CARD_VECTOR = "card_vector"
CARD_DISTANCE = "card_distance"
SETTLED = tuple(s.value for s in CardState if s is not CardState.PENDING)
_USER_PREFIX_CHARS = 8


class FirestoreEmailCardIndex(EmailCardIndex):

    def __init__(self, db_client: Any, env_config: EnvironmentConfig) -> None:
        self._col = db_client.collection(env_config.domain_email_facts_collection)

    async def find_by_keys(self, user_id: str, field: str, keys: Sequence[str], limit: int) -> List[EmailCard]:
        if field not in KEY_FIELDS:
            raise ValueError(f"unknown key field {field!r}")
        return await self._contains_any(user_id, field, keys, limit)

    async def list_in_months(self, user_id: str, months: Sequence[str], limit: int) -> List[EmailCard]:
        return await self._contains_any(user_id, "month_keys", months, limit)

    async def find_nearest(self, user_id: str, vector: Sequence[float], limit: int) -> List[Tuple[EmailCard, float]]:
        if limit <= 0:
            return []
        started = time.monotonic()
        # the distance comes back under a projection only when its name is selected too (src/adapters/CLAUDE.md)
        query = self._carded(user_id).select([*READ_FIELDS, CARD_DISTANCE]).find_nearest(
            vector_field=CARD_VECTOR, query_vector=Vector([float(x) for x in vector]), limit=limit,
            distance_measure=DistanceMeasure.COSINE, distance_result_field=CARD_DISTANCE)
        hits: List[Tuple[EmailCard, float]] = []
        for snap in await query.get():
            data = snap.to_dict() or {}
            card = self._card(user_id, snap.id, data)
            if card is not None:
                hits.append((card, float(data[CARD_DISTANCE])))
        self._debug("find_nearest", user_id, len(hits), started)
        return hits

    async def count_carded(self, user_id: str) -> int:
        return await _count(self._carded(user_id))

    async def count_uncarded(self, user_id: str) -> int:
        stored = self._col.where(filter=FieldFilter("user_id", "==", user_id))
        settled = stored.where(filter=FieldFilter("card_state", "in", list(SETTLED)))
        return max(await _count(stored) - await _count(settled), 0)

    # -- implementation ---------------------------------------------------------------------------------------------

    def _carded(self, user_id: str) -> Any:
        return (self._col.where(filter=FieldFilter("user_id", "==", user_id))
                .where(filter=FieldFilter("card_state", "==", CardState.CARDED.value)))

    async def _contains_any(self, user_id: str, field: str, keys: Sequence[str], limit: int) -> List[EmailCard]:
        if len(keys) > CONCEPT_KEYS_MAX:
            raise ValueError(f"at most {CONCEPT_KEYS_MAX} keys per read, got {len(keys)}")
        if not keys or limit <= 0:
            return []
        started = time.monotonic()
        query = (self._carded(user_id).where(filter=FieldFilter(field, "array_contains_any", list(keys)))
                 .select(list(READ_FIELDS)).limit(limit))
        cards = [c for c in (self._card(user_id, snap.id, snap.to_dict() or {}) for snap in await query.get())
                 if c is not None]
        self._debug(field, user_id, len(cards), started)
        return cards

    @staticmethod
    def _card(user_id: str, doc_id: str, data: Dict[str, Any]) -> Optional[EmailCard]:
        """The document's card; None for another user's document (ERROR) or an unreadable card (WARNING)."""
        if data.get("user_id") != user_id:
            logger.error(f"[EmailCardIndex] skipped a document of another user for user={user_id[:_USER_PREFIX_CHARS]}")
            return None
        try:
            return card_from_document(data)
        except ValueError:
            email_id = doc_id[len(user_id) + 1:] if doc_id.startswith(f"{user_id}_") else doc_id
            logger.warning(f"[EmailCardIndex] unreadable card {email_id}")
            return None

    @staticmethod
    def _debug(read: str, user_id: str, found: int, started: float) -> None:
        logger.debug(f"[EmailCardIndex] read={read} user={user_id[:_USER_PREFIX_CHARS]} found={found} "
                     f"ms={int((time.monotonic() - started) * 1000)}")


async def _count(query: Any) -> int:
    result = await query.count(alias="n").get()
    return int(result[0][0].value)
